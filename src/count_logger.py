"""
==========================================================
FillPac AI
Production Count Event Logger
==========================================================

Purpose
-------
Persists one record to SQL Server whenever a physical bag
crossing event is confirmed.

Architecture
------------
Camera
   |
   v
Detection
   |
   v
Tracking
   |
   v
Physical Bag Center Crossing
   |
   v
Counter
   |
   v
CountLogger
   |
   +----> SQL Server (dbo.production_events)
   |
   +----> Dashboard Events
   |
   +----> Analytics
   |
   +----> CSV Export


Important
---------
This logger does NOT perform counting.

Counting remains the responsibility of the existing
physical-center crossing logic.

The logger only records confirmed events.

CHANGE LOG
----------
count_events.jsonl has been removed. Every confirmed bag
crossing (log_count / log_count_event / log) is now written
straight to dbo.production_events via database/repository.py.
Generic events (log_event) are written to
dbo.application_logs instead. The public API of this class
is unchanged so existing callers do not need to change.
"""

import logging
import threading
import uuid

from datetime import datetime, timezone

from database.repository import (
    get_resume_counts,
    save_application_log,
    save_production_event,
)


logger = logging.getLogger(
    "fillpac.count_logger"
)


# ==========================================================
# HELPERS
# ==========================================================

def safe_int(
    value,
    default=0,
):
    """
    Safely convert a value to integer.
    """

    try:
        return int(value)

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return default


def safe_float(
    value,
    default=None,
):
    """
    Safely convert a value to float.
    """

    if value is None:
        return default

    try:
        return float(value)

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return default


def utc_now():
    """
    Return current UTC datetime.
    """

    return datetime.now(
        timezone.utc
    )


def utc_now_iso():
    """
    Return ISO-8601 UTC timestamp.
    """

    return (
        utc_now()
        .isoformat()
    )


# ==========================================================
# SHIFT CALCULATION
#
# Default production shifts:
#
# Shift A : 06:00 - 14:00
# Shift B : 14:00 - 22:00
# Shift C : 22:00 - 06:00
#
# This can later be moved into config.yaml if your factory
# uses different shift timings.
# ==========================================================

def determine_shift(
    timestamp=None,
):
    """
    Determine production shift from timestamp.

    Parameters
    ----------
    timestamp:
        datetime instance or None.

    Returns
    -------
    str
        shift-a
        shift-b
        shift-c
    """

    if timestamp is None:

        timestamp = utc_now()

    if not isinstance(
        timestamp,
        datetime,
    ):

        timestamp = utc_now()

    hour = timestamp.hour

    if 6 <= hour < 14:
        return "shift-a"

    if 14 <= hour < 22:
        return "shift-b"

    return "shift-c"


# ==========================================================
# PRINT STATUS NORMALIZATION
# ==========================================================

def determine_print_status(
    print_present,
    print_detection_enabled=True,
):
    """
    Convert print result into dashboard-friendly status.

    Returns
    -------
    printed
    missing
    disabled
    unknown
    """

    if not print_detection_enabled:
        return "disabled"

    if print_present is True:
        return "printed"

    if print_present is False:
        return "missing"

    return "unknown"


# ==========================================================
# COUNT LOGGER
# ==========================================================

