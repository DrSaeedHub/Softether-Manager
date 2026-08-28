"""Password hashing and session tokens, on the standard library.

Passwords are hashed with **scrypt** (:mod:`hashlib`), which ships with Python
and is memory-hard; there is no bcrypt wheel to build on an arm64 host with no
compiler. Tokens are **HMAC-signed** JSON with an expiry -- the panel has one
kind of principal (an administrator) and one signer (itself), so a JWT library
would add nothing but a dependency.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Optional

from .secrets_store import session_key

# scrypt parameters: interactive-login strength (~16 MiB, tens of ms).
# Recorded in every hash, so they can be raised later without breaking
# existing hashes.
_N, _R, _P = 2**14, 8, 1
_SALT_BYTES = 16
_KEY_LEN = 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_KEY_LEN)
    return "scrypt$%d$%d$%d$%s$%s" % (
        _N,
        _R,
        _P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(key).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, key_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(key_b64)
        key = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(key, expected)


# ---------------------------------------------------------------------------
# session tokens
# ---------------------------------------------------------------------------


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload: bytes) -> str:
    mac = hmac.new(session_key().encode("utf-8"), payload, hashlib.sha256).digest()
    return _b64encode(mac)


def make_token(user_id: int, username: str, expire_minutes: int) -> str:
    payload = json.dumps(
        {"uid": user_id, "sub": username, "exp": int(time.time()) + expire_minutes * 60},
        separators=(",", ":"),
    ).encode("utf-8")
    body = _b64encode(payload)
    return f"{body}.{_sign(payload)}"


def read_token(token: str) -> Optional[dict[str, Any]]:
    """The claims of a valid, unexpired token, or None.

    None for every kind of bad token -- malformed, forged, expired -- because
    the caller answers all three the same way: sign in again.
    """
    try:
        body, signature = token.split(".", 1)
        payload = _b64decode(body)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(_sign(payload), signature):
        return None
    try:
        claims = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(claims, dict) or int(claims.get("exp", 0)) < time.time():
        return None
    return claims
