"""
==========================================================
FillPac AI
Production Dashboard State Manager
==========================================================

Purpose
-------
Maintains the runtime state consumed by the dashboard
backend.

Architecture
------------
Camera Pipeline
      |
      | update_camera(...)
      v
DashboardState
      |
      +---- Camera Runtime State
      +---- Production Totals
      +---- Service Health
      +---- Jam Runtime State
      +---- System Status
      |
      v
SQL Server (dbo.system_state / dbo.camera_status)
      |
      v
Dashboard Backend
      |
      +---- REST API
      +---- Socket.IO
      +---- Production Dashboard
      +---- Live Monitor
      +---- Camera Management
      +---- Jam Monitoring


Important
---------
DashboardState does NOT perform:

- YOLO inference
- Bag counting
- Tracking
- Print detection
- Jam detection

It only stores and publishes the latest runtime state.

Counting remains controlled by the existing physical
bag-center crossing logic.

Jam detection remains controlled by JamDetector inside
the camera pipeline.
==========================================================
"""

import logging
import math
import threading
import time

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from database.repository import load_dashboard_state, save_dashboard_state


logger = logging.getLogger(
    "fillpac.dashboard_state"
)


# ==========================================================
# HELPERS
# ==========================================================

def utc_now():
    """
    Current UTC datetime.
    """

    return datetime.now(
        timezone.utc
    )


def utc_now_iso():
    """
    Current UTC ISO timestamp.
    """

    return (
        utc_now()
        .isoformat()
    )


def safe_int(
    value,
    default=0,
):
    """
    Safely convert a value to int.
    """

    try:
        return int(value)

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return default


def safe_float(
    value,
    default=0.0,
):
    """
    Safely convert a value to finite float.
    """

    try:

        result = float(
            value
        )

        if not math.isfinite(
            result
        ):
            return default

        return result

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return default


def safe_bool(
    value,
    default=False,
):
    """
    Safely normalize common boolean representations.
    """

    if isinstance(
        value,
        bool,
    ):
        return value

    if value is None:
        return default

    if isinstance(
        value,
        str,
    ):

        normalized = (
            value
            .strip()
            .lower()
        )

        if normalized in {
            "true",
            "1",
            "yes",
            "on",
            "enabled",
        }:
            return True

        if normalized in {
            "false",
            "0",
            "no",
            "off",
            "disabled",
        }:
            return False

    return bool(
        value
    )


# ==========================================================
# JAM HELPERS
# ==========================================================

JAM_STATUSES = {
    "normal",
    "slow",
    "warning",
    "jam",
    "recovering",
    "disabled",
}


def normalize_jam_status(
    value,
    enabled=True,
):
    """
    Normalize jam detector state for dashboard storage.
    """

    if not enabled:
        return "disabled"

    status = str(
        value or "normal"
    ).strip().lower()

    if status not in JAM_STATUSES:
        return "normal"

    return status


def normalize_track_ids(
    value,
):
    """
    Normalize active jam Track IDs into a JSON-safe list.
    """

    if value is None:
        return []

    if isinstance(
        value,
        (
            list,
            tuple,
            set,
        ),
    ):

        result = []

        for item in value:

            try:
                item = int(item)

            except (
                TypeError,
                ValueError,
                OverflowError,
            ):
                continue

            if item not in result:
                result.append(item)

        return result

    try:

        return [
            int(value)
        ]

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):

        return []


# ==========================================================
# DASHBOARD STATE
# ==========================================================

