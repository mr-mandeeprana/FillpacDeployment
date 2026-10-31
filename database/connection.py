from __future__ import annotations

import os
import struct
import sys
import threading
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pyodbc

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


# ==========================================================
# CONFIG FILE LOCATION
# ==========================================================
#
# Source run:   database/connection.py -> project root is one
#               level up.
# Frozen EXE:   the folder containing the EXE (config.yaml sits
#               beside it, NOT inside the PyInstaller bundle).
#
# FILLPAC_CONFIG_FILE can point at a different config.yaml
# (e.g. for tests); by default the project-root file that the
# dashboard Settings page edits is used.

if getattr(sys, "frozen", False):

    PROJECT_ROOT = Path(sys.executable).resolve().parent

else:

    PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_FILE = Path(
    os.getenv(
        "FILLPAC_CONFIG_FILE",
        str(PROJECT_ROOT / "config.yaml"),
    )
).resolve()

# SQL-auth password is NEVER stored in config.yaml (the Settings
# page would show it in plain text). Only used when
# database.authentication.trusted_connection is false.
DB_PASSWORD_ENV = "FILLPAC_DB_PASSWORD"


# ==========================================================
# DATABASE CONFIG (config.yaml -> database:)
# ==========================================================

_config_lock = threading.Lock()
_cached_config: dict[str, Any] | None = None


def _as_bool(value: Any, field: str) -> bool:

    if isinstance(value, bool):
        return value

    if isinstance(value, str):

        text = value.strip().lower()

        if text in ("true", "yes", "1", "on"):
            return True

        if text in ("false", "no", "0", "off"):
            return False

    if isinstance(value, int) and value in (0, 1):
        return bool(value)

    raise RuntimeError(
        f"config.yaml database.{field} must be true or false "
        f"(got {value!r})."
    )


def normalize_database_config(raw: Any) -> dict[str, Any]:
    """
    Validate a raw `database:` mapping (from config.yaml or from
    the Settings page) and fill in defaults. Raises RuntimeError
    with a readable message on bad input.
    """

    if not isinstance(raw, dict):
        raise RuntimeError(
            "config.yaml has no 'database:' section. Add one "
            "(server, name, driver, authentication, ...)."
        )

    server = str(raw.get("server") or "").strip()
    name = str(raw.get("name") or "").strip()

    if not server:
        raise RuntimeError("config.yaml database.server is required.")

    if not name:
        raise RuntimeError("config.yaml database.name is required.")

    authentication = raw.get("authentication") or {}

    if not isinstance(authentication, dict):
        raise RuntimeError(
            "config.yaml database.authentication must be a mapping."
        )

    try:
        connect_timeout = int(raw.get("connect_timeout", 10))
    except (TypeError, ValueError):
        raise RuntimeError(
            "config.yaml database.connect_timeout must be a "
            "whole number of seconds."
        )

    if connect_timeout < 1:
        raise RuntimeError(
            "config.yaml database.connect_timeout must be >= 1."
        )

    return {
        "enabled": _as_bool(raw.get("enabled", True), "enabled"),
        "server": server,
        "name": name,
        "driver": str(raw.get("driver") or "SQL Server").strip(),
        "trusted_connection": _as_bool(
            authentication.get("trusted_connection", True),
            "authentication.trusted_connection",
        ),
        "username": str(authentication.get("username") or "").strip(),
        "encrypt": _as_bool(raw.get("encrypt", False), "encrypt"),
        "trust_server_certificate": _as_bool(
            raw.get("trust_server_certificate", True),
            "trust_server_certificate",
        ),
        "connect_timeout": connect_timeout,
    }


def _read_database_section() -> Any:

    if yaml is None:
        raise RuntimeError(
            "PyYAML is required to read the database settings "
            "from config.yaml. Install it with: "
            "python -m pip install PyYAML"
        )

    if not CONFIG_FILE.exists():
        raise RuntimeError(f"config.yaml not found: {CONFIG_FILE}")

    with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    if not isinstance(data, dict):
        raise RuntimeError(f"{CONFIG_FILE} is not a YAML mapping.")

    return data.get("database")


def load_database_config(force_reload: bool = False) -> dict[str, Any]:
    """
    Database settings from config.yaml, read once per process
    (restart the service, or call reload_database_config(), to
    pick up edits -- same rule as the rest of config.yaml).
    """

    global _cached_config

    with _config_lock:

        if _cached_config is None or force_reload:
            _cached_config = normalize_database_config(
                _read_database_section()
            )

        return dict(_cached_config)


def reload_database_config() -> dict[str, Any]:
    return load_database_config(force_reload=True)


# ==========================================================
# CONNECTION STRING
# ==========================================================

def _odbc_value(value: str) -> str:
    """Brace-escape a value that may contain ; = { or }."""
    return "{" + value.replace("}", "}}") + "}"


def build_connection_string(database: dict[str, Any]) -> str:

    parts = [
        f"DRIVER={{{database['driver']}}}",
        f"SERVER={database['server']}",
        f"DATABASE={database['name']}",
    ]

    if database["trusted_connection"]:

        parts.append("Trusted_Connection=yes")

    else:

        password = os.getenv(DB_PASSWORD_ENV, "")

        if not database["username"] or not password:
            raise RuntimeError(
                "SQL authentication is selected "
                "(database.authentication.trusted_connection: "
                "false) but a username is missing in config.yaml "
                f"or the {DB_PASSWORD_ENV} environment variable "
                "is not set."
            )

        parts.append(f"UID={_odbc_value(database['username'])}")
        parts.append(f"PWD={_odbc_value(password)}")

    # The legacy "SQL Server" driver does not understand Encrypt;
    # only the newer "ODBC Driver NN for SQL Server" family does.
    if "odbc driver" in database["driver"].lower():
        parts.append(
            "Encrypt=" + ("yes" if database["encrypt"] else "no")
        )

    parts.append(
        "TrustServerCertificate="
        + ("yes" if database["trust_server_certificate"] else "no")
    )

    return ";".join(parts) + ";"


