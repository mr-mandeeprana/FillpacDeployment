"""
==========================================================
FillPac AI
Production Dashboard API + Socket.IO Server
==========================================================

Dashboard responsibilities
--------------------------
- Read DashboardState from SQL Server
  (dbo.system_state / dbo.camera_status)
- Read confirmed bag-count history from SQL Server
  (dbo.production_events)
- Read Condition C (ROI occupancy) jam history from SQL
  Server (dbo.jam_events)
- Expose REST APIs
- Broadcast state through Socket.IO
- Calculate production analytics
- Expose jam monitoring information
- Serve optional processed live frames

IMPORTANT
---------
The dashboard backend does NOT perform:

- YOLO inference
- Tracking
- Counting
- Print detection
- Jam detection

Jam detection remains inside the camera Pipeline.

The dashboard only displays the jam state produced by
the AI pipeline.

CHANGE LOG
----------
state.json and count_events.jsonl have been removed. This
server now reads everything from SQL Server through
database/repository.py. ROI snapshot *images* are still read
from disk (SQL Server has no good place to put binary image
data) -- only the JSON event/state files are gone.

REPORT FIX
----------
- New GET /api/report-data: per-camera bags / printed / missing
  for a date-time range, used by the Reports page.
- /analytics now counts printed / missing from dbo.print_events
  (production_events.printed_count / unprinted_count are 0).
==========================================================
"""

import asyncio
import csv
import io
import logging
import math

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel

import socketio

from database.connection import test_database_config
from database.repository import (
    delete_session as db_delete_session,
    get_active_jam_events,
    get_condition_c_events as db_get_condition_c_events,
    get_production_summary,
    get_report_summary,
    get_batch_counter,
    get_recent_jam_events,
    get_recent_print_events,
    get_recent_production_events,
    get_session_user,
    load_dashboard_state,
    start_batch_counter,
    stop_batch_counter,
    touch_session,
    verify_user_credentials,
)
from database.repository import (
    create_session as db_create_session,
    create_user as db_create_user,
    ensure_default_admin_user,
)


# ==========================================================
# OPTIONAL DEPENDENCIES
# ==========================================================

try:
    import yaml
except ImportError:
    yaml = None

# ConfigManager (src/config_manager.py) is the only place that
# writes config.yaml from the dashboard's Settings page. It
# backs up config.yaml before every write and validates
# candidate content using the same rules src.config.Config
# applies to the running pipeline, so a bad edit here can never
# corrupt the file the AI pipeline reads on its next restart.
from src.config import Config
from src.config_manager import (
    ConfigManager,
    ConfigValidationError,
)


# ==========================================================
# PROJECT PATHS
# ==========================================================

BACKEND_DIR = Path(__file__).resolve().parent

DASHBOARD_DIR = BACKEND_DIR.parent

PROJECT_ROOT = DASHBOARD_DIR.parent

CONFIG_FILE = PROJECT_ROOT / "config.yaml"

CONFIG_MANAGER = ConfigManager(CONFIG_FILE)

# NOTE: state.json, count_events.jsonl and the
# logs/condition_c/events/*.json files are gone -- that data
# now lives in SQL Server (dbo.system_state, dbo.camera_status,
# dbo.production_events, dbo.jam_events). ROI snapshot images
# themselves are still written to disk by ROIOccupancyDetector,
# so CONDITION_C_IMAGES_DIR is kept.

CONDITION_C_IMAGES_DIR = (
    PROJECT_ROOT
    / "logs"
    / "condition_c"
    / "images"
)

# The dashboard frontend (index.html, js/, css/, assets/) lives
# alongside this backend, under dashboard/frontend. Serving it
# from this same FastAPI app means the browser can load
# http://127.0.0.1:8000/ and get a working dashboard that talks
# to this API on the same origin -- no separate static server
# or file:// URL needed.

FRONTEND_DIR = (
    DASHBOARD_DIR
    / "frontend"
)


# ==========================================================
# SERVER CONFIGURATION
# ==========================================================

# SQL Server reads get a smaller retry budget than the old
# file reads did -- a transient file lock clears in
# milliseconds, but a SQL connection problem generally won't
# resolve within the same request, so retrying 10 times at
# 30ms only added latency without helping. These retries exist
# for genuinely transient issues (a momentary connection blip).

STATE_WATCH_INTERVAL = 0.20

STATE_READ_RETRIES = 3

# A camera that has not updated its own state for this long is
# not running, whatever status was last persisted. The pipeline
# publishes every ~0.5 s while a camera is alive.
CAMERA_STALE_SECONDS = 15

STATE_READ_RETRY_DELAY = 0.10

EVENT_READ_RETRIES = 3

EVENT_READ_RETRY_DELAY = 0.10

LIVE_FRAME_INTERVAL = 0.10


# ==========================================================
# LOGGER
# ==========================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger(
    "fillpac.dashboard"
)


# ==========================================================
# FASTAPI
# ==========================================================

api = FastAPI(
    title="FillPac AI Dashboard API",
    description=(
        "Production monitoring API for "
        "FillPac AI Bag Counting, "
        "Print Detection and Jam Detection System."
    ),
    version="3.0.0",
)


# ==========================================================
# CORS
# ==========================================================

api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==========================================================
# SOCKET.IO
# ==========================================================

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    logger=False,
    engineio_logger=False,
)


# ==========================================================
# LIVE PIPELINES
# ==========================================================

LIVE_PIPELINES = {}


# ==========================================================
# BASIC HELPERS
# ==========================================================

def safe_int(value, default=0):

    try:
        return int(value)

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return default


def safe_float(value, default=0.0):

    try:

        result = float(value)

        if not math.isfinite(result):
            return default

        return result

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return default


def safe_round(value, digits=2):

    return round(
        safe_float(value),
        digits,
    )


def safe_bool(value, default=False):

    if isinstance(value, bool):
        return value

    if value is None:
        return default

    if isinstance(value, str):

        value = value.strip().lower()

        if value in {
            "true",
            "1",
            "yes",
            "on",
            "enabled",
        }:
            return True

        if value in {
            "false",
            "0",
            "no",
            "off",
            "disabled",
        }:
            return False

    return bool(value)


def utc_now():

    return datetime.now(
        timezone.utc
    )


def utc_now_iso():

    return utc_now().isoformat()


# ==========================================================
# JAM STATUS HELPERS
# ==========================================================

JAM_STATUS_PRIORITY = {

    "normal": 0,

    "recovering": 1,

    "slow": 2,

    "warning": 3,

    "jam": 4,
}


def normalize_jam_status(status):

    value = str(
        status or "normal"
    ).strip().lower()

    if value not in JAM_STATUS_PRIORITY:
        return "normal"

    return value


def get_highest_jam_status(
    cameras,
):

    """
    Determine overall plant jam status.

    Highest severity wins:

    NORMAL
       ↓
    RECOVERING
       ↓
    SLOW
       ↓
    WARNING
       ↓
    JAM
    """

    highest_status = "normal"

    highest_priority = 0

    for camera in cameras.values():

        status = normalize_jam_status(
            camera.get(
                "jam_status",
                "normal",
            )
        )

        priority = JAM_STATUS_PRIORITY.get(
            status,
            0,
        )

        if priority > highest_priority:

            highest_priority = priority

            highest_status = status

    return highest_status


# ==========================================================
# DEFAULT DASHBOARD STATE
# ==========================================================

def default_dashboard_state(
    system_status="offline",
):

    return {

        "system_status":
            system_status,

        "startup_time":
            None,

        "updated_at":
            None,

        "service_status":
            {},

        "total_count":
            0,

        "total_printed_count":
            0,

        "total_missing_count":
            0,

        "total_printed_bags_count":
            0,

        "total_not_printed_bags_count":
            0,
        "count_type":
            "roi",
        "cameras":
            {},
    }


# ==========================================================
# STALE CAMERA STATUS
# ==========================================================

LIVE_CAMERA_STATUSES = {"online", "running", "active"}


def apply_stale_camera_status(state):
    """
    A camera that stopped (or never started in this run) keeps the
    status saved by an earlier run, e.g. "online". If its own
    updated_at is older than CAMERA_STALE_SECONDS, report it as
    offline with 0 fps instead.
    """

    cameras = state.get("cameras")

    if not isinstance(cameras, dict):
        return state

    now = datetime.now(timezone.utc)

    for camera in cameras.values():

        if not isinstance(camera, dict):
            continue

        if str(camera.get("status", "")).lower() not in LIVE_CAMERA_STATUSES:
            continue

        stale = True

        try:
            value = str(camera.get("updated_at") or "")

            if value.endswith("Z"):
                value = value[:-1] + "+00:00"

            updated = datetime.fromisoformat(value)

            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)

            stale = (now - updated).total_seconds() > CAMERA_STALE_SECONDS

        except (TypeError, ValueError):
            stale = True

        if stale:
            camera["status"] = "offline"
            camera["fps"] = 0.0

    return state


# ==========================================================
# PER-CAMERA PRODUCTION SWITCHES / COUNT TYPE
#
#   production.include_bags_in_total   (bag count -> total bags)
#   production.include_prints_in_total (print counts -> dashboard
#                                       data and print totals)
#   dashboard.default_count_type       roi | line
# ==========================================================

def get_live_dashboard_state():
    """The DashboardState of the running application, if any."""

    for pipeline in list(LIVE_PIPELINES.values()):

        live_state = getattr(
            pipeline,
            "dashboard_state",
            None,
        )

        if live_state is not None:
            return live_state

    return None


