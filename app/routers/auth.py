"""Signing in, the first account, and the account itself."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from ..audit import record
from ..config import settings
from ..db import get_db, utc_now
from ..deps import SESSION_COOKIE, CurrentUser
from ..security import hash_password, make_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=1, max_length=256)


def _account_count() -> int:
    row = get_db().query_one('SELECT COUNT(*) AS n FROM "PanelUser" WHERE "IsDeleted" = 0')
    return int(row["n"]) if row else 0


def _issue(response: Response, user_id: int, username: str) -> dict[str, Any]:
    token = make_token(user_id, username, settings.session_expire_minutes)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_expire_minutes * 60,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {"token": token, "username": username}


@router.get("/state")
def auth_state() -> dict[str, Any]:
    """What the sign-in screen needs to know before anyone is signed in."""
    return {"setup_required": _account_count() == 0}


@router.post("/setup")
def setup(body: Credentials, response: Response) -> dict[str, Any]:
    """Create the first account. Refused once any account exists."""
    if _account_count() > 0:
        raise HTTPException(status_code=409, detail="An account already exists on this panel.")
    now = utc_now()
    user_id = get_db().execute(
        'INSERT INTO "PanelUser"("Username", "PasswordHash", "CreatedDate", "UpdatedDate") '
        "VALUES (:u, :h, :now, :now)",
        {"u": body.username, "h": hash_password(body.password), "now": now},
    )
    record({"UserID": user_id, "Username": body.username}, "auth.setup", "panel_user", body.username)
    return _issue(response, user_id, body.username)


@router.post("/login")
def login(body: Credentials, response: Response) -> dict[str, Any]:
    user = get_db().query_one(
        'SELECT * FROM "PanelUser" WHERE "Username" = :u AND "IsDeleted" = 0',
        {"u": body.username},
    )
    if user is None or not verify_password(body.password, user["PasswordHash"]):
        # One answer for both a wrong name and a wrong password.
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    return _issue(response, int(user["UserID"]), user["Username"])


@router.post("/logout")
def logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: dict[str, Any] = CurrentUser) -> dict[str, Any]:
    return {"username": user["Username"]}


@router.put("/password")
def change_password(body: PasswordChange, user: dict[str, Any] = CurrentUser) -> dict[str, Any]:
    row = get_db().query_one(
        'SELECT "PasswordHash" FROM "PanelUser" WHERE "UserID" = :id', {"id": user["UserID"]}
    )
    if row is None or not verify_password(body.current_password, row["PasswordHash"]):
        raise HTTPException(status_code=403, detail="The current password is wrong.")
    get_db().execute(
        'UPDATE "PanelUser" SET "PasswordHash" = :h, "UpdatedDate" = :now WHERE "UserID" = :id',
        {"h": hash_password(body.new_password), "now": utc_now(), "id": user["UserID"]},
    )
    record(user, "auth.password_changed", "panel_user", user["Username"])
    return {"ok": True}
