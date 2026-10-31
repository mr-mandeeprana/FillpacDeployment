"""
==========================================================
FillPac AI
Configuration Loader
==========================================================

Purpose
-------
Loads config.yaml and normalizes configuration for:

- Model
- Tracker
- Physical-center counting
- Print detection
- Movement-based bag jam detection - Condition A
- Distance-based bag spacing jam detection - Condition B
- Display
- Cameras

Jam Detection - Condition A
---------------------------
Each camera receives its own jam_detection configuration.

Global jam_detection values act as defaults.
Camera-specific values override the global defaults.

Bag Spacing - Condition B
-------------------------
Each camera receives its own bag_spacing configuration.

Condition B detects a jam immediately when the calibrated
physical edge-to-edge distance between adjacent bags is
less than or equal to the configured threshold.

Example:

bag_spacing:
    enabled: true
    direction: up
    jam_threshold_mm: 305.0

    roi:
        x1: 0
        y1: 150
        x2: 477
        y2: 500

    calibration:

        image_points:
            - [150, 150]
            - [400, 150]
            - [470, 500]
            - [60, 500]

        world_points_mm:
            - [0, 0]
            - [800, 0]
            - [800, 2000]
            - [0, 2000]

Camera-specific bag_spacing values can override the
global configuration.
==========================================================
"""

import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


