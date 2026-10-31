"""
==========================================================
FillPac AI
Production Camera Pipeline
==========================================================

Each camera owns:
- Camera
- Tracker
- Counter
- PrintDetector
- JamDetector              -> Condition A
- BagSpacingDetector       -> Condition B
- PrintROICounter (print counting, no voting)
- Latest annotated frame

YOLO is NOT loaded inside this class.

All camera pipelines use ONE shared InferenceManager.

Counting Principle
------------------
The physical bag CENTER crossing the configured counting
line is the counting trigger.

Track ID is used only for:
- temporal movement history
- print association
- jam movement analysis
- spacing pair association
- duplicate protection
- diagnostics

Track ID itself does NOT trigger a count.

Jam Detection
-------------
Condition A:
    Movement-based JamDetector.

Condition B:
    Calibrated physical edge-to-edge bag spacing.

Final Jam:
    Condition A OR Condition B

Jam detection NEVER changes the physical-center counting
logic.

Print Counting
--------------
No voting. PrintROICounter counts a print the moment it is
confirmed (print center inside the print ROI, seen on
min_frames consecutive frames, one count per bag track):

- Print counted         -> printed_count += 1 (print_roi_count)
- Counted bag leaves
  without a counted print -> missing_count += 1
==========================================================
"""
from database.repository import (
    save_print_event,
    save_roi_snapshot,
    start_jam_event,
    end_jam_event,
    get_production_summary,
    get_restore_counts,
)
from datetime import datetime, timezone

import threading
import time
import queue
from collections import deque

import cv2

from src.bag_spacing_detector import BagSpacingDetector
from src.camera import Camera
from src.counter import Counter
from src.entry_roi_counter import EntryROICounter
from src.jam_detector import JamDetector
from src.roi_occupancy_detector import ROIOccupancyDetector
from src.print_detector import PrintDetector
from src.print_roi_counter import PrintROICounter
from src.tracker import Tracker
from src.visualizer import Visualizer


class _BackgroundIOWorker:
    """Non-blocking background worker for SQL/Elasticsearch I/O.

    The camera/inference thread only places a small callable into a bounded
    queue and immediately continues. A single worker preserves event order
    for a camera while preventing network/database latency from stalling
    real-time video processing.
    """

    def __init__(self, name, logger, max_queue_size=512):
        self.name = name
        self.logger = logger
        self.queue = queue.Queue(maxsize=max_queue_size)
        self.stop_event = threading.Event()
        self._closed = False
        self.thread = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name=f"IO-{name}",
        )
        self.thread.start()

    def submit(self, func, *args, **kwargs):
        if self._closed:
            return False

        try:
            self.queue.put_nowait((func, args, kwargs))
            return True
        except queue.Full:
            self.logger.warning(
                f"{self.name}: background I/O queue full; "
                f"dropping {getattr(func, '__name__', 'event')} event."
            )
            return False

    def _worker_loop(self):
        while True:
            try:
                item = self.queue.get(timeout=0.5)
            except queue.Empty:
                if self.stop_event.is_set():
                    break
                continue

            if item is None:
                self.queue.task_done()
                break

            func, args, kwargs = item

            try:
                func(*args, **kwargs)
            except Exception as error:
                self.logger.warning(
                    f"{self.name}: background I/O operation failed: {error}"
                )
            finally:
                self.queue.task_done()

    def close(self, timeout=2.0):
        if self._closed:
            return

        self._closed = True
        self.stop_event.set()

        # Put the sentinel behind already queued work so queued production
        # events get a chance to finish during normal application shutdown.
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            # The worker is still draining. Do not block the camera thread.
            pass

        if self.thread.is_alive():
            self.thread.join(timeout=timeout)


class _AsyncServiceProxy:
    """Proxy that turns service method calls into background I/O jobs."""

    def __init__(self, service, worker):
        self._service = service
        self._worker = worker

    def __getattr__(self, name):
        method = getattr(self._service, name)

        def submit_async(*args, **kwargs):
            return self._worker.submit(method, *args, **kwargs)

        return submit_async


