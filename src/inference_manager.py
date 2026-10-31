"""
==========================================================
FillPac AI
Inference Manager
==========================================================

Purpose
-------
Centralized inference manager for multi-camera processing.

Architecture
------------
Camera 1 ─┐
Camera 2 ─┤
Camera 3 ─┼──> InferenceManager ──> ONE YOLO Detector
Camera 4 ─┘

Each camera keeps only its latest pending frame.

Important
---------
- Only ONE YOLO model is used.
- Only ONE worker performs YOLO inference.
- Old pending frames are replaced by newer frames.
- Tracking and counting remain camera-specific.
- Suitable for MP4 development and RTSP production.
==========================================================
"""

import threading
import time
from dataclasses import dataclass


@dataclass
class InferenceRequest:
    """
    Represents one camera inference request.
    """

    camera_name: str
    frame: object
    request_id: int
    submitted_at: float


@dataclass
class InferenceResult:
    """
    Represents the result of one inference request.
    """

    camera_name: str
    request_id: int
    detections: list
    submitted_at: float
    completed_at: float
    inference_time: float
    error: str | None = None


class InferenceManager:

    def __init__(
        self,
        detector,
        logger=None,
    ):
        """
        Parameters
        ----------
        detector:
            Shared Detector instance.

            This Detector owns the ONE YOLO model.

        logger:
            Application logger.
        """

        if detector is None:
            raise ValueError(
                "InferenceManager requires "
                "a valid Detector instance."
            )

        self.detector = detector

        self.logger = logger

        # ==================================================
        # SYNCHRONIZATION
        # ==================================================

        self._lock = threading.RLock()

        self._condition = threading.Condition(
            self._lock
        )

        self._stop_event = threading.Event()

        # ==================================================
        # REQUEST STORAGE
        #
        # Only ONE pending request is stored per camera.
        #
        # If a newer frame arrives before the previous
        # pending frame is processed, the old pending frame
        # is replaced.
        # ==================================================

        self._pending_requests = {}

        # ==================================================
        # RESULTS
        #
        # Latest completed result for each camera.
        # ==================================================

        self._results = {}

        # ==================================================
        # REQUEST COUNTERS
        # ==================================================

        self._request_counters = {}

        # ==================================================
        # CAMERA PROCESSING ORDER
        #
        # Used for basic round-robin fairness.
        # ==================================================

        self._camera_order = []

        self._next_camera_index = 0

        # ==================================================
        # STATISTICS
        # ==================================================

        self.total_requests = 0

        self.total_inferences = 0

        self.total_replaced_frames = 0

        self.total_errors = 0

        # ==================================================
        # WORKER
        # ==================================================

        self._worker_thread = None

        self._started = False

    # ======================================================
    # START
    # ======================================================

    def start(self):

        with self._lock:

            if (
                self._worker_thread
                is not None
                and self._worker_thread.is_alive()
            ):
                return

            self._stop_event.clear()

            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                daemon=True,
                name="InferenceManager",
            )

            self._worker_thread.start()

            self._started = True

        self._log(
            "info",
            "InferenceManager started. "
            "Using one shared YOLO model.",
        )

    # ======================================================
    # SUBMIT FRAME
    # ======================================================

    def submit(
        self,
        camera_name,
        frame,
    ):
        """
        Submit the latest frame for a camera.

        Returns
        -------
        request_id : int

        Notes
        -----
        If the camera already has a pending frame,
        that pending frame is replaced with this
        newer frame.
        """

        if frame is None:
            return None

        if not self._started:
            raise RuntimeError(
                "InferenceManager has not been started."
            )

        if self._stop_event.is_set():
            return None

        with self._condition:

            # ----------------------------------------------
            # Register camera
            # ----------------------------------------------

            if (
                camera_name
                not in self._camera_order
            ):

                self._camera_order.append(
                    camera_name
                )

            # ----------------------------------------------
            # Generate request ID
            # ----------------------------------------------

            request_id = (
                self._request_counters.get(
                    camera_name,
                    0,
                )
                + 1
            )

            self._request_counters[
                camera_name
            ] = request_id

            # ----------------------------------------------
            # Check frame replacement
            # ----------------------------------------------

            if (
                camera_name
                in self._pending_requests
            ):

                self.total_replaced_frames += 1

            # ----------------------------------------------
            # Store latest frame
            # ----------------------------------------------

            self._pending_requests[
                camera_name
            ] = InferenceRequest(
                camera_name=camera_name,
                frame=frame,
                request_id=request_id,
                submitted_at=time.monotonic(),
            )

            self.total_requests += 1

            # Log queue size to detect overload
            queue_size = len(self._pending_requests)

            if queue_size > 5:
                self._log(
                    "warning",
                    f"Inference queue size {queue_size} (camera: {camera_name})",
                )

            # Wake inference worker
            self._condition.notify()

        return request_id

    # ======================================================
    # GET RESULT
    # ======================================================

    def get_result(
        self,
        camera_name,
        request_id=None,
    ):
        """
        Get the latest completed inference result.

        Parameters
        ----------
        camera_name:
            Camera requesting its result.

        request_id:
            Optional request ID.

            If provided, a result is returned only
            when the completed result matches or is
            newer than this request.

        Returns
        -------
        InferenceResult or None
        """

        with self._lock:

            result = self._results.get(
                camera_name
            )

            if result is None:
                return None

            if (
                request_id is not None
                and result.request_id
                < request_id
            ):
                return None

            return result

    # ======================================================
    # WAIT FOR RESULT
    # ======================================================

    def infer(
        self,
        camera_name,
        frame,
        timeout=15.0,
    ):
        """
        Submit a frame and wait for its inference result.

        This method provides a simple interface for
        Pipeline:

            detections = inference_manager.infer(
                camera_name,
                frame,
            )

        The actual YOLO inference still runs only in
        the central InferenceManager worker.

        Returns
        -------
        list
            Detection dictionaries.

        Raises
        ------
        TimeoutError
            If inference does not finish before timeout.

        RuntimeError
            If inference fails.
        """

        request_id = self.submit(
            camera_name,
            frame,
        )

        if request_id is None:
            return []

        deadline = (
            time.monotonic()
            + float(timeout)
        )

        with self._condition:

            while not self._stop_event.is_set():

                result = self._results.get(
                    camera_name
                )

                if (
                    result is not None
                    and result.request_id
                    >= request_id
                ):

                    if result.error:

                        raise RuntimeError(
                            "Inference failed for "
                            f"{camera_name}: "
                            f"{result.error}"
                        )

                    return result.detections

                remaining = (
                    deadline
                    - time.monotonic()
                )

                if remaining <= 0:

                    raise TimeoutError(
                        "Inference timeout for "
                        f"{camera_name} "
                        f"(request {request_id})."
                    )

                self._condition.wait(
                    timeout=min(
                        remaining,
                        0.1,
                    )
                )

        return []

    # ======================================================
    # WORKER LOOP
    # ======================================================

    def _worker_loop(self):

        self._log(
            "info",
            "Inference worker running.",
        )

        while not self._stop_event.is_set():

            request = (
                self._get_next_request()
            )

            if request is None:
                continue

            self._process_request(
                request
            )

        self._log(
            "info",
            "Inference worker stopped.",
        )

    # ======================================================
    # GET NEXT REQUEST
    # ======================================================

    def _get_next_request(self):
        """
        Select next camera request using basic
        round-robin scheduling.
        """

        with self._condition:

            while (
                not self._pending_requests
                and not self._stop_event.is_set()
            ):

                self._condition.wait(
                    timeout=0.5
                )

            if self._stop_event.is_set():
                return None

            if not self._camera_order:
                return None

            camera_count = len(
                self._camera_order
            )

            # ----------------------------------------------
            # Round-robin camera selection
            # ----------------------------------------------

            for offset in range(
                camera_count
            ):

                index = (
                    self._next_camera_index
                    + offset
                ) % camera_count

                camera_name = (
                    self._camera_order[
                        index
                    ]
                )

                request = (
                    self._pending_requests.pop(
                        camera_name,
                        None,
                    )
                )

                if request is not None:

                    self._next_camera_index = (
                        index + 1
                    ) % camera_count

                    return request

            return None

    # ======================================================
    # PROCESS REQUEST
    # ======================================================

    def _process_request(
        self,
        request,
    ):

        start_time = (
            time.monotonic()
        )

        try:

            detections = (
                self.detector.detect(
                    request.frame,
                    camera_name=
                        request.camera_name,
                )
            )

            completed_at = (
                time.monotonic()
            )

            result = InferenceResult(
                camera_name=
                    request.camera_name,
                request_id=
                    request.request_id,
                detections=
                    detections,
                submitted_at=
                    request.submitted_at,
                completed_at=
                    completed_at,
                inference_time=
                    completed_at
                    - start_time,
                error=None,
            )

            self.total_inferences += 1

            # Log slow inference
            if result.inference_time > 1:
                self._log(
                    "warning",
                    f"Inference for {request.camera_name} took {result.inference_time:.2f}s",
                )

        except Exception as error:

            completed_at = (
                time.monotonic()
            )

            self.total_errors += 1

            result = InferenceResult(
                camera_name=
                    request.camera_name,
                request_id=
                    request.request_id,
                detections=[],
                submitted_at=
                    request.submitted_at,
                completed_at=
                    completed_at,
                inference_time=
                    completed_at
                    - start_time,
                error=str(
                    error
                ),
            )

            self._log(
                "error",
                "Inference failed for "
                f"{request.camera_name}: "
                f"{error}",
            )

        # --------------------------------------------------
        # Publish result
        # --------------------------------------------------

        with self._condition:

            self._results[
                request.camera_name
            ] = result

            # Wake waiting pipeline
            self._condition.notify_all()

    # ======================================================
    # STATISTICS
    # ======================================================

    def get_stats(self):

        with self._lock:

            return {
                "started":
                    self._started,

                "worker_alive":
                    (
                        self._worker_thread
                        is not None
                        and
                        self._worker_thread.is_alive()
                    ),

                "pending_cameras":
                    len(
                        self._pending_requests
                    ),

                "registered_cameras":
                    len(
                        self._camera_order
                    ),

                "total_requests":
                    self.total_requests,

                "total_inferences":
                    self.total_inferences,

                "replaced_frames":
                    self.total_replaced_frames,

                "errors":
                    self.total_errors,
            }

    # ======================================================
    # STOP
    # ======================================================

    def stop(
        self,
        timeout=5.0,
    ):

        if not self._started:
            return

        self._log(
            "info",
            "Stopping InferenceManager.",
        )

        self._stop_event.set()

        with self._condition:
            self._condition.notify_all()

        if (
            self._worker_thread
            is not None
        ):

            self._worker_thread.join(
                timeout=timeout
            )

        with self._lock:

            self._pending_requests.clear()

            self._results.clear()

        self._worker_thread = None

        self._started = False

        self._log(
            "info",
            "InferenceManager closed.",
        )

    # ======================================================
    # LOGGER
    # ======================================================

    def _log(
        self,
        level,
        message,
    ):

        if self.logger is not None:

            log_method = getattr(
                self.logger,
                level,
                None,
            )

            if log_method:

                log_method(
                    message
                )

            return

        print(
            f"[{level.upper()}] "
            f"{message}"
        )