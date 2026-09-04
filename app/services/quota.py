"""Traffic quotas: a byte ceiling on a Virtual Hub, or on one user's config.

Two kinds of subject, one mechanism. A quota says *how much* may move
(``LimitBytes``), *which direction counts* (``Metric``), and nothing else --
the amount and the unit the operator typed are the same number in bytes by
the time they get here.

**What "used" means.** Exactly what the panel's Transfer column says, and
nothing else. SoftEther counts per hub and per user, cumulatively, from the
moment the object was created, and offers no RPC to zero a counter -- so the
figure the panel shows, and the figure a quota measures, are both

    transfer = raw counter - baseline

with the baseline at zero until somebody resets it. A ceiling set on a config
that has already moved 144 GB therefore starts at 144 GB, not at nothing: the
operator set a ceiling on the number in front of them, and that is the number
it applies to. :func:`reset_transfer` moves the baseline up to the current
reading, which zeroes the panel's Transfer and the quota together, because
they were never two figures to begin with.

The raw reading is cached on the row (``LastSendBytes``/``LastRecvBytes``) so
a list of quotas costs no RPCs; anything looking at *one* subject refreshes it
first, and the enforcement tick refreshes everything it is watching.

**Direction.** The panel's convention throughout: SoftEther's ``Recv`` is what
the subject *downloaded*, its ``Send`` is what the subject *uploaded*.

**What "over" does.** A user over quota has ``policy:Access`` denied and their
live sessions cut; a hub over quota is taken offline. Both are SoftEther's own
switch for "this may not carry traffic", both are reversible, and what was
there before the block is kept in ``RestoreState`` so lifting it puts the
subject back exactly as it was rather than at some assumed default.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional

from ..audit import record
from ..db import get_db, utc_now

logger = logging.getLogger(__name__)

#: What a quota measures. ``total`` is the two directions added together.
METRICS = ("total", "download", "upload")

#: The kinds of thing a quota can be attached to.
SUBJECTS = ("hub", "user")

#: The units offered, and what each is worth. Binary multiples, because
#: ``formatBytes`` renders binary multiples: an operator who types 10 GB and
#: then watches the meter would otherwise see the two disagree by 7%.
UNITS: dict[str, int] = {
    "MB": 1024 ** 2,
    "GB": 1024 ** 3,
    "TB": 1024 ** 4,
}

#: Columns every read wants, in one place so the row shape is stated once.
_COLUMNS = (
    '"TrafficQuotaID", "SubjectType", "HubName", "UserName", "UserKey", "LimitBytes", '
    '"Metric", "IsEnabled", "BaseSendBytes", "BaseRecvBytes", "LastSendBytes", '
    '"LastRecvBytes", "CycleStartDate", "ExceededDate", "EnforcedDate", "RestoreState", '
    '"CreatedDate", "UpdatedDate"'
)


class QuotaError(ValueError):
    """A quota was described in a way the panel will not store."""


# --- units ---------------------------------------------------------------------


def to_bytes(amount: float, unit: str) -> int:
    """The operator's number and unit, as bytes."""
    if unit not in UNITS:
        raise QuotaError(f"Unknown unit {unit!r}; expected one of {', '.join(UNITS)}.")
    if amount < 0:
        raise QuotaError("A limit cannot be negative.")
    return int(round(amount * UNITS[unit]))


def split_bytes(total: int) -> tuple[float, str]:
    """Bytes back into the largest unit that renders them without a long tail
    -- so a quota saved as 2 TB comes back to the form as 2 TB, not 2048 GB."""
    for unit in ("TB", "GB", "MB"):
        size = UNITS[unit]
        if total >= size:
            value = total / size
            if abs(value - round(value, 2)) < 1e-9:
                return round(value, 2), unit
    return round(total / UNITS["MB"], 2), "MB"


# --- reading -------------------------------------------------------------------


def _key(name: str) -> str:
    """SoftEther matches usernames without case; so does every lookup here."""
    return name.casefold()


def net_bytes(row: dict[str, Any]) -> tuple[int, int]:
    """(uploaded, downloaded) since the last reset -- the panel's Transfer.

    Clamped at zero: a raw counter below its own baseline means SoftEther
    started it over, and negative traffic is not a thing.
    """
    upload = max(0, int(row["LastSendBytes"]) - int(row["BaseSendBytes"]))
    download = max(0, int(row["LastRecvBytes"]) - int(row["BaseRecvBytes"]))
    # The clamp is belt and braces: :func:`observe` drops a baseline that the
    # counter has fallen below, so this should never actually bite.
    return upload, download


