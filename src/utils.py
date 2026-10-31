"""
==========================================================
FillPac AI
Utility Functions
==========================================================
"""

import math
import time


class Utils:
    @staticmethod
    def get_center(bbox):
        x1, y1, x2, y2 = bbox
        center_x = int((x1 + x2) / 2)
        center_y = int((y1 + y2) / 2)
        return center_x, center_y

    @staticmethod
    def distance(point1, point2):
        return math.sqrt((point1[0] - point2[0]) ** 2 + (point1[1] - point2[1]) ** 2)

    @staticmethod
    def calculate_fps(start_time):
        current_time = time.time()
        fps = 1 / max(current_time - start_time, 1e-6)
        return fps, current_time

    @staticmethod
    def get_class_name(class_id):
        classes = {0: "Bag", 1: "Print"}
        return classes.get(class_id, "Unknown")