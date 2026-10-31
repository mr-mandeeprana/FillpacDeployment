"""
==========================================================
FillPac AI
Logger Module
==========================================================

Author  : Mandeep Rana
Project : Bag Counting & Print Detection Model

Description
-----------
Centralized logging for the application.
"""

import logging
import logging.handlers
import os


class Logger:

    def __init__(
        self,
        log_file="logs/application.log",
        level=logging.INFO,
        max_bytes: int = 10_485_760,
        backup_count: int = 5,
    ):

        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)

        self.logger = logging.getLogger("FillPacAI")
        self.logger.setLevel(self._parse_level(level))

        # Avoid duplicate handlers
        if self.logger.hasHandlers():
            self.logger.handlers.clear()

        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)

        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            mode="a",
            encoding="utf-8",
            maxBytes=max_bytes,
            backupCount=backup_count,
        )
        file_handler.setFormatter(formatter)

        self.logger.addHandler(console_handler)
        self.logger.addHandler(file_handler)

    @staticmethod
    def _parse_level(level):
        if isinstance(level, int):
            return level

        if isinstance(level, str):
            level_name = level.strip().upper()
            if level_name in logging._nameToLevel:
                return logging._nameToLevel[level_name]

        return logging.INFO

    # ======================================================

    def info(self, message):
        self.logger.info(message)

    # ======================================================

    def warning(self, message):
        self.logger.warning(message)

    # ======================================================

    def error(self, message):
        self.logger.error(message)

    # ======================================================

    def critical(self, message):
        self.logger.critical(message)

    # ======================================================

    def debug(self, message):
        self.logger.debug(message)
