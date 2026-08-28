"""Server-level SoftEther operations on the managed local instance, under /server.

Complex write bodies are wire-format JSON objects (``Port_u32``-style keys)
passed through to the RPC after the identifying fields are injected. The
SoftEther server validates them; inventing a second schema here would only add
a place for the two to disagree. Identifying values (ports, names) travel in
the URL, panel-style.
"""
from __future__ import annotations

import base64
from typing import Any, Optional

from fastapi import APIRouter, Body
from pydantic import BaseModel

from ..audit import record
from ..deps import CurrentUser
from ..se import rpc

router = APIRouter(prefix="/server", tags=["softether-server"])

Wire = dict[str, Any]


# --- identity and health -----------------------------------------------------


@router.get("/info")
def server_info(user: dict = CurrentUser) -> Wire:
    return rpc("GetServerInfo")


@router.get("/status")
def server_status(user: dict = CurrentUser) -> Wire:
    return rpc("GetServerStatus")


@router.get("/caps")
def server_caps(user: dict = CurrentUser) -> Wire:
    return rpc("GetCaps")


@router.get("/admin-msg")
def admin_msg(user: dict = CurrentUser) -> Wire:
    return rpc("GetAdminMsg")


@router.get("/overview")
def overview(user: dict = CurrentUser) -> Wire:
    """Everything the server dashboard shows, in one round trip.

    Each section is fetched independently and failures are carried as data:
    a hub-administrator credential cannot read server-wide state, and one
    denied section must not blank the whole page.
    """
    sections = {
        "info": ("GetServerInfo", {}),
        "status": ("GetServerStatus", {}),
        "hubs": ("EnumHub", {}),
        "listeners": ("EnumListener", {}),
        "connections": ("EnumConnection", {}),
        "ipsec": ("GetIPsecServices", {}),
        "openvpn": ("GetOpenVpnSstpConfig", {}),
        "azure": ("GetAzureStatus", {}),
        "ddns": ("GetDDnsClientStatus", {}),
        "bridge_support": ("GetBridgeSupport", {}),
    }
    out: Wire = {}
    for key, (method, params) in sections.items():
        try:
            out[key] = {"ok": True, "data": rpc(method, params)}
        except Exception as exc:  # noqa: BLE001 - carried to the browser as data
            detail = getattr(exc, "detail", None)
            message = detail.get("message") if isinstance(detail, dict) else str(detail or exc)
            out[key] = {"ok": False, "error": message}
    return out


# --- listeners ----------------------------------------------------------------


class ListenerIn(BaseModel):
    port: int
    enable: bool = True


@router.get("/listeners")
def enum_listeners(user: dict = CurrentUser) -> Wire:
    return rpc("EnumListener")


@router.post("/listeners")
def create_listener(body: ListenerIn, user: dict = CurrentUser) -> Wire:
    out = rpc("CreateListener", {"Port_u32": body.port, "Enable_bool": body.enable})
    record(user, "listener.created", "server", "", f"port {body.port}")
    return out


@router.put("/listeners/{port}")
def enable_listener(port: int, body: ListenerIn, user: dict = CurrentUser) -> Wire:
    out = rpc("EnableListener", {"Port_u32": port, "Enable_bool": body.enable})
    record(user, "listener.toggled", "server", "", f"port {port} -> {body.enable}")
    return out


@router.delete("/listeners/{port}")
def delete_listener(port: int, user: dict = CurrentUser) -> Wire:
    out = rpc("DeleteListener", {"Port_u32": port})
    record(user, "listener.deleted", "server", "", f"port {port}")
    return out


@router.get("/special-listener")
def get_special_listener(user: dict = CurrentUser) -> Wire:
    return rpc("GetSpecialListener")