class Config:

    # ======================================================
    # INITIALIZATION
    # ======================================================

    def __init__(
        self,
        config_path: str | None = None,
    ):

        config_path = (
            config_path
            or os.getenv(
                "FILLPAC_CONFIG_PATH"
            )
            or "config.yaml"
        )

        # --------------------------------------------------
        # Resolve relative paths against the deployment
        # folder instead of the current working directory.
        #
        # Frozen (PyInstaller EXE): folder containing the EXE
        # Source run:               project root (parent of src/)
        # --------------------------------------------------

        if getattr(
            sys,
            "frozen",
            False,
        ):

            base_dir = Path(
                sys.executable
            ).resolve().parent

        else:

            base_dir = Path(
                __file__
            ).resolve().parents[1]

        self.config_path = Path(
            config_path
        )

        if not self.config_path.is_absolute():

            self.config_path = (
                base_dir
                / self.config_path
            )

        self.config_path = (
            self.config_path.resolve()
        )

        if not self.config_path.is_file():

            raise FileNotFoundError(
                "Configuration file not found: "
                f"{self.config_path}"
            )

        self.data = {}

        self._load()

    # ======================================================
    # LOAD
    # ======================================================

    def _load(
        self,
    ) -> None:

        with open(
            self.config_path,
            "r",
            encoding="utf-8",
        ) as fh:

            raw = (
                yaml.safe_load(
                    fh
                )
                or {}
            )

        if not isinstance(
            raw,
            dict,
        ):

            raise ValueError(
                "Root YAML configuration must be "
                "a dictionary/object."
            )

        self.data = (
            self._normalize(
                raw
            )
        )

        self.validate()

    # ======================================================
    # NORMALIZATION
    # ======================================================

    def _normalize(
        self,
        data: dict,
    ) -> dict:

        # --------------------------------------------------
        # GLOBAL DEFAULTS
        # --------------------------------------------------

        global_model = deepcopy(
            data.get(
                "model",
                {},
            )
            or {}
        )

        global_counting = deepcopy(
            data.get(
                "counting",
                {},
            )
            or {}
        )

        global_print = deepcopy(
            data.get(
                "print_detection",
                {},
            )
            or {}
        )

        global_jam = deepcopy(
            data.get(
                "jam_detection",
                {},
            )
            or {}
        )

        global_bag_spacing = deepcopy(
            data.get(
                "bag_spacing",
                {},
            )
            or {}
        )

        global_condition_c = deepcopy(
            data.get(
                "condition_c",
                {},
            )
            or {}
        )

        global_display = deepcopy(
            data.get(
                "display",
                {},
            )
            or {}
        )

        # Optional top-level "production:" block = defaults for
        # every camera's production switches.
        global_production = data.get(
            "production",
            {},
        )

        if not isinstance(
            global_production,
            dict,
        ):
            global_production = {}

        result = deepcopy(
            data
        )

        # --------------------------------------------------
        # CAMERAS
        # --------------------------------------------------

        raw_cameras = (
            data.get(
                "cameras",
                [],
            )
            or []
        )

        if not isinstance(
            raw_cameras,
            list,
        ):

            raise ValueError(
                "'cameras' must be a list"
            )

        cameras = []

        for cam in raw_cameras:

            if not isinstance(
                cam,
                dict,
            ):

                raise ValueError(
                    "Each camera configuration "
                    "must be a dictionary."
                )

            camera = deepcopy(
                cam
            )

            # ==============================================
            # MODEL
            # ==============================================

            camera[
                "model"
            ] = self._merge_dicts(
                global_model,
                camera.get(
                    "model",
                    {},
                ),
            )

            # ==============================================
            # COUNTING
            # ==============================================

            camera[
                "counting"
            ] = self._merge_dicts(
                global_counting,
                camera.get(
                    "counting",
                    {},
                ),
            )

            # ==============================================
            # PRINT DETECTION
            # ==============================================

            pd = camera.get(
                "print_detection",
                global_print,
            )

            # Support:
            #
            # print_detection: true
            #
            # or:
            #
            # print_detection:
            #     enabled: true

            if isinstance(
                pd,
                bool,
            ):

                pd = {
                    "enabled":
                        pd,

                    "confidence":
                        global_print.get(
                            "confidence",
                            0.25,
                        ),
                }

            elif not isinstance(
                pd,
                dict,
            ):

                pd = {}

            camera[
                "print_detection"
            ] = self._merge_dicts(
                global_print,
                pd,
            )

            # ==============================================
            # JAM DETECTION - CONDITION A
            # ==============================================

            jd = camera.get(
                "jam_detection",
                global_jam,
            )

            # Support:
            #
            # jam_detection: true
            #
            # or:
            #
            # jam_detection:
            #     enabled: true

            if isinstance(
                jd,
                bool,
            ):

                jd = {
                    "enabled":
                        jd
                }

            elif not isinstance(
                jd,
                dict,
            ):

                jd = {}

            camera_jam = (
                self._merge_dicts(
                    global_jam,
                    jd,
                )
            )

            # ----------------------------------------------
            # JAM ROI
            #
            # _merge_dicts is shallow.
            # Therefore ROI is merged separately.
            # ----------------------------------------------

            global_jam_roi = (
                global_jam.get(
                    "roi",
                    {},
                )
                or {}
            )

            camera_jam_roi = (
                jd.get(
                    "roi",
                    {},
                )
                or {}
            )

            merged_jam_roi = (
                self._merge_dicts(
                    global_jam_roi,
                    camera_jam_roi,
                )
            )

            camera_jam[
                "roi"
            ] = self._normalize_roi(
                merged_jam_roi
            )

            camera[
                "jam_detection"
            ] = camera_jam

            # ==============================================
            # BAG SPACING JAM - CONDITION B
            # ==============================================

            bs = camera.get(
                "bag_spacing",
                global_bag_spacing,
            )

            # Support:
            #
            # bag_spacing: true
            #
            # or:
            #
            # bag_spacing:
            #     enabled: true

            if isinstance(
                bs,
                bool,
            ):

                bs = {
                    "enabled":
                        bs
                }

            elif not isinstance(
                bs,
                dict,
            ):

                bs = {}

            camera_bag_spacing = (
                self._merge_dicts(
                    global_bag_spacing,
                    bs,
                )
            )

            # ----------------------------------------------
            # BAG SPACING ROI
            #
            # ROI is merged separately because the standard
            # dictionary merge is shallow.
            # ----------------------------------------------

            global_spacing_roi = (
                global_bag_spacing.get(
                    "roi",
                    {},
                )
                or {}
            )

            camera_spacing_roi = (
                bs.get(
                    "roi",
                    {},
                )
                or {}
            )

            merged_spacing_roi = (
                self._merge_dicts(
                    global_spacing_roi,
                    camera_spacing_roi,
                )
            )

            camera_bag_spacing[
                "roi"
            ] = self._normalize_roi(
                merged_spacing_roi
            )

            # ----------------------------------------------
            # CAMERA CALIBRATION
            #
            # Global calibration acts as the development
            # default.
            #
            # In production each camera can override its
            # own image_points/world_points_mm.
            # ----------------------------------------------

            global_calibration = (
                global_bag_spacing.get(
                    "calibration",
                    {},
                )
                or {}
            )

            camera_calibration = (
                bs.get(
                    "calibration",
                    {},
                )
                or {}
            )

            camera_bag_spacing[
                "calibration"
            ] = self._merge_dicts(
                global_calibration,
                camera_calibration,
            )

            camera[
                "bag_spacing"
            ] = camera_bag_spacing

            # ==============================================
            # ROI OCCUPANCY JAM - CONDITION C
            # ==============================================

            cc = camera.get(
                "condition_c",
                global_condition_c,
            )

            if isinstance(
                cc,
                bool,
            ):

                cc = {
                    "enabled":
                        cc
                }

            elif not isinstance(
                cc,
                dict,
            ):

                cc = {}

            camera_condition_c = self._merge_dicts(
                global_condition_c,
                cc,
            )

            global_condition_c_roi = (
                global_condition_c.get(
                    "roi",
                    {},
                )
                or {}
            )

            camera_condition_c_roi = (
                cc.get(
                    "roi",
                    {},
                )
                or {}
            )

            merged_condition_c_roi = self._merge_dicts(
                global_condition_c_roi,
                camera_condition_c_roi,
            )

            camera_condition_c[
                "roi"
            ] = self._normalize_roi(
                merged_condition_c_roi
            )

            camera[
                "condition_c"
            ] = camera_condition_c

            # ==============================================
            # DISPLAY
            # ==============================================

            camera[
                "display"
            ] = self._merge_dicts(
                global_display,
                camera.get(
                    "display",
                    {},
                ),
            )

            # ==============================================
            # AI ON/OFF + PRODUCTION SWITCHES
            #
            # ai_enabled: false -> stream video only (no model)
            # production:
            #   count_enabled     -> run counting + save events
            #   include_bags_in_total  -> add BAG count to total bags
            #   include_prints_in_total  -> show PRINT counts on the dashboard
            # ==============================================

            camera_name_for_errors = camera.get(
                "name",
                "camera",
            )

            camera[
                "ai_enabled"
            ] = self._to_bool(
                camera.get(
                    "ai_enabled",
                    True,
                ),
                f"{camera_name_for_errors}: ai_enabled",
            )

            camera_production = camera.get(
                "production",
                {},
            )

            if camera_production is None:
                camera_production = {}

            if not isinstance(
                camera_production,
                dict,
            ):
                raise ValueError(
                    f"{camera_name_for_errors}: "
                    "'production' must be a mapping."
                )

            merged_production = dict(
                self.PRODUCTION_DEFAULTS
            )
            merged_production.update(global_production)
            merged_production.update(camera_production)

            # Legacy key names from earlier versions:
            #   include_in_total  -> include_bags_in_total
            #   show_print_count / show_on_dashboard
            #                     -> include_prints_in_total
            # They only apply when the new key is not given
            # explicitly (in the camera or in the global block).
            for legacy_key, new_key in (
                ("include_in_total", "include_bags_in_total"),
                ("show_print_count", "include_prints_in_total"),
                ("show_on_dashboard", "include_prints_in_total"),
            ):

                if legacy_key not in merged_production:
                    continue

                legacy_value = self._to_bool(
                    merged_production.pop(legacy_key),
                    f"{camera_name_for_errors}: "
                    f"production.{legacy_key}",
                )

                # A legacy key set on the camera beats the global
                # default; a legacy key in the global block only
                # fills in when the new key is not given there.
                if new_key not in camera_production and (
                    legacy_key in camera_production
                    or new_key not in global_production
                ):
                    merged_production[new_key] = legacy_value

            # show_bag_count was removed (bags: include_bags_in_total).
            merged_production.pop("show_bag_count", None)

            for production_key in self.PRODUCTION_DEFAULTS:
                merged_production[
                    production_key
                ] = self._to_bool(
                    merged_production[production_key],
                    f"{camera_name_for_errors}: "
                    f"production.{production_key}",
                )

            camera[
                "production"
            ] = merged_production

            # ==============================================
            # COUNTING ROI
            # ==============================================

            camera[
                "roi"
            ] = self._normalize_roi(
                camera.get(
                    "roi",
                    {},
                )
            )

            # ==============================================
            # RUNTIME DEFAULTS
            # ==============================================

            camera.setdefault(
                "buffer_size",
                1,
            )

            camera.setdefault(
                "queue_size",
                3,
            )

            camera.setdefault(
                "read_timeout",
                0.2,
            )

            camera.setdefault(
                "inference_timeout",
                5.0,
            )

            camera.setdefault(
                "dashboard_publish_interval",
                0.5,
            )

            camera.setdefault(
                "enabled",
                True,
            )

            camera.setdefault(
                "mode",
                "video",
            )

            cameras.append(
                camera
            )

        result[
            "cameras"
        ] = cameras

        return result

    # ======================================================
    # PER-CAMERA SWITCHES (ai_enabled / production.*)
    # ======================================================

    COUNT_TYPES = ("roi", "line")

    # Every key is optional in config.yaml; defaults equal the
    # behaviour before these switches existed.
    PRODUCTION_DEFAULTS = {
        # Run the bag counters (line + entry ROI) and save count events.
        "count_enabled": True,
        # Add this camera's BAG count to the total bags number.
        "include_bags_in_total": True,
        # Add this camera's PRINT counts (printed / not printed) to the
        # dashboard data: shown for the camera and added to the total
        # printed / not printed. Independent of include_bags_in_total.
        "include_prints_in_total": True,
    }

    @staticmethod
    def _to_bool(value, field: str) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text in ("true", "yes", "on", "1"):
                return True
            if text in ("false", "no", "off", "0"):
                return False
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise ValueError(f"{field} must be true or false (got {value!r}).")

    @classmethod
    def normalize_count_type(cls, value, field: str = "count_type") -> str:
        """Return 'roi' or 'line'; raise ValueError otherwise."""
        text = str(value if value is not None else "").strip().lower()
        aliases = {
            "roi": "roi", "entry_roi": "roi", "entry": "roi",
            "line": "line", "line_counting": "line", "crossing": "line",
        }
        if text not in aliases:
            raise ValueError(
                f"{field} must be one of {', '.join(cls.COUNT_TYPES)} "
                f"(got {value!r})."
            )
        return aliases[text]

    # ======================================================
    # ROI NORMALIZATION
    # ======================================================

    @staticmethod
    def _normalize_roi(
        roi,
    ) -> dict:

        if not isinstance(
            roi,
            dict,
        ):

            roi = {}

        return {

            "x1":
                Config._safe_int(
                    roi.get(
                        "x1",
                        0,
                    )
                ),

            "y1":
                Config._safe_int(
                    roi.get(
                        "y1",
                        0,
                    )
                ),

            "x2":
                Config._safe_int(
                    roi.get(
                        "x2",
                        0,
                    )
                ),

            "y2":
                Config._safe_int(
                    roi.get(
                        "y2",
                        0,
                    )
                ),
        }

    # ======================================================
    # DICTIONARY MERGE
    # ======================================================

    @staticmethod
    def _merge_dicts(
        base: dict,
        override: dict,
    ) -> dict:

        if not isinstance(
            base,
            dict,
        ):

            base = {}

        if not isinstance(
            override,
            dict,
        ):

            override = {}

        out = deepcopy(
            base
        )

        out.update(
            deepcopy(
                override
            )
        )

        return out

    # ======================================================
    # SAFE INTEGER
    # ======================================================

    @staticmethod
    def _safe_int(
        value,
        default=0,
    ) -> int:

        try:

            return int(
                value
            )

        except (
            TypeError,
            ValueError,
            OverflowError,
        ):

            return int(
                default
            )

    # ======================================================
    # GET
    # ======================================================

    def get(
        self,
        *keys: str,
        default: Any = None,
    ) -> Any:

        node = self.data

        try:

            for key in keys:

                node = node[
                    key
                ]

            return node

        except (
            KeyError,
            TypeError,
            IndexError,
        ):

            return default

    # ======================================================
    # ITEM ACCESS
    # ======================================================

    def __getitem__(
        self,
        key: str,
    ) -> Any:

        return self.data[
            key
        ]

    # ======================================================
    # CONTAINS
    # ======================================================

    def __contains__(
        self,
        key: str,
    ) -> bool:

        return key in self.data

    # ======================================================
    # RELOAD
    # ======================================================

    def reload(
        self,
    ) -> None:

        self._load()

    # ======================================================
    # VALIDATION
    # ======================================================

    def validate(
        self,
    ) -> None:

        # --------------------------------------------------
        # DASHBOARD COUNT TYPE  (dashboard.default_count_type)
        # --------------------------------------------------
        dashboard_section = self.data.get(
            "dashboard",
            {},
        )

        if (
            isinstance(dashboard_section, dict)
            and "default_count_type" in dashboard_section
        ):
            self.normalize_count_type(
                dashboard_section["default_count_type"],
                "dashboard.default_count_type",
            )

        # --------------------------------------------------
        # CAMERAS
        # --------------------------------------------------

        cameras = self.data.get(
            "cameras",
            [],
        )

        if not isinstance(
            cameras,
            list,
        ):

            raise ValueError(
                "'cameras' must be a list"
            )

        camera_ids = set()

        camera_names = set()

        for index, camera in enumerate(
            cameras
        ):

            if not isinstance(
                camera,
                dict,
            ):

                raise ValueError(
                    f"Camera #{index + 1} must "
                    "be a dictionary."
                )

            # ==============================================
            # CAMERA ID
            # ==============================================

            camera_id = camera.get(
                "id"
            )

            if camera_id is not None:

                if camera_id in camera_ids:

                    raise ValueError(
                        "Duplicate camera ID: "
                        f"{camera_id}"
                    )

                camera_ids.add(
                    camera_id
                )

            # ==============================================
            # CAMERA NAME
            # ==============================================

            camera_name = camera.get(
                "name"
            )

            if not camera_name:

                raise ValueError(
                    f"Camera #{index + 1} "
                    "is missing 'name'."
                )

            if camera_name in camera_names:

                raise ValueError(
                    "Duplicate camera name: "
                    f"{camera_name}"
                )

            camera_names.add(
                camera_name
            )

            # ==============================================
            # COUNTING ROI
            # ==============================================

            roi = camera.get(
                "roi",
                {},
            )

            if not isinstance(
                roi,
                dict,
            ):

                raise ValueError(
                    f"{camera_name}: "
                    "counting ROI must be "
                    "a dictionary."
                )

            # ==============================================
            # JAM DETECTION - CONDITION A
            # ==============================================

            jam = camera.get(
                "jam_detection",
                {},
            )

            if not isinstance(
                jam,
                dict,
            ):

                raise ValueError(
                    f"{camera_name}: "
                    "jam_detection must be "
                    "a dictionary."
                )

            jam_enabled = bool(
                jam.get(
                    "enabled",
                    False,
                )
            )

            if jam_enabled:

                self._validate_jam_config(
                    camera_name,
                    jam,
                )

            # ==============================================
            # BAG SPACING JAM - CONDITION B
            # ==============================================

            bag_spacing = camera.get(
                "bag_spacing",
                {},
            )

            if not isinstance(
                bag_spacing,
                dict,
            ):

                raise ValueError(
                    f"{camera_name}: "
                    "bag_spacing must be "
                    "a dictionary."
                )

            spacing_enabled = bool(
                bag_spacing.get(
                    "enabled",
                    False,
                )
            )

            if spacing_enabled:

                self._validate_bag_spacing_config(
                    camera_name,
                    bag_spacing,
                )

            # ==============================================
            # ROI OCCUPANCY JAM - CONDITION C
            # ==============================================

            condition_c = camera.get(
                "condition_c",
                {},
            )

            if not isinstance(
                condition_c,
                dict,
            ):

                raise ValueError(
                    f"{camera_name}: "
                    "condition_c must be a dictionary."
                )

            condition_c_enabled = bool(
                condition_c.get(
                    "enabled",
                    False,
                )
            )

            if condition_c_enabled:

                self._validate_condition_c_config(
                    camera_name,
                    condition_c,
                )

    # ======================================================
    # JAM VALIDATION - CONDITION A
    # ======================================================

    @staticmethod
    def _validate_jam_config(
        camera_name,
        jam,
    ) -> None:

        # --------------------------------------------------
        # TIMING
        # --------------------------------------------------

        try:

            history_seconds = float(
                jam.get(
                    "history_seconds",
                    1.0,
                )
            )

            min_history_seconds = float(
                jam.get(
                    "min_history_seconds",
                    0.5,
                )
            )

            warning_time = float(
                jam.get(
                    "warning_time_seconds",
                    2.0,
                )
            )

            jam_time = float(
                jam.get(
                    "jam_time_seconds",
                    5.0,
                )
            )

            recovery_time = float(
                jam.get(
                    "recovery_time_seconds",
                    1.0,
                )
            )

        except (
            TypeError,
            ValueError,
        ) as error:

            raise ValueError(
                f"{camera_name}: invalid "
                "jam timing configuration."
            ) from error

        if history_seconds <= 0:

            raise ValueError(
                f"{camera_name}: "
                "jam history_seconds must "
                "be > 0."
            )

        if min_history_seconds <= 0:

            raise ValueError(
                f"{camera_name}: "
                "min_history_seconds must "
                "be > 0."
            )

        if (
            min_history_seconds
            >
            history_seconds
        ):

            raise ValueError(
                f"{camera_name}: "
                "min_history_seconds cannot "
                "exceed history_seconds."
            )

        if warning_time < 0:

            raise ValueError(
                f"{camera_name}: "
                "warning_time_seconds cannot "
                "be negative."
            )

        if jam_time < warning_time:

            raise ValueError(
                f"{camera_name}: "
                "jam_time_seconds must be >= "
                "warning_time_seconds."
            )

        if recovery_time < 0:

            raise ValueError(
                f"{camera_name}: "
                "recovery_time_seconds cannot "
                "be negative."
            )

        # --------------------------------------------------
        # SPEED
        # --------------------------------------------------

        try:

            stationary_speed = float(
                jam.get(
                    "stationary_speed_threshold",
                    10.0,
                )
            )

            slow_speed = float(
                jam.get(
                    "slow_speed_threshold",
                    30.0,
                )
            )

        except (
            TypeError,
            ValueError,
        ) as error:

            raise ValueError(
                f"{camera_name}: invalid "
                "jam speed thresholds."
            ) from error

        if stationary_speed < 0:

            raise ValueError(
                f"{camera_name}: "
                "stationary_speed_threshold "
                "cannot be negative."
            )

        if slow_speed < stationary_speed:

            raise ValueError(
                f"{camera_name}: "
                "slow_speed_threshold must be "
                ">= stationary_speed_threshold."
            )

        # --------------------------------------------------
        # JAM ROI
        # --------------------------------------------------

        roi = jam.get(
            "roi",
            {},
        )

        if not isinstance(
            roi,
            dict,
        ):

            raise ValueError(
                f"{camera_name}: "
                "jam ROI must be a dictionary."
            )

        x1 = Config._safe_int(
            roi.get(
                "x1",
                0,
            )
        )

        y1 = Config._safe_int(
            roi.get(
                "y1",
                0,
            )
        )

        x2 = Config._safe_int(
            roi.get(
                "x2",
                0,
            )
        )

        y2 = Config._safe_int(
            roi.get(
                "y2",
                0,
            )
        )

        if x1 == x2:

            raise ValueError(
                f"{camera_name}: "
                "jam ROI width cannot be zero."
            )

        if y1 == y2:

            raise ValueError(
                f"{camera_name}: "
                "jam ROI height cannot be zero."
            )

        # --------------------------------------------------
        # TRACK AGE
        # --------------------------------------------------

        try:

            min_track_age = int(
                jam.get(
                    "min_track_age",
                    4,
                )
            )

        except (
            TypeError,
            ValueError,
        ) as error:

            raise ValueError(
                f"{camera_name}: invalid "
                "jam min_track_age."
            ) from error

        if min_track_age < 1:

            raise ValueError(
                f"{camera_name}: "
                "jam min_track_age must "
                "be >= 1."
            )

        # --------------------------------------------------
        # TRACK TTL
        # --------------------------------------------------

        try:

            ttl = float(
                jam.get(
                    "track_ttl_seconds",
                    2.0,
                )
            )

        except (
            TypeError,
            ValueError,
        ) as error:

            raise ValueError(
                f"{camera_name}: invalid "
                "track_ttl_seconds."
            ) from error

        if ttl <= 0:

            raise ValueError(
                f"{camera_name}: "
                "track_ttl_seconds must "
                "be > 0."
            )

    # ======================================================
    # BAG SPACING VALIDATION - CONDITION B
    # ======================================================

    @staticmethod
    def _validate_bag_spacing_config(
        camera_name,
        config,
    ) -> None:

        # --------------------------------------------------
        # JAM THRESHOLD
        # --------------------------------------------------

        try:

            jam_threshold_mm = float(
                config.get(
                    "jam_threshold_mm",
                    305.0,
                )
            )

        except (
            TypeError,
            ValueError,
        ) as error:

            raise ValueError(
                f"{camera_name}: invalid "
                "bag_spacing jam_threshold_mm."
            ) from error

        if jam_threshold_mm <= 0:

            raise ValueError(
                f"{camera_name}: "
                "bag_spacing jam_threshold_mm "
                "must be > 0."
            )

        # --------------------------------------------------
        # ENGINEERING GAP VALUES
        # --------------------------------------------------

        try:

            minimum_safe_gap_mm = float(
                config.get(
                    "minimum_safe_gap_mm",
                    300.0,
                )
            )

            measurement_margin_mm = float(
                config.get(
                    "measurement_margin_mm",
                    5.0,
                )
            )

        except (
            TypeError,
            ValueError,
        ) as error:

            raise ValueError(
                f"{camera_name}: invalid "
                "bag spacing engineering values."
            ) from error

        if minimum_safe_gap_mm <= 0:

            raise ValueError(
                f"{camera_name}: "
                "minimum_safe_gap_mm must "
                "be > 0."
            )

        if measurement_margin_mm < 0:

            raise ValueError(
                f"{camera_name}: "
                "measurement_margin_mm cannot "
                "be negative."
            )

        # --------------------------------------------------
        # CONSISTENCY CHECK
        # --------------------------------------------------

        expected_threshold = (
            minimum_safe_gap_mm
            +
            measurement_margin_mm
        )

        if abs(
            jam_threshold_mm
            -
            expected_threshold
        ) > 0.001:

            raise ValueError(
                f"{camera_name}: "
                "bag_spacing jam_threshold_mm "
                "must equal "
                "minimum_safe_gap_mm + "
                "measurement_margin_mm. "
                f"Expected {expected_threshold:.3f} mm, "
                f"got {jam_threshold_mm:.3f} mm."
            )

        # --------------------------------------------------
        # DIRECTION
        # --------------------------------------------------

        direction = str(
            config.get(
                "direction",
                "up",
            )
        ).strip().lower()

        if direction not in {
            "up",
            "down",
            "left",
            "right",
        }:

            raise ValueError(
                f"{camera_name}: "
                "bag_spacing direction must be "
                "'up', 'down', 'left' or 'right'."
            )

        # --------------------------------------------------
        # MINIMUM TRACK AGE
        # --------------------------------------------------

        try:

            min_track_age = int(
                config.get(
                    "min_track_age",
                    4,
                )
            )

        except (
            TypeError,
            ValueError,
        ) as error:

            raise ValueError(
                f"{camera_name}: invalid "
                "bag_spacing min_track_age."
            ) from error

        if min_track_age < 1:

            raise ValueError(
                f"{camera_name}: "
                "bag_spacing min_track_age "
                "must be >= 1."
            )

        # --------------------------------------------------
        # SPACING ROI
        # --------------------------------------------------

        roi = config.get(
            "roi",
            {},
        )

        if not isinstance(
            roi,
            dict,
        ):

            raise ValueError(
                f"{camera_name}: "
                "bag_spacing ROI must be "
                "a dictionary."
            )

        x1 = Config._safe_int(
            roi.get(
                "x1",
                0,
            )
        )

        y1 = Config._safe_int(
            roi.get(
                "y1",
                0,
            )
        )

        x2 = Config._safe_int(
            roi.get(
                "x2",
                0,
            )
        )

        y2 = Config._safe_int(
            roi.get(
                "y2",
                0,
            )
        )

        if x1 == x2:

            raise ValueError(
                f"{camera_name}: "
                "bag_spacing ROI width "
                "cannot be zero."
            )

        if y1 == y2:

            raise ValueError(
                f"{camera_name}: "
                "bag_spacing ROI height "
                "cannot be zero."
            )

        # --------------------------------------------------
        # CALIBRATION
        # --------------------------------------------------

        calibration = config.get(
            "calibration",
            {},
        )

        if not isinstance(
            calibration,
            dict,
        ):

            raise ValueError(
                f"{camera_name}: "
                "bag_spacing calibration must "
                "be a dictionary."
            )

        image_points = calibration.get(
            "image_points",
            []
        )

        world_points = calibration.get(
            "world_points_mm",
            []
        )

        if (
            not isinstance(
                image_points,
                list,
            )
            or
            len(image_points) != 4
        ):

            raise ValueError(
                f"{camera_name}: "
                "bag_spacing calibration."
                "image_points must contain "
                "exactly 4 points."
            )

        if (
            not isinstance(
                world_points,
                list,
            )
            or
            len(world_points) != 4
        ):

            raise ValueError(
                f"{camera_name}: "
                "bag_spacing calibration."
                "world_points_mm must contain "
                "exactly 4 points."
            )

        # --------------------------------------------------
        # VALIDATE POINT FORMAT
        # --------------------------------------------------

        for label, points in (
            (
                "image_points",
                image_points,
            ),
            (
                "world_points_mm",
                world_points,
            ),
        ):

            for index, point in enumerate(
                points
            ):

                if (
                    not isinstance(
                        point,
                        (list, tuple),
                    )
                    or
                    len(point) != 2
                ):

                    raise ValueError(
                        f"{camera_name}: "
                        "bag_spacing calibration."
                        f"{label}[{index}] must "
                        "be [x, y]."
                    )

                try:

                    x = float(
                        point[0]
                    )

                    y = float(
                        point[1]
                    )

                except (
                    TypeError,
                    ValueError,
                ) as error:

                    raise ValueError(
                        f"{camera_name}: "
                        "bag_spacing calibration."
                        f"{label}[{index}] contains "
                        "invalid coordinates."
                    ) from error

                if not (
                    x == x
                    and
                    y == y
                ):

                    raise ValueError(
                        f"{camera_name}: "
                        "bag_spacing calibration."
                        f"{label}[{index}] contains "
                        "NaN coordinates."
                    )

        # --------------------------------------------------
        # CHECK DUPLICATE CALIBRATION POINTS
        # --------------------------------------------------

        image_point_set = {
            (
                float(point[0]),
                float(point[1]),
            )
            for point in image_points
        }

        world_point_set = {
            (
                float(point[0]),
                float(point[1]),
            )
            for point in world_points
        }

        if len(
            image_point_set
        ) != 4:

            raise ValueError(
                f"{camera_name}: "
                "bag_spacing image calibration "
                "points must be unique."
            )

        if len(
            world_point_set
        ) != 4:

            raise ValueError(
                f"{camera_name}: "
                "bag_spacing world calibration "
                "points must be unique."
            )

    # ======================================================
    # ROI OCCUPANCY VALIDATION - CONDITION C
    # ======================================================

    @staticmethod
    def _validate_condition_c_config(
        camera_name,
        config,
    ) -> None:

        try:

            max_allowed_bags = int(
                config.get(
                    "max_allowed_bags",
                    1,
                )
            )

            min_track_age = int(
                config.get(
                    "min_track_age",
                    4,
                )
            )

            cooldown = float(
                config.get(
                    "capture_cooldown_seconds",
                    5,
                )
            )

        except (
            TypeError,
            ValueError,
        ) as error:

            raise ValueError(
                f"{camera_name}: invalid condition_c configuration."
            ) from error

        if max_allowed_bags < 1:

            raise ValueError(
                f"{camera_name}: "
                "condition_c max_allowed_bags must be >= 1."
            )

        if min_track_age < 1:

            raise ValueError(
                f"{camera_name}: "
                "condition_c min_track_age must be >= 1."
            )

        if cooldown < 0:

            raise ValueError(
                f"{camera_name}: "
                "condition_c capture_cooldown_seconds must be >= 0."
            )

        roi = config.get(
            "roi",
            {},
        )

        if not isinstance(
            roi,
            dict,
        ):

            raise ValueError(
                f"{camera_name}: "
                "condition_c ROI must be a dictionary."
            )

        x1 = Config._safe_int(
            roi.get("x1", 0)
        )

        y1 = Config._safe_int(
            roi.get("y1", 0)
        )

        x2 = Config._safe_int(
            roi.get("x2", 0)
        )

        y2 = Config._safe_int(
            roi.get("y2", 0)
        )

        if x1 == x2:

            raise ValueError(
                f"{camera_name}: "
                "condition_c ROI width cannot be zero."
            )

        if y1 == y2:

            raise ValueError(
                f"{camera_name}: "
                "condition_c ROI height cannot be zero."
            )