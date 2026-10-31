"""
==========================================================
FillPac AI
Production Print Detection Module
==========================================================

Purpose
-------
Associates YOLO print detections with tracked physical bags.

Classes
-------
class_id = 0 : Bag
class_id = 1 : Print

Important
---------
This module determines print presence for each bag in the
CURRENT frame.

It does NOT make the final Printed / Not Printed decision.

Final print classification is handled by Pipeline using
multiple observations collected for each bag track before
the physical bag crosses the counting line.

Matching
--------
A print detection must:
- Be class_id = 1
- Pass confidence threshold
- Pass print area filters
- Pass aspect-ratio filters
- Be inside or meaningfully overlap the bag
- Pass minimum overlap ratio
- Optionally pass IoU threshold
- Optionally pass center-distance threshold

Each print can belong to only one bag.
Each bag can receive only one best print match.
==========================================================
"""

import math


class PrintDetector:

    def __init__(
        self,
        confidence_threshold=0.4,
        iou_threshold=0.0,
        min_overlap_ratio=0.3,
        min_print_area=0.0,
        max_print_area=0.0,
        min_aspect_ratio=0.0,
        max_aspect_ratio=0.0,
        max_center_distance=0.0,
    ):

        # ==================================================
        # CONFIGURATION
        # ==================================================

        self.confidence_threshold = min(
            max(
                float(
                    confidence_threshold
                ),
                0.0,
            ),
            1.0,
        )

        self.iou_threshold = min(
            max(
                float(
                    iou_threshold
                ),
                0.0,
            ),
            1.0,
        )

        self.min_overlap_ratio = min(
            max(
                float(
                    min_overlap_ratio
                ),
                0.0,
            ),
            1.0,
        )

        self.min_print_area = max(
            float(
                min_print_area
            ),
            0.0,
        )

        self.max_print_area = max(
            float(
                max_print_area
            ),
            0.0,
        )

        self.min_aspect_ratio = max(
            float(
                min_aspect_ratio
            ),
            0.0,
        )

        self.max_aspect_ratio = max(
            float(
                max_aspect_ratio
            ),
            0.0,
        )

        self.max_center_distance = max(
            float(
                max_center_distance
            ),
            0.0,
        )

    # ======================================================
    # BOUNDING BOX VALIDATION
    # ======================================================

    @staticmethod
    def is_valid_bbox(
        bbox,
    ):

        if bbox is None:

            return False

        if not isinstance(
            bbox,
            (
                list,
                tuple,
            ),
        ):

            return False

        if len(
            bbox
        ) != 4:

            return False

        try:

            x1, y1, x2, y2 = (
                map(
                    float,
                    bbox,
                )
            )

        except (
            TypeError,
            ValueError,
        ):

            return False

        return (
            x2 > x1
            and y2 > y1
        )

    # ======================================================
    # INSIDE CHECK
    # ======================================================

    @staticmethod
    def is_inside(
        bag_box,
        print_box,
    ):

        bx1, by1, bx2, by2 = (
            bag_box
        )

        px1, py1, px2, py2 = (
            print_box
        )

        return (
            px1 >= bx1
            and py1 >= by1
            and px2 <= bx2
            and py2 <= by2
        )

    # ======================================================
    # OVERLAP CHECK
    # ======================================================

    @staticmethod
    def is_overlap(
        box_a,
        box_b,
    ):

        ax1, ay1, ax2, ay2 = (
            box_a
        )

        bx1, by1, bx2, by2 = (
            box_b
        )

        return not (
            bx2 <= ax1
            or bx1 >= ax2
            or by2 <= ay1
            or by1 >= ay2
        )

    # ======================================================
    # BBOX CENTER
    # ======================================================

    @staticmethod
    def bbox_center(
        bbox,
    ):

        x1, y1, x2, y2 = (
            bbox
        )

        return (
            (
                x1
                + x2
            )
            / 2.0,
            (
                y1
                + y2
            )
            / 2.0,
        )

    # ======================================================
    # CENTER DISTANCE
    # ======================================================

    @staticmethod
    def center_distance(
        box_a,
        box_b,
    ):

        ax, ay = (
            PrintDetector
            .bbox_center(
                box_a
            )
        )

        bx, by = (
            PrintDetector
            .bbox_center(
                box_b
            )
        )

        return math.hypot(
            ax - bx,
            ay - by,
        )

    # ======================================================
    # BBOX AREA
    # ======================================================

    @staticmethod
    def bbox_area(
        bbox,
    ):

        x1, y1, x2, y2 = (
            bbox
        )

        width = max(
            x2 - x1,
            0,
        )

        height = max(
            y2 - y1,
            0,
        )

        return (
            width
            * height
        )

    # ======================================================
    # INTERSECTION AREA
    # ======================================================

    @staticmethod
    def intersection_area(
        box_a,
        box_b,
    ):

        ax1, ay1, ax2, ay2 = (
            box_a
        )

        bx1, by1, bx2, by2 = (
            box_b
        )

        inter_x1 = max(
            ax1,
            bx1,
        )

        inter_y1 = max(
            ay1,
            by1,
        )

        inter_x2 = min(
            ax2,
            bx2,
        )

        inter_y2 = min(
            ay2,
            by2,
        )

        inter_width = max(
            inter_x2
            - inter_x1,
            0,
        )

        inter_height = max(
            inter_y2
            - inter_y1,
            0,
        )

        return (
            inter_width
            * inter_height
        )

    # ======================================================
    # IOU
    # ======================================================

    @staticmethod
    def bbox_iou(
        box_a,
        box_b,
    ):

        intersection = (
            PrintDetector
            .intersection_area(
                box_a,
                box_b,
            )
        )

        area_a = (
            PrintDetector
            .bbox_area(
                box_a
            )
        )

        area_b = (
            PrintDetector
            .bbox_area(
                box_b
            )
        )

        union = (
            area_a
            + area_b
            - intersection
        )

        if union <= 0:

            return 0.0

        return (
            intersection
            / union
        )

    # ======================================================
    # PRINT OVERLAP RATIO
    # ======================================================

    @staticmethod
    def overlap_ratio(
        bag_box,
        print_box,
    ):
        """
        Returns the fraction of the PRINT bounding box
        that overlaps the BAG bounding box.

        Example:
            1.0 = entire print detection is inside bag
            0.5 = half of print detection overlaps bag
            0.0 = no overlap
        """

        print_area = (
            PrintDetector
            .bbox_area(
                print_box
            )
        )

        if print_area <= 0:

            return 0.0

        intersection = (
            PrintDetector
            .intersection_area(
                bag_box,
                print_box,
            )
        )

        return (
            intersection
            / print_area
        )

    # ======================================================
    # PRINT QUALITY FILTER
    # ======================================================

    def _passes_print_quality(
        self,
        detection,
    ):

        if not isinstance(
            detection,
            dict,
        ):

            return False

        # ----------------------------------------------
        # Must be Print class
        # ----------------------------------------------

        if detection.get(
            "class_id"
        ) != 1:

            return False

        # ----------------------------------------------
        # Confidence
        # ----------------------------------------------

        try:

            confidence = float(
                detection.get(
                    "confidence",
                    0.0,
                )
            )

        except (
            TypeError,
            ValueError,
        ):

            return False

        if (
            confidence
            < self.confidence_threshold
        ):

            return False

        # ----------------------------------------------
        # Bounding Box
        # ----------------------------------------------

        bbox = detection.get(
            "bbox"
        )

        if not self.is_valid_bbox(
            bbox
        ):

            return False

        # ----------------------------------------------
        # Area
        # ----------------------------------------------

        area = self.bbox_area(
            bbox
        )

        if (
            area
            < self.min_print_area
        ):

            return False

        if (
            self.max_print_area > 0
            and area > self.max_print_area
        ):

            return False

        # ----------------------------------------------
        # Width / Height
        # ----------------------------------------------

        x1, y1, x2, y2 = (
            bbox
        )

        width = max(
            x2 - x1,
            0,
        )

        height = max(
            y2 - y1,
            0,
        )

        if (
            width <= 0
            or height <= 0
        ):

            return False

        # ----------------------------------------------
        # Aspect Ratio
        # ----------------------------------------------

        aspect_ratio = (
            width
            / height
        )

        if (
            self.min_aspect_ratio > 0
            and aspect_ratio
            < self.min_aspect_ratio
        ):

            return False

        if (
            self.max_aspect_ratio > 0
            and aspect_ratio
            > self.max_aspect_ratio
        ):

            return False

        return True

    # ======================================================
    # BAG QUALITY CHECK
    # ======================================================

    @staticmethod
    def _is_valid_bag(
        bag,
    ):

        if not isinstance(
            bag,
            dict,
        ):

            return False

        bbox = bag.get(
            "bbox"
        )

        return (
            PrintDetector
            .is_valid_bbox(
                bbox
            )
        )

    # ======================================================
    # CANDIDATE MATCH
    # ======================================================

    def _candidate_match(
        self,
        bag_index,
        print_index,
        bag,
        print_det,
    ):

        bag_box = bag.get(
            "bbox"
        )

        print_box = print_det.get(
            "bbox"
        )

        if not self.is_valid_bbox(
            bag_box
        ):

            return None

        if not self.is_valid_bbox(
            print_box
        ):

            return None

        # ----------------------------------------------
        # Basic geometry
        # ----------------------------------------------

        inside = self.is_inside(
            bag_box,
            print_box,
        )

        overlaps = self.is_overlap(
            bag_box,
            print_box,
        )

        # Print must physically overlap the bag.
        if not overlaps:

            return None

        # ----------------------------------------------
        # IoU
        # ----------------------------------------------

        iou = self.bbox_iou(
            bag_box,
            print_box,
        )

        # ----------------------------------------------
        # Print overlap ratio
        #
        # This is generally more useful than IoU because
        # the print box is much smaller than the bag box.
        # ----------------------------------------------

        overlap_ratio = (
            self.overlap_ratio(
                bag_box,
                print_box,
            )
        )

        # ----------------------------------------------
        # Center distance
        # ----------------------------------------------

        distance = (
            self.center_distance(
                bag_box,
                print_box,
            )
        )

        # ----------------------------------------------
        # Minimum print overlap requirement
        # ----------------------------------------------

        if (
            overlap_ratio
            < self.min_overlap_ratio
        ):

            return None

        # ----------------------------------------------
        # Optional IoU requirement
        #
        # IoU is disabled when configured as 0.
        #
        # Because print boxes are normally much smaller
        # than bag boxes, the effective threshold is
        # limited to the maximum theoretically possible
        # IoU for the two boxes.
        # ----------------------------------------------

        if self.iou_threshold > 0:

            effective_threshold = (
                self._effective_iou_threshold(
                    bag_box,
                    print_box,
                )
            )

            if (
                not inside
                and iou
                < effective_threshold
            ):

                return None

        # ----------------------------------------------
        # Optional center-distance filter
        # ----------------------------------------------

        if (
            self.max_center_distance > 0
            and distance
            > self.max_center_distance
        ):

            return None

        return {
            "bag_index":
                bag_index,

            "print_index":
                print_index,

            "inside":
                inside,

            "iou":
                iou,

            "overlap_ratio":
                overlap_ratio,

            "distance":
                distance,

            "confidence":
                float(
                    print_det.get(
                        "confidence",
                        0.0,
                    )
                ),

            "print_bbox":
                print_box,
        }

    # ======================================================
    # EFFECTIVE IOU THRESHOLD
    # ======================================================

    def _effective_iou_threshold(
        self,
        bag_box,
        print_box,
    ):

        if self.iou_threshold <= 0:

            return 0.0

        bag_area = self.bbox_area(
            bag_box
        )

        print_area = self.bbox_area(
            print_box
        )

        larger_area = max(
            bag_area,
            print_area,
        )

        if larger_area <= 0:

            return (
                self.iou_threshold
            )

        # If the smaller box were completely inside the
        # larger box, this is approximately the maximum IoU
        # possible for these two box areas.
        max_possible_iou = (

            min(
                bag_area,
                print_area,
            )

            / larger_area
        )

        return min(
            self.iou_threshold,
            max_possible_iou,
        )

    # ======================================================
    # FIND BEST MATCHES
    # ======================================================

    def best_matches(
        self,
        bags,
        prints,
    ):
        """
        Creates one-to-one bag-to-print assignments.

        A print detection can only be assigned to one bag.
        A bag can only receive one best print detection.
        """

        candidates = []

        # ----------------------------------------------
        # Build candidate matches
        # ----------------------------------------------

        for bag_index, bag in enumerate(
            bags
        ):

            if not self._is_valid_bag(
                bag
            ):

                continue

            for print_index, print_det in enumerate(
                prints
            ):

                match = (
                    self._candidate_match(
                        bag_index,
                        print_index,
                        bag,
                        print_det,
                    )
                )

                if match is not None:

                    candidates.append(
                        match
                    )

        # ----------------------------------------------
        # Sort strongest matches first
        #
        # Priority:
        # 1. Print completely inside bag
        # 2. Larger print-overlap ratio
        # 3. Higher print confidence
        # 4. Higher IoU
        # 5. Smaller center distance
        # ----------------------------------------------

        candidates.sort(
            key=lambda match: (
                match[
                    "inside"
                ],
                match[
                    "overlap_ratio"
                ],
                match[
                    "confidence"
                ],
                match[
                    "iou"
                ],
                -match[
                    "distance"
                ],
            ),
            reverse=True,
        )

        matches = {}

        used_bags = set()

        used_prints = set()

        # ----------------------------------------------
        # Greedy one-to-one assignment
        # ----------------------------------------------

        for match in candidates:

            bag_index = match[
                "bag_index"
            ]

            print_index = match[
                "print_index"
            ]

            if (
                bag_index in used_bags
                or print_index in used_prints
            ):

                continue

            matches[
                bag_index
            ] = match

            used_bags.add(
                bag_index
            )

            used_prints.add(
                print_index
            )

        return matches

    # ======================================================
    # UPDATE
    # ======================================================

    def update(
        self,
        bags,
        detections,
    ):
        """
        Match print detections to tracked bags.

        Parameters
        ----------
        bags:
            Tracked bag dictionaries from Tracker.

        detections:
            All YOLO detections from Detector /
            InferenceManager.

        Returns
        -------
        list
            One result for each valid tracked bag.

        Example
        -------
        {
            "track_id": 10,
            "bag_bbox": (...),
            "print_present": True,
            "print_bbox": (...),
            "print_confidence": 0.91,
            "inside": True,
            "iou": 0.04,
            "overlap_ratio": 1.0,
            "center_distance": 50.2
        }
        """

        bags = (
            bags
            or []
        )

        detections = (
            detections
            or []
        )

        # ==================================================
        # FILTER VALID PRINT DETECTIONS
        # ==================================================

        prints = [

            detection

            for detection
            in detections

            if self._passes_print_quality(
                detection
            )
        ]

        # ==================================================
        # MATCH PRINTS TO BAGS
        # ==================================================

        matches = self.best_matches(
            bags,
            prints,
        )

        # ==================================================
        # BUILD RESULTS
        # ==================================================

        results = []

        for bag_index, bag in enumerate(
            bags
        ):

            if not self._is_valid_bag(
                bag
            ):

                continue

            bag_box = bag[
                "bbox"
            ]

            match = matches.get(
                bag_index
            )

            results.append(
                {
                    "track_id":
                        bag.get(
                            "track_id"
                        ),

                    "bag_bbox":
                        bag_box,

                    "print_present":
                        match
                        is not None,

                    "print_bbox":
                        (
                            match[
                                "print_bbox"
                            ]
                            if match
                            else None
                        ),

                    "print_confidence":
                        (
                            match[
                                "confidence"
                            ]
                            if match
                            else 0.0
                        ),

                    "inside":
                        (
                            match[
                                "inside"
                            ]
                            if match
                            else False
                        ),

                    "iou":
                        (
                            match[
                                "iou"
                            ]
                            if match
                            else 0.0
                        ),

                    "overlap_ratio":
                        (
                            match[
                                "overlap_ratio"
                            ]
                            if match
                            else 0.0
                        ),

                    "center_distance":
                        (
                            match[
                                "distance"
                            ]
                            if match
                            else None
                        ),
                }
            )

        return results