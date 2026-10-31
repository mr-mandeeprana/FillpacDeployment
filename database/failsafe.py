"""
==========================================================
FillPac AI
SQL Server Write Failover Queue
==========================================================

Purpose
-------
Every write in database/repository.py (production counts,
print results, jam events, dashboard state, application logs)
used to be wrapped in a try/except at the call site in
pipeline.py / count_logger.py / dashboard.py that simply
logged the error and moved on. If SQL Server was unreachable
for any reason -- including the connection error 4060 seen
previously -- the event itself was gone for good.

This module gives every write a local safety net: if SQL
Server is unreachable, the call is appended to a small
JSON-lines queue file on local disk instead of being dropped.
A background thread periodically retries queued writes against
SQL Server and removes each entry once it succeeds.

Nothing here changes behavior while SQL Server is healthy --
this only activates on failure, and callers keep seeing the
same exceptions/log lines they always have (see
database/repository.py's _resilient_write decorator).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


QUEUE_DIR = Path("logs") / "sql_failover"
QUEUE_PATH = QUEUE_DIR / "pending_writes.jsonl"

# How often the background thread retries whatever is queued.
_REPLAY_INTERVAL_SECONDS = 30

# After this many failed replay attempts for a single entry
# (~4 hours at the default interval), stop retrying it and log
# it as abandoned rather than queuing forever. The full entry
# (op + kwargs) is logged at that point so it isn't silently
# lost even then.
_MAX_ATTEMPTS_PER_ENTRY = 500

_queue_lock = threading.Lock()
_replay_thread_lock = threading.Lock()
_replay_thread_started = False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def enqueue(op: str, kwargs: dict[str, Any]) -> None:
    """
    Append one failed write to the local failover queue.

    Raises if even the local disk write fails -- the caller
    (database/repository.py's _resilient_write) treats that as
    a genuine data-loss event and logs it as such, since there
    is nowhere left to put the event.
    """

    QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    entry = {
        "op": op,
        "kwargs": kwargs,
        "queued_at": _utc_now_iso(),
        "attempts": 0,
    }

    line = json.dumps(entry, ensure_ascii=False, default=str)

    with _queue_lock:
        with open(QUEUE_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    _ensure_replay_thread_started()


def pending_count() -> int:
    """
    Number of writes currently waiting to be replayed. Safe to
    call from the dashboard/health-check code to surface "N
    events waiting on SQL Server" to an operator.
    """

    with _queue_lock:
        if not QUEUE_PATH.exists():
            return 0

        with open(QUEUE_PATH, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())


def replay_pending(max_entries: int = 500) -> tuple[int, int]:
    """
    Attempt to re-apply queued writes to SQL Server.

    Returns (succeeded, still_pending). Safe to call even when
    the queue file doesn't exist or SQL Server is still down --
    entries that fail again simply stay queued for the next
    call.
    """

    from . import repository  # local import: avoids circular import

    with _queue_lock:

        if not QUEUE_PATH.exists():
            return (0, 0)

        with open(QUEUE_PATH, "r", encoding="utf-8") as f:
            raw_lines = [line for line in f if line.strip()]

        entries = []

        for line in raw_lines:
            try:
                entries.append(json.loads(line))
            except (TypeError, ValueError):
                logger.error(
                    "Dropping unreadable SQL failover queue "
                    "entry (corrupt JSON): %r",
                    line[:200],
                )

        remaining = []
        succeeded = 0

        for entry in entries[:max_entries]:

            op = entry.get("op")
            kwargs = entry.get("kwargs") or {}

            func = getattr(repository, op, None)
            raw_func = getattr(func, "_raw", None) if func else None

            if raw_func is None:
                logger.error(
                    "Dropping SQL failover queue entry for "
                    "unknown/unresolvable operation: %s", op,
                )
                continue

            try:
                raw_func(**kwargs)
                succeeded += 1

            except Exception as error:

                entry["attempts"] = int(entry.get("attempts", 0)) + 1
                entry["last_error"] = str(error)

                if entry["attempts"] >= _MAX_ATTEMPTS_PER_ENTRY:
                    logger.error(
                        "Abandoning SQL failover entry after %s "
                        "failed replay attempts | op=%s | "
                        "kwargs=%s | last_error=%s",
                        entry["attempts"], op, kwargs, error,
                    )
                    continue

                remaining.append(entry)

        # Anything past max_entries this round wasn't attempted;
        # keep it queued as-is.
        remaining.extend(entries[max_entries:])

        _write_queue_atomic(remaining)

        if succeeded:
            logger.info(
                "SQL failover: replayed %s queued write(s); "
                "%s still pending.",
                succeeded, len(remaining),
            )

        return (succeeded, len(remaining))


def _write_queue_atomic(entries: list[dict]) -> None:
    """
    Rewrite the queue file to contain exactly `entries`, via a
    temp file + atomic rename so a crash mid-write can't corrupt
    or truncate the queue.
    """

    QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    if not entries:
        if QUEUE_PATH.exists():
            QUEUE_PATH.unlink()
        return

    fd, tmp_path = tempfile.mkstemp(dir=QUEUE_DIR, suffix=".tmp")

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(
                    json.dumps(entry, ensure_ascii=False, default=str)
                    + "\n"
                )
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, QUEUE_PATH)

    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _replay_loop(interval_seconds: float) -> None:

    while True:

        time.sleep(interval_seconds)

        try:
            replay_pending()
        except Exception:
            logger.exception(
                "Unexpected error in SQL failover replay loop "
                "(will retry on the next interval)."
            )


def _ensure_replay_thread_started() -> None:

    global _replay_thread_started

    with _replay_thread_lock:

        if _replay_thread_started:
            return

        thread = threading.Thread(
            target=_replay_loop,
            args=(_REPLAY_INTERVAL_SECONDS,),
            name="sql-failover-replay",
            daemon=True,
        )
        thread.start()

        _replay_thread_started = True

        logger.info(
            "Started SQL failover replay thread (interval=%ss).",
            _REPLAY_INTERVAL_SECONDS,
        )


# Start the replay thread as soon as this module is imported
# (i.e. as soon as the app starts up), not just after the first
# new failure -- this also picks up and replays any entries left
# over in the queue file from before a restart or crash.
_ensure_replay_thread_started()