def metered(upload: int, download: int, metric: str) -> int:
    """The one of those figures the metric actually measures."""
    if metric == "upload":
        return upload
    if metric == "download":
        return download
    return upload + download


def used_bytes(row: dict[str, Any]) -> int:
    """What this quota's metric says has been consumed."""
    upload, download = net_bytes(row)
    return metered(upload, download, str(row["Metric"]))


def public(row: dict[str, Any]) -> dict[str, Any]:
    """One quota, in the shape the API and the UI speak."""
    limit = int(row["LimitBytes"])
    upload, download = net_bytes(row)
    used = metered(upload, download, str(row["Metric"]))
    amount, unit = split_bytes(limit)
    return {
        "subject": str(row["SubjectType"]),
        "hub": str(row["HubName"]),
        "username": str(row["UserName"]),
        # A row with no limit is a baseline and nothing else -- what a config
        # whose transfer was reset, but which has no ceiling, looks like.
        "has_limit": limit > 0,
        "limit_bytes": limit,
        "limit": amount,
        "unit": unit,
        "metric": str(row["Metric"]),
        "enabled": bool(row["IsEnabled"]),
        "upload_bytes": upload,
        "download_bytes": download,
        # Where the last reset put the zero. The UI subtracts these from the
        # counters it already has, so its Transfer column and this agree to
        # the byte instead of to the last tick.
        "base_send_bytes": int(row["BaseSendBytes"]),
        "base_recv_bytes": int(row["BaseRecvBytes"]),
        "used_bytes": used,
        "remaining_bytes": max(0, limit - used) if limit > 0 else None,
        "percent": round(min(100.0, used / limit * 100), 2) if limit > 0 else 0.0,
        "exceeded": bool(row["ExceededDate"]),
        "exceeded_date": row["ExceededDate"],
        "blocked": bool(row["EnforcedDate"]),
        "blocked_date": row["EnforcedDate"],
        "cycle_start": str(row["CycleStartDate"]),
        "updated_date": str(row["UpdatedDate"]),
    }


def _row(subject: str, hub: str, username: str = "") -> Optional[dict[str, Any]]:
    return get_db().query_one(
        f'SELECT {_COLUMNS} FROM "TrafficQuota" '
        'WHERE "SubjectType" = :subject AND "HubName" = :hub AND "UserKey" = :key',
        {"subject": subject, "hub": hub, "key": _key(username)},
    )


def get(subject: str, hub: str, username: str = "") -> Optional[dict[str, Any]]:
    """One quota, or ``None`` when the subject has no ceiling set."""
    row = _row(subject, hub, username)
    return public(row) if row else None


def all_quotas() -> list[dict[str, Any]]:
    """Every quota on the server -- what the user tables render their meters
    from, in one request rather than one per row."""
    rows = get_db().query_all(
        f'SELECT {_COLUMNS} FROM "TrafficQuota" ORDER BY "HubName", "SubjectType", "UserName"'
    )
    return [public(row) for row in rows]


def _all_rows() -> list[dict[str, Any]]:
    return get_db().query_all(f'SELECT {_COLUMNS} FROM "TrafficQuota"')


def any_configured() -> bool:
    """Whether anything is worth reading counters for. The quota tick asks
    this first, so a panel with no quotas pays one indexed query a minute."""
    return get_db().query_one('SELECT 1 AS one FROM "TrafficQuota" LIMIT 1') is not None


# --- writing -------------------------------------------------------------------