def get_excluded_camera_names(cameras, flag):
    """Names of cameras whose `flag` is switched off."""

    excluded = set()

    for name, camera in (cameras or {}).items():

        if (
            isinstance(camera, dict)
            and not safe_bool(
                camera.get(flag, True),
                True,
            )
        ):
            excluded.add(str(name))

    return excluded


def get_excluded_cameras(cameras):
    """(bag_excluded, print_excluded) camera name sets."""

    return (
        get_excluded_camera_names(cameras, "include_bags_in_total"),
        get_excluded_camera_names(cameras, "include_prints_in_total"),
    )


def totals_from_summary(summary, bag_excluded, print_excluded):
    """
    Re-add a get_production_summary() result: bags skip cameras
    with include_bags_in_total off, printed / missing skip cameras
    with include_prints_in_total off.
    Returns (total, printed, missing).
    """

    total = printed = missing = 0

    for camera_name, values in (
        (summary or {}).get("cameras") or {}
    ).items():

        if not isinstance(values, dict):
            continue

        name = str(camera_name)

        if name not in bag_excluded:
            total += safe_int(values.get("total"))

        if name not in print_excluded:
            printed += safe_int(values.get("printed"))
            missing += safe_int(values.get("missing"))

    return total, printed, missing


def apply_live_settings():
    """
    Push the settings that CAN change in a running process, read
    from the config.yaml that was just saved/restored:

        dashboard.default_count_type
        production.count_enabled
        production.include_bags_in_total
        production.include_prints_in_total

    Everything else (including ai_enabled) still needs a restart.
    Returns a list of readable strings. Never raises: a settings
    save must not fail because of this.
    """

    applied = []

    try:

        settings = Config(config_path=str(CONFIG_FILE))

        live_state = get_live_dashboard_state()

        if live_state is not None:

            active = live_state.set_count_type(
                Config.normalize_count_type(
                    settings.get(
                        "dashboard",
                        "default_count_type",
                        default="roi",
                    )
                )
            )

            applied.append(f"count type = {active}")

        for camera in settings.get("cameras", default=[]) or []:

            name = str(camera.get("name", ""))
            pipeline = LIVE_PIPELINES.get(name)

            if pipeline is None:
                continue

            flags = dict(camera.get("production") or {})

            pipeline.count_enabled = bool(
                flags.get("count_enabled", True)
            )
            pipeline.include_bags_in_total = bool(
                flags.get("include_bags_in_total", True)
            )
            pipeline.include_prints_in_total = bool(
                flags.get("include_prints_in_total", True)
            )
            pipeline.counting_active = bool(
                getattr(pipeline, "ai_enabled", True)
                and pipeline.count_enabled
            )

            if live_state is not None:
                live_state.update_camera(
                    name,
                    count_enabled=pipeline.count_enabled,
                    include_bags_in_total=pipeline.include_bags_in_total,
                    include_prints_in_total=(
                        pipeline.include_prints_in_total
                    ),
                )

            applied.append(
                f"{name}: counting="
                f"{'on' if pipeline.count_enabled else 'off'}, "
                f"bags in total="
                f"{'yes' if pipeline.include_bags_in_total else 'no'}, "
                f"prints in total="
                f"{'yes' if pipeline.include_prints_in_total else 'no'}"
            )

    except Exception as error:

        logger.warning(
            "Could not apply settings live: %s",
            error,
        )

    return applied


# ==========================================================
# READ DASHBOARD STATE
# ==========================================================

async def read_dashboard_state():

    last_error = None

    for attempt in range(
        STATE_READ_RETRIES
    ):

        try:

            state = await asyncio.to_thread(
                load_dashboard_state
            )

            if state is None:

                return default_dashboard_state(
                    "offline"
                )

            if not isinstance(
                state,
                dict,
            ):

                raise ValueError(
                    "Dashboard state must be "
                    "a JSON object."
                )

            state.setdefault(
                "system_status",
                "unknown",
            )

            state.setdefault(
                "service_status",
                {},
            )

            state.setdefault(
                "cameras",
                {},
            )

            state.setdefault(
                "count_type",
                "roi",
            )

            state.setdefault(
                "total_count",
                0,
            )

            state.setdefault(
                "total_printed_count",
                state.get(
                    "total_printed_bags_count",
                    0,
                ),
            )

            state.setdefault(
                "total_missing_count",
                state.get(
                    "total_not_printed_bags_count",
                    0,
                ),
            )

            return apply_stale_camera_status(state)

        except Exception as error:

            last_error = error

            if (
                attempt
                <
                STATE_READ_RETRIES - 1
            ):

                await asyncio.sleep(
                    STATE_READ_RETRY_DELAY
                )

    logger.warning(
        "Could not read dashboard state from "
        "SQL Server: %s",
        last_error,
    )

    return default_dashboard_state(
        "error"
    )


# ==========================================================
# PARSE EVENT TIME
# ==========================================================

def parse_event_datetime(
    timestamp,
):

    if not timestamp:
        return None

    try:

        value = str(
            timestamp
        ).strip()

        if value.endswith("Z"):

            value = (
                value[:-1]
                + "+00:00"
            )

        parsed = datetime.fromisoformat(
            value
        )

        if parsed.tzinfo is None:

            parsed = parsed.replace(
                tzinfo=timezone.utc
            )

        return parsed

    except (
        TypeError,
        ValueError,
    ):

        return None


# ==========================================================
# SHIFT CALCULATION
# ==========================================================

def determine_shift(
    timestamp,
):

    event_time = parse_event_datetime(
        timestamp
    )

    if event_time is None:
        return "unknown"

    hour = event_time.hour

    if 6 <= hour < 14:
        return "shift-a"

    if 14 <= hour < 22:
        return "shift-b"

    return "shift-c"


# ==========================================================
# EVENT NORMALIZATION
# ==========================================================

def normalize_event(
    event,
):

    normalized = dict(event)

    print_status = normalized.get(
        "print_status"
    )

    if print_status is None:

        print_present = normalized.get(
            "print_present"
        )

        if print_present is True:

            print_status = "printed"

        elif print_present is False:

            print_status = "missing"

        else:

            print_status = "unknown"

    normalized[
        "print_status"
    ] = str(
        print_status
    ).lower()

    timestamp = normalized.get(
        "timestamp"
    )

    normalized[
        "timestamp"
    ] = timestamp

    normalized[
        "camera"
    ] = str(
        normalized.get(
            "camera",
            "Unknown",
        )
    )

    normalized[
        "total_count"
    ] = safe_int(
        normalized.get(
            "total_count"
        )
    )

    normalized[
        "printed_count"
    ] = safe_int(
        normalized.get(
            "printed_count"
        )
    )

    normalized[
        "missing_count"
    ] = safe_int(
        normalized.get(
            "missing_count"
        )
    )

    normalized[
        "shift"
    ] = (
        normalized.get(
            "shift"
        )
        or
        determine_shift(
            timestamp
        )
    )

    # Jam fields are optional because older count
    # events may not contain jam information.

    if "jam_status" in normalized:

        normalized[
            "jam_status"
        ] = normalize_jam_status(
            normalized.get(
                "jam_status"
            )
        )

    if "jam_detected" in normalized:

        normalized[
            "jam_detected"
        ] = safe_bool(
            normalized.get(
                "jam_detected"
            )
        )

    return normalized


# ==========================================================
# READ COUNT EVENTS
# ==========================================================

async def read_count_events():

    last_error = None

    for attempt in range(
        EVENT_READ_RETRIES
    ):

        try:

            raw_events = await asyncio.to_thread(
                get_recent_production_events
            )

            events = []

            for event in raw_events:

                if not isinstance(
                    event,
                    dict,
                ):
                    continue

                events.append(
                    normalize_event(
                        event
                    )
                )

            return events

        except Exception as error:

            last_error = error

            if (
                attempt
                <
                EVENT_READ_RETRIES - 1
            ):

                await asyncio.sleep(
                    EVENT_READ_RETRY_DELAY
                )

    logger.warning(
        "Could not read count events from "
        "SQL Server: %s",
        last_error,
    )

    return []


# ==========================================================
# READ CONDITION C EVENTS
# ==========================================================

async def read_condition_c_events():

    try:

        events = await asyncio.to_thread(
            db_get_condition_c_events
        )

    except Exception as error:

        logger.warning(
            "Could not read Condition C events "
            "from SQL Server: %s",
            error,
        )

        return []

    return [

        event

        for event in events

        if isinstance(event, dict)
    ]


# ==========================================================
# PRINT QUALITY
# ==========================================================

def calculate_print_quality(
    printed,
    missing,
):

    printed = safe_int(
        printed
    )

    missing = safe_int(
        missing
    )

    inspected = (
        printed
        +
        missing
    )

    if inspected <= 0:
        return 0.0

    return round(
        (
            printed
            /
            inspected
        )
        * 100.0,
        2,
    )


# ==========================================================
# PRODUCTION RATE
# ==========================================================

