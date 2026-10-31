"""
==========================================================
FillPac AI
Login: password hashing + session token helpers
==========================================================

Deliberately stdlib-only (hashlib/hmac/secrets) so login does
not depend on bcrypt/PyJWT/etc. being installed on top of the
existing pyodbc + FastAPI + python-socketio stack.

Passwords
---------
PBKDF2-HMAC-SHA256 with a random 16-byte salt per user and a
high iteration count. The salt and hash are stored separately
in dbo.users (password_salt, password_hash), both as hex.

Sessions
--------
Logging in creates a random 36-byte URL-safe token stored in
dbo.auth_sessions next to the user id and an expiry. The
browser sends it back as `Authorization: Bearer <token>` and
every protected request looks it up. This is intentionally NOT
a signed/self-contained token (JWT) -- logging a user out, or
revoking a session, is just deleting the row, and there is no
extra crypto dependency to install.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

# ==========================================================
# CONFIG
# ==========================================================

PBKDF2_ITERATIONS = 260_000
SALT_BYTES = 16
TOKEN_BYTES = 36

# How long a session stays valid without activity. Every
# verified request slides this forward (see
# repository.touch_session), so an active user is never logged
# out mid-shift; an idle browser expires after this long.
SESSION_LIFETIME_HOURS = 12


# ==========================================================
# PASSWORDS
# ==========================================================

def hash_password(password: str) -> tuple[str, str]:
    """
    Hash a plaintext password.

    Returns (password_hash_hex, password_salt_hex).
    """

    if not password:
        raise ValueError("Password must not be empty.")

    salt = secrets.token_bytes(SALT_BYTES)

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )

    return digest.hex(), salt.hex()


def verify_password(
    password: str,
    password_hash: str,
    password_salt: str,
) -> bool:
    """
    Check a plaintext password against a stored hash/salt pair.
    Constant-time comparison to avoid timing side-channels.
    """

    if not password:
        return False

    try:
        salt = bytes.fromhex(password_salt)
        expected = bytes.fromhex(password_hash)

    except (TypeError, ValueError):
        return False

    candidate = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )

    return hmac.compare_digest(candidate, expected)


# ==========================================================
# SESSION TOKENS
# ==========================================================

def generate_session_token() -> str:
    """
    A random, unguessable session token. Not a JWT -- just an
    opaque key into dbo.auth_sessions.
    """

    return secrets.token_urlsafe(TOKEN_BYTES)
