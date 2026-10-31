"""
==========================================================
FillPac AI
Print ROI Counter
==========================================================

Counts PRINTS separately from the bag entry-ROI counter.

No voting. A print is counted the moment it is confirmed:

    PrintDetector matched a print to a bag track
        -> print confidence >= min_confidence
        -> print CENTER inside the print ROI (if configured)
        -> seen on `min_frames` CONSECUTIVE frames
        -> track not already counted
        => print_roi_count += 1

One count per bag track ID, so a print visible for many
frames is still counted once. Bags that never get a counted
print are reported as missing by the Pipeline when they leave.
==========================================================
"""


class PrintROICounter:

    def __init__(
        self,
        roi=None,
        min_frames=2,
        min_confidence=0.0,
        max_history=300,
    ):
        # roi=None / empty -> whole frame is the print ROI
        self.has_roi = False
        self.x_min = self.y_min = self.x_max = self.y_max = 0.0

        if isinstance(roi, dict) and {"x1", "y1", "x2", "y2"} <= set(roi):
            x1, y1 = float(roi["x1"]), float(roi["y1"])
            x2, y2 = float(roi["x2"]), float(roi["y2"])

            if x1 == x2 or y1 == y2:
                raise ValueError(
                    "Print ROI must have non-zero width and height."
                )

            self.x_min, self.x_max = min(x1, x2), max(x1, x2)
            self.y_min, self.y_max = min(y1, y2), max(y1, y2)
            self.has_roi = True

        self.min_frames = max(int(min_frames), 1)
        self.min_confidence = min(max(float(min_confidence), 0.0), 1.0)
        self.max_history = max(int(max_history), 1)

        self.total_count = 0

        # track_id -> consecutive qualifying frames
        self._streaks = {}

        # Insertion-ordered so the oldest IDs are pruned first.
        self.counted_track_ids = {}

        # Prints counted in the CURRENT frame.
        self.last_counted_prints = []

    # ------------------------------------------------------

    @staticmethod
    def _center(bbox):
        x1, y1, x2, y2 = bbox
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    def _inside_roi(self, center):
        if not self.has_roi:
            return True
        x, y = center
        return (
            self.x_min <= x <= self.x_max
            and self.y_min <= y <= self.y_max
        )

    # ------------------------------------------------------

    def update(self, print_results):
        """
        print_results: output of PrintDetector.update()
        (only stable tracks should be passed in).

        Returns total_count.
        """

        self.last_counted_prints = []
        new_streaks = {}

        for result in print_results or []:

            track_id = result.get("track_id")

            if track_id is None or not result.get("print_present"):
                continue

            bbox = result.get("print_bbox")

            if bbox is None:
                continue

            confidence = float(result.get("print_confidence") or 0.0)

            if confidence < self.min_confidence:
                continue

            center = self._center(bbox)

            if not self._inside_roi(center):
                continue

            # Consecutive-frame confirmation (not a vote ratio):
            # any frame without a qualifying print resets it.
            streak = self._streaks.get(track_id, 0) + 1
            new_streaks[track_id] = streak

            if track_id in self.counted_track_ids:
                continue

            if streak < self.min_frames:
                continue

            self.total_count += 1
            self.counted_track_ids[track_id] = True

            self.last_counted_prints.append(
                {
                    "track_id": track_id,
                    "print_bbox": bbox,
                    "center": (int(center[0]), int(center[1])),
                    "confidence": round(confidence, 4),
                }
            )

        self._streaks = new_streaks

        while len(self.counted_track_ids) > self.max_history:
            self.counted_track_ids.pop(next(iter(self.counted_track_ids)))

        return self.total_count

    # ------------------------------------------------------

    def has_counted(self, track_id):
        return track_id in self.counted_track_ids

    @property
    def roi(self):
        if not self.has_roi:
            return None
        return {
            "x1": int(self.x_min),
            "y1": int(self.y_min),
            "x2": int(self.x_max),
            "y2": int(self.y_max),
        }

    def reset(self):
        self.total_count = 0
        self._streaks.clear()
        self.counted_track_ids.clear()
        self.last_counted_prints = []
