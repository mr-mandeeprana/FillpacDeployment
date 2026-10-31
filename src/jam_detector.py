"""
==========================================================
FillPac AI
Bag Jam Detection Engine - V1
==========================================================

Purpose
-------
Detect bags that fail to make sufficient physical movement
inside a configured Jam ROI.

V1 Architecture
---------------
YOLO
    |
    v
ByteTrack
    |
    v
Track center (X, Y)
    |
    v
JamDetector
    |
    +---- Rolling X/Y trajectory
    +---- Euclidean displacement
    +---- Speed (pixels / second)
    +---- Stationary timer
    +---- Jam state machine
    |
    v
NORMAL / SLOW / WARNING / JAM / RECOVERING


Important
---------
- Jam detection does NOT perform bag counting.
- Physical-center counting remains inside Counter.
- Track IDs are used only for temporal association.
- Movement is calculated using timestamps, therefore
  inference FPS variations do not directly affect speed.
- V1 intentionally does NOT include accumulation/blockage
  detection. That belongs to V2.
==========================================================
"""

import math
import time

from collections import deque


class JamDetector:
    """
    Camera-specific bag jam detector.

    Every Pipeline should create its OWN JamDetector.

    Expected track structure
    ------------------------
    {
        "track_id": int,
        "bbox": (...),
        "center": (cx, cy),
        "confidence": float,
        "unstable": bool,
        "motion_jump": bool,
        ...
    }
    """

    # ======================================================
    # STATES
    # ======================================================

    NORMAL = "normal"
    SLOW = "slow"
    WARNING = "warning"
    JAM = "jam"
    RECOVERING = "recovering"

    # ======================================================
    # INITIALIZATION
    # ======================================================

    def __init__(
        self,
        config=None,
    ):

        config = config or {}

        # ==================================================
        # ENABLE / DISABLE
        # ==================================================

        self.enabled = bool(
            config.get(
                "enabled",
                False,
            )
        )

        # ==================================================
        # JAM ROI
        #
        # Rectangular ROI:
        #
        # x1,y1 -------- x2,y1
        #   |              |
        #   |              |
        # x1,y2 -------- x2,y2
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
        # TRAJECTORY CONFIGURATION
        # ==================================================

        self.history_seconds = max(
            float(
                config.get(
                    "history_seconds",
                    1.0,
                )
            ),
            0.1,
        )

        # Minimum history duration before movement
        # classification becomes valid.
        self.min_history_seconds = max(
            float(
                config.get(
                    "min_history_seconds",
                    0.5,
                )
            ),
            0.1,
        )

        # ==================================================
        # MOVEMENT THRESHOLDS
        #
        # Movement is measured between the oldest and newest
        # points in the rolling trajectory window.
        #
        # IMPORTANT:
        # These are initial values only.
        # They MUST later be calibrated using real FillPac
        # production footage.
        # ==================================================

        self.stationary_speed_threshold = max(
            float(
                config.get(
                    "stationary_speed_threshold",
                    10.0,
                )
            ),
            0.0,
        )

        self.slow_speed_threshold = max(
            float(
                config.get(
                    "slow_speed_threshold",
                    30.0,
                )
            ),
            self.stationary_speed_threshold,
        )

        # ==================================================
        # STATE TIMING
        # ==================================================

        self.warning_time = max(
            float(
                config.get(
                    "warning_time_seconds",
                    2.0,
                )
            ),
            0.0,
        )

        self.jam_time = max(
            float(
                config.get(
                    "jam_time_seconds",
                    5.0,
                )
            ),
            self.warning_time,
        )

        self.recovery_time = max(
            float(
                config.get(
                    "recovery_time_seconds",
                    1.0,
                )
            ),
            0.0,
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
        # HISTORY CLEANUP
        #
        # Remove tracks that have disappeared for longer
        # than this period.
        # ==================================================

        self.track_ttl_seconds = max(
            float(
                config.get(
                    "track_ttl_seconds",
                    2.0,
                )
            ),
            0.5,
        )

        # ==================================================
        # INTERNAL MEMORY
        # ==================================================

        # track_id -> deque(
        #     [
        #       (timestamp, x, y),
        #       ...
        #     ]
        # )
        self.history = {}

        # track_id -> state
        self.states = {}

        # track_id -> timestamp
        self.stationary_since = {}

        # track_id -> timestamp
        self.recovery_since = {}

        # track_id -> timestamp
        self.last_seen = {}

        # Latest calculated metrics.
        self.metrics = {}

        # ==================================================
        # CAMERA-LEVEL STATE
        # ==================================================

        self.camera_state = self.NORMAL

        self.active_jam_track_ids = set()

        self.last_result = self._empty_result()

    # ======================================================
    # PUBLIC UPDATE
    # ======================================================

    def update(
        self,
        tracks,
        timestamp=None,
    ):
        """
        Process all current tracks.

        Parameters
        ----------
        tracks:
            List of tracked bag dictionaries.

        timestamp:
            Optional monotonic timestamp.

            If None, time.perf_counter() is used.

        Returns
        -------
        dict containing camera-level jam state and
        per-track metrics.
        """

        if timestamp is None:

            timestamp = (
                time.perf_counter()
            )

        if not self.enabled:

            self.last_result = (
                self._empty_result()
            )

            return self.last_result

        active_ids = set()

        track_results = []

        # ==================================================
        # PROCESS TRACKS
        # ==================================================

        for track in tracks:

            track_id = track.get(
                "track_id"
            )

            center = track.get(
                "center"
            )

            if (
                track_id is None
                or
                center is None
            ):

                continue

            try:

                track_id = int(
                    track_id
                )

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

                continue

            # ----------------------------------------------
            # Track age
            # ----------------------------------------------

            track_age = int(
                track.get(
                    "track_age",
                    0,
                )
                or 0
            )

            if (
                track_age
                < self.min_track_age
            ):

                continue

            # ----------------------------------------------
            # Track quality
            # ----------------------------------------------

            if (
                self.ignore_unstable_tracks
                and
                track.get(
                    "unstable",
                    False,
                )
            ):

                continue

            if (
                self.ignore_motion_jumps
                and
                track.get(
                    "motion_jump",
                    False,
                )
            ):

                continue

            active_ids.add(
                track_id
            )

            self.last_seen[
                track_id
            ] = timestamp

            # ----------------------------------------------
            # Jam ROI
            # ----------------------------------------------

            inside_roi = (
                self._point_inside_roi(
                    cx,
                    cy,
                )
            )

            if not inside_roi:

                self._handle_outside_roi(
                    track_id
                )

                continue

            # ----------------------------------------------
            # Add trajectory point
            # ----------------------------------------------

            self._add_history_point(
                track_id=track_id,
                timestamp=timestamp,
                x=cx,
                y=cy,
            )

            # ----------------------------------------------
            # Calculate movement
            # ----------------------------------------------

            movement = (
                self._calculate_movement(
                    track_id
                )
            )

            # History not mature enough yet.
            if movement is None:

                state = self.states.get(
                    track_id,
                    self.NORMAL,
                )

                track_results.append(
                    {
                        "track_id":
                            track_id,

                        "state":
                            state,

                        "center_x":
                            cx,

                        "center_y":
                            cy,

                        "inside_roi":
                            True,

                        "history_ready":
                            False,

                        "distance_pixels":
                            0.0,

                        "speed_px_s":
                            0.0,

                        "stationary_seconds":
                            0.0,
                    }
                )

                continue

            distance = movement[
                "distance"
            ]

            speed = movement[
                "speed"
            ]

            duration = movement[
                "duration"
            ]

            # ----------------------------------------------
            # State machine
            # ----------------------------------------------

            state = (
                self._update_state(
                    track_id=track_id,
                    speed=speed,
                    timestamp=timestamp,
                )
            )

            stationary_seconds = (
                self._stationary_duration(
                    track_id,
                    timestamp,
                )
            )

            metrics = {

                "track_id":
                    track_id,

                "state":
                    state,

                "center_x":
                    cx,

                "center_y":
                    cy,

                "inside_roi":
                    True,

                "history_ready":
                    True,

                "distance_pixels":
                    round(
                        distance,
                        3,
                    ),

                "speed_px_s":
                    round(
                        speed,
                        3,
                    ),

                "history_duration_seconds":
                    round(
                        duration,
                        3,
                    ),

                "stationary_seconds":
                    round(
                        stationary_seconds,
                        3,
                    ),
            }

            self.metrics[
                track_id
            ] = metrics

            track_results.append(
                metrics
            )

        # ==================================================
        # CLEAN DISAPPEARED TRACKS
        # ==================================================

        self._cleanup(
            timestamp
        )

        # ==================================================
        # CAMERA LEVEL STATE
        # ==================================================

        self._update_camera_state()

        self.last_result = {

            "enabled":
                True,

            "status":
                self.camera_state,

            "jam_detected":
                self.camera_state
                == self.JAM,

            "warning":
                self.camera_state
                == self.WARNING,

            "active_jam_track_ids":
                sorted(
                    self.active_jam_track_ids
                ),

            "active_jam_count":
                len(
                    self.active_jam_track_ids
                ),

            "tracks":
                track_results,
        }

        return self.last_result

    # ======================================================
    # ADD HISTORY
    # ======================================================

    def _add_history_point(
        self,
        track_id,
        timestamp,
        x,
        y,
    ):

        if track_id not in self.history:

            self.history[
                track_id
            ] = deque()

        history = self.history[
            track_id
        ]

        history.append(
            (
                timestamp,
                x,
                y,
            )
        )

        cutoff = (
            timestamp
            - self.history_seconds
        )

        # Keep one point just before the cutoff where
        # possible. This gives a stable full-window
        # displacement measurement.
        while (
            len(history) > 2
            and
            history[1][0]
            < cutoff
        ):

            history.popleft()

    # ======================================================
    # MOVEMENT CALCULATION
    # ======================================================

    def _calculate_movement(
        self,
        track_id,
    ):

        history = self.history.get(
            track_id
        )

        if (
            history is None
            or
            len(history) < 2
        ):

            return None

        first = history[0]
        last = history[-1]

        duration = (
            last[0]
            - first[0]
        )

        if (
            duration
            < self.min_history_seconds
        ):

            return None

        dx = (
            last[1]
            - first[1]
        )

        dy = (
            last[2]
            - first[2]
        )

        # ----------------------------------------------
        # Euclidean displacement
        # ----------------------------------------------

        distance = math.hypot(
            dx,
            dy,
        )

        # ----------------------------------------------
        # Pixels / second
        # ----------------------------------------------

        if duration <= 0:

            speed = 0.0

        else:

            speed = (
                distance
                / duration
            )

        return {

            "dx":
                dx,

            "dy":
                dy,

            "distance":
                distance,

            "duration":
                duration,

            "speed":
                speed,
        }

    # ======================================================
    # STATE MACHINE
    # ======================================================

    def _update_state(
        self,
        track_id,
        speed,
        timestamp,
    ):

        current_state = (
            self.states.get(
                track_id,
                self.NORMAL,
            )
        )

        # ==================================================
        # STATIONARY / VERY LOW MOVEMENT
        # ==================================================

        if (
            speed
            <= self.stationary_speed_threshold
        ):

            self.recovery_since.pop(
                track_id,
                None,
            )

            if (
                track_id
                not in self.stationary_since
            ):

                self.stationary_since[
                    track_id
                ] = timestamp

            stationary_duration = (
                timestamp
                - self.stationary_since[
                    track_id
                ]
            )

            if (
                stationary_duration
                >= self.jam_time
            ):

                state = self.JAM

            elif (
                stationary_duration
                >= self.warning_time
            ):

                state = self.WARNING

            else:

                state = self.SLOW

            self.states[
                track_id
            ] = state

            return state

        # ==================================================
        # SLOW BUT STILL MOVING
        # ==================================================

        if (
            speed
            <= self.slow_speed_threshold
        ):

            # A slowly moving bag should not accumulate
            # stationary time indefinitely.
            self.stationary_since.pop(
                track_id,
                None,
            )

            self.recovery_since.pop(
                track_id,
                None,
            )

            self.states[
                track_id
            ] = self.SLOW

            return self.SLOW

        # ==================================================
        # NORMAL MOVEMENT
        # ==================================================

        self.stationary_since.pop(
            track_id,
            None,
        )

        # If the bag was previously warning/jammed,
        # require sustained movement before returning
        # directly to NORMAL.
        if current_state in (
            self.WARNING,
            self.JAM,
            self.RECOVERING,
        ):

            if (
                track_id
                not in self.recovery_since
            ):

                self.recovery_since[
                    track_id
                ] = timestamp

            recovery_duration = (
                timestamp
                - self.recovery_since[
                    track_id
                ]
            )

            if (
                recovery_duration
                < self.recovery_time
            ):

                self.states[
                    track_id
                ] = self.RECOVERING

                return self.RECOVERING

        self.recovery_since.pop(
            track_id,
            None,
        )

        self.states[
            track_id
        ] = self.NORMAL

        return self.NORMAL

    # ======================================================
    # STATIONARY DURATION
    # ======================================================

    def _stationary_duration(
        self,
        track_id,
        timestamp,
    ):

        stationary_since = (
            self.stationary_since.get(
                track_id
            )
        )

        if stationary_since is None:

            return 0.0

        return max(
            0.0,
            timestamp
            - stationary_since,
        )

    # ======================================================
    # CAMERA STATE
    # ======================================================

    def _update_camera_state(
        self,
    ):

        self.active_jam_track_ids = {

            track_id

            for (
                track_id,
                state,
            )

            in self.states.items()

            if state == self.JAM
        }

        states = set(
            self.states.values()
        )

        # Highest severity wins.

        if self.JAM in states:

            self.camera_state = (
                self.JAM
            )

        elif self.WARNING in states:

            self.camera_state = (
                self.WARNING
            )

        elif self.RECOVERING in states:

            self.camera_state = (
                self.RECOVERING
            )

        elif self.SLOW in states:

            self.camera_state = (
                self.SLOW
            )

        else:

            self.camera_state = (
                self.NORMAL
            )

    # ======================================================
    # ROI
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

        # Invalid / zero-sized ROI.
        if (
            x1 == x2
            or
            y1 == y2
        ):

            return False

        return (
            x1 <= x <= x2
            and
            y1 <= y <= y2
        )

    # ======================================================
    # OUTSIDE ROI
    # ======================================================

    def _handle_outside_roi(
        self,
        track_id,
    ):

        # Once the bag leaves the monitoring region,
        # its jam state should not remain active.

        self.history.pop(
            track_id,
            None,
        )

        self.stationary_since.pop(
            track_id,
            None,
        )

        self.recovery_since.pop(
            track_id,
            None,
        )

        self.metrics.pop(
            track_id,
            None,
        )

        self.states.pop(
            track_id,
            None,
        )

    # ======================================================
    # CLEANUP
    # ======================================================

    def _cleanup(
        self,
        timestamp,
    ):

        stale_ids = []

        for (
            track_id,
            last_seen,
        ) in list(
            self.last_seen.items()
        ):

            if (
                timestamp
                - last_seen
                >
                self.track_ttl_seconds
            ):

                stale_ids.append(
                    track_id
                )

        for track_id in stale_ids:

            self.history.pop(
                track_id,
                None,
            )

            self.states.pop(
                track_id,
                None,
            )

            self.stationary_since.pop(
                track_id,
                None,
            )

            self.recovery_since.pop(
                track_id,
                None,
            )

            self.last_seen.pop(
                track_id,
                None,
            )

            self.metrics.pop(
                track_id,
                None,
            )

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
                self.NORMAL,

            "jam_detected":
                False,

            "warning":
                False,

            "active_jam_track_ids":
                [],

            "active_jam_count":
                0,

            "tracks":
                [],
        }

    # ======================================================
    # RESET
    # ======================================================

    def reset(
        self,
    ):
        """
        Clear all runtime jam-detection memory.
        """

        self.history.clear()

        self.states.clear()

        self.stationary_since.clear()

        self.recovery_since.clear()

        self.last_seen.clear()

        self.metrics.clear()

        self.active_jam_track_ids.clear()

        self.camera_state = (
            self.NORMAL
        )

        self.last_result = (
            self._empty_result()
        )