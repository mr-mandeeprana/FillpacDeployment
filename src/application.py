"""
==========================================================
FillPac AI
Production Application Orchestrator
==========================================================

Architecture
------------

Application
    |
    +-- DashboardState
    |
    +-- CountLogger
    |
    +-- Elasticsearch
    |
    +-- ONE Detector / YOLO Model
    |
    +-- ONE InferenceManager
    |       |
    |       +-- Camera 1 Pipeline
    |       +-- Camera 2 Pipeline
    |       +-- Camera 3 Pipeline
    |       +-- Camera 4 Pipeline
    |
    +-- Each Pipeline owns:
            - Camera
            - Tracker
            - Counter
            - PrintDetector
            - Print history
            - Latest annotated frame


Important
---------
YOLO is loaded exactly ONCE.

All enabled cameras share ONE InferenceManager.

Each camera still owns its own:
- Camera connection
- ByteTrack tracker
- Physical-center Counter
- PrintDetector
- Print voting/history

The dashboard does NOT perform inference.
==========================================================
"""

import threading
import time
from pathlib import Path

import cv2

from src.config import Config
from src.count_logger import CountLogger
from src.dashboard import DashboardState
from src.detector import Detector
from src.elasticsearch import ElasticSearch
from src.inference_manager import InferenceManager
from src.logger import Logger
from src.pipeline import Pipeline


