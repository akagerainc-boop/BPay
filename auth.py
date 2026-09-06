"""Admin authentication: password hashing and JWT issuing/checking."""

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
from flask import jsonify, request

_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 with a per-password random salt."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), _ITERATIONS
    ).hex()
    return f"pbkdf2${_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), int(iterations)
        ).hex()
        # Constant-time so a wrong password can't be narrowed by timing.
        return hmac.compare_digest(candidate, digest)
    except (ValueError, AttributeError):
        return False


def _secret() -> str:
    return os.getenv("JWT_SECRET", "change-me-to-a-long-random-string")


def issue_token(admin_id: int, username: str) -> str:
    hours = int(os.getenv("JWT_HOURS", "12"))
    payload = {
        "sub": str(admin_id),
        "username": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=hours),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, _secret(), algorithm="HS256")


def require_admin(fn):
    """Rejects any request without a valid, unexpired admin token."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify({"error": "Missing bearer token"}), 401
        token = header[7:]
        try:
            request.admin = jwt.decode(token, _secret(), algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Session expired, sign in again"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid token"}), 401
        return fn(*args, **kwargs)

    return wrapper