class Pipeline:

    # ======================================================
    # INITIALIZATION
    # ======================================================

    def __init__(
        self,
        camera_config,
        tracker_config,
        display_config,
        logger,
        inference_manager,
        dashboard_state=None,
        elasticsearch=None,
        count_logger=None,
    ):

        # ==================================================
        # BASIC CONFIGURATION
        # ==================================================

        self.camera_config = camera_config
        self.logger = logger
        self.inference_manager = inference_manager
        self.dashboard_state = dashboard_state
        self.elasticsearch = elasticsearch
        self.count_logger = count_logger

        self.name = camera_config["name"]

        # ==================================================
        # BACKGROUND I/O
        # ==================================================
        # Database / Elasticsearch operations must never block the
        # camera + inference thread. One worker is intentionally used
        # per camera so event order is preserved.
        self._io_worker = _BackgroundIOWorker(
            name=self.name,
            logger=self.logger,
            max_queue_size=int(
                camera_config.get(
                    "io_queue_size",
                    512,
                )
            ),
        )

        if self.elasticsearch is not None:
            self.elasticsearch = _AsyncServiceProxy(
                self.elasticsearch,
                self._io_worker,
            )

        if self.count_logger is not None:
            self.count_logger = _AsyncServiceProxy(
                self.count_logger,
                self._io_worker,
            )

        self.camera_id = camera_config.get(
            "id"
        )

        self.roi = camera_config["roi"]

        self.window_name = self.name

        # ==================================================
        # PER-CAMERA SWITCHES  (config.yaml)
        #
        # ai_enabled: false
        #     Stream-only camera: video is read and shown, the model
        #     is NEVER called, no counting / print / jam logic runs.
        # production.count_enabled: false
        #     Model still runs (jam / print monitoring) but the line
        #     and entry-ROI counters do not count and no count events
        #     are written for this camera.
        # production.include_bags_in_total: false
        #     Camera counts and shows its own numbers but is NOT added
        #     to the total production number.
        # production.include_prints_in_total: false
        #     Hide that camera's print counts on the dashboard and
        #     leave them out of the total printed / not printed.
        # ==================================================

        self.ai_enabled = bool(
            camera_config.get("ai_enabled", True)
        )

        production_config = (
            camera_config.get("production", {}) or {}
        )

        self.count_enabled = bool(
            production_config.get("count_enabled", True)
        )

        self.include_bags_in_total = bool(
            production_config.get("include_bags_in_total", True)
        )



        self.include_prints_in_total = bool(
            production_config.get("include_prints_in_total", True)
        )

        # Counting only happens when the model runs AND counting is on.
        self.counting_active = bool(
            self.ai_enabled and self.count_enabled
        )

        if (
            self.ai_enabled
            and self.inference_manager is None
        ):

            raise ValueError(
                f"{self.name}: Pipeline requires "
                "a valid InferenceManager "
                "(or set ai_enabled: false for a "
                "stream-only camera)."
            )

        # ==================================================
        # CAMERA MODE
        # ==================================================

        self.camera_mode = camera_config.get(
            "mode",
            "video",
        )

        # ==================================================
        # RUNTIME STATE
        # ==================================================

        self.connected = False

        self.previous_time = (
            time.perf_counter()
        )

        self.last_count = 0
        self.last_print_status = None

        # Separate ROI-entry count
        self.entry_roi_count = 0

        self.printed_count = 0
        self.missing_count = 0

        self.frame_index = 0
        self.processed_frame_count = 0

        # Performance profiling: enabled by default. Set
        # performance_monitoring: false in camera config to disable.
        self.performance_monitoring = bool(camera_config.get("performance_monitoring", True))
        self.performance_log_interval = max(float(camera_config.get("performance_log_interval", 2.0)), 0.5)

        # Frame skipping is OFF by default (1 = run YOLO on every frame).
        # Set frame_skip: 2 in a camera block to deliberately infer on
        # every 2nd frame only after measuring that the GPU can't keep up.
        self.frame_skip = max(int(camera_config.get("frame_skip", 1)), 1)

        # On-screen FPS is averaged over a rolling window (seconds).
        self.fps_window_seconds = max(float(camera_config.get("fps_window_seconds", 1.0)), 0.25)
        self._fps_window = deque()
        self._perf_wall_start = time.perf_counter()
        self._performance_last_log_time = time.perf_counter()
        self._performance_samples = 0
        self._performance_totals = {
            "read": 0.0, "yolo": 0.0, "track": 0.0, "print": 0.0,
            "jam": 0.0, "count": 0.0, "draw": 0.0, "events": 0.0,
            "total": 0.0,
        }

        self.last_error = None

        # ==================================================
        # DASHBOARD UPDATE CONTROL
        # ==================================================

        self.last_dashboard_publish_time = 0.0

        # Elasticsearch camera heartbeat is intentionally throttled.
        # Sending it for every processed frame creates unnecessary network
        # traffic and can build an I/O queue faster than Elasticsearch can
        # consume it.
        self.last_elasticsearch_camera_event_time = 0.0

        self.elasticsearch_camera_event_interval = max(
            float(
                camera_config.get(
                    "elasticsearch_camera_event_interval",
                    1.0,
                )
            ),
            0.1,
        )

        self.dashboard_publish_interval = max(
            float(
                camera_config.get(
                    "dashboard_publish_interval",
                    0.5,
                )
            ),
            0.0,
        )

        # ==================================================
        # INFERENCE CONFIGURATION
        # ==================================================

        self.inference_timeout = max(
            float(
                camera_config.get(
                    "inference_timeout",
                    5.0,
                )
            ),
            0.1,
        )

        # ==================================================
        # CONFIGURATION
        # ==================================================

        counting_config = (
            camera_config.get(
                "counting",
                {},
            )
            or {}
        )

        # ==================================================
        # ENTRY ROI COUNTING CONFIGURATION
        # ==================================================

        entry_roi_counting_config = (
            camera_config.get(
                "entry_roi_counting",
                {},
            )
            or {}
        )

        print_config = (
            camera_config.get(
                "print_detection",
                {},
            )
            or {}
        )

        jam_config = (
            camera_config.get(
                "jam_detection",
                {},
            )
            or {}
        )

        spacing_config = (
            camera_config.get(
                "bag_spacing",
                {},
            )
            or {}
        )

        condition_c_config = (
            camera_config.get(
                "condition_c",
                {},
            )
            or {}
        )

        # ==================================================
        # DISPLAY CONFIGURATION
        # ==================================================

        self.display_config = dict(
            display_config or {}
        )

        self.display_config.update(
            camera_config.get(
                "display",
                {},
            )
            or {}
        )

        # ==================================================
        # CAMERA
        # ==================================================

        self.camera = Camera(
            name=self.name,
            source=camera_config.get(
                "source"
            ),
            mode=self.camera_mode,
            buffer_size=camera_config.get(
                "buffer_size",
                1,
            ),
            reconnect_attempts=camera_config.get(
                "reconnect_attempts",
                5,
            ),
            reconnect_delay=camera_config.get(
                "reconnect_delay",
                2.0,
            ),
            realtime_playback=bool(
                camera_config.get(
                    "realtime_playback",
                    False,
                )
            ),
            logger=self.logger,
        )

        # ==================================================
        # TRACKER
        # ==================================================

        self.tracker = Tracker(
            tracker_config=tracker_config,
        )

        # ==================================================
        # COUNTER
        # ==================================================

        counting_roi = camera_config.get(
            "roi"
        )

        if not isinstance(counting_roi, dict):
            raise ValueError(
                f"{self.name}: counting ROI must be a "
                f"dictionary with x1,y1,x2,y2. "
                f"Got: {counting_roi}"
            )

        required_roi_keys = {
            "x1",
            "y1",
            "x2",
            "y2",
        }

        missing_keys = (
            required_roi_keys
            - set(counting_roi.keys())
        )

        if missing_keys:
            raise ValueError(
                f"{self.name}: counting ROI is missing "
                f"{sorted(missing_keys)}"
            )

        self.counter = Counter(

            roi=counting_roi,

            direction=counting_config.get(
                "direction",
                "down",
            ),

            duplicate_distance=counting_config.get(
                "duplicate_distance",
                40,
            ),

            duplicate_time=counting_config.get(
                "duplicate_time",
                0.8,
            ),

            max_history=counting_config.get(
                "max_history",
                200,
            ),

            line_tolerance=counting_config.get(
                "line_tolerance",
                20,
            ),

            late_start_margin=counting_config.get(
                "late_start_margin",
                40,
            ),

            min_track_frames=counting_config.get(
                "min_track_frames",
                4,
            ),

            stale_track_frames=counting_config.get(
                "stale_track_frames",
                120,
            ),

            minimum_cross_distance=counting_config.get(
                "minimum_cross_distance",
                counting_config.get(
                    "min_cross_distance",
                    0,
                ),
            ),
        )

        # ==================================================
        # SEPARATE ENTRY ROI BAG COUNTER
        # ==================================================

        self.entry_roi_counting_enabled = bool(
            entry_roi_counting_config.get(
                "enabled",
                False,
            )
        )

        entry_roi = (
            entry_roi_counting_config.get(
                "roi",
                {},
            )
            or {}
        )

        if self.entry_roi_counting_enabled:

            self.entry_roi_counter = EntryROICounter(
                roi=entry_roi,

                min_track_frames=entry_roi_counting_config.get(
                    "min_track_frames",
                    4,
                ),

                duplicate_distance=entry_roi_counting_config.get(
                    "duplicate_distance",
                    50,
                ),

                max_history=entry_roi_counting_config.get(
                    "max_history",
                    300,
                ),
            )

        else:

            self.entry_roi_counter = None

        # ==================================================
        # PRINT DETECTION
        # ==================================================

        self.print_detection_enabled = bool(
            print_config.get(
                "enabled",
                False,
            )
        )

        self.print_detector = PrintDetector(
            confidence_threshold=print_config.get(
                "confidence_threshold",
                print_config.get(
                    "confidence",
                    0.4,
                ),
            ),
            iou_threshold=print_config.get(
                "iou_threshold",
                0.0,
            ),
            min_overlap_ratio=print_config.get(
                "min_overlap_ratio",
                0.3,
            ),
            min_print_area=print_config.get(
                "min_print_area",
                0.0,
            ),
            max_print_area=print_config.get(
                "max_print_area",
                0.0,
            ),
            min_aspect_ratio=print_config.get(
                "min_aspect_ratio",
                0.0,
            ),
            max_aspect_ratio=print_config.get(
                "max_aspect_ratio",
                0.0,
            ),
            max_center_distance=print_config.get(
                "max_center_distance",
                0.0,
            ),
        )

        # ==================================================
        # PRINT ROI COUNTING (replaces the old print voting)
        # ==================================================

        print_roi_counting_config = (
            camera_config.get(
                "print_roi_counting",
                {},
            )
            or {}
        )

        self.print_roi_counting_enabled = bool(
            self.print_detection_enabled
            and print_roi_counting_config.get(
                "enabled",
                True,
            )
        )

        if self.print_roi_counting_enabled:

            self.print_roi_counter = PrintROICounter(
                roi=print_roi_counting_config.get(
                    "roi"
                ),
                min_frames=print_roi_counting_config.get(
                    "min_frames",
                    2,
                ),
                min_confidence=print_roi_counting_config.get(
                    "min_confidence",
                    0.0,
                ),
                max_history=print_roi_counting_config.get(
                    "max_history",
                    300,
                ),
            )

        else:

            self.print_roi_counter = None

        # A counted bag is reported as MISSING once it has been
        # unseen for this many frames without a counted print
        # (max is a safety net for tracks that never disappear).
        self.print_missing_grace_frames = max(
            int(
                print_roi_counting_config.get(
                    "missing_grace_frames",
                    10,
                )
            ),
            1,
        )

        self.print_missing_max_frames = max(
            int(
                print_roi_counting_config.get(
                    "missing_max_frames",
                    450,
                )
            ),
            1,
        )

        # ==================================================
        # JAM DETECTION - CONDITION A
        #
        # Existing movement/time based detector.
        # ==================================================

        self.jam_detector = JamDetector(
            config=jam_config,
        )

        self.jam_detection_enabled = bool(
            self.jam_detector.enabled
        )

        self.jam_roi = dict(
            self.jam_detector.roi
        )

        self.jam_result = (
            self.jam_detector._empty_result()
        )

        # ==================================================
        # BAG SPACING - CONDITION B
        #
        # No timer.
        #
        # Physical calibrated edge-to-edge distance:
        #
        # gap <= threshold -> JAM
        # ==================================================

        self.bag_spacing_detector = (
            BagSpacingDetector(
                config=spacing_config,
            )
        )

        self.bag_spacing_enabled = bool(
            self.bag_spacing_detector.enabled
        )

        self.spacing_roi = dict(
            self.bag_spacing_detector.roi
        )

        self.spacing_result = (
            self.bag_spacing_detector._empty_result()
        )

        # ==================================================
        # CONDITION C - ROI OCCUPANCY JAM
        # ==================================================

        self.condition_c_detector = (
            ROIOccupancyDetector(
                config=condition_c_config,
            )
        )

        self.condition_c_enabled = bool(
            self.condition_c_detector.enabled
        )

        self.condition_c_roi = dict(
            self.condition_c_detector.roi
        )

        self.condition_c_result = {

            "jam": False,

            "status": "normal",

            "bag_count": 0,

            "track_ids": [],

            "minimum_gap_mm": None,

            "distances": [],

            "image_path": None,
        }

        self.condition_c_start_time = None

        # ==================================================
        # STREAM-ONLY CAMERA (ai_enabled: false)
        # Report every AI feature as disabled to the dashboard.
        # ==================================================

        if not self.ai_enabled:

            self.print_detection_enabled = False
            self.jam_detection_enabled = False
            self.bag_spacing_enabled = False
            self.condition_c_enabled = False

            self.logger.info(
                f"{self.name}: ai_enabled=false -> STREAM ONLY "
                "(model, counting, print and jam detection "
                "are off for this camera)."
            )

        elif not self.count_enabled:

            self.logger.info(
                f"{self.name}: production.count_enabled=false -> "
                "bag counting is off for this camera."
            )

        # ==================================================
        # FINAL JAM RESULT
        #
        # Condition A OR Condition B
        # ==================================================

        self.final_jam_result = (
            self._build_final_jam_result()
        )

        # ==================================================
        # JAM EVENT LOGGING STATE
        # ==================================================

        self.previous_jam_detected = False
        self.previous_jam_types = set()

        self.previous_condition_a_jam = False
        self.previous_condition_b_jam = False
        self.previous_condition_c_jam = False

        # SQL Server jam event tracking
        self.condition_a_sql_jam_id = None
        self.condition_a_sql_jam_start = None

        self.condition_b_sql_jam_id = None
        self.condition_b_sql_jam_start = None

        self.condition_c_sql_jam_id = None
        self.condition_c_sql_jam_start = None

        # ==================================================
        # VISUALIZER
        # ==================================================

        self.visualizer = Visualizer()

        # ==================================================
        # PRINT ROI COUNTING STATE
        # ==================================================

        self.print_roi_count = 0

        # Prints counted by PrintROICounter in the CURRENT frame.
        self._print_counted_this_frame = []

        # Entry-counted bags waiting to be reported printed/missing.
        self._pending_print = {}
        self._resolved_print_results = []
        self._active_track_ids = set()

        # ==================================================
        # FRAME SHARING
        # ==================================================

        self._frame_lock = threading.Lock()

        self._latest_frame = None

        # Incremented on every new annotated frame so consumers
        # (local display window) can skip frames they already showed.
        self._latest_frame_seq = 0
        self._published_seq = -1

        # ==================================================
        # RESTORE PREVIOUS COUNTS
        # ==================================================

        self._counts_restored_from_sql = False
        self._last_restore_attempt = time.monotonic()
        self._camera_registered = False
        self._restore_in_flight = False
        self._pending_restore = None

        self._restore_persisted_counts()

        # ==================================================
        # REGISTER CAMERA
        # ==================================================

        self._register_dashboard_camera()
        self._camera_registered = True

    # ======================================================
    # RESTORE PERSISTED COUNTS
    # ======================================================

    def _restore_persisted_counts(
        self,
        prefetched=None,
    ):
        """
        Restore persistent production/ROI counters so a restart
        CONTINUES the old count instead of starting from 0.

        Source of truth: dbo.production_events (one row per bag).
        The dashboard snapshot is only a fallback while SQL is
        unreachable.

        Safe to call repeatedly: counters are only ever raised
        (max of current and stored), never lowered, so a late
        retry can't undo bags counted since startup.

        Sets self._counts_restored_from_sql = True once SQL has
        been read successfully (even if it had no rows yet);
        until then _retry_restore_counts() keeps trying.
        """

        sql_count = None
        sql_printed = 0
        sql_missing = 0

        try:
            sql_summary = (
                prefetched
                if prefetched is not None
                else get_restore_counts(
                    self.name,
                )
            )

            if isinstance(sql_summary, dict):
                sql_count = int(sql_summary.get("total", 0) or 0)
                sql_printed = int(sql_summary.get("printed", 0) or 0)
                sql_missing = int(sql_summary.get("missing", 0) or 0)

        except Exception as sql_error:
            self.logger.error(
                f"{self.name}: could not read stored counts from "
                f"SQL Server, will retry: {sql_error}"
            )

        fallback_count = 0
        fallback_printed = 0
        fallback_missing = 0

        if sql_count is None and self.dashboard_state is not None:

            try:
                camera_state = (
                    (self.dashboard_state.snapshot() or {})
                    .get("cameras", {})
                    .get(self.name, {})
                ) or {}

                fallback_count = int(
                    camera_state.get(
                        "entry_roi_count",
                        camera_state.get(
                            "total_count",
                            camera_state.get("count", 0),
                        ),
                    ) or 0
                )
                fallback_printed = int(
                    camera_state.get(
                        "printed_count",
                        camera_state.get("printed_bags_count", 0),
                    ) or 0
                )
                fallback_missing = int(
                    camera_state.get(
                        "missing_count",
                        camera_state.get("not_printed_bags_count", 0),
                    ) or 0
                )

            except Exception as snap_error:
                self.logger.warning(
                    f"{self.name}: dashboard snapshot fallback "
                    f"unavailable: {snap_error}"
                )

        if sql_count is not None:
            target_count = sql_count
            target_printed = sql_printed
            target_missing = sql_missing
            source = "SQL production_events"
            self._counts_restored_from_sql = True

        else:
            target_count = fallback_count
            target_printed = fallback_printed
            target_missing = fallback_missing
            source = "dashboard snapshot fallback"

        # Never lower a counter that already moved this run.
        current = max(
            int(self.entry_roi_count or 0),
            int(
                getattr(self.entry_roi_counter, "total_count", 0) or 0
            ),
        )

        restored_count = max(target_count, current)
        restored_printed = max(target_printed, int(self.printed_count or 0))
        restored_missing = max(target_missing, int(self.missing_count or 0))

        if self.counter is not None:
            self.counter.total_count = max(
                restored_count,
                int(getattr(self.counter, "total_count", 0) or 0),
            )

        self.last_count = restored_count

        if self.entry_roi_counter is not None:
            self.entry_roi_counter.total_count = restored_count

        self.entry_roi_count = restored_count
        self.printed_count = restored_printed
        self.missing_count = restored_missing
        self.print_roi_count = restored_printed

        if self.print_roi_counter is not None:
            self.print_roi_counter.total_count = restored_printed

        self.logger.info(
            f"{self.name}: restored persistent production "
            f"count={restored_count}, "
            f"printed={restored_printed}, "
            f"missing={restored_missing}, "
            f"source={source}"
        )

        # Push to the dashboard now (a retry happens after
        # registration, so the screen must not wait for the next
        # bag to show the real numbers).
        if self.dashboard_state is not None and self._camera_registered:

            try:
                self.dashboard_state.update_camera(
                    camera_name=self.name,
                    count=restored_count,
                    total_count=restored_count,
                    entry_roi_count=restored_count,
                    line_count=int(
                        getattr(
                            self.counter,
                            "total_count",
                            restored_count,
                        ) or 0
                    ),
                    printed_count=restored_printed,
                    print_roi_count=restored_printed,
                    missing_count=restored_missing,
                )

            except Exception as push_error:
                self.logger.warning(
                    f"{self.name}: could not push restored counts "
                    f"to dashboard: {push_error}"
                )

    def _retry_restore_counts(
        self,
    ):
        """
        If SQL Server wasn't reachable when the camera started,
        keep retrying until the stored counts have been read once.

        Called from the frame loop, so it never touches SQL itself:
        the read runs on this camera's background I/O worker (at
        most every 10 s) and the camera thread only applies the
        result. A slow or blocked database can therefore never
        stall frame processing, for this camera or any other.
        """

        if self._counts_restored_from_sql:
            return

        now = time.monotonic()

        if (
            not self._restore_in_flight
            and now - self._last_restore_attempt >= 10.0
        ):

            self._last_restore_attempt = now
            self._restore_in_flight = True

            if not self._io_worker.submit(self._restore_worker_job):
                self._restore_in_flight = False

    def _restore_worker_job(
        self,
    ):
        """Runs on the I/O worker thread: read SQL only."""

        try:
            self._pending_restore = get_restore_counts(self.name)

        except Exception as error:
            self.logger.warning(
                f"{self.name}: stored counts still unavailable: "
                f"{error}"
            )

        finally:
            self._restore_in_flight = False

    def _apply_pending_restore(
        self,
    ):
        """Camera thread: apply counts the worker fetched."""

        pending = self._pending_restore

        if pending is None or self._counts_restored_from_sql:
            return

        self._pending_restore = None

        self._restore_persisted_counts(prefetched=pending)

    # ======================================================
    # REGISTER CAMERA
    # ======================================================

    def _register_dashboard_camera(
        self,
    ):

        if self.dashboard_state is None:
            return

        try:

            self.dashboard_state.register_camera(

                camera_name=self.name,

                camera_id=self.camera_id,

                enabled=self.camera_config.get(
                    "enabled",
                    True,
                ),

                configured=True,

                mode=self.camera_mode,

                print_detection_enabled=(
                    self.print_detection_enabled
                ),
                jam_detection_enabled=(
                    self.jam_detection_enabled
                ),
                source_type=self.camera_mode,
                ai_enabled=self.ai_enabled,
                count_enabled=self.count_enabled,
                include_bags_in_total=self.include_bags_in_total,

                include_prints_in_total=self.include_prints_in_total,

                metadata={

                    "roi":
                        self.roi,

                    "counting_method":
                        "physical_center",

                    "counting_direction":
                        self.camera_config
                        .get(
                            "counting",
                            {},
                        )
                        .get(
                            "direction",
                            "down",
                        ),

                    # Condition A
                    "jam_detection_enabled":
                        self.jam_detection_enabled,

                    "jam_roi":
                        self.jam_roi,

                    # Condition B
                    "bag_spacing_enabled":
                        self.bag_spacing_enabled,

                    "spacing_roi":
                        self.spacing_roi,

                    "spacing_threshold_mm":
                        self.bag_spacing_detector
                        .jam_threshold_mm,

                    "condition_c_enabled":
                        self.condition_c_enabled,

                    "condition_c_roi":
                        self.condition_c_roi,
                },
            )

            self.dashboard_state.update_camera(

                camera_name=self.name,

                count=(
                    self.entry_roi_counter.total_count
                    if self.entry_roi_counter is not None
                    else 0
                ),

                total_count=(
                    self.entry_roi_counter.total_count
                    if self.entry_roi_counter is not None
                    else 0
                ),
                entry_roi_count=(
                    self.entry_roi_counter.total_count
                    if self.entry_roi_counter is not None
                    else 0
                ),
                line_count=self.counter.total_count,
                printed_count=self.printed_count,
                missing_count=self.missing_count,
                print_roi_count=self.print_roi_count,
                print_detection_enabled=(
                    self.print_detection_enabled
                ),
                print_status=(
                    "unknown"
                    if self.print_detection_enabled
                    else "disabled"
                ),

                status="offline",

                fps=0.0,

                frame_count=0,

                # Condition A
                movement_jam_enabled=(
                    self.jam_detection_enabled
                ),

                movement_jam_status=(
                    "normal"
                    if self.jam_detection_enabled
                    else "disabled"
                ),

                movement_jam_detected=False,

                movement_jam_warning=False,

                movement_jam_track_ids=[],

                # Condition B
                spacing_detection_enabled=(
                    self.bag_spacing_enabled
                ),

                spacing_status=(
                    "normal"
                    if self.bag_spacing_enabled
                    else "disabled"
                ),

                spacing_jam_detected=False,

                spacing_threshold_mm=(
                    self.bag_spacing_detector
                    .jam_threshold_mm
                ),

                minimum_gap_mm=None,

                spacing_pairs=[],

                spacing_jam_pairs=[],

                spacing_jam_track_ids=[],

                # ==========================================
                # CONDITION C
                #
                # ROI Occupancy Detector
                #
                # Publishes:
                # - ROI Bag Count
                # - Track IDs
                # - Minimum Gap
                # - Distances
                # - ROI Image
                # - ROI Coordinates
                # - Max Allowed Bags / Occupancy %
                # - Jam Timestamp / Duration
                # ==========================================
                condition_c_enabled=(
                    self.condition_c_enabled
                ),

                condition_c_status=(
                    "normal"
                    if self.condition_c_enabled
                    else "disabled"
                ),

                condition_c_detected=False,

                condition_c_bag_count=0,

                condition_c_track_ids=[],

                condition_c_minimum_gap_mm=None,

                condition_c_distances=[],

                condition_c_image_path=None,

                condition_c_roi=(
                    self.condition_c_roi
                ),

                condition_c_max_allowed_bags=(
                    self.condition_c_detector
                    .max_allowed_bags
                ),

                condition_c_roi_occupancy=0,

                condition_c_timestamp=None,

                condition_c_duration=0,

                # Final combined jam
                jam_detection_enabled=bool(
                    self.jam_detection_enabled
                    or
                    self.bag_spacing_enabled
                    or
                    self.condition_c_enabled
                ),

                jam_status="normal",

                jam_detected=False,

                jam_warning=False,

                jam_types=[],

                active_jam_count=0,

                active_jam_track_ids=[],
            )

        except Exception as error:

            self.logger.warning(
                f"{self.name}: dashboard camera "
                f"registration failed: {error}"
            )

    # ======================================================
    # PERFORMANCE PROFILING
    # ======================================================

    def _record_performance(self, stage, elapsed_seconds):
        if not self.performance_monitoring or stage not in self._performance_totals:
            return
        self._performance_totals[stage] += max(float(elapsed_seconds), 0.0)

    def _log_performance(self):
        if not self.performance_monitoring:
            return
        self._performance_samples += 1
        now = time.perf_counter()
        if now - self._performance_last_log_time < self.performance_log_interval:
            return
        samples = max(self._performance_samples, 1)
        avg_ms = {k: (v / samples) * 1000.0 for k, v in self._performance_totals.items()}
        total_ms = avg_ms["total"]
        # MAX_FPS: what the processing stages could sustain (1 / busy time).
        # FPS: what was actually achieved over wall-clock time. A large gap
        # means the loop is waiting (video pacing, locks, queues) rather
        # than computing.
        max_fps = 1000.0 / total_ms if total_ms > 0.0 else 0.0
        wall = max(now - self._perf_wall_start, 1e-6)
        processing_fps = self._performance_samples / wall
        self.logger.info(
            f"{self.name} PERFORMANCE | FPS={processing_fps:.2f} | MAX_FPS={max_fps:.2f} | TOTAL={total_ms:.1f}ms | "
            f"READ={avg_ms['read']:.1f}ms | YOLO={avg_ms['yolo']:.1f}ms | "
            f"TRACK={avg_ms['track']:.1f}ms | PRINT={avg_ms['print']:.1f}ms | "
            f"JAM={avg_ms['jam']:.1f}ms | COUNT={avg_ms['count']:.1f}ms | "
            f"DRAW={avg_ms['draw']:.1f}ms | EVENTS={avg_ms['events']:.1f}ms"
        )
        self._performance_samples = 0
        for key in self._performance_totals:
            self._performance_totals[key] = 0.0
        self._performance_last_log_time = now
        self._perf_wall_start = now

    # PROCESS FRAME
    # ======================================================

    def process(
        self,
    ):

        try:

            # ==============================================
            # CONNECT CAMERA
            # ==============================================

            if not self.connected:

                self.connected = (
                    self.camera.connect()
                )

                if not self.connected:

                    self._publish_runtime_status(
                        fps=0.0,
                        print_status="offline",
                        runtime_status="offline",
                        force=True,
                    )

                    return False

                self.previous_time = (
                    time.perf_counter()
                )

                self._fps_window.clear()

                self.last_error = None

                self._publish_runtime_status(
                    fps=0.0,
                    print_status=(
                        "unknown"
                        if self.print_detection_enabled
                        else "disabled"
                    ),
                    runtime_status="online",
                    force=True,
                )

            # ==============================================
            # READ FRAME
            # ==============================================

            _perf_total_start = time.perf_counter()
            _perf_stage_start = _perf_total_start
            frame = self.read_frame()
            self._record_performance("read", time.perf_counter() - _perf_stage_start)

            if frame is None:

                self.release()

                self._publish_runtime_status(
                    fps=0.0,
                    print_status="offline",
                    runtime_status="offline",
                    force=True,
                )

                return False

            self.frame_index += 1

            # ==============================================
            # STREAM ONLY (ai_enabled: false)
            # No model call, tracking, counting, print or jam
            # logic -- just keep the video moving.
            # ==============================================

            if not self.ai_enabled:

                return self._process_stream_only(
                    frame,
                    _perf_total_start,
                )

            # ==============================================
            # OPTIONAL FRAME SKIP (disabled by default)
            # ==============================================

            if (
                self.frame_skip > 1
                and self.frame_index % self.frame_skip != 0
            ):
                # Deliberate, configured skip only (frame_skip in
                # camera config). Default is no skipping.
                return True

            self.processed_frame_count += 1

            # ==============================================
            # YOLO
            # ==============================================

            _perf_stage_start = time.perf_counter()
            detections = self.detect(
                frame
            )
            self._record_performance("yolo", time.perf_counter() - _perf_stage_start)

            # ==============================================
            # TRACKER FRAME SIZE
            # ==============================================

            height, width = (
                frame.shape[:2]
            )

            self.tracker.set_frame_size(
                width=width,
                height=height,
            )

            # ==============================================
            # TRACK BAGS
            # ==============================================

            _perf_stage_start = time.perf_counter()
            tracks = self.track(
                detections
            )
            self._record_performance("track", time.perf_counter() - _perf_stage_start)

            # ==============================================
            # PRINT DETECTION
            # ==============================================

            _perf_stage_start = time.perf_counter()
            print_results = (
                self.print_detection(
                    tracks,
                    detections,
                )
            )

            self.update_print_roi_count(
                print_results,
                tracks,
            )
            self._record_performance("print", time.perf_counter() - _perf_stage_start)

            # ==============================================
            # CONDITION A - MOVEMENT JAM
            # ==============================================

            _perf_stage_start = time.perf_counter()
            self.jam_result = (
                self.jam_detector.update(
                    tracks,
                    timestamp=time.perf_counter(),
                )
            )

            # ==============================================
            # CONDITION B - BAG SPACING JAM
            #
            # No timer.
            # Uses current live tracks.
            # ==============================================

            self.spacing_result = (
                self.bag_spacing_detector.update(
                    tracks
                )
            )

            # ==============================================
            # CONDITION C
            # ==============================================

            try:

                self.condition_c_result = (
                    self.condition_c_detector.process(
                        frame=frame,
                        tracks=tracks,
                        spacing_result=self.spacing_result,
                        camera_name=self.name,
                    )
                )

            except Exception as error:

                import traceback

                self.logger.error(
                    f"{self.name}: Condition C failed: {error}"
                )

                traceback.print_exc()

                self.condition_c_result = {

                    "jam": False,

                    "status": "normal",

                    "bag_count": 0,

                    "track_ids": [],

                    "minimum_gap_mm": None,

                    "distances": [],

                    "image_path": None,
                }

            if self.condition_c_result.get("jam", False):

                if self.condition_c_start_time is None:

                    self.condition_c_start_time = (
                        time.perf_counter()
                    )

            else:

                self.condition_c_start_time = None

            # ==============================================
            # FINAL JAM
            #
            # A OR B OR C
            # ==============================================

            self.final_jam_result = (
                self._build_final_jam_result()
            )
            self._record_performance("jam", time.perf_counter() - _perf_stage_start)

            # ==============================================
            # PERSIST JAM EVENTS
            # ==============================================

            self._log_jam_events()

            # ==============================================
            # FILTER COUNTING TRACKS
            # ==============================================

            _perf_stage_start = time.perf_counter()
            countable_tracks = (
                self.countable_tracks(
                    tracks
                )
            )

            # ==============================================
            # EXISTING PHYSICAL-CENTER LINE COUNTING
            # ==============================================

            if self.counting_active:

                count = self.count(
                    countable_tracks
                )

            else:

                # production.count_enabled: false -> counter is
                # not fed and keeps its last value.
                count = self.counter.total_count

            # ==============================================
            # NEW ENTRY ROI COUNTING
            #
            # OUTSIDE → INSIDE ROI = +1
            # ==============================================

            self._apply_pending_restore()
            self._retry_restore_counts()

            if (
                self.entry_roi_counter is not None
                and self.counting_active
            ):
                self.entry_roi_count = (
                    self.entry_roi_counter.update(
                        countable_tracks
                    )
                )

            # ==============================================
            # ENTRY ROI COUNT EVENTS
            # ==============================================

            if (
                self.entry_roi_counter is not None
                and self.entry_roi_counter.last_counted_bags
            ):

                for roi_bag in (
                    self.entry_roi_counter.last_counted_bags
                ):

                    track_id = roi_bag.get("track_id")
                    center = roi_bag.get("center")

                    print(
                        f"[ENTRY ROI COUNT +1] "
                        f"Track ID={track_id} "
                        f"Center={center} "
                        f"TOTAL={self.entry_roi_count}"
                    )

                    # ------------------------------------------
                    # PERSIST ROI EVENT
                    # ------------------------------------------
                    if self.count_logger is not None:

                        try:

                            self.count_logger.log_event(
                                event_type="roi_entry",
                                camera_name=self.name,
                                track_id=track_id,
                                center=center,
                                roi=self.entry_roi_counter.roi,
                                roi_count=self.entry_roi_count,
                            )

                        except Exception as error:

                            self.logger.warning(
                                f"{self.name}: "
                                f"failed to log ROI entry event: "
                                f"{error}"
                            )

            self._record_performance("count", time.perf_counter() - _perf_stage_start)

            entry_roi_count = (
                self.entry_roi_counter.total_count
                if self.entry_roi_counter is not None
                else 0
            )

            # ==============================================
            # FPS
            # ==============================================

            fps = self.calculate_fps()

            # ==============================================
            # VISUALIZATION
            # ==============================================

            _perf_stage_start = time.perf_counter()
            self.draw(
                frame,
                tracks,
                detections,
                print_results,
                count,
                fps,
                entry_roi_count,
            )

            self._set_latest_frame(
                frame
            )
            self._record_performance("draw", time.perf_counter() - _perf_stage_start)

            # ==============================================
            # EVENTS
            # ==============================================

            _perf_stage_start = time.perf_counter()
            self._publish_events(
                count,
                fps,
                print_results,
            )
            self._record_performance("events", time.perf_counter() - _perf_stage_start)
            self._record_performance("total", time.perf_counter() - _perf_total_start)
            self._log_performance()

            self.last_error = None

            return True

        except TimeoutError as error:

            self.last_error = str(
                error
            )

            self.logger.warning(
                f"{self.name} inference timeout: "
                f"{error}"
            )

            self._publish_runtime_status(
                fps=0.0,
                print_status="inference_timeout",
                runtime_status="online",
                force=True,
            )

            return True

        except Exception as error:

            self.last_error = str(
                error
            )

            self.logger.error(
                f"Unhandled exception in pipeline "
                f"{self.name}: {error}"
            )

            self.release()

            self._publish_runtime_status(
                fps=0.0,
                print_status="error",
                runtime_status="error",
                force=True,
            )

            return False

    # ======================================================
    # STREAM-ONLY FRAME (ai_enabled: false)
    # ======================================================

    def _process_stream_only(
        self,
        frame,
        perf_total_start,
    ):
        """
        Handle one frame for a camera with ai_enabled: false.
        The model, tracker, counters, print and jam detectors are
        never touched.
        """

        self.processed_frame_count += 1

        # Keep restored (historical) counts available.
        self._apply_pending_restore()
        self._retry_restore_counts()

        fps = self.calculate_fps()

        try:
            cv2.putText(
                frame,
                f"{self.name}  |  STREAM ONLY (AI OFF)",
                (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.0,
                (0, 215, 255),
                2,
                cv2.LINE_AA,
            )

            if self.display_config.get("show_fps", True):
                cv2.putText(
                    frame,
                    f"FPS: {fps:.1f}",
                    (20, 80),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (0, 255, 0),
                    2,
                    cv2.LINE_AA,
                )
        except Exception:
            pass

        self._set_latest_frame(frame)

        self._publish_runtime_status(
            fps=fps,
            print_status="disabled",
            runtime_status="online",
            frame_processed=True,
        )

        if self.elasticsearch is not None:

            now_monotonic = time.monotonic()

            if (
                now_monotonic
                - self.last_elasticsearch_camera_event_time
                >= self.elasticsearch_camera_event_interval
            ):
                self.last_elasticsearch_camera_event_time = (
                    now_monotonic
                )
                self.elasticsearch.create_camera_event(
                    self.name,
                    fps,
                    "online",
                )

        self._record_performance(
            "total",
            time.perf_counter() - perf_total_start,
        )
        self._log_performance()

        self.last_error = None

        return True

    # ======================================================
    # CONDITION C RESULT ACCESSOR
    # ======================================================

    def _condition_c(
        self,
    ):

        return (
            self.condition_c_result
            if isinstance(
                self.condition_c_result,
                dict,
            )
            else {}
        )

    # ======================================================
    # ENTRY ROI STATE
    # ======================================================

    def _get_entry_roi_state(self):
        """
        Return current bags whose physical center
        is inside the Entry ROI.
        """

        if self.entry_roi_counter is None:
            return {
                "count": 0,
                "track_ids": [],
                "bags": [],
            }

        bags = []

        for track_id, inside in (
            self.entry_roi_counter.track_inside_roi.items()
        ):

            if not inside:
                continue

            center = (
                self.entry_roi_counter.track_centers.get(
                    track_id
                )
            )

            if center is None:
                continue

            bags.append(
                {
                    "track_id": track_id,
                    "center": [
                        float(center[0]),
                        float(center[1]),
                    ],
                }
            )

        return {
            "count": len(bags),
            "track_ids": [
                bag["track_id"]
                for bag in bags
            ],
            "bags": bags,
        }

    # ======================================================
    # BUILD FINAL JAM RESULT
    # ======================================================

    def _build_final_jam_result(
        self,
    ):

        movement = (
            self.jam_result
            if isinstance(
                self.jam_result,
                dict,
            )
            else {}
        )

        spacing = (
            self.spacing_result
            if isinstance(
                self.spacing_result,
                dict,
            )
            else {}
        )

        condition_c = self._condition_c()

        movement_detected = bool(
            movement.get(
                "jam_detected",
                False,
            )
        )

        movement_warning = bool(
            movement.get(
                "warning",
                False,
            )
        )

        spacing_detected = bool(
            spacing.get(
                "jam_detected",
                False,
            )
        )

        condition_c_detected = bool(
            condition_c.get(
                "jam",
                False,
            )
        )

        jam_detected = bool(
            movement_detected
            or
            spacing_detected
            or
            condition_c_detected
        )

        jam_types = []

        if movement_detected:

            jam_types.append(
                "movement"
            )

        if spacing_detected:

            jam_types.append(
                "bag_spacing"
            )

        if condition_c_detected:

            jam_types.append(
                "roi_occupancy"
            )

        movement_ids = (
            movement.get(
                "active_jam_track_ids",
                [],
            )
            or []
        )

        spacing_ids = (
            spacing.get(
                "active_jam_track_ids",
                [],
            )
            or []
        )

        condition_c_ids = (
            condition_c.get(
                "track_ids",
                [],
            )
            or []
        )

        active_ids = sorted(
            {
                int(track_id)
                for track_id
                in (
                    list(movement_ids)
                    +
                    list(spacing_ids)
                    +
                    list(condition_c_ids)
                )
                if track_id is not None
            }
        )

        if jam_detected:

            status = "jam"

        elif movement_warning:

            status = "warning"

        elif (
            self.jam_detection_enabled
            or
            self.bag_spacing_enabled
            or
            self.condition_c_enabled
        ):

            status = "normal"

        else:

            status = "disabled"

        return {

            "status":
                status,

            "jam_detected":
                jam_detected,

            "warning":
                (
                    movement_warning
                    and
                    not jam_detected
                ),

            "jam_types":
                jam_types,

            "movement_jam_detected":
                movement_detected,

            "spacing_jam_detected":
                spacing_detected,

            "active_jam_count":
                len(active_ids),

            "active_jam_track_ids":
                active_ids,

            "minimum_gap_mm":
                spacing.get(
                    "minimum_gap_mm"
                ),

            "spacing_threshold_mm":
                spacing.get(
                    "threshold_mm",
                    self.bag_spacing_detector
                    .jam_threshold_mm,
                ),

            "spacing_jam_pairs":
                spacing.get(
                    "jam_pairs",
                    [],
                )
                or [],

            "condition_c_bag_count":
                condition_c.get(
                    "bag_count",
                    0,
                ),

            "condition_c_track_ids":
                condition_c.get(
                    "track_ids",
                    [],
                )
                or [],

            "condition_c_minimum_gap_mm":
                condition_c.get(
                    "minimum_gap_mm"
                ),

            "condition_c_distances":
                condition_c.get(
                    "distances",
                    [],
                )
                or [],

            "condition_c_image_path":
                condition_c.get(
                    "image_path"
                ),
        }

    # ======================================================
    # JAM EVENT LOGGER
    # ======================================================

    def _log_jam_events(self):

        if self.count_logger is None:
            return

        try:

            # ==================================================
            # CONDITION A - MOVEMENT JAM
            # ==================================================

            condition_a_jam = bool(
                self.jam_result.get(
                    "jam",
                    False,
                )
            )

            if condition_a_jam != self.previous_condition_a_jam:

                # --------------------------------------------------
                # JAM START
                # --------------------------------------------------

                if condition_a_jam:

                    try:

                        self.condition_a_sql_jam_start = (
                            datetime.now(timezone.utc)
                        )

                        self.condition_a_sql_jam_id = (
                            start_jam_event(
                                camera_id=self.name,
                                condition_code="A",
                                condition_name="MOVEMENT_JAM",
                                jam_type="movement",
                                track_ids=(
                                    self.jam_result.get(
                                        "active_jam_track_ids",
                                        [],
                                    )
                                ),
                                reason=(
                                    self.jam_result.get(
                                        "reason"
                                    )
                                ),
                                metadata={
                                    "condition_code": "A",
                                    "condition_name": "MOVEMENT_JAM",
                                    "jam_type": "movement",
                                    "jam_result": self.jam_result,
                                    "roi": self.jam_roi,
                                    "track_ids": (
                                        self.jam_result.get(
                                            "active_jam_track_ids",
                                            [],
                                        )
                                        or []
                                    ),
                                },
                            )
                        )

                    except Exception as error:

                        self.condition_a_sql_jam_id = None
                        self.condition_a_sql_jam_start = None

                        self.logger.warning(
                            f"{self.name}: failed to start "
                            f"SQL Condition A jam event: {error}"
                        )

                # --------------------------------------------------
                # JAM RECOVERY
                # --------------------------------------------------

                else:

                    if self.condition_a_sql_jam_id is not None:

                        start_time = (
                            self.condition_a_sql_jam_start
                        )

                        if start_time is not None:

                            duration_seconds = (
                                datetime.now(timezone.utc)
                                - start_time
                            ).total_seconds()

                        else:

                            duration_seconds = 0.0

                        try:

                            end_jam_event(
                                jam_event_id=(
                                    self.condition_a_sql_jam_id
                                ),
                                duration_seconds=(
                                    duration_seconds
                                ),
                                status="RECOVERED",
                            )

                        except Exception as error:

                            self.logger.warning(
                                f"{self.name}: failed to end "
                                f"SQL Condition A jam event: {error}"
                            )

                    self.condition_a_sql_jam_id = None
                    self.condition_a_sql_jam_start = None

                # --------------------------------------------------
                # EXISTING COUNT LOGGER
                # --------------------------------------------------

                try:

                    self.count_logger.log_event(
                        event_type=(
                            "jam_condition_a"
                            if condition_a_jam
                            else "jam_condition_a_recovered"
                        ),
                        camera_name=self.name,
                        status=(
                            "jam"
                            if condition_a_jam
                            else "normal"
                        ),
                        jam_result=self.jam_result,
                        roi=self.jam_roi,
                    )

                except Exception as error:

                    self.logger.warning(
                        f"{self.name}: failed CountLogger "
                        f"Condition A event: {error}"
                    )

                self.previous_condition_a_jam = (
                    condition_a_jam
                )

            # ==================================================
            # CONDITION B - BAG SPACING JAM
            # ==================================================

            condition_b_jam = bool(
                self.spacing_result.get(
                    "jam_detected",
                    False,
                )
            )

            if condition_b_jam != self.previous_condition_b_jam:

                # --------------------------------------------------
                # JAM START
                # --------------------------------------------------

                if condition_b_jam:

                    try:

                        self.condition_b_sql_jam_start = (
                            datetime.now(timezone.utc)
                        )

                        self.condition_b_sql_jam_id = (
                            start_jam_event(
                                camera_id=self.name,
                                condition_code="B",
                                condition_name="BAG_SPACING_JAM",
                                jam_type="spacing",
                                track_ids=(
                                    self.spacing_result.get(
                                        "active_jam_track_ids",
                                        [],
                                    )
                                ),
                                reason=(
                                    self.spacing_result.get(
                                        "reason"
                                    )
                                ),
                                roi_snapshot_id=None,
                                metadata={
                                    "condition_code": "B",
                                    "condition_name": "BAG_SPACING_JAM",
                                    "jam_type": "spacing",
                                    "spacing_result": (
                                        self.spacing_result
                                    ),
                                    "roi": self.spacing_roi,
                                    "track_ids": (
                                        self.spacing_result.get(
                                            "active_jam_track_ids",
                                            [],
                                        )
                                        or []
                                    ),
                                },
                            )
                        )

                    except Exception as error:

                        self.condition_b_sql_jam_id = None
                        self.condition_b_sql_jam_start = None

                        self.logger.warning(
                            f"{self.name}: failed to start "
                            f"SQL Condition B jam event: {error}"
                        )

                # --------------------------------------------------
                # JAM RECOVERY
                # --------------------------------------------------

                else:

                    if self.condition_b_sql_jam_id is not None:

                        start_time = (
                            self.condition_b_sql_jam_start
                        )

                        if start_time is not None:

                            duration_seconds = (
                                datetime.now(timezone.utc)
                                - start_time
                            ).total_seconds()

                        else:

                            duration_seconds = 0.0

                        try:

                            end_jam_event(
                                jam_event_id=(
                                    self.condition_b_sql_jam_id
                                ),
                                duration_seconds=(
                                    duration_seconds
                                ),
                                status="RECOVERED",
                            )

                        except Exception as error:

                            self.logger.warning(
                                f"{self.name}: failed to end "
                                f"SQL Condition B jam event: {error}"
                            )

                    self.condition_b_sql_jam_id = None
                    self.condition_b_sql_jam_start = None

                # --------------------------------------------------
                # EXISTING COUNT LOGGER
                # --------------------------------------------------

                try:

                    self.count_logger.log_event(
                        event_type=(
                            "jam_condition_b"
                            if condition_b_jam
                            else "jam_condition_b_recovered"
                        ),
                        camera_name=self.name,
                        status=(
                            "jam"
                            if condition_b_jam
                            else "normal"
                        ),
                        spacing_result=self.spacing_result,
                        roi=self.spacing_roi,
                    )

                except Exception as error:

                    self.logger.warning(
                        f"{self.name}: failed CountLogger "
                        f"Condition B event: {error}"
                    )

                self.previous_condition_b_jam = (
                    condition_b_jam
                )

            # ==================================================
            # CONDITION C - ROI OCCUPANCY JAM
            # ==================================================

            condition_c_result = (
                self._condition_c()
            )

            condition_c_jam = bool(
                condition_c_result.get(
                    "jam",
                    False,
                )
            )

            if condition_c_jam != self.previous_condition_c_jam:

                # --------------------------------------------------
                # JAM START
                # --------------------------------------------------

                if condition_c_jam:

                    try:

                        self.condition_c_sql_jam_start = (
                            datetime.now(timezone.utc)
                        )

                        # ------------------------------------------
                        # Save the ROI snapshot image FIRST so we
                        # have a snapshot ID to attach to the jam
                        # event below. Without this, roi_snapshots
                        # stayed empty and jam_events.roi_snapshot_id
                        # was always NULL even though the detector
                        # already wrote the image to disk.
                        # ------------------------------------------

                        condition_c_snapshot_id = None

                        condition_c_image_path = (
                            condition_c_result.get("image_path")
                        )

                        if condition_c_image_path:

                            try:

                                condition_c_snapshot_id = (
                                    save_roi_snapshot(
                                        camera_id=self.name,
                                        event_type="condition_c",
                                        image_path=str(
                                            condition_c_image_path
                                        ),
                                        metadata={
                                            "condition_code": "C",
                                            "condition_name": (
                                                "ROI_OCCUPANCY_JAM"
                                            ),
                                            "jam_type": (
                                                "roi_occupancy"
                                            ),
                                            "condition_c_result": (
                                                condition_c_result
                                            ),
                                            "roi": (
                                                self.condition_c_roi
                                            ),
                                            "track_ids": (
                                                condition_c_result.get(
                                                    "track_ids",
                                                    [],
                                                )
                                                or []
                                            ),
                                        },
                                    )
                                )

                            except Exception as error:

                                condition_c_snapshot_id = None

                                self.logger.warning(
                                    f"{self.name}: failed to save "
                                    "Condition C ROI snapshot to "
                                    f"SQL: {error}"
                                )

                        self.condition_c_sql_jam_id = (
                            start_jam_event(
                                camera_id=self.name,
                                condition_code="C",
                                condition_name="ROI_OCCUPANCY_JAM",
                                jam_type="roi_occupancy",
                                track_ids=(
                                    condition_c_result.get(
                                        "track_ids",
                                        [],
                                    )
                                ),
                                reason=(
                                    condition_c_result.get(
                                        "reason",
                                        "ROI occupancy exceeded limit",
                                    )
                                ),
                                roi_snapshot_id=(
                                    condition_c_snapshot_id
                                ),
                                metadata={
                                    "condition_code": "C",
                                    "condition_name": "ROI_OCCUPANCY_JAM",
                                    "jam_type": "roi_occupancy",
                                    "condition_c_result": (
                                        condition_c_result
                                    ),
                                    "roi": (
                                        self.condition_c_roi
                                    ),
                                    "track_ids": (
                                        condition_c_result.get(
                                            "track_ids",
                                            [],
                                        )
                                        or []
                                    ),
                                },
                            )
                        )

                    except Exception as error:

                        self.condition_c_sql_jam_id = None
                        self.condition_c_sql_jam_start = None

                        self.logger.warning(
                            f"{self.name}: failed to start "
                            f"SQL Condition C jam event: {error}"
                        )

                # --------------------------------------------------
                # JAM RECOVERY
                # --------------------------------------------------

                else:

                    if self.condition_c_sql_jam_id is not None:

                        start_time = (
                            self.condition_c_sql_jam_start
                        )

                        if start_time is not None:

                            duration_seconds = (
                                datetime.now(timezone.utc)
                                - start_time
                            ).total_seconds()

                        else:

                            duration_seconds = 0.0

                        try:

                            end_jam_event(
                                jam_event_id=(
                                    self.condition_c_sql_jam_id
                                ),
                                duration_seconds=(
                                    duration_seconds
                                ),
                                status="RECOVERED",
                            )

                        except Exception as error:

                            self.logger.warning(
                                f"{self.name}: failed to end "
                                f"SQL Condition C jam event: {error}"
                            )

                    self.condition_c_sql_jam_id = None
                    self.condition_c_sql_jam_start = None

                # --------------------------------------------------
                # EXISTING COUNT LOGGER
                # --------------------------------------------------

                try:

                    self.count_logger.log_event(
                        event_type=(
                            "jam_condition_c"
                            if condition_c_jam
                            else "jam_condition_c_recovered"
                        ),
                        camera_name=self.name,
                        status=(
                            "jam"
                            if condition_c_jam
                            else "normal"
                        ),
                        condition_c_result=(
                            condition_c_result
                        ),
                        roi=self.condition_c_roi,
                    )

                except Exception as error:

                    self.logger.warning(
                        f"{self.name}: failed CountLogger "
                        f"Condition C event: {error}"
                    )

                self.previous_condition_c_jam = (
                    condition_c_jam
                )

        except Exception as error:

            self.logger.warning(
                f"{self.name}: failed to log jam events: "
                f"{error}"
            )

    # ======================================================
    # PIPELINE THREAD
    # ======================================================

    def run(
        self,
        stop_event,
    ):

        self.logger.info(
            f"{self.name} pipeline thread started."
        )

        while not stop_event.is_set():

            active = self.process()

            if not active:

                time.sleep(
                    0.1
                )

        self.logger.info(
            f"{self.name} pipeline thread stopping."
        )

        self.release()

        self._publish_runtime_status(
            fps=0.0,
            print_status="offline",
            runtime_status="offline",
            force=True,
        )

        # Finish/stop the background persistence worker only when the
        # camera pipeline itself is shutting down. `release()` is also
        # used for reconnects, so the worker must remain alive there.
        try:
            self._io_worker.close(timeout=2.0)
        except Exception as error:
            self.logger.warning(
                f"{self.name}: background I/O worker shutdown failed: {error}"
            )

    # ======================================================
    # READ FRAME
    # ======================================================

    def read_frame(
        self,
    ):

        success, frame = (
            self.camera.read()
        )

        if not success:

            self.logger.warning(
                f"{self.name} frame read failed "
                "or stream ended."
            )

            return None

        return frame

    # ======================================================
    # DETECT
    # ======================================================

    def detect(
        self,
        frame,
    ):

        return self.inference_manager.infer(

            camera_name=self.name,

            frame=frame,

            timeout=self.inference_timeout,
        )

    # ======================================================
    # TRACK
    # ======================================================

    def track(
        self,
        detections,
    ):

        bag_detections = [

            detection

            for detection in detections

            if detection.get(
                "class_id"
            ) == 0
        ]

        return self.tracker.update(
            bag_detections
        )

    # ======================================================
    # COUNTABLE TRACKS
    # ======================================================

    @staticmethod
    def countable_tracks(
        tracks,
    ):

        return [

            track

            for track in tracks

            if not track.get(
                "unstable",
                False,
            )
        ]

    # ======================================================
    # COUNT
    # ======================================================

    def count(
        self,
        tracks,
    ):

        return self.counter.update(
            tracks
        )

    # ======================================================
    # PRINT DETECTION
    # ======================================================

    def print_detection(
        self,
        tracks,
        detections,
    ):

        if not self.print_detection_enabled:

            return []

        return self.print_detector.update(
            tracks,
            detections,
        )

    # ======================================================
    # UPDATE PRINT ROI COUNT
    # ======================================================

    def update_print_roi_count(
        self,
        print_results,
        tracks,
    ):
        """
        Feed this frame's PrintDetector output to PrintROICounter.

        No voting: a print is counted once, the moment it is
        confirmed. printed_count follows the counter total.
        """

        self._print_counted_this_frame = []

        track_lookup = {

            track.get("track_id"): track

            for track in tracks

            if track.get("track_id") is not None
        }

        self._active_track_ids = set(track_lookup)

        if (
            self.print_roi_counter is None
            or not self.counting_active
        ):
            return

        valid_results = [

            result

            for result in (print_results or [])

            if result.get("track_id") is not None
            and self._is_valid_print_observation(
                track_lookup.get(
                    result.get("track_id"),
                    {},
                )
            )
        ]

        self.print_roi_counter.update(
            valid_results
        )

        counted_prints = list(
            self.print_roi_counter.last_counted_prints
        )

        self.print_roi_count = (
            self.print_roi_counter.total_count
        )

        if not counted_prints:
            return

        self.printed_count += len(counted_prints)
        self._print_counted_this_frame = counted_prints

        for counted_print in counted_prints:

            print(
                f"[PRINT ROI COUNT +1] "
                f"Track ID={counted_print.get('track_id')} "
                f"Center={counted_print.get('center')} "
                f"TOTAL={self.print_roi_count}"
            )

    # ======================================================
    # VALID PRINT OBSERVATION
    # ======================================================

    @staticmethod
    def _is_valid_print_observation(
        track,
    ):

        if track.get("unstable", False):
            return False

        if track.get("motion_jump", False):
            return False

        return True

    # ======================================================
    # DRAW
    # ======================================================

    def draw(
        self,
        frame,
        tracks,
        detections,
        print_results,
        count,
        fps,
        entry_roi_count=0,
    ):

        self.visualizer.visualize(

            frame=frame,

            camera_name=self.name,

            count=count,

            entry_roi_count=entry_roi_count,

            printed_count=self.printed_count,

            missing_count=self.missing_count,

            fps=fps,

            roi=self.roi,

            bag_tracks=tracks,

            all_detections=detections,

            print_results=print_results,

            display_config=self.display_config,

            counted_bags=(
                self.counter.last_counted_bags
            ),

            print_roi=(
                self.print_roi_counter.roi
                if self.print_roi_counter is not None
                else None
            ),

            print_roi_count=self.print_roi_count,

            # Condition A
            jam_result=self.jam_result,

            jam_roi=self.jam_roi,

            # Condition B
            spacing_result=self.spacing_result,

            spacing_roi=self.spacing_roi,

            # Final A OR B
            final_jam_result=(
                self.final_jam_result
            ),

            condition_c_result=(
                self.condition_c_result
            ),

            condition_c_roi=(
                self.condition_c_roi
            ),
        )

    # ======================================================
    # DISPLAY
    # ======================================================
    def publish(
        self,
    ):

        # OpenCV popup windows are disabled. The dashboard still gets
        # frames through get_latest_frame(), so nothing else changes.
        return

        # Nothing new since the last imshow: skip the work entirely.
        # (The application loop calls publish() every ~10 ms, far more
        # often than a new annotated frame is produced.)
        seq = self._latest_frame_seq

        if seq == self._published_seq:
            return

        # imshow only reads the frame, so no defensive copy is needed.
        frame = self.get_latest_frame(copy=False)

        if frame is None:
            return

        # Create a resizable window once, sized to fit the screen.
        # Display only: the frame and ROI coordinates are unchanged.
        if not getattr(self, "_window_ready", False):
            cv2.namedWindow(self.window_name, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(self.window_name, 1280, 720)
            self._window_ready = True

        cv2.imshow(
            self.window_name,
            frame,
        )

        self._published_seq = seq

    # ======================================================
    # FRAME STORAGE
    # ======================================================

    def _set_latest_frame(
        self,
        frame,
    ):

        # No copy: process() allocates a fresh frame on every read and
        # never touches it again after draw(), so the stored array is
        # effectively immutable. This removes one 2304x1296 copy
        # (~8.5 MiB) per processed frame.
        with self._frame_lock:

            self._latest_frame = frame

            self._latest_frame_seq += 1

    def get_latest_frame(
        self,
        copy=True,
    ):

        # copy=True (default) keeps the old safe behaviour for external
        # callers that may draw on / modify the frame. Read-only
        # consumers (JPEG encode, imshow) should pass copy=False.
        with self._frame_lock:

            frame = self._latest_frame

        if frame is None:
            return None

        return frame.copy() if copy else frame

    def _get_latest_frame(
        self,
    ):

        return self.get_latest_frame()

    # ======================================================
    # FPS
    # ======================================================

    def calculate_fps(
        self,
    ):

        # Rolling-window FPS: (frames - 1) / time span over the last
        # fps_window_seconds. Smooth enough to read on screen, but a
        # sustained slowdown still shows up within about a second.
        now = time.perf_counter()

        self.previous_time = now

        window = self._fps_window

        window.append(now)

        cutoff = now - self.fps_window_seconds

        while len(window) > 2 and window[0] < cutoff:
            window.popleft()

        if len(window) < 2:
            return 0.0

        span = window[-1] - window[0]

        if span <= 0.0:
            return 0.0

        return (len(window) - 1) / span

    # ======================================================
    # RELEASE
    # ======================================================

    def _submit_io(
        self,
        func,
        *args,
        **kwargs,
    ):
        """Queue a blocking persistence operation without stalling video."""

        return self._io_worker.submit(
            func,
            *args,
            **kwargs,
        )

    # ======================================================
    # RELEASE
    # ======================================================

    def release(
        self,
    ):

        try:

            self.camera.release()

        except Exception:

            self.logger.warning(
                f"{self.name}: camera release failed."
            )

        self.connected = False

        # ----------------------------------------------
        # Reset Condition A
        # ----------------------------------------------

        try:

            self.jam_detector.reset()

            self.jam_result = (
                self.jam_detector._empty_result()
            )

        except Exception:

            self.logger.warning(
                f"{self.name}: JamDetector reset failed."
            )

        # ----------------------------------------------
        # Reset Condition B result
        #
        # BagSpacingDetector has no temporal jam state.
        # ----------------------------------------------

        try:

            self.spacing_result = (
                self.bag_spacing_detector
                ._empty_result()
            )

        except Exception:

            self.logger.warning(
                f"{self.name}: BagSpacingDetector "
                "reset failed."
            )

        # ----------------------------------------------
        # Reset Condition C result
        #
        # ROIOccupancyDetector has no temporal jam state.
        # ----------------------------------------------

        try:

            self.condition_c_result = {

                "jam": False,

                "status": "normal",

                "bag_count": 0,

                "track_ids": [],

                "minimum_gap_mm": None,

                "distances": [],

                "image_path": None,
            }

            self.condition_c_start_time = None

        except Exception:

            self.logger.warning(
                f"{self.name}: ROIOccupancyDetector "
                "reset failed."
            )

        try:

            self.final_jam_result = (
                self._build_final_jam_result()
            )

        except Exception:

            self.final_jam_result = {
                "status": "disabled",
                "jam_detected": False,
                "warning": False,
                "jam_types": [],
                "active_jam_count": 0,
                "active_jam_track_ids": [],
            }

        with self._frame_lock:

            self._latest_frame = None

    # ======================================================
    # PUBLISH EVENTS
    # ======================================================

    def _publish_events(
        self,
        count,
        fps,
        print_results,
    ):

        counted_results = (
            self._update_print_totals()
        )

        resolved_print_results = (
            self._resolved_print_results
        )

        live_print_status = (
            self._summarize_print_status(
                print_results
            )
        )

        counted_print_status = (

            self._summarize_counted_print_status(
                resolved_print_results
            )

            if resolved_print_results

            else None
        )

        # ==================================================
        # DASHBOARD
        # ==================================================

        self._publish_runtime_status(

            fps=fps,

            print_status=live_print_status,

            runtime_status="online",

            frame_processed=True,

            count_event=bool(
                counted_results
            ),

            entry_roi_count=(
                self.entry_roi_counter.total_count
                if self.entry_roi_counter is not None
                else 0
            ),
        )

        # ==================================================
        # ELASTICSEARCH CAMERA EVENT
        # ==================================================

        if self.elasticsearch is not None:

            now_monotonic = time.monotonic()

            if (
                now_monotonic
                - self.last_elasticsearch_camera_event_time
                >= self.elasticsearch_camera_event_interval
            ):

                self.last_elasticsearch_camera_event_time = (
                    now_monotonic
                )

                # Non-blocking because self.elasticsearch is an async proxy.
                self.elasticsearch.create_camera_event(
                    self.name,
                    fps,
                    "online",
                )

        # ==================================================
        # PRINT EVENTS (one per bag, no voting)
        #
        # printed -> written the moment PrintROICounter counts it
        # missing -> written when a counted bag leaves without one
        # ==================================================

        print_events = []

        for counted_print in self._print_counted_this_frame:

            print_events.append(
                (
                    "printed",
                    counted_print.get("track_id"),
                    counted_print.get("confidence"),
                )
            )

        for resolved_bag in resolved_print_results:

            if resolved_bag.get("print_present") is False:

                print_events.append(
                    (
                        "missing",
                        resolved_bag.get("track_id"),
                        None,
                    )
                )

        for (
            event_result,
            event_track_id,
            event_confidence,
        ) in print_events:

            if self.elasticsearch is not None:

                try:

                    self.elasticsearch.create_print_event(
                        self.name,
                        event_result == "printed",
                    )

                except Exception:

                    self.logger.warning(
                        f"{self.name}: failed publishing "
                        "print event to Elasticsearch."
                    )

            try:

                self._submit_io(
                    save_print_event,
                    camera_id=self.name,
                    result=event_result,
                    track_id=event_track_id,
                    confidence=event_confidence,
                    timestamp=None,
                    metadata={
                        "print_present": event_result == "printed",
                        "track_id": event_track_id,
                        "source": "print_roi_counter",
                    },
                )

            except Exception as e:

                self.logger.error(
                    f"{self.name}: failed saving "
                    f"print event to SQL Server: {e}"
                )

        # ==================================================
        # COUNT EVENTS
        # ==================================================

        for counted_bag in counted_results:

            if self.elasticsearch is not None:

                try:

                    self.elasticsearch.create_count_event(

                        self.name,

                        counted_bag.get(
                            "total_count",
                            (
                                self.entry_roi_counter.total_count
                                if self.entry_roi_counter is not None
                                else self.counter.total_count
                            ),
                        ),

                        counted_bag.get(
                            "center"
                        ),
                    )

                except Exception:

                    self.logger.warning(
                        f"{self.name}: failed publishing "
                        "count event to Elasticsearch."
                    )

            # ==================================================
            # SQL SERVER PRODUCTION EVENT
            #
            # Written once, via count_logger.log_count(), which
            # is the single source of truth for dbo.production_
            # events (one row per confirmed bag, bag_count=1).
            #
            # A duplicate direct save_production_event() call
            # used to live here as well -- it wrote a SECOND row
            # per bag with bag_count=self.counter.total_count
            # (the camera's running total, not a per-event count
            # of 1), which both double-counted events and put the
            # wrong value in bag_count. Removed in favor of the
            # single call below.
            # ==================================================

            if self.count_logger is not None:

                try:

                    # --------------------------------------------------
                    # PRODUCTION COUNT SOURCE OF TRUTH
                    #
                    # `count` here is the line/crossing counter
                    # (self.counter), which used to be written as
                    # total_count/count in the persisted event --
                    # i.e. the same value already stored separately
                    # as line_count below. That made the dashboard's
                    # live count (which reads entry_roi_counter,
                    # see _publish_runtime_status()) disagree with
                    # the historical production_events/count_events
                    # records for the same camera.
                    #
                    # ROI entry counting is now the single source of
                    # truth for the production/total bag count, to
                    # match what the live dashboard already shows.
                    # The line counter is kept as line_count, purely
                    # as a diagnostic/legacy comparison value.
                    # --------------------------------------------------

                    production_count = (
                        self.entry_roi_counter.total_count
                        if self.entry_roi_counter is not None
                        else count
                    )

                    self.count_logger.log_count(

                        camera_name=self.name,

                        total_count=production_count,

                        track_id=counted_bag.get(
                            "track_id"
                        ),

                        center=counted_bag.get(
                            "center"
                        ),

                        print_present=counted_bag.get(
                            "print_present"
                        ),

                        printed_count=counted_bag.get(
                            "printed_count"
                        ),

                        missing_count=counted_bag.get(
                            "missing_count"
                        ),

                        print_detection_enabled=(
                            self.print_detection_enabled
                        ),
                        include_bags_in_total=self.include_bags_in_total,
                        # Extra diagnostic fields -- preserved in
                        # metadata_json (same fields the removed
                        # direct save_production_event() call used
                        # to pass as top-level columns).
                        #
                        # roi_count is explicitly named (in addition
                        # to total_count/count above) so downstream
                        # consumers don't have to guess which counter
                        # total_count came from.
                        roi_count=production_count,

                        line_count=self.counter.total_count,

                        frame_roi_count=(
                            self.entry_roi_counter.total_count
                            if self.entry_roi_counter is not None
                            else 0
                        ),

                        bags_inside_roi=(
                            sum(
                                1
                                for inside in
                                self.entry_roi_counter.track_inside_roi.values()
                                if inside
                            )
                            if self.entry_roi_counter is not None
                            else 0
                        ),
                    )

                except Exception as e:

                    self.logger.exception(
                        f"{self.name}: failed writing count event: {e}"
                    )

        self.last_count = count

        if counted_print_status is not None:

            self.last_print_status = (
                counted_print_status
            )

    # ======================================================
    # UPDATE PRINT TOTALS
    # ======================================================

    def _update_print_totals(
        self,
    ):

        """
        Returns the bags entry-counted THIS frame (for production
        events). Each counted bag is queued; _resolve_pending_prints()
        reports it printed (PrintROICounter counted a print for its
        track) or missing (it left without one). Resolved bags are
        exposed via self._resolved_print_results.
        """

        counted_results = []

        counted_bags = (
            self.entry_roi_counter.last_counted_bags
             if self.entry_roi_counter is not None
            else []
        ) or []

        for counted_bag in counted_bags:

            track_id = counted_bag.get(
                "track_id"
            )

            if (
                self.print_roi_counter is not None
                and track_id is not None
            ):

                self._pending_print[track_id] = {
                    "bag": dict(counted_bag),
                    "since": self.frame_index,
                    "last_active": self.frame_index,
                }

            result = dict(
                counted_bag
            )

            result["print_present"] = None
            result["print_confidence"] = None
            result["printed_count"] = self.printed_count
            result["missing_count"] = self.missing_count

            counted_results.append(
                result
            )

        self._resolved_print_results = (
            self._resolve_pending_prints()
        )

        return counted_results

    # ======================================================
    # RESOLVE PENDING PRINT DECISIONS
    # ======================================================

    def _resolve_pending_prints(
        self,
    ):

        resolved = []

        if not self._pending_print:
            return resolved

        for track_id in list(self._pending_print):

            info = self._pending_print[track_id]

            if track_id in self._active_track_ids:
                info["last_active"] = self.frame_index

            has_print = (
                self.print_roi_counter is not None
                and self.print_roi_counter.has_counted(track_id)
            )

            unseen_frames = (
                self.frame_index - info["last_active"]
            )

            age_frames = (
                self.frame_index - info["since"]
            )

            left_or_timed_out = (
                unseen_frames >= self.print_missing_grace_frames
                or age_frames >= self.print_missing_max_frames
            )

            # A bag with a counted print is printed right away.
            # A bag without one is only MISSING once it has left.
            if not has_print and not left_or_timed_out:
                continue

            del self._pending_print[track_id]

            if not has_print:
                self.missing_count += 1

            result = dict(info["bag"])

            result["print_present"] = has_print
            result["print_confidence"] = None
            result["printed_count"] = self.printed_count
            result["missing_count"] = self.missing_count

            resolved.append(result)

        return resolved

    # ======================================================
    # SUMMARIZE LIVE PRINT STATUS
    # ======================================================

    def _summarize_print_status(
        self,
        print_results,
    ):

        if not self.print_detection_enabled:

            return "disabled"

        if not print_results:

            return "unknown"

        statuses = []

        for result in print_results:

            value = result.get(
                "print_present"
            )

            if value is True:

                statuses.append(
                    "printed"
                )

            elif value is False:

                statuses.append(
                    "missing"
                )

        if not statuses:

            return "unknown"

        if "missing" in statuses:

            return "missing"

        return "printed"

    # ======================================================
    # SUMMARIZE COUNTED PRINT STATUS
    # ======================================================

    @staticmethod
    def _summarize_counted_print_status(
        counted_results,
    ):

        if not counted_results:
            return None

        statuses = []

        for result in counted_results:

            value = result.get(
                "print_present"
            )

            if value is True:

                statuses.append(
                    "printed"
                )

            elif value is False:

                statuses.append(
                    "missing"
                )

            else:

                statuses.append(
                    "unknown"
                )

        if "missing" in statuses:

            return "missing"

        if "printed" in statuses:

            return "printed"

        return "unknown"

    # ======================================================
    # PUBLISH RUNTIME STATUS
    # ======================================================

    def _publish_runtime_status(
        self,
        fps,
        print_status,
        runtime_status=None,
        force=False,
        frame_processed=False,
        count_event=False,
        entry_roi_count=None,
    ):

        if self.dashboard_state is None:
            return

        current_time = (
            time.monotonic()
        )

        if (
            not force
            and
            not count_event
            and
            (
                current_time
                -
                self.last_dashboard_publish_time
            )
            <
            self.dashboard_publish_interval
        ):

            return

        self.last_dashboard_publish_time = (
            current_time
        )

        now_iso = (
            datetime.now(
                timezone.utc
            )
            .isoformat()
        )

        runtime_status = (
            runtime_status
            or
            (
                "online"
                if self.connected
                else "offline"
            )
        )

        # ==================================================
        # CONDITION A
        # ==================================================

        movement = (
            self.jam_result
            if isinstance(
                self.jam_result,
                dict,
            )
            else {}
        )

        movement_status = (
            movement.get(
                "status",
                "normal",
            )
            if self.jam_detection_enabled
            else "disabled"
        )

        movement_detected = bool(
            movement.get(
                "jam_detected",
                False,
            )
        )

        movement_warning = bool(
            movement.get(
                "warning",
                False,
            )
        )

        movement_ids = (
            movement.get(
                "active_jam_track_ids",
                [],
            )
            or []
        )

        movement_tracks = (
            movement.get(
                "tracks",
                [],
            )
            or []
        )

        # ==================================================
        # CONDITION B
        # ==================================================

        spacing = (
            self.spacing_result
            if isinstance(
                self.spacing_result,
                dict,
            )
            else {}
        )

        spacing_status = (
            spacing.get(
                "status",
                "normal",
            )
            if self.bag_spacing_enabled
            else "disabled"
        )

        spacing_detected = bool(
            spacing.get(
                "jam_detected",
                False,
            )
        )

        spacing_pairs = (
            spacing.get(
                "pairs",
                [],
            )
            or []
        )

        spacing_jam_pairs = (
            spacing.get(
                "jam_pairs",
                [],
            )
            or []
        )

        spacing_ids = (
            spacing.get(
                "active_jam_track_ids",
                [],
            )
            or []
        )

        minimum_gap_mm = (
            spacing.get(
                "minimum_gap_mm"
            )
        )

        spacing_threshold_mm = (
            spacing.get(
                "threshold_mm",
                self.bag_spacing_detector
                .jam_threshold_mm,
            )
        )

        # ==================================================
        # CONDITION C
        #
        # ROI Occupancy Detector
        #
        # Publishes:
        # - ROI Bag Count
        # - Track IDs
        # - Minimum Gap
        # - Distances
        # - ROI Image
        # - ROI Coordinates
        # - Max Allowed Bags / Occupancy %
        # - Jam Timestamp / Duration
        # ==================================================

        condition_c = self._condition_c()

        condition_c_status = (
            condition_c.get(
                "status",
                "normal",
            )
            if self.condition_c_enabled
            else "disabled"
        )

        condition_c_max_allowed_bags = (
            self.condition_c_detector.max_allowed_bags
        )

        condition_c_roi_occupancy = (
            condition_c.get(
                "bag_count",
                0,
            )
            /
            condition_c_max_allowed_bags
            if condition_c_max_allowed_bags
            else 0
        )

        condition_c_timestamp = (
            datetime.now(
                timezone.utc
            ).isoformat()
            if condition_c.get(
                "jam",
                False,
            )
            else None
        )

        condition_c_duration = (
            round(
                time.perf_counter()
                -
                self.condition_c_start_time,
                1,
            )
            if self.condition_c_start_time is not None
            else 0
        )

        # ==================================================
        # FINAL A OR B
        # ==================================================

        final_jam = (
            self.final_jam_result
            if isinstance(
                self.final_jam_result,
                dict,
            )
            else {}
        )

        final_status = (
            final_jam.get(
                "status",
                "normal",
            )
        )

        final_detected = bool(
            final_jam.get(
                "jam_detected",
                False,
            )
        )

        final_warning = bool(
            final_jam.get(
                "warning",
                False,
            )
        )

        final_ids = (
            final_jam.get(
                "active_jam_track_ids",
                [],
            )
            or []
        )

        final_types = (
            final_jam.get(
                "jam_types",
                [],
            )
            or []
        )

        # ==================================================
        # ENTRY ROI STATE
        # ==================================================

        entry_roi_state = (
            self._get_entry_roi_state()
        )

        try:

            self.dashboard_state.update_camera(

                camera_name=self.name,

                count=(
                    self.entry_roi_counter.total_count
                    if self.entry_roi_counter is not None
                    else 0
                ),

                total_count=(
                    self.entry_roi_counter.total_count
                    if self.entry_roi_counter is not None
                    else 0
                ),

                # ==========================================
                # ENTRY ROI COUNT
                # ==========================================

                entry_roi_count=(
                    entry_roi_count
                    if entry_roi_count is not None
                    else (
                        self.entry_roi_counter.total_count
                        if self.entry_roi_counter is not None
                        else 0
                    )
                ),

                line_count=self.counter.total_count,
                entry_roi_active_count=(
                    entry_roi_state["count"]
                ),

                entry_roi_active_track_ids=(
                    entry_roi_state["track_ids"]
                ),

                entry_roi_active_bags=(
                    entry_roi_state["bags"]
                ),

                entry_roi=(
                    self.entry_roi_counter.roi
                    if self.entry_roi_counter is not None
                    else None
                ),

                fps=fps,

                status=runtime_status,

                print_status=print_status,

                printed_count=self.printed_count,

                missing_count=self.missing_count,

                print_roi_count=self.print_roi_count,

                print_roi=(
                    self.print_roi_counter.roi
                    if self.print_roi_counter is not None
                    else None
                ),

                printed_bags_count=(
                    self.printed_count
                ),

                not_printed_bags_count=(
                    self.missing_count
                ),

                print_detection_enabled=(
                    self.print_detection_enabled
                ),

                frame_count=(
                    self.processed_frame_count
                ),

                last_frame_at=(
                    now_iso
                    if frame_processed
                    else None
                ),

                last_count_at=(
                    now_iso
                    if count_event
                    else None
                ),

                last_error=(
                    self.last_error
                    if self.last_error
                    else ""
                ),

                # ==========================================
                # CONDITION A
                # ==========================================

                movement_jam_enabled=(
                    self.jam_detection_enabled
                ),

                movement_jam_status=(
                    movement_status
                ),

                movement_jam_detected=(
                    movement_detected
                ),

                movement_jam_warning=(
                    movement_warning
                ),

                movement_jam_track_ids=(
                    movement_ids
                ),

                movement_jam_tracks=(
                    movement_tracks
                ),

                # ==========================================
                # CONDITION B
                # ==========================================

                spacing_detection_enabled=(
                    self.bag_spacing_enabled
                ),

                spacing_status=(
                    spacing_status
                ),

                spacing_jam_detected=(
                    spacing_detected
                ),

                spacing_threshold_mm=(
                    spacing_threshold_mm
                ),

                minimum_gap_mm=(
                    minimum_gap_mm
                ),

                spacing_pairs=(
                    spacing_pairs
                ),

                spacing_jam_pairs=(
                    spacing_jam_pairs
                ),

                spacing_jam_track_ids=(
                    spacing_ids
                ),

                # ==========================================
                # CONDITION C
                #
                # ROI Occupancy Detector
                #
                # Publishes:
                # - ROI Bag Count
                # - Track IDs
                # - Minimum Gap
                # - Distances
                # - ROI Image
                # - ROI Coordinates
                # - Max Allowed Bags / Occupancy %
                # - Jam Timestamp / Duration
                # ==========================================

                condition_c_enabled=(
                    self.condition_c_enabled
                ),

                condition_c_status=(
                    condition_c_status
                ),

                condition_c_detected=(
                    condition_c.get(
                        "jam",
                        False,
                    )
                ),

                condition_c_bag_count=(
                    condition_c.get(
                        "bag_count",
                        0,
                    )
                ),

                condition_c_track_ids=(
                    condition_c.get(
                        "track_ids",
                        [],
                    )
                ),

                condition_c_minimum_gap_mm=(
                    condition_c.get(
                        "minimum_gap_mm"
                    )
                ),

                condition_c_distances=(
                    condition_c.get(
                        "distances",
                        [],
                    )
                ),

                condition_c_image_path=(
                    condition_c.get(
                        "image_path"
                    )
                ),

                condition_c_roi=(
                    self.condition_c_roi
                ),

                condition_c_max_allowed_bags=(
                    condition_c_max_allowed_bags
                ),

                condition_c_roi_occupancy=(
                    condition_c_roi_occupancy
                ),

                condition_c_timestamp=(
                    condition_c_timestamp
                ),

                condition_c_duration=(
                    condition_c_duration
                ),

                # ==========================================
                # FINAL JAM
                #
                # Keep existing field names so dashboard
                # code remains backward-compatible.
                # ==========================================

                jam_detection_enabled=bool(
                    self.jam_detection_enabled
                    or
                    self.bag_spacing_enabled
                    or
                    self.condition_c_enabled
                ),

                jam_status=(
                    final_status
                ),

                jam_detected=(
                    final_detected
                ),

                jam_warning=(
                    final_warning
                ),

                jam_types=(
                    final_types
                ),

                active_jam_count=len(
                    final_ids
                ),

                active_jam_track_ids=(
                    final_ids
                ),

                # Preserve old V1 field for compatibility.
                jam_tracks=(
                    movement_tracks
                ),
            )

        except Exception as error:

            self.logger.warning(
                f"{self.name}: dashboard runtime "
                f"update failed: {error}"
            )