def calculate_production_rate(
    snapshot,
):

    total_count = safe_int(
        snapshot.get(
            "total_count"
        )
    )

    startup_time = snapshot.get(
        "startup_time"
    )

    if not startup_time:
        return 0.0

    start = parse_event_datetime(
        startup_time
    )

    if start is None:
        return 0.0

    try:

        elapsed_seconds = (
            utc_now()
            -
            start
        ).total_seconds()

    except TypeError:
        return 0.0

    if elapsed_seconds <= 0:
        return 0.0

    elapsed_hours = (
        elapsed_seconds
        /
        3600.0
    )

    return round(
        total_count
        /
        elapsed_hours,
        2,
    )


# ==========================================================
# CONDITION C - IMAGE URL
# ==========================================================

def build_condition_c_image_url(
    image_path,
):

    if not image_path:
        return None

    filename = Path(
        str(image_path)
    ).name

    if not filename:
        return None

    return (
        "/condition-c/image/"
        f"{filename}"
    )


# ==========================================================
# CAMERA PRODUCTION DATA
#
# JAM-AWARE VERSION
# ==========================================================

def build_camera_production(
    cameras,
):

    result = {}

    for (
        camera_name,
        camera,
    ) in cameras.items():

        count = safe_int(
            camera.get(
                "count"
            )
        )

        printed = safe_int(
            camera.get(
                "printed_count",
                camera.get(
                    "printed_bags_count",
                    0,
                ),
            )
        )

        missing = safe_int(
            camera.get(
                "missing_count",
                camera.get(
                    "not_printed_bags_count",
                    0,
                ),
            )
        )

        jam_status = normalize_jam_status(
            camera.get(
                "jam_status",
                "normal",
            )
        )

        active_jam_track_ids = (
            camera.get(
                "active_jam_track_ids",
                [],
            )
            or []
        )

        result[
            camera_name
        ] = {

            # ==============================================
            # PRODUCTION
            # ==============================================

            "ai_enabled":
                safe_bool(camera.get("ai_enabled", True), True),

            "count_enabled":
                safe_bool(camera.get("count_enabled", True), True),

            "include_bags_in_total":
                safe_bool(
                    camera.get("include_bags_in_total", True),
                    True,
                ),

            "include_prints_in_total":
                safe_bool(
                    camera.get("include_prints_in_total", True),
                    True,
                ),

            "count_type":
                camera.get("count_type", "roi"),

            "count":
                count,

            "printed_count":
                printed,

            "missing_count":
                missing,

            "print_quality":
                calculate_print_quality(
                    printed,
                    missing,
                ),

            # ==============================================
            # CAMERA
            # ==============================================

            "fps":
                safe_round(
                    camera.get(
                        "fps"
                    )
                ),

            "status":
                camera.get(
                    "status",
                    "offline",
                ),

            "print_status":
                camera.get(
                    "print_status"
                ),

            "updated_at":
                camera.get(
                    "updated_at"
                ),

            # ==============================================
            # JAM DETECTION
            # ==============================================

            "jam_detection_enabled":
                safe_bool(
                    camera.get(
                        "jam_detection_enabled",
                        False,
                    )
                ),

            "jam_status":
                jam_status,

            "jam_detected":
                safe_bool(
                    camera.get(
                        "jam_detected",
                        False,
                    )
                ),

            "jam_warning":
                safe_bool(
                    camera.get(
                        "jam_warning",
                        False,
                    )
                ),

            "active_jam_count":
                safe_int(
                    camera.get(
                        "active_jam_count",
                        0,
                    )
                ),

            "active_jam_track_ids":
                active_jam_track_ids,

            # ==============================================
            # CONDITION C - ROI OCCUPANCY
            # ==============================================

            "condition_c_enabled":
                safe_bool(
                    camera.get(
                        "condition_c_enabled",
                        False,
                    )
                ),

            "condition_c_detected":
                safe_bool(
                    camera.get(
                        "condition_c_detected",
                        False,
                    )
                ),

            "condition_c_status":
                camera.get(
                    "condition_c_status",
                    "normal",
                ),

            "condition_c_bag_count":
                safe_int(
                    camera.get(
                        "condition_c_bag_count",
                        0,
                    )
                ),

            "condition_c_track_ids":
                camera.get(
                    "condition_c_track_ids",
                    [],
                )
                or [],

            "condition_c_minimum_gap_mm":
                camera.get(
                    "condition_c_minimum_gap_mm"
                ),

            "condition_c_image_path":
                camera.get(
                    "condition_c_image_path"
                ),

            "condition_c_image_exists":
                bool(
                    camera.get(
                        "condition_c_image_path"
                    )
                ),

            "condition_c_image_url":
                build_condition_c_image_url(
                    camera.get(
                        "condition_c_image_path"
                    )
                ),

            "condition_c_distances":
                camera.get(
                    "condition_c_distances",
                    [],
                )
                or [],

            "condition_c_spacing_pairs":
                camera.get(
                    "condition_c_spacing_pairs",
                    [],
                )
                or [],

            "condition_c_roi":
                camera.get(
                    "condition_c_roi",
                    {},
                ),

            "condition_c_timestamp":
                camera.get(
                    "condition_c_timestamp"
                ),

            "condition_c_duration":
                camera.get(
                    "condition_c_duration",
                    0,
                ),

        }

    return result


# ==========================================================
# JAM SUMMARY
# ==========================================================

def build_jam_summary(
    cameras,
):

    overall_status = (
        get_highest_jam_status(
            cameras
        )
    )

    jam_camera_count = 0

    warning_camera_count = 0

    slow_camera_count = 0

    recovering_camera_count = 0

    normal_camera_count = 0

    total_active_jams = 0

    condition_c_jam_camera_count = 0

    condition_c_active_tracks = 0

    condition_c_total_bags = 0

    camera_result = {}

    for (
        camera_name,
        camera,
    ) in cameras.items():

        status = normalize_jam_status(
            camera.get(
                "jam_status",
                "normal",
            )
        )

        active_count = safe_int(
            camera.get(
                "active_jam_count",
                0,
            )
        )

        total_active_jams += active_count

        condition_c_detected = safe_bool(
            camera.get(
                "condition_c_detected",
                False,
            )
        )

        condition_c_track_ids = (
            camera.get(
                "condition_c_track_ids",
                [],
            )
            or []
        )

        condition_c_bag_count = safe_int(
            camera.get(
                "condition_c_bag_count",
                0,
            )
        )

        if condition_c_detected:

            condition_c_jam_camera_count += 1

        condition_c_active_tracks += len(
            condition_c_track_ids
        )

        condition_c_total_bags += (
            condition_c_bag_count
        )

        if status == "jam":

            jam_camera_count += 1

        elif status == "warning":

            warning_camera_count += 1

        elif status == "slow":

            slow_camera_count += 1

        elif status == "recovering":

            recovering_camera_count += 1

        else:

            normal_camera_count += 1

        camera_result[
            camera_name
        ] = {

            "enabled":
                safe_bool(
                    camera.get(
                        "jam_detection_enabled",
                        False,
                    )
                ),

            "status":
                status,

            "jam_detected":
                safe_bool(
                    camera.get(
                        "jam_detected",
                        False,
                    )
                ),

            "warning":
                safe_bool(
                    camera.get(
                        "jam_warning",
                        False,
                    )
                ),

            "active_jam_count":
                active_count,

            "active_jam_track_ids":
                camera.get(
                    "active_jam_track_ids",
                    [],
                )
                or [],

            "condition_c_enabled":
                safe_bool(
                    camera.get(
                        "condition_c_enabled",
                        False,
                    )
                ),

            "condition_c_detected":
                condition_c_detected,

            "condition_c_bag_count":
                condition_c_bag_count,

            "condition_c_track_ids":
                condition_c_track_ids,

            "condition_c_minimum_gap_mm":
                camera.get(
                    "condition_c_minimum_gap_mm"
                ),

            "updated_at":
                camera.get(
                    "updated_at"
                ),

        }

    return {

        "overall_status":
            overall_status,

        "camera_count":
            len(cameras),

        "normal_cameras":
            normal_camera_count,

        "slow_cameras":
            slow_camera_count,

        "warning_cameras":
            warning_camera_count,

        "jam_cameras":
            jam_camera_count,

        "recovering_cameras":
            recovering_camera_count,

        "active_jams":
            total_active_jams,

        "condition_c_jam_cameras":
            condition_c_jam_camera_count,

        "condition_c_active_tracks":
            condition_c_active_tracks,

        "condition_c_total_tracks":
            condition_c_active_tracks,

        "condition_c_total_bags":
            condition_c_total_bags,

        "cameras":
            camera_result,

    }


# ==========================================================
# SAFE CONFIGURATION
# ==========================================================

