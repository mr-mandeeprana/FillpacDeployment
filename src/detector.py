"""
==========================================================
FillPac AI
YOLO Detector
==========================================================

Purpose
-------
Loads ONE YOLO model and performs object detection.

Production Architecture
-----------------------
Application
    |
    v
Detector (ONE YOLO model)
    |
    v
InferenceManager
    |
    +---- Camera 1 detections
    +---- Camera 2 detections
    +---- Camera 3 detections
    +---- Camera 4 detections

Important
---------
Detector contains NO tracking or counting state.

Each camera must maintain its own:
- Tracker
- Counter
- Print history
==========================================================
"""

from pathlib import Path
import threading
import time
import os

import numpy as np
import torch
from ultralytics import YOLO

# Limit CPU threads to reduce contention on multi-core machines
torch.set_num_threads(max(1, (os.cpu_count() or 1) // 2))
torch.set_num_interop_threads(2)


class Detector:
    """
    Shared YOLO detector.

    Only one instance of this class should normally be created
    by the FillPac AI application.

    The shared InferenceManager uses this detector for all
    enabled camera pipelines.
    """

    def __init__(
        self,
        model_path: str,
        confidence: float = 0.5,
        iou: float = 0.45,
        device: str = "cpu",
        image_size: int = 640,
        half: bool = True,
        max_detections: int = 100,
        allowed_classes=None,
        min_bbox_area: float = 0.0,
        bag_confidence=None,
        print_confidence=None,
        class_confidence_thresholds=None,
        detection_roi=None,
        logger=None,
    ):
        # ==================================================
        # CONFIGURATION
        # ==================================================

        self.model_path = Path(model_path)

        self.confidence = float(confidence)

        self.iou = float(iou)

        self.image_size = int(image_size)

        self.max_detections = int(max_detections)

        self.logger = logger

        # ==================================================
        # ALLOWED CLASSES
        # ==================================================

        self.allowed_classes = (
            {int(class_id) for class_id in allowed_classes}
            if allowed_classes is not None
            else None
        )

        # ==================================================
        # DETECTION FILTERS
        # ==================================================

        self.min_bbox_area = max(float(min_bbox_area), 0.0)

        self.class_confidence_thresholds = (
            self._build_class_confidence_thresholds(
                class_confidence_thresholds=class_confidence_thresholds,
                bag_confidence=bag_confidence,
                print_confidence=print_confidence,
            )
        )

        self.detection_roi = self._validate_detection_roi(detection_roi)

        # Per-camera filter sets (see register_camera_filters).
        # The YOLO model is shared, but each camera keeps its OWN
        # confidence / detection ROI / min area / classes.
        self._camera_filters = {}

        # Restrict YOLO post-processing to configured classes.
        # This reduces NMS/post-processing work without changing the
        # downstream detection format or counting logic.
        self.predict_classes = (
            sorted(self.allowed_classes)
            if self.allowed_classes is not None
            else None
        )

        # ==================================================
        # DETECTOR STATE
        # ==================================================

        self.frame_index = 0

        # Additional protection around the shared model.
        #
        # In the intended architecture, only the single
        # InferenceManager worker calls detect().
        #
        # This lock protects the model if another thread
        # accidentally calls detect() directly.
        self._inference_lock = threading.Lock()

        # ==================================================
        # DEVICE
        # ==================================================

        requested_device = str(device).strip().lower()

        # Support:
        #   cpu
        #   cuda
        #   cuda:0
        #   cuda:1
        #   gpu
        is_cuda_request = (
            requested_device in {"cuda", "gpu"}
            or requested_device.startswith("cuda:")
        )

        if is_cuda_request and torch.cuda.is_available():

            # Explicit GPU index, e.g. cuda:0
            if requested_device.startswith("cuda:"):
                try:
                    gpu_index = int(requested_device.split(":", 1)[1])

                    if gpu_index < 0 or gpu_index >= torch.cuda.device_count():
                        raise ValueError(
                            f"Invalid CUDA device index: {gpu_index}. "
                            f"Available GPUs: {torch.cuda.device_count()}"
                        )

                    self.device = requested_device

                except (ValueError, IndexError):
                    self._log(
                        "warning",
                        f"Invalid CUDA device '{requested_device}'. "
                        "Falling back to cuda:0.",
                    )
                    self.device = "cuda:0"

            else:
                # "cuda" / "gpu" means first GPU
                self.device = "cuda:0"

        else:

            if is_cuda_request:
                self._log(
                    "warning",
                    "CUDA requested but unavailable. "
                    "Falling back to CPU.",
                )

            self.device = "cpu"

        # --------------------------------------------------
        # FP16 / Quantization
        # --------------------------------------------------
        #
        # Keep the existing "half" configuration parameter
        # for compatibility with config.yaml.
        #
        # We do NOT pass half= to Ultralytics predict().
        #
        # When CUDA is available and half=True, the newer
        # quantize="fp16" argument is used instead.
        #
        # CPU inference remains full precision.
        # --------------------------------------------------

        self.half = bool(
            half
            and self.device.startswith("cuda")
        )

        # Precision kwargs passed to predict(); finalized during warm-up.
        self._precision_kwargs = (
            {"quantize": "fp16"} if self.half else {}
        )

        # Fixed-size camera frames benefit from cuDNN autotuning.
        # This is only enabled for CUDA; CPU behavior is unchanged.
        if self.device.startswith("cuda"):
            try:
                torch.backends.cudnn.benchmark = True
            except Exception:
                pass

        # ==================================================
        # MODEL INITIALIZATION
        # ==================================================

        self._validate_model_file()

        self._log(
            "info",
            f"Loading YOLO model: {self.model_path}",
        )

        self._load_model()

        self._fuse_model()

        self._warm_up_model()

        self._log(
            "info",
            "YOLO model ready. "
            f"Device: {self.device}, "
            f"imgsz: {self.image_size}, "
            f"fp16: {self.half}",
        )

    # ======================================================
    # BUILD PREDICTION ARGUMENTS
    # ======================================================

    def _get_precision_kwargs(self):
        """
        Return the Ultralytics precision arguments in use.

        CUDA + half=True:
            quantize="fp16" (or half=True if the installed
            Ultralytics version rejects "quantize"; this is
            decided once during warm-up)

        CPU:
            no extra arguments

        Keeping this in one place ensures normal inference and
        warm-up use identical precision settings.
        """

        if self.half and self.device.startswith("cuda"):
            return dict(self._precision_kwargs)

        return {}

    # ======================================================
    # DETECT
    # ======================================================

    def detect(self, frame, camera_name=None):
        """
        Run YOLO inference on one frame.

        Returns
        -------
        list
            List of detection dictionaries.
        """

        if frame is None:
            return []

        # --------------------------------------------------
        # Shared detector safety
        # --------------------------------------------------

        with self._inference_lock:
            self.frame_index += 1

            frame_index = self.frame_index

            timestamp = time.time()

            # ----------------------------------------------
            # Precision configuration
            # ----------------------------------------------

            predict_kwargs = self._get_precision_kwargs()

            # ----------------------------------------------
            # YOLO INFERENCE (timed)
            # ----------------------------------------------

            start = time.perf_counter()

            with torch.inference_mode():
                results = self.model.predict(
                    source=frame,
                    conf=self.confidence,
                    iou=self.iou,
                    imgsz=self.image_size,
                    max_det=self.max_detections,
                    device=self.device,
                    classes=self.predict_classes,
                    rect=True,
                    agnostic_nms=False,
                    verbose=False,
                    stream=False,
                    **predict_kwargs,
                )

            elapsed = time.perf_counter() - start

            if elapsed > 1:
                self._log(
                    "warning",
                    f"Inference time {elapsed:.2f}s",
                )

        # ==================================================
        # BUILD DETECTIONS
        # ==================================================

        detections = []

        # Filters of the camera that submitted this frame.
        filters = self._camera_filters.get(camera_name)

        for result in results:
            if result.boxes is None:
                continue

            for box in result.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())

                class_id = int(box.cls[0])

                confidence = float(box.conf[0])

                detection = self._build_detection(
                    bbox=(x1, y1, x2, y2),
                    confidence=confidence,
                    class_id=class_id,
                    frame_index=frame_index,
                    timestamp=timestamp,
                )

                if self._passes_detection_filters(detection, filters):
                    detections.append(detection)

        return detections

    # ======================================================
    # CLASS NAME
    # ======================================================

    def get_class_name(self, class_id):
        """
        Return the YOLO class name for a class ID.
        """

        if isinstance(self.model.names, dict):
            return self.model.names.get(class_id, str(class_id))

        if 0 <= class_id < len(self.model.names):
            return self.model.names[class_id]

        return str(class_id)

    # ======================================================
    # CLASS CONFIDENCE THRESHOLDS
    # ======================================================

    def _build_class_confidence_thresholds(
        self,
        class_confidence_thresholds,
        bag_confidence,
        print_confidence,
    ):
        """
        Build per-class confidence thresholds.

        Class 0:
            Bag

        Class 1:
            Print
        """

        thresholds = {}

        for class_id, threshold in (
            class_confidence_thresholds or {}
        ).items():
            thresholds[int(class_id)] = float(threshold)

        if bag_confidence is not None:
            thresholds[0] = float(bag_confidence)

        if print_confidence is not None:
            thresholds[1] = float(print_confidence)

        return thresholds

    # ======================================================
    # BUILD DETECTION
    # ======================================================

    def _build_detection(
        self,
        bbox,
        confidence,
        class_id,
        frame_index,
        timestamp,
    ):
        """
        Convert a raw YOLO box into the FillPac AI
        detection dictionary format.
        """

        x1, y1, x2, y2 = bbox

        width = max(x2 - x1, 0)

        height = max(y2 - y1, 0)

        area = width * height

        center = (
            (x1 + x2) // 2,
            (y1 + y2) // 2,
        )

        return {
            "bbox": bbox,
            "center": center,
            "width": width,
            "height": height,
            "area": area,
            "confidence": confidence,
            "class_id": class_id,
            "class_name": self.get_class_name(class_id),
            "frame_index": frame_index,
            "timestamp": timestamp,
        }

    # ======================================================
    # DETECTION FILTERS
    # ======================================================

    def register_camera_filters(self, camera_name, model_config):
        """
        Register the per-camera model settings for ONE camera.

        The YOLO model is shared by every camera, but these settings
        belong to each camera and must not be taken from whichever
        camera happened to be first:

            confidence, bag_confidence, print_confidence,
            class_confidence_thresholds, min_bbox_area,
            detection_roi, allowed_classes

        Shared YOLO parameters are widened so no camera loses
        detections it needs (lowest confidence, union of classes,
        highest max_detections); each camera is then filtered with
        its own values in _passes_detection_filters().
        """

        cfg = model_config or {}

        confidence = float(cfg.get("confidence", self.confidence))

        allowed = cfg.get("allowed_classes")

        allowed_set = (
            {int(class_id) for class_id in allowed}
            if allowed is not None
            else None
        )

        self._camera_filters[camera_name] = {
            "confidence": confidence,
            "class_thresholds": self._build_class_confidence_thresholds(
                class_confidence_thresholds=cfg.get(
                    "class_confidence_thresholds"
                ),
                bag_confidence=cfg.get("bag_confidence"),
                print_confidence=cfg.get("print_confidence"),
            ),
            "min_bbox_area": max(
                float(cfg.get("min_bbox_area", 0) or 0),
                0.0,
            ),
            "detection_roi": self._validate_detection_roi(
                cfg.get("detection_roi")
            ),
            "allowed_classes": allowed_set,
        }

        # ---- widen the shared YOLO call so every camera is served ----

        self.confidence = min(self.confidence, confidence)

        self.max_detections = max(
            self.max_detections,
            int(cfg.get("max_detections", self.max_detections)),
        )

        if allowed_set is None:
            self.predict_classes = None
            self.allowed_classes = None
        elif self.predict_classes is not None:
            self.allowed_classes = (
                set(self.allowed_classes or set()) | allowed_set
            )
            self.predict_classes = sorted(self.allowed_classes)

    def _passes_detection_filters(self, detection, filters=None):
        """
        Apply FillPac-specific detection filtering.

        `filters` is the per-camera filter set from
        register_camera_filters(); when it is None the detector's
        own (global) settings are used.
        """

        if filters is None:
            allowed_classes = self.allowed_classes
            default_confidence = self.confidence
            class_thresholds = self.class_confidence_thresholds
            min_bbox_area = self.min_bbox_area
            detection_roi = self.detection_roi
        else:
            allowed_classes = filters["allowed_classes"]
            default_confidence = filters["confidence"]
            class_thresholds = filters["class_thresholds"]
            min_bbox_area = filters["min_bbox_area"]
            detection_roi = filters["detection_roi"]

        class_id = detection["class_id"]

        # Allowed classes
        if (
            allowed_classes is not None
            and class_id not in allowed_classes
        ):
            return False

        # Per-class confidence
        confidence_threshold = class_thresholds.get(
            class_id,
            default_confidence,
        )

        if detection["confidence"] < confidence_threshold:
            return False

        # Bag minimum bounding-box area (Bag class only)
        if (
            min_bbox_area > 0
            and class_id == 0
            and detection["area"] < min_bbox_area
        ):
            return False

        # Detection ROI
        if (
            detection_roi
            and not self._inside_detection_roi(
                detection["center"],
                detection_roi,
            )
        ):
            return False

        return True

    # ======================================================
    # DETECTION ROI CHECK
    # ======================================================

    def _inside_detection_roi(self, center, detection_roi=None):
        """
        Check whether a detection center is inside the
        given (or the default) detection ROI.
        """

        roi = (
            detection_roi
            if detection_roi is not None
            else self.detection_roi
        )

        x, y = center

        x1 = roi.get("x1")
        y1 = roi.get("y1")
        x2 = roi.get("x2")
        y2 = roi.get("y2")

        if x1 is not None and x < x1:
            return False

        if x2 is not None and x > x2:
            return False

        if y1 is not None and y < y1:
            return False

        if y2 is not None and y > y2:
            return False

        return True

    # ======================================================
    # VALIDATE DETECTION ROI
    # ======================================================

    @staticmethod
    def _validate_detection_roi(detection_roi):
        """
        Validate configured detection ROI coordinates.
        """

        if not detection_roi:
            return detection_roi

        x1 = detection_roi.get("x1")
        y1 = detection_roi.get("y1")
        x2 = detection_roi.get("x2")
        y2 = detection_roi.get("y2")

        if x1 is not None and x2 is not None and x1 > x2:
            raise ValueError(
                "Invalid detection_roi: "
                f"x1 ({x1}) must be "
                "less than or equal to "
                f"x2 ({x2})."
            )

        if y1 is not None and y2 is not None and y1 > y2:
            raise ValueError(
                "Invalid detection_roi: "
                f"y1 ({y1}) must be "
                "less than or equal to "
                f"y2 ({y2})."
            )

        return detection_roi

    # ======================================================
    # VALIDATE MODEL FILE
    # ======================================================

    def _validate_model_file(self):
        """
        Validate YOLO model checkpoint.
        """

        if not self.model_path.exists():
            raise FileNotFoundError(
                f"YOLO model file not found: {self.model_path}"
            )

        if self.model_path.stat().st_size == 0:
            raise ValueError(
                "YOLO model file "
                "is empty: "
                f"{self.model_path}. "
                "Replace it with a "
                "valid trained checkpoint."
            )

    # ======================================================
    # LOAD MODEL
    # ======================================================

    def _load_model(self):
        """
        Load the YOLO model.

        If CUDA loading fails, automatically fall back
        to CPU inference.
        """

        try:
            self.model = YOLO(str(self.model_path))

            self.model.to(self.device)

        except Exception as exc:
            if self.device.startswith("cuda"):
                self._log(
                    "warning",
                    "Failed to load model "
                    "on CUDA: "
                    f"{exc}. "
                    "Falling back to CPU.",
                )

                self.device = "cpu"

                self.half = False

                self.model = YOLO(str(self.model_path))

                self.model.to(self.device)

            else:
                raise

    # ======================================================
    # FUSE MODEL
    # ======================================================

    def _fuse_model(self):
        """
        Fuse compatible model layers when supported.
        """

        try:
            self.model.fuse()

        except Exception as exc:
            self._log(
                "warning",
                f"Could not fuse YOLO model layers: {exc}",
            )

    # ======================================================
    # WARM UP MODEL
    # ======================================================

    def _warm_up_model(self):
        """
        Run one dummy inference before processing camera frames.

        This initializes CUDA kernels and also verifies the FP16
        argument: if "quantize" is rejected by the installed
        Ultralytics version, it falls back to half=True, then to
        full precision, so detect() never fails on the first frame.
        """

        dummy = np.zeros(
            (self.image_size, self.image_size, 3),
            dtype=np.uint8,
        )

        if self.half and self.device.startswith("cuda"):
            candidates = [
                {"quantize": "fp16"},
                {"half": True},
                {},
            ]
        else:
            candidates = [{}]

        last_exc = None

        for kwargs in candidates:
            try:
                with torch.inference_mode():
                    self.model.predict(
                        source=dummy,
                        imgsz=self.image_size,
                        device=self.device,
                        classes=self.predict_classes,
                        rect=True,
                        agnostic_nms=False,
                        verbose=False,
                        **kwargs,
                    )

                self._precision_kwargs = kwargs

                if self.half and not kwargs:
                    self._log(
                        "warning",
                        "FP16 not supported by this setup. "
                        "Using full precision.",
                    )
                    self.half = False

                elif kwargs and kwargs != {"quantize": "fp16"}:
                    self._log(
                        "warning",
                        f"'quantize' rejected; using {kwargs}.",
                    )

                return

            except Exception as exc:
                last_exc = exc

        self._log(
            "warning",
            f"Could not warm up YOLO model: {last_exc}",
        )

    # ======================================================
    # LOGGER
    # ======================================================

    def _log(self, level, message):
        """
        Write detector log message.
        """

        if self.logger is not None:
            log_method = getattr(self.logger, level, None)

            if log_method:
                log_method(message)

            return

        print(f"[{level.upper()}] {message}")