class Application:

    # ======================================================
    # INITIALIZATION
    # ======================================================

    def __init__(self):

        # ==================================================
        # BASIC APPLICATION STATE
        # ==================================================

        self.stopped = False

        self.stop_event = threading.Event()

        self.pipeline_threads = []

        self.pipelines = []

        self.detector = None

        self.inference_manager = None

        self.elasticsearch = None

        self.dashboard_state = None

        self.count_logger = None


        # ==================================================
        # CONFIGURATION
        # ==================================================

        self.config = Config()

        self.config.validate()


        # ==================================================
        # LOGGER
        # ==================================================

        self.logger = Logger(

            log_file=self.config.get(
                "logging",
                "file",
                default="logs/application.log",
            ),

            level=self.config.get(
                "logging",
                "level",
                default="INFO",
            ),
        )


        self.logger.info(
            "================================================"
        )

        self.logger.info(
            "Starting FillPac AI"
        )

        self.logger.info(
            "================================================"
        )


        # ==================================================
        # DASHBOARD CONFIGURATION
        # ==================================================

        dashboard_config = (
            self.config.get(
                "dashboard",
                default={},
            )
            or {}
        )


        dashboard_enabled = bool(
            dashboard_config.get(
                "enabled",
                True,
            )
        )


        # ==================================================
        # DASHBOARD STATE
        #
        # DashboardState persists to SQL Server
        # (dbo.system_state / dbo.camera_status) -- there is
        # no state.json file to configure anymore.
        # ==================================================

        self.dashboard_state = DashboardState(

            publish_interval=dashboard_config.get(
                "publish_interval",
                dashboard_config.get(
                    "persist_interval_seconds",
                    0.25,
                ),
            ),

            stale_timeout=dashboard_config.get(
                "stale_timeout",
                10.0,
            ),

            auto_publish=dashboard_enabled,
            default_count_type=Config.normalize_count_type(
                dashboard_config.get(
                    "default_count_type",
                    "roi",
                ),
                "dashboard.default_count_type",
            ),
        )


        self.dashboard_state.update_health(
            {
                "dashboard_enabled":
                    dashboard_enabled,

                "model_loaded":
                    False,

                "inference_manager_running":
                    False,

                "elasticsearch_connected":
                    False,
            }
        )


        self.dashboard_state.set_system_status(
            "starting"
        )


        # ==================================================
        # COUNT EVENT LOGGER
        #
        # CountLogger writes confirmed bag counts straight to
        # SQL Server (dbo.production_events) -- there is no
        # count_events.jsonl file to configure anymore.
        # ==================================================

        logging_config = (
            self.config.get(
                "logging",
                default={},
            )
            or {}
        )


        self.count_logger = CountLogger(

            enabled=logging_config.get(
                "count_events_enabled",
                True,
            ),
        )


        # ==================================================
        # ELASTICSEARCH
        # ==================================================

        elasticsearch_config = (
            self.config.get(
                "elasticsearch",
                default={},
            )
            or {}
        )


        try:

            self.elasticsearch = ElasticSearch(

                host=elasticsearch_config.get(
                    "host",
                    "localhost",
                ),

                port=elasticsearch_config.get(
                    "port",
                    9200,
                ),

                indices=elasticsearch_config.get(
                    "indices"
                ),

                enabled=elasticsearch_config.get(
                    "enabled",
                    False,
                ),

                logger=self.logger,
            )


            elasticsearch_connected = (
                self._is_elasticsearch_connected()
            )


            self.dashboard_state.update_health(
                {
                    "elasticsearch_connected":
                        elasticsearch_connected
                }
            )


        except Exception as error:

            # Elasticsearch must not prevent the vision
            # system from starting.

            self.elasticsearch = None


            self.dashboard_state.update_health(
                {
                    "elasticsearch_connected":
                        False
                }
            )


            self.logger.warning(
                "Elasticsearch initialization failed: "
                f"{error}"
            )


        # ==================================================
        # ENABLED CAMERAS
        # ==================================================

        enabled_cameras = [

            camera

            for camera in self.config.get(
                "cameras",
                default=[],
            )

            if camera.get(
                "enabled",
                True,
            )
        ]


        if not enabled_cameras:

            self.logger.warning(
                "No enabled cameras found "
                "in configuration."
            )


            self.dashboard_state.set_system_status(
                "idle"
            )


            self.dashboard_state.flush()

            return


        # ==================================================
        # AI CAMERAS
        #
        # Cameras with ai_enabled: false only stream video, so the
        # model and inference manager are loaded ONLY when at least
        # one enabled camera needs them.
        # ==================================================
        ai_cameras = [
            camera
            for camera in enabled_cameras
            if camera.get(
                "ai_enabled",
                True,
            )
        ]

        if not ai_cameras:
            self.logger.info(
                "No camera has ai_enabled=true: running in "
                "stream-only mode, model not loaded."
            )
            self.dashboard_state.update_health(
                {
                    "model_loaded": False,
                    "inference_manager_running": False,
                }
            )

        if ai_cameras:

            # ==================================================
            # SHARED MODEL CONFIGURATION
            #
            # Current project architecture uses the same model
            # for all cameras. Therefore model configuration is
            # taken from the first enabled camera.
            # ==================================================

            model_config = (
                ai_cameras[
                    0
                ].get(
                    "model",
                    {},
                )
                or {}
            )


            # ==================================================
            # DETECTOR
            #
            # ONE Detector = ONE YOLO model.
            # ==================================================

            try:

                model_device = model_config.get(
                    "device",
                    "cpu",
                )


                model_half = bool(
                    model_config.get(
                        "half",
                        False,
                    )
                )


                # ----------------------------------------------
                # FP16 should not be enabled for CPU inference.
                # ----------------------------------------------

                if str(
                    model_device
                ).lower() == "cpu":

                    model_half = False


                self.logger.info(
                    "Initializing shared YOLO Detector."
                )


                self.logger.info(
                    f"Model device: {model_device}"
                )


                self.logger.info(
                    f"FP16 enabled: {model_half}"
                )


                # ----------------------------------------------
                # Resolve the model path relative to the folder
                # that contains config.yaml (deployment folder),
                # not the current working directory.
                # ----------------------------------------------

                model_path = Path(
                    model_config["path"]
                )


                if not model_path.is_absolute():

                    model_path = (
                        self.config.config_path.parent
                        / model_path
                    )


                model_path = model_path.resolve()


                if not model_path.is_file():

                    raise FileNotFoundError(
                        "YOLO model file not found: "
                        f"{model_path}"
                    )


                self.logger.info(
                    f"Model path: {model_path}"
                )


                self.detector = Detector(

                    model_path=str(
                        model_path
                    ),

                    confidence=model_config.get(
                        "confidence",
                        0.5,
                    ),

                    iou=model_config.get(
                        "iou",
                        0.45,
                    ),

                    device=model_device,

                    image_size=model_config.get(
                        "image_size",
                        640,
                    ),

                    half=model_half,

                    max_detections=model_config.get(
                        "max_detections",
                        100,
                    ),

                    allowed_classes=model_config.get(
                        "allowed_classes"
                    ),

                    min_bbox_area=model_config.get(
                        "min_bbox_area",
                        0,
                    ),

                    bag_confidence=model_config.get(
                        "bag_confidence"
                    ),

                    print_confidence=model_config.get(
                        "print_confidence"
                    ),

                    class_confidence_thresholds=
                        model_config.get(
                            "class_confidence_thresholds"
                        ),

                    detection_roi=model_config.get(
                        "detection_roi"
                    ),

                    logger=self.logger,
                )


                # ----------------------------------------------
                # PER-CAMERA MODEL FILTERS
                #
                # The YOLO model is shared, but each camera keeps
                # its OWN detection_roi, confidence, bag/print
                # confidence, min_bbox_area and allowed_classes.
                # Without this, every camera was filtered with the
                # FIRST enabled camera's values.
                # ----------------------------------------------

                for camera_entry in ai_cameras:

                    self.detector.register_camera_filters(
                        camera_entry["name"],
                        camera_entry.get("model") or {},
                    )

                self.logger.info(
                    "Per-camera detection filters registered for: "
                    + ", ".join(
                        str(camera_entry["name"])
                        for camera_entry in ai_cameras
                    )
                )

                self.logger.info(
                    "Shared YOLO Detector initialized."
                )


                self.dashboard_state.update_health(
                    {
                        "model_loaded":
                            True
                    }
                )


            except Exception as error:

                self.dashboard_state.update_health(
                    {
                        "model_loaded":
                            False,

                        "inference_manager_running":
                            False,
                    }
                )


                self.dashboard_state.set_system_status(
                    "error"
                )


                self.dashboard_state.flush()


                self.logger.error(
                    "Shared YOLO initialization failed: "
                    f"{error}"
                )


                raise


            # ==================================================
            # SHARED INFERENCE MANAGER
            # ==================================================

            try:

                self.inference_manager = (
                    InferenceManager(

                        detector=self.detector,

                        logger=self.logger,
                    )
                )


                self.inference_manager.start()


                self.logger.info(
                    "Shared InferenceManager started."
                )


                self.dashboard_state.update_health(
                    {
                        "inference_manager_running":
                            True
                    }
                )


            except Exception as error:

                self.dashboard_state.update_health(
                    {
                        "inference_manager_running":
                            False
                    }
                )


                self.dashboard_state.set_system_status(
                    "error"
                )


                self.dashboard_state.flush()


                self.logger.error(
                    "InferenceManager initialization failed: "
                    f"{error}"
                )


                self.detector = None


                raise


        # ==================================================
        # PIPELINE CONFIGURATION
        # ==================================================

        tracker_config = (
            self.config.get(
                "tracker",
                default={},
            )
            or {}
        )


        display_config = (
            self.config.get(
                "display",
                default={},
            )
            or {}
        )


        # ==================================================
        # INITIALIZE CAMERA PIPELINES
        # ==================================================

        try:

            for camera_config in enabled_cameras:

                pipeline = Pipeline(

                    camera_config=
                        camera_config,

                    tracker_config=
                        tracker_config,

                    display_config=
                        display_config,

                    logger=
                        self.logger,

                    # --------------------------------------
                    # SAME shared inference manager
                    # --------------------------------------

                    inference_manager=
                        self.inference_manager,

                    dashboard_state=
                        self.dashboard_state,

                    elasticsearch=
                        self.elasticsearch,

                    count_logger=
                        self.count_logger,
                )


                self.pipelines.append(
                    pipeline
                )


                self.logger.info(
                    f'{camera_config["name"]} '
                    "pipeline initialized."
                )


        except Exception as error:

            self.dashboard_state.set_system_status(
                "error"
            )


            self.dashboard_state.flush()


            self.logger.error(
                "Pipeline initialization failed: "
                f"{error}"
            )


            # ----------------------------------------------
            # Clean up anything initialized before failure.
            # ----------------------------------------------

            for pipeline in self.pipelines:

                try:

                    pipeline.release()

                except Exception:

                    pass


            if self.inference_manager is not None:

                try:

                    self.inference_manager.stop(
                        timeout=5
                    )

                except TypeError:

                    self.inference_manager.stop()

                except Exception:

                    pass


            self.dashboard_state.update_health(
                {
                    "inference_manager_running":
                        False
                }
            )


            self.detector = None


            self.dashboard_state.update_health(
                {
                    "model_loaded":
                        False
                }
            )


            raise


        # ==================================================
        # INITIALIZATION COMPLETE
        # ==================================================

        self.dashboard_state.set_system_status(
            "ready"
        )


        self.dashboard_state.flush()


        self.logger.info(
            f"{len(self.pipelines)} camera pipeline(s) "
            "initialized."
        )


    # ======================================================
    # RUN APPLICATION
    # ======================================================

    def run(
        self,
    ):

        self.logger.info(
            "Application run requested."
        )


        # ==================================================
        # CHECK PIPELINES
        # ==================================================

        if not self.pipelines:

            self.logger.warning(
                "No enabled camera pipelines available."
            )


            self.dashboard_state.set_system_status(
                "idle"
            )


            self.dashboard_state.flush()

            return


        # ==================================================
        # VERIFY INFERENCE MANAGER
        # ==================================================

        if (
            self.inference_manager is None
            and any(
                pipeline.ai_enabled
                for pipeline in self.pipelines
            )
        ):
            self.logger.error(
                "InferenceManager is not available."
            )


            self.dashboard_state.set_system_status(
                "error"
            )


            self.dashboard_state.flush()

            return


        # ==================================================
        # SYSTEM RUNNING
        # ==================================================

        self.dashboard_state.set_system_status(
            "running"
        )


        self.dashboard_state.update_health(
            {
                "model_loaded":
                    self.detector is not None,

                "inference_manager_running":
                    self.inference_manager is not None,
                "elasticsearch_connected":
                    self._is_elasticsearch_connected(),
            }
        )


        self.dashboard_state.flush()


        # ==================================================
        # START PIPELINE THREADS
        # ==================================================

        for pipeline in self.pipelines:

            thread = threading.Thread(

                target=pipeline.run,

                args=(
                    self.stop_event,
                ),

                daemon=True,

                name=(
                    f"Pipeline-{pipeline.name}"
                ),
            )


            thread.start()


            self.pipeline_threads.append(
                thread
            )


            self.logger.info(
                f"{pipeline.name} pipeline thread started."
            )


        # ==================================================
        # MAIN APPLICATION LOOP
        # ==================================================

        try:

            while not self.stop_event.is_set():

                # ==========================================
                # DISPLAY LATEST ANNOTATED FRAMES
                # ==========================================

                for pipeline in self.pipelines:

                    try:

                        pipeline.publish()

                    except Exception as error:

                        self.logger.warning(
                            f"{pipeline.name} "
                            "display publish failed: "
                            f"{error}"
                        )


                # ==========================================
                # ESC KEY
                # ==========================================

                try:

                    key = cv2.waitKey(
                        1
                    )


                    if (
                        key
                        &
                        0xFF
                    ) == 27:

                        self.logger.info(
                            "ESC key pressed."
                        )


                        self.stop_event.set()

                        break


                except cv2.error:

                    # Headless OpenCV environments may not
                    # support waitKey().
                    pass


                time.sleep(
                    0.01
                )


        # ==================================================
        # KEYBOARD INTERRUPT
        # ==================================================

        except KeyboardInterrupt:

            self.logger.info(
                "Keyboard interrupt received."
            )


            self.stop_event.set()


        # ==================================================
        # UNEXPECTED APPLICATION ERROR
        # ==================================================

        except Exception as error:

            self.logger.error(
                "Unhandled exception in application run: "
                f"{error}"
            )


            self.dashboard_state.set_system_status(
                "error"
            )


            self.dashboard_state.flush()


            self.stop_event.set()


        # ==================================================
        # SHUTDOWN
        # ==================================================

        finally:

            self.stop()


    # ======================================================
    # STOP APPLICATION
    # ======================================================

    def stop(
        self,
    ):

        if self.stopped:
            return


        self.stopped = True


        self.logger.info(
            "================================================"
        )

        self.logger.info(
            "Stopping FillPac AI"
        )

        self.logger.info(
            "================================================"
        )


        # ==================================================
        # DASHBOARD STATUS
        # ==================================================

        if self.dashboard_state is not None:

            try:

                self.dashboard_state.set_system_status(
                    "stopping"
                )

                self.dashboard_state.flush()

            except Exception:

                self.logger.warning(
                    "Unable to publish stopping state."
                )


        # ==================================================
        # SIGNAL PIPELINES
        # ==================================================

        self.stop_event.set()


        # ==================================================
        # WAIT FOR PIPELINE THREADS
        #
        # Do this before shutting down InferenceManager.
        # ==================================================

        for thread in self.pipeline_threads:

            try:

                thread.join(
                    timeout=5
                )


                if thread.is_alive():

                    self.logger.warning(
                        f"{thread.name} did not stop "
                        "within timeout."
                    )


            except Exception as error:

                self.logger.warning(
                    f"Failed joining {thread.name}: "
                    f"{error}"
                )


        # ==================================================
        # RELEASE PIPELINES / CAMERAS
        # ==================================================

        for pipeline in self.pipelines:

            try:

                pipeline.release()

            except Exception as error:

                self.logger.warning(
                    f"{pipeline.name} release failed: "
                    f"{error}"
                )


        # ==================================================
        # STOP SHARED INFERENCE MANAGER
        # ==================================================

        if self.inference_manager is not None:

            try:

                self.inference_manager.stop(
                    timeout=5
                )


            except TypeError:

                # Compatibility in case current manager
                # stop() has no timeout parameter.

                try:

                    self.inference_manager.stop()

                except Exception as error:

                    self.logger.warning(
                        "InferenceManager shutdown "
                        f"failed: {error}"
                    )


            except Exception as error:

                self.logger.warning(
                    "InferenceManager shutdown failed: "
                    f"{error}"
                )


        self.inference_manager = None


        # ==================================================
        # UPDATE INFERENCE HEALTH
        # ==================================================

        if self.dashboard_state is not None:

            try:

                self.dashboard_state.update_health(
                    {
                        "inference_manager_running":
                            False
                    }
                )

            except Exception:

                pass


        # ==================================================
        # RELEASE DETECTOR REFERENCE
        #
        # Python / PyTorch will release the model resources
        # when no longer referenced.
        # ==================================================

        self.detector = None


        if self.dashboard_state is not None:

            try:

                self.dashboard_state.update_health(
                    {
                        "model_loaded":
                            False
                    }
                )

            except Exception:

                pass


        # ==================================================
        # CLOSE ELASTICSEARCH
        # ==================================================

        if self.elasticsearch is not None:

            try:

                self.elasticsearch.close()

            except Exception as error:

                self.logger.warning(
                    "Elasticsearch shutdown failed: "
                    f"{error}"
                )


        self.elasticsearch = None


        if self.dashboard_state is not None:

            try:

                self.dashboard_state.update_health(
                    {
                        "elasticsearch_connected":
                            False
                    }
                )

            except Exception:

                pass


        # ==================================================
        # FINAL DASHBOARD STATE
        #
        # DashboardState.stop(mark_offline=True) performs
        # the final state publish to SQL Server and stops the
        # background state publisher.
        # ==================================================

        if self.dashboard_state is not None:

            try:

                self.dashboard_state.stop(
                    mark_offline=True
                )

            except Exception as error:

                self.logger.warning(
                    "DashboardState shutdown failed: "
                    f"{error}"
                )


        # ==================================================
        # CLOSE OPENCV WINDOWS
        # ==================================================

        try:

            cv2.destroyAllWindows()

        except cv2.error:

            pass


        self.logger.info(
            "FillPac AI application closed."
        )


    # ======================================================
    # ELASTICSEARCH CONNECTION STATUS
    # ======================================================

    def _is_elasticsearch_connected(
        self,
    ):

        if self.elasticsearch is None:
            return False


        try:

            checker = getattr(
                self.elasticsearch,
                "is_connected",
                None,
            )


            if callable(
                checker
            ):

                return bool(
                    checker()
                )


            return False


        except Exception:

            return False


    # ======================================================
    # APPLICATION STATUS
    # ======================================================

    def get_status(
        self,
    ):

        pipeline_status = []


        for pipeline in self.pipelines:

            try:

                pipeline_status.append(
                    pipeline.get_status()
                )

            except Exception:

                pipeline_status.append(
                    {
                        "name":
                            getattr(
                                pipeline,
                                "name",
                                "unknown",
                            ),

                        "status":
                            "unknown",
                    }
                )


        return {

            "stopped":
                self.stopped,

            "model_loaded":
                self.detector is not None,

            "inference_manager_running":
                self.inference_manager
                is not None,

            "elasticsearch_connected":
                self._is_elasticsearch_connected(),

            "pipeline_count":
                len(
                    self.pipelines
                ),

            "pipelines":
                pipeline_status,

            "dashboard":
                (
                    self.dashboard_state.get_status()
                    if self.dashboard_state
                    is not None
                    else None
                ),

            "count_logger":
                (
                    self.count_logger.get_status()
                    if self.count_logger
                    is not None
                    else None
                ),
        }