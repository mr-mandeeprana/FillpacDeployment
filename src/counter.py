"""
==========================================================
FillPac AI
Physical Bag Center Counter
==========================================================

Purpose
-------
Counts physical bags crossing a configured counting line.
The line may be horizontal or vertical -- orientation is
inferred from the supplied roi (x1, y1, x2, y2):

    abs(x2 - x1) < abs(y2 - y1)  -> vertical line   (x1 == x2)
    otherwise                    -> horizontal line  (y1 == y2)

Counting is based on:
- Bag bounding-box center (physical center crossing only)
- Crossing direction
- Track stability
- Minimum crossing distance
- Spatial duplicate suppression
- Time-based duplicate suppression

Important
---------
Track IDs are used only to maintain movement history.

The actual count event is based ONLY on the physical bag
center crossing the counting line. Bounding-box edges are
NOT used for counting.

If ByteTrack changes the track ID near the counting line,
duplicate_distance + duplicate_time help prevent the same
physical bag from being counted twice.
==========================================================
"""

import math
import time


class Counter:

    # Directions that mean "crossing toward decreasing coordinate"
    # (toward smaller y for a horizontal line, smaller x for a vertical one).
    DECREASING_DIRECTIONS = {"up", "left"}

    # Directions that mean "crossing toward increasing coordinate".
    INCREASING_DIRECTIONS = {"down", "right"}

    VALID_DIRECTIONS = {"up", "down", "left", "right", "both"}

    def __init__(
        self,
        roi=None,
        direction="down",
        duplicate_distance=40,
        duplicate_time=0.8,
        max_history=200,
        line_tolerance=20,
        late_start_margin=40,
        min_track_frames=4,
        stale_track_frames=120,
        minimum_cross_distance=0,
        roi_y=None,
        recent_count_frames=24,
    ):

        # ==================================================
        # COUNTING LINE
        # ==================================================

        # ==================================================
        # BACKWARD-COMPATIBLE ROI SUPPORT
        # ==================================================

        if roi is None and roi_y is not None:

            # Legacy/simple horizontal counting line.
            # Preserve the existing physical-center logic.
            roi = {
                "x1": 0,
                "y1": roi_y,
                "x2": 1920,
                "y2": roi_y,
            }

        elif not isinstance(roi, dict):

            raise ValueError(
                "Counter roi must be a dictionary containing "
                "x1, y1, x2, y2."
            )

        self.line_x1 = float(roi.get("x1", 0))
        self.line_y1 = float(roi.get("y1", 0))
        self.line_x2 = float(roi.get("x2", 0))
        self.line_y2 = float(roi.get("y2", 0))

        self.line_is_vertical = abs(self.line_x2 - self.line_x1) < abs(
            self.line_y2 - self.line_y1
        )

        # The coordinate the line sits at: an x value for a vertical
        # line, a y value for a horizontal one.
        self.line_position = self.line_x1 if self.line_is_vertical else self.line_y1

        self.direction = str(direction).lower()

        if self.direction not in self.VALID_DIRECTIONS:
            raise ValueError(
                "Counter direction must be one of: "
                + ", ".join(sorted(self.VALID_DIRECTIONS))
            )

        # Catch the exact bug this class was rewritten for: an
        # orientation/direction mismatch that silently produces
        # Count: 0 instead of failing loudly.
        if self.line_is_vertical and self.direction in {"up", "down"}:
            raise ValueError(
                "Counting line is vertical (x1 == x2) but direction is "
                f"'{self.direction}'. A vertical line needs 'left', "
                "'right', or 'both'."
            )

        if not self.line_is_vertical and self.direction in {"left", "right"}:
            raise ValueError(
                "Counting line is horizontal (y1 == y2) but direction is "
                f"'{self.direction}'. A horizontal line needs 'up', "
                "'down', or 'both'."
            )

        # ==================================================
        # DUPLICATE PROTECTION
        # ==================================================

        self.duplicate_distance = max(float(duplicate_distance), 0.0)

        self.duplicate_time = max(float(duplicate_time), 0.0)

        # Frame-based window used by _can_count() for duplicate
        # suppression (see notes on recent_counts below). Kept
        # alongside duplicate_time for entries written before this
        # field existed.
        self.recent_count_frames = max(int(recent_count_frames), 0)

        # ==================================================
        # TRACK HISTORY CONFIGURATION
        # ==================================================

        self.max_history = max(int(max_history), 1)

        self.line_tolerance = max(int(line_tolerance), 0)

        self.late_start_margin = max(int(late_start_margin), 0)

        self.min_track_frames = max(int(min_track_frames), 1)

        self.stale_track_frames = max(int(stale_track_frames), 1)

        self.minimum_cross_distance = max(int(minimum_cross_distance), 0)

        # ==================================================
        # TOTAL COUNT
        # ==================================================

        self.total_count = 0

        # ==================================================
        # CURRENT FRAME COUNT EVENTS
        # ==================================================

        self.last_count_center = None

        self.last_counted_bags = []

        # ==================================================
        # TRACK STATE
        # ==================================================

        self.track_centers = {}

        self.track_previous_centers = {}

        self.track_zones = {}

        self.track_start_zones = {}

        self.track_start_centers = {}

        self.track_states = {}

        self.track_frame_count = {}

        self.track_last_seen = {}

        # Prevent the same active Track ID
        # from being counted more than once.
        self.counted_track_ids = set()

        # ==================================================
        # PHYSICAL BAG DUPLICATE HISTORY
        #
        # Each item:
        #
        # {
        #     "center": (x, y),
        #     "timestamp": monotonic_time,
        #     "track_id": id,
        #     "frame_index": frame_index,
        # }
        #
        # This protects against ByteTrack assigning a new
        # track ID to the same physical bag near the line.
        # ==================================================

        self.recent_counts = []

        # ==================================================
        # FRAME INDEX
        # ==================================================

        self.frame_index = 0

    # ======================================================
    # GET BOUNDING BOX CENTER
    # ======================================================

    @staticmethod
    def get_center(bbox):

        x1, y1, x2, y2 = bbox

        center_x = int((x1 + x2) / 2)
        center_y = int((y1 + y2) / 2)

        return (center_x, center_y)

    # ======================================================
    # AXIS HELPERS
    #
    # These are the only place orientation is resolved -- every
    # other method below works purely in terms of "the coordinate
    # along the line's crossing axis", so it doesn't matter whether
    # the line is horizontal or vertical.
    # ======================================================

    def _axis_value(self, point):
        """x for a vertical line, y for a horizontal line."""
        return point[0] if self.line_is_vertical else point[1]

    def _bbox_edges(self, bbox):
        """
        Returns (leading_edge, trailing_edge) along the crossing axis,
        where "leading" is whichever edge reaches the line first when
        travelling in a DECREASING direction (up/left), and "trailing"
        is whichever edge reaches the line first when travelling in an
        INCREASING direction (down/right).
        """
        x1, y1, x2, y2 = bbox
        if self.line_is_vertical:
            return x1, x2
        return y1, y2

    # ======================================================
    # UPDATE
    # ======================================================

    def update(self, tracks):

        self.frame_index += 1

        self.last_count_center = None

        self.last_counted_bags = []

        active_track_ids = set()

        # Current monotonic time for duplicate checks.
        current_time = time.monotonic()

        # Remove expired physical count records.
        self._trim_recent_counts(current_time)

        # ==================================================
        # PROCESS TRACKS
        # ==================================================

        for track in tracks:

            # ----------------------------------------------
            # Only count Bag class
            # ----------------------------------------------

            if track.get("class_id") != 0:
                continue

            # ----------------------------------------------
            # Track ID
            # ----------------------------------------------

            track_id = track.get("track_id")

            if track_id is None:
                continue

            active_track_ids.add(track_id)

            # ----------------------------------------------
            # Track frame count
            # ----------------------------------------------

            self.track_frame_count[track_id] = (
                self.track_frame_count.get(track_id, 0) + 1
            )

            # ----------------------------------------------
            # Track last seen
            # ----------------------------------------------

            self.track_last_seen[track_id] = self.frame_index

            # ----------------------------------------------
            # Physical center
            # ----------------------------------------------

            center = self.get_center(track["bbox"])

            # ----------------------------------------------
            # Previous center / zone
            # ----------------------------------------------

            previous_center = self.track_centers.get(track_id)

            previous_zone = self.track_zones.get(track_id)

            current_zone = self._get_zone(center)

            # ----------------------------------------------
            # Track starting state
            # ----------------------------------------------

            self.track_start_zones.setdefault(track_id, current_zone)

            self.track_start_centers.setdefault(track_id, center)

            # ----------------------------------------------
            # Store previous center
            # ----------------------------------------------

            if previous_center is not None:
                self.track_previous_centers[track_id] = previous_center

            # ----------------------------------------------
            # Update center
            # ----------------------------------------------

            self.track_centers[track_id] = center

            # ----------------------------------------------
            # Update zone
            # ----------------------------------------------

            self._update_track_zone(track_id, previous_zone, current_zone)

            # ----------------------------------------------
            # Count decision
            # ----------------------------------------------

            if self._should_count(
                track_id=track_id,
                bbox=track["bbox"],
                center=center,
                previous_center=previous_center,
                previous_zone=previous_zone,
                current_zone=current_zone,
                current_time=current_time,
            ):

                self._register_count(
                    track_id=track_id,
                    bbox=track["bbox"],
                    center=center,
                    timestamp=current_time,
                )

        # ==================================================
        # CLEAN OLD TRACK HISTORY
        # ==================================================

        self._trim_history(active_track_ids)

        return self.total_count

    # ======================================================
    # REGISTER COUNT
    # ======================================================

    def _register_count(self, track_id, bbox, center, timestamp):

        self.total_count += 1

        self.counted_track_ids.add(track_id)

        self.last_count_center = center

        self.last_counted_bags.append(
            {
                "track_id": track_id,
                "bbox": bbox,
                "center": center,
            }
        )

        self.track_states[track_id] = "COUNTED"

        self.recent_counts.append(
            {
                "center": center,
                "timestamp": timestamp,
                "track_id": track_id,
                "frame_index": self.frame_index,
            }
        )

    # ======================================================
    # SHOULD COUNT
    #
    # ROBUST PHYSICAL-CENTER COUNTING
    # --------------------------------
    # Instead of only checking whether the center crossed the
    # line between the immediately previous frame and this one,
    # this asks: "has this bag's center been observed on the
    # BEFORE side, and has it now reached the AFTER side?" The
    # track's BEFORE_LINE state persists across frames (even
    # while the center sits inside the tolerance zone), which
    # is more robust to noisy per-frame center jitter than a
    # strict previous-vs-current comparison.
    #
    # Counting is based ONLY on the physical center.
    # Bounding-box edges are NOT used for the count decision.
    # ======================================================

    def _should_count(
        self,
        track_id,
        bbox,
        center,
        previous_center,
        previous_zone,
        current_zone,
        current_time,
    ):

        # --------------------------------------------------
        # 1. TRACK STABILITY
        # --------------------------------------------------

        if not self._is_stable(track_id):
            self._advance_state(track_id, "BEFORE_LINE")
            return False

        # --------------------------------------------------
        # 2. DUPLICATE PROTECTION
        # --------------------------------------------------

        if not self._can_count(track_id, center, current_time):
            return False

        # --------------------------------------------------
        # 3. CURRENT POSITION
        # --------------------------------------------------

        axis = self._axis_value(center)
        line = self.line_position
        tolerance = self.line_tolerance

        # --------------------------------------------------
        # 4. REMEMBER THAT THE BAG WAS BEFORE THE LINE
        # --------------------------------------------------

        if self.direction in self.DECREASING_DIRECTIONS:
            # left / up
            if axis > line + tolerance:
                self._advance_state(track_id, "BEFORE_LINE")

        elif self.direction in self.INCREASING_DIRECTIONS:
            # right / down
            if axis < line - tolerance:
                self._advance_state(track_id, "BEFORE_LINE")

        # --------------------------------------------------
        # 5. CHECK MOVEMENT
        # --------------------------------------------------

        if previous_center is None:
            return False

        delta = self._axis_value(center) - self._axis_value(previous_center)

        if self.direction in self.DECREASING_DIRECTIONS:
            if delta >= 0:
                return False

        elif self.direction in self.INCREASING_DIRECTIONS:
            if delta <= 0:
                return False

        # --------------------------------------------------
        # 6. CENTER HAS REACHED OTHER SIDE
        # --------------------------------------------------

        crossed = False

        if self.direction in self.DECREASING_DIRECTIONS:
            crossed = (
                self.track_states.get(track_id) == "BEFORE_LINE"
                and axis <= line
            )

        elif self.direction in self.INCREASING_DIRECTIONS:
            crossed = (
                self.track_states.get(track_id) == "BEFORE_LINE"
                and axis >= line
            )

        # BOTH
        elif self.direction == "both":
            crossed = (
                self.track_states.get(track_id) == "BEFORE_LINE"
                and abs(axis - line) <= tolerance
            )

        # --------------------------------------------------
        # 7. PENDING CENTER CROSSING
        #
        # Do NOT count immediately when the center first
        # reaches/crosses the line.
        #
        # First mark the crossing as pending, then require
        # minimum_cross_distance before confirming the count.
        # --------------------------------------------------

        if crossed:

            # No additional distance requirement.
            if self.minimum_cross_distance <= 0:

                self._advance_state(
                    track_id,
                    "COUNTED",
                )

                return True

            # Crossing detected, but count is pending.
            self._advance_state(
                track_id,
                "CENTER_CROSSED",
            )

            # Check whether enough physical center movement
            # has already occurred.
            if self._has_minimum_cross_distance(
                center
            ):

                self._advance_state(
                    track_id,
                    "COUNTED",
                )

                return True

            return False

        # --------------------------------------------------
        # 8. CONFIRM PENDING CENTER CROSSING
        # --------------------------------------------------

        if (
            self.track_states.get(track_id)
            == "CENTER_CROSSED"
        ):

            if self._has_minimum_cross_distance(
                center
            ):

                self._advance_state(
                    track_id,
                    "COUNTED",
                )

                return True

        return False

    # ======================================================
    # TRACK STABILITY
    # ======================================================

    def _is_stable(self, track_id):
        return self.track_frame_count.get(track_id, 0) >= self.min_track_frames

    # ======================================================
    # CAN COUNT
    # ======================================================

    def _can_count(
        self,
        track_id,
        center,
        current_time,
    ):

        if track_id in self.counted_track_ids:
            return False

        # ----------------------------------------------
        # Physical duplicate detection
        #
        # A nearby physical center is considered a
        # duplicate only while it remains inside the
        # configured recent-count frame window.
        #
        # This is important when ByteTrack changes the
        # track ID of the same physical bag.
        # ----------------------------------------------

        current_frame = getattr(
            self,
            "frame_index",
            0,
        )

        for item in self.recent_counts:

            # ------------------------------------------
            # Frame-based duplicate window
            # ------------------------------------------

            counted_frame = item.get(
                "frame_index"
            )

            if counted_frame is not None:

                frame_age = (
                    current_frame
                    - counted_frame
                )

                if (
                    frame_age
                    > self.recent_count_frames
                ):
                    continue

            else:
                # --------------------------------------
                # Backward compatibility for entries
                # without frame_index.
                # --------------------------------------

                elapsed_time = (
                    current_time
                    - item["timestamp"]
                )

                if (
                    elapsed_time
                    > self.duplicate_time
                ):
                    continue

            previous_center = item["center"]

            distance = math.hypot(
                previous_center[0] - center[0],
                previous_center[1] - center[1],
            )

            if (
                distance
                <= self.duplicate_distance
            ):
                return False

        return True

    # ======================================================
    # ADVANCE TRACK STATE
    # ======================================================

    def _advance_state(self, track_id, state):

        if self.track_states.get(track_id) == "COUNTED":
            return

        state_rank = {
            "NEW": 0,
            "BEFORE_LINE": 1,
            "ENTERING_ROI": 2,
            "CENTER_CROSSED": 3,
            "COUNTED": 4,
        }

        current_state = self.track_states.get(track_id, "NEW")

        if state_rank[state] >= state_rank[current_state]:
            self.track_states[track_id] = state

    # ======================================================
    # MOVEMENT DIRECTION
    # ======================================================

    def _is_moving_in_count_direction(self, previous_center, center):

        if previous_center is None:
            return False

        delta = self._axis_value(center) - self._axis_value(previous_center)

        if self.direction in self.DECREASING_DIRECTIONS:
            return delta < 0

        if self.direction in self.INCREASING_DIRECTIONS:
            return delta > 0

        # both
        return delta != 0

    # ======================================================
    # CENTER CROSSED ROI
    # ======================================================

    def _center_crossed_roi(self, previous_center, center):

        if previous_center is None:
            return False

        previous_value = self._axis_value(previous_center)
        current_value = self._axis_value(center)
        line = self.line_position

        if self.direction in self.DECREASING_DIRECTIONS:
            return previous_value > line and current_value <= line

        if self.direction in self.INCREASING_DIRECTIONS:
            return previous_value < line and current_value >= line

        # both
        return (previous_value > line and current_value <= line) or (
            previous_value < line and current_value >= line
        )

    # ======================================================
    # BBOX REACHED ROI
    #
    # "leading_edge" is whichever bbox edge would touch the line
    # first for a DECREASING crossing (up/left); "trailing_edge"
    # is whichever edge touches first for an INCREASING crossing
    # (down/right).
    #
    # No longer used by _should_count() (counting is now
    # center-only), kept for callers that may still want to
    # inspect bbox/line proximity (e.g. debugging/visualization).
    # ======================================================

    def _bbox_reached_roi(self, previous_zone, bbox):

        if previous_zone is None:
            return False

        leading_edge, trailing_edge = self._bbox_edges(bbox)
        line = self.line_position
        tol = self.line_tolerance

        if self.direction in self.DECREASING_DIRECTIONS:
            return previous_zone == "high" and leading_edge <= (line + tol)

        if self.direction in self.INCREASING_DIRECTIONS:
            return previous_zone == "low" and trailing_edge >= (line - tol)

        # both
        return (previous_zone == "low" and trailing_edge >= (line - tol)) or (
            previous_zone == "high" and leading_edge <= (line + tol)
        )

    # ======================================================
    # BBOX PAST ROI
    #
    # Still used by late-start recovery, where bbox position
    # gives a useful sanity check for tracks that start very
    # close to the line.
    # ======================================================

    def _bbox_past_roi(self, bbox):

        leading_edge, trailing_edge = self._bbox_edges(bbox)
        line = self.line_position
        tol = self.line_tolerance

        if self.direction in self.DECREASING_DIRECTIONS:
            return leading_edge <= (line + tol)

        if self.direction in self.INCREASING_DIRECTIONS:
            return trailing_edge >= (line - tol)

        # both
        return trailing_edge >= (line - tol) or leading_edge <= (line + tol)

    # ======================================================
    # GET ZONE
    #
    # "low" = below the line's position (smaller y, or smaller x for
    # a vertical line); "high" = the opposite side; "line" = within
    # tolerance of the line itself.
    # ======================================================

    def _get_zone(self, point):

        value = self._axis_value(point)
        line = self.line_position
        tol = self.line_tolerance

        if value < (line - tol):
            return "low"

        if value > (line + tol):
            return "high"

        return "line"

    # ======================================================
    # VALID LATE START
    # ======================================================

    def _is_valid_late_start(self, track_id, bbox, center, current_zone):

        start_zone = self.track_start_zones.get(track_id)

        start_center = self.track_start_centers.get(track_id, center)
        start_value = self._axis_value(start_center)
        line = self.line_position

        if self.direction in self.DECREASING_DIRECTIONS:
            started_after_line = start_zone == "low" or start_value <= line

        elif self.direction in self.INCREASING_DIRECTIONS:
            started_after_line = start_zone == "high" or start_value >= line

        else:
            # both
            started_after_line = start_zone in {"low", "high"}

        return (
            started_after_line
            and self._bbox_past_roi(bbox)
            and self._center_past_roi(center, current_zone)
            and self._inside_late_start_margin(center, current_zone)
        )

    # ======================================================
    # CENTER PAST ROI
    # ======================================================

    def _center_past_roi(self, center, current_zone):

        value = self._axis_value(center)
        line = self.line_position

        if self.direction in self.DECREASING_DIRECTIONS:
            return current_zone == "low" or value <= line

        if self.direction in self.INCREASING_DIRECTIONS:
            return current_zone == "high" or value >= line

        # both
        return current_zone != "line"

    # ======================================================
    # CONFIRMED PENDING CROSSING
    #
    # PHYSICAL-CENTER VERSION: no longer requires the bbox to
    # have passed the line -- only that this track already
    # registered a CENTER_CROSSED state and its center is past
    # the line by at least the minimum crossing distance.
    # ======================================================

    def _is_confirmed_pending_crossing(self, track_id, center, current_zone):

        return (
            self.track_states.get(track_id) == "CENTER_CROSSED"
            and self._center_past_roi(center, current_zone)
            and self._has_minimum_cross_distance(center)
        )

    # ======================================================
    # MINIMUM CROSS DISTANCE
    # ======================================================

    def _has_minimum_cross_distance(self, center):

        if self.minimum_cross_distance <= 0:
            return True

        value = self._axis_value(center)
        line = self.line_position

        if self.direction in self.DECREASING_DIRECTIONS:
            return (line - value) >= self.minimum_cross_distance

        if self.direction in self.INCREASING_DIRECTIONS:
            return (value - line) >= self.minimum_cross_distance

        # both
        return abs(value - line) >= self.minimum_cross_distance

    # ======================================================
    # LATE START MARGIN
    # ======================================================

    def _inside_late_start_margin(self, center, current_zone):

        value = self._axis_value(center)
        line = self.line_position
        tol = self.line_tolerance
        margin = self.late_start_margin

        if self.direction in self.DECREASING_DIRECTIONS:
            return current_zone == "low" and value >= (line - tol - margin)

        if self.direction in self.INCREASING_DIRECTIONS:
            return current_zone == "high" and value <= (line + tol + margin)

        # both
        return current_zone != "line" and abs(value - line) <= (tol + margin)

    # ======================================================
    # UPDATE TRACK ZONE
    # ======================================================

    def _update_track_zone(self, track_id, previous_zone, current_zone):

        if current_zone == "line":

            if previous_zone is None:
                if self.direction in self.DECREASING_DIRECTIONS:
                    self.track_zones[track_id] = "high"
                else:
                    self.track_zones[track_id] = "low"
            else:
                self.track_zones[track_id] = previous_zone

            return

        self.track_zones[track_id] = current_zone

    # ======================================================
    # TRIM RECENT PHYSICAL COUNTS
    # ======================================================

    def _trim_recent_counts(self, current_time):

        if self.duplicate_time <= 0 and self.recent_count_frames <= 0:
            self.recent_counts = []
            return

        self.recent_counts = [
            item
            for item in self.recent_counts
            if (
                (self.frame_index - item.get("frame_index", self.frame_index))
                <= self.recent_count_frames
            )
            or ((current_time - item["timestamp"]) <= self.duplicate_time)
        ]

    # ======================================================
    # TRIM TRACK HISTORY
    # ======================================================

    def _trim_history(self, active_track_ids):

        fresh_ids = {
            track_id
            for track_id, last_seen in self.track_last_seen.items()
            if (self.frame_index - last_seen) <= self.stale_track_frames
        }

        recent_ids = sorted(
            fresh_ids,
            key=lambda track_id: self.track_last_seen.get(track_id, 0),
            reverse=True,
        )

        retained_ids = []

        for track_id in list(active_track_ids) + recent_ids:

            if track_id not in retained_ids:
                retained_ids.append(track_id)

            if len(retained_ids) >= self.max_history:
                break

        retained_set = set(retained_ids)

        self.track_centers = {
            track_id: center
            for track_id, center in self.track_centers.items()
            if track_id in retained_set
        }

        self.track_previous_centers = {
            track_id: center
            for track_id, center in self.track_previous_centers.items()
            if track_id in retained_set
        }

        self.track_zones = {
            track_id: zone
            for track_id, zone in self.track_zones.items()
            if track_id in retained_set
        }

        self.track_start_zones = {
            track_id: zone
            for track_id, zone in self.track_start_zones.items()
            if track_id in retained_set
        }

        self.track_start_centers = {
            track_id: center
            for track_id, center in self.track_start_centers.items()
            if track_id in retained_set
        }

        self.counted_track_ids = {
            track_id for track_id in self.counted_track_ids if track_id in retained_set
        }

        self.track_states = {
            track_id: state
            for track_id, state in self.track_states.items()
            if track_id in retained_set
        }

        self.track_frame_count = {
            track_id: frame_count
            for track_id, frame_count in self.track_frame_count.items()
            if track_id in retained_set
        }

        self.track_last_seen = {
            track_id: frame_index
            for track_id, frame_index in self.track_last_seen.items()
            if track_id in retained_set
        }

        # --------------------------------------------------
        # CLEAN PER-TRACK DUPLICATE TIME HISTORY
        #
        # duplicate_time is normally a single numeric config value
        # (see _can_count()'s "elapsed_time > self.duplicate_time"),
        # so this only applies when it's been set up as a per-track
        # dict (e.g. by a test harness treating it as track state).
        # --------------------------------------------------
        if isinstance(self.duplicate_time, dict):
            self.duplicate_time = {
                track_id: value
                for track_id, value in self.duplicate_time.items()
                if track_id in retained_set
            }

        # --------------------------------------------------
        # CLEAN STALE RECENT COUNT EVENTS
        # --------------------------------------------------
        if self.recent_count_frames > 0:
            self.recent_counts = [
                item
                for item in self.recent_counts
                if (
                    self.frame_index
                    - item.get(
                        "frame",
                        item.get(
                            "frame_index",
                            self.frame_index,
                        ),
                    )
                ) <= self.recent_count_frames
            ]
        else:
            self.recent_counts = []