def read_safe_config():

    if yaml is None:

        return {
            "available": False,
            "error": (
                "PyYAML is not installed."
            ),
        }

    if not CONFIG_FILE.exists():

        return {
            "available": False,
            "error": (
                "config.yaml not found."
            ),
        }

    try:

        with open(
            CONFIG_FILE,
            "r",
            encoding="utf-8",
        ) as file:

            config = (
                yaml.safe_load(file)
                or {}
            )

    except (
        OSError,
        yaml.YAMLError,
    ) as error:

        logger.warning(
            "Could not read config: %s",
            error,
        )

        return {
            "available": False,
            "error": str(error),
        }

    cameras = []

    for camera in config.get(
        "cameras",
        [],
    ):

        if not isinstance(
            camera,
            dict,
        ):
            continue

        cameras.append(
            {
                "id":
                    camera.get(
                        "id"
                    ),

                "name":
                    camera.get(
                        "name"
                    ),

                "enabled":
                    bool(
                        camera.get(
                            "enabled",
                            True,
                        )
                    ),

                "mode":
                    camera.get(
                        "mode"
                    ),

                "roi":
                    camera.get(
                        "roi",
                        {},
                    ),

                "counting":
                    camera.get(
                        "counting",
                        {},
                    ),

                "print_detection":
                    camera.get(
                        "print_detection",
                        {},
                    ),

                # ==========================================
                # JAM CONFIGURATION
                # ==========================================

                "jam_detection":
                    camera.get(
                        "jam_detection",
                        {},
                    ),

                "bag_spacing":
                    camera.get(
                        "bag_spacing",
                        {},
                    ),

                "condition_c":
                    camera.get(
                        "condition_c",
                        {},
                    ),

                "dashboard_publish_interval":
                    camera.get(
                        "dashboard_publish_interval"
                    ),

                "inference_timeout":
                    camera.get(
                        "inference_timeout"
                    ),
            }
        )

    return {

        "available":
            True,

        "dashboard":
            config.get(
                "dashboard",
                {},
            ),

        "counting":
            config.get(
                "counting",
                {},
            ),

        "print_detection":
            config.get(
                "print_detection",
                {},
            ),

        "jam_detection":
            config.get(
                "jam_detection",
                {},
            ),

        "bag_spacing":
            config.get(
                "bag_spacing",
                {},
            ),

        "condition_c":
            config.get(
                "condition_c",
                {},
            ),

        "tracker":
            config.get(
                "tracker",
                {},
            ),

        "cameras":
            cameras,
    }


# ==========================================================
# ROOT
# ==========================================================

@api.get("/")
async def root():

    index_file = (
        FRONTEND_DIR
        / "index.html"
    )

    if index_file.exists():

        return FileResponse(
            index_file,
            media_type="text/html",
        )

    # Frontend not found on disk -- fall back to the API
    # status payload so the server still reports something
    # useful instead of a bare 404.

    return await api_info()


# ==========================================================
# API INFO
# ==========================================================

@api.get("/api")
async def api_info():

    return {

        "service":
            "FillPac AI Dashboard API",

        "version":
            "3.0.0",

        "status":
            "running",

        "features": [
            "bag-counting",
            "print-detection",
            "jam-monitoring",
            "analytics",
            "live-monitor",
        ],

        "timestamp":
            utc_now_iso(),

        "storage":
            "sql-server",
    }


# ==========================================================
# LOGIN PAGE
# ==========================================================

@api.get("/login")
async def login_page():

    login_file = (
        FRONTEND_DIR
        / "login.html"
    )

    if login_file.exists():

        return FileResponse(
            login_file,
            media_type="text/html",
        )

    raise HTTPException(
        status_code=404,
        detail="login.html not found in frontend directory.",
    )


# ==========================================================
# AUTHENTICATION
#
# Backs login_themed.html / dashboard_auth_themed.js, which
# already call these three endpoints:
#   POST /api/auth/login
#   GET  /api/auth/verify
#   POST /api/auth/logout
#
# Sessions are opaque tokens stored in SQL Server
# (dbo.auth_sessions) -- see database/repository.py and
# database/auth.py. `Depends(get_current_user)` is added to
# every data-bearing route below so a valid
# `Authorization: Bearer <token>` header is required to reach
# it; the static frontend routes ("/", "/login", static
# assets) stay open since the SPA itself enforces login
# client-side before it renders anything useful.
# ==========================================================

class LoginRequest(BaseModel):
    username: str
    password: str


async def get_current_user(
    authorization: str | None = Header(default=None),
) -> dict:

    if not authorization or not authorization.lower().startswith("bearer "):
        logger.info(
            "get_current_user: rejected request with no/malformed "
            "Authorization header (got: %r).",
            authorization,
        )
        raise HTTPException(
            status_code=401,
            detail="Missing or malformed Authorization header.",
        )

    token = authorization.split(" ", 1)[1].strip()

    # get_session_user() logs the specific reason (not found /
    # expired / deactivated) at INFO level -- check the server
    # log right after a 401 to see which one fired.
    user = get_session_user(token)

    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired session. Please log in again.",
        )

    touch_session(token)

    return user


async def require_admin(
    user: dict = Depends(get_current_user),
) -> dict:
    """
    Same as get_current_user, but additionally requires the
    "admin" role (dbo.users.role -- see database/repository.py's
    create_user/ensure_default_admin_user). Used to gate
    config.yaml writes so an "operator" account can view the
    Settings page but not change values that affect jam/count
    detection; only "admin" accounts can save/reload/reset.
    """

    if (user.get("role") or "").lower() != "admin":
        raise HTTPException(
            status_code=403,
            detail="This action requires an administrator account.",
        )

    return user


@api.post("/api/auth/login")
async def auth_login(payload: LoginRequest):

    user = verify_user_credentials(
        payload.username.strip(),
        payload.password,
    )

    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password.",
        )

    session = db_create_session(user["id"])

    return {
        "token": session["token"],
        "expires_at": session["expires_at"],
        "user_id": user["id"],
        "username": user["username"],
        "role": user["role"],
    }


@api.get("/api/auth/verify")
async def auth_verify(user: dict = Depends(get_current_user)):

    return {
        "valid": True,
        "user_id": user["id"],
        "username": user["username"],
        "role": user["role"],
    }


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "operator"


@api.post("/api/users")
async def api_create_user(
    payload: CreateUserRequest,
    _admin: dict = Depends(require_admin),
):
    """
    Create a dashboard login account. Admin session required,
    so an operator cannot call this directly.
    """

    role = (payload.role or "").strip().lower()

    if role not in {"admin", "operator"}:
        raise HTTPException(
            status_code=400,
            detail="Role must be admin or operator.",
        )

    try:
        user = await asyncio.to_thread(
            db_create_user,
            payload.username,
            payload.password,
            role,
        )

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        )

    logger.info(
        "User '%s' (%s) created by admin '%s'.",
        user["username"], user["role"], _admin.get("username"),
    )

    return {
        "created": True,
        "username": user["username"],
        "role": user["role"],
    }


@api.post("/api/auth/logout")
async def auth_logout(
    authorization: str | None = Header(default=None),
):

    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        db_delete_session(token)

    return {"status": "logged out"}


# ==========================================================
# FRONTEND STATIC ASSETS
#
# Matches index.html's relative references:
#   href="css/dashboard.css"
#   src="js/dashboard.js"
#   src="assets/Logo-BEUMER-Group.webp"
# ==========================================================

if (FRONTEND_DIR / "js").is_dir():

    api.mount(
        "/js",
        StaticFiles(
            directory=str(FRONTEND_DIR / "js")
        ),
        name="frontend-js",
    )

if (FRONTEND_DIR / "css").is_dir():

    api.mount(
        "/css",
        StaticFiles(
            directory=str(FRONTEND_DIR / "css")
        ),
        name="frontend-css",
    )

if (FRONTEND_DIR / "assets").is_dir():

    api.mount(
        "/assets",
        StaticFiles(
            directory=str(FRONTEND_DIR / "assets")
        ),
        name="frontend-assets",
    )


# ==========================================================
# HEALTH
# ==========================================================

@api.get("/health")
async def health():

    snapshot = (
        await read_dashboard_state()
    )

    cameras = (
        snapshot.get(
            "cameras",
            {},
        )
        or {}
    )

    service_status = (
        snapshot.get(
            "service_status",
            {},
        )
        or {}
    )

    online_cameras = sum(

        1

        for camera
        in cameras.values()

        if str(
            camera.get(
                "status",
                "",
            )
        ).lower()

        in {
            "online",
            "running",
            "active",
        }
    )

    jam_summary = (
        build_jam_summary(
            cameras
        )
    )

    return {

        "status":
            "ok",

        "system_status":
            snapshot.get(
                "system_status",
                "unknown",
            ),

        "model_loaded":
            safe_bool(
                service_status.get(
                    "model_loaded",
                    False,
                )
            ),

        "inference_manager_running":
            safe_bool(
                service_status.get(
                    "inference_manager_running",
                    False,
                )
            ),

        "elasticsearch_connected":
            safe_bool(
                service_status.get(
                    "elasticsearch_connected",
                    False,
                )
            ),

        "dashboard_enabled":
            safe_bool(
                service_status.get(
                    "dashboard_enabled",
                    True,
                )
            ),

        "camera_count":
            len(cameras),

        "online_cameras":
            online_cameras,

        "total_count":
            safe_int(
                snapshot.get(
                    "total_count"
                )
            ),

        "total_printed_count":
            safe_int(
                snapshot.get(
                    "total_printed_count"
                )
            ),

        "total_missing_count":
            safe_int(
                snapshot.get(
                    "total_missing_count"
                )
            ),

        # ==============================================
        # JAM HEALTH
        # ==============================================

        "jam_status":
            jam_summary[
                "overall_status"
            ],

        "active_jams":
            jam_summary[
                "active_jams"
            ],

        "jam_cameras":
            jam_summary[
                "jam_cameras"
            ],

        "warning_cameras":
            jam_summary[
                "warning_cameras"
            ],

        "startup_time":
            snapshot.get(
                "startup_time"
            ),

        "updated_at":
            snapshot.get(
                "updated_at"
            ),

        "service_status":
            service_status,

        "storage":
            "sql-server",
    }


# ==========================================================
# STATE
# ==========================================================

@api.get("/state")
async def state(_user: dict = Depends(get_current_user)):

    return (
        await read_dashboard_state()
    )


