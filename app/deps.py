"""Request dependencies: who is asking.

The token travels in an HttpOnly cookie (what the browser uses) or an
``Authorization: Bearer`` header (what scripts use). Both carry the same
HMAC-signed claims from :mod:`app.security`.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import Depends, HTTPException, Request

from .db import get_db
from .security import read_token

SESSION_COOKIE = "sem_session"


def _token_from_request(request: Request) -> Optional[str]:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.cookies.get(SESSION_COOKIE)


def require_user(request: Request) -> dict[str, Any]:
    token = _token_from_request(request)
    claims = read_token(token) if token else None
    if not claims:
        raise HTTPException(status_code=401, detail="Not signed in.")
    user = get_db().query_one(
        'SELECT "UserID", "Username" FROM "PanelUser" '
        'WHERE "UserID" = :id AND "IsDeleted" = 0',
        {"id": claims.get("uid")},
    )
    if user is None:
        raise HTTPException(status_code=401, detail="This account no longer exists.")
    return user


CurrentUser = Depends(require_user)
