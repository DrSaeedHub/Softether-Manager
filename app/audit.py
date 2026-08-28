"""The audit trail: every state-changing panel action, one row.

The ledger of human decisions. Reads are not recorded -- the trail answers
"who changed what", not "who looked".
"""
from __future__ import annotations

from typing import Any, Optional

from .db import get_db, utc_now


def record(
    user: Optional[dict[str, Any]],
    action: str,
    target_type: str = "",
    target_key: str = "",
    detail: str = "",
) -> None:
    get_db().execute(
        'INSERT INTO "AuditLog"("UserID", "Username", "Action", "TargetType", "TargetKey", '
        '"Detail", "CreatedDate") VALUES (:uid, :username, :action, :ttype, :tkey, :detail, :now)',
        {
            "uid": user.get("UserID") if user else None,
            "username": (user.get("Username") if user else "") or "system",
            "action": action,
            "ttype": target_type,
            "tkey": target_key,
            "detail": detail[:2000],
            "now": utc_now(),
        },
    )