class DashboardState:
    """
    Thread-safe FillPac dashboard runtime state manager.

    One instance should be shared by all camera pipelines
    and the main application.

    The state is periodically written to SQL Server
    (dbo.system_state / dbo.camera_status) via repository.py.
    state.json is no longer used -- the dashboard backend
    should read the same SQL tables directly instead of
    watching a file.
    """

    # ======================================================
    # INITIALIZATION
    # ======================================================

    def __init__(
        self,
        state_file=None,
        publish_interval=0.5,
        stale_timeout=10.0,
        auto_publish=True,
        enabled=True,
        persist_interval_seconds=0.0,
        default_count_type="roi",
    ):

        # --------------------------------------------------
        # Dashboard enabled
        # --------------------------------------------------

        self.enabled = safe_bool(
            enabled,
            True,
        )

        # --------------------------------------------------
        # Disk-write throttle for callers that trigger an
        # immediate publish (e.g. update_camera()).
        #
        # 0 (default) means every such call publishes right
        # away -- the original synchronous behavior. A positive
        # value means at most one disk write per that many
        # seconds; in-memory state (get_state()/snapshot()) is
        # always current regardless, only the on-disk file is
        # throttled. The background auto-publish thread (if
        # running) will still catch up any writes skipped by
        # the throttle on its own publish_interval cadence.
        # --------------------------------------------------

        self.persist_interval_seconds = max(
            0.0,
            safe_float(
                persist_interval_seconds,
                0.0,
            ),
        )

        # --------------------------------------------------
        # State file (DEPRECATED)
        #
        # state.json has been replaced by SQL Server storage.
        # The parameter is still accepted so existing callers
        # (config.yaml / application.py) don't break, but it
        # is no longer used for anything.
        # --------------------------------------------------

        if state_file is not None:

            logger.warning(
                "DashboardState(state_file=...) is deprecated "
                "and ignored -- state is now stored in SQL "
                "Server (dbo.system_state / dbo.camera_status)."
            )


        # --------------------------------------------------
        # Configuration
        # --------------------------------------------------

        self.publish_interval = max(
            0.05,
            safe_float(
                publish_interval,
                0.5,
            ),
        )

        self.stale_timeout = max(
            1.0,
            safe_float(
                stale_timeout,
                10.0,
            ),
        )

        self.auto_publish = bool(
            auto_publish
        )

        # Explicit status set through set_system_status()
        # must not be overwritten by automatic status detection.
        self._system_status_explicit = False


        # --------------------------------------------------
        # Synchronization
        # --------------------------------------------------

        self._lock = (
            threading.RLock()
        )

        self._publish_lock = (
            threading.Lock()
        )

        self._stop_event = (
            threading.Event()
        )

        self._publisher_thread = None


        # --------------------------------------------------
        # Internal publishing state
        # --------------------------------------------------

        self._dirty = True

        self._last_publish_monotonic = 0.0

        self._last_publish_time = None

        self._publish_count = 0

        self._publish_errors = 0

        self._last_error = None


        # --------------------------------------------------
        # Runtime timestamps
        # --------------------------------------------------

        self._startup_datetime = (
            utc_now()
        )

        self._startup_monotonic = (
            time.monotonic()
        )


        # --------------------------------------------------
        # Main state
        # --------------------------------------------------

        self._state = {

            "system_status":
                "starting",

            "startup_time":
                self._startup_datetime.isoformat(),

            "updated_at":
                utc_now_iso(),

            "service_status": {

                "model_loaded":
                    False,

                "inference_manager_running":
                    False,

                "elasticsearch_connected":
                    False,

                "dashboard_enabled":
                    True,
            },

            "total_count":
                0,
            "total_printed_count":
                0,
            "total_missing_count":
                0,
            # Which counter the dashboard shows: "roi" or "line".
            "count_type":
                self.normalize_count_type(
                    default_count_type
                ),
            # ----------------------------------------------
            # Backward-compatible aliases
            # ----------------------------------------------

            "total_printed_bags_count":
                0,

            "total_not_printed_bags_count":
                0,

            "cameras":
                {},
        }


        # --------------------------------------------------
        # Load previously persisted state, if present.
        #
        # DashboardState is otherwise stateless across process
        # restarts -- when a snapshot already exists in SQL
        # Server (written by a prior run, or by another
        # process/instance), seed the in-memory state from it
        # so cameras and totals aren't silently reset to empty
        # every time a fresh DashboardState is constructed.
        # --------------------------------------------------

        self._load_persisted_state()


        # --------------------------------------------------
        # Initial publish
        # --------------------------------------------------

        self.publish(
            force=True
        )


        # --------------------------------------------------
        # Background publisher
        # --------------------------------------------------

        if self.auto_publish:

            self.start()


        logger.info(
            "DashboardState initialized."
        )

        logger.info(
            "Dashboard state storage: SQL Server "
            "(dbo.system_state / dbo.camera_status)"
        )

        logger.info(
            "Dashboard publish interval: %.3fs",
            self.publish_interval,
        )


    # ======================================================
    # LOAD PERSISTED STATE
    # ======================================================

    def _load_persisted_state(
        self,
    ):
        """
        Merge previously persisted dashboard state (if any)
        into the freshly built self._state.

        Called once during __init__, before the initial
        publish. Runs single-threaded (no other thread has a
        reference to this instance yet), so no locking is
        required here.

        State is loaded from SQL Server (dbo.system_state /
        dbo.camera_status) via repository.load_dashboard_state().
        A missing snapshot, or a database that isn't reachable
        yet, is not fatal -- the freshly built in-memory
        defaults are kept in that case.
        """

        try:

            loaded = load_dashboard_state()

        except Exception:

            logger.warning(
                "Could not load persisted dashboard state "
                "from SQL Server, starting fresh.",
                exc_info=True,
            )

            return

        if not isinstance(
            loaded,
            dict,
        ):
            return

        # ----------------------------------------------------
        # Cameras -- restored as-is. Existing accessors read
        # camera fields through .get(...) with defaults, so a
        # persisted camera dict doesn't need to be re-run
        # through the full register_camera() schema here.
        # ----------------------------------------------------

        if isinstance(
            loaded.get("cameras"),
            dict,
        ):

            self._state[
                "cameras"
            ] = loaded["cameras"]

            # A restored camera is NOT running yet in this process.
            # Its saved status ("online" from the previous run) is
            # stale; it becomes online again only when its pipeline
            # reports in. Disabled/configured flags are kept.
            for restored in self._state["cameras"].values():

                if isinstance(restored, dict):

                    if str(restored.get("status", "")).lower() in (
                        "online", "running", "active",
                    ):
                        restored["status"] = "offline"

                    restored["fps"] = 0.0

        # ----------------------------------------------------
        # Service status -- merge over the defaults rather than
        # replacing wholesale, so newly added health keys still
        # get their default values even against an older file.
        # ----------------------------------------------------

        if isinstance(
            loaded.get("service_status"),
            dict,
        ):

            self._state[
                "service_status"
            ].update(
                loaded["service_status"]
            )

        # ----------------------------------------------------
        # Scalar top-level fields.
        # ----------------------------------------------------

        for key in (
            "system_status",
            "total_count",
            "total_printed_count",
            "total_missing_count",
            "total_printed_bags_count",
            "total_not_printed_bags_count",
        ):

            if key in loaded:

                self._state[
                    key
                ] = loaded[key]


    # ======================================================
    # COUNT TYPE (ROI / LINE)
    # ======================================================

    COUNT_TYPES = ("roi", "line")

    @classmethod
    def normalize_count_type(cls, value, default="roi"):
        """Return "roi" or "line"; unknown values give `default`."""
        text = str(value if value is not None else "").strip().lower()
        if text in ("roi", "entry_roi", "entry"):
            return "roi"
        if text in ("line", "line_counting", "crossing"):
            return "line"
        return default

    def get_count_type(self):
        with self._lock:
            return self._state.get("count_type", "roi")

    def set_count_type(self, count_type):
        """
        Switch the count shown between ROI entry counting and
        counting-line counting. Camera counts and totals are
        recalculated immediately. Raises ValueError if unknown.
        """
        normalized = self.normalize_count_type(
            count_type,
            default=None,
        )

        if normalized is None:
            raise ValueError(
                "count_type must be 'roi' or 'line' "
                f"(got {count_type!r})."
            )

        with self._lock:
            changed = self._state.get("count_type") != normalized
            self._state["count_type"] = normalized
            self._recalculate_totals_locked()
            if changed:
                self._touch_locked()

        if changed:
            logger.info(
                "Dashboard count type set to '%s'.",
                normalized,
            )
            self._publish_throttled()

        return normalized

    # ======================================================
    # CAMERA REGISTRATION
    # ======================================================

    def register_camera(
        self,
        camera_name,
        *,
        camera_id=None,
        enabled=True,
        configured=True,
        mode=None,
        print_detection_enabled=False,
        jam_detection_enabled=False,
        source_type=None,
        metadata=None,
        ai_enabled=True,
        count_enabled=True,
        include_bags_in_total=True,
        include_prints_in_total=True,
    ):
        """
        Register a camera in dashboard state.

        ai_enabled        False = stream only (no model, no counting)
        count_enabled     False = no bag counting for this camera
        include_bags_in_total  False = not added to total production
        include_prints_in_total  False = print counts hidden on the dashboard
                          and not added to total printed / not printed

        This should normally be called once while building
        each camera pipeline.

        Jam fields are initialized here so every camera has
        a predictable schema before the first processed
        frame arrives.
        """

        name = str(
            camera_name
        )

        now = utc_now_iso()

        with self._lock:

            existing = (
                self._state[
                    "cameras"
                ].get(
                    name,
                    {}
                )
            )

            normalized_jam_enabled = (
                safe_bool(
                    existing.get(
                        "jam_detection_enabled",
                        jam_detection_enabled,
                    )
                )
            )

            camera_state = {

                "id":
                    camera_id,

                "name":
                    name,

                "enabled":
                    safe_bool(
                        enabled,
                        True,
                    ),

                "configured":
                    safe_bool(
                        configured,
                        True,
                    ),

                "mode":
                    mode,

                "source_type":
                    source_type,

                # Per-camera switches (from config.yaml)
                "ai_enabled":
                    safe_bool(ai_enabled, True),
                "count_enabled":
                    safe_bool(count_enabled, True),
                "include_bags_in_total":
                    safe_bool(include_bags_in_total, True),
                "include_prints_in_total":
                    safe_bool(include_prints_in_total, True),
                "line_count":
                    safe_int(
                        existing.get(
                            "line_count",
                            existing.get(
                                "total_count",
                                existing.get("count", 0),
                            ),
                        )
                    ),

                "status":
                    existing.get(
                        "status",
                        "offline",
                    ),

                "count":
                    safe_int(
                        existing.get(
                            "count",
                            0,
                        )
                    ),

                "total_count":
                    safe_int(
                        existing.get(
                            "total_count",
                            existing.get(
                                "count",
                                0,
                            ),
                        )
                    ),

                "entry_roi_count":
                    safe_int(
                        existing.get(
                            "entry_roi_count",
                            existing.get(
                                "total_count",
                                existing.get(
                                    "count",
                                    0,
                                ),
                            ),
                        )
                    ),

                "print_roi_count":
                    safe_int(
                        existing.get(
                            "print_roi_count",
                            0,
                        )
                    ),

                "print_roi":
                    existing.get(
                        "print_roi"
                    ),

                "printed_count":
                    safe_int(
                        existing.get(
                            "printed_count",
                            0,
                        )
                    ),

                "missing_count":
                    safe_int(
                        existing.get(
                            "missing_count",
                            0,
                        )
                    ),

                # ------------------------------------------
                # Compatibility aliases
                # ------------------------------------------

                "printed_bags_count":
                    safe_int(
                        existing.get(
                            "printed_bags_count",
                            existing.get(
                                "printed_count",
                                0,
                            ),
                        )
                    ),

                "not_printed_bags_count":
                    safe_int(
                        existing.get(
                            "not_printed_bags_count",
                            existing.get(
                                "missing_count",
                                0,
                            ),
                        )
                    ),

                # ------------------------------------------
                # PRINT DETECTION
                # ------------------------------------------

                "print_detection_enabled":
                    safe_bool(
                        print_detection_enabled
                    ),

                "print_status":
                    existing.get(
                        "print_status",
                        (
                            "unknown"
                            if print_detection_enabled
                            else "disabled"
                        ),
                    ),

                # ------------------------------------------
                # JAM DETECTION
                # ------------------------------------------

                "jam_detection_enabled":
                    normalized_jam_enabled,

                "jam_status":
                    normalize_jam_status(
                        existing.get(
                            "jam_status",
                            "normal",
                        ),
                        normalized_jam_enabled,
                    ),

                "jam_detected":
                    safe_bool(
                        existing.get(
                            "jam_detected",
                            False,
                        )
                    ),

                "jam_warning":
                    safe_bool(
                        existing.get(
                            "jam_warning",
                            False,
                        )
                    ),

                "active_jam_count":
                    max(
                        0,
                        safe_int(
                            existing.get(
                                "active_jam_count",
                                0,
                            )
                        ),
                    ),

                "active_jam_track_ids":
                    normalize_track_ids(
                        existing.get(
                            "active_jam_track_ids",
                            [],
                        )
                    ),

                # ------------------------------------------
                # CONDITION C
                # ------------------------------------------

                "condition_c_enabled":
                    safe_bool(
                        existing.get(
                            "condition_c_enabled",
                            False,
                        )
                    ),

                "condition_c_detected":
                    safe_bool(
                        existing.get(
                            "condition_c_detected",
                            False,
                        )
                    ),

                "condition_c_bag_count":
                    safe_int(
                        existing.get(
                            "condition_c_bag_count",
                            0,
                        )
                    ),

                "condition_c_track_ids":
                    normalize_track_ids(
                        existing.get(
                            "condition_c_track_ids",
                            [],
                        )
                    ),

                "condition_c_status":
                    existing.get(
                        "condition_c_status",
                        "normal",
                    ),

                "condition_c_minimum_gap_mm":
                    existing.get(
                        "condition_c_minimum_gap_mm"
                    ),

                "condition_c_distances":
                    existing.get(
                        "condition_c_distances",
                        [],
                    ),

                "condition_c_image_path":
                    existing.get(
                        "condition_c_image_path"
                    ),

                "condition_c_image_url":
                    existing.get(
                        "condition_c_image_url"
                    ),

                "condition_c_timestamp":
                    existing.get(
                        "condition_c_timestamp"
                    ),

                # ------------------------------------------
                # FPS
                # ------------------------------------------

                "fps":
                    safe_float(
                        existing.get(
                            "fps",
                            0.0,
                        )
                    ),

                "frame_count":
                    safe_int(
                        existing.get(
                            "frame_count",
                            0,
                        )
                    ),

                "last_frame_at":
                    existing.get(
                        "last_frame_at"
                    ),

                "last_detection_at":
                    existing.get(
                        "last_detection_at"
                    ),

                "last_count_at":
                    existing.get(
                        "last_count_at"
                    ),

                "last_error":
                    existing.get(
                        "last_error"
                    ),

                "registered_at":
                    existing.get(
                        "registered_at",
                        now,
                    ),

                "updated_at":
                    now,

                "metadata":
                    (
                        deepcopy(
                            metadata
                        )
                        if isinstance(
                            metadata,
                            dict,
                        )
                        else {}
                    ),
            }

            self._state[
                "cameras"
            ][name] = camera_state

            self._touch_locked()

            self._recalculate_totals_locked()


    # ======================================================
    # CAMERA UPDATE
    # ======================================================

    def update_camera(
        self,
        camera_name,
        *,
        status=None,
        count=None,
        total_count=None,
        entry_roi_count=None,
        line_count=None,
        printed_count=None,
        missing_count=None,
        print_roi_count=None,
        print_roi=None,
        printed_bags_count=None,
        not_printed_bags_count=None,
        fps=None,
        frame_count=None,
        print_status=None,
        print_detection_enabled=None,

        # --------------------------------------------------
        # JAM DETECTION
        # --------------------------------------------------

        jam_detection_enabled=None,
        jam_status=None,
        jam_detected=None,
        jam_warning=None,
        active_jam_count=None,
        active_jam_track_ids=None,

        enabled=None,
        configured=None,
        mode=None,
        source_type=None,
        last_frame_at=None,
        last_detection_at=None,
        last_count_at=None,
        last_error=None,
        metadata=None,
        **extra_fields,
    ):
        """
        Update camera runtime state.

        The common jam fields are explicit parameters.

        Additional future fields are still supported through
        **extra_fields, so the method remains extensible.
        """

        name = str(
            camera_name
        )

        with self._lock:

            if (
                name
                not in
                self._state[
                    "cameras"
                ]
            ):

                self.register_camera(
                    name,
                    print_detection_enabled=(
                        print_detection_enabled
                        if print_detection_enabled
                        is not None
                        else False
                    ),
                    jam_detection_enabled=(
                        jam_detection_enabled
                        if jam_detection_enabled
                        is not None
                        else False
                    ),
                )


            camera = (
                self._state[
                    "cameras"
                ][name]
            )


            # ----------------------------------------------
            # Status
            # ----------------------------------------------

            if status is not None:

                camera[
                    "status"
                ] = str(
                    status
                ).lower()


            # ----------------------------------------------
            # Count
            # ----------------------------------------------

            if total_count is not None:

                normalized_count = (
                    max(
                        0,
                        safe_int(
                            total_count
                        ),
                    )
                )

                camera[
                    "count"
                ] = normalized_count

                camera[
                    "total_count"
                ] = normalized_count

            elif count is not None:

                normalized_count = (
                    max(
                        0,
                        safe_int(
                            count
                        ),
                    )
                )

                camera[
                    "count"
                ] = normalized_count

                camera[
                    "total_count"
                ] = normalized_count


            # ----------------------------------------------
            # Entry ROI production count
            # ----------------------------------------------

            if entry_roi_count is not None:

                camera[
                    "entry_roi_count"
                ] = max(
                    0,
                    safe_int(
                        entry_roi_count
                    ),
                )

            elif (
                "entry_roi_count"
                not in camera
            ):

                camera[
                    "entry_roi_count"
                ] = safe_int(
                    camera.get(
                        "total_count",
                        camera.get(
                            "count",
                            0,
                        ),
                    )
                )


            # ----------------------------------------------
            # Counting-line count (shown when count_type="line")
            # ----------------------------------------------
            if line_count is not None:
                camera[
                    "line_count"
                ] = max(
                    0,
                    safe_int(
                        line_count
                    ),
                )

            # ----------------------------------------------
            # Print ROI count (PrintROICounter total)
            # ----------------------------------------------

            if print_roi_count is not None:

                camera[
                    "print_roi_count"
                ] = max(
                    0,
                    safe_int(
                        print_roi_count
                    ),
                )

            elif "print_roi_count" not in camera:

                camera[
                    "print_roi_count"
                ] = 0

            if print_roi is not None:

                camera[
                    "print_roi"
                ] = self._json_safe(
                    print_roi
                )

            elif "print_roi" not in camera:

                camera[
                    "print_roi"
                ] = None

            # ----------------------------------------------
            # Printed count
            # ----------------------------------------------
            if printed_count is not None:

                normalized_printed = (
                    max(
                        0,
                        safe_int(
                            printed_count
                        ),
                    )
                )

                camera[
                    "printed_count"
                ] = normalized_printed

                camera[
                    "printed_bags_count"
                ] = normalized_printed

            elif printed_bags_count is not None:

                normalized_printed = (
                    max(
                        0,
                        safe_int(
                            printed_bags_count
                        ),
                    )
                )

                camera[
                    "printed_count"
                ] = normalized_printed

                camera[
                    "printed_bags_count"
                ] = normalized_printed


            # ----------------------------------------------
            # Missing print count
            # ----------------------------------------------

            if missing_count is not None:

                normalized_missing = (
                    max(
                        0,
                        safe_int(
                            missing_count
                        ),
                    )
                )

                camera[
                    "missing_count"
                ] = normalized_missing

                camera[
                    "not_printed_bags_count"
                ] = normalized_missing

            elif (
                not_printed_bags_count
                is not None
            ):

                normalized_missing = (
                    max(
                        0,
                        safe_int(
                            not_printed_bags_count
                        ),
                    )
                )

                camera[
                    "missing_count"
                ] = normalized_missing

                camera[
                    "not_printed_bags_count"
                ] = normalized_missing


            # ----------------------------------------------
            # FPS
            # ----------------------------------------------

            if fps is not None:

                camera[
                    "fps"
                ] = max(
                    0.0,
                    safe_float(
                        fps
                    ),
                )


            # ----------------------------------------------
            # Frame count
            # ----------------------------------------------

            if frame_count is not None:

                camera[
                    "frame_count"
                ] = max(
                    0,
                    safe_int(
                        frame_count
                    ),
                )


            # ----------------------------------------------
            # PRINT DETECTION
            # ----------------------------------------------

            if (
                print_detection_enabled
                is not None
            ):

                camera[
                    "print_detection_enabled"
                ] = safe_bool(
                    print_detection_enabled
                )

                if not camera[
                    "print_detection_enabled"
                ]:

                    camera[
                        "print_status"
                    ] = "disabled"


            if print_status is not None:

                camera[
                    "print_status"
                ] = str(
                    print_status
                ).lower()


            # ----------------------------------------------
            # JAM DETECTION ENABLED
            # ----------------------------------------------

            if (
                jam_detection_enabled
                is not None
            ):

                camera[
                    "jam_detection_enabled"
                ] = safe_bool(
                    jam_detection_enabled
                )

                if not camera[
                    "jam_detection_enabled"
                ]:

                    camera[
                        "jam_status"
                    ] = "disabled"

                    camera[
                        "jam_detected"
                    ] = False

                    camera[
                        "jam_warning"
                    ] = False

                    camera[
                        "active_jam_count"
                    ] = 0

                    camera[
                        "active_jam_track_ids"
                    ] = []

                    camera[
                        "condition_c_detected"
                    ] = False

                    camera[
                        "condition_c_bag_count"
                    ] = 0

                    camera[
                        "condition_c_track_ids"
                    ] = []

                    camera[
                        "condition_c_status"
                    ] = "disabled"

                    camera[
                        "condition_c_minimum_gap_mm"
                    ] = None

                    camera[
                        "condition_c_distances"
                    ] = []

                    camera[
                        "condition_c_image_path"
                    ] = None

                    camera[
                        "condition_c_image_url"
                    ] = None

                    camera[
                        "condition_c_timestamp"
                    ] = None


            # ----------------------------------------------
            # JAM STATUS
            # ----------------------------------------------

            if jam_status is not None:

                camera[
                    "jam_status"
                ] = normalize_jam_status(
                    jam_status,
                    camera.get(
                        "jam_detection_enabled",
                        False,
                    ),
                )


            # ----------------------------------------------
            # JAM DETECTED
            # ----------------------------------------------

            if jam_detected is not None:

                camera[
                    "jam_detected"
                ] = safe_bool(
                    jam_detected
                )


            # ----------------------------------------------
            # JAM WARNING
            # ----------------------------------------------

            if jam_warning is not None:

                camera[
                    "jam_warning"
                ] = safe_bool(
                    jam_warning
                )


            # ----------------------------------------------
            # ACTIVE JAM TRACK IDS
            # ----------------------------------------------

            if (
                active_jam_track_ids
                is not None
            ):

                normalized_ids = (
                    normalize_track_ids(
                        active_jam_track_ids
                    )
                )

                camera[
                    "active_jam_track_ids"
                ] = normalized_ids

                # If active_jam_count was not supplied,
                # derive it from the track ID collection.

                if active_jam_count is None:

                    camera[
                        "active_jam_count"
                    ] = len(
                        normalized_ids
                    )


            # ----------------------------------------------
            # ACTIVE JAM COUNT
            # ----------------------------------------------

            if active_jam_count is not None:

                camera[
                    "active_jam_count"
                ] = max(
                    0,
                    safe_int(
                        active_jam_count
                    ),
                )


            # ----------------------------------------------
            # Keep jam flags consistent with JAM state
            # ----------------------------------------------

            current_jam_status = (
                normalize_jam_status(
                    camera.get(
                        "jam_status",
                        "normal",
                    ),
                    camera.get(
                        "jam_detection_enabled",
                        False,
                    ),
                )
            )

            camera[
                "jam_status"
            ] = current_jam_status


            if current_jam_status == "jam":

                camera[
                    "jam_detected"
                ] = True

                camera[
                    "jam_warning"
                ] = False

            elif current_jam_status == "warning":

                camera[
                    "jam_detected"
                ] = False

                camera[
                    "jam_warning"
                ] = True

            elif current_jam_status in {
                "normal",
                "slow",
                "recovering",
                "disabled",
            }:

                camera[
                    "jam_detected"
                ] = False

                camera[
                    "jam_warning"
                ] = False


            # ----------------------------------------------
            # Configuration state
            # ----------------------------------------------

            if enabled is not None:

                camera[
                    "enabled"
                ] = safe_bool(
                    enabled
                )


            if configured is not None:

                camera[
                    "configured"
                ] = safe_bool(
                    configured
                )


            if mode is not None:

                camera[
                    "mode"
                ] = mode


            if source_type is not None:

                camera[
                    "source_type"
                ] = source_type


            # ----------------------------------------------
            # Runtime timestamps
            # ----------------------------------------------

            if last_frame_at is not None:

                camera[
                    "last_frame_at"
                ] = self._normalize_timestamp(
                    last_frame_at
                )


            if last_detection_at is not None:

                camera[
                    "last_detection_at"
                ] = self._normalize_timestamp(
                    last_detection_at
                )


            if last_count_at is not None:

                camera[
                    "last_count_at"
                ] = self._normalize_timestamp(
                    last_count_at
                )


            # ----------------------------------------------
            # Error
            # ----------------------------------------------

            if last_error is not None:

                camera[
                    "last_error"
                ] = (
                    str(
                        last_error
                    )
                    if last_error
                    else None
                )


            # ----------------------------------------------
            # Metadata
            # ----------------------------------------------

            if isinstance(
                metadata,
                dict,
            ):

                current_metadata = (
                    camera.setdefault(
                        "metadata",
                        {},
                    )
                )

                current_metadata.update(
                    deepcopy(
                        metadata
                    )
                )


            # ----------------------------------------------
            # Additional future fields
            #
            # Examples:
            #
            # jam_track_id
            # jam_speed_px_s
            # jam_stationary_seconds
            # jam_distance_pixels
            # jam_center_x
            # jam_center_y
            # ----------------------------------------------

            for (
                key,
                value,
            ) in extra_fields.items():

                if key.startswith(
                    "_"
                ):
                    continue

                camera[
                    key
                ] = self._json_safe(
                    value
                )


            # ----------------------------------------------
            # Keep condition_c_status consistent
            # ----------------------------------------------

            condition_c_detected = safe_bool(
                camera.get(
                    "condition_c_detected",
                    False,
                )
            )

            if not camera.get(
                "condition_c_enabled",
                False,
            ):

                camera[
                    "condition_c_status"
                ] = "disabled"

            elif condition_c_detected:

                camera[
                    "condition_c_status"
                ] = "jam"

            else:

                camera[
                    "condition_c_status"
                ] = "normal"


            # ----------------------------------------------
            # Condition C timestamp
            # ----------------------------------------------

            condition_c_changed = any(
                key in extra_fields
                for key in (
                    "condition_c_detected",
                    "condition_c_bag_count",
                    "condition_c_track_ids",
                )
            )

            if condition_c_changed:

                camera[
                    "condition_c_timestamp"
                ] = utc_now_iso()


            # ----------------------------------------------
            # Condition C image URL
            # ----------------------------------------------

            if "condition_c_image_path" in extra_fields:

                condition_c_image_path = (
                    camera.get(
                        "condition_c_image_path"
                    )
                )

                camera[
                    "condition_c_image_url"
                ] = (
                    f"/condition-c/image/"
                    f"{Path(condition_c_image_path).name}"
                    if condition_c_image_path
                    else None
                )


            # ----------------------------------------------
            # Update timestamp
            # ----------------------------------------------

            camera[
                "updated_at"
            ] = utc_now_iso()


            self._touch_locked()

            self._recalculate_totals_locked()

        # In-memory state is already current at this point
        # (updated under the lock above). Whether this also
        # hits disk right now depends on persist_interval_seconds:
        # 0 (default) publishes immediately every call; a
        # positive value throttles disk writes to at most once
        # per that interval, while get_state()/snapshot() still
        # reflect the latest in-memory values regardless. Any
        # write skipped by the throttle stays "dirty" and will
        # be caught by the background auto-publish thread (if
        # running) on its own publish_interval cadence, or by an
        # explicit flush().
        self._publish_throttled()

    def camera_online(
        self,
        camera_name,
        **kwargs,
    ):
        """
        Mark camera online.
        """

        self.update_camera(
            camera_name,
            status="online",
            last_error="",
            **kwargs,
        )


    # ======================================================
    # CAMERA OFFLINE
    # ======================================================

    def camera_offline(
        self,
        camera_name,
        error=None,
        **kwargs,
    ):
        """
        Mark camera offline.
        """

        self.update_camera(
            camera_name,
            status="offline",
            fps=0.0,
            last_error=error,
            **kwargs,
        )


    # ======================================================
    # CAMERA ERROR
    # ======================================================

    def camera_error(
        self,
        camera_name,
        error,
        **kwargs,
    ):
        """
        Mark camera in error state.
        """

        self.update_camera(
            camera_name,
            status="error",
            fps=0.0,
            last_error=error,
            **kwargs,
        )


    # ======================================================
    # CAMERA FRAME
    # ======================================================

    def camera_frame(
        self,
        camera_name,
        *,
        fps=None,
        frame_count=None,
    ):
        """
        Convenience update when a frame is successfully
        processed.
        """

        self.update_camera(
            camera_name,
            status="online",
            fps=fps,
            frame_count=frame_count,
            last_frame_at=utc_now_iso(),
        )


    # ======================================================
    # COUNT EVENT UPDATE
    # ======================================================

    def update_count(
        self,
        camera_name,
        total_count,
        *,
        printed_count=None,
        missing_count=None,
        print_status=None,
    ):
        """
        Convenience method after a confirmed physical
        bag-center crossing.
        """

        self.update_camera(
            camera_name,
            total_count=total_count,
            printed_count=printed_count,
            missing_count=missing_count,
            print_status=print_status,
            last_count_at=utc_now_iso(),
        )


    # ======================================================
    # HEALTH UPDATE
    # ======================================================

    def update_health(
        self,
        health=None,
        **kwargs,
    ):
        """
        Update application/service health.
        """

        updates = {}

        if isinstance(
            health,
            dict,
        ):

            updates.update(
                health
            )


        updates.update(
            kwargs
        )


        with self._lock:

            service_status = (
                self._state[
                    "service_status"
                ]
            )


            for (
                key,
                value,
            ) in updates.items():

                if key in {
                    "model_loaded",
                    "inference_manager_running",
                    "elasticsearch_connected",
                    "dashboard_enabled",
                }:

                    service_status[
                        key
                    ] = safe_bool(
                        value
                    )

                else:

                    service_status[
                        key
                    ] = self._json_safe(
                        value
                    )


            self._touch_locked()

            self._update_system_status_locked()


    # ======================================================
    # SYSTEM STATUS
    # ======================================================

    def set_system_status(
        self,
        status,
    ):
        """
        Explicitly set application system status.

        Explicit status is authoritative and must not be
        overwritten by automatic system-status detection.
        """

        with self._lock:

            self._state[
                "system_status"
            ] = str(
                status
            ).lower()

            self._system_status_explicit = True

            self._touch_locked()

        # Persist explicit system-status changes immediately.
        self.publish(
            force=True
        )


    # ======================================================
    # MARK RUNNING
    # ======================================================

    def mark_running(
        self,
    ):

        self.set_system_status(
            "online"
        )


    # ======================================================
    # MARK STOPPING
    # ======================================================

    def mark_stopping(
        self,
    ):

        self.set_system_status(
            "stopping"
        )


    # ======================================================
    # MARK OFFLINE
    # ======================================================

    def mark_offline(
        self,
    ):

        with self._lock:

            self._state[
                "system_status"
            ] = "offline"

            for camera in (
                self._state[
                    "cameras"
                ].values()
            ):

                camera[
                    "status"
                ] = "offline"

                camera[
                    "fps"
                ] = 0.0

                camera[
                    "updated_at"
                ] = utc_now_iso()


            self._touch_locked()

        self.publish(
            force=True
        )


    # ======================================================
    # GET CAMERA
    # ======================================================

    def get_camera(
        self,
        camera_name,
    ):
        """
        Return safe copy of one camera.
        """

        with self._lock:

            camera = (
                self._state[
                    "cameras"
                ].get(
                    str(
                        camera_name
                    )
                )
            )

            if camera is None:
                return None

            return deepcopy(
                camera
            )


    # ======================================================
    # GET CAMERAS
    # ======================================================

    def get_cameras(
        self,
    ):
        """
        Return safe copy of all camera states.
        """

        with self._lock:

            return deepcopy(
                self._state[
                    "cameras"
                ]
            )


    # ======================================================
    # GET STATE
    # ======================================================

    def get_state(
        self,
    ):
        """
        Return complete dashboard state.
        """

        with self._lock:

            self._recalculate_totals_locked()

            snapshot = deepcopy(
                self._state
            )

        snapshot[
            "state_stale"
        ] = self._calculate_state_stale(
            snapshot
        )

        snapshot[
            "uptime_seconds"
        ] = max(
            0.0,
            time.monotonic()
            -
            self._startup_monotonic,
        )

        return snapshot


    # ======================================================
    # SNAPSHOT (ALIAS)
    # ======================================================

    def snapshot(
        self,
    ):
        """
        Alias for get_state(), for callers/tests using the
        "snapshot" naming instead.
        """

        return self.get_state()


    # ======================================================
    # GET STATUS
    # ======================================================

    def get_status(
        self,
    ):
        """
        Return DashboardState service information.
        """

        with self._lock:

            return {

                "running":
                    (
                        self._publisher_thread
                        is not None
                        and
                        self._publisher_thread.is_alive()
                    ),

                "auto_publish":
                    self.auto_publish,

                "publish_interval":
                    self.publish_interval,

                "stale_timeout":
                    self.stale_timeout,

                "storage":
                    "sql-server",

                "publish_count":
                    self._publish_count,

                "publish_errors":
                    self._publish_errors,

                "last_publish_time":
                    self._last_publish_time,

                "last_error":
                    self._last_error,
            }


    # ======================================================
    # RECALCULATE TOTALS
    # ======================================================

    def _recalculate_totals_locked(
        self,
    ):
        """
        Calculate global production totals from cameras.

        0. BAG vs PRINT: include_bags_in_total only affects the bag
           total; include_prints_in_total only affects the printed / not
           printed totals.
        1. COUNT TYPE: each camera publishes both entry_roi_count
           and line_count; its displayed count/total_count is the
           one selected by count_type ("roi" or "line"). Cameras
           that never published line_count (legacy callers) are
           left untouched.
        2. INCLUDE IN TOTAL: a camera with include_bags_in_total=false
           still shows its own numbers but is NOT added to
           total_count / total_printed_count / total_missing_count.
        """

        count_type = self.normalize_count_type(
            self._state.get("count_type", "roi")
        )

        self._state["count_type"] = count_type

        total_count = 0
        total_printed = 0
        total_missing = 0

        for camera in self._state["cameras"].values():

            if not isinstance(camera, dict):
                continue

            if "line_count" in camera:

                if count_type == "line":
                    selected = safe_int(camera.get("line_count", 0))
                else:
                    selected = safe_int(
                        camera.get(
                            "entry_roi_count",
                            camera.get(
                                "total_count",
                                camera.get("count", 0),
                            ),
                        )
                    )

                selected = max(0, selected)
                camera["count"] = selected
                camera["total_count"] = selected

            camera["count_type"] = count_type

            # Bag total: controlled by include_bags_in_total
            if safe_bool(
                camera.get("include_bags_in_total", True),
                True,
            ):
                total_count += max(
                    0,
                    safe_int(
                        camera.get(
                            "count",
                            camera.get("total_count", 0),
                        )
                    ),
                )

            # Print totals: controlled by include_prints_in_total
            if not safe_bool(
                camera.get("include_prints_in_total", True),
                True,
            ):
                continue

            total_printed += max(
                0,
                safe_int(
                    camera.get(
                        "printed_count",
                        camera.get("printed_bags_count", 0),
                    )
                ),
            )

            total_missing += max(
                0,
                safe_int(
                    camera.get(
                        "missing_count",
                        camera.get("not_printed_bags_count", 0),
                    )
                ),
            )

        self._state["total_count"] = total_count
        self._state["total_printed_count"] = total_printed
        self._state["total_missing_count"] = total_missing

        # Compatibility aliases
        self._state["total_printed_bags_count"] = total_printed
        self._state["total_not_printed_bags_count"] = total_missing

    # ======================================================
    # AUTOMATIC SYSTEM STATUS
    # ======================================================

    def _update_system_status_locked(
        self,
    ):
        """
        Update high-level status from application health.

        Explicit stopping/offline/error states are preserved.
        Explicitly assigned application states are also preserved.
        """

        current = str(
            self._state.get(
                "system_status",
                "",
            )
        ).lower()

        # Explicit application status must not be
        # overwritten by automatic health calculation.
        if getattr(
            self,
            "_system_status_explicit",
            False,
        ):
            return


        if current in {
            "stopping",
            "offline",
            "error",
        }:
            return


        health = (
            self._state[
                "service_status"
            ]
        )


        model_loaded = safe_bool(
            health.get(
                "model_loaded"
            )
        )

        inference_running = safe_bool(
            health.get(
                "inference_manager_running"
            )
        )


        if (
            model_loaded
            and
            inference_running
        ):

            self._state[
                "system_status"
            ] = "online"

        elif model_loaded:

            self._state[
                "system_status"
            ] = "starting"

        else:

            self._state[
                "system_status"
            ] = "starting"


    # ======================================================
    # TOUCH STATE
    # ======================================================

    def _touch_locked(
        self,
    ):
        """
        Mark state as modified.
        """

        self._state[
            "updated_at"
        ] = utc_now_iso()

        self._dirty = True


    # ======================================================
    # STALE STATE
    # ======================================================

    def _calculate_state_stale(
        self,
        snapshot,
    ):
        """
        Determine whether state has stopped updating.
        """

        updated_at = snapshot.get(
            "updated_at"
        )

        if not updated_at:
            return True


        try:

            value = str(
                updated_at
            )

            if value.endswith(
                "Z"
            ):

                value = (
                    value[:-1]
                    + "+00:00"
                )


            updated = (
                datetime.fromisoformat(
                    value
                )
            )


            if updated.tzinfo is None:

                updated = (
                    updated.replace(
                        tzinfo=timezone.utc
                    )
                )


            age = (
                utc_now()
                -
                updated
            ).total_seconds()


            return (
                age
                >
                self.stale_timeout
            )


        except (
            TypeError,
            ValueError,
        ):

            return True


    # ======================================================
    # PUBLISH
    # ======================================================

    def publish(
        self,
        force=False,
    ):
        """
        Publish dashboard state to SQL Server.

        The entire method runs under a dedicated
        ``_publish_lock`` so only one thread is ever writing
        to the database at a time. ``_lock`` alone was not
        sufficient here because it was only held while
        copying state, not while the write was actually
        happening -- two publisher/caller threads could race
        past that copy step at nearly the same moment and
        then contend over the same SQL Server rows.
        """

        with self._publish_lock:

            with self._lock:

                if (
                    not force
                    and
                    not self._dirty
                ):

                    return False


                self._recalculate_totals_locked()


                snapshot = deepcopy(
                    self._state
                )


                snapshot[
                    "state_stale"
                ] = False


                snapshot[
                    "uptime_seconds"
                ] = max(
                    0.0,
                    time.monotonic()
                    -
                    self._startup_monotonic,
                )


            try:

                save_dashboard_state(
                    snapshot
                )


                with self._lock:

                    self._dirty = False

                    self._last_publish_monotonic = (
                        time.monotonic()
                    )

                    self._last_publish_time = (
                        utc_now_iso()
                    )

                    self._publish_count += 1

                    self._last_error = None


                return True


            except Exception as error:

                with self._lock:

                    self._publish_errors += 1

                    self._last_error = str(
                        error
                    )


                logger.exception(
                    "Failed publishing dashboard state "
                    "to SQL Server."
                )


                return False


    # ======================================================
    # FORCE PUBLISH
    # ======================================================

    def flush(
        self,
    ):
        """
        Immediately write latest state.
        """

        return self.publish(
            force=True
        )


    # ======================================================
    # THROTTLED PUBLISH
    # ======================================================

    def _publish_throttled(
        self,
    ):
        """
        Publish to disk, respecting persist_interval_seconds.

        In-memory state is always current the moment a caller
        like update_camera() returns -- this only decides
        whether *this particular call* also writes state.json
        right now, or leaves that to the next explicit flush()
        or the background auto-publish thread.
        """

        if self.persist_interval_seconds <= 0:

            self.publish(
                force=True
            )

            return

        with self._lock:

            elapsed = (
                time.monotonic()
                - self._last_publish_monotonic
            )

        if elapsed >= self.persist_interval_seconds:

            self.publish(
                force=True
            )

        # Otherwise: leave _dirty set (already true from
        # _touch_locked()). The background publisher picks it
        # up on its own interval if auto_publish is running;
        # otherwise the next throttled or explicit publish call
        # will flush it once the interval has elapsed.


    # ======================================================
    # BACKGROUND PUBLISHER
    # ======================================================

    def _publisher_loop(
        self,
    ):
        """
        Background state publisher.
        """

        logger.info(
            "Dashboard state publisher started."
        )


        while not self._stop_event.wait(
            self.publish_interval
        ):

            try:

                self.publish()

            except Exception:

                logger.exception(
                    (
                        "Unexpected dashboard "
                        "publisher error."
                    )
                )


        logger.info(
            "Dashboard state publisher stopped."
        )


    # ======================================================
    # START
    # ======================================================

    def start(
        self,
    ):
        """
        Start background publishing.
        """

        with self._lock:

            if (
                self._publisher_thread
                is not None
                and
                self._publisher_thread.is_alive()
            ):

                return


            self._stop_event.clear()


            self._publisher_thread = (
                threading.Thread(
                    target=self._publisher_loop,
                    name="DashboardStatePublisher",
                    daemon=True,
                )
            )


            self._publisher_thread.start()


    # ======================================================
    # STOP
    # ======================================================

    def stop(
        self,
        mark_offline=True,
    ):
        """
        Stop DashboardState cleanly.
        """

        if mark_offline:

            with self._lock:

                self._state[
                    "system_status"
                ] = "offline"


                for camera in (
                    self._state[
                        "cameras"
                    ].values()
                ):

                    camera[
                        "status"
                    ] = "offline"

                    camera[
                        "fps"
                    ] = 0.0

                    camera[
                        "updated_at"
                    ] = utc_now_iso()


                self._touch_locked()


            self.publish(
                force=True
            )


        self._stop_event.set()


        thread = (
            self._publisher_thread
        )


        if (
            thread is not None
            and
            thread.is_alive()
            and
            thread
            is not
            threading.current_thread()
        ):

            thread.join(
                timeout=max(
                    1.0,
                    self.publish_interval * 4,
                )
            )


        self._publisher_thread = None


    # ======================================================
    # RESET COUNTS
    # ======================================================

    def reset_counts(
        self,
        camera_name=None,
    ):
        """
        Reset dashboard production counters.

        IMPORTANT:
        This only resets DashboardState values.

        It does NOT reset the actual Counter object inside
        a running camera pipeline.
        """

        with self._lock:

            if camera_name is None:

                cameras = (
                    self._state[
                        "cameras"
                    ].values()
                )

            else:

                camera = (
                    self._state[
                        "cameras"
                    ].get(
                        str(
                            camera_name
                        )
                    )
                )

                if camera is None:
                    return False

                cameras = [
                    camera
                ]


            for camera in cameras:

                camera[
                    "count"
                ] = 0

                camera[
                    "total_count"
                ] = 0

                camera[
                    "entry_roi_count"
                ] = 0

                camera[
                    "line_count"
                ] = 0

                camera[
                    "printed_count"
                ] = 0

                camera[
                    "missing_count"
                ] = 0

                camera[
                    "printed_bags_count"
                ] = 0

                camera[
                    "not_printed_bags_count"
                ] = 0

                camera[
                    "last_count_at"
                ] = None

                camera[
                    "updated_at"
                ] = utc_now_iso()


            self._recalculate_totals_locked()

            self._touch_locked()


        return True


    # ======================================================
    # NORMALIZE TIMESTAMP
    # ======================================================

    @staticmethod
    def _normalize_timestamp(
        value,
    ):

        if value is None:
            return None


        if isinstance(
            value,
            datetime,
        ):

            if value.tzinfo is None:

                value = (
                    value.replace(
                        tzinfo=timezone.utc
                    )
                )

            return value.isoformat()


        return str(
            value
        )


    # ======================================================
    # JSON SAFE
    # ======================================================

    @staticmethod
    def _json_safe(
        value,
    ):
        """
        Convert common runtime values into JSON-safe data.
        """

        if value is None:
            return None


        if isinstance(
            value,
            (
                str,
                int,
                float,
                bool,
            ),
        ):

            return value


        if isinstance(
            value,
            datetime,
        ):

            return value.isoformat()


        if isinstance(
            value,
            Path,
        ):

            return str(
                value
            )


        if isinstance(
            value,
            dict,
        ):

            return {

                str(key):
                    DashboardState._json_safe(
                        item
                    )

                for (
                    key,
                    item,
                )
                in value.items()

            }


        if isinstance(
            value,
            (
                list,
                tuple,
                set,
            ),
        ):

            return [

                DashboardState._json_safe(
                    item
                )

                for item in value

            ]


        # --------------------------------------------------
        # NumPy scalar compatibility
        # --------------------------------------------------

        item_method = getattr(
            value,
            "item",
            None,
        )


        if callable(
            item_method
        ):

            try:

                return item_method()

            except Exception:

                pass


        return str(
            value
        )


    # ======================================================
    # CONTEXT MANAGER
    # ======================================================

    def __enter__(
        self,
    ):

        self.start()

        return self


    def __exit__(
        self,
        exc_type,
        exc_value,
        traceback,
    ):

        self.stop()