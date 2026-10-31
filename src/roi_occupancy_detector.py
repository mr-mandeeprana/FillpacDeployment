"""
==========================================================
FillPac AI
ROI Occupancy Jam Detector
Condition C
==========================================================

Condition C declares a jam when the number of bags inside
the configured ROI exceeds max_allowed_bags.

CHANGE LOG
----------
The per-event JSON log file (logs/jam_events/*.json) has
been removed. ROI snapshot images are still written to disk
(SQL Server has no good way to store binary image data), but
the event metadata that used to accompany each image in its
own JSON file should now be persisted by the caller via
database/repository.py -- e.g. start_jam_event(condition_code
="C", ...) and save_roi_snapshot(image_path=..., ...) -- using
the dict returned by process() below, which is unchanged.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import cv2


class ROIOccupancyDetector:

    def __init__(self, config: dict):

        self.config = config or {}

        self.enabled = bool(
            self.config.get(
                "enabled",
                False,
            )
        )

        self.roi = dict(
            self.config.get(
                "roi",
                {},
            )
        )

        self.max_allowed_bags = max(
            int(
                self.config.get(
                    "max_allowed_bags",
                    1,
                )
            ),
            1,
        )

        self.min_track_age = max(
            int(
                self.config.get(
                    "min_track_age",
                    4,
                )
            ),
            1,
        )

        self.ignore_unstable_tracks = bool(
            self.config.get(
                "ignore_unstable_tracks",
                True,
            )
        )

        self.ignore_motion_jumps = bool(
            self.config.get(
                "ignore_motion_jumps",
                True,
            )
        )

        self.capture_cooldown = max(
            float(
                self.config.get(
                    "capture_cooldown_seconds",
                    5,
                )
            ),
            0.0,
        )

        # =================================================
        # STATE DEBOUNCE (HYSTERESIS)
        # =================================================
        #
        # bag_count is a raw per-frame count of tracks whose
        # center currently falls inside the ROI. Right at the
        # occupancy boundary (e.g. a bag entering/leaving the
        # ROI edge, or a single missed detection), this count
        # can flicker by +/-1 for a frame or two, which used to
        # flip `jam` true/false every frame and start+end a
        # jam_events row in SQL Server for each flicker. These
        # confirm-frame counts require the over/under-threshold
        # condition to hold for N consecutive frames before the
        # reported jam state actually changes. Defaults to 1
        # (no debounce, previous behavior).

        self.jam_confirm_frames = max(
            int(
                self.config.get(
                    "jam_confirm_frames",
                    1,
                )
            ),
            1,
        )

        self.recovery_confirm_frames = max(
            int(
                self.config.get(
                    "recovery_confirm_frames",
                    self.jam_confirm_frames,
                )
            ),
            1,
        )

        self._pending_jam_state = None
        self._pending_jam_streak = 0

        self.save_roi_image = bool(
            self.config.get(
                "save_roi_image",
                True,
            )
        )

        # "save_logs" (per-event JSON log file) is deprecated --
        # event metadata now belongs in SQL Server, written by
        # the caller through database/repository.py using the
        # dict returned by process(). The key is still accepted
        # in config.yaml so it doesn't need to be removed, but
        # it no longer does anything.
        self.save_logs = False

        self.save_distances = bool(
            self.config.get(
                "save_distances",
                True,
            )
        )

        self.camera_name = str(
            self.config.get(
                "camera_name",
                "unknown",
            )
        )

        self.image_directory = Path(
            self.config.get(
                "image_directory",
                "logs/condition_c/images",
            )
        )

        self.image_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.last_capture_time = 0.0

        self.last_saved_track_ids = set()

        # =================================================
        # JAM EVENT LIFECYCLE STATE
        # =================================================

        self.previous_jam = False

        self.jam_start_time = None

        self.event_id = 0

    # =====================================================
    # PROCESS
    # =====================================================

    def _debounce_jam_state(
        self,
        raw_jam,
    ):
        """
        Require `jam_confirm_frames` (entering a jam) or
        `recovery_confirm_frames` (recovering) consecutive
        frames of agreement with the raw over/under threshold
        occupancy count before the reported jam state flips.
        See the STATE DEBOUNCE comment in __init__.
        """

        if raw_jam == self.previous_jam:

            self._pending_jam_state = None
            self._pending_jam_streak = 0

            return self.previous_jam

        if self._pending_jam_state == raw_jam:
            self._pending_jam_streak += 1
        else:
            self._pending_jam_state = raw_jam
            self._pending_jam_streak = 1

        required_streak = (
            self.jam_confirm_frames
            if raw_jam
            else self.recovery_confirm_frames
        )

        if self._pending_jam_streak >= required_streak:

            self._pending_jam_state = None
            self._pending_jam_streak = 0

            return raw_jam

        return self.previous_jam

    def process(
        self,
        frame,
        tracks,
        spacing_result=None,
        camera_name=None,
    ):

        empty = {
            "jam": False,
            "status": "disabled" if not self.enabled else "normal",
            "bag_count": 0,
            "track_ids": [],
            "track_centers": [],
            "minimum_gap_mm": None,
            "average_gap_mm": None,
            "distances": [],
            "spacing_threshold_mm": None,
            "roi": dict(self.roi) if self.roi else {},
            "max_allowed_bags": self.max_allowed_bags,
            "occupancy_percent": 0,
            "jam_duration": 0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_id": self.event_id,
            "image_path": None,
        }

        if not self.enabled:
            return empty

        if not self.roi:
            return empty

        try:

            x1 = int(self.roi["x1"])
            y1 = int(self.roi["y1"])
            x2 = int(self.roi["x2"])
            y2 = int(self.roi["y2"])

        except Exception:
            return empty

        roi_tracks = []

        for track in (tracks or []):

            if not isinstance(track, dict):
                continue

            # ------------------------------
            # Track age
            # ------------------------------

            track_age = int(
                track.get(
                    "track_age",
                    0,
                )
                or 0
            )

            if track_age < self.min_track_age:
                continue

            # ------------------------------
            # Ignore unstable tracks
            # ------------------------------

            if (
                self.ignore_unstable_tracks
                and
                track.get(
                    "unstable",
                    False,
                )
            ):
                continue

            # ------------------------------
            # Ignore motion jumps
            # ------------------------------

            if (
                self.ignore_motion_jumps
                and
                track.get(
                    "motion_jump",
                    False,
                )
            ):
                continue

            bbox = track.get("bbox")

            if (
                bbox is None
                or
                len(bbox) != 4
            ):
                continue

            x_min, y_min, x_max, y_max = bbox

            cx = int(
                (x_min + x_max) / 2
            )

            cy = int(
                (y_min + y_max) / 2
            )

            if (
                x1 <= cx <= x2
                and
                y1 <= cy <= y2
            ):
                roi_tracks.append(track)

        bag_count = len(
            roi_tracks
        )

        raw_jam = (
            bag_count
            >
            self.max_allowed_bags
        )

        jam = self._debounce_jam_state(raw_jam)

        new_jam = (
            jam
            and
            not self.previous_jam
        )

        jam_cleared = (
            self.previous_jam
            and
            not jam
        )

        self.previous_jam = jam

        if new_jam:

            self.jam_start_time = (
                time.time()
            )

        elif jam_cleared:

            self.jam_start_time = None

        jam_duration = (
            (
                time.time()
                -
                self.jam_start_time
            )
            if (
                jam
                and
                self.jam_start_time is not None
            )
            else 0
        )

        track_ids = [
            t.get("track_id")
            for t in roi_tracks
            if t.get("track_id") is not None
        ]

        track_centers = []

        for t in roi_tracks:

            track_id = t.get("track_id")

            if track_id is None:
                continue

            t_bbox = t.get("bbox")

            if (
                t_bbox is None
                or
                len(t_bbox) != 4
            ):
                continue

            t_x_min, t_y_min, t_x_max, t_y_max = t_bbox

            track_centers.append(
                {
                    "id": track_id,
                    "center": [
                        int(
                            (t_x_min + t_x_max) / 2
                        ),
                        int(
                            (t_y_min + t_y_max) / 2
                        ),
                    ],
                }
            )

        roi_track_ids = set(track_ids)

        track_changed = (
            roi_track_ids
            !=
            self.last_saved_track_ids
        )

        roi_distances = []

        minimum_gap = None

        gap_values = []

        spacing_threshold_mm = (
            spacing_result.get(
                "threshold_mm"
            )
            if spacing_result
            else None
        )

        if (
            spacing_result
            and
            self.save_distances
        ):

            for pair in (
                spacing_result.get(
                    "distances",
                    [],
                )
                or []
            ):

                if not isinstance(pair, dict):
                    continue

                if (
                    pair.get("track1") in roi_track_ids
                    and
                    pair.get("track2") in roi_track_ids
                ):

                    roi_distances.append(pair)

                    gap = pair.get("distance_mm")

                    if gap is not None:

                        gap_values.append(gap)

                        if (
                            minimum_gap is None
                            or
                            gap < minimum_gap
                        ):
                            minimum_gap = gap

        average_gap = (
            sum(gap_values) / len(gap_values)
            if gap_values
            else None
        )

        occupancy_percent = (
            (bag_count / self.max_allowed_bags)
            * 100
        )

        result = {

            "jam": jam,

            "status": "jam" if jam else "normal",

            "bag_count": bag_count,

            "track_ids": track_ids,

            "track_centers": track_centers,

            "minimum_gap_mm": minimum_gap,

            "average_gap_mm": average_gap,

            "distances": roi_distances,

            "spacing_threshold_mm": spacing_threshold_mm,

            "roi": {
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
            },

            "max_allowed_bags": self.max_allowed_bags,

            "occupancy_percent": occupancy_percent,

            "jam_duration": jam_duration,

            "timestamp": datetime.now(
                timezone.utc
            ).isoformat(),

            "event_id": self.event_id,

            "image_path": None,
        }

        should_capture = (
            jam
            and
            track_changed
        )

        if should_capture:

            self.last_capture_time = (
                time.time()
            )

            self.last_saved_track_ids = (
                roi_track_ids.copy()
            )

            image_path = self._save(
                frame,
                result,
                camera_name=camera_name,
            )

            result["image_path"] = image_path

        return result

    # =====================================================
    # SAVE
    # =====================================================

    def _save(
        self,
        frame,
        result,
        camera_name=None,
    ):

        self.event_id += 1

        result["event_id"] = self.event_id

        file_timestamp = time.strftime(
            "%Y%m%d_%H%M%S"
        )

        image_path = None

        if (
            self.save_roi_image
            and
            frame is not None
        ):

            try:

                x1 = int(self.roi["x1"])
                y1 = int(self.roi["y1"])
                x2 = int(self.roi["x2"])
                y2 = int(self.roi["y2"])

                roi_image = frame[
                    y1:y2,
                    x1:x2,
                ]

                if roi_image.size > 0:

                    candidate_path = (
                        self.image_directory
                        /
                        f"{file_timestamp}.jpg"
                    )

                    cv2.imwrite(
                        str(
                            candidate_path
                        ),
                        roi_image,
                    )

                    image_path = str(
                        candidate_path
                    )

            except Exception:
                pass

        # NOTE: The per-event JSON log file that used to be
        # written here has been removed. The caller (pipeline)
        # is responsible for persisting this event to SQL
        # Server -- e.g.:
        #
        #   snapshot_id = save_roi_snapshot(
        #       camera_id=camera_name or self.camera_name,
        #       event_type="condition_c",
        #       image_path=image_path,
        #       metadata=result,
        #   )
        #
        #   start_jam_event(
        #       camera_id=camera_name or self.camera_name,
        #       jam_type="ROI_OCCUPANCY_JAM",
        #       condition_code="C",
        #       track_ids=result.get("track_ids"),
        #       roi_snapshot_id=snapshot_id,
        #       metadata=result,
        #   )
        #
        # using the `result` dict returned by process().

        return image_path