# ==========================================================
# CAMERAS
# ==========================================================

@api.get("/cameras")
async def cameras(_user: dict = Depends(get_current_user)):

    snapshot = (
        await read_dashboard_state()
    )

    return snapshot.get(
        "cameras",
        {},
    )


# ==========================================================
# SINGLE CAMERA
# ==========================================================

@api.get(
    "/cameras/{camera_name}"
)
async def camera(
    camera_name: str,
    _user: dict = Depends(get_current_user),
):

    snapshot = (
        await read_dashboard_state()
    )

    cameras_data = snapshot.get(
        "cameras",
        {},
    )

    camera_data = cameras_data.get(
        camera_name
    )

    if camera_data is None:

        return JSONResponse(

            status_code=404,

            content={
                "error":
                    f"{camera_name} not found",
            },
        )

    return camera_data


# ==========================================================
# PRODUCTION
# ==========================================================

@api.get("/production")
async def production(_user: dict = Depends(get_current_user)):

    snapshot = (
        await read_dashboard_state()
    )

    cameras_data = (
        snapshot.get(
            "cameras",
            {},
        )
        or {}
    )

    # ----------------------------------------------------------
    # PERSISTENT PRODUCTION SOURCE OF TRUTH
    # ----------------------------------------------------------
    #
    # Do NOT use the live DashboardState totals for production
    # history. Those values are runtime state and can be zero
    # briefly while cameras are restarting.
    #
    # dbo.production_events contains one row per confirmed bag,
    # so get_production_summary() gives restart-safe totals.
    # (Printed / missing are counted from dbo.print_events inside
    # get_production_summary().)
    # ----------------------------------------------------------
    try:
        production_summary = await asyncio.to_thread(
            get_production_summary,
            None,
        )

        # Cameras switched off with include_bags_in_total /
        # include_prints_in_total are left out of these totals
        # (their own numbers still show per camera).
        bag_excluded, print_excluded = get_excluded_cameras(
            cameras_data
        )

        total, printed, missing = totals_from_summary(
            production_summary,
            bag_excluded,
            print_excluded,
        )

    except Exception as error:
        logger.warning(
            "Could not read canonical production totals "
            "from SQL Server; using live dashboard state: %s",
            error,
        )

        total = safe_int(
            snapshot.get(
                "total_count"
            )
        )

        printed = safe_int(
            snapshot.get(
                "total_printed_count",
                snapshot.get(
                    "total_printed_bags_count",
                    0,
                ),
            )
        )

        missing = safe_int(
            snapshot.get(
                "total_missing_count",
                snapshot.get(
                    "total_not_printed_bags_count",
                    0,
                ),
            )
        )

    jam_summary = (
        build_jam_summary(
            cameras_data
        )
    )

    return {

        "count_type":
            snapshot.get(
                "count_type",
                "roi",
            ),

        "total_bags":
            total,

        "printed_bags":
            printed,

        "not_printed_bags":
            missing,

        "print_quality":
            calculate_print_quality(
                printed,
                missing,
            ),

        "production_rate_per_hour":
            calculate_production_rate(
                snapshot
            ),

        # ==============================================
        # JAM SUMMARY
        # ==============================================

        "jam_status":
            jam_summary[
                "overall_status"
            ],

        "active_jams":
            jam_summary[
                "active_jams"
            ],

        "jam_cameras":
            jam_summary[
                "jam_cameras"
            ],

        "warning_cameras":
            jam_summary[
                "warning_cameras"
            ],

        "cameras":
            build_camera_production(
                cameras_data
            ),

        "updated_at":
            snapshot.get(
                "updated_at"
            ),
    }


# ==========================================================
# PRODUCTION SUMMARY (CANONICAL, SQL-DERIVED)
#
# Unlike /production above (which reflects the live in-memory
# DashboardState published by the running pipeline), this reads
# directly from dbo.production_events via
# database.repository.get_production_summary() -- the single
# source of truth for historical totals. Useful for
# reconciliation (comparing live vs. persisted totals) and for
# any consumer that wants SQL-accurate numbers even if the live
# pipeline/dashboard-state publisher is stale or restarting.
# ==========================================================

@api.get("/production/summary")
async def production_summary(
    camera_id: str | None = None,
    _user: dict = Depends(get_current_user),
):

    try:

        summary = await asyncio.to_thread(
            get_production_summary,
            camera_id,
        )

    except Exception as error:

        logger.warning(
            "Could not read production summary "
            "from SQL Server: %s",
            error,
        )

        return JSONResponse(
            status_code=503,
            content={
                "error":
                    "Production summary unavailable.",
            },
        )

    if camera_id is None and isinstance(summary, dict):

        snapshot = await read_dashboard_state()

        bag_excluded, print_excluded = get_excluded_cameras(
            snapshot.get("cameras")
        )

        if bag_excluded or print_excluded:

            total, printed, missing = totals_from_summary(
                summary,
                bag_excluded,
                print_excluded,
            )

            summary = dict(summary)
            summary["total_bags"] = total
            summary["total_printed"] = printed
            summary["total_missing"] = missing
            summary["bags_excluded_cameras"] = sorted(bag_excluded)
            summary["prints_excluded_cameras"] = sorted(print_excluded)

    return summary


# ==========================================================
# REPORT DATA
#
# Per-camera bags / printed / missing for a date-time range,
# used by the Reports page. Bags come from dbo.production_events,
# printed / missing from dbo.print_events.
# ==========================================================

@api.get("/api/report-data")
async def report_data(
    start: str,
    end: str,
    _user: dict = Depends(get_current_user),
):
    """Per-camera bags / printed / missing for the Reports page."""

    if parse_event_datetime(start) is None or parse_event_datetime(end) is None:
        raise HTTPException(status_code=400, detail="Invalid start/end.")

    try:
        summary = await asyncio.to_thread(get_report_summary, start, end)

    except Exception as error:
        logger.warning("Report data unavailable: %s", error)
        raise HTTPException(status_code=503, detail="Report data unavailable.")

    snapshot = await read_dashboard_state()

    bag_ex, print_ex = get_excluded_cameras(snapshot.get("cameras"))

    total, printed, missing = totals_from_summary(summary, bag_ex, print_ex)

    return {
        **summary,
        "total_bags": total,
        "total_printed": printed,
        "total_missing": missing,
    }


# ==========================================================
# BATCH COUNTER
#
# A separate counter: the operator feeds a count limit, and the
# dashboard shows how many bags were counted since then
# (restart-safe: derived from production_events).
# ==========================================================

class BatchCounterRequest(BaseModel):
    target: int
    camera: str | None = "all"


BATCH_COUNTER_MAX_TARGET = 10_000_000


@api.get("/api/batch-counter")
async def batch_counter_get(_user: dict = Depends(get_current_user)):

    try:
        batch_state = await read_dashboard_state()

        batch_bag_excluded, batch_print_excluded = (
            get_excluded_cameras(batch_state.get("cameras"))
        )

        counter = await asyncio.to_thread(
            get_batch_counter,
            sorted(batch_bag_excluded),
            sorted(batch_print_excluded),
        )

    except Exception as error:
        logger.warning("Could not read batch counter: %s", error)
        raise HTTPException(status_code=503, detail="Batch counter unavailable.")

    if counter is None or not counter["active"]:
        return {"active": False}

    return counter


@api.post("/api/batch-counter")
async def batch_counter_start(
    payload: BatchCounterRequest,
    _user: dict = Depends(get_current_user),
):

    if payload.target < 1 or payload.target > BATCH_COUNTER_MAX_TARGET:
        raise HTTPException(
            status_code=400,
            detail=f"Count limit must be between 1 and {BATCH_COUNTER_MAX_TARGET}.",
        )

    try:
        return await asyncio.to_thread(
            start_batch_counter,
            payload.target,
            payload.camera,
        )

    except Exception as error:
        logger.error("Could not start batch counter: %s", error)
        raise HTTPException(status_code=503, detail="Could not start batch counter.")


@api.post("/api/batch-counter/stop")
async def batch_counter_stop(_user: dict = Depends(get_current_user)):

    try:
        await asyncio.to_thread(stop_batch_counter)

    except Exception as error:
        logger.error("Could not stop batch counter: %s", error)
        raise HTTPException(status_code=503, detail="Could not stop batch counter.")

    return {"active": False}


# ==========================================================
# JAM MONITORING API
#
# NEW ENDPOINT:
#
# GET /jams
# ==========================================================

@api.get("/jams")
async def jams(_user: dict = Depends(get_current_user)):

    snapshot = (
        await read_dashboard_state()
    )

    cameras_data = (
        snapshot.get(
            "cameras",
            {},
        )
        or {}
    )

    summary = (
        build_jam_summary(
            cameras_data
        )
    )

    return {

        **summary,

        "generated_at":
            utc_now_iso(),
    }


# ==========================================================
# JAM EVENTS -- ACTIVE (CANONICAL, SQL-DERIVED)
#
# Unlike /jams above (which summarizes jam_status strings from
# the live DashboardState), this reads ACTIVE rows directly
# from dbo.jam_events. Because Condition A/B/C are independent
# (see database/repository.py's start_jam_event() docstring), a
# single camera can legitimately have more than one entry here
# at once -- e.g. a spacing jam and an occupancy jam together.
# ==========================================================

