"""The traffic sampler: append-only snapshots of SoftEther's counters.

SoftEther keeps *cumulative* transfer counters per user and per hub, and keeps
no history at all. The panel's usage charts therefore come from here: a
background thread snapshots the counters on a schedule into append-only sample
tables, and every chart is derived from the deltas between snapshots.

An unconfigured or unreachable server simply contributes no samples for that
tick, which the charts render as a gap -- the truthful picture.

The thread carries a third schedule that is not a sampler: traffic quota
enforcement (:mod:`app.services.quota`). It rides here because it wants the
same counters, and the traffic pass donates the ones it has already read.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ..db import get_db, utc_now
from ..settings_store import get_setting
from . import quota

logger = logging.getLogger(__name__)

_stop = threading.Event()
_thread: threading.Thread | None = None
#: Wakes early when a setting changes or on shutdown; checked every second.
_TICK = 1.0


def start() -> None:
    global _thread
    if _thread is not None:
        return
    _thread = threading.Thread(target=_loop, daemon=True, name="traffic-sampler")
    _thread.start()


def stop() -> None:
    _stop.set()


def _loop() -> None:
    # Three independent schedules share the thread: traffic ticks in minutes,
    # sessions in seconds -- session comings and goings are worth catching at
    # a resolution that would be wasteful for cumulative byte counters -- and
    # quota enforcement in seconds too, because a ceiling that bites minutes
    # late is a ceiling that leaked. The first pass runs shortly after startup
    # so a fresh install has data within a minute, not after a full interval.
    next_traffic = time.monotonic() + 15
    next_sessions = time.monotonic() + 15
    next_quota = time.monotonic() + 15
    while not _stop.is_set():
        now = time.monotonic()
        if now >= next_traffic:
            try:
                sample_traffic()
            except Exception:  # noqa: BLE001 - the sampler must never die
                logger.exception("traffic sampling failed")
            try:
                interval = max(1, int(get_setting("sample_interval_minutes")))
            except Exception:  # noqa: BLE001
                interval = 5
            next_traffic = time.monotonic() + interval * 60
        if now >= next_sessions:
            try:
                sample_sessions()
            except Exception:  # noqa: BLE001 - the sampler must never die
                logger.exception("session sampling failed")
            try:
                seconds = max(5, int(get_setting("session_interval_seconds")))
            except Exception:  # noqa: BLE001
                seconds = 60
            next_sessions = time.monotonic() + seconds
        if now >= next_quota:
            try:
                quota.tick()
            except Exception:  # noqa: BLE001 - a ceiling failing to bite must not kill the thread
                logger.exception("quota enforcement failed")
            try:
                seconds = max(10, int(get_setting("quota_interval_seconds")))
            except Exception:  # noqa: BLE001
                seconds = 60
            next_quota = time.monotonic() + seconds
        _stop.wait(_TICK)


def _flag(key: str, default: bool = True) -> bool:
    try:
        return bool(get_setting(key))
    except Exception:  # noqa: BLE001 - before the database is ready, assume on
        return default


def sample_all() -> dict[str, Any]:
    """One full pass -- traffic, sessions and quotas together. Kept for tests
    and manual invocation; the background loop schedules the three
    independently."""
    out = sample_traffic()
    out["sessions"] = sample_sessions()
    out["quotas"] = quota.tick()
    return out


def sample_sessions() -> dict[str, Any]:
    """One session-history pass over every hub: who is connected right now,
    reconciled against the open rows. Runs on its own (seconds) schedule --
    a session that lasts half a minute deserves to be seen, and cumulative
    byte counters do not need that resolution.
    """
    from ..se import connection, get_client

    if not connection()["configured"]:
        return {"hubs": 0, "skipped": "not configured"}
    if not _flag("session_monitor_enabled"):
        return {"hubs": 0, "skipped": "monitoring disabled"}

    db = get_db()
    now = utc_now()
    try:
        client = get_client()
        hubs = client.call("EnumHub", {}).raw.get("HubList", [])
        hub_names = [h.get("HubName_str", "") for h in hubs if h.get("HubName_str")]
        for hub in hub_names:
            try:
                _sample_sessions(client, db, hub, now)
            except Exception:  # noqa: BLE001 - session history is a bonus, not load-bearing
                logger.debug("session sampling failed for hub %s", hub, exc_info=True)
        return {"hubs": len(hub_names)}
    except Exception as exc:  # noqa: BLE001 - an offline server is a gap, not a crash
        logger.debug("session sampling failed: %s", exc)
        return {"hubs": 0, "error": str(exc)}


def sample_traffic() -> dict[str, Any]:
    """One traffic pass over the managed server.

    The hub and user counters that feed the usage charts. Retention pruning
    rides on this (minutes) schedule whether or not the monitor is on --
    turning monitoring off must not freeze old rows in place for ever.
    """
    from ..se import connection, get_client

    if not connection()["configured"]:
        return {"hubs": 0, "users": 0, "skipped": "not configured"}

    if not _flag("traffic_monitor_enabled"):
        db = get_db()
        _prune(db)
        return {"hubs": 0, "users": 0, "skipped": "monitoring disabled"}

    db = get_db()
    now = utc_now()
    try:
        client = get_client()
        hubs = client.call("EnumHub", {}).raw.get("HubList", [])
        hub_names = [h.get("HubName_str", "") for h in hubs if h.get("HubName_str")]

        user_rows: list[dict[str, Any]] = []
        hub_rows: list[dict[str, Any]] = []
        for hub in hub_names:
            status = client.call("GetHubStatus", {"HubName_str": hub}).raw
            hub_rows.append(
                {
                    "hub": hub,
                    "send": int(status.get("Send.UnicastBytes_u64", 0))
                    + int(status.get("Send.BroadcastBytes_u64", 0)),
                    "recv": int(status.get("Recv.UnicastBytes_u64", 0))
                    + int(status.get("Recv.BroadcastBytes_u64", 0)),
                    "sessions": int(status.get("NumSessions_u32", 0)),
                    "users": int(status.get("NumUsers_u32", 0)),
                    "now": now,
                }
            )
            users = client.call("EnumUser", {"HubName_str": hub}).raw.get("UserList", [])
            for entry in users:
                user_rows.append(
                    {
                        "hub": hub,
                        "name": entry.get("Name_str", ""),
                        "send": int(entry.get("Ex.Send.UnicastBytes_u64", 0))
                        + int(entry.get("Ex.Send.BroadcastBytes_u64", 0)),
                        "recv": int(entry.get("Ex.Recv.UnicastBytes_u64", 0))
                        + int(entry.get("Ex.Recv.BroadcastBytes_u64", 0)),
                        "logins": int(entry.get("NumLogin_u32", 0)),
                        "now": now,
                    }
                )
        if hub_rows:
            db.execute_many(
                'INSERT INTO "HubTrafficSample"("HubName", "SendBytes", "RecvBytes", '
                '"NumSessions", "NumUsers", "SampledDate") '
                "VALUES (:hub, :send, :recv, :sessions, :users, :now)",
                hub_rows,
            )
        if user_rows:
            db.execute_many(
                'INSERT INTO "UserTrafficSample"("HubName", "UserName", "SendBytes", '
                '"RecvBytes", "NumLogin", "SampledDate") '
                "VALUES (:hub, :name, :send, :recv, :logins, :now)",
                user_rows,
            )
        _prune(db)
        # The counters are already in hand, so the ceilings get a free look at
        # them -- the quota tick's own read covers the gap between these
        # slower passes.
        _feed_quotas(hub_rows, user_rows)
        return {"hubs": len(hub_rows), "users": len(user_rows)}
    except Exception as exc:  # noqa: BLE001 - an offline server is a gap, not a crash
        logger.debug("sampling failed: %s", exc)
        return {"hubs": 0, "users": 0, "error": str(exc)}


def _feed_quotas(hub_rows: list[dict[str, Any]], user_rows: list[dict[str, Any]]) -> None:
    """Hand the traffic pass's readings to the quota ledger."""
    try:
        if not bool(get_setting("quota_enforcement_enabled")):
            return
    except Exception:  # noqa: BLE001 - before the database is ready, assume on
        pass
    readings = [
        {"subject": "hub", "hub": r["hub"], "send": r["send"], "recv": r["recv"]}
        for r in hub_rows
    ] + [
        {"subject": "user", "hub": r["hub"], "user": r["name"], "send": r["send"], "recv": r["recv"]}
        for r in user_rows
    ]
    try:
        quota.observe_and_enforce(readings)
    except Exception:  # noqa: BLE001 - a ceiling failing must not lose the samples
        logger.exception("quota update from the traffic pass failed")