@router.put("/special-listener")
def set_special_listener(body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("SetSpecialListener", body)
    record(user, "special_listener.updated", "server", "")
    return out


# --- protocols: IPsec / OpenVPN+SSTP / Azure / DDNS ---------------------------


@router.get("/ipsec")
def get_ipsec(user: dict = CurrentUser) -> Wire:
    return rpc("GetIPsecServices")


@router.put("/ipsec")
def set_ipsec(body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("SetIPsecServices", body)
    record(user, "ipsec.updated", "server", "")
    return out


@router.get("/openvpn")
def get_openvpn(user: dict = CurrentUser) -> Wire:
    return rpc("GetOpenVpnSstpConfig")


@router.put("/openvpn")
def set_openvpn(body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("SetOpenVpnSstpConfig", body)
    record(user, "openvpn.updated", "server", "")
    return out


@router.post("/openvpn/sample-config")
def make_openvpn_config(user: dict = CurrentUser) -> Wire:
    result = rpc("MakeOpenVpnConfigFile")
    # The archive comes back base64; hand it over as-is with its name, and let
    # the browser turn it into a download.
    return {
        "filename": result.get("FileName_str", "openvpn_config.zip"),
        "zip_base64": result.get("Buffer_bin", ""),
    }


@router.get("/azure")
def get_azure(user: dict = CurrentUser) -> Wire:
    return rpc("GetAzureStatus")


@router.put("/azure")
def set_azure(body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("SetAzureStatus", body)
    record(user, "azure.toggled", "server", "")
    return out


@router.get("/ddns")
def get_ddns(user: dict = CurrentUser) -> Wire:
    return rpc("GetDDnsClientStatus")


class DdnsHostname(BaseModel):
    hostname: str


@router.put("/ddns/hostname")
def set_ddns_hostname(body: DdnsHostname, user: dict = CurrentUser) -> Wire:
    out = rpc("ChangeDDnsClientHostname", {"StrValue_str": body.hostname})
    record(user, "ddns.hostname_changed", "server", "", body.hostname)
    return out


@router.get("/ddns/proxy")
def get_ddns_proxy(user: dict = CurrentUser) -> Wire:
    # The upstream RPC name really is spelled "Settng".
    return rpc("GetDDnsInternetSettng")


@router.put("/ddns/proxy")
def set_ddns_proxy(body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("SetDDnsInternetSettng", body)
    record(user, "ddns.proxy_updated", "server", "")
    return out


# --- encryption, certificate, administrator password --------------------------


@router.get("/cipher")
def get_cipher(user: dict = CurrentUser) -> Wire:
    return rpc("GetServerCipher")


class CipherIn(BaseModel):
    cipher: str


@router.put("/cipher")
def set_cipher(body: CipherIn, user: dict = CurrentUser) -> Wire:
    out = rpc("SetServerCipher", {"String_str": body.cipher})
    record(user, "cipher.updated", "server", "", body.cipher)
    return out


@router.get("/cert")
def get_cert(user: dict = CurrentUser) -> Wire:
    result = rpc("GetServerCert")
    # The private key never leaves the VPN server through this panel. The
    # certificate is public by definition; the key is not needed to display
    # or verify anything.
    return {"cert_base64": result.get("Cert_bin", "")}


class CertIn(BaseModel):
    cert_base64: str
    key_base64: str


@router.put("/cert")
def set_cert(body: CertIn, user: dict = CurrentUser) -> Wire:
    out = rpc("SetServerCert", {"Cert_bin": body.cert_base64, "Key_bin": body.key_base64})
    record(user, "cert.replaced", "server", "")
    return out


class RegenerateCertIn(BaseModel):
    common_name: str


@router.post("/cert/regenerate")
def regenerate_cert(body: RegenerateCertIn, user: dict = CurrentUser) -> Wire:
    out = rpc("RegenerateServerCert", {"StrValue_str": body.common_name})
    record(user, "cert.regenerated", "server", "", body.common_name)
    return out


class AdminPasswordIn(BaseModel):
    password: str


@router.put("/admin-password")
def set_admin_password(body: AdminPasswordIn, user: dict = CurrentUser) -> Wire:
    """Change the VPN server's administrator password, and keep the panel's
    stored credential in step -- otherwise the very next call would fail."""
    from ..se import connection, set_connection

    out = rpc("SetServerPassword", {"PlainTextPassword_str": body.password})
    info = connection()
    set_connection(info["host"], info["port"], body.password)
    record(user, "server.admin_password_changed", "server", "")
    return out


# --- connections ----------------------------------------------------------------


@router.get("/connections")
def enum_connections(user: dict = CurrentUser) -> Wire:
    return rpc("EnumConnection")


@router.get("/connections/{name}")
def connection_info(name: str, user: dict = CurrentUser) -> Wire:
    return rpc("GetConnectionInfo", {"Name_str": name})


@router.delete("/connections/{name}")
def disconnect_connection(name: str, user: dict = CurrentUser) -> Wire:
    out = rpc("DisconnectConnection", {"Name_str": name})
    record(user, "connection.disconnected", "server", "", name)
    return out


# --- local bridge -----------------------------------------------------------------


@router.get("/bridges")
def enum_bridges(user: dict = CurrentUser) -> Wire:
    return rpc("EnumLocalBridge")


@router.get("/bridges/support")
def bridge_support(user: dict = CurrentUser) -> Wire:
    return rpc("GetBridgeSupport")


@router.get("/bridges/devices")
def enum_ethernet(user: dict = CurrentUser) -> Wire:
    return rpc("EnumEthernet")


class BridgeIn(BaseModel):
    device: str
    hub: str


@router.post("/bridges")
def add_bridge(body: BridgeIn, user: dict = CurrentUser) -> Wire:
    out = rpc("AddLocalBridge", {"DeviceName_str": body.device, "HubNameLB_str": body.hub})
    record(user, "bridge.created", "server", "", f"{body.device} <-> {body.hub}")
    return out


@router.post("/bridges/delete")
def delete_bridge(body: BridgeIn, user: dict = CurrentUser) -> Wire:
    # A bridge is identified by the (device, hub) pair, which does not fit in
    # a path segment; deletion is a POST with the same body as creation.
    out = rpc("DeleteLocalBridge", {"DeviceName_str": body.device, "HubNameLB_str": body.hub})
    record(user, "bridge.deleted", "server", "", f"{body.device} <-> {body.hub}")
    return out


# --- layer-3 switches ----------------------------------------------------------


@router.get("/l3")
def enum_l3(user: dict = CurrentUser) -> Wire:
    return rpc("EnumL3Switch")


class L3Name(BaseModel):
    name: str


@router.post("/l3")
def add_l3(body: L3Name, user: dict = CurrentUser) -> Wire:
    out = rpc("AddL3Switch", {"Name_str": body.name})
    record(user, "l3.created", "server", "", body.name)
    return out


@router.delete("/l3/{name}")
def del_l3(name: str, user: dict = CurrentUser) -> Wire:
    out = rpc("DelL3Switch", {"Name_str": name})
    record(user, "l3.deleted", "server", "", name)
    return out


@router.post("/l3/{name}/start")
def start_l3(name: str, user: dict = CurrentUser) -> Wire:
    out = rpc("StartL3Switch", {"Name_str": name})
    record(user, "l3.started", "server", "", name)
    return out


@router.post("/l3/{name}/stop")
def stop_l3(name: str, user: dict = CurrentUser) -> Wire:
    out = rpc("StopL3Switch", {"Name_str": name})
    record(user, "l3.stopped", "server", "", name)
    return out


@router.get("/l3/{name}/interfaces")
def enum_l3_if(name: str, user: dict = CurrentUser) -> Wire:
    return rpc("EnumL3If", {"Name_str": name})


@router.post("/l3/{name}/interfaces")
def add_l3_if(name: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("AddL3If", {**body, "Name_str": name})
    record(user, "l3.interface_added", "server", "", name)
    return out


@router.post("/l3/{name}/interfaces/delete")
def del_l3_if(name: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("DelL3If", {**body, "Name_str": name})
    record(user, "l3.interface_deleted", "server", "", name)
    return out


@router.get("/l3/{name}/routes")
def enum_l3_routes(name: str, user: dict = CurrentUser) -> Wire:
    return rpc("EnumL3Table", {"Name_str": name})


@router.post("/l3/{name}/routes")
def add_l3_route(name: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("AddL3Table", {**body, "Name_str": name})
    record(user, "l3.route_added", "server", "", name)
    return out


@router.post("/l3/{name}/routes/delete")
def del_l3_route(name: str, body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("DelL3Table", {**body, "Name_str": name})
    record(user, "l3.route_deleted", "server", "", name)
    return out


# --- clustering -----------------------------------------------------------------


@router.get("/farm")
def get_farm(user: dict = CurrentUser) -> Wire:
    return rpc("GetFarmSetting")


@router.put("/farm")
def set_farm(body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("SetFarmSetting", body)
    record(user, "farm.updated", "server", "")
    return out


@router.get("/farm/status")
def farm_status(user: dict = CurrentUser) -> Wire:
    return rpc("GetFarmConnectionStatus")


@router.get("/farm/members")
def farm_members(user: dict = CurrentUser) -> Wire:
    return rpc("EnumFarmMember")


@router.get("/farm/members/{member_id}")
def farm_member(member_id: int, user: dict = CurrentUser) -> Wire:
    return rpc("GetFarmInfo", {"Id_u32": member_id})


# --- keep-alive and syslog --------------------------------------------------------


@router.get("/keepalive")
def get_keep(user: dict = CurrentUser) -> Wire:
    return rpc("GetKeep")


@router.put("/keepalive")
def set_keep(body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("SetKeep", body)
    record(user, "keepalive.updated", "server", "")
    return out


@router.get("/syslog")
def get_syslog(user: dict = CurrentUser) -> Wire:
    return rpc("GetSysLog")


@router.put("/syslog")
def set_syslog(body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("SetSysLog", body)
    record(user, "syslog.updated", "server", "")
    return out


# --- EtherIP / L2TPv3 client ids ---------------------------------------------------


@router.get("/etherip")
def enum_etherip(user: dict = CurrentUser) -> Wire:
    return rpc("EnumEtherIpId")


@router.post("/etherip")
def add_etherip(body: Wire = Body(...), user: dict = CurrentUser) -> Wire:
    out = rpc("AddEtherIpId", body)
    record(user, "etherip.created", "server", "", str(body.get("Id_str", "")))
    return out


@router.get("/etherip/{etherip_id}")
def get_etherip(etherip_id: str, user: dict = CurrentUser) -> Wire:
    return rpc("GetEtherIpId", {"Id_str": etherip_id})


@router.delete("/etherip/{etherip_id}")
def delete_etherip(etherip_id: str, user: dict = CurrentUser) -> Wire:
    out = rpc("DeleteEtherIpId", {"Id_str": etherip_id})
    record(user, "etherip.deleted", "server", "", etherip_id)
    return out


# --- logs ------------------------------------------------------------------------


@router.get("/logs")
def enum_log_files(user: dict = CurrentUser) -> Wire:
    return rpc("EnumLogFile")


@router.get("/logs/read")
def read_log_file(path: str, offset: int = 0, user: dict = CurrentUser) -> Wire:
    params: Wire = {"FilePath_str": path}
    if offset:
        params["Offset_u32"] = offset
    result = rpc("ReadLogFile", params)
    buffer = result.get("Buffer_bin", "") or ""
    try:
        raw = base64.b64decode(buffer)
    except (ValueError, TypeError):
        raw = b""
    return {
        "path": path,
        "offset": offset,
        "text": raw.decode("utf-8", "replace"),
        "bytes": len(raw),
    }


# --- configuration file, flush, reboot, crash ---------------------------------------


@router.get("/config")
def get_config(user: dict = CurrentUser) -> Wire:
    result = rpc("GetConfig")
    return {
        "filename": result.get("FileName_str", "vpn_server.config"),
        "config_base64": result.get("FileData_bin", ""),
    }


class ConfigIn(BaseModel):
    config_base64: str


@router.put("/config")
def set_config(body: ConfigIn, user: dict = CurrentUser) -> Wire:
    out = rpc("SetConfig", {"FileData_bin": body.config_base64})
    record(user, "config.restored", "server", "")
    return out


@router.post("/flush")
def flush(user: dict = CurrentUser) -> Wire:
    out = rpc("Flush")
    record(user, "server.flushed", "server", "")
    return out


@router.post("/reboot")
def reboot(user: dict = CurrentUser) -> Wire:
    out = rpc("RebootServer")
    record(user, "server.rebooted", "server", "")
    return out


@router.post("/crash")
def crash(user: dict = CurrentUser) -> Wire:
    """Forcefully terminate the VPN server process. The most violent thing the
    API can do, exposed because the API has it -- behind its own endpoint so
    nothing can reach it by accident."""
    out = rpc("Crash")
    record(user, "server.crashed", "server", "")
    return out


# --- default hub admin options (server-wide reference data) -------------------------


@router.get("/default-hub-admin-options")
def default_hub_admin_options(hub: Optional[str] = None, user: dict = CurrentUser) -> Wire:
    return rpc("GetDefaultHubAdminOptions", {"HubName_str": hub or ""})