@api.get("/jams/active")
async def jams_active(_user: dict = Depends(get_current_user)):

    try:

        events = await asyncio.to_thread(
            get_active_jam_events
        )

    except Exception as error:

        logger.warning(
            "Could not read active jam events "
            "from SQL Server: %s",
            error,
        )

        return JSONResponse(
            status_code=503,
            content={
                "error":
                    "Active jam events unavailable.",
            },
        )

    return {
        "active_jams": events,
        "generated_at": utc_now_iso(),
    }


# ==========================================================
# JAM EVENTS -- RECENT (CANONICAL, SQL-DERIVED)
# ==========================================================

@api.get("/jams/recent")
async def jams_recent(
    camera_id: str | None = None,
    limit: int = Query(default=100, le=1000),
    hours: int = Query(default=24, le=24 * 30),
    _user: dict = Depends(get_current_user),
):

    try:

        events = await asyncio.to_thread(
            get_recent_jam_events,
            camera_id,
            limit,
            hours,
        )

    except Exception as error:

        logger.warning(
            "Could not read recent jam events "
            "from SQL Server: %s",
            error,
        )

        return JSONResponse(
            status_code=503,
            content={
                "error":
                    "Recent jam events unavailable.",
            },
        )

    return {
        "jams": events,
        "generated_at": utc_now_iso(),
    }


# ==========================================================
# PRINT EVENTS -- RECENT (CANONICAL, SQL-DERIVED)
# ==========================================================

@api.get("/print-events/recent")
async def print_events_recent(
    camera_id: str | None = None,
    limit: int = Query(default=500, le=5000),
    _user: dict = Depends(get_current_user),
):

    try:

        events = await asyncio.to_thread(
            get_recent_print_events,
            camera_id,
            limit,
        )

    except Exception as error:

        logger.warning(
            "Could not read recent print events "
            "from SQL Server: %s",
            error,
        )

        return JSONResponse(
            status_code=503,
            content={
                "error":
                    "Recent print events unavailable.",
            },
        )

    return {
        "print_events": events,
        "generated_at": utc_now_iso(),
    }


# ==========================================================
# EVENTS
# ==========================================================

@api.get("/events")
async def events(
    limit: int = Query(
        default=100,
        ge=1,
        le=10000,
    ),
    camera: str | None = Query(
        default=None
    ),
    print_status: str | None = Query(
        default=None
    ),
    shift: str | None = Query(
        default=None
    ),
    start: str | None = Query(
        default=None
    ),
    end: str | None = Query(
        default=None
    ),
    _user: dict = Depends(get_current_user),
):

    all_events = (
        await read_count_events()
    )

    # "all" / "" mean "no filter" - both the UI's default
    # option value and an empty string should be treated
    # as not filtering at all.

    def _is_wildcard(value):

        return (
            value is None
            or value.strip() == ""
            or value.strip().lower() == "all"
        )

    if not _is_wildcard(camera):

        camera_lower = (
            camera
            .strip()
            .lower()
        )

        all_events = [

            event

            for event in all_events

            if str(
                event.get(
                    "camera",
                    "",
                )
            ).lower()
            ==
            camera_lower
        ]

    if not _is_wildcard(print_status):

        requested_status = (
            print_status
            .strip()
            .lower()
        )

        all_events = [

            event

            for event in all_events

            if str(
                event.get(
                    "print_status",
                    "",
                )
            ).lower()
            ==
            requested_status
        ]

    if not _is_wildcard(shift):

        requested_shift = (
            shift
            .strip()
            .lower()
        )

        all_events = [

            event

            for event in all_events

            if str(
                event.get(
                    "shift",
                    "",
                )
            ).lower()
            ==
            requested_shift
        ]

    start_time = (
        parse_event_datetime(start)
        if not _is_wildcard(start)
        else None
    )

    end_time = (
        parse_event_datetime(end)
        if not _is_wildcard(end)
        else None
    )

    if start_time is not None or end_time is not None:

        filtered_events = []

        for event in all_events:

            event_time = parse_event_datetime(
                event.get("timestamp")
            )

            if event_time is None:
                continue

            if (
                start_time is not None
                and event_time < start_time
            ):
                continue

            if (
                end_time is not None
                and event_time > end_time
            ):
                continue

            filtered_events.append(event)

        all_events = filtered_events

    all_events.reverse()

    limited_events = (
        all_events[
            :limit
        ]
    )

    return {

        "count":
            len(
                limited_events
            ),

        "events":
            limited_events,
    }


# ==========================================================
# EVENTS CSV EXPORT
# ==========================================================

@api.get("/events/export")
async def export_events(_user: dict = Depends(get_current_user)):

    events_data = (
        await read_count_events()
    )

    output = io.StringIO()

    fieldnames = [

        "timestamp",

        "event",

        "camera",

        "total_count",

        "track_id",

        "center_x",

        "center_y",

        "print_present",

        "print_status",

        "printed_count",

        "missing_count",

        "shift",

        "jam_status",

        "jam_detected",
    ]

    writer = csv.DictWriter(
        output,
        fieldnames=fieldnames,
        extrasaction="ignore",
    )

    writer.writeheader()

    for event in events_data:

        writer.writerow(
            event
        )

    csv_data = (
        output.getvalue()
    )

    output.close()

    filename = (
        "fillpac_events_"
        +
        utc_now().strftime(
            "%Y%m%d_%H%M%S"
        )
        +
        ".csv"
    )

    return StreamingResponse(

        iter(
            [
                csv_data
            ]
        ),

        media_type="text/csv",

        headers={
            "Content-Disposition":
                (
                    "attachment; "
                    f'filename="{filename}"'
                )
        },
    )


# ==========================================================
# ANALYTICS
# ==========================================================

@api.get("/analytics")
async def analytics(_user: dict = Depends(get_current_user)):

    events_data = (
        await read_count_events()
    )

    # Cameras switched off with include_bags_in_total /
    # include_prints_in_total are left out of bag totals /
    # print totals.
    analytics_state = await read_dashboard_state()

    bag_excluded, print_excluded = get_excluded_cameras(
        analytics_state.get("cameras")
    )

    total_events = 0

    printed_events = 0

    missing_events = 0

    unknown_events = 0

    hourly = defaultdict(
        lambda: {
            "total": 0,
            "printed": 0,
            "missing": 0,
        }
    )

    by_camera = defaultdict(
        lambda: {
            "total": 0,
            "printed": 0,
            "missing": 0,
        }
    )

    by_shift = defaultdict(
        lambda: {
            "total": 0,
            "printed": 0,
            "missing": 0,
        }
    )

    missing_statuses = {
        "missing",
        "not_printed",
        "not-printed",
    }

    def add_to_bucket(bucket, count_bag, count_print, status):

        if count_bag:
            bucket["total"] += 1

        if count_print:

            if status == "printed":
                bucket["printed"] += 1

            elif status in missing_statuses:
                bucket["missing"] += 1

    for event in events_data:

        status = str(
            event.get(
                "print_status",
                "unknown",
            )
        ).lower()

        camera_name = str(
            event.get(
                "camera",
                "Unknown",
            )
        )

        shift = str(
            event.get(
                "shift",
                "unknown",
            )
        )

        event_time = (
            parse_event_datetime(
                event.get(
                    "timestamp"
                )
            )
        )

        count_bag = camera_name not in bag_excluded
        count_print = camera_name not in print_excluded

        if not count_bag and not count_print:
            continue

        if count_bag:
            total_events += 1

        if count_print:

            if status == "printed":
                printed_events += 1

            elif status in missing_statuses:
                missing_events += 1

            else:
                unknown_events += 1

        add_to_bucket(
            by_camera[camera_name],
            count_bag,
            count_print,
            status,
        )

        add_to_bucket(
            by_shift[shift],
            count_bag,
            count_print,
            status,
        )

        if event_time is not None:

            add_to_bucket(
                hourly[
                    event_time.strftime("%Y-%m-%d %H:00")
                ],
                count_bag,
                count_print,
                status,
            )

    # ------------------------------------------------------
    # Printed / missing come from dbo.print_events (one row
    # per print result). The production event rows do not
    # carry a reliable print_status, so recount from here.
    # ------------------------------------------------------

    try:
        print_rows = await asyncio.to_thread(
            get_recent_print_events, None, 5000
        )

    except Exception as error:
        logger.warning("Analytics: print events unavailable: %s", error)
        print_rows = None

    if print_rows is not None:

        printed_events = 0
        missing_events = 0

        for bucket_map in (hourly, by_camera, by_shift):
            for bucket in bucket_map.values():
                bucket["printed"] = 0
                bucket["missing"] = 0

        for row in print_rows:

            cam = str(row.get("camera_id") or "Unknown")

            if cam in print_excluded:
                continue

            result = str(row.get("result") or "").lower()

            if result == "printed":
                key = "printed"
                printed_events += 1

            elif result in missing_statuses:
                key = "missing"
                missing_events += 1

            else:
                continue

            stamp = row.get("timestamp")

            by_camera[cam][key] += 1
            by_shift[determine_shift(stamp)][key] += 1

            when = parse_event_datetime(stamp)

            if when is not None:
                hourly[when.strftime("%Y-%m-%d %H:00")][key] += 1

    hourly_result = []

    for hour in sorted(
        hourly.keys()
    ):

        bucket = hourly[
            hour
        ]

        hourly_result.append(
            {
                "hour":
                    hour,

                "total":
                    bucket[
                        "total"
                    ],

                "printed":
                    bucket[
                        "printed"
                    ],

                "missing":
                    bucket[
                        "missing"
                    ],

                "print_quality":
                    calculate_print_quality(
                        bucket[
                            "printed"
                        ],
                        bucket[
                            "missing"
                        ],
                    ),
            }
        )

    camera_result = []

    for camera_name in sorted(
        by_camera.keys()
    ):

        bucket = (
            by_camera[
                camera_name
            ]
        )

        camera_result.append(
            {
                "camera":
                    camera_name,

                "total":
                    bucket[
                        "total"
                    ],

                "printed":
                    bucket[
                        "printed"
                    ],

                "missing":
                    bucket[
                        "missing"
                    ],

                "print_quality":
                    calculate_print_quality(
                        bucket[
                            "printed"
                        ],
                        bucket[
                            "missing"
                        ],
                    ),
            }
        )

    shift_result = []

    for shift in [
        "shift-a",
        "shift-b",
        "shift-c",
        "unknown",
    ]:

        if shift not in by_shift:
            continue

        bucket = (
            by_shift[
                shift
            ]
        )

        shift_result.append(
            {
                "shift":
                    shift,

                "total":
                    bucket[
                        "total"
                    ],

                "printed":
                    bucket[
                        "printed"
                    ],

                "missing":
                    bucket[
                        "missing"
                    ],

                "print_quality":
                    calculate_print_quality(
                        bucket[
                            "printed"
                        ],
                        bucket[
                            "missing"
                        ],
                    ),
            }
        )

    # ------------------------------------------------------
    # Current jam information comes from the live dashboard
    # state, NOT count event history.
    # ------------------------------------------------------

    snapshot = (
        await read_dashboard_state()
    )

    jam_summary = (
        build_jam_summary(
            snapshot.get(
                "cameras",
                {},
            )
            or {}
        )
    )

    # ------------------------------------------------------
    # CONDITION C ANALYTICS
    #
    # Each saved event represents one ROI occupancy jam
    # capture (ROIOccupancyDetector only saves on jam).
    # ------------------------------------------------------

    condition_c_events = (
        await read_condition_c_events()
    )

    roi_jam_events = len(
        condition_c_events
    )

    roi_bag_counts = [
        safe_int(
            event.get(
                "bag_count",
                0,
            )
        )
        for event in condition_c_events
    ]

    roi_gap_values = [
        event.get(
            "minimum_gap_mm"
        )
        for event in condition_c_events
        if event.get(
            "minimum_gap_mm"
        )
        is not None
    ]

    average_roi_bag_count = (
        safe_round(
            sum(roi_bag_counts)
            /
            len(roi_bag_counts)
        )
        if roi_bag_counts
        else 0.0
    )

    average_roi_gap_mm = (
        safe_round(
            sum(roi_gap_values)
            /
            len(roi_gap_values)
        )
        if roi_gap_values
        else None
    )

    return {

        "total_events":
            total_events,

        "printed_events":
            printed_events,

        "missing_events":
            missing_events,

        "unknown_events":
            unknown_events,

        "print_quality":
            calculate_print_quality(
                printed_events,
                missing_events,
            ),

        "hourly":
            hourly_result,

        "by_camera":
            camera_result,

        "by_shift":
            shift_result,

        # ==============================================
        # CURRENT JAM ANALYTICS
        # ==============================================

        "jam":
            jam_summary,

        # ==============================================
        # CONDITION C ANALYTICS
        # ==============================================

        "roi_jam_events":
            roi_jam_events,

        "average_roi_bag_count":
            average_roi_bag_count,

        "average_roi_gap_mm":
            average_roi_gap_mm,

        "generated_at":
            utc_now_iso(),
    }