def save(
    subject: str,
    hub: str,
    username: str,
    limit_bytes: int,
    metric: str,
    enabled: bool = True,
) -> dict[str, Any]:
    """Create or change a quota.

    A new ceiling starts measuring what the subject has *already* moved --
    the operator set it on the figure in front of them. Consumption is kept
    across a change too: raising a ceiling is not resetting the meter, and
    :func:`reset_transfer` exists for when they mean that.

    ``limit_bytes`` of 0 stores a row with no ceiling at all, which is how a
    transfer baseline exists on a subject nobody has limited.
    """
    if subject not in SUBJECTS:
        raise QuotaError(f"Unknown subject {subject!r}.")
    if metric not in METRICS:
        raise QuotaError(f"Unknown metric {metric!r}; expected one of {', '.join(METRICS)}.")
    if subject == "user" and not username:
        raise QuotaError("A user quota needs a username.")
    if limit_bytes < 0:
        raise QuotaError("A limit cannot be negative.")

    now = utc_now()
    fields = {
        "subject": subject,
        "hub": hub,
        "user": username if subject == "user" else "",
        "key": _key(username) if subject == "user" else "",
        "limit": int(limit_bytes),
        "metric": metric,
        "enabled": 1 if enabled else 0,
        "now": now,
    }
    get_db().execute(
        'INSERT INTO "TrafficQuota"("SubjectType", "HubName", "UserName", "UserKey", '
        '"LimitBytes", "Metric", "IsEnabled", "CycleStartDate", "CreatedDate", "UpdatedDate") '
        "VALUES (:subject, :hub, :user, :key, :limit, :metric, :enabled, :now, :now, :now) "
        'ON CONFLICT("SubjectType", "HubName", "UserKey") DO UPDATE SET '
        '"UserName" = :user, "LimitBytes" = :limit, "Metric" = :metric, '
        '"IsEnabled" = :enabled, "UpdatedDate" = :now',
        fields,
    )
    return get(subject, hub, username)  # type: ignore[return-value]


def reset_transfer(subject: str, hub: str, username: str = "") -> dict[str, Any]:
    """Zero the subject's transfer: the baseline moves up to the counter.

    This is the panel's only way to reset a counter -- SoftEther has no RPC
    for it -- and because the panel's Transfer column is measured the same
    way, this zeroes what the operator sees as well as what the ceiling
    measures. They were never two figures.

    A subject with no record yet gets one, carrying no ceiling: a baseline is
    worth keeping whether or not anything is limited.
    """
    row = _row(subject, hub, username)
    if row is None:
        save(subject, hub, username, 0, "total", True)
        row = _row(subject, hub, username)
        if row is None:  # pragma: no cover - the insert above just succeeded
            raise QuotaError("The traffic record could not be created.")

    # The freshest reading is the honest zero; the cached one is the fallback
    # when the server cannot be reached, and still zeroes what the panel shows.
    reading = _live_counters(subject, hub, username)
    send, recv = reading if reading else (int(row["LastSendBytes"]), int(row["LastRecvBytes"]))

    release(row, "transfer reset by the operator")
    get_db().execute(
        'UPDATE "TrafficQuota" SET "BaseSendBytes" = :send, "BaseRecvBytes" = :recv, '
        '"LastSendBytes" = :send, "LastRecvBytes" = :recv, "CycleStartDate" = :now, '
        '"ExceededDate" = NULL, "UpdatedDate" = :now '
        'WHERE "TrafficQuotaID" = :id',
        {"id": row["TrafficQuotaID"], "send": send, "recv": recv, "now": utc_now()},
    )
    return get(subject, hub, username)  # type: ignore[return-value]


def delete(subject: str, hub: str, username: str = "") -> bool:
    """Remove the ceiling, lifting its block first -- so taking a limit away
    never leaves the subject cut off with nothing left to explain why.

    The *record* survives if it still carries a baseline: somebody reset this
    subject's transfer, and that zero is theirs to keep whether or not a
    ceiling stands on top of it. With nothing left to remember, the row goes.
    """
    row = _row(subject, hub, username)
    if row is None:
        return False
    release(row, "limit removed by the operator")
    keep = int(row["BaseSendBytes"]) > 0 or int(row["BaseRecvBytes"]) > 0
    if keep:
        get_db().execute(
            'UPDATE "TrafficQuota" SET "LimitBytes" = 0, "ExceededDate" = NULL, '
            '"UpdatedDate" = :now WHERE "TrafficQuotaID" = :id',
            {"id": row["TrafficQuotaID"], "now": utc_now()},
        )
    else:
        get_db().execute(
            'DELETE FROM "TrafficQuota" WHERE "TrafficQuotaID" = :id',
            {"id": row["TrafficQuotaID"]},
        )
    return True


def forget_user(hub: str, username: str) -> None:
    """Drop a deleted user's quota. Nothing to release: the account is gone."""
    get_db().execute(
        'DELETE FROM "TrafficQuota" WHERE "SubjectType" = \'user\' '
        'AND "HubName" = :hub AND "UserKey" = :key',
        {"hub": hub, "key": _key(username)},
    )


def forget_hub(hub: str) -> None:
    """Drop everything belonging to a deleted hub -- its own quota and every
    user quota inside it."""
    get_db().execute('DELETE FROM "TrafficQuota" WHERE "HubName" = :hub', {"hub": hub})


# --- counting ------------------------------------------------------------------