class CountLogger:
    """
    Thread-safe persistent bag count event logger.

    Each confirmed bag crossing is stored as one row in
    dbo.production_events. Each row represents exactly ONE
    confirmed bag (bag_count = 1, printed_count / unprinted_
    count reflect that single bag's print result) -- totals
    are obtained on the dashboard/reporting side with
    SUM(bag_count), SUM(printed_count), etc.

    The full original event (including diagnostic-only
    fields like track_id and physical center) is preserved
    in the row's metadata_json column so nothing is lost
    compared to the old JSONL format.
    """

    def __init__(
        self,
        file_path=None,
        enabled=True,
        flush_immediately=True,
        log_file=None,
    ):

        # Backward/test-compatible alias
        if log_file is not None:
            file_path = log_file

        if file_path is not None:

            logger.warning(
                "CountLogger(file_path=...) is deprecated and "
                "ignored -- count events are now written to "
                "SQL Server (dbo.production_events)."
            )

        self.enabled = bool(
            enabled
        )

        # Kept for backward compatibility. SQL Server writes
        # are always immediately committed, so this no longer
        # changes behavior, but existing callers may still
        # read/pass the attribute.
        self.flush_immediately = bool(
            flush_immediately
        )

        self._events_written = 0

        self._write_errors = 0

        self._last_event = None

        self._last_error = None

        # Per-camera totals already stored in SQL Server before
        # this run started, so counts CONTINUE after a restart
        # instead of going back to 0. See _baseline_for().
        self._baselines = {}

        self._baseline_lock = threading.Lock()

        self._baseline_camera_locks = {}


        logger.info(
            "CountLogger initialized."
        )

        logger.info(
            "Count event storage: SQL Server "
            "(dbo.production_events)"
        )

        logger.info(
            "Count event logging enabled: %s",
            self.enabled,
        )


    # ======================================================
    # RESTART CONTINUITY
    # ======================================================

    def get_baseline(
        self,
        camera_name,
    ):
        """
        Totals already saved in SQL Server for this camera:
        {"total": int, "printed": int, "missing": int, ...}

        Use this to seed the live counter at start-up
        (pipeline.py) so it does not begin at 0 after a rerun.
        """

        return get_resume_counts(
            str(camera_name)
        )

    def _baseline_for(
        self,
        camera_name,
        first_total,
    ):
        """
        Offset to add to this run's cumulative counters.

        Decided once per camera, on its first event of the run,
        BEFORE that event is saved:

        - Counter restarted from 0 (first_total <= stored
          total): offset = stored totals, so the cumulative
          values saved in metadata_json continue the old count.
        - Counter was already seeded from the database
          (first_total > stored total): offset = 0, so nothing
          is counted twice.

        Each camera has its own lock, so one camera's SQL lookup
        never makes another camera's writer wait. If SQL Server
        cannot be read, no offset is used and the lookup is
        retried on the next event.
        """

        with self._baseline_lock:

            if camera_name in self._baselines:
                return self._baselines[camera_name]

            camera_lock = self._baseline_camera_locks.setdefault(
                camera_name,
                threading.Lock(),
            )

        with camera_lock:

            cached = self._baselines.get(camera_name)

            if cached is not None:
                return cached

            try:
                stored = get_resume_counts(camera_name)

            except Exception:

                logger.exception(
                    "Could not read stored counts for %s; "
                    "will retry on the next event.",
                    camera_name,
                )

                return {"total": 0, "printed": 0, "missing": 0}

            if first_total > stored["total"]:

                offset = {"total": 0, "printed": 0, "missing": 0}

            else:

                offset = {
                    "total": stored["total"],
                    "printed": stored["printed"],
                    "missing": stored["missing"],
                }

            self._baselines[camera_name] = offset

            logger.info(
                "Count baseline for %s: total=%s printed=%s "
                "missing=%s",
                camera_name,
                offset["total"],
                offset["printed"],
                offset["missing"],
            )

            return offset


    # ======================================================
    # MAIN EVENT LOGGING API
    # ======================================================

    def log_count(
        self,
        camera_name,
        total_count,
        track_id=None,
        center=None,
        print_present=None,
        printed_count=0,
        missing_count=0,
        print_detection_enabled=True,
        timestamp=None,
        **extra_fields,
    ):
        """
        Persist one confirmed physical bag crossing event.

        This signature intentionally keeps compatibility with
        the existing FillPac pipeline.

        Parameters
        ----------
        camera_name:
            Camera that produced the confirmed count.

        total_count:
            Current camera count after crossing. Stored in
            metadata_json for reference; the row's own
            bag_count column is always 1 (one confirmed bag).

        track_id:
            Tracker ID associated with the detection.
            This is diagnostic metadata only.
            It is NOT the counting method.

        center:
            Physical bounding-box center used during
            crossing detection.

        print_present:
            True  -> print detected
            False -> print missing
            None  -> no classification / unknown

        printed_count:
            Camera cumulative printed count (diagnostic,
            stored in metadata_json).

        missing_count:
            Camera cumulative missing-print count
            (diagnostic, stored in metadata_json).

        print_detection_enabled:
            Whether print inspection is enabled for camera.

        timestamp:
            Optional datetime. Current UTC time is used
            when omitted.

        extra_fields:
            Additional future metadata can be written without
            breaking the logger API.

        Returns
        -------
        dict | None
            Persisted event dictionary or None if disabled/
            write failed.
        """

        if not self.enabled:
            return None


        # --------------------------------------------------
        # Timestamp
        # --------------------------------------------------

        if timestamp is None:

            event_time = (
                utc_now()
            )

        elif isinstance(
            timestamp,
            datetime,
        ):

            event_time = timestamp

            if event_time.tzinfo is None:

                event_time = (
                    event_time.replace(
                        tzinfo=timezone.utc
                    )
                )

        else:

            event_time = (
                utc_now()
            )


        timestamp_iso = (
            event_time.isoformat()
        )


        # --------------------------------------------------
        # Physical center
        # --------------------------------------------------

        center_x = None
        center_y = None

        if center is not None:

            try:

                if len(center) >= 2:

                    center_x = safe_float(
                        center[0]
                    )

                    center_y = safe_float(
                        center[1]
                    )

            except (
                TypeError,
                IndexError,
            ):

                center_x = None
                center_y = None


        # --------------------------------------------------
        # Print status
        # --------------------------------------------------

        print_status = (
            determine_print_status(
                print_present=
                    print_present,

                print_detection_enabled=
                    print_detection_enabled,
            )
        )


        # --------------------------------------------------
        # Shift
        # --------------------------------------------------

        shift = (
            determine_shift(
                event_time
            )
        )


        # --------------------------------------------------
        # Build event (kept for return value / last_event /
        # metadata_json fidelity -- same shape as the old
        # JSONL record).
        # --------------------------------------------------

        event = {

            "event_id":
                uuid.uuid4().hex,

            "event":
                "bag_count",

            "timestamp":
                timestamp_iso,

            "shift":
                shift,

            "camera":
                str(
                    camera_name
                ),

            "total_count":
                safe_int(
                    total_count
                ),

            "count":
                safe_int(
                    total_count
                ),

            "track_id":
                (
                    safe_int(
                        track_id
                    )
                    if track_id is not None
                    else None
                ),

            "center_x":
                center_x,

            "center_y":
                center_y,

            "print_detection_enabled":
                bool(
                    print_detection_enabled
                ),

            "print_present":
                (
                    bool(
                        print_present
                    )
                    if print_present is not None
                    else None
                ),

            "print_status":
                print_status,

            "printed_count":
                safe_int(
                    printed_count
                ),

            "missing_count":
                safe_int(
                    missing_count
                ),
        }

        if extra_fields:

            for (
                key,
                value,
            ) in extra_fields.items():

                if key in event:
                    continue

                event[key] = (
                    self._json_safe(
                        value
                    )
                )


        # --------------------------------------------------
        # Continue from the stored totals after a restart
        # --------------------------------------------------

        offset = self._baseline_for(
            event["camera"],
            event["total_count"],
        )

        event["total_count"] += offset["total"]
        event["count"] += offset["total"]
        event["printed_count"] += offset["printed"]
        event["missing_count"] += offset["missing"]


        # --------------------------------------------------
        # Write to SQL Server
        #
        # Each confirmed bag is one row. bag_count is always
        # 1; printed_count/unprinted_count reflect this bag's
        # own print result (0/1), not the camera's running
        # totals -- those are queried with SUM() downstream.
        # The full event above is kept in metadata_json.
        # --------------------------------------------------

        success = (
            self._write_event(
                event
            )
        )

        if not success:
            return None


        self._last_event = (
            event.copy()
        )

        return event


    # ======================================================
    # COMPATIBILITY ALIAS (log_count_event)
    # ======================================================

    def log_count_event(
        self,
        camera_name,
        total_count=0,
        track_id=None,
        center=None,
        print_present=None,
        printed_count=0,
        missing_count=0,
        print_detection_enabled=True,
        **kwargs,
    ):
        """
        Compatibility wrapper for log_count().

        Keeps the public API compatible with tests and
        older callers while using the existing log_count()
        implementation.
        """

        return self.log_count(
            camera_name=camera_name,
            total_count=total_count,
            track_id=track_id,
            center=center,
            print_present=print_present,
            printed_count=printed_count,
            missing_count=missing_count,
            print_detection_enabled=print_detection_enabled,
            **kwargs,
        )


    # ======================================================
    # COMPATIBILITY ALIAS
    # ======================================================

    def log(
        self,
        camera_name,
        total_count,
        track_id=None,
        center=None,
        print_present=None,
        printed_count=0,
        missing_count=0,
        print_detection_enabled=True,
        **kwargs,
    ):
        """
        Compatibility wrapper.

        Allows existing code to call:

            count_logger.log(...)

        while new code may use:

            count_logger.log_count(...)
        """

        return self.log_count(

            camera_name=
                camera_name,

            total_count=
                total_count,

            track_id=
                track_id,

            center=
                center,

            print_present=
                print_present,

            printed_count=
                printed_count,

            missing_count=
                missing_count,

            print_detection_enabled=
                print_detection_enabled,

            **kwargs,
        )


    # ======================================================
    # WRITE EVENT
    # ======================================================

    def _write_event(
        self,
        event,
    ):
        """
        Persist one confirmed bag-count event to
        dbo.production_events.

        One confirmed bag = one SQL row. line_count,
        frame_roi_count and bags_inside_roi are read from the
        event dict (pipeline.py already passes them into
        log_count() as extra_fields) and mapped onto the
        matching save_production_event() columns -- previously
        these were left at their 0 defaults even though the
        values were already sitting in metadata_json.
        """

        try:

            # --------------------------------------------------
            # Extract complete production event information
            # --------------------------------------------------

            line_count = event.get(
                "line_count",
                0,
            )

            frame_roi_count = event.get(
                "frame_roi_count",
                0,
            )

            bags_inside_roi = event.get(
                "bags_inside_roi",
                0,
            )

            # Make sure numeric values are valid
            try:
                line_count = int(line_count or 0)
            except (TypeError, ValueError):
                line_count = 0

            try:
                frame_roi_count = int(
                    frame_roi_count or 0
                )
            except (TypeError, ValueError):
                frame_roi_count = 0

            try:
                bags_inside_roi = int(
                    bags_inside_roi or 0
                )
            except (TypeError, ValueError):
                bags_inside_roi = 0

            # --------------------------------------------------
            # Save complete event to SQL Server
            # --------------------------------------------------

            save_production_event(

                camera_id=
                    event.get("camera"),

                bag_count=
                    1,

                printed_count=
                    1
                    if event.get("print_status") == "printed"
                    else 0,

                unprinted_count=
                    1
                    if event.get("print_status") == "missing"
                    else 0,

                # --------------------------------------------------
                # IMPORTANT: these were previously missing
                # --------------------------------------------------

                line_count=
                    line_count,

                frame_roi_count=
                    frame_roi_count,

                bags_inside_roi=
                    bags_inside_roi,

                timestamp=
                    event.get("timestamp"),

                metadata=
                    event,
            )

            self._events_written += 1

            logger.debug(
                (
                    "Count event persisted | "
                    "camera=%s | "
                    "count=%s | "
                    "print=%s | "
                    "line_count=%s | "
                    "frame_roi_count=%s | "
                    "bags_inside_roi=%s"
                ),
                event.get(
                    "camera"
                ),
                event.get(
                    "total_count"
                ),
                event.get(
                    "print_status"
                ),
                line_count,
                frame_roi_count,
                bags_inside_roi,
            )

            return True


        except Exception as error:

            self._write_errors += 1

            self._last_error = str(
                error
            )

            logger.exception(
                (
                    "Failed writing count event to SQL "
                    "Server | camera=%s | count=%s"
                ),
                event.get(
                    "camera"
                ),
                event.get(
                    "total_count"
                ),
            )

            return False


    # ======================================================
    # GENERIC EVENT
    # ======================================================

    def log_event(
        self,
        event_type,
        camera_name=None,
        **data,
    ):
        """
        Write a generic FillPac event to dbo.application_logs.

        This is available for events such as:

        camera_online
        camera_offline
        inference_timeout
        model_error
        print_alert

        Bag counting should continue using log_count().
        """

        if not self.enabled:
            return None

        event_time = (
            utc_now()
        )

        event = {

            "event_id":
                uuid.uuid4().hex,

            "event":
                str(
                    event_type
                ),

            "timestamp":
                event_time.isoformat(),

            "shift":
                determine_shift(
                    event_time
                ),

            "camera":
                (
                    str(
                        camera_name
                    )
                    if camera_name is not None
                    else None
                ),
        }


        for (
            key,
            value,
        ) in data.items():

            if key in event:
                continue

            event[key] = (
                self._json_safe(
                    value
                )
            )


        try:

            save_application_log(

                level=
                    "INFO",

                logger=
                    "fillpac.count_logger",

                message=
                    str(event_type),

                event_type=
                    str(event_type),

                camera_id=
                    event.get("camera"),

                metadata=
                    event,
            )

            self._events_written += 1

        except Exception as error:

            self._write_errors += 1

            self._last_error = str(
                error
            )

            logger.exception(
                "Failed writing generic event to SQL Server | "
                "event=%s | camera=%s",
                event_type,
                camera_name,
            )

            return None


        self._last_event = (
            event.copy()
        )

        return event


    # ======================================================
    # JSON SAFE CONVERSION
    # ======================================================

    @staticmethod
    def _json_safe(
        value,
    ):
        """
        Convert common values into JSON-safe values.
        """

        if value is None:
            return None

        if isinstance(
            value,
            (
                str,
                int,
                float,
                bool,
            ),
        ):
            return value

        if isinstance(
            value,
            datetime,
        ):

            return value.isoformat()

        if isinstance(
            value,
            dict,
        ):

            return {

                str(key):
                    CountLogger._json_safe(
                        item
                    )

                for (
                    key,
                    item,
                )
                in value.items()

            }

        if isinstance(
            value,
            (
                list,
                tuple,
                set,
            ),
        ):

            return [

                CountLogger._json_safe(
                    item
                )

                for item in value

            ]


        # NumPy scalar compatibility
        item_method = getattr(
            value,
            "item",
            None,
        )

        if callable(
            item_method
        ):

            try:

                return (
                    item_method()
                )

            except Exception:

                pass


        return str(
            value
        )


    # ======================================================
    # STATUS
    # ======================================================

    def get_status(
        self,
    ):
        """
        Return CountLogger runtime information.
        """

        return {

            "enabled":
                self.enabled,

            "storage":
                "sql-server",

            "events_written":
                self._events_written,

            "write_errors":
                self._write_errors,

            "last_error":
                self._last_error,

            "last_event":
                (
                    self._last_event.copy()
                    if self._last_event
                    else None
                ),
        }


    # ======================================================
    # ENABLE
    # ======================================================

    def enable(
        self,
    ):

        self.enabled = True

        logger.info(
            "Count event logging enabled."
        )


    # ======================================================
    # DISABLE
    # ======================================================

    def disable(
        self,
    ):

        self.enabled = False

        logger.info(
            "Count event logging disabled."
        )


    # ======================================================
    # EVENT FILE PATH (DEPRECATED)
    # ======================================================

    def get_file_path(
        self,
    ):
        """
        Deprecated. Count events are stored in SQL Server,
        not a file. Kept only so old callers don't crash.
        """

        return None