# ==========================================================
# DATETIMEOFFSET SUPPORT (ODBC SQL type -155)
# ==========================================================
#
# pyodbc has no built-in mapping for DATETIMEOFFSET, so any
# SELECT that returns one of these columns raises:
#   ODBC SQL type -155 is not yet supported
# An output converter fixes every query at once. Values come
# back as timezone-aware UTC datetimes.

SQL_SS_TIMESTAMPOFFSET = -155


def _handle_datetimeoffset(raw: bytes) -> datetime:

    year, month, day, hour, minute, second, nanos, tz_h, tz_m = (
        struct.unpack("<6hI2h", raw)
    )

    value = datetime(
        year, month, day, hour, minute, second,
        nanos // 1000,
        tzinfo=timezone(timedelta(hours=tz_h, minutes=tz_m)),
    )

    return value.astimezone(timezone.utc)


# ==========================================================
# CONNECTION
# ==========================================================

def get_connection() -> pyodbc.Connection:
    """
    Create a SQL Server connection from the database: section
    of config.yaml.
    """

    database = load_database_config()

    if not database["enabled"]:
        raise RuntimeError(
            "Database is disabled (database.enabled: false in "
            f"{CONFIG_FILE})."
        )

    try:
        conn = pyodbc.connect(
            build_connection_string(database),
            timeout=database["connect_timeout"],
            autocommit=False,
        )

        conn.add_output_converter(
            SQL_SS_TIMESTAMPOFFSET, _handle_datetimeoffset
        )

        return conn

    except pyodbc.Error as exc:
        raise RuntimeError(
            "\n"
            "========================================\n"
            "SQL SERVER CONNECTION FAILED\n"
            "========================================\n"
            f"Server   : {database['server']}\n"
            f"Database : {database['name']}\n"
            f"Driver   : {database['driver']}\n"
            f"Config   : {CONFIG_FILE}\n"
            "\n"
            "Check:\n"
            "1. SQL Server is installed and the service is running.\n"
            f"2. Database '{database['name']}' exists.\n"
            "3. The login/Windows account has access to it.\n"
            "4. database: settings in config.yaml are correct.\n"
            f"\nOriginal error:\n{exc}\n"
        ) from exc


# ==========================================================
# TRANSACTION CONTEXT
# ==========================================================

@contextmanager
def database_connection():
    """
    Safe SQL Server transaction context.
    """

    conn = get_connection()

    try:
        yield conn
        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# ==========================================================
# DATABASE INITIALIZATION
# ==========================================================

def initialize_database() -> None:
    """Verify connectivity and the selected database."""

    database = load_database_config()

    with database_connection() as conn:

        cursor = conn.cursor()

        cursor.execute("SELECT @@SERVERNAME, DB_NAME()")

        server_name, database_name = cursor.fetchone()

        if database_name.lower() != database["name"].lower():

            raise RuntimeError(
                f"Connected to unexpected database: "
                f"{database_name}. Expected: {database['name']}"
            )

        print(
            "SQL Server connected successfully: "
            f"{server_name} / {database_name}"
        )


# ==========================================================
# SETTINGS PAGE: TEST A CANDIDATE CONFIG (nothing is saved)
# ==========================================================

def test_database_config(candidate: Any) -> dict[str, Any]:
    """
    Try a connection using a database: mapping that has NOT been
    saved yet (backs the Settings page "Test Database
    Connection" button). Never raises; returns
    {"ok": bool, "message": str}.
    """

    try:
        database = normalize_database_config(candidate)

        connection = pyodbc.connect(
            build_connection_string(database),
            timeout=database["connect_timeout"],
            autocommit=True,
        )

    except RuntimeError as exc:
        return {"ok": False, "message": str(exc)}

    except pyodbc.Error as exc:
        return {"ok": False, "message": f"Connection failed: {exc}"}

    try:
        cursor = connection.cursor()
        cursor.execute("SELECT @@SERVERNAME, DB_NAME()")
        server_name, database_name = cursor.fetchone()

    except pyodbc.Error as exc:
        return {"ok": False, "message": f"Query failed: {exc}"}

    finally:
        connection.close()

    if database_name.lower() != database["name"].lower():
        return {
            "ok": False,
            "message": (
                f"Connected to '{database_name}' but the "
                f"configured database is '{database['name']}'."
            ),
        }

    return {
        "ok": True,
        "message": (
            f"Connected: {server_name} / {database_name}. "
            "Login credentials and database access are valid."
        ),
    }


# ==========================================================
# COMPATIBILITY
# ==========================================================

def get_database_path() -> str:
    """Compatibility helper; SQL Server has no local .db path."""

    database = load_database_config()

    return (
        f"SQL Server: {database['server']} / "
        f"Database: {database['name']}"
    )


# Kept for `from database.connection import DATABASE_PATH`.
# Resolved lazily-safe: does not read config at import time.
DATABASE_PATH = f"SQL Server configured in {CONFIG_FILE}"