def observe(readings: Iterable[dict[str, Any]]) -> int:
    """Store the newest raw counters for every subject that has a record.

    Each reading is ``{"subject", "hub", "user", "send", "recv"}`` carrying
    SoftEther's cumulative counters. A reading *below* the stored baseline can
    only mean the counter started over -- the server was rebuilt, the account
    recreated -- and a baseline then points at a total that no longer exists,
    so it drops to zero. Everything now on the counter accumulated after the
    reset, which is exactly what the transfer should say.
    """
    rows = _all_rows()
    if not rows:
        return 0
    index = {(r["SubjectType"], r["HubName"], r["UserKey"]): r for r in rows}

    now = utc_now()
    updates: list[dict[str, Any]] = []
    for reading in readings:
        subject = str(reading["subject"])
        name = str(reading.get("user", "")) if subject == "user" else ""
        row = index.get((subject, str(reading["hub"]), _key(name)))
        if row is None:
            continue
        send = max(0, int(reading.get("send", 0)))
        recv = max(0, int(reading.get("recv", 0)))
        updates.append(
            {
                "id": row["TrafficQuotaID"],
                "send": send,
                "recv": recv,
                "base_send": 0 if send < int(row["BaseSendBytes"]) else int(row["BaseSendBytes"]),
                "base_recv": 0 if recv < int(row["BaseRecvBytes"]) else int(row["BaseRecvBytes"]),
                "now": now,
            }
        )
    if updates:
        get_db().execute_many(
            'UPDATE "TrafficQuota" SET "LastSendBytes" = :send, "LastRecvBytes" = :recv, '
            '"BaseSendBytes" = :base_send, "BaseRecvBytes" = :base_recv, "UpdatedDate" = :now '
            'WHERE "TrafficQuotaID" = :id',
            updates,
        )
    return len(updates)


def _live_counters(subject: str, hub: str, username: str = "") -> Optional[tuple[int, int]]:
    """One subject's cumulative (send, recv), read now. ``None`` when it
    cannot be asked -- the server is down, or the object is gone."""
    from ..se import rpc

    try:
        if subject == "hub":
            status = rpc("GetHubStatus", {"HubName_str": hub})
            return (
                int(status.get("Send.UnicastBytes_u64", 0))
                + int(status.get("Send.BroadcastBytes_u64", 0)),
                int(status.get("Recv.UnicastBytes_u64", 0))
                + int(status.get("Recv.BroadcastBytes_u64", 0)),
            )
        found = rpc("GetUser", {"HubName_str": hub, "Name_str": username})
        return (
            int(found.get("Send.UnicastBytes_u64", 0))
            + int(found.get("Send.BroadcastBytes_u64", 0)),
            int(found.get("Recv.UnicastBytes_u64", 0))
            + int(found.get("Recv.BroadcastBytes_u64", 0)),
        )
    except Exception as exc:  # noqa: BLE001 - a stale figure beats no answer
        logger.debug("live counters unavailable for %s %s/%s: %s", subject, hub, username, exc)
        return None


def refresh(subject: str, hub: str, username: str = "") -> None:
    """Bring one subject's cached reading up to date, if it has a record.

    Anything looking at a single subject calls this first, so a detail view
    and the Transfer figure beside it never disagree by a tick.
    """
    if _row(subject, hub, username) is None:
        return
    reading = _live_counters(subject, hub, username)
    if reading is None:
        return
    observe([
        {"subject": subject, "hub": hub, "user": username, "send": reading[0], "recv": reading[1]}
    ])


# --- enforcing -----------------------------------------------------------------


def _is_over(row: dict[str, Any]) -> bool:
    limit = int(row["LimitBytes"])
    return bool(row["IsEnabled"]) and limit > 0 and used_bytes(row) >= limit


def _user_body(record_user: dict[str, Any]) -> dict[str, Any]:
    """A GetUser payload turned back into a SetUser body.

    The statistics are read-only and the password is never echoed: SoftEther
    leaves the stored credential alone when ``Auth_Password_str`` is absent,
    which is what makes editing a policy safe. This is the same rule the
    panel's own policy editor saves by.
    """
    return {
        key: value
        for key, value in record_user.items()
        if not key.startswith(("Recv.", "Send.")) and key not in ("Auth_Password_str", "NumLogin_u32")
    }