# ==========================================================
# CONFIG
# ==========================================================

@api.get("/config")
async def config(_user: dict = Depends(get_current_user)):

    return (
        read_safe_config()
    )


# ==========================================================
# SETTINGS (full read/write access to config.yaml)
# ==========================================================
#
# /config above is a reduced, read-only view used by the live
# dashboard. These routes expose and edit the COMPLETE
# config.yaml so the Settings page never has to guess at a
# field it doesn't explicitly list.
#
# IMPORTANT -- saving here does NOT change the behaviour of an
# already-running FillPac AI process. Every detector
# (JamDetector, BagSpacingDetector, EntryROICounter, etc.)
# copies its thresholds out of config.yaml exactly once, inside
# Pipeline.__init__, when the process starts. A saved change
# takes effect the next time the FillPac AI service is
# restarted -- every response below says so explicitly via
# "applied_live": False so the frontend can show that to the
# operator instead of implying the change is already live.

@api.get("/api/settings")
async def get_settings(
    _user: dict = Depends(require_admin),
):

    try:
        data = CONFIG_MANAGER.load()

    except FileNotFoundError as error:
        raise HTTPException(
            status_code=404,
            detail=str(error),
        )

    except ConfigValidationError as error:
        raise HTTPException(
            status_code=500,
            detail=str(error),
        )

    return {
        "config": data,
        "last_modified": (
            datetime.fromtimestamp(
                CONFIG_FILE.stat().st_mtime,
                tz=timezone.utc,
            ).isoformat()
            if CONFIG_FILE.exists()
            else None
        ),
    }


@api.post("/api/settings/validate")
async def validate_settings(
    payload: dict,
    _user: dict = Depends(require_admin),
):
    """
    Validate a candidate config WITHOUT writing it -- backs the
    [Validate] button so an admin can check edits are legal
    before committing to [Save].
    """

    try:
        CONFIG_MANAGER.validate(payload)

    except ConfigValidationError as error:
        return {
            "valid": False,
            "error": str(error),
        }

    return {
        "valid": True,
        "error": None,
    }


@api.post("/api/settings/database/test")
async def test_database_settings(
    payload: dict,
    _user: dict = Depends(require_admin),
):
    """
    Try a connection with the database: settings currently in
    the Settings form (NOT yet saved). Backs the [Test Database]
    button so an admin can confirm the server/driver/credentials
    work BEFORE saving -- a bad database: section blocks login
    after the next restart, because sessions live in SQL Server.
    """

    result = await asyncio.to_thread(
        test_database_config,
        payload,
    )

    return result


@api.put("/api/settings")
async def put_settings(
    payload: dict,
    _user: dict = Depends(require_admin),
):

    try:
        result = CONFIG_MANAGER.save(payload)

    except ConfigValidationError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        )

    logger.info(
        "config.yaml saved via /api/settings (%d field(s) "
        "changed, backup: %s)",
        len(result["changes"]),
        result["backup_path"],
    )

    live_applied = await asyncio.to_thread(
        apply_live_settings
    )

    return {
        "saved": True,
        "applied_live": False,
        "live_applied": live_applied,
        "message": (
            "Settings saved. The count type and the production "
            "switches (counting, bags in total, prints in total) "
            "are already live; restart the FillPac AI service to "
            "apply everything else (including AI on/off per "
            "camera)."
        ),
        "backup_path": result["backup_path"],
        "changes": result["changes"],
        "comments_preserved": result.get(
            "comments_preserved",
            False,
        ),
    }


@api.post("/api/settings/reload")
async def reload_settings(
    _user: dict = Depends(require_admin),
):
    """
    Confirms config.yaml on disk is currently valid and
    loadable -- the same check the AI pipeline runs at startup.
    Does NOT push values into the running pipeline; see the
    note above the /api/settings routes.
    """

    try:
        data = CONFIG_MANAGER.load()
        CONFIG_MANAGER.validate(data)

    except (FileNotFoundError, ConfigValidationError) as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        )

    return {
        "valid": True,
        "applied_live": False,
        "message": (
            "config.yaml is valid. Restart the FillPac AI "
            "service to apply it."
        ),
    }


@api.get("/api/settings/backups")
async def list_settings_backups(
    _user: dict = Depends(require_admin),
):

    return {
        "backups": CONFIG_MANAGER.list_backups(),
    }


@api.post("/api/settings/reset")
async def reset_settings(
    backup: str | None = Query(default=None),
    _user: dict = Depends(require_admin),
):
    """
    Restore config.yaml from a backup written by a previous
    /api/settings save. Defaults to the most recent backup
    (i.e. "undo my last save") when `backup` is not given.
    """

    backups = CONFIG_MANAGER.list_backups()

    if not backups:
        raise HTTPException(
            status_code=404,
            detail="No config.yaml backups exist yet.",
        )

    target = backup or backups[0]

    try:
        data = CONFIG_MANAGER.restore_backup(target)

    except FileNotFoundError as error:
        raise HTTPException(
            status_code=404,
            detail=str(error),
        )

    except ConfigValidationError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        )

    logger.info(
        "config.yaml restored from backup: %s",
        target,
    )

    return {
        "restored": True,
        "applied_live": False,
        "live_applied": await asyncio.to_thread(
            apply_live_settings
        ),
        "restored_from": target,
        "config": data,
        "message": (
            "config.yaml restored. Restart the FillPac AI "
            "service to apply it."
        ),
    }


# ==========================================================
# COUNT TYPE (dashboard switch: ROI counting / line counting)
# ==========================================================

class CountTypeRequest(BaseModel):
    count_type: str


