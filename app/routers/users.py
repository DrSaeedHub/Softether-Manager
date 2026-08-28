"""Every user on the server, across all Virtual Hubs: /users.

The sidebar's Users page. Each entry is the EnumUser record with the hub it
belongs to; a hub that cannot be enumerated contributes an error entry rather
than silently vanishing from the list.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ..deps import CurrentUser
from ..se import rpc
from .se_hub import online_usernames

router = APIRouter(prefix="/users", tags=["users"])


@router.get("")
def all_users(user: dict = CurrentUser) -> dict[str, Any]:
    hubs = [h.get("HubName_str", "") for h in rpc("EnumHub").get("HubList", []) if h.get("HubName_str")]
    users: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for hub in hubs:
        try:
            online = online_usernames(hub)
            for entry in rpc("EnumUser", {"HubName_str": hub}).get("UserList", []):
                name = str(entry.get("Name_str", "")).casefold()
                users.append({**entry, "HubName_str": hub, "Online_bool": name in online})
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            errors.append({"hub": hub, "error": str(detail.get("message", "failed"))})
    return {"hubs": hubs, "users": users, "errors": errors}
