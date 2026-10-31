"""
==========================================================
FillPac AI
Bag Spacing / Distance Jam Detector - Condition B
==========================================================

Purpose
-------
Detect an immediate bag-spacing jam when the calibrated
physical EDGE-TO-EDGE gap between two adjacent bags is
less than or equal to the configured threshold.

Condition B
-----------
    edge_gap_mm <= jam_threshold_mm
        -> JAM

    edge_gap_mm > jam_threshold_mm
        -> NORMAL

Important
---------
- NO timer is used.
- NO stationary condition is required.
- NO movement history is required.
- Only adjacent bags are compared.
- Track IDs are used only for association.
- Pixel coordinates are transformed into calibrated
  physical conveyor coordinates using homography.
- Gap is measured longitudinally along the conveyor.
- This detector works independently from Condition A.

Expected Configuration
----------------------
bag_spacing:

    enabled: true

    # Camera 2:
    # bags move RIGHT -> LEFT
    direction: left

    minimum_safe_gap_mm: 300.0
    measurement_margin_mm: 5.0
    jam_threshold_mm: 305.0

    min_track_age: 4

    ignore_unstable_tracks: true
    ignore_motion_jumps: true

    roi:
        x1: 273
        y1: 120
        x2: 1007
        y2: 600

    calibration:

        image_points:
            - [YOUR_P1_X, YOUR_P1_Y]
            - [YOUR_P2_X, YOUR_P2_Y]
            - [YOUR_P3_X, YOUR_P3_Y]
            - [YOUR_P4_X, YOUR_P4_Y]

        world_points_mm:
            - [0, 0]
            - [YOUR_LENGTH_MM, 0]
            - [YOUR_LENGTH_MM, YOUR_WIDTH_MM]
            - [0, YOUR_WIDTH_MM]

Output
------
update(tracks) returns:

{
    "enabled": True,
    "status": "normal" | "jam",
    "jam_detected": bool,

    "threshold_mm": 305.0,
    "minimum_gap_mm": ...,

    "pairs": [...],
    "distances": [...],
    "jam_pairs": [...],

    "active_jam_track_ids": [...]
}

Each pair contains both descriptive field names and
visualizer-compatible aliases.
==========================================================
"""

import math

import cv2
import numpy as np