def block(row: dict[str, Any]) -> bool:
    """Cut the subject off, remembering what it was before.

    Returns whether the block took. A failure -- the server is down, the RPC
    was refused -- leaves the row unenforced so the next tick tries again,
    rather than recording a block that never happened.
    """
    from ..se import rpc

    subject = str(row["SubjectType"])
    hub = str(row["HubName"])
    name = str(row["UserName"])
    try:
        if subject == "hub":
            was_online = bool(rpc("GetHub", {"HubName_str": hub}).get("Online_bool", True))
            restore = {"Online_bool": was_online}
            rpc("SetHubOnline", {"HubName_str": hub, "Online_bool": False})
        else:
            current = rpc("GetUser", {"HubName_str": hub, "Name_str": name})
            restore = {
                "UsePolicy_bool": bool(current.get("UsePolicy_bool")),
                "policy:Access_bool": bool(current.get("policy:Access_bool", True)),
            }
            body = _user_body(current)
            body["UsePolicy_bool"] = True
            body["policy:Access_bool"] = False
            rpc("SetUser", {**body, "HubName_str": hub, "Name_str": name})
            _cut_sessions(hub, name)
    except Exception as exc:  # noqa: BLE001 - an unreachable server is a retry, not a crash
        logger.warning("could not enforce the %s quota on %s/%s: %s", subject, hub, name, exc)
        return False

    now = utc_now()
    get_db().execute(
        'UPDATE "TrafficQuota" SET "EnforcedDate" = :now, "RestoreState" = :restore, '
        '"ExceededDate" = COALESCE("ExceededDate", :now), "UpdatedDate" = :now '
        'WHERE "TrafficQuotaID" = :id',
        {"id": row["TrafficQuotaID"], "now": now, "restore": json.dumps(restore)},
    )
    record(
        None,
        "quota.blocked",
        "hub" if subject == "hub" else "vpn_user",
        hub if subject == "hub" else name,
        f"{_describe(row)} reached; {'hub taken offline' if subject == 'hub' else f'access denied in hub {hub}'}",
    )
    return True


def release(row: dict[str, Any], reason: str = "back under the limit") -> bool:
    """Lift a block, putting back exactly what :func:`block` found.

    Safe to call on a row that is not blocked -- it does nothing. When the
    stored state cannot be applied the row is still cleared: leaving it marked
    blocked would stop the next tick ever trying again.
    """
    if not row["EnforcedDate"]:
        return False
    from ..se import rpc

    subject = str(row["SubjectType"])
    hub = str(row["HubName"])
    name = str(row["UserName"])
    try:
        restore = json.loads(row["RestoreState"] or "{}")
    except ValueError:
        restore = {}

    try:
        if subject == "hub":
            # A hub that was already offline when the quota bit stays offline:
            # the quota did not take it down, so the quota does not raise it.
            if restore.get("Online_bool", True):
                rpc("SetHubOnline", {"HubName_str": hub, "Online_bool": True})
        else:
            current = rpc("GetUser", {"HubName_str": hub, "Name_str": name})
            body = _user_body(current)
            body["UsePolicy_bool"] = bool(restore.get("UsePolicy_bool", False))
            body["policy:Access_bool"] = bool(restore.get("policy:Access_bool", True))
            rpc("SetUser", {**body, "HubName_str": hub, "Name_str": name})
    except Exception as exc:  # noqa: BLE001 - the subject may be gone entirely
        logger.warning("could not lift the %s quota block on %s/%s: %s", subject, hub, name, exc)

    now = utc_now()
    get_db().execute(
        'UPDATE "TrafficQuota" SET "EnforcedDate" = NULL, "RestoreState" = \'\', '
        '"UpdatedDate" = :now WHERE "TrafficQuotaID" = :id',
        {"id": row["TrafficQuotaID"], "now": now},
    )
    record(
        None,
        "quota.released",
        "hub" if subject == "hub" else "vpn_user",
        hub if subject == "hub" else name,
        reason,
    )
    return True


def _cut_sessions(hub: str, username: str) -> None:
    """Disconnect what the user has open. Denying access stops the *next*
    login; a session already up would otherwise keep spending."""
    from ..se import rpc

    wanted = _key(username)
    sessions = rpc("EnumSession", {"HubName_str": hub}).get("SessionList", [])
    for session in sessions:
        if _key(str(session.get("Username_str", ""))) != wanted:
            continue
        try:
            rpc("DeleteSession", {"HubName_str": hub, "Name_str": str(session.get("Name_str", ""))})
        except Exception:  # noqa: BLE001 - a session that already dropped is not an error
            logger.debug("session %s already gone", session.get("Name_str"), exc_info=True)