def _sample_sessions(client: Any, db: Any, hub: str, now: str) -> None:
    """Reconcile the live session list against ``VpnSessionSample``: a login
    not seen before opens a row, one still present refreshes its running
    totals, and one that has dropped out of the list gets its end time set
    to the last moment it was confirmed alive.

    ``PacketSize_u64``/``PacketNum_u64`` are SoftEther's own running totals
    for the session (Send+Recv combined, see ``GetTrafficPacketSize`` in
    Cedar.c). Splitting that into a download and an upload needs
    ``GetSessionStatus``, one call per live session, which also feeds the
    per-session traffic series the session charts are drawn from.
    """
    want_series = _flag("session_traffic_enabled")
    live = client.call("EnumSession", {"HubName_str": hub}).raw.get("SessionList", [])
    live_by_key = {str(s.get("UniqueId_bin", "")): s for s in live if s.get("UniqueId_bin")}

    open_rows = db.query_all(
        'SELECT "SessionKey", "StartedDate" FROM "VpnSessionSample" '
        'WHERE "HubName" = :hub AND "EndedDate" IS NULL',
        {"hub": hub},
    )
    open_keys = {r["SessionKey"] for r in open_rows}

    #: session key -> (download, upload), read once per session per tick.
    traffic: dict[str, tuple[int, int]] = {}

    for key, s in live_by_key.items():
        started = str(s.get("CreatedTime_dt") or now)
        fields = {
            "hub": hub,
            "key": key,
            "user": str(s.get("Username_str", "")),
            "ip": str(s.get("ClientIP_ip", "")),
            "hostname": str(s.get("Hostname_str", "")),
            "started": started,
            "now": now,
            "bytes": int(s.get("PacketSize_u64", 0) or 0),
            "packets": int(s.get("PacketNum_u64", 0) or 0),
        }
        down, up = (
            _session_direction_bytes(client, hub, str(s.get("Name_str", "")))
            if want_series
            else (0, 0)
        )
        traffic[key] = (down, up)
        fields["down"] = down
        fields["up"] = up
        if key in open_keys:
            db.execute(
                'UPDATE "VpnSessionSample" SET "LastSeenDate" = :now, '
                '"BytesTotal" = :bytes, "PacketsTotal" = :packets, '
                '"DownloadBytes" = :down, "UploadBytes" = :up '
                'WHERE "HubName" = :hub AND "SessionKey" = :key',
                fields,
            )
        else:
            db.execute(
                'INSERT INTO "VpnSessionSample"'
                '("HubName", "UserName", "SessionKey", "ClientIP", "ClientHostname", '
                '"StartedDate", "EndedDate", "LastSeenDate", "BytesTotal", "PacketsTotal", '
                '"DownloadBytes", "UploadBytes") '
                "VALUES (:hub, :user, :key, :ip, :hostname, :started, NULL, :now, :bytes, "
                ":packets, :down, :up) "
                'ON CONFLICT("HubName", "SessionKey") DO NOTHING',
                fields,
            )

    # The time series behind each session's usage chart: one row per live
    # session per tick, cumulative, deltas derived at read time like every
    # other chart in the panel. Written after the upserts so a session that
    # opened this tick already has its row to point at.
    if live_by_key and want_series:
        ids = {
            r["SessionKey"]: r["VpnSessionSampleID"]
            for r in db.query_all(
                'SELECT "VpnSessionSampleID", "SessionKey" FROM "VpnSessionSample" '
                'WHERE "HubName" = :hub AND "EndedDate" IS NULL',
                {"hub": hub},
            )
        }
        points = [
            {
                "id": ids[key],
                "down": int(traffic.get(key, (0, 0))[0]),
                "up": int(traffic.get(key, (0, 0))[1]),
                "total": int(live_by_key[key].get("PacketSize_u64", 0) or 0),
                "now": now,
            }
            for key in live_by_key
            if key in ids
        ]
        if points:
            db.execute_many(
                'INSERT INTO "VpnSessionTrafficSample"'
                '("VpnSessionSampleID", "DownloadBytes", "UploadBytes", "TotalBytes", '
                '"SampledDate") '
                "VALUES (:id, :down, :up, :total, :now)",
                points,
            )

    vanished = open_keys - set(live_by_key)
    if vanished:
        db.execute_many(
            'UPDATE "VpnSessionSample" SET "EndedDate" = "LastSeenDate" '
            'WHERE "HubName" = :hub AND "SessionKey" = :key AND "EndedDate" IS NULL',
            [{"hub": hub, "key": key} for key in vanished],
        )


def _session_direction_bytes(client: Any, hub: str, session_name: str) -> tuple[int, int]:
    """The session's cumulative (download, upload), from the client's side.

    SoftEther counts from the server's: what it *sent* is the client's
    download. A session that cannot be asked -- it dropped between the
    listing and this call -- contributes zeroes rather than failing the tick.
    """
    if not session_name:
        return 0, 0
    try:
        status = client.call(
            "GetSessionStatus", {"HubName_str": hub, "Name_str": session_name}
        ).raw
    except Exception:  # noqa: BLE001 - a vanished session is not an error
        return 0, 0
    return int(status.get("TotalSendSize_u64", 0) or 0), int(status.get("TotalRecvSize_u64", 0) or 0)


#: A session still marked open this long after its last confirmed sighting
#: cannot really still be running -- the hub was deleted, or the panel missed
#: every tick during a long outage. Closed at prune time so it does not sit
#: open forever.
_STALE_SESSION_HOURS = 48


def _prune(db: Any) -> None:
    try:
        days = max(1, int(get_setting("sample_retention_days")))
    except Exception:  # noqa: BLE001
        days = 90
    window = f"-{days} days"
    db.execute(
        'DELETE FROM "UserTrafficSample" WHERE "SampledDate" < datetime(\'now\', :window)',
        {"window": window},
    )
    db.execute(
        'DELETE FROM "HubTrafficSample" WHERE "SampledDate" < datetime(\'now\', :window)',
        {"window": window},
    )

    db.execute(
        'UPDATE "VpnSessionSample" SET "EndedDate" = "LastSeenDate" '
        'WHERE "EndedDate" IS NULL AND "LastSeenDate" < datetime(\'now\', :stale)',
        {"stale": f"-{_STALE_SESSION_HOURS} hours"},
    )
    try:
        session_days = max(1, int(get_setting("session_history_retention_days")))
    except Exception:  # noqa: BLE001
        session_days = 30
    db.execute(
        'DELETE FROM "VpnSessionSample" WHERE "EndedDate" IS NOT NULL '
        "AND \"EndedDate\" < datetime('now', :window)",
        {"window": f"-{session_days} days"},
    )
    # The per-session series shares the session's retention. Dated pruning
    # first, then anything orphaned by a session row that has just gone.
    db.execute(
        'DELETE FROM "VpnSessionTrafficSample" '
        "WHERE \"SampledDate\" < datetime('now', :window)",
        {"window": f"-{session_days} days"},
    )
    db.execute(
        'DELETE FROM "VpnSessionTrafficSample" WHERE "VpnSessionSampleID" NOT IN '
        '(SELECT "VpnSessionSampleID" FROM "VpnSessionSample")'
    )