class BagSpacingDetector:

    NORMAL = "normal"
    JAM = "jam"
    DISABLED = "disabled"

    # ======================================================
    # INITIALIZATION
    # ======================================================

    def __init__(
        self,
        config=None,
    ):

        config = config or {}

        # ==================================================
        # BASIC CONFIGURATION
        # ==================================================

        self.enabled = bool(
            config.get(
                "enabled",
                False,
            )
        )

        self.minimum_safe_gap_mm = max(
            float(
                config.get(
                    "minimum_safe_gap_mm",
                    300.0,
                )
            ),
            0.0,
        )

        self.measurement_margin_mm = max(
            float(
                config.get(
                    "measurement_margin_mm",
                    5.0,
                )
            ),
            0.0,
        )

        self.jam_threshold_mm = max(
            float(
                config.get(
                    "jam_threshold_mm",
                    (
                        self.minimum_safe_gap_mm
                        +
                        self.measurement_margin_mm
                    ),
                )
            ),
            0.0,
        )

        # ==================================================
        # CONVEYOR DIRECTION
        # ==================================================

        self.direction = str(
            config.get(
                "direction",
                "up",
            )
        ).strip().lower()

        if self.direction not in {
            "up",
            "down",
            "left",
            "right",
        }:

            raise ValueError(
                "BagSpacingDetector direction must "
                "be 'up', 'down', 'left' or 'right'."
            )

        # ==================================================
        # TRACK QUALITY
        # ==================================================

        self.min_track_age = max(
            int(
                config.get(
                    "min_track_age",
                    4,
                )
            ),
            1,
        )

        self.ignore_unstable_tracks = bool(
            config.get(
                "ignore_unstable_tracks",
                True,
            )
        )

        self.ignore_motion_jumps = bool(
            config.get(
                "ignore_motion_jumps",
                True,
            )
        )

        # ==================================================
        # STATE DEBOUNCE (HYSTERESIS)
        # ==================================================
        #
        # The raw per-frame decision (edge_gap_mm <= threshold)
        # is intentionally instantaneous with no timer, as
        # documented above. But bags gliding right at the
        # threshold produce a gap measurement that jitters a
        # few mm frame-to-frame due to detection/tracking noise,
        # which was flipping jam_detected true/false on
        # consecutive frames. Each flip is a state TRANSITION to
        # the pipeline, which starts/ends a SQL jam_events row --
        # so undebounced jitter was flooding jam_events with
        # many near-duplicate, sub-second rows for what is
        # physically a single jam (or no jam at all).
        #
        # This does not change the underlying gap_mm decision
        # rule -- it only requires that decision to be observed
        # for N consecutive frames before the *reported*
        # jam_detected flips, exactly like the existing
        # unstable_frame_threshold pattern used elsewhere in this
        # codebase for track quality.
        #
        # Defaults to 1 (no debounce, old behavior) so existing
        # deployments are unaffected unless the new keys are set.

        self.jam_confirm_frames = max(
            int(
                config.get(
                    "jam_confirm_frames",
                    1,
                )
            ),
            1,
        )

        self.recovery_confirm_frames = max(
            int(
                config.get(
                    "recovery_confirm_frames",
                    self.jam_confirm_frames,
                )
            ),
            1,
        )

        # Debounced/reported jam state (what callers see).
        self._debounced_jam_detected = False

        # Consecutive frames the RAW decision has matched the
        # candidate state opposite to the currently reported one.
        self._pending_jam_state = None
        self._pending_jam_streak = 0

        # ==================================================
        # SPACING ROI
        # ==================================================

        roi = (
            config.get(
                "roi",
                {},
            )
            or {}
        )

        self.roi = {

            "x1":
                int(
                    roi.get(
                        "x1",
                        0,
                    )
                ),

            "y1":
                int(
                    roi.get(
                        "y1",
                        0,
                    )
                ),

            "x2":
                int(
                    roi.get(
                        "x2",
                        0,
                    )
                ),

            "y2":
                int(
                    roi.get(
                        "y2",
                        0,
                    )
                ),
        }

        # ==================================================
        # CALIBRATION
        # ==================================================

        calibration = (
            config.get(
                "calibration",
                {},
            )
            or {}
        )

        self.image_points = np.asarray(
            calibration.get(
                "image_points",
                [],
            ),
            dtype=np.float32,
        )

        self.world_points = np.asarray(
            calibration.get(
                "world_points_mm",
                [],
            ),
            dtype=np.float32,
        )

        self.homography = None

        if self.enabled:

            self._build_homography()

        # ==================================================
        # LATEST RESULT
        # ==================================================

        self.last_result = (
            self._empty_result()
        )

    # ======================================================
    # BUILD HOMOGRAPHY
    # ======================================================

    def _build_homography(
        self,
    ):

        if self.image_points.shape != (
            4,
            2,
        ):

            raise ValueError(
                "bag_spacing.calibration.image_points "
                "must contain exactly four [x, y] points."
            )

        if self.world_points.shape != (
            4,
            2,
        ):

            raise ValueError(
                "bag_spacing.calibration.world_points_mm "
                "must contain exactly four [x, y] points."
            )

        homography = (
            cv2.getPerspectiveTransform(
                self.image_points,
                self.world_points,
            )
        )

        if homography is None:

            raise ValueError(
                "Unable to calculate bag-spacing "
                "homography."
            )

        if not np.all(
            np.isfinite(
                homography
            )
        ):

            raise ValueError(
                "Bag-spacing homography contains "
                "invalid values."
            )

        self.homography = homography

    # ======================================================
    # PIXEL -> PHYSICAL WORLD
    # ======================================================

    def pixel_to_world(
        self,
        point,
    ):

        if self.homography is None:

            return None

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
            IndexError,
        ):

            return None

        if not (
            math.isfinite(
                x
            )
            and
            math.isfinite(
                y
            )
        ):

            return None

        src = np.array(
            [
                [
                    [
                        x,
                        y,
                    ]
                ]
            ],
            dtype=np.float32,
        )

        try:

            transformed = (
                cv2.perspectiveTransform(
                    src,
                    self.homography,
                )
            )

        except cv2.error:

            return None

        wx, wy = (
            transformed[
                0
            ][
                0
            ]
        )

        wx = float(
            wx
        )

        wy = float(
            wy
        )

        if not (
            math.isfinite(
                wx
            )
            and
            math.isfinite(
                wy
            )
        ):

            return None

        return (
            wx,
            wy,
        )

    # ======================================================
    # ROI CHECK
    # ======================================================

    def _point_inside_roi(
        self,
        x,
        y,
    ):

        x1 = min(
            self.roi["x1"],
            self.roi["x2"],
        )

        x2 = max(
            self.roi["x1"],
            self.roi["x2"],
        )

        y1 = min(
            self.roi["y1"],
            self.roi["y2"],
        )

        y2 = max(
            self.roi["y1"],
            self.roi["y2"],
        )

        # ----------------------------------------------
        # No ROI configured -> allow whole frame.
        # ----------------------------------------------

        if (
            x1 == x2
            and
            y1 == y2
        ):

            return True

        return (
            x1 <= x <= x2
            and
            y1 <= y <= y2
        )

    # ======================================================
    # TRACK VALIDATION
    # ======================================================

    def _valid_track(
        self,
        track,
    ):

        if not isinstance(
            track,
            dict,
        ):

            return False

        # ----------------------------------------------
        # BAG CLASS ONLY
        #
        # Tracker output may not always contain class_id.
        # If class_id exists, it must be class 0.
        # ----------------------------------------------

        class_id = track.get(
            "class_id"
        )

        if (
            class_id is not None
            and
            class_id != 0
        ):

            return False

        # ----------------------------------------------
        # TRACK ID
        # ----------------------------------------------

        if track.get(
            "track_id"
        ) is None:

            return False

        # ----------------------------------------------
        # BOUNDING BOX
        # ----------------------------------------------

        bbox = track.get(
            "bbox"
        )

        if (
            bbox is None
            or
            len(
                bbox
            ) != 4
        ):

            return False

        try:

            x1, y1, x2, y2 = [
                float(
                    value
                )
                for value in bbox
            ]

        except (
            TypeError,
            ValueError,
        ):

            return False

        if not all(
            math.isfinite(
                value
            )
            for value in (
                x1,
                y1,
                x2,
                y2,
            )
        ):

            return False

        if (
            x2 <= x1
            or
            y2 <= y1
        ):

            return False

        # ----------------------------------------------
        # CENTER
        # ----------------------------------------------

        center = track.get(
            "center"
        )

        # If tracker doesn't provide center,
        # calculate it from bbox.

        if (
            center is None
            or
            len(
                center
            ) != 2
        ):

            center = (
                (
                    x1
                    +
                    x2
                )
                / 2.0,
                (
                    y1
                    +
                    y2
                )
                / 2.0,
            )

        try:

            cx = float(
                center[0]
            )

            cy = float(
                center[1]
            )

        except (
            TypeError,
            ValueError,
            IndexError,
        ):

            return False

        # ----------------------------------------------
        # TRACK AGE
        # ----------------------------------------------

        track_age = int(
            track.get(
                "track_age",
                track.get(
                    "age",
                    0,
                ),
            )
            or 0
        )

        if (
            track_age
            <
            self.min_track_age
        ):

            return False

        # ----------------------------------------------
        # UNSTABLE TRACK
        # ----------------------------------------------

        if (
            self.ignore_unstable_tracks
            and
            track.get(
                "unstable",
                False,
            )
        ):

            return False

        # ----------------------------------------------
        # MOTION JUMP
        # ----------------------------------------------

        if (
            self.ignore_motion_jumps
            and
            track.get(
                "motion_jump",
                False,
            )
        ):

            return False

        # ----------------------------------------------
        # SPACING ROI
        # ----------------------------------------------

        return self._point_inside_roi(
            cx,
            cy,
        )

    # ======================================================
    # PREPARE BAG
    # ======================================================

    def _prepare_bag(
        self,
        track,
    ):

        x1, y1, x2, y2 = [
            float(
                value
            )
            for value in track[
                "bbox"
            ]
        ]

        cx = (
            x1
            +
            x2
        ) / 2.0

        cy = (
            y1
            +
            y2
        ) / 2.0

        return {

            "track_id":
                int(
                    track[
                        "track_id"
                    ]
                ),

            "bbox":
                (
                    x1,
                    y1,
                    x2,
                    y2,
                ),

            "center":
                (
                    cx,
                    cy,
                ),

            "track_age":
                int(
                    track.get(
                        "track_age",
                        track.get(
                            "age",
                            0,
                        ),
                    )
                    or 0
                ),
        }

    # ======================================================
    # ORDER BAGS ALONG CONVEYOR
    # ======================================================

    def _sort_bags(self, bags):
        """
        Order bags along the physical conveyor direction.

        Image coordinates:
            X -> left/right
            Y -> top/bottom

        For horizontal conveyor:
            left  -> right
            right -> left

        For vertical conveyor:
            up
            down
        """

        if self.direction == "right":
            # Smaller X is physically behind.
            # Larger X is physically ahead.
            return sorted(
                bags,
                key=lambda bag: bag["center"][0],
                reverse=True,
            )

        if self.direction == "left":
            # Larger X is physically behind.
            # Smaller X is physically ahead.
            return sorted(
                bags,
                key=lambda bag: bag["center"][0],
            )

        if self.direction == "up":
            return sorted(
                bags,
                key=lambda bag: bag["center"][1],
            )

        # down
        return sorted(
            bags,
            key=lambda bag: bag["center"][1],
            reverse=True,
        )

    # ======================================================
    # GET FACING BAG EDGES
    # ======================================================

    def _facing_edge_points(
        self,
        front_bag,
        rear_bag,
    ):
        """
        Return the two bag edges that face each other.

        The conveyor direction determines which edges
        are used.

        LEFT movement:

            REAR BAG          FRONT BAG
            ┌─────────┐ GAP ┌─────────┐
            │         │     │         │
            └─────────┘     └─────────┘
                      ↑     ↑
                    right  left
                     edge  edge

        RIGHT movement:

            FRONT BAG          REAR BAG
            ┌─────────┐ GAP ┌─────────┐
            │         │     │         │
            └─────────┘     └─────────┘
                      ↑     ↑
                    right  left
                     edge  edge

        UP movement:
            bottom / top

        DOWN movement:
            top / bottom
        """

        fx1, fy1, fx2, fy2 = (
            front_bag["bbox"]
        )

        rx1, ry1, rx2, ry2 = (
            rear_bag["bbox"]
        )

        front_cy = (
            fy1 + fy2
        ) / 2.0

        rear_cy = (
            ry1 + ry2
        ) / 2.0

        front_cx = (
            fx1 + fx2
        ) / 2.0

        rear_cx = (
            rx1 + rx2
        ) / 2.0

        # ============================================
        # LEFT
        # ============================================

        if self.direction == "left":

            # Front bag is on the LEFT.
            # Its trailing edge is RIGHT edge.
            front_edge = (
                fx2,
                front_cy,
            )

            # Rear bag is on the RIGHT.
            # Its leading edge is LEFT edge.
            rear_edge = (
                rx1,
                rear_cy,
            )

        # ============================================
        # RIGHT
        # ============================================

        elif self.direction == "right":

            # Front bag is on the RIGHT.
            # Its trailing edge is LEFT edge.
            front_edge = (
                fx1,
                front_cy,
            )

            # Rear bag is on the LEFT.
            # Its leading edge is RIGHT edge.
            rear_edge = (
                rx2,
                rear_cy,
            )

        # ============================================
        # UP
        # ============================================

        elif self.direction == "up":

            front_edge = (
                front_cx,
                fy2,
            )

            rear_edge = (
                rear_cx,
                ry1,
            )

        # ============================================
        # DOWN
        # ============================================

        else:

            front_edge = (
                front_cx,
                fy1,
            )

            rear_edge = (
                rear_cx,
                ry2,
            )

        return (
            front_edge,
            rear_edge,
        )

    # ======================================================
    # CALCULATE ADJACENT PAIR
    # ======================================================

    def _calculate_pair(
        self,
        front_bag,
        rear_bag,
    ):

        # --------------------------------------------------
        # PIXEL EDGE POINTS
        # --------------------------------------------------

        (
            front_edge_px,
            rear_edge_px,
        ) = self._facing_edge_points(
            front_bag,
            rear_bag,
        )

        # --------------------------------------------------
        # PIXEL -> WORLD
        # --------------------------------------------------

        front_edge_world = (
            self.pixel_to_world(
                front_edge_px
            )
        )

        rear_edge_world = (
            self.pixel_to_world(
                rear_edge_px
            )
        )

        if (
            front_edge_world is None
            or
            rear_edge_world is None
        ):

            return None

        # --------------------------------------------------
        # PHYSICAL LONGITUDINAL GAP
        #
        # Horizontal conveyor:
        #     World X = conveyor direction
        #
        # Vertical conveyor:
        #     World Y = conveyor direction
        #
        # Only longitudinal displacement is measured.
        # --------------------------------------------------

        if self.direction in {
            "left",
            "right",
        }:

            front_world_axis = float(
                front_edge_world[0]
            )

            rear_world_axis = float(
                rear_edge_world[0]
            )

        else:

            front_world_axis = float(
                front_edge_world[1]
            )

            rear_world_axis = float(
                rear_edge_world[1]
            )

        gap_mm = abs(
            rear_world_axis
            -
            front_world_axis
        )

        if not math.isfinite(
            gap_mm
        ):

            return None

        # --------------------------------------------------
        # CONDITION B
        #
        # Immediate decision.
        #
        # NO TIMER.
        # --------------------------------------------------

        jam_detected = bool(
            gap_mm
            <=
            self.jam_threshold_mm
        )

        status = (
            self.JAM
            if jam_detected
            else self.NORMAL
        )

        # --------------------------------------------------
        # NORMALIZED OUTPUT VALUES
        # --------------------------------------------------

        front_edge_px_out = (
            round(
                float(
                    front_edge_px[
                        0
                    ]
                ),
                2,
            ),
            round(
                float(
                    front_edge_px[
                        1
                    ]
                ),
                2,
            ),
        )

        rear_edge_px_out = (
            round(
                float(
                    rear_edge_px[
                        0
                    ]
                ),
                2,
            ),
            round(
                float(
                    rear_edge_px[
                        1
                    ]
                ),
                2,
            ),
        )

        front_edge_world_out = (
            round(
                float(
                    front_edge_world[
                        0
                    ]
                ),
                2,
            ),
            round(
                float(
                    front_edge_world[
                        1
                    ]
                ),
                2,
            ),
        )

        rear_edge_world_out = (
            round(
                float(
                    rear_edge_world[
                        0
                    ]
                ),
                2,
            ),
            round(
                float(
                    rear_edge_world[
                        1
                    ]
                ),
                2,
            ),
        )

        gap_mm_out = round(
            float(
                gap_mm
            ),
            2,
        )

        front_track_id = (
            front_bag[
                "track_id"
            ]
        )

        rear_track_id = (
            rear_bag[
                "track_id"
            ]
        )

        # --------------------------------------------------
        # RETURN PAIR
        #
        # Both canonical fields and aliases are returned.
        #
        # This keeps pipeline/dashboard/visualizer
        # compatibility straightforward.
        # --------------------------------------------------

        return {

            # ==============================================
            # TRACK ASSOCIATION
            # ==============================================

            "front_track_id":
                front_track_id,

            "rear_track_id":
                rear_track_id,

            # Generic aliases used by visualizer.
            "track_id_a":
                front_track_id,

            "track_id_b":
                rear_track_id,

            "track_ids":
                [
                    front_track_id,
                    rear_track_id,
                ],

            # ==============================================
            # IMAGE EDGE POINTS
            # ==============================================

            "front_edge_px":
                front_edge_px_out,

            "rear_edge_px":
                rear_edge_px_out,

            # Visualizer-compatible aliases.
            "edge_point_a":
                front_edge_px_out,

            "edge_point_b":
                rear_edge_px_out,

            "image_edge_a":
                front_edge_px_out,

            "image_edge_b":
                rear_edge_px_out,

            # ==============================================
            # PHYSICAL EDGE POINTS
            # ==============================================

            "front_edge_world_mm":
                front_edge_world_out,

            "rear_edge_world_mm":
                rear_edge_world_out,

            "world_edge_a_mm":
                front_edge_world_out,

            "world_edge_b_mm":
                rear_edge_world_out,

            # ==============================================
            # GAP
            # ==============================================

            "edge_gap_mm":
                gap_mm_out,

            # Generic alias.
            "gap_mm":
                gap_mm_out,

            # ==============================================
            # THRESHOLD
            # ==============================================

            "threshold_mm":
                self.jam_threshold_mm,

            "minimum_safe_gap_mm":
                self.minimum_safe_gap_mm,

            "measurement_margin_mm":
                self.measurement_margin_mm,

            # ==============================================
            # DECISION
            # ==============================================

            "jam_detected":
                jam_detected,

            "is_jam":
                jam_detected,

            "status":
                status,

            # ==============================================
            # STANDARDIZED DISTANCE FIELDS
            # ==============================================

            "track1":
                front_track_id,

            "track2":
                rear_track_id,

            "distance_mm":
                gap_mm_out,

            "distance_px":
                round(
                    abs(
                        front_edge_px[0]
                        -
                        rear_edge_px[0]
                    ),
                    2,
                )
                if self.direction in {
                    "left",
                    "right",
                }
                else
                round(
                    abs(
                        front_edge_px[1]
                        -
                        rear_edge_px[1]
                    ),
                    2,
                ),

            # ==============================================
            # BAG INFORMATION
            # ==============================================

            "front_bbox":
                front_bag["bbox"],

            "rear_bbox":
                rear_bag["bbox"],

            "front_center":
                front_bag["center"],

            "rear_center":
                rear_bag["center"],
        }

    # ======================================================
    # UPDATE
    # ======================================================

    def _debounce_jam_state(
        self,
        raw_jam_detected,
    ):
        """
        Turn the instantaneous per-frame gap decision into a
        stable, hysteresis-filtered jam_detected flag.

        A candidate state opposite to the currently reported one
        must be observed for `jam_confirm_frames` (going into a
        jam) or `recovery_confirm_frames` (coming out of one)
        consecutive frames before the reported state actually
        flips. This prevents a gap measurement that jitters
        around the threshold from producing a burst of
        start/end jam_events rows in SQL Server for what is
        physically a single event.
        """

        if raw_jam_detected == self._debounced_jam_detected:

            # Raw decision agrees with what's already reported --
            # nothing pending, reset any partial streak.
            self._pending_jam_state = None
            self._pending_jam_streak = 0

            return self._debounced_jam_detected

        # Raw decision disagrees with the reported state --
        # accumulate (or start) a streak toward flipping it.

        if self._pending_jam_state == raw_jam_detected:
            self._pending_jam_streak += 1
        else:
            self._pending_jam_state = raw_jam_detected
            self._pending_jam_streak = 1

        required_streak = (
            self.jam_confirm_frames
            if raw_jam_detected
            else self.recovery_confirm_frames
        )

        if self._pending_jam_streak >= required_streak:

            self._debounced_jam_detected = raw_jam_detected
            self._pending_jam_state = None
            self._pending_jam_streak = 0

        return self._debounced_jam_detected

    def update(
        self,
        tracks,
    ):

        # ==================================================
        # DISABLED
        # ==================================================

        if not self.enabled:

            self.last_result = (
                self._empty_result()
            )

            return self.last_result

        tracks = (
            tracks
            or []
        )

        # ==================================================
        # VALIDATE / PREPARE BAGS
        # ==================================================

        valid_bags = []

        for track in tracks:

            if not self._valid_track(
                track
            ):

                continue

            try:

                bag = (
                    self._prepare_bag(
                        track
                    )
                )

            except (
                TypeError,
                ValueError,
                IndexError,
                KeyError,
            ):

                continue

            valid_bags.append(
                bag
            )

        # ==================================================
        # ORDER BAGS ALONG CONVEYOR
        # ==================================================

        ordered_bags = (
            self._sort_bags(
                valid_bags
            )
        )

        # ==================================================
        # ADJACENT PAIRS ONLY
        #
        # Example:
        #
        # A B C D
        #
        # Compare:
        #
        # A-B
        # B-C
        # C-D
        #
        # Do NOT compare:
        #
        # A-C
        # A-D
        # B-D
        # ==================================================

        pairs = []

        for index in range(
            len(
                ordered_bags
            )
            -
            1
        ):

            front_bag = (
                ordered_bags[
                    index
                ]
            )

            rear_bag = (
                ordered_bags[
                    index
                    +
                    1
                ]
            )

            pair = (
                self._calculate_pair(
                    front_bag,
                    rear_bag,
                )
            )

            if pair is None:

                continue

            pairs.append(
                pair
            )

        # ==================================================
        # JAM PAIRS
        # ==================================================

        jam_pairs = [

            pair

            for pair in pairs

            if pair.get(
                "jam_detected",
                False,
            )
        ]

        # ==================================================
        # MINIMUM PHYSICAL GAP
        # ==================================================

        minimum_gap_mm = None

        if pairs:

            minimum_gap_mm = min(
                float(
                    pair[
                        "edge_gap_mm"
                    ]
                )
                for pair in pairs
            )

            minimum_gap_mm = round(
                minimum_gap_mm,
                2,
            )

        # ==================================================
        # FINAL CONDITION B STATE
        # ==================================================

        raw_jam_detected = bool(
            jam_pairs
        )

        jam_detected = self._debounce_jam_state(
            raw_jam_detected
        )

        status = (
            self.JAM
            if jam_detected
            else self.NORMAL
        )

        # ==================================================
        # JAM REASON
        # ==================================================

        reason = None

        if jam_detected:

            if (
                minimum_gap_mm is not None
                and minimum_gap_mm < self.jam_threshold_mm
            ):
                reason = (
                    f"Minimum bag spacing "
                    f"{minimum_gap_mm:.2f} mm "
                    f"is below threshold "
                    f"{self.jam_threshold_mm:.2f} mm"
                )

            else:
                reason = "Minimum spacing between bags exceeded limit"

        # ==================================================
        # ACTIVE JAM TRACK IDS
        # ==================================================

        active_jam_track_ids = sorted(
            {
                track_id

                for pair in jam_pairs

                for track_id in (
                    pair[
                        "front_track_id"
                    ],
                    pair[
                        "rear_track_id"
                    ],
                )

                if track_id is not None
            }
        )

        # ==================================================
        # RESULT
        # ==================================================

        self.last_result = {

            "enabled":
                True,

            "status":
                status,

            "jam_detected":
                jam_detected,

            "jam_type":
                (
                    "bag_spacing"
                    if jam_detected
                    else None
                ),

            "reason":
                reason,

            # ==============================================
            # CONFIGURATION
            # ==============================================

            "threshold_mm":
                self.jam_threshold_mm,

            "minimum_safe_gap_mm":
                self.minimum_safe_gap_mm,

            "measurement_margin_mm":
                self.measurement_margin_mm,

            "direction":
                self.direction,

            # ==============================================
            # ROI / CALIBRATION INFO
            # ==============================================

            "roi":
                dict(
                    self.roi
                ),

            "calibrated":
                (
                    self.homography
                    is not None
                ),

            # ==============================================
            # LIVE BAG INFORMATION
            # ==============================================

            "bag_count_in_roi":
                len(
                    ordered_bags
                ),

            "pair_count":
                len(
                    pairs
                ),

            # ==============================================
            # DISTANCE
            # ==============================================

            "minimum_gap_mm":
                minimum_gap_mm,

            # ==============================================
            # PAIRS
            # ==============================================

            "pairs":
                pairs,

            # Canonical distance output
            "distances":
                pairs,

            "jam_pairs":
                jam_pairs,

            # ==============================================
            # JAM TRACKS
            # ==============================================

            "active_jam_count":
                len(
                    active_jam_track_ids
                ),

            "active_jam_track_ids":
                active_jam_track_ids,
        }

        return self.last_result

    # ======================================================
    # RESET
    # ======================================================

    def reset(
        self,
    ):

        # Condition B has no temporal timer/history.
        #
        # Reset clears the latest result and any in-progress
        # debounce streak/state.

        self.last_result = (
            self._empty_result()
        )

        self._debounced_jam_detected = False
        self._pending_jam_state = None
        self._pending_jam_streak = 0

    # ======================================================
    # EMPTY RESULT
    # ======================================================

    def _empty_result(
        self,
    ):

        return {

            "enabled":
                self.enabled,

            "status":
                (
                    self.NORMAL
                    if self.enabled
                    else self.DISABLED
                ),

            "jam_detected":
                False,

            "jam_type":
                None,

            # ==============================================
            # CONFIGURATION
            # ==============================================

            "threshold_mm":
                self.jam_threshold_mm,

            "minimum_safe_gap_mm":
                self.minimum_safe_gap_mm,

            "measurement_margin_mm":
                self.measurement_margin_mm,

            "direction":
                self.direction,

            # ==============================================
            # ROI / CALIBRATION
            # ==============================================

            "roi":
                dict(
                    self.roi
                ),

            "calibrated":
                (
                    self.homography
                    is not None
                ),

            # ==============================================
            # LIVE VALUES
            # ==============================================

            "bag_count_in_roi":
                0,

            "pair_count":
                0,

            "minimum_gap_mm":
                None,

            # ==============================================
            # PAIRS
            # ==============================================

            "pairs":
                [],

            "distances":
                [],

            "jam_pairs":
                [],

            # ==============================================
            # JAM TRACKS
            # ==============================================

            "active_jam_count":
                0,

            "active_jam_track_ids":
                [],
        }