@api.get("/api/count-type")
async def get_count_type_route(
    _user: dict = Depends(get_current_user),
):

    live_state = get_live_dashboard_state()

    if live_state is not None:
        return {
            "count_type": live_state.get_count_type(),
            "applied_live": True,
        }

    snapshot = await read_dashboard_state()

    return {
        "count_type": snapshot.get("count_type", "roi"),
        "applied_live": False,
    }


@api.put("/api/count-type")
async def set_count_type_route(
    payload: CountTypeRequest,
    user: dict = Depends(get_current_user),
):
    """
    Switch what the dashboard counts show: "roi" (entry ROI
    counting) or "line" (counting-line crossing).

    Applies to the running system immediately. For an
    administrator the choice is also written to config.yaml
    (dashboard.default_count_type) so it survives a restart.
    """

    try:
        wanted = Config.normalize_count_type(payload.count_type)

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        )

    live_state = get_live_dashboard_state()

    if live_state is not None:
        live_state.set_count_type(wanted)

    persisted = False
    persist_error = None

    if (user.get("role") or "").lower() == "admin":

        try:
            data = CONFIG_MANAGER.load()

            dashboard_section = data.get("dashboard")

            if not isinstance(dashboard_section, dict):
                dashboard_section = {}
                data["dashboard"] = dashboard_section

            if dashboard_section.get("default_count_type") != wanted:
                dashboard_section["default_count_type"] = wanted
                await asyncio.to_thread(CONFIG_MANAGER.save, data)

            persisted = True

        except Exception as error:
            persist_error = str(error)
            logger.warning(
                "Could not save count type to config.yaml: %s",
                error,
            )

    return {
        "count_type": wanted,
        "applied_live": live_state is not None,
        "persisted": persisted,
        "persist_error": persist_error,
    }


# ==========================================================
# LIVE PIPELINE REGISTRATION
# ==========================================================

def register_live_pipeline(
    camera_name,
    pipeline,
):

    if not camera_name:
        return

    if pipeline is None:
        return

    LIVE_PIPELINES[
        str(
            camera_name
        )
    ] = pipeline

    logger.info(
        "Registered live pipeline: %s",
        camera_name,
    )


def unregister_live_pipeline(
    camera_name,
):

    LIVE_PIPELINES.pop(
        str(
            camera_name
        ),
        None,
    )


# ==========================================================
# GET PIPELINE FRAME
# ==========================================================

def get_pipeline_frame(
    pipeline,
):

    if pipeline is None:
        return None

    getter = getattr(
        pipeline,
        "get_latest_frame",
        None,
    )

    if callable(
        getter
    ):

        try:

            frame = getter()

            if frame is None:
                return None

            return frame.copy()

        except Exception:
            return None

    frame_lock = getattr(
        pipeline,
        "_frame_lock",
        None,
    )

    if frame_lock is None:
        return None

    try:

        with frame_lock:

            frame = getattr(
                pipeline,
                "_latest_frame",
                None,
            )

            if frame is None:
                return None

            return frame.copy()

    except Exception:

        return None


# ==========================================================
# LIVE FRAME GENERATOR
# ==========================================================

async def live_frame_generator(
    camera_name,
):

    try:

        import cv2

    except ImportError:

        logger.error(
            "OpenCV unavailable for live monitor."
        )

        return

    while True:

        pipeline = (
            LIVE_PIPELINES.get(
                camera_name
            )
        )

        if pipeline is None:

            await asyncio.sleep(
                1.0
            )

            continue

        frame = (
            get_pipeline_frame(
                pipeline
            )
        )

        if frame is None:

            await asyncio.sleep(
                LIVE_FRAME_INTERVAL
            )

            continue

        try:

            success, encoded = (
                cv2.imencode(
                    ".jpg",
                    frame,
                    [
                        cv2.IMWRITE_JPEG_QUALITY,
                        75,
                    ],
                )
            )

        except Exception:

            success = False

            encoded = None

        if not success:

            await asyncio.sleep(
                LIVE_FRAME_INTERVAL
            )

            continue

        frame_bytes = (
            encoded.tobytes()
        )

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n"
            +
            frame_bytes
            +
            b"\r\n"
        )

        await asyncio.sleep(
            LIVE_FRAME_INTERVAL
        )


# ==========================================================
# LIVE CAMERA
# ==========================================================

@api.get(
    "/live/{camera_name}"
)
async def live_camera(
    camera_name: str,
):

    if (
        camera_name
        not in LIVE_PIPELINES
    ):

        return JSONResponse(

            status_code=404,

            content={
                "error":
                    (
                        "Live pipeline is not "
                        "registered for "
                        f"{camera_name}."
                    ),

                "camera":
                    camera_name,

                "live_available":
                    False,
            },
        )

    return StreamingResponse(

        live_frame_generator(
            camera_name
        ),

        media_type=(
            "multipart/x-mixed-replace; "
            "boundary=frame"
        ),
    )


# ==========================================================
# CONDITION C - ROI SNAPSHOT IMAGE
# ==========================================================

@api.get(
    "/condition-c/image/{filename}"
)
async def condition_c_image(
    filename: str,
):

    safe_name = Path(
        filename
    ).name

    if not safe_name:

        return JSONResponse(

            status_code=404,

            content={
                "error":
                    "Invalid image filename."
            },
        )

    file_path = (
        CONDITION_C_IMAGES_DIR
        /
        safe_name
    )

    if (
        not file_path.exists()
        or
        not file_path.is_file()
    ):

        return JSONResponse(

            status_code=404,

            content={
                "error":
                    "ROI snapshot not found.",

                "filename":
                    safe_name,
            },
        )

    return FileResponse(
        file_path,
        media_type="image/jpeg",
    )


# ==========================================================
# SOCKET CONNECT
# ==========================================================

@sio.event
async def connect(
    sid,
    environ,
    auth=None,
):

    token = (auth or {}).get("token") if isinstance(auth, dict) else None

    user = (
        await asyncio.to_thread(get_session_user, token)
        if token
        else None
    )

    if user is None:
        logger.warning(
            "Rejected Socket.IO connection %s: missing/invalid "
            "auth token.",
            sid,
        )
        raise socketio.exceptions.ConnectionRefusedError(
            "Unauthorized"
        )

    await asyncio.to_thread(touch_session, token)

    logger.info(
        "Dashboard connected: %s (user=%s)",
        sid,
        user["username"],
    )

    snapshot = (
        await read_dashboard_state()
    )

    await sio.emit(
        "dashboard_state",
        snapshot,
        to=sid,
    )


# ==========================================================
# SOCKET DISCONNECT
# ==========================================================

@sio.event
async def disconnect(
    sid,
):

    logger.info(
        "Dashboard disconnected: %s",
        sid,
    )


# ==========================================================
# STATE WATCHER
# ==========================================================

async def dashboard_state_watcher():

    logger.info(
        "Dashboard state watcher started."
    )

    logger.info(
        "Watching SQL Server: dbo.system_state / "
        "dbo.camera_status"
    )

    last_signature = None

    while True:

        try:

            snapshot = (
                await read_dashboard_state()
            )

            signature = (

                snapshot.get(
                    "updated_at"
                ),

                len(
                    snapshot.get(
                        "cameras",
                        {},
                    )
                    or {}
                ),
            )

            if (
                snapshot.get(
                    "system_status"
                )
                != "error"
                and
                (
                    last_signature is None
                    or
                    signature
                    !=
                    last_signature
                )
            ):

                await sio.emit(
                    "dashboard_state",
                    snapshot,
                )

                last_signature = (
                    signature
                )

        except asyncio.CancelledError:

            raise

        except Exception:

            logger.exception(
                "Dashboard watcher error."
            )

        await asyncio.sleep(
            STATE_WATCH_INTERVAL
        )


# ==========================================================
# STARTUP
# ==========================================================

@api.on_event(
    "startup"
)
async def startup_event():

    logger.info(
        "Starting FillPac AI Dashboard API."
    )

    logger.info(
        "Project root: %s",
        PROJECT_ROOT,
    )

    logger.info(
        "State source: SQL Server "
        "(dbo.system_state / dbo.camera_status)",
    )

    logger.info(
        "Config file: %s",
        CONFIG_FILE,
    )

    index_file = (
        FRONTEND_DIR
        / "index.html"
    )

    logger.info(
        "Frontend dir: %s (index.html found: %s)",
        FRONTEND_DIR,
        index_file.exists(),
    )

    # Ensure there is at least one login account so the
    # dashboard isn't locked out on a fresh SQL Server database.
    # No-ops once any user exists.
    try:
        await asyncio.to_thread(ensure_default_admin_user)
    except Exception:
        logger.exception(
            "Could not ensure a default admin user exists "
            "(SQL Server may be unreachable at startup -- "
            "login will not work until this succeeds)."
        )

    api.state.dashboard_watcher = (
        asyncio.create_task(
            dashboard_state_watcher()
        )
    )


# ==========================================================
# SHUTDOWN
# ==========================================================

@api.on_event(
    "shutdown"
)
async def shutdown_event():

    watcher = getattr(
        api.state,
        "dashboard_watcher",
        None,
    )

    if watcher is not None:

        watcher.cancel()

        try:

            await watcher

        except asyncio.CancelledError:

            pass

    logger.info(
        "FillPac AI Dashboard API stopped."
    )


# ==========================================================
# ASGI APPLICATION
#
# Start with:
#
# uvicorn dashboard.backend.server:app
#
# NOT:
#
# uvicorn dashboard.backend.server:api
# ==========================================================

app = socketio.ASGIApp(
    sio,
    api,
)