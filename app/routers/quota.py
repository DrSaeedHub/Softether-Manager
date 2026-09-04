"""Traffic quotas: /quotas/...

A ceiling on how much a Virtual Hub, or one user's config, may move before the
panel cuts it off. The domain lives in :mod:`app.services.quota`; this is the
door to it.

The paths name the subject rather than nesting under the hub router, because a
quota is the panel's own record about a SoftEther object, not a SoftEther
object itself -- and because the user tables want every quota on the server in
one request, which a hub-nested path could not serve.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..audit import record
from ..deps import CurrentUser
from ..services import quota

router = APIRouter(prefix="/quotas", tags=["quota"])

Wire = dict[str, Any]


class QuotaIn(BaseModel):
    """A limit as a person states it: a number, a unit, and what it counts."""

    limit: float = Field(ge=0, le=1_000_000)
    unit: str = "GB"
    metric: str = "total"
    enabled: bool = True


def _settle(subject: str, hub: str, username: str = "") -> Optional[Wire]:
    """Re-evaluate after a change so the answer describes the world as it now
    is -- a raised ceiling lifts its block in the same request that raised it.

    Enforcement talks to the VPN server, which may be down; that is the
    quota tick's problem to retry, never a reason to fail the save.
    """
    try:
        quota.enforce()
    except Exception:  # noqa: BLE001 - the tick retries
        pass
    return quota.get(subject, hub, username)


def _save(subject: str, hub: str, username: str, body: QuotaIn, user: dict) -> Wire:
    try:
        limit_bytes = quota.to_bytes(body.limit, body.unit)
        quota.save(subject, hub, username, limit_bytes, body.metric, body.enabled)
    except quota.QuotaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record(
        user,
        "quota.set",
        "hub" if subject == "hub" else "vpn_user",
        hub if subject == "hub" else username,
        f"{body.limit:g} {body.unit} {body.metric}"
        + ("" if body.enabled else " (off)")
        + (f" in hub {hub}" if subject == "user" else ""),
    )
    return _settle(subject, hub, username) or {}


def _missing(subject: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"No traffic limit is set on this {subject}.")


# --- everything at once ----------------------------------------------------------


@router.get("")
def list_quotas(user: dict = CurrentUser) -> list[Wire]:
    """Every quota on the server. The user tables draw their meters from this
    one call rather than one per row."""
    return quota.all_quotas()


# --- a hub ------------------------------------------------------------------------


@router.get("/hub/{hub}")
def get_hub_quota(hub: str, user: dict = CurrentUser) -> Wire:
    found = quota.get("hub", hub)
    if found is None:
        raise _missing("hub")
    return found


@router.put("/hub/{hub}")
def set_hub_quota(hub: str, body: QuotaIn, user: dict = CurrentUser) -> Wire:
    return _save("hub", hub, "", body, user)


@router.delete("/hub/{hub}")
def delete_hub_quota(hub: str, user: dict = CurrentUser) -> Wire:
    if not quota.delete("hub", hub):
        raise _missing("hub")
    record(user, "quota.removed", "hub", hub, "")
    return {"ok": True}


@router.post("/hub/{hub}/reset")
def reset_hub_quota(hub: str, user: dict = CurrentUser) -> Wire:
    out = quota.reset("hub", hub)
    if out is None:
        raise _missing("hub")
    record(user, "quota.reset", "hub", hub, "")
    return _settle("hub", hub) or out


# --- one user's config --------------------------------------------------------------


@router.get("/user/{hub}/{name}")
def get_user_quota(hub: str, name: str, user: dict = CurrentUser) -> Wire:
    found = quota.get("user", hub, name)
    if found is None:
        raise _missing("config")
    return found


@router.put("/user/{hub}/{name}")
def set_user_quota(hub: str, name: str, body: QuotaIn, user: dict = CurrentUser) -> Wire:
    return _save("user", hub, name, body, user)


@router.delete("/user/{hub}/{name}")
def delete_user_quota(hub: str, name: str, user: dict = CurrentUser) -> Wire:
    if not quota.delete("user", hub, name):
        raise _missing("config")
    record(user, "quota.removed", "vpn_user", name, f"hub {hub}")
    return {"ok": True}


@router.post("/user/{hub}/{name}/reset")
def reset_user_quota(hub: str, name: str, user: dict = CurrentUser) -> Wire:
    out = quota.reset("user", hub, name)
    if out is None:
        raise _missing("config")
    record(user, "quota.reset", "vpn_user", name, f"hub {hub}")
    return _settle("user", hub, name) or out