def _describe(row: dict[str, Any]) -> str:
    metric = {"upload": "upload", "download": "download", "total": "combined"}[str(row["Metric"])]
    amount, unit = split_bytes(int(row["LimitBytes"]))
    return f"the {amount:g} {unit} {metric} limit"


def enforce() -> list[dict[str, Any]]:
    """Compare every quota to its limit and make the world match.

    Both directions: over and not yet blocked gets blocked; blocked and no
    longer over -- the operator raised the ceiling, reset the cycle or turned
    the quota off -- gets released.
    """
    changed: list[dict[str, Any]] = []
    for row in _all_rows():
        over = _is_over(row)
        blocked = bool(row["EnforcedDate"])
        if over and not blocked:
            if block(row):
                changed.append({"subject": row["SubjectType"], "hub": row["HubName"],
                                "username": row["UserName"], "action": "blocked"})
        elif not over and blocked:
            release(row)
            changed.append({"subject": row["SubjectType"], "hub": row["HubName"],
                            "username": row["UserName"], "action": "released"})
        elif over and not row["ExceededDate"]:
            get_db().execute(
                'UPDATE "TrafficQuota" SET "ExceededDate" = :now WHERE "TrafficQuotaID" = :id',
                {"id": row["TrafficQuotaID"], "now": utc_now()},
            )
    return changed


def observe_and_enforce(readings: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The whole cycle for a batch of counters someone else already read."""
    observe(readings)
    return enforce()


# --- the tick ------------------------------------------------------------------


def _read_counters() -> list[dict[str, Any]]:
    """Read only what some quota is actually watching.

    A hub with only user quotas costs one ``EnumUser``; a hub with only its
    own quota costs one ``GetHubStatus``. A hub nobody limits costs nothing.
    """
    from ..se import get_client

    rows = _all_rows()
    if not rows:
        return []
    wants_hub = {str(r["HubName"]) for r in rows if r["SubjectType"] == "hub"}
    wants_users = {str(r["HubName"]) for r in rows if r["SubjectType"] == "user"}

    client = get_client()
    live = {
        str(h.get("HubName_str", ""))
        for h in client.call("EnumHub", {}).raw.get("HubList", [])
        if h.get("HubName_str")
    }

    readings: list[dict[str, Any]] = []
    for hub in sorted(wants_hub & live):
        status = client.call("GetHubStatus", {"HubName_str": hub}).raw
        readings.append(
            {
                "subject": "hub",
                "hub": hub,
                "send": int(status.get("Send.UnicastBytes_u64", 0))
                + int(status.get("Send.BroadcastBytes_u64", 0)),
                "recv": int(status.get("Recv.UnicastBytes_u64", 0))
                + int(status.get("Recv.BroadcastBytes_u64", 0)),
            }
        )
    for hub in sorted(wants_users & live):
        for entry in client.call("EnumUser", {"HubName_str": hub}).raw.get("UserList", []):
            readings.append(
                {
                    "subject": "user",
                    "hub": hub,
                    "user": str(entry.get("Name_str", "")),
                    "send": int(entry.get("Ex.Send.UnicastBytes_u64", 0))
                    + int(entry.get("Ex.Send.BroadcastBytes_u64", 0)),
                    "recv": int(entry.get("Ex.Recv.UnicastBytes_u64", 0))
                    + int(entry.get("Ex.Recv.BroadcastBytes_u64", 0)),
                }
            )
    return readings


def tick() -> dict[str, Any]:
    """One enforcement pass on the quota clock.

    Separate from the traffic sampler because the two answer to different
    clocks: a chart is happy with a reading every few minutes, a ceiling
    should not be a few minutes late. The sampler still donates the counters
    it has already read (see :func:`observe_and_enforce`).
    """
    from ..se import connection
    from ..settings_store import get_setting

    if not any_configured():
        return {"quotas": 0, "skipped": "no quotas set"}
    try:
        if not bool(get_setting("quota_enforcement_enabled")):
            return {"quotas": 0, "skipped": "enforcement disabled"}
    except Exception:  # noqa: BLE001 - before the database is ready, assume on
        pass
    if not connection()["configured"]:
        return {"quotas": 0, "skipped": "not configured"}
    try:
        readings = _read_counters()
    except Exception as exc:  # noqa: BLE001 - an offline server is a gap, not a crash
        logger.debug("quota counters unavailable: %s", exc)
        return {"quotas": 0, "error": str(exc)}
    changed = observe_and_enforce(readings)
    return {"quotas": len(readings), "changed": changed}
