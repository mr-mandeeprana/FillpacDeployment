"""
Entry ROI Counter

Counts physical bags when the BAG CENTER enters the configured
rectangular ROI.

Counting is based ONLY on:

- Bag physical center
- Track stability
- Center inside ROI
- One count per Track ID

Bounding-box edges are NOT used.

A bag is counted once:

    NEW TRACK
        ↓
    STABLE TRACK
        ↓
    BAG CENTER INSIDE ROI
        ↓
      COUNT +1

Important:
- Track IDs are used only to prevent repeated counting of the
  same tracked bag.
- Physical entry distance is NOT used for duplicate rejection.
- This allows two bags that enter very close together to both count.
"""

class EntryROICounter:

    def __init__(
        self,
        roi,
        min_track_frames=4,
        max_history=300,
        duplicate_distance=60,
    ):
        if not isinstance(roi, dict):
            raise ValueError(
                "Entry ROI must be a dictionary containing "
                "x1, y1, x2, y2."
            )

        # ==================================================
        # ROI
        # ==================================================

        self.x1 = float(roi.get("x1", 0))
        self.y1 = float(roi.get("y1", 0))
        self.x2 = float(roi.get("x2", 0))
        self.y2 = float(roi.get("y2", 0))

        if self.x1 == self.x2 or self.y1 == self.y2:
            raise ValueError(
                "Entry ROI must be a rectangle with "
                "non-zero width and height."
            )

        self.x_min = min(self.x1, self.x2)
        self.x_max = max(self.x1, self.x2)

        self.y_min = min(self.y1, self.y2)
        self.y_max = max(self.y1, self.y2)

        # ==================================================
        # SETTINGS
        # ==================================================

        self.min_track_frames = max(
            int(min_track_frames),
            1,
        )

        self.max_history = max(
            int(max_history),
            1,
        )

        # Kept only for compatibility with existing code/config.
        #
        # IMPORTANT:
        # This value is NOT used to reject a new bag.
        #
        # Two different bags can enter at almost exactly
        # the same physical location.
        self.duplicate_distance = max(
            float(duplicate_distance),
            0.0,
        )

        # ==================================================
        # TOTAL COUNT
        # ==================================================

        self.total_count = 0

        # ==================================================
        # TRACK STATE
        # ==================================================

        # Number of frames each track has been observed
        self.track_frame_count = {}

        # Latest center for every track
        self.track_centers = {}

        # Whether the center was inside ROI in the
        # previous processed frame
        self.track_inside_roi = {}

        # Track IDs that have already been counted
        self.counted_track_ids = set()

        # ==================================================
        # EVENT HISTORY
        # ==================================================

        self.recent_entries = []

        # ==================================================
        # CURRENT FRAME EVENTS
        # ==================================================

        self.last_counted_bags = []

    # ======================================================
    # CENTER
    # ======================================================

    @staticmethod
    def get_center(bbox):
        """
        Calculate physical center of bounding box.

        bbox:
            [x1, y1, x2, y2]
        """

        if bbox is None or len(bbox) != 4:
            return None

        x1, y1, x2, y2 = bbox

        return (
            int((x1 + x2) / 2),
            int((y1 + y2) / 2),
        )

    # ======================================================
    # CENTER INSIDE ROI
    # ======================================================

    def _center_inside_roi(self, center):
        """
        Return True when BAG CENTER is inside ROI.
        """

        if center is None:
            return False

        x, y = center

        return (
            self.x_min <= x <= self.x_max
            and
            self.y_min <= y <= self.y_max
        )

    # ======================================================
    # UPDATE
    # ======================================================

    def update(self, tracks):
        """
        Update counter using current frame tracks.

        Expected track format:

        {
            "track_id": 7,
            "class_id": 0,
            "bbox": [x1, y1, x2, y2]
        }

        Returns:
            total_count
        """

        # Reset current-frame events
        self.last_counted_bags = []

        active_track_ids = set()

        # Safety
        if tracks is None:
            tracks = []

        # ==================================================
        # PROCESS TRACKS
        # ==================================================

        for track in tracks:

            if not isinstance(track, dict):
                continue

            # --------------------------------------------------
            # ONLY BAG CLASS
            # --------------------------------------------------

            class_id = track.get("class_id")

            if class_id != 0:
                continue

            # --------------------------------------------------
            # TRACK ID
            # --------------------------------------------------

            track_id = track.get("track_id")

            if track_id is None:
                continue

            active_track_ids.add(track_id)

            # --------------------------------------------------
            # BBOX
            # --------------------------------------------------

            bbox = track.get("bbox")

            if bbox is None:
                continue

            # --------------------------------------------------
            # PHYSICAL CENTER
            # --------------------------------------------------

            center = self.get_center(bbox)

            if center is None:
                continue

            # --------------------------------------------------
            # TRACK FRAME COUNT
            # --------------------------------------------------

            previous_frame_count = (
                self.track_frame_count.get(
                    track_id,
                    0,
                )
            )

            current_frame_count = (
                previous_frame_count + 1
            )

            self.track_frame_count[track_id] = (
                current_frame_count
            )

            # --------------------------------------------------
            # CURRENT ROI STATE
            # --------------------------------------------------

            current_inside = (
                self._center_inside_roi(center)
            )

            # --------------------------------------------------
            # SAVE CURRENT STATE
            # --------------------------------------------------

            self.track_centers[track_id] = center

            self.track_inside_roi[track_id] = (
                current_inside
            )

            # ==================================================
            # STABILITY CHECK
            # ==================================================

            if (
                current_frame_count
                < self.min_track_frames
            ):
                continue

            # ==================================================
            # ALREADY COUNTED
            # ==================================================

            if track_id in self.counted_track_ids:
                continue

            # ==================================================
            # BAG CENTER MUST BE INSIDE ROI
            # ==================================================

            if not current_inside:
                continue

            # ==================================================
            # COUNT
            # ==================================================

            self.total_count += 1

            # Mark this TRACK ID as counted.
            #
            # This is the duplicate protection.
            #
            # Same Track ID staying inside ROI:
            #     NO additional count.
            #
            # Different Track ID:
            #     Can count independently.
            self.counted_track_ids.add(
                track_id
            )

            # --------------------------------------------------
            # EVENT
            # --------------------------------------------------

            count_event = {
                "track_id": track_id,
                "bbox": bbox,
                "center": center,
            }

            self.last_counted_bags.append(
                count_event
            )

            self.recent_entries.append(
                {
                    "track_id": track_id,
                    "center": center,
                }
            )

            # --------------------------------------------------
            # DEBUG LOG
            # --------------------------------------------------

            print(
                f"[ENTRY ROI COUNT +1] "
                f"Track ID={track_id} "
                f"Center={center} "
                f"TOTAL={self.total_count}"
            )

        # ==================================================
        # LIMIT EVENT HISTORY
        # ==================================================

        if (
            len(self.recent_entries)
            > self.max_history
        ):
            self.recent_entries = (
                self.recent_entries[
                    -self.max_history:
                ]
            )

        # ==================================================
        # CLEANUP
        # ==================================================

        self._cleanup(
            active_track_ids
        )

        return self.total_count

    # ======================================================
    # CLEANUP
    # ======================================================

    def _cleanup(self, active_track_ids):
        """
        Keep memory bounded.

        IMPORTANT:
        We do not remove currently active tracks.
        """

        # Nothing to clean
        if len(self.track_centers) <= self.max_history:
            return

        keep_ids = set(active_track_ids)

        # --------------------------------------------------
        # OLD TRACK IDs
        # --------------------------------------------------

        old_ids = [
            track_id
            for track_id in self.track_centers
            if track_id not in keep_ids
        ]

        # Keep only the newest old IDs
        old_ids = old_ids[
            -self.max_history:
        ]

        keep_ids.update(old_ids)

        # --------------------------------------------------
        # CENTER HISTORY
        # --------------------------------------------------

        self.track_centers = {
            track_id: center
            for track_id, center
            in self.track_centers.items()
            if track_id in keep_ids
        }

        # --------------------------------------------------
        # FRAME COUNTS
        # --------------------------------------------------

        self.track_frame_count = {
            track_id: count
            for track_id, count
            in self.track_frame_count.items()
            if track_id in keep_ids
        }

        # --------------------------------------------------
        # ROI STATE
        # --------------------------------------------------

        self.track_inside_roi = {
            track_id: inside
            for track_id, inside
            in self.track_inside_roi.items()
            if track_id in keep_ids
        }

        # --------------------------------------------------
        # COUNTED TRACK IDs
        # --------------------------------------------------

        self.counted_track_ids = {
            track_id
            for track_id in self.counted_track_ids
            if track_id in keep_ids
        }

    # ======================================================
    # RESET
    # ======================================================

    def reset(self):
        """
        Reset counter completely.
        """

        self.total_count = 0

        self.track_frame_count.clear()

        self.track_centers.clear()

        self.track_inside_roi.clear()

        self.counted_track_ids.clear()

        self.recent_entries.clear()

        self.last_counted_bags = []

    # ======================================================
    # ROI PROPERTY
    # ======================================================

    @property
    def roi(self):
        """
        Return ROI in integer coordinates.
        """

        return {
            "x1": int(self.x1),
            "y1": int(self.y1),
            "x2": int(self.x2),
            "y2": int(self.y2),
        }

    # ======================================================
    # STATUS
    # ======================================================

    @property
    def counted_count(self):
        """
        Alias for total_count.
        """

        return self.total_count

    # ======================================================
    # DEBUG INFORMATION
    # ======================================================

    def get_debug_state(self):
        """
        Return useful diagnostic information.
        """

        return {
            "total_count": self.total_count,
            "active_tracks": len(
                self.track_centers
            ),
            "counted_tracks": len(
                self.counted_track_ids
            ),
            "roi": self.roi,
            "min_track_frames": (
                self.min_track_frames
            ),
            "last_counted_bags": (
                self.last_counted_bags
            ),
        }