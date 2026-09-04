"""Hub-level SoftEther operations: /hubs/{hub}/...

Everything inside a Virtual Hub lives here -- users, groups, sessions, access
control, certificates, SecureNAT, cascade links and the address tables. Write
bodies are wire-format JSON (see :mod:`app.routers.se_server` for why).
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from ..audit import record
from ..db import get_db
from ..deps import CurrentUser
from ..se import rpc

router = APIRouter(prefix="/hubs", tags=["softether-hub"])

Wire = dict[str, Any]


# --- hubs themselves -----------------------------------------------------------


@router.get("")
def enum_hubs(user: dict = CurrentUser) -> Wire:
    return rpc("EnumHub")


@router.post("")
def create_hub(body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("CreateHub", body)
    record(user, "hub.created", "hub", str(body.get("HubName_str", "")), "")
    return out


@router.get("/{hub}")
def get_hub(hub: str, user: dict = CurrentUser) -> Wire:
    return rpc("GetHub", {"HubName_str": hub})


@router.put("/{hub}")
def set_hub(hub: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("SetHub", {**body, "HubName_str": hub})
    record(user, "hub.updated", "hub", hub, "")
    return out


@router.delete("/{hub}")
def delete_hub(hub: str, user: dict = CurrentUser) -> Wire:
    out = rpc("DeleteHub", {"HubName_str": hub})
    from ..services.quota import forget_hub

    # The hub's own ceiling and every config ceiling inside it went with it.
    forget_hub(hub)
    record(user, "hub.deleted", "hub", hub, "")
    return out


class OnlineIn(BaseModel):
    online: bool


@router.put("/{hub}/online")
def set_hub_online(hub: str, body: OnlineIn, user: dict = CurrentUser) -> Wire:
    out = rpc("SetHubOnline", {"HubName_str": hub, "Online_bool": body.online})
    record(user, "hub.online_toggled", "hub", hub, f"online={body.online}")
    return out


@router.get("/{hub}/status")
def hub_status(hub: str, user: dict = CurrentUser) -> Wire:
    return rpc("GetHubStatus", {"HubName_str": hub})


# --- hub settings: log, radius, message, options ---------------------------------


@router.get("/{hub}/log-settings")
def get_hub_log(hub: str, user: dict = CurrentUser) -> Wire:
    return rpc("GetHubLog", {"HubName_str": hub})


@router.put("/{hub}/log-settings")
def set_hub_log(hub: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("SetHubLog", {**body, "HubName_str": hub})
    record(user, "hub.log_settings_updated", "hub", hub)
    return out


@router.get("/{hub}/radius")
def get_radius(hub: str, user: dict = CurrentUser) -> Wire:
    return rpc("GetHubRadius", {"HubName_str": hub})


@router.put("/{hub}/radius")
def set_radius(hub: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("SetHubRadius", {**body, "HubName_str": hub})
    record(user, "hub.radius_updated", "hub", hub)
    return out


@router.get("/{hub}/message")
def get_hub_msg(hub: str, user: dict = CurrentUser) -> Wire:
    result = rpc("GetHubMsg", {"HubName_str": hub})
    raw = result.get("Msg_bin", "") or ""
    try:
        text = base64.b64decode(raw).decode("utf-8", "replace")
    except (ValueError, TypeError):
        text = ""
    return {"message": text}


class MessageIn(BaseModel):
    message: str


@router.put("/{hub}/message")
def set_hub_msg(hub: str, body: MessageIn, user: dict = CurrentUser) -> Wire:
    encoded = base64.b64encode(body.message.encode("utf-8")).decode("ascii")
    out = rpc("SetHubMsg", {"HubName_str": hub, "Msg_bin": encoded})
    record(user, "hub.message_updated", "hub", hub)
    return out


@router.get("/{hub}/admin-options")
def get_admin_options(hub: str, user: dict = CurrentUser) -> Wire:
    return rpc("GetHubAdminOptions", {"HubName_str": hub})


@router.put("/{hub}/admin-options")
def set_admin_options(hub: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("SetHubAdminOptions", {**body, "HubName_str": hub})
    record(user, "hub.admin_options_updated", "hub", hub)
    return out


@router.get("/{hub}/ext-options")
def get_ext_options(hub: str, user: dict = CurrentUser) -> Wire:
    return rpc("GetHubExtOptions", {"HubName_str": hub})


@router.put("/{hub}/ext-options")
def set_ext_options(hub: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("SetHubExtOptions", {**body, "HubName_str": hub})
    record(user, "hub.ext_options_updated", "hub", hub)
    return out


# --- users ------------------------------------------------------------------------


@router.get("/{hub}/users")
def enum_users(hub: str, user: dict = CurrentUser) -> Wire:
    return rpc("EnumUser", {"HubName_str": hub})


def _remember_password(hub: str, body: Wire) -> None:
    """A plaintext password passing through the panel is hashed SoftEther's
    way and cached, so .vpn files can embed it later without asking again."""
    name = str(body.get("Name_str", ""))
    password = body.get("Auth_Password_str")
    if name and password and int(body.get("AuthType_u32", 1)) == 1:
        from ..credentials import save_credential
        from ..vpnfile import hashed_password

        save_credential(hub, name, hashed_password(str(password), name))


@router.post("/{hub}/users")
def create_user(hub: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("CreateUser", {**body, "HubName_str": hub})
    _remember_password(hub, body)
    record(user, "user.created", "vpn_user", str(body.get("Name_str", "")), f"hub {hub}")
    return out


@router.get("/{hub}/users/{name}")
def get_user(hub: str, name: str, user: dict = CurrentUser) -> Wire:
    return rpc("GetUser", {"HubName_str": hub, "Name_str": name})


@router.put("/{hub}/users/{name}")
def set_user(hub: str, name: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("SetUser", {**body, "HubName_str": hub, "Name_str": name})
    _remember_password(hub, {**body, "Name_str": name})
    record(user, "user.updated", "vpn_user", name, f"hub {hub}")
    return out


@router.delete("/{hub}/users/{name}")
def delete_user(hub: str, name: str, user: dict = CurrentUser) -> Wire:
    out = rpc("DeleteUser", {"HubName_str": hub, "Name_str": name})
    from ..credentials import delete_credential
    from ..services.quota import forget_user

    delete_credential(hub, name)
    forget_user(hub, name)
    record(user, "user.deleted", "vpn_user", name, f"hub {hub}")
    return out


class VpnFileIn(BaseModel):
    host: str
    port: int = 443
    # Embed the credential so the client connects with no typing. The hash is
    # taken from what the panel has stored (or recovers from the server's own
    # config); a typed password overrides and refreshes that store.
    embed_password: bool = True
    password: str = ""
    # Naming overrides; empty falls back to the templates in the settings.
    account_name: str = ""
    filename: str = ""


@router.get("/{hub}/users/{name}/credential-state")
def user_credential_state(hub: str, name: str, user: dict = CurrentUser) -> Wire:
    """Whether a credential can be embedded without typing the password --
    checking the panel's store first, then the server's configuration file
    (which caches what it finds)."""
    from ..credentials import obtain_credential, stored_credential

    if stored_credential(hub, name):
        return {"available": True, "source": "panel"}
    found = None
    try:
        found = obtain_credential(hub, name)
    except Exception:  # the config RPC failing must not break the dialog
        pass
    return {"available": bool(found), "source": "server_config" if found else None}


@router.post("/{hub}/users/{name}/vpn-file")
def user_vpn_file(hub: str, name: str, body: VpnFileIn, user: dict = CurrentUser) -> Wire:
    """A ready-to-import SoftEther VPN Client connection file for this user.

    The user's auth type shapes the file: password users get standard hashed
    auth, RADIUS/NT users plaintext auth, certificate users a certificate
    placeholder (the certificate itself never leaves the client side).
    """
    from ..credentials import obtain_credential, save_credential
    from ..settings_store import get_setting
    from ..vpnfile import (
        DEFAULT_ACCOUNT_NAME_TEMPLATE,
        DEFAULT_FILENAME_TEMPLATE,
        build_vpn_file,
        hashed_password,
        normalize_options,
        render_name,
        safe_filename,
    )

    record_user = rpc("GetUser", {"HubName_str": hub, "Name_str": name})
    auth_type = int(record_user.get("AuthType_u32", 1))
    host = body.host.strip()

    password_hash = ""
    if body.embed_password and body.password and auth_type == 1:
        # The typed password is the freshest truth; remember it.
        password_hash = hashed_password(body.password, name)
        save_credential(hub, name, password_hash)
    elif body.embed_password and not body.password and auth_type == 1:
        password_hash = obtain_credential(hub, name) or ""
        if not password_hash:
            raise HTTPException(
                status_code=409,
                detail="No stored credential for this user — type the password once "
                "(the panel will remember it), or turn off embedding.",
            )

    names = {"hub": hub, "username": name, "host": host, "port": body.port}
    account = body.account_name.strip() or render_name(
        str(get_setting("vpn_account_name_template") or DEFAULT_ACCOUNT_NAME_TEMPLATE), **names
    )
    filename = safe_filename(
        body.filename.strip()
        or render_name(str(get_setting("vpn_filename_template") or DEFAULT_FILENAME_TEMPLATE), **names)
    )

    content = build_vpn_file(
        host=host,
        port=body.port,
        hub=hub,
        username=name,
        user_auth_type=auth_type,
        account_name=account,
        password=body.password if body.embed_password else "",
        password_hash=password_hash,
        options=normalize_options(get_setting("vpn_template")),
    )
    record(
        user,
        "user.vpn_file_generated",
        "vpn_user",
        name,
        f"hub {hub} -> {host}:{body.port}"
        + (" (credential embedded)" if password_hash or (body.embed_password and body.password) else ""),
    )
    return {"filename": filename, "content": content}


def online_usernames(hub: str) -> set[str]:
    """Case-folded names with at least one live session on this hub.

    Read from EnumSession rather than the stored history: the sampler only
    writes on its tick, and "is this user connected right now" must not be
    minutes stale. SecureNAT and bridge sessions carry no real user and are
    left out.
    """
    try:
        result = rpc("EnumSession", {"HubName_str": hub})
    except Exception:  # noqa: BLE001 - an unreachable hub means "nobody known online"
        return set()
    names: set[str] = set()
    for s in result.get("SessionList", []):
        if s.get("SecureNATMode_bool") or s.get("BridgeMode_bool") or s.get("LinkMode_bool"):
            continue
        name = str(s.get("Username_str", "")).strip()
        if name:
            names.add(name.casefold())
    return names


@router.get("/{hub}/users-online")
def hub_users_online(hub: str, user: dict = CurrentUser) -> Wire:
    """The case-folded usernames currently connected to this hub."""
    return {"HubName_str": hub, "usernames": sorted(online_usernames(hub))}


def _session_history_rows(hub: str, username: str | None, limit: int, before_id: int) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    where = ['"HubName" = :hub']
    params: dict[str, Any] = {"hub": hub, "limit": limit, "before": before_id}
    if username:
        # Stored under whatever case the client logged in with; matched the
        # way SoftEther itself matches -- without case.
        where.append('LOWER("UserName") = LOWER(:username)')
        params["username"] = username
    if before_id:
        where.append('"VpnSessionSampleID" < :before')
    return get_db().query_all(
        'SELECT "VpnSessionSampleID" AS id, "UserName" AS username, "ClientIP" AS client_ip, '
        '"ClientHostname" AS client_hostname, "StartedDate" AS started_date, '
        '"EndedDate" AS ended_date, "LastSeenDate" AS last_seen_date, '
        '"BytesTotal" AS bytes_total, "PacketsTotal" AS packets_total, '
        '"DownloadBytes" AS download_bytes, "UploadBytes" AS upload_bytes '
        f'FROM "VpnSessionSample" WHERE {" AND ".join(where)} '
        'ORDER BY "VpnSessionSampleID" DESC LIMIT :limit',
        params,
    )


@router.get("/{hub}/session-history")
def hub_session_history(
    hub: str, limit: int = 100, before_id: int = 0, user: dict = CurrentUser
) -> list[dict[str, Any]]:
    """Every login the sampler has seen on this hub -- who, from what IP,
    when, and how much it moved -- newest first."""
    return _session_history_rows(hub, None, limit, before_id)


@router.get("/{hub}/users/{name}/session-history")
def user_session_history(
    hub: str, name: str, limit: int = 100, before_id: int = 0, user: dict = CurrentUser
) -> list[dict[str, Any]]:
    return _session_history_rows(hub, name, limit, before_id)


@router.get("/{hub}/session-history/{session_id}/usage")
def session_usage(hub: str, session_id: int, user: dict = CurrentUser) -> Wire:
    """One session's traffic over its life, derived from the sampler's
    cumulative snapshots the same way every other chart here is.

    Shaped like the hub and user usage payloads so the same chart draws it:
    ``recv`` is the download, ``send`` the upload.
    """
    session = get_db().query_one(
        'SELECT "VpnSessionSampleID" AS id, "UserName" AS username, "ClientIP" AS client_ip, '
        '"StartedDate" AS started_date, "EndedDate" AS ended_date, '
        '"DownloadBytes" AS download_bytes, "UploadBytes" AS upload_bytes '
        'FROM "VpnSessionSample" WHERE "VpnSessionSampleID" = :id AND "HubName" = :hub',
        {"id": session_id, "hub": hub},
    )
    if session is None:
        raise HTTPException(status_code=404, detail="No such session in this hub's history.")

    rows = get_db().query_all(
        'SELECT "DownloadBytes", "UploadBytes", "TotalBytes", "SampledDate" '
        'FROM "VpnSessionTrafficSample" '
        'WHERE "VpnSessionSampleID" = :id ORDER BY "SampledDate"',
        {"id": session_id},
    )
    points: list[dict[str, Any]] = []
    combined: list[dict[str, Any]] = []
    total_send = total_recv = total_combined = 0
    previous: Optional[dict[str, Any]] = None

    def _delta(current: int, before: int) -> int:
        # Counters only grow within a session; a fall means the server
        # restarted and started over, so the new value is the movement.
        step = current - before
        return current if step < 0 else step

    for row in rows:
        if previous is not None:
            recv_delta = _delta(row["DownloadBytes"], previous["DownloadBytes"])
            send_delta = _delta(row["UploadBytes"], previous["UploadBytes"])
            combined_delta = _delta(row["TotalBytes"], previous["TotalBytes"])
            total_recv += recv_delta
            total_send += send_delta
            total_combined += combined_delta
            points.append({"t": row["SampledDate"], "send": send_delta, "recv": recv_delta})
            combined.append({"t": row["SampledDate"], "total": combined_delta})
        previous = row
    # The chart labels its window in hours; a session's window is its own
    # life, so that is what is reported -- at least one, so it never reads
    # as covering nothing.
    span_hours = 1
    try:
        started = datetime.fromisoformat(str(session["started_date"]).replace("Z", "+00:00"))
        ended_raw = session["ended_date"]
        ended = (
            datetime.fromisoformat(str(ended_raw).replace("Z", "+00:00"))
            if ended_raw
            else datetime.now(timezone.utc)
        )
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        span_hours = max(1, round((ended - started).total_seconds() / 3600))
    except (ValueError, TypeError):
        pass
    # SoftEther reports a direction split only for sessions with a client
    # transport of their own; SecureNAT and the like move bytes with no
    # split to report. Then the combined counter is all there is to draw,
    # and saying which one this is beats drawing zeroes as a download.
    return {
        "session": session,
        "hours": span_hours,
        "points": points,
        "total_send": total_send,
        "total_recv": total_recv,
        "samples": len(rows),
        "split_available": (total_send + total_recv) > 0,
        "combined": combined,
        "total_combined": total_combined,
    }


@router.get("/{hub}/users/{name}/sessions")
def user_sessions(hub: str, name: str, user: dict = CurrentUser) -> Wire:
    """The live sessions belonging to one user -- EnumSession, filtered."""
    result = rpc("EnumSession", {"HubName_str": hub})
    # SoftEther matches usernames case-insensitively: an account created as
    # "Saeed" logs in as "saeed" and its sessions come back under whichever
    # case the client typed. Comparing exactly hid a user's own sessions.
    wanted = name.casefold()
    sessions = [
        s for s in result.get("SessionList", [])
        if str(s.get("Username_str", "")).casefold() == wanted
    ]
    return {"HubName_str": hub, "SessionList": sessions}


@router.get("/{hub}/users/{name}/usage")
def user_usage(hub: str, name: str, hours: int = 24, user: dict = CurrentUser) -> Wire:
    """Usage over a window, derived from the sampler's cumulative snapshots.

    Counters only grow, except when the VPN server restarts and they reset;
    a negative delta is therefore read as "the counter started over" and the
    new absolute value is taken as the movement since the previous sample.
    """
    hours = max(1, min(hours, 24 * 90))
    rows = get_db().query_all(
        'SELECT "SendBytes", "RecvBytes", "NumLogin", "SampledDate" FROM "UserTrafficSample" '
        'WHERE "HubName" = :hub AND "UserName" = :name '
        "AND \"SampledDate\" >= datetime('now', :window) "
        'ORDER BY "SampledDate"',
        {"hub": hub, "name": name, "window": f"-{hours} hours"},
    )
    points: list[dict[str, Any]] = []
    total_send = total_recv = 0
    previous: Optional[dict[str, Any]] = None
    for row in rows:
        if previous is not None:
            send_delta = row["SendBytes"] - previous["SendBytes"]
            recv_delta = row["RecvBytes"] - previous["RecvBytes"]
            if send_delta < 0:
                send_delta = row["SendBytes"]
            if recv_delta < 0:
                recv_delta = row["RecvBytes"]
            total_send += send_delta
            total_recv += recv_delta
            points.append({"t": row["SampledDate"], "send": send_delta, "recv": recv_delta})
        previous = row
    return {
        "hours": hours,
        "points": points,
        "total_send": total_send,
        "total_recv": total_recv,
        "samples": len(rows),
    }


@router.get("/{hub}/traffic")
def hub_traffic(hub: str, hours: int = 24, user: dict = CurrentUser) -> Wire:
    """The hub's throughput over a window, derived from the sampler's
    snapshots the same way per-user usage is."""
    hours = max(1, min(hours, 24 * 90))
    rows = get_db().query_all(
        'SELECT "SendBytes", "RecvBytes", "NumSessions", "SampledDate" FROM "HubTrafficSample" '
        'WHERE "HubName" = :hub '
        "AND \"SampledDate\" >= datetime('now', :window) "
        'ORDER BY "SampledDate"',
        {"hub": hub, "window": f"-{hours} hours"},
    )
    points: list[dict[str, Any]] = []
    total_send = total_recv = 0
    previous: Optional[dict[str, Any]] = None
    for row in rows:
        if previous is not None:
            send_delta = row["SendBytes"] - previous["SendBytes"]
            recv_delta = row["RecvBytes"] - previous["RecvBytes"]
            if send_delta < 0:
                send_delta = row["SendBytes"]
            if recv_delta < 0:
                recv_delta = row["RecvBytes"]
            total_send += send_delta
            total_recv += recv_delta
            points.append(
                {"t": row["SampledDate"], "send": send_delta, "recv": recv_delta,
                 "sessions": row["NumSessions"]}
            )
        previous = row
    return {
        "hours": hours,
        "points": points,
        "total_send": total_send,
        "total_recv": total_recv,
        "samples": len(rows),
    }


# --- groups ----------------------------------------------------------------------


@router.get("/{hub}/groups")
def enum_groups(hub: str, user: dict = CurrentUser) -> Wire:
    return rpc("EnumGroup", {"HubName_str": hub})


@router.post("/{hub}/groups")
def create_group(hub: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("CreateGroup", {**body, "HubName_str": hub})
    record(user, "group.created", "vpn_group", str(body.get("Name_str", "")), f"hub {hub}")
    return out


@router.get("/{hub}/groups/{name}")
def get_group(hub: str, name: str, user: dict = CurrentUser) -> Wire:
    return rpc("GetGroup", {"HubName_str": hub, "Name_str": name})


@router.put("/{hub}/groups/{name}")
def set_group(hub: str, name: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("SetGroup", {**body, "HubName_str": hub, "Name_str": name})
    record(user, "group.updated", "vpn_group", name, f"hub {hub}")
    return out


@router.delete("/{hub}/groups/{name}")
def delete_group(hub: str, name: str, user: dict = CurrentUser) -> Wire:
    out = rpc("DeleteGroup", {"HubName_str": hub, "Name_str": name})
    record(user, "group.deleted", "vpn_group", name, f"hub {hub}")
    return out


# --- sessions -----------------------------------------------------------------------


@router.get("/{hub}/sessions")
def enum_sessions(hub: str, user: dict = CurrentUser) -> Wire:
    return rpc("EnumSession", {"HubName_str": hub})


@router.get("/{hub}/sessions/{name:path}")
def session_status(hub: str, name: str, user: dict = CurrentUser) -> Wire:
    return rpc("GetSessionStatus", {"HubName_str": hub, "Name_str": name})


@router.delete("/{hub}/sessions/{name:path}")
def delete_session(hub: str, name: str, user: dict = CurrentUser) -> Wire:
    out = rpc("DeleteSession", {"HubName_str": hub, "Name_str": name})
    record(user, "session.disconnected", "session", name, f"hub {hub}")
    return out


# --- access list and source-IP limits ----------------------------------------------


@router.get("/{hub}/access")
def enum_access(hub: str, user: dict = CurrentUser) -> Wire:
    return rpc("EnumAccess", {"HubName_str": hub})


@router.post("/{hub}/access")
def add_access(hub: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("AddAccess", {"HubName_str": hub, "AccessListSingle": [body]})
    record(user, "access.rule_added", "hub", hub)
    return out


@router.put("/{hub}/access")
def set_access_list(hub: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    rules = body.get("AccessList")
    if not isinstance(rules, list):
        raise HTTPException(status_code=422, detail="Body must carry an AccessList array.")
    out = rpc("SetAccessList", {"HubName_str": hub, "AccessList": rules})
    record(user, "access.list_replaced", "hub", hub, f"{len(rules)} rules")
    return out


@router.delete("/{hub}/access/{rule_id}")
def delete_access(hub: str, rule_id: int, user: dict = CurrentUser) -> Wire:
    out = rpc("DeleteAccess", {"HubName_str": hub, "Id_u32": rule_id})
    record(user, "access.rule_deleted", "hub", hub, f"rule {rule_id}")
    return out


@router.get("/{hub}/ac-list")
def get_ac_list(hub: str, user: dict = CurrentUser) -> Wire:
    return rpc("GetAcList", {"HubName_str": hub})


@router.put("/{hub}/ac-list")
def set_ac_list(hub: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    rules = body.get("ACList")
    if not isinstance(rules, list):
        raise HTTPException(status_code=422, detail="Body must carry an ACList array.")
    out = rpc("SetAcList", {"HubName_str": hub, "ACList": rules})
    record(user, "ac_list.replaced", "hub", hub, f"{len(rules)} rules")
    return out


# --- trusted CAs and the revocation list ---------------------------------------------


@router.get("/{hub}/ca")
def enum_ca(hub: str, user: dict = CurrentUser) -> Wire:
    return rpc("EnumCa", {"HubName_str": hub})


class CaIn(BaseModel):
    cert_base64: str


@router.post("/{hub}/ca")
def add_ca(hub: str, body: CaIn, user: dict = CurrentUser) -> Wire:
    out = rpc("AddCa", {"HubName_str": hub, "Cert_bin": body.cert_base64})
    record(user, "ca.added", "hub", hub)
    return out


@router.get("/{hub}/ca/{key}")
def get_ca(hub: str, key: int, user: dict = CurrentUser) -> Wire:
    return rpc("GetCa", {"HubName_str": hub, "Key_u32": key})


@router.delete("/{hub}/ca/{key}")
def delete_ca(hub: str, key: int, user: dict = CurrentUser) -> Wire:
    out = rpc("DeleteCa", {"HubName_str": hub, "Key_u32": key})
    record(user, "ca.deleted", "hub", hub, f"key {key}")
    return out


@router.get("/{hub}/crl")
def enum_crl(hub: str, user: dict = CurrentUser) -> Wire:
    return rpc("EnumCrl", {"HubName_str": hub})


@router.post("/{hub}/crl")
def add_crl(hub: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("AddCrl", {**body, "HubName_str": hub})
    record(user, "crl.added", "hub", hub)
    return out


@router.get("/{hub}/crl/{key}")
def get_crl(hub: str, key: int, user: dict = CurrentUser) -> Wire:
    return rpc("GetCrl", {"HubName_str": hub, "Key_u32": key})


@router.put("/{hub}/crl/{key}")
def set_crl(hub: str, key: int, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("SetCrl", {**body, "HubName_str": hub, "Key_u32": key})
    record(user, "crl.updated", "hub", hub, f"key {key}")
    return out


@router.delete("/{hub}/crl/{key}")
def del_crl(hub: str, key: int, user: dict = CurrentUser) -> Wire:
    out = rpc("DelCrl", {"HubName_str": hub, "Key_u32": key})
    record(user, "crl.deleted", "hub", hub, f"key {key}")
    return out


# --- SecureNAT -------------------------------------------------------------------------


@router.get("/{hub}/securenat")
def securenat_overview(hub: str, user: dict = CurrentUser) -> Wire:
    out: Wire = {}
    out["status"] = rpc("GetSecureNATStatus", {"HubName_str": hub})
    out["options"] = rpc("GetSecureNATOption", {"RpcHubName_str": hub})
    return out


@router.post("/{hub}/securenat/enable")
def enable_securenat(hub: str, user: dict = CurrentUser) -> Wire:
    out = rpc("EnableSecureNAT", {"HubName_str": hub})
    record(user, "securenat.enabled", "hub", hub)
    return out


@router.post("/{hub}/securenat/disable")
def disable_securenat(hub: str, user: dict = CurrentUser) -> Wire:
    out = rpc("DisableSecureNAT", {"HubName_str": hub})
    record(user, "securenat.disabled", "hub", hub)
    return out


@router.put("/{hub}/securenat/options")
def set_securenat_options(hub: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    # SetSecureNATOption expects the complete option struct -- the virtual
    # MAC included -- and answers "invalid parameter" to a partial one. The
    # stored options are read first and the request merged over them, so a
    # caller may send only what changed.
    current = rpc("GetSecureNATOption", {"RpcHubName_str": hub})
    out = rpc("SetSecureNATOption", {**current, **body, "RpcHubName_str": hub})
    record(user, "securenat.options_updated", "hub", hub)
    return out


@router.get("/{hub}/securenat/nat-table")
def enum_nat(hub: str, user: dict = CurrentUser) -> Wire:
    return rpc("EnumNAT", {"HubName_str": hub})


@router.get("/{hub}/securenat/dhcp-table")
def enum_dhcp(hub: str, user: dict = CurrentUser) -> Wire:
    return rpc("EnumDHCP", {"HubName_str": hub})


# --- cascade links ------------------------------------------------------------------------


@router.get("/{hub}/links")
def enum_links(hub: str, user: dict = CurrentUser) -> Wire:
    return rpc("EnumLink", {"HubName_str": hub})


@router.post("/{hub}/links")
def create_link(hub: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("CreateLink", {**body, "HubName_Ex_str": hub})
    record(user, "link.created", "hub", hub, str(body.get("AccountName_utf", "")))
    return out


@router.get("/{hub}/links/{name}")
def get_link(hub: str, name: str, user: dict = CurrentUser) -> Wire:
    return rpc("GetLink", {"HubName_Ex_str": hub, "AccountName_utf": name})


@router.put("/{hub}/links/{name}")
def set_link(hub: str, name: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("SetLink", {**body, "HubName_Ex_str": hub, "AccountName_utf": name})
    record(user, "link.updated", "hub", hub, name)
    return out


@router.delete("/{hub}/links/{name}")
def delete_link(hub: str, name: str, user: dict = CurrentUser) -> Wire:
    out = rpc("DeleteLink", {"HubName_str": hub, "AccountName_utf": name})
    record(user, "link.deleted", "hub", hub, name)
    return out


@router.get("/{hub}/links/{name}/status")
def link_status(hub: str, name: str, user: dict = CurrentUser) -> Wire:
    return rpc("GetLinkStatus", {"HubName_Ex_str": hub, "AccountName_utf": name})


class LinkOnlineIn(BaseModel):
    online: bool


@router.put("/{hub}/links/{name}/online")
def set_link_online(hub: str, name: str, body: LinkOnlineIn, user: dict = CurrentUser) -> Wire:
    method = "SetLinkOnline" if body.online else "SetLinkOffline"
    out = rpc(method, {"HubName_str": hub, "AccountName_utf": name})
    record(user, "link.online_toggled", "hub", hub, f"{name} -> {body.online}")
    return out


class LinkRenameIn(BaseModel):
    new_name: str


@router.post("/{hub}/links/{name}/rename")
def rename_link(hub: str, name: str, body: LinkRenameIn, user: dict = CurrentUser) -> Wire:
    out = rpc(
        "RenameLink",
        {"HubName_str": hub, "OldAccountName_utf": name, "NewAccountName_utf": body.new_name},
    )
    record(user, "link.renamed", "hub", hub, f"{name} -> {body.new_name}")
    return out


# --- address tables --------------------------------------------------------------------------


@router.get("/{hub}/mac-table")
def enum_mac_table(hub: str, user: dict = CurrentUser) -> Wire:
    return rpc("EnumMacTable", {"HubName_str": hub})


@router.delete("/{hub}/mac-table/{key}")
def delete_mac_entry(hub: str, key: int, user: dict = CurrentUser) -> Wire:
    out = rpc("DeleteMacTable", {"HubName_str": hub, "Key_u32": key})
    record(user, "mac_table.entry_deleted", "hub", hub, f"key {key}")
    return out


@router.get("/{hub}/ip-table")
def enum_ip_table(hub: str, user: dict = CurrentUser) -> Wire:
    return rpc("EnumIpTable", {"HubName_str": hub})


@router.delete("/{hub}/ip-table/{key}")
def delete_ip_entry(hub: str, key: int, user: dict = CurrentUser) -> Wire:
    out = rpc("DeleteIpTable", {"HubName_str": hub, "Key_u32": key})
    record(user, "ip_table.entry_deleted", "hub", hub, f"key {key}")
    return out
