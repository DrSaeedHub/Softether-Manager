# -*- coding: utf-8 -*-
"""
softether
=========

A complete, dependency-free Python client for the **SoftEther VPN Server
JSON-RPC API Suite** (the ``/api/`` endpoint exposed by SoftEther VPN Server
and VPN Bridge, June 2019 and later).

The module is deliberately *monolithic*: everything -- transport, TLS handling,
error taxonomy, type coercion, enumerations, structure builders and all
135 RPC methods -- lives in this single file.  Drop ``softether.py``
next to your code and import it; there is nothing else to install.

Quick start
-----------

::

    from softether import SoftEtherClient, UserAuthType

    with SoftEtherClient("127.0.0.1", 5555, password="secret") as c:
        print(c.get_server_info().Version)
        c.create_hub("VPN1", admin_password_plain_text="hubpass", online=True)
        c.create_user("VPN1", "alice", auth_type=UserAuthType.PASSWORD,
                      auth_password="s3cr3t")
        for s in c.enum_session("VPN1").SessionList:
            print(s.Name, s.ClientIP, s.PacketSize)

Virtual Hub administrator mode (a hub password instead of the server password)::

    c = SoftEtherClient("127.0.0.1", 5555, password="hubpass", hub="VPN1")

Design notes
------------

* **Transport** -- HTTPS 1.1 ``POST`` to ``/api/`` over a persistent
  (keep-alive) connection, per the JSON-RPC 2.0 specification.  The server
  supports neither notifications nor batches, and this client emits neither.
* **Authentication** -- the ``X-VPNADMIN-HUBNAME`` / ``X-VPNADMIN-PASSWORD``
  headers documented by SoftEther.  HTTP Basic authentication is available as
  an alternative via ``auth="basic"``.
* **Types** -- the API encodes the type of every field in its name suffix.
  This client converts transparently in both directions:

  =============  =========================  ======================================
  Suffix         Python (send)              Python (receive)
  =============  =========================  ======================================
  ``_u32``       ``int`` 0..2**32-1         ``int``
  ``_u64``       ``int`` 0..2**64-1         ``int``
  ``_str``       ``str`` (ASCII only)       ``str``
  ``_utf``       ``str`` (UTF-8)            ``str``
  ``_bin``       ``bytes`` or base64 str    ``bytes``
  ``_ip``        ``str`` / ``ipaddress``    ``str``
  ``_bool``      ``bool``                   ``bool``
  ``_dt``        ``datetime`` / str / epoch ``datetime`` (naive UTC) or ``None``
  =============  =========================  ======================================

* **Errors** -- every failure mode raises a subclass of :class:`SoftEtherError`.
  Nothing is ever returned as ``None`` to signal failure.
* **Omitted arguments** -- any keyword left at ``None`` is not sent, and the
  VPN Server then applies its own default for that field.  Mind the warning
  carried by every ``Set*`` method: those RPCs replace a whole record rather
  than patching it, so read the current values with the matching ``Get*`` call
  and pass them back unless you mean to clear them.

Generated from the official *SoftEther VPN Server JSON-RPC Suite Document*.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import http.client
import ipaddress
import json
import logging
import socket
import ssl
import threading
import time
import warnings
from enum import IntEnum
from typing import Any, Dict, Mapping, Optional, Sequence, Union

__version__ = "1.0.0"

LOG = logging.getLogger("softether")

#: Default administration port of SoftEther VPN Server.  A stock installation
#: also listens on 443 and 992.
DEFAULT_PORT = 5555

#: Wire format used by every ``_dt`` field.
_DT_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"

#: SoftEther encodes "no value" in a ``_dt`` field as one of these instants.
_DT_NULL = ("0001-01-01T00:00:00.000", "1970-01-01T00:00:00.000")

_U32_MAX = 0xFFFFFFFF
_U64_MAX = 0xFFFFFFFFFFFFFFFF


# ==========================================================================
# Exceptions
# ==========================================================================

class SoftEtherError(Exception):
    """Base class for every error raised by this module."""


class ValidationError(SoftEtherError, ValueError):
    """An argument failed client-side validation and was never sent.

    Raised for out-of-range integers, non-ASCII text in an ASCII-only field,
    malformed IP addresses, malformed base64, unknown enumeration values and
    unknown keys inside a structure.

    Attributes:
        method: RPC method the argument belonged to.
        field:  Wire name of the offending field, e.g. ``HubName_str``.
        value:  The rejected value.
    """

    def __init__(self, message: str, *, method: str = "", field: str = "", value: Any = None):
        super().__init__(message)
        self.method = method
        self.field = field
        self.value = value


class TransportError(SoftEtherError):
    """The request could not be exchanged with the server."""


class ConnectionFailedError(TransportError):
    """The TCP/TLS connection to the VPN Server could not be established."""


class TimeoutError(TransportError):  # noqa: A001 - deliberate shadow of the builtin
    """The server did not answer within the configured timeout."""


class TLSError(TransportError):
    """The TLS handshake failed (untrusted certificate, protocol mismatch, ...)."""


class CertificateFingerprintError(TLSError):
    """The server certificate did not match the pinned fingerprint."""

    def __init__(self, expected: str, actual: str):
        super().__init__(
            "server certificate fingerprint mismatch: expected %s, got %s" % (expected, actual)
        )
        self.expected = expected
        self.actual = actual


class HTTPError(TransportError):
    """The endpoint answered with a non-200 HTTP status."""

    def __init__(self, status: int, reason: str, body: str = ""):
        super().__init__("HTTP %s %s%s" % (status, reason, (": " + body[:400]) if body else ""))
        self.status = status
        self.reason = reason
        self.body = body


class AuthenticationError(SoftEtherError):
    """The administrator password (or Virtual Hub password) was rejected.

    Raised on HTTP 401/403 and on the ``ERR_AUTH_FAILED`` family of RPC errors.
    """

    code: int = 0
    name: str = ""
    message: str = ""
    method: str = ""


class ApiDisabledError(SoftEtherError):
    """``/api/`` is not served by this host.

    Either the VPN Server predates June 2019, or ``DisableJsonRpcWebApi`` is
    set to ``true`` in ``vpn_server.config``, or the port belongs to something
    else entirely.
    """


class ProtocolError(SoftEtherError):
    """The answer was not a well-formed JSON-RPC 2.0 response."""

    def __init__(self, message: str, body: str = ""):
        super().__init__(message)
        self.body = body


class RpcError(SoftEtherError):
    """The VPN Server executed the call and answered with a JSON-RPC error.

    Attributes:
        code:    Numeric SoftEther error code (``error.code``).
        name:    Symbolic name reported by the server, e.g. ``ERR_HUB_NOT_FOUND``
                 (parsed out of ``error.message``; empty when absent).
        message: Full ``error.message`` text.
        method:  RPC method that failed.
        data:    ``error.data``, when the server supplied it.
    """

    def __init__(self, code: int, message: str, *, method: str = "", data: Any = None):
        self.code = int(code)
        self.message = message or ""
        self.name = _error_symbol(self.message)
        self.method = method
        self.data = data
        super().__init__(
            "%s failed: %s (code %d)" % (method or "RPC", self.message or self.name or "error", self.code)
        )


class NotFoundError(RpcError):
    """The named object (hub, user, group, session, listener, ...) does not exist."""


class AlreadyExistsError(RpcError):
    """An object with that name already exists."""


class NotSupportedError(RpcError):
    """The call is not available in this mode (VPN Bridge, cluster member, ...)."""


class PermissionError(RpcError):  # noqa: A001 - deliberate shadow of the builtin
    """The authenticated principal lacks the privilege required by this call."""


class BusyError(RpcError):
    """The object is busy, or a capacity/licence limit was reached."""


class InvalidParameterError(RpcError):
    """The server rejected a parameter value."""


class InsecureTLSWarning(UserWarning):
    """Emitted when certificate verification is off and no fingerprint is pinned."""


# SoftEther reports the symbolic error name inside ``error.message``.  The
# numeric codes are not part of the published JSON-RPC document, so
# classification is driven by that symbol, which is stable across releases.
# Unrecognised errors surface as a plain :class:`RpcError` carrying ``code``
# and ``message`` verbatim -- never swallowed, never guessed at.
_ERROR_SYMBOL_MAP = (
    (("OBJECT_NOT_FOUND", "NOT_FOUND", "NO_SUCH", "NOT_EXISTS", "OBJECT_EXISTS_NOT"),
     NotFoundError),
    (("ALREADY_EXISTS", "OBJECT_EXISTS", "LOCAL_BRIDGE_EXISTS", "DUPLICATE"),
     AlreadyExistsError),
    (("AUTH_FAILED", "ACCESS_DENIED", "NOT_ADMINPACK", "ADMIN_PASSWORD", "PASSWORD_IS_WRONG"),
     AuthenticationError),
    (("NOT_ENOUGH_RIGHT", "SECURITY_POLICY", "ADMIN_ONLY"), PermissionError),
    (("NOT_SUPPORTED", "NOT_FARM_CONTROLLER", "NOT_CLUSTER_MEMBER", "CLUSTER_MEMBER",
      "BRIDGE_NOT_SUPPORT", "NOT_AVAILABLE", "BETA_EXPIRES", "SERVER_CANT_ACCEPT"),
     NotSupportedError),
    (("BUSY", "TOO_MANY", "CAPACITY", "LICENSE"), BusyError),
    (("INVALID_PARAMETER", "INVALID_VALUE", "INVALID_NAME", "INVALID_PROTOCOL", "INVALID_",
      "TOO_LONG", "TOO_SHORT", "NOT_RFC", "OUT_OF_RANGE"), InvalidParameterError),
)


def _error_symbol(message: Any) -> str:
    """Extract the ``ERR_XXX`` symbol from a server error message, if present."""
    if not message:
        return ""
    text = str(message).replace(",", " ").replace("(", " ").replace(")", " ")
    for token in text.split():
        if token.startswith("ERR_"):
            return token.rstrip(".:;")
    return ""


def _rpc_error(code: Any, message: Any, method: str, data: Any = None) -> SoftEtherError:
    """Build the most specific error class for a JSON-RPC error object."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = -1
    message = "" if message is None else str(message)
    symbol = _error_symbol(message) or message.upper()
    for needles, cls in _ERROR_SYMBOL_MAP:
        for needle in needles:
            if needle in symbol:
                if cls is AuthenticationError:
                    exc = AuthenticationError(
                        "%s failed: %s (code %d)" % (method or "RPC", message or symbol, code)
                    )
                    exc.code, exc.name, exc.message, exc.method = code, symbol, message, method
                    return exc
                return cls(code, message, method=method, data=data)
    return RpcError(code, message, method=method, data=data)


# ========================================================================
# Enumerations
# ========================================================================

class ServerType(IntEnum):
    """Operation mode of the VPN Server.

    Wire field ``ServerType_u32`` of GetFarmSetting, GetServerInfo, GetServerStatus,
    SetFarmSetting.
    """

    STAND_ALONE_SERVER = 0
    """Stand-alone server"""
    FARM_CONTROLLER_SERVER = 1
    """Farm controller server"""
    FARM_MEMBER_SERVER = 2
    """Farm member server"""

    # Short spellings of the members above; each one is an alias, not
    # a distinct member, so iteration and validation are unaffected.
    STANDALONE = 0
    FARM_CONTROLLER = 1
    FARM_MEMBER = 2


class OsType(IntEnum):
    """Operating system the VPN Server runs on (``OsType_u32``).

    Wire field ``OsType_u32`` of GetServerInfo.
    """

    WINDOWS_95 = 1100
    """Windows 95"""
    WINDOWS_98 = 1200
    """Windows 98"""
    WINDOWS_ME = 1300
    """Windows Me"""
    WINDOWS_UNKNOWN = 1400
    """Windows (unknown)"""
    WINDOWS_NT_4_0_WORKSTATION = 2100
    """Windows NT 4.0 Workstation"""
    WINDOWS_NT_4_0_SERVER = 2110
    """Windows NT 4.0 Server"""
    WINDOWS_NT_4_0_SERVER_ENTERPRISE_EDITION = 2111
    """Windows NT 4.0 Server, Enterprise Edition"""
    WINDOWS_NT_4_0_TERMINAL_SERVER = 2112
    """Windows NT 4.0 Terminal Server"""
    BACKOFFICE_SERVER_4_5 = 2113
    """BackOffice Server 4.5"""
    SMALL_BUSINESS_SERVER_4_5 = 2114
    """Small Business Server 4.5"""
    WINDOWS_2000_PROFESSIONAL = 2200
    """Windows 2000 Professional"""
    WINDOWS_2000_SERVER = 2211
    """Windows 2000 Server"""
    WINDOWS_2000_ADVANCED_SERVER = 2212
    """Windows 2000 Advanced Server"""
    WINDOWS_2000_DATACENTER_SERVER = 2213
    """Windows 2000 Datacenter Server"""
    BACKOFFICE_SERVER_2000 = 2214
    """BackOffice Server 2000"""
    SMALL_BUSINESS_SERVER_2000 = 2215
    """Small Business Server 2000"""
    WINDOWS_XP_HOME_EDITION = 2300
    """Windows XP Home Edition"""
    WINDOWS_XP_PROFESSIONAL = 2301
    """Windows XP Professional"""
    WINDOWS_SERVER_2003_WEB_EDITION = 2410
    """Windows Server 2003 Web Edition"""
    WINDOWS_SERVER_2003_STANDARD_EDITION = 2411
    """Windows Server 2003 Standard Edition"""
    WINDOWS_SERVER_2003_ENTERPRISE_EDITION = 2412
    """Windows Server 2003 Enterprise Edition"""
    WINDOWS_SERVER_2003_DATACENTER_EDITION = 2413
    """Windows Server 2003 DataCenter Edition"""
    BACKOFFICE_SERVER_2003 = 2414
    """BackOffice Server 2003"""
    SMALL_BUSINESS_SERVER_2003 = 2415
    """Small Business Server 2003"""
    WINDOWS_VISTA = 2500
    """Windows Vista"""
    WINDOWS_SERVER_2008 = 2510
    """Windows Server 2008"""
    WINDOWS_7 = 2600
    """Windows 7"""
    WINDOWS_SERVER_2008_R2 = 2610
    """Windows Server 2008 R2"""
    WINDOWS_8 = 2700
    """Windows 8"""
    WINDOWS_SERVER_2012 = 2710
    """Windows Server 2012"""
    WINDOWS_8_1 = 2701
    """Windows 8.1"""
    WINDOWS_SERVER_2012_R2 = 2711
    """Windows Server 2012 R2"""
    WINDOWS_10 = 2702
    """Windows 10"""
    WINDOWS_SERVER_10 = 2712
    """Windows Server 10"""
    WINDOWS_11_OR_LATER = 2800
    """Windows 11 or later"""
    WINDOWS_SERVER_11_OR_LATER = 2810
    """Windows Server 11 or later"""
    UNKNOWN_UNIX = 3000
    """Unknown UNIX"""
    LINUX = 3100
    """Linux"""
    SOLARIS = 3200
    """Solaris"""
    CYGWIN = 3300
    """Cygwin"""
    BSD = 3400
    """BSD"""
    MACOS_X = 3500
    """MacOS X"""


class HubType(IntEnum):
    """Kind of Virtual Hub, in a clustered VPN Server.

    Wire field ``HubType_u32`` of CreateHub, EnumHub, GetHub, GetHubStatus, SetHub.
    """

    STAND_ALONE_HUB = 0
    """Stand-alone HUB"""
    STATIC_HUB = 1
    """Static HUB"""
    DYNAMIC_HUB = 2
    """Dynamic HUB"""

    # Short spellings of the members above; each one is an alias, not
    # a distinct member, so iteration and validation are unaffected.
    STANDALONE = 0
    STATIC = 1
    DYNAMIC = 2


class ConnectionType(IntEnum):
    """Kind of TCP connection established with the VPN Server.

    Wire field ``Type_u32`` of EnumConnection, GetConnectionInfo.
    """

    VPN_CLIENT = 0
    """VPN Client"""
    DURING_INITIALIZATION = 1
    """During initialization"""
    LOGIN_CONNECTION = 2
    """Login connection"""
    ADDITIONAL_CONNECTION = 3
    """Additional connection"""
    RPC_FOR_SERVER_FARM = 4
    """RPC for server farm"""
    RPC_FOR_MANAGEMENT = 5
    """RPC for Management"""
    HUB_ENUMERATION = 6
    """HUB enumeration"""
    PASSWORD_CHANGE = 7
    """Password change"""
    SSTP = 8
    """SSTP"""
    OPENVPN = 9
    """OpenVPN"""


class LogSwitchType(IntEnum):
    """How often a log file is rotated.

    Wire field ``SecurityLogSwitchType_u32`` of GetHubLog, SetHubLog.
    Wire field ``PacketLogSwitchType_u32`` of GetHubLog, SetHubLog.
    """

    NO_SWITCHING = 0
    """No switching"""
    SECONDLY_BASIS = 1
    """Secondly basis"""
    MINUTELY_BASIS = 2
    """Minutely basis"""
    HOURLY_BASIS = 3
    """Hourly basis"""
    DAILY_BASIS = 4
    """Daily basis"""
    MONTHLY_BASIS = 5
    """Monthly basis"""

    # Short spellings of the members above; each one is an alias, not
    # a distinct member, so iteration and validation are unaffected.
    NONE = 0
    SECOND = 1
    MINUTE = 2
    HOUR = 3
    DAY = 4
    MONTH = 5


class PacketLogConfig(IntEnum):
    """How much of a packet is written to the packet log.

    Wire field ``PacketLogConfig_u32`` of GetHubLog, SetHubLog.
    """

    NOT_SAVE = 0
    """Not save"""
    ONLY_HEADER = 1
    """Only header"""
    ALL_PAYLOADS = 2
    """All payloads"""

    # Short spellings of the members above; each one is an alias, not
    # a distinct member, so iteration and validation are unaffected.
    NONE = 0
    HEADER = 1
    ALL = 2


class ProxyType(IntEnum):
    """How a cascade connection reaches the remote server.

    Wire field ``ProxyType_u32`` of CreateLink, GetDDnsInternetSettng, GetLink,
    SetDDnsInternetSettng, SetLink.
    """

    DIRECT_TCP_CONNECTION = 0
    """Direct TCP connection"""
    CONNECTION_VIA_HTTP_PROXY_SERVER = 1
    """Connection via HTTP proxy server"""
    CONNECTION_VIA_SOCKS_PROXY_SERVER = 2
    """Connection via SOCKS proxy server"""

    # Short spellings of the members above; each one is an alias, not
    # a distinct member, so iteration and validation are unaffected.
    DIRECT = 0
    HTTP = 1
    SOCKS = 2


class ClientAuthType(IntEnum):
    """Authentication type of a cascade connection (client side).

    Wire field ``AuthType_u32`` of CreateLink, GetLink, SetLink.
    """

    ANONYMOUS_AUTHENTICATION = 0
    """Anonymous authentication"""
    SHA_0_HASHED_PASSWORD_AUTHENTICATION = 1
    """SHA-0 hashed password authentication"""
    PLAIN_PASSWORD_AUTHENTICATION = 2
    """Plain password authentication"""
    CERTIFICATE_AUTHENTICATION = 3
    """Certificate authentication"""

    # Short spellings of the members above; each one is an alias, not
    # a distinct member, so iteration and validation are unaffected.
    ANONYMOUS = 0
    SHA0_HASHED_PASSWORD = 1
    PLAIN_PASSWORD = 2
    CERT = 3


class SessionStatus(IntEnum):
    """State of a VPN session.

    Wire field ``SessionStatus_u32`` of GetLinkStatus, GetSessionStatus.
    """

    CONNECTING = 0
    """Connecting"""
    NEGOTIATING = 1
    """Negotiating"""
    DURING_USER_AUTHENTICATION = 2
    """During user authentication"""
    CONNECTION_COMPLETE = 3
    """Connection complete"""
    WAIT_TO_RETRY = 4
    """Wait to retry"""
    IDLE_STATE = 5
    """Idle state"""


class AccessProtocol(IntEnum):
    """IP protocol number matched by an access list rule.

    Wire field ``Protocol_u32`` of AddAccess, EnumAccess, SetAccessList.
    """

    ICMP_FOR_IPV4 = 1
    """ICMP for IPv4"""
    TCP = 6
    """TCP"""
    UDP = 17
    """UDP"""
    ICMP_FOR_IPV6 = 58
    """ICMP for IPv6"""

    # Short spellings of the members above; each one is an alias, not
    # a distinct member, so iteration and validation are unaffected.
    ICMPV4 = 1
    ICMPV6 = 58


class UserAuthType(IntEnum):
    """Authentication method of a Virtual Hub user.

    Wire field ``AuthType_u32`` of CreateUser, EnumUser, GetUser, SetUser.
    """

    ANONYMOUS_AUTHENTICATION = 0
    """Anonymous authentication"""
    PASSWORD_AUTHENTICATION = 1
    """Password authentication"""
    USER_CERTIFICATE_AUTHENTICATION = 2
    """User certificate authentication"""
    ROOT_CERTIFICATE_WHICH_IS_ISSUED_BY_TRUSTED_CERTIFICATE_AUTHORITY = 3
    """Root certificate which is issued by trusted Certificate Authority"""
    RADIUS_AUTHENTICATION = 4
    """Radius authentication"""
    WINDOWS_NT_AUTHENTICATION = 5
    """Windows NT authentication"""

    # Short spellings of the members above; each one is an alias, not
    # a distinct member, so iteration and validation are unaffected.
    ANONYMOUS = 0
    PASSWORD = 1
    USER_CERT = 2
    ROOT_CERT = 3
    RADIUS = 4
    NT_DOMAIN = 5


class KeepConnectProtocol(IntEnum):
    """Transport used by the keep-alive Internet connection.

    Wire field ``KeepConnectProtocol_u32`` of GetKeep, SetKeep.
    """

    TCP = 0
    """TCP"""
    UDP = 1
    """UDP"""


class NatProtocol(IntEnum):
    """Transport used by a SecureNAT session.

    Wire field ``Protocol_u32`` of EnumNAT.
    """

    TCP = 0
    """TCP"""
    UDP = 1
    """UDP"""
    DNS = 2
    """DNS"""
    ICMP = 3
    """ICMP"""


class NatTcpStatus(IntEnum):
    """State of a SecureNAT TCP session.

    Wire field ``TcpStatus_u32`` of EnumNAT.
    """

    CONNECTING = 0
    """Connecting"""
    SEND_THE_RST_CONNECTION_FAILURE_OR_DISCONNECTED = 1
    """Send the RST (Connection failure or disconnected)"""
    CONNECTION_COMPLETE = 2
    """Connection complete"""
    CONNECTION_ESTABLISHED = 3
    """Connection established"""
    WAIT_FOR_SOCKET_DISCONNECTION = 4
    """Wait for socket disconnection"""

    # Short spellings of the members above; each one is an alias, not
    # a distinct member, so iteration and validation are unaffected.
    RST_SENT = 1


class SysLogSaveType(IntEnum):
    """What the VPN Server sends to syslog.

    Wire field ``SaveType_u32`` of GetSysLog, SetSysLog.
    """

    DO_NOT_USE_SYSLOG = 0
    """Do not use syslog"""
    ONLY_SERVER_LOG = 1
    """Only server log"""
    SERVER_AND_VIRTUAL_HUB_SECURITY_LOG = 2
    """Server and Virtual HUB security log"""
    SERVER_VIRTUAL_HUB_SECURITY_AND_PACKET_LOG = 3
    """Server, Virtual HUB security, and packet log"""

    # Short spellings of the members above; each one is an alias, not
    # a distinct member, so iteration and validation are unaffected.
    NONE = 0
    SERVER_LOG = 1
    SERVER_AND_HUB_SECURITY_LOG = 2
    ALL = 3


class PacketLogIndex(IntEnum):
    """Index into the ``PacketLogConfig_u32`` array of SetHubLog / GetHubLog."""

    TCP_CONNECTION = 0
    TCP_ALL = 1
    DHCP = 2
    UDP = 3
    ICMP = 4
    IP = 5
    ARP = 6
    ETHERNET = 7


#: Enumeration classes bound to the input fields that use them.
_ENUM_FIELDS: Dict[str, Dict[str, Any]] = {
    "CreateHub": {"HubType_u32": HubType},
    "CreateLink": {"ProxyType_u32": ProxyType, "AuthType_u32": ClientAuthType},
    "CreateUser": {"AuthType_u32": UserAuthType},
    "SetDDnsInternetSettng": {"ProxyType_u32": ProxyType},
    "SetFarmSetting": {"ServerType_u32": ServerType},
    "SetHub": {"HubType_u32": HubType},
    "SetHubLog": {"SecurityLogSwitchType_u32": LogSwitchType, "PacketLogSwitchType_u32": LogSwitchType, "PacketLogConfig_u32": PacketLogConfig},
    "SetKeep": {"KeepConnectProtocol_u32": KeepConnectProtocol},
    "SetLink": {"ProxyType_u32": ProxyType, "AuthType_u32": ClientAuthType},
    "SetSysLog": {"SaveType_u32": SysLogSaveType},
    "SetUser": {"AuthType_u32": UserAuthType},
}


# ==========================================================================
# Value coercion  (wire <-> Python)
# ==========================================================================

def _fail(msg: str, method: str, field: str, value: Any) -> "ValidationError":
    return ValidationError(
        "%s%s: %s" % (method + "." if method else "", field, msg),
        method=method, field=field, value=value,
    )


def _enc_uint(value: Any, bits: int, method: str, field: str) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, IntEnum):
        value = int(value)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int):
        raise _fail("expected an integer, got %s" % type(value).__name__, method, field, value)
    top = _U32_MAX if bits == 32 else _U64_MAX
    if value < 0 or value > top:
        raise _fail("value %d out of range for uint%d (0..%d)" % (value, bits, top),
                    method, field, value)
    return value


def _enc_ascii(value: Any, method: str, field: str) -> str:
    if isinstance(value, bytes):
        value = value.decode("ascii", "strict")
    if not isinstance(value, str):
        raise _fail("expected a string, got %s" % type(value).__name__, method, field, value)
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        raise _fail("field accepts ASCII characters only", method, field, value) from None
    if "\x00" in value:
        raise _fail("field must not contain NUL characters", method, field, value)
    return value


def _enc_utf(value: Any, method: str, field: str) -> str:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            raise _fail("bytes are not valid UTF-8", method, field, value) from None
    if not isinstance(value, str):
        raise _fail("expected a string, got %s" % type(value).__name__, method, field, value)
    if "\x00" in value:
        raise _fail("field must not contain NUL characters", method, field, value)
    return value


def _enc_bool(value: Any, method: str, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "yes", "no"):
        return value.lower() in ("true", "1", "yes")
    raise _fail("expected a boolean, got %r" % (value,), method, field, value)


def _enc_bin(value: Any, method: str, field: str) -> str:
    """Binary fields travel as base64.  Accepts bytes or an existing base64 string."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, str):
        try:
            base64.b64decode(value.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError, ValueError):
            raise _fail(
                "string value must already be base64-encoded; pass bytes to have it encoded",
                method, field, value,
            ) from None
        return value
    raise _fail("expected bytes or a base64 string, got %s" % type(value).__name__,
                method, field, value)


def _enc_ip(value: Any, method: str, field: str) -> str:
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return ""
        try:
            return str(ipaddress.ip_address(text))
        except ValueError:
            raise _fail("%r is not a valid IP address" % (value,), method, field, value) from None
    if isinstance(value, int):
        try:
            return str(ipaddress.IPv4Address(value))
        except ValueError:
            raise _fail("%r is not a valid IPv4 address" % (value,), method, field, value) from None
    raise _fail("expected an IP address, got %s" % type(value).__name__, method, field, value)


def _enc_dt(value: Any, method: str, field: str) -> str:
    if isinstance(value, datetime.datetime):
        moment = value
        if moment.tzinfo is not None:
            moment = moment.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return moment.strftime(_DT_FORMAT)[:-3]
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day).strftime(_DT_FORMAT)[:-3]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        moment = datetime.datetime.utcfromtimestamp(float(value))
        return moment.strftime(_DT_FORMAT)[:-3]
    if isinstance(value, str):
        return value
    raise _fail("expected a datetime, ISO-8601 string or POSIX timestamp, got %s"
                % type(value).__name__, method, field, value)


_ENCODERS = {
    "u32": lambda v, m, f: _enc_uint(v, 32, m, f),
    "u64": lambda v, m, f: _enc_uint(v, 64, m, f),
    "str": _enc_ascii,
    "utf": _enc_utf,
    "bin": _enc_bin,
    "ip": _enc_ip,
    "bool": _enc_bool,
    "dt": _enc_dt,
}


def _decode_value(key: str, value: Any) -> Any:
    """Convert one response value according to the type suffix of its name."""
    if isinstance(value, list):
        return [_decode_value(key, item) for item in value]
    if isinstance(value, dict):
        return RpcResult(value)
    if value is None:
        return None
    if key.endswith("_bin") and isinstance(value, str):
        try:
            return base64.b64decode(value.encode("ascii"))
        except (binascii.Error, UnicodeEncodeError, ValueError):
            return value
    if key.endswith("_dt") and isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        for candidate in (text, text[:-1] if text.endswith("Z") else text):
            for fmt in (_DT_FORMAT, "%Y-%m-%dT%H:%M:%S"):
                try:
                    parsed = datetime.datetime.strptime(candidate, fmt)
                except ValueError:
                    continue
                if parsed.year <= 1 or value in _DT_NULL:
                    return None
                return parsed
        return value
    return value


# ==========================================================================
# Result container
# ==========================================================================

class RpcResult(Dict[str, Any]):
    """The ``result`` object of a JSON-RPC answer, decoded.

    A plain :class:`dict` with the wire field names as keys and Python-native
    values (``bytes`` for ``_bin``, ``datetime`` or ``None`` for ``_dt``).
    Nested objects are wrapped recursively, so list elements behave the same
    way::

        info = client.get_server_info()
        info["Version_str"]      # wire name
        info.Version_str         # attribute
        info.Version             # attribute, suffix dropped
        info.version             # attribute, snake_case

    Some field names contain dots (``Recv.UnicastBytes_u64``).  Those are
    reachable by key, and by attribute with the dot written as nothing or as an
    underscore: ``status.Recv_UnicastBytes`` and ``status.recv_unicast_bytes``
    both work.

    ``raw`` keeps the untouched JSON for the rare case you need it.
    """

    __slots__ = ("raw",)

    def __init__(self, data: Optional[Mapping[str, Any]] = None):
        super().__init__()
        self.raw: Dict[str, Any] = dict(data or {})
        for key, value in self.raw.items():
            super().__setitem__(key, _decode_value(key, value))

    # -- lookup helpers ---------------------------------------------------
    @staticmethod
    def _normalise(name: str) -> str:
        return name.lower().replace("_", "").replace(".", "")

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError:
            pass
        target = self._normalise(item)
        for key in self:
            base = key
            for suffix in ("_u32", "_u64", "_str", "_utf", "_bin", "_ip", "_bool", "_dt"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            if self._normalise(base) == target or self._normalise(key) == target:
                return self[key]
        raise AttributeError(
            "%r has no field %r (available: %s)"
            % (type(self).__name__, item, ", ".join(sorted(self)) or "<empty>")
        )

    def field(self, name: str, default: Any = None) -> Any:
        """Fetch a field by wire name, name without suffix, or snake_case name."""
        try:
            return self.__getattr__(name)
        except AttributeError:
            return default

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        inner = ", ".join("%s=%r" % (k, v) for k, v in list(self.items())[:8])
        if len(self) > 8:
            inner += ", ..."
        return "RpcResult(%s)" % inner


# ========================================================================
# Structure definitions
# ========================================================================

#: Wire fields of every structure list accepted by an RPC, with their kinds.
_STRUCT_DEFS: Dict[str, Dict[str, str]] = {
    "ACList": {
        "Id_u32": "u32",
        "Priority_u32": "u32",
        "Deny_bool": "bool",
        "Masked_bool": "bool",
        "IpAddress_ip": "ip",
        "SubnetMask_ip": "ip",
    },
    "AccessList": {
        "Id_u32": "u32",
        "Note_utf": "utf",
        "Active_bool": "bool",
        "Priority_u32": "u32",
        "Discard_bool": "bool",
        "IsIPv6_bool": "bool",
        "SrcIpAddress_ip": "ip",
        "SrcSubnetMask_ip": "ip",
        "DestIpAddress_ip": "ip",
        "DestSubnetMask_ip": "ip",
        "SrcIpAddress6_bin": "bin",
        "SrcSubnetMask6_bin": "bin",
        "DestIpAddress6_bin": "bin",
        "DestSubnetMask6_bin": "bin",
        "Protocol_u32": "u32",
        "SrcPortStart_u32": "u32",
        "SrcPortEnd_u32": "u32",
        "DestPortStart_u32": "u32",
        "DestPortEnd_u32": "u32",
        "SrcUsername_str": "str",
        "DestUsername_str": "str",
        "CheckSrcMac_bool": "bool",
        "SrcMacAddress_bin": "bin",
        "SrcMacMask_bin": "bin",
        "CheckDstMac_bool": "bool",
        "DstMacAddress_bin": "bin",
        "DstMacMask_bin": "bin",
        "CheckTcpState_bool": "bool",
        "Established_bool": "bool",
        "Delay_u32": "u32",
        "Jitter_u32": "u32",
        "Loss_u32": "u32",
        "RedirectUrl_str": "str",
    },
    "AccessListSingle": {
        "Id_u32": "u32",
        "Note_utf": "utf",
        "Active_bool": "bool",
        "Priority_u32": "u32",
        "Discard_bool": "bool",
        "IsIPv6_bool": "bool",
        "SrcIpAddress_ip": "ip",
        "SrcSubnetMask_ip": "ip",
        "DestIpAddress_ip": "ip",
        "DestSubnetMask_ip": "ip",
        "SrcIpAddress6_bin": "bin",
        "SrcSubnetMask6_bin": "bin",
        "DestIpAddress6_bin": "bin",
        "DestSubnetMask6_bin": "bin",
        "Protocol_u32": "u32",
        "SrcPortStart_u32": "u32",
        "SrcPortEnd_u32": "u32",
        "DestPortStart_u32": "u32",
        "DestPortEnd_u32": "u32",
        "SrcUsername_str": "str",
        "DestUsername_str": "str",
        "CheckSrcMac_bool": "bool",
        "SrcMacAddress_bin": "bin",
        "SrcMacMask_bin": "bin",
        "CheckDstMac_bool": "bool",
        "DstMacAddress_bin": "bin",
        "DstMacMask_bin": "bin",
        "CheckTcpState_bool": "bool",
        "Established_bool": "bool",
        "Delay_u32": "u32",
        "Jitter_u32": "u32",
        "Loss_u32": "u32",
        "RedirectUrl_str": "str",
    },
    "AdminOptionList": {
        "Name_str": "str",
        "Value_u32": "u32",
        "Descrption_utf": "utf",
    },
    "CAList": {
        "Key_u32": "u32",
        "SubjectName_utf": "utf",
        "IssuerName_utf": "utf",
        "Expires_dt": "dt",
    },
    "CRLList": {
        "Key_u32": "u32",
        "CrlInfo_utf": "utf",
    },
    "CapsList": {
        "CapsName_str": "str",
        "CapsValue_u32": "u32",
        "CapsDescrption_utf": "utf",
    },
    "ConnectionList": {
        "Name_str": "str",
        "Hostname_str": "str",
        "Ip_ip": "ip",
        "Port_u32": "u32",
        "ConnectedTime_dt": "dt",
        "Type_u32": "u32",
    },
    "DhcpTable": {
        "Id_u32": "u32",
        "LeasedTime_dt": "dt",
        "ExpireTime_dt": "dt",
        "MacAddress_bin": "bin",
        "IpAddress_ip": "ip",
        "Mask_u32": "u32",
        "Hostname_str": "str",
    },
    "EthList": {
        "DeviceName_str": "str",
        "NetworkConnectionName_utf": "utf",
    },
    "FarmMemberList": {
        "Id_u32": "u32",
        "Controller_bool": "bool",
        "ConnectedTime_dt": "dt",
        "Ip_ip": "ip",
        "Hostname_str": "str",
        "Point_u32": "u32",
        "NumSessions_u32": "u32",
        "NumTcpConnections_u32": "u32",
        "NumHubs_u32": "u32",
        "AssignedClientLicense_u32": "u32",
        "AssignedBridgeLicense_u32": "u32",
    },
    "GroupList": {
        "Name_str": "str",
        "Realname_utf": "utf",
        "Note_utf": "utf",
        "NumUsers_u32": "u32",
        "DenyAccess_bool": "bool",
    },
    "HubList": {
        "HubName_str": "str",
        "Online_bool": "bool",
        "HubType_u32": "u32",
        "NumUsers_u32": "u32",
        "NumGroups_u32": "u32",
        "NumSessions_u32": "u32",
        "NumMacTables_u32": "u32",
        "NumIpTables_u32": "u32",
        "LastCommTime_dt": "dt",
        "LastLoginTime_dt": "dt",
        "CreatedTime_dt": "dt",
        "NumLogin_u32": "u32",
        "IsTrafficFilled_bool": "bool",
        "Ex.Recv.BroadcastBytes_u64": "u64",
        "Ex.Recv.BroadcastCount_u64": "u64",
        "Ex.Recv.UnicastBytes_u64": "u64",
        "Ex.Recv.UnicastCount_u64": "u64",
        "Ex.Send.BroadcastBytes_u64": "u64",
        "Ex.Send.BroadcastCount_u64": "u64",
        "Ex.Send.UnicastBytes_u64": "u64",
        "Ex.Send.UnicastCount_u64": "u64",
    },
    "HubsList": {
        "HubName_str": "str",
        "DynamicHub_bool": "bool",
    },
    "IpTable": {
        "Key_u32": "u32",
        "SessionName_str": "str",
        "IpAddress_ip": "ip",
        "DhcpAllocated_bool": "bool",
        "CreatedTime_dt": "dt",
        "UpdatedTime_dt": "dt",
        "RemoteItem_bool": "bool",
        "RemoteHostname_str": "str",
    },
    "L3IFList": {
        "Name_str": "str",
        "HubName_str": "str",
        "IpAddress_ip": "ip",
        "SubnetMask_ip": "ip",
    },
    "L3SWList": {
        "Name_str": "str",
        "NumInterfaces_u32": "u32",
        "NumTables_u32": "u32",
        "Active_bool": "bool",
        "Online_bool": "bool",
    },
    "L3Table": {
        "Name_str": "str",
        "NetworkAddress_ip": "ip",
        "SubnetMask_ip": "ip",
        "GatewayAddress_ip": "ip",
        "Metric_u32": "u32",
    },
    "LinkList": {
        "AccountName_utf": "utf",
        "Online_bool": "bool",
        "Connected_bool": "bool",
        "LastError_u32": "u32",
        "ConnectedTime_dt": "dt",
        "Hostname_str": "str",
        "TargetHubName_str": "str",
    },
    "ListenerList": {
        "Ports_u32": "u32",
        "Enables_bool": "bool",
        "Errors_bool": "bool",
    },
    "LocalBridgeList": {
        "DeviceName_str": "str",
        "HubNameLB_str": "str",
        "Online_bool": "bool",
        "Active_bool": "bool",
        "TapMode_bool": "bool",
    },
    "LogFiles": {
        "ServerName_str": "str",
        "FilePath_str": "str",
        "FileSize_u32": "u32",
        "UpdatedTime_dt": "dt",
    },
    "MacTable": {
        "Key_u32": "u32",
        "SessionName_str": "str",
        "MacAddress_bin": "bin",
        "CreatedTime_dt": "dt",
        "UpdatedTime_dt": "dt",
        "RemoteItem_bool": "bool",
        "RemoteHostname_str": "str",
        "VlanId_u32": "u32",
    },
    "NatTable": {
        "Id_u32": "u32",
        "Protocol_u32": "u32",
        "SrcIp_ip": "ip",
        "SrcHost_str": "str",
        "SrcPort_u32": "u32",
        "DestIp_ip": "ip",
        "DestHost_str": "str",
        "DestPort_u32": "u32",
        "CreatedTime_dt": "dt",
        "LastCommTime_dt": "dt",
        "SendSize_u64": "u64",
        "RecvSize_u64": "u64",
        "TcpStatus_u32": "u32",
    },
    "SessionList": {
        "Name_str": "str",
        "RemoteSession_bool": "bool",
        "RemoteHostname_str": "str",
        "Username_str": "str",
        "ClientIP_ip": "ip",
        "Hostname_str": "str",
        "MaxNumTcp_u32": "u32",
        "CurrentNumTcp_u32": "u32",
        "PacketSize_u64": "u64",
        "PacketNum_u64": "u64",
        "LinkMode_bool": "bool",
        "SecureNATMode_bool": "bool",
        "BridgeMode_bool": "bool",
        "Layer3Mode_bool": "bool",
        "Client_BridgeMode_bool": "bool",
        "Client_MonitorMode_bool": "bool",
        "VLanId_u32": "u32",
        "UniqueId_bin": "bin",
        "CreatedTime_dt": "dt",
        "LastCommTime_dt": "dt",
    },
    "Settings": {
        "Id_str": "str",
        "HubName_str": "str",
        "UserName_str": "str",
        "Password_str": "str",
    },
    "UserList": {
        "Name_str": "str",
        "GroupName_str": "str",
        "Realname_utf": "utf",
        "Note_utf": "utf",
        "AuthType_u32": "u32",
        "NumLogin_u32": "u32",
        "LastLoginTime_dt": "dt",
        "DenyAccess_bool": "bool",
        "IsTrafficFilled_bool": "bool",
        "IsExpiresFilled_bool": "bool",
        "Expires_dt": "dt",
        "Ex.Recv.BroadcastBytes_u64": "u64",
        "Ex.Recv.BroadcastCount_u64": "u64",
        "Ex.Recv.UnicastBytes_u64": "u64",
        "Ex.Recv.UnicastCount_u64": "u64",
        "Ex.Send.BroadcastBytes_u64": "u64",
        "Ex.Send.BroadcastCount_u64": "u64",
        "Ex.Send.UnicastBytes_u64": "u64",
        "Ex.Send.UnicastCount_u64": "u64",
    },
}

#: Case- and suffix-insensitive aliases for the fields above.
_STRUCT_ALIASES: Dict[str, Dict[str, str]] = {
    "ACList": {
        "idu32": "Id_u32",
        "id": "Id_u32",
        "priorityu32": "Priority_u32",
        "priority": "Priority_u32",
        "denybool": "Deny_bool",
        "deny": "Deny_bool",
        "maskedbool": "Masked_bool",
        "masked": "Masked_bool",
        "ipaddressip": "IpAddress_ip",
        "ipaddress": "IpAddress_ip",
        "subnetmaskip": "SubnetMask_ip",
        "subnetmask": "SubnetMask_ip",
    },
    "AccessList": {
        "idu32": "Id_u32",
        "id": "Id_u32",
        "noteutf": "Note_utf",
        "note": "Note_utf",
        "activebool": "Active_bool",
        "active": "Active_bool",
        "priorityu32": "Priority_u32",
        "priority": "Priority_u32",
        "discardbool": "Discard_bool",
        "discard": "Discard_bool",
        "isipv6bool": "IsIPv6_bool",
        "isipv6": "IsIPv6_bool",
        "srcipaddressip": "SrcIpAddress_ip",
        "srcipaddress": "SrcIpAddress_ip",
        "srcsubnetmaskip": "SrcSubnetMask_ip",
        "srcsubnetmask": "SrcSubnetMask_ip",
        "destipaddressip": "DestIpAddress_ip",
        "destipaddress": "DestIpAddress_ip",
        "destsubnetmaskip": "DestSubnetMask_ip",
        "destsubnetmask": "DestSubnetMask_ip",
        "srcipaddress6bin": "SrcIpAddress6_bin",
        "srcipaddress6": "SrcIpAddress6_bin",
        "srcsubnetmask6bin": "SrcSubnetMask6_bin",
        "srcsubnetmask6": "SrcSubnetMask6_bin",
        "destipaddress6bin": "DestIpAddress6_bin",
        "destipaddress6": "DestIpAddress6_bin",
        "destsubnetmask6bin": "DestSubnetMask6_bin",
        "destsubnetmask6": "DestSubnetMask6_bin",
        "protocolu32": "Protocol_u32",
        "protocol": "Protocol_u32",
        "srcportstartu32": "SrcPortStart_u32",
        "srcportstart": "SrcPortStart_u32",
        "srcportendu32": "SrcPortEnd_u32",
        "srcportend": "SrcPortEnd_u32",
        "destportstartu32": "DestPortStart_u32",
        "destportstart": "DestPortStart_u32",
        "destportendu32": "DestPortEnd_u32",
        "destportend": "DestPortEnd_u32",
        "srcusernamestr": "SrcUsername_str",
        "srcusername": "SrcUsername_str",
        "destusernamestr": "DestUsername_str",
        "destusername": "DestUsername_str",
        "checksrcmacbool": "CheckSrcMac_bool",
        "checksrcmac": "CheckSrcMac_bool",
        "srcmacaddressbin": "SrcMacAddress_bin",
        "srcmacaddress": "SrcMacAddress_bin",
        "srcmacmaskbin": "SrcMacMask_bin",
        "srcmacmask": "SrcMacMask_bin",
        "checkdstmacbool": "CheckDstMac_bool",
        "checkdstmac": "CheckDstMac_bool",
        "dstmacaddressbin": "DstMacAddress_bin",
        "dstmacaddress": "DstMacAddress_bin",
        "dstmacmaskbin": "DstMacMask_bin",
        "dstmacmask": "DstMacMask_bin",
        "checktcpstatebool": "CheckTcpState_bool",
        "checktcpstate": "CheckTcpState_bool",
        "establishedbool": "Established_bool",
        "established": "Established_bool",
        "delayu32": "Delay_u32",
        "delay": "Delay_u32",
        "jitteru32": "Jitter_u32",
        "jitter": "Jitter_u32",
        "lossu32": "Loss_u32",
        "loss": "Loss_u32",
        "redirecturlstr": "RedirectUrl_str",
        "redirecturl": "RedirectUrl_str",
    },
    "AccessListSingle": {
        "idu32": "Id_u32",
        "id": "Id_u32",
        "noteutf": "Note_utf",
        "note": "Note_utf",
        "activebool": "Active_bool",
        "active": "Active_bool",
        "priorityu32": "Priority_u32",
        "priority": "Priority_u32",
        "discardbool": "Discard_bool",
        "discard": "Discard_bool",
        "isipv6bool": "IsIPv6_bool",
        "isipv6": "IsIPv6_bool",
        "srcipaddressip": "SrcIpAddress_ip",
        "srcipaddress": "SrcIpAddress_ip",
        "srcsubnetmaskip": "SrcSubnetMask_ip",
        "srcsubnetmask": "SrcSubnetMask_ip",
        "destipaddressip": "DestIpAddress_ip",
        "destipaddress": "DestIpAddress_ip",
        "destsubnetmaskip": "DestSubnetMask_ip",
        "destsubnetmask": "DestSubnetMask_ip",
        "srcipaddress6bin": "SrcIpAddress6_bin",
        "srcipaddress6": "SrcIpAddress6_bin",
        "srcsubnetmask6bin": "SrcSubnetMask6_bin",
        "srcsubnetmask6": "SrcSubnetMask6_bin",
        "destipaddress6bin": "DestIpAddress6_bin",
        "destipaddress6": "DestIpAddress6_bin",
        "destsubnetmask6bin": "DestSubnetMask6_bin",
        "destsubnetmask6": "DestSubnetMask6_bin",
        "protocolu32": "Protocol_u32",
        "protocol": "Protocol_u32",
        "srcportstartu32": "SrcPortStart_u32",
        "srcportstart": "SrcPortStart_u32",
        "srcportendu32": "SrcPortEnd_u32",
        "srcportend": "SrcPortEnd_u32",
        "destportstartu32": "DestPortStart_u32",
        "destportstart": "DestPortStart_u32",
        "destportendu32": "DestPortEnd_u32",
        "destportend": "DestPortEnd_u32",
        "srcusernamestr": "SrcUsername_str",
        "srcusername": "SrcUsername_str",
        "destusernamestr": "DestUsername_str",
        "destusername": "DestUsername_str",
        "checksrcmacbool": "CheckSrcMac_bool",
        "checksrcmac": "CheckSrcMac_bool",
        "srcmacaddressbin": "SrcMacAddress_bin",
        "srcmacaddress": "SrcMacAddress_bin",
        "srcmacmaskbin": "SrcMacMask_bin",
        "srcmacmask": "SrcMacMask_bin",
        "checkdstmacbool": "CheckDstMac_bool",
        "checkdstmac": "CheckDstMac_bool",
        "dstmacaddressbin": "DstMacAddress_bin",
        "dstmacaddress": "DstMacAddress_bin",
        "dstmacmaskbin": "DstMacMask_bin",
        "dstmacmask": "DstMacMask_bin",
        "checktcpstatebool": "CheckTcpState_bool",
        "checktcpstate": "CheckTcpState_bool",
        "establishedbool": "Established_bool",
        "established": "Established_bool",
        "delayu32": "Delay_u32",
        "delay": "Delay_u32",
        "jitteru32": "Jitter_u32",
        "jitter": "Jitter_u32",
        "lossu32": "Loss_u32",
        "loss": "Loss_u32",
        "redirecturlstr": "RedirectUrl_str",
        "redirecturl": "RedirectUrl_str",
    },
    "AdminOptionList": {
        "namestr": "Name_str",
        "name": "Name_str",
        "valueu32": "Value_u32",
        "value": "Value_u32",
        "descrptionutf": "Descrption_utf",
        "descrption": "Descrption_utf",
    },
    "CAList": {
        "keyu32": "Key_u32",
        "key": "Key_u32",
        "subjectnameutf": "SubjectName_utf",
        "subjectname": "SubjectName_utf",
        "issuernameutf": "IssuerName_utf",
        "issuername": "IssuerName_utf",
        "expiresdt": "Expires_dt",
        "expires": "Expires_dt",
    },
    "CRLList": {
        "keyu32": "Key_u32",
        "key": "Key_u32",
        "crlinfoutf": "CrlInfo_utf",
        "crlinfo": "CrlInfo_utf",
    },
    "CapsList": {
        "capsnamestr": "CapsName_str",
        "capsname": "CapsName_str",
        "capsvalueu32": "CapsValue_u32",
        "capsvalue": "CapsValue_u32",
        "capsdescrptionutf": "CapsDescrption_utf",
        "capsdescrption": "CapsDescrption_utf",
    },
    "ConnectionList": {
        "namestr": "Name_str",
        "name": "Name_str",
        "hostnamestr": "Hostname_str",
        "hostname": "Hostname_str",
        "ipip": "Ip_ip",
        "ip": "Ip_ip",
        "portu32": "Port_u32",
        "port": "Port_u32",
        "connectedtimedt": "ConnectedTime_dt",
        "connectedtime": "ConnectedTime_dt",
        "typeu32": "Type_u32",
        "type": "Type_u32",
    },
    "DhcpTable": {
        "idu32": "Id_u32",
        "id": "Id_u32",
        "leasedtimedt": "LeasedTime_dt",
        "leasedtime": "LeasedTime_dt",
        "expiretimedt": "ExpireTime_dt",
        "expiretime": "ExpireTime_dt",
        "macaddressbin": "MacAddress_bin",
        "macaddress": "MacAddress_bin",
        "ipaddressip": "IpAddress_ip",
        "ipaddress": "IpAddress_ip",
        "masku32": "Mask_u32",
        "mask": "Mask_u32",
        "hostnamestr": "Hostname_str",
        "hostname": "Hostname_str",
    },
    "EthList": {
        "devicenamestr": "DeviceName_str",
        "devicename": "DeviceName_str",
        "networkconnectionnameutf": "NetworkConnectionName_utf",
        "networkconnectionname": "NetworkConnectionName_utf",
    },
    "FarmMemberList": {
        "idu32": "Id_u32",
        "id": "Id_u32",
        "controllerbool": "Controller_bool",
        "controller": "Controller_bool",
        "connectedtimedt": "ConnectedTime_dt",
        "connectedtime": "ConnectedTime_dt",
        "ipip": "Ip_ip",
        "ip": "Ip_ip",
        "hostnamestr": "Hostname_str",
        "hostname": "Hostname_str",
        "pointu32": "Point_u32",
        "point": "Point_u32",
        "numsessionsu32": "NumSessions_u32",
        "numsessions": "NumSessions_u32",
        "numtcpconnectionsu32": "NumTcpConnections_u32",
        "numtcpconnections": "NumTcpConnections_u32",
        "numhubsu32": "NumHubs_u32",
        "numhubs": "NumHubs_u32",
        "assignedclientlicenseu32": "AssignedClientLicense_u32",
        "assignedclientlicense": "AssignedClientLicense_u32",
        "assignedbridgelicenseu32": "AssignedBridgeLicense_u32",
        "assignedbridgelicense": "AssignedBridgeLicense_u32",
    },
    "GroupList": {
        "namestr": "Name_str",
        "name": "Name_str",
        "realnameutf": "Realname_utf",
        "realname": "Realname_utf",
        "noteutf": "Note_utf",
        "note": "Note_utf",
        "numusersu32": "NumUsers_u32",
        "numusers": "NumUsers_u32",
        "denyaccessbool": "DenyAccess_bool",
        "denyaccess": "DenyAccess_bool",
    },
    "HubList": {
        "hubnamestr": "HubName_str",
        "hubname": "HubName_str",
        "onlinebool": "Online_bool",
        "online": "Online_bool",
        "hubtypeu32": "HubType_u32",
        "hubtype": "HubType_u32",
        "numusersu32": "NumUsers_u32",
        "numusers": "NumUsers_u32",
        "numgroupsu32": "NumGroups_u32",
        "numgroups": "NumGroups_u32",
        "numsessionsu32": "NumSessions_u32",
        "numsessions": "NumSessions_u32",
        "nummactablesu32": "NumMacTables_u32",
        "nummactables": "NumMacTables_u32",
        "numiptablesu32": "NumIpTables_u32",
        "numiptables": "NumIpTables_u32",
        "lastcommtimedt": "LastCommTime_dt",
        "lastcommtime": "LastCommTime_dt",
        "lastlogintimedt": "LastLoginTime_dt",
        "lastlogintime": "LastLoginTime_dt",
        "createdtimedt": "CreatedTime_dt",
        "createdtime": "CreatedTime_dt",
        "numloginu32": "NumLogin_u32",
        "numlogin": "NumLogin_u32",
        "istrafficfilledbool": "IsTrafficFilled_bool",
        "istrafficfilled": "IsTrafficFilled_bool",
        "ex.recv.broadcastbytesu64": "Ex.Recv.BroadcastBytes_u64",
        "ex.recv.broadcastbytes": "Ex.Recv.BroadcastBytes_u64",
        "exrecvbroadcastbytes": "Ex.Recv.BroadcastBytes_u64",
        "ex.recv.broadcastcountu64": "Ex.Recv.BroadcastCount_u64",
        "ex.recv.broadcastcount": "Ex.Recv.BroadcastCount_u64",
        "exrecvbroadcastcount": "Ex.Recv.BroadcastCount_u64",
        "ex.recv.unicastbytesu64": "Ex.Recv.UnicastBytes_u64",
        "ex.recv.unicastbytes": "Ex.Recv.UnicastBytes_u64",
        "exrecvunicastbytes": "Ex.Recv.UnicastBytes_u64",
        "ex.recv.unicastcountu64": "Ex.Recv.UnicastCount_u64",
        "ex.recv.unicastcount": "Ex.Recv.UnicastCount_u64",
        "exrecvunicastcount": "Ex.Recv.UnicastCount_u64",
        "ex.send.broadcastbytesu64": "Ex.Send.BroadcastBytes_u64",
        "ex.send.broadcastbytes": "Ex.Send.BroadcastBytes_u64",
        "exsendbroadcastbytes": "Ex.Send.BroadcastBytes_u64",
        "ex.send.broadcastcountu64": "Ex.Send.BroadcastCount_u64",
        "ex.send.broadcastcount": "Ex.Send.BroadcastCount_u64",
        "exsendbroadcastcount": "Ex.Send.BroadcastCount_u64",
        "ex.send.unicastbytesu64": "Ex.Send.UnicastBytes_u64",
        "ex.send.unicastbytes": "Ex.Send.UnicastBytes_u64",
        "exsendunicastbytes": "Ex.Send.UnicastBytes_u64",
        "ex.send.unicastcountu64": "Ex.Send.UnicastCount_u64",
        "ex.send.unicastcount": "Ex.Send.UnicastCount_u64",
        "exsendunicastcount": "Ex.Send.UnicastCount_u64",
    },
    "HubsList": {
        "hubnamestr": "HubName_str",
        "hubname": "HubName_str",
        "dynamichubbool": "DynamicHub_bool",
        "dynamichub": "DynamicHub_bool",
    },
    "IpTable": {
        "keyu32": "Key_u32",
        "key": "Key_u32",
        "sessionnamestr": "SessionName_str",
        "sessionname": "SessionName_str",
        "ipaddressip": "IpAddress_ip",
        "ipaddress": "IpAddress_ip",
        "dhcpallocatedbool": "DhcpAllocated_bool",
        "dhcpallocated": "DhcpAllocated_bool",
        "createdtimedt": "CreatedTime_dt",
        "createdtime": "CreatedTime_dt",
        "updatedtimedt": "UpdatedTime_dt",
        "updatedtime": "UpdatedTime_dt",
        "remoteitembool": "RemoteItem_bool",
        "remoteitem": "RemoteItem_bool",
        "remotehostnamestr": "RemoteHostname_str",
        "remotehostname": "RemoteHostname_str",
    },
    "L3IFList": {
        "namestr": "Name_str",
        "name": "Name_str",
        "hubnamestr": "HubName_str",
        "hubname": "HubName_str",
        "ipaddressip": "IpAddress_ip",
        "ipaddress": "IpAddress_ip",
        "subnetmaskip": "SubnetMask_ip",
        "subnetmask": "SubnetMask_ip",
    },
    "L3SWList": {
        "namestr": "Name_str",
        "name": "Name_str",
        "numinterfacesu32": "NumInterfaces_u32",
        "numinterfaces": "NumInterfaces_u32",
        "numtablesu32": "NumTables_u32",
        "numtables": "NumTables_u32",
        "activebool": "Active_bool",
        "active": "Active_bool",
        "onlinebool": "Online_bool",
        "online": "Online_bool",
    },
    "L3Table": {
        "namestr": "Name_str",
        "name": "Name_str",
        "networkaddressip": "NetworkAddress_ip",
        "networkaddress": "NetworkAddress_ip",
        "subnetmaskip": "SubnetMask_ip",
        "subnetmask": "SubnetMask_ip",
        "gatewayaddressip": "GatewayAddress_ip",
        "gatewayaddress": "GatewayAddress_ip",
        "metricu32": "Metric_u32",
        "metric": "Metric_u32",
    },
    "LinkList": {
        "accountnameutf": "AccountName_utf",
        "accountname": "AccountName_utf",
        "onlinebool": "Online_bool",
        "online": "Online_bool",
        "connectedbool": "Connected_bool",
        "connected": "Connected_bool",
        "lasterroru32": "LastError_u32",
        "lasterror": "LastError_u32",
        "connectedtimedt": "ConnectedTime_dt",
        "connectedtime": "ConnectedTime_dt",
        "hostnamestr": "Hostname_str",
        "hostname": "Hostname_str",
        "targethubnamestr": "TargetHubName_str",
        "targethubname": "TargetHubName_str",
    },
    "ListenerList": {
        "portsu32": "Ports_u32",
        "ports": "Ports_u32",
        "enablesbool": "Enables_bool",
        "enables": "Enables_bool",
        "errorsbool": "Errors_bool",
        "errors": "Errors_bool",
    },
    "LocalBridgeList": {
        "devicenamestr": "DeviceName_str",
        "devicename": "DeviceName_str",
        "hubnamelbstr": "HubNameLB_str",
        "hubnamelb": "HubNameLB_str",
        "onlinebool": "Online_bool",
        "online": "Online_bool",
        "activebool": "Active_bool",
        "active": "Active_bool",
        "tapmodebool": "TapMode_bool",
        "tapmode": "TapMode_bool",
    },
    "LogFiles": {
        "servernamestr": "ServerName_str",
        "servername": "ServerName_str",
        "filepathstr": "FilePath_str",
        "filepath": "FilePath_str",
        "filesizeu32": "FileSize_u32",
        "filesize": "FileSize_u32",
        "updatedtimedt": "UpdatedTime_dt",
        "updatedtime": "UpdatedTime_dt",
    },
    "MacTable": {
        "keyu32": "Key_u32",
        "key": "Key_u32",
        "sessionnamestr": "SessionName_str",
        "sessionname": "SessionName_str",
        "macaddressbin": "MacAddress_bin",
        "macaddress": "MacAddress_bin",
        "createdtimedt": "CreatedTime_dt",
        "createdtime": "CreatedTime_dt",
        "updatedtimedt": "UpdatedTime_dt",
        "updatedtime": "UpdatedTime_dt",
        "remoteitembool": "RemoteItem_bool",
        "remoteitem": "RemoteItem_bool",
        "remotehostnamestr": "RemoteHostname_str",
        "remotehostname": "RemoteHostname_str",
        "vlanidu32": "VlanId_u32",
        "vlanid": "VlanId_u32",
    },
    "NatTable": {
        "idu32": "Id_u32",
        "id": "Id_u32",
        "protocolu32": "Protocol_u32",
        "protocol": "Protocol_u32",
        "srcipip": "SrcIp_ip",
        "srcip": "SrcIp_ip",
        "srchoststr": "SrcHost_str",
        "srchost": "SrcHost_str",
        "srcportu32": "SrcPort_u32",
        "srcport": "SrcPort_u32",
        "destipip": "DestIp_ip",
        "destip": "DestIp_ip",
        "desthoststr": "DestHost_str",
        "desthost": "DestHost_str",
        "destportu32": "DestPort_u32",
        "destport": "DestPort_u32",
        "createdtimedt": "CreatedTime_dt",
        "createdtime": "CreatedTime_dt",
        "lastcommtimedt": "LastCommTime_dt",
        "lastcommtime": "LastCommTime_dt",
        "sendsizeu64": "SendSize_u64",
        "sendsize": "SendSize_u64",
        "recvsizeu64": "RecvSize_u64",
        "recvsize": "RecvSize_u64",
        "tcpstatusu32": "TcpStatus_u32",
        "tcpstatus": "TcpStatus_u32",
    },
    "SessionList": {
        "namestr": "Name_str",
        "name": "Name_str",
        "remotesessionbool": "RemoteSession_bool",
        "remotesession": "RemoteSession_bool",
        "remotehostnamestr": "RemoteHostname_str",
        "remotehostname": "RemoteHostname_str",
        "usernamestr": "Username_str",
        "username": "Username_str",
        "clientipip": "ClientIP_ip",
        "clientip": "ClientIP_ip",
        "hostnamestr": "Hostname_str",
        "hostname": "Hostname_str",
        "maxnumtcpu32": "MaxNumTcp_u32",
        "maxnumtcp": "MaxNumTcp_u32",
        "currentnumtcpu32": "CurrentNumTcp_u32",
        "currentnumtcp": "CurrentNumTcp_u32",
        "packetsizeu64": "PacketSize_u64",
        "packetsize": "PacketSize_u64",
        "packetnumu64": "PacketNum_u64",
        "packetnum": "PacketNum_u64",
        "linkmodebool": "LinkMode_bool",
        "linkmode": "LinkMode_bool",
        "securenatmodebool": "SecureNATMode_bool",
        "securenatmode": "SecureNATMode_bool",
        "bridgemodebool": "BridgeMode_bool",
        "bridgemode": "BridgeMode_bool",
        "layer3modebool": "Layer3Mode_bool",
        "layer3mode": "Layer3Mode_bool",
        "clientbridgemodebool": "Client_BridgeMode_bool",
        "clientbridgemode": "Client_BridgeMode_bool",
        "clientmonitormodebool": "Client_MonitorMode_bool",
        "clientmonitormode": "Client_MonitorMode_bool",
        "vlanidu32": "VLanId_u32",
        "vlanid": "VLanId_u32",
        "uniqueidbin": "UniqueId_bin",
        "uniqueid": "UniqueId_bin",
        "createdtimedt": "CreatedTime_dt",
        "createdtime": "CreatedTime_dt",
        "lastcommtimedt": "LastCommTime_dt",
        "lastcommtime": "LastCommTime_dt",
    },
    "Settings": {
        "idstr": "Id_str",
        "id": "Id_str",
        "hubnamestr": "HubName_str",
        "hubname": "HubName_str",
        "usernamestr": "UserName_str",
        "username": "UserName_str",
        "passwordstr": "Password_str",
        "password": "Password_str",
    },
    "UserList": {
        "namestr": "Name_str",
        "name": "Name_str",
        "groupnamestr": "GroupName_str",
        "groupname": "GroupName_str",
        "realnameutf": "Realname_utf",
        "realname": "Realname_utf",
        "noteutf": "Note_utf",
        "note": "Note_utf",
        "authtypeu32": "AuthType_u32",
        "authtype": "AuthType_u32",
        "numloginu32": "NumLogin_u32",
        "numlogin": "NumLogin_u32",
        "lastlogintimedt": "LastLoginTime_dt",
        "lastlogintime": "LastLoginTime_dt",
        "denyaccessbool": "DenyAccess_bool",
        "denyaccess": "DenyAccess_bool",
        "istrafficfilledbool": "IsTrafficFilled_bool",
        "istrafficfilled": "IsTrafficFilled_bool",
        "isexpiresfilledbool": "IsExpiresFilled_bool",
        "isexpiresfilled": "IsExpiresFilled_bool",
        "expiresdt": "Expires_dt",
        "expires": "Expires_dt",
        "ex.recv.broadcastbytesu64": "Ex.Recv.BroadcastBytes_u64",
        "ex.recv.broadcastbytes": "Ex.Recv.BroadcastBytes_u64",
        "exrecvbroadcastbytes": "Ex.Recv.BroadcastBytes_u64",
        "ex.recv.broadcastcountu64": "Ex.Recv.BroadcastCount_u64",
        "ex.recv.broadcastcount": "Ex.Recv.BroadcastCount_u64",
        "exrecvbroadcastcount": "Ex.Recv.BroadcastCount_u64",
        "ex.recv.unicastbytesu64": "Ex.Recv.UnicastBytes_u64",
        "ex.recv.unicastbytes": "Ex.Recv.UnicastBytes_u64",
        "exrecvunicastbytes": "Ex.Recv.UnicastBytes_u64",
        "ex.recv.unicastcountu64": "Ex.Recv.UnicastCount_u64",
        "ex.recv.unicastcount": "Ex.Recv.UnicastCount_u64",
        "exrecvunicastcount": "Ex.Recv.UnicastCount_u64",
        "ex.send.broadcastbytesu64": "Ex.Send.BroadcastBytes_u64",
        "ex.send.broadcastbytes": "Ex.Send.BroadcastBytes_u64",
        "exsendbroadcastbytes": "Ex.Send.BroadcastBytes_u64",
        "ex.send.broadcastcountu64": "Ex.Send.BroadcastCount_u64",
        "ex.send.broadcastcount": "Ex.Send.BroadcastCount_u64",
        "exsendbroadcastcount": "Ex.Send.BroadcastCount_u64",
        "ex.send.unicastbytesu64": "Ex.Send.UnicastBytes_u64",
        "ex.send.unicastbytes": "Ex.Send.UnicastBytes_u64",
        "exsendunicastbytes": "Ex.Send.UnicastBytes_u64",
        "ex.send.unicastcountu64": "Ex.Send.UnicastCount_u64",
        "ex.send.unicastcount": "Ex.Send.UnicastCount_u64",
        "exsendunicastcount": "Ex.Send.UnicastCount_u64",
    },
}

#: RPC parameters that carry a list of structures.
_STRUCT_FIELDS: Dict[str, str] = {
    "ACList": "ACList",
    "AccessList": "AccessList",
    "AccessListSingle": "AccessListSingle",
    "AdminOptionList": "AdminOptionList",
}

#: RPC parameters that carry a list of scalars, and their maximum length.
_SCALAR_LIST_FIELDS: Dict[str, int] = {
    "Ports_u32": 0,
    "PacketLogConfig_u32": 16,
}


# ========================================================================
# Method registry
# ========================================================================

#: Every RPC method name this module implements, in document order.
RPC_METHODS: Sequence[str] = (
    "Test",
    "GetServerInfo",
    "GetServerStatus",
    "CreateListener",
    "EnumListener",
    "DeleteListener",
    "EnableListener",
    "SetServerPassword",
    "SetFarmSetting",
    "GetFarmSetting",
    "GetFarmInfo",
    "EnumFarmMember",
    "GetFarmConnectionStatus",
    "SetServerCert",
    "GetServerCert",
    "GetServerCipher",
    "SetServerCipher",
    "CreateHub",
    "SetHub",
    "GetHub",
    "EnumHub",
    "DeleteHub",
    "GetHubRadius",
    "SetHubRadius",
    "EnumConnection",
    "DisconnectConnection",
    "GetConnectionInfo",
    "SetHubOnline",
    "GetHubStatus",
    "SetHubLog",
    "GetHubLog",
    "AddCa",
    "EnumCa",
    "GetCa",
    "DeleteCa",
    "CreateLink",
    "GetLink",
    "SetLink",
    "EnumLink",
    "SetLinkOnline",
    "SetLinkOffline",
    "DeleteLink",
    "RenameLink",
    "GetLinkStatus",
    "AddAccess",
    "DeleteAccess",
    "EnumAccess",
    "SetAccessList",
    "CreateUser",
    "SetUser",
    "GetUser",
    "DeleteUser",
    "EnumUser",
    "CreateGroup",
    "SetGroup",
    "GetGroup",
    "DeleteGroup",
    "EnumGroup",
    "EnumSession",
    "GetSessionStatus",
    "DeleteSession",
    "EnumMacTable",
    "DeleteMacTable",
    "EnumIpTable",
    "DeleteIpTable",
    "SetKeep",
    "GetKeep",
    "EnableSecureNAT",
    "DisableSecureNAT",
    "SetSecureNATOption",
    "GetSecureNATOption",
    "EnumNAT",
    "EnumDHCP",
    "GetSecureNATStatus",
    "EnumEthernet",
    "AddLocalBridge",
    "DeleteLocalBridge",
    "EnumLocalBridge",
    "GetBridgeSupport",
    "RebootServer",
    "GetCaps",
    "GetConfig",
    "SetConfig",
    "GetDefaultHubAdminOptions",
    "GetHubAdminOptions",
    "SetHubAdminOptions",
    "GetHubExtOptions",
    "SetHubExtOptions",
    "AddL3Switch",
    "DelL3Switch",
    "EnumL3Switch",
    "StartL3Switch",
    "StopL3Switch",
    "AddL3If",
    "DelL3If",
    "EnumL3If",
    "AddL3Table",
    "DelL3Table",
    "EnumL3Table",
    "EnumCrl",
    "AddCrl",
    "DelCrl",
    "GetCrl",
    "SetCrl",
    "SetAcList",
    "GetAcList",
    "EnumLogFile",
    "ReadLogFile",
    "SetSysLog",
    "GetSysLog",
    "SetHubMsg",
    "GetHubMsg",
    "Crash",
    "GetAdminMsg",
    "Flush",
    "SetIPsecServices",
    "GetIPsecServices",
    "AddEtherIpId",
    "GetEtherIpId",
    "DeleteEtherIpId",
    "EnumEtherIpId",
    "SetOpenVpnSstpConfig",
    "GetOpenVpnSstpConfig",
    "GetDDnsClientStatus",
    "ChangeDDnsClientHostname",
    "RegenerateServerCert",
    "MakeOpenVpnConfigFile",
    "SetSpecialListener",
    "GetSpecialListener",
    "GetAzureStatus",
    "SetAzureStatus",
    "GetDDnsInternetSettng",
    "SetDDnsInternetSettng",
    "GetVgsConfig",
    "SetVgsConfig",
)

#: RPC method name -> the Python method that implements it.
_RPC_TO_PYTHON: Dict[str, str] = {
    "Test": "test",
    "GetServerInfo": "get_server_info",
    "GetServerStatus": "get_server_status",
    "CreateListener": "create_listener",
    "EnumListener": "enum_listener",
    "DeleteListener": "delete_listener",
    "EnableListener": "enable_listener",
    "SetServerPassword": "set_server_password",
    "SetFarmSetting": "set_farm_setting",
    "GetFarmSetting": "get_farm_setting",
    "GetFarmInfo": "get_farm_info",
    "EnumFarmMember": "enum_farm_member",
    "GetFarmConnectionStatus": "get_farm_connection_status",
    "SetServerCert": "set_server_cert",
    "GetServerCert": "get_server_cert",
    "GetServerCipher": "get_server_cipher",
    "SetServerCipher": "set_server_cipher",
    "CreateHub": "create_hub",
    "SetHub": "set_hub",
    "GetHub": "get_hub",
    "EnumHub": "enum_hub",
    "DeleteHub": "delete_hub",
    "GetHubRadius": "get_hub_radius",
    "SetHubRadius": "set_hub_radius",
    "EnumConnection": "enum_connection",
    "DisconnectConnection": "disconnect_connection",
    "GetConnectionInfo": "get_connection_info",
    "SetHubOnline": "set_hub_online",
    "GetHubStatus": "get_hub_status",
    "SetHubLog": "set_hub_log",
    "GetHubLog": "get_hub_log",
    "AddCa": "add_ca",
    "EnumCa": "enum_ca",
    "GetCa": "get_ca",
    "DeleteCa": "delete_ca",
    "CreateLink": "create_link",
    "GetLink": "get_link",
    "SetLink": "set_link",
    "EnumLink": "enum_link",
    "SetLinkOnline": "set_link_online",
    "SetLinkOffline": "set_link_offline",
    "DeleteLink": "delete_link",
    "RenameLink": "rename_link",
    "GetLinkStatus": "get_link_status",
    "AddAccess": "add_access",
    "DeleteAccess": "delete_access",
    "EnumAccess": "enum_access",
    "SetAccessList": "set_access_list",
    "CreateUser": "create_user",
    "SetUser": "set_user",
    "GetUser": "get_user",
    "DeleteUser": "delete_user",
    "EnumUser": "enum_user",
    "CreateGroup": "create_group",
    "SetGroup": "set_group",
    "GetGroup": "get_group",
    "DeleteGroup": "delete_group",
    "EnumGroup": "enum_group",
    "EnumSession": "enum_session",
    "GetSessionStatus": "get_session_status",
    "DeleteSession": "delete_session",
    "EnumMacTable": "enum_mac_table",
    "DeleteMacTable": "delete_mac_table",
    "EnumIpTable": "enum_ip_table",
    "DeleteIpTable": "delete_ip_table",
    "SetKeep": "set_keep",
    "GetKeep": "get_keep",
    "EnableSecureNAT": "enable_securenat",
    "DisableSecureNAT": "disable_securenat",
    "SetSecureNATOption": "set_securenat_option",
    "GetSecureNATOption": "get_securenat_option",
    "EnumNAT": "enum_nat",
    "EnumDHCP": "enum_dhcp",
    "GetSecureNATStatus": "get_securenat_status",
    "EnumEthernet": "enum_ethernet",
    "AddLocalBridge": "add_local_bridge",
    "DeleteLocalBridge": "delete_local_bridge",
    "EnumLocalBridge": "enum_local_bridge",
    "GetBridgeSupport": "get_bridge_support",
    "RebootServer": "reboot_server",
    "GetCaps": "get_caps",
    "GetConfig": "get_config",
    "SetConfig": "set_config",
    "GetDefaultHubAdminOptions": "get_default_hub_admin_options",
    "GetHubAdminOptions": "get_hub_admin_options",
    "SetHubAdminOptions": "set_hub_admin_options",
    "GetHubExtOptions": "get_hub_ext_options",
    "SetHubExtOptions": "set_hub_ext_options",
    "AddL3Switch": "add_l3_switch",
    "DelL3Switch": "del_l3_switch",
    "EnumL3Switch": "enum_l3_switch",
    "StartL3Switch": "start_l3_switch",
    "StopL3Switch": "stop_l3_switch",
    "AddL3If": "add_l3_if",
    "DelL3If": "del_l3_if",
    "EnumL3If": "enum_l3_if",
    "AddL3Table": "add_l3_table",
    "DelL3Table": "del_l3_table",
    "EnumL3Table": "enum_l3_table",
    "EnumCrl": "enum_crl",
    "AddCrl": "add_crl",
    "DelCrl": "del_crl",
    "GetCrl": "get_crl",
    "SetCrl": "set_crl",
    "SetAcList": "set_ac_list",
    "GetAcList": "get_ac_list",
    "EnumLogFile": "enum_log_file",
    "ReadLogFile": "read_log_file",
    "SetSysLog": "set_sys_log",
    "GetSysLog": "get_sys_log",
    "SetHubMsg": "set_hub_msg",
    "GetHubMsg": "get_hub_msg",
    "Crash": "crash",
    "GetAdminMsg": "get_admin_msg",
    "Flush": "flush",
    "SetIPsecServices": "set_ipsec_services",
    "GetIPsecServices": "get_ipsec_services",
    "AddEtherIpId": "add_ether_ip_id",
    "GetEtherIpId": "get_ether_ip_id",
    "DeleteEtherIpId": "delete_ether_ip_id",
    "EnumEtherIpId": "enum_ether_ip_id",
    "SetOpenVpnSstpConfig": "set_openvpn_sstp_config",
    "GetOpenVpnSstpConfig": "get_openvpn_sstp_config",
    "GetDDnsClientStatus": "get_ddns_client_status",
    "ChangeDDnsClientHostname": "change_ddns_client_hostname",
    "RegenerateServerCert": "regenerate_server_cert",
    "MakeOpenVpnConfigFile": "make_openvpn_config_file",
    "SetSpecialListener": "set_special_listener",
    "GetSpecialListener": "get_special_listener",
    "GetAzureStatus": "get_azure_status",
    "SetAzureStatus": "set_azure_status",
    "GetDDnsInternetSettng": "get_ddns_internet_settng",
    "SetDDnsInternetSettng": "set_ddns_internet_settng",
    "GetVgsConfig": "get_vgs_config",
    "SetVgsConfig": "set_vgs_config",
}


# ========================================================================
# Structure builders
# ========================================================================

def access_rule(
    *,
    id: Optional[int] = None,
    note: Optional[str] = None,
    active: Optional[bool] = None,
    priority: Optional[int] = None,
    discard: Optional[bool] = None,
    is_ipv6: Optional[bool] = None,
    src_ip_address: Optional[str] = None,
    src_subnet_mask: Optional[str] = None,
    dest_ip_address: Optional[str] = None,
    dest_subnet_mask: Optional[str] = None,
    src_ip_address6: Optional[Union[bytes, bytearray, str]] = None,
    src_subnet_mask6: Optional[Union[bytes, bytearray, str]] = None,
    dest_ip_address6: Optional[Union[bytes, bytearray, str]] = None,
    dest_subnet_mask6: Optional[Union[bytes, bytearray, str]] = None,
    protocol: Optional[int] = None,
    src_port_start: Optional[int] = None,
    src_port_end: Optional[int] = None,
    dest_port_start: Optional[int] = None,
    dest_port_end: Optional[int] = None,
    src_username: Optional[str] = None,
    dest_username: Optional[str] = None,
    check_src_mac: Optional[bool] = None,
    src_mac_address: Optional[Union[bytes, bytearray, str]] = None,
    src_mac_mask: Optional[Union[bytes, bytearray, str]] = None,
    check_dst_mac: Optional[bool] = None,
    dst_mac_address: Optional[Union[bytes, bytearray, str]] = None,
    dst_mac_mask: Optional[Union[bytes, bytearray, str]] = None,
    check_tcp_state: Optional[bool] = None,
    established: Optional[bool] = None,
    delay: Optional[int] = None,
    jitter: Optional[int] = None,
    loss: Optional[int] = None,
    redirect_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one access list rule.

    Every argument left at ``None`` is omitted, and the VPN Server then applies its own default
    for that field.

    Args:
        id: ID (``Id_u32``, number (uint32))
        note: Specify a description (note) for this rule (``Note_utf``, string (UTF8))
        active: Enabled flag (true: enabled, false: disabled) (``Active_bool``, boolean)
        priority: Specify an integer of 1 or higher to indicate the priority of the rule. Higher
            priority is given to rules with the lower priority values. (``Priority_u32``, number
            (uint32))
        discard: The flag if the rule is DISCARD operation or PASS operation. When a packet
            matches this rule condition, this operation is decided. When the operation of the rule
            is PASS, the packet is allowed to pass, otherwise the packet will be discarded.
            (``Discard_bool``, boolean)
        is_ipv6: The flag if the rule is for IPv6. Specify false for IPv4, or specify true for
            IPv6. (``IsIPv6_bool``, boolean)
        src_ip_address: Valid only if the rule is IPv4 mode (IsIPv6_bool == false). Specify a
            source IPv4 address as a rule condition. You must also specify the SrcSubnetMask_ip
            field. (``SrcIpAddress_ip``, string (IP address))
        src_subnet_mask: Valid only if the rule is IPv4 mode (IsIPv6_bool == false). Specify a
            source IPv4 subnet mask as a rule condition. "0.0.0.0" means all hosts.
            "255.255.255.255" means one single host. (``SrcSubnetMask_ip``, string (IP address))
        dest_ip_address: Valid only if the rule is IPv4 mode (IsIPv6_bool == false). Specify a
            destination IPv4 address as a rule condition. You must also specify the
            DestSubnetMask_ip field. (``DestIpAddress_ip``, string (IP address))
        dest_subnet_mask: Valid only if the rule is IPv4 mode (IsIPv6_bool == false). Specify a
            destination IPv4 subnet mask as a rule condition. "0.0.0.0" means all hosts.
            "255.255.255.255" means one single host. (``DestSubnetMask_ip``, string (IP address))
        src_ip_address6: Valid only if the rule is IPv6 mode (IsIPv6_bool == true). Specify a
            source IPv6 address as a rule condition. The field must be a byte array of 16 bytes (128
            bits) to contain the IPv6 address in binary form. You must also specify the
            SrcSubnetMask6_bin field. (``SrcIpAddress6_bin``, string (Base64 binary))
        src_subnet_mask6: Valid only if the rule is IPv6 mode (IsIPv6_bool == true). Specify a
            source IPv6 subnet mask as a rule condition. The field must be a byte array of 16 bytes
            (128 bits) to contain the IPv6 subnet mask in binary form. (``SrcSubnetMask6_bin``,
            string (Base64 binary))
        dest_ip_address6: Valid only if the rule is IPv6 mode (IsIPv6_bool == true). Specify a
            destination IPv6 address as a rule condition. The field must be a byte array of 16 bytes
            (128 bits) to contain the IPv6 address in binary form. You must also specify the
            DestSubnetMask6_bin field. (``DestIpAddress6_bin``, string (Base64 binary))
        dest_subnet_mask6: Valid only if the rule is IPv6 mode (IsIPv6_bool == true). Specify a
            destination IPv6 subnet mask as a rule condition. The field must be a byte array of 16
            bytes (128 bits) to contain the IPv6 subnet mask in binary form.
            (``DestSubnetMask6_bin``, string (Base64 binary))
        protocol: The IP protocol number (``Protocol_u32``, number (enum))
        src_port_start: The Start Value of the Source Port Number Range. If the specified
            protocol is TCP/IP or UDP/IP, specify the source port number as the rule condition.
            Protocols other than this will be ignored. When this parameter is not specified, the
            rules will apply to all port numbers. (``SrcPortStart_u32``, number (uint32))
        src_port_end: The End Value of the Source Port Number Range. If the specified protocol
            is TCP/IP or UDP/IP, specify the source port number as the rule condition. Protocols
            other than this will be ignored. When this parameter is not specified, the rules will
            apply to all port numbers. (``SrcPortEnd_u32``, number (uint32))
        dest_port_start: The Start Value of the Destination Port Number Range. If the specified
            protocol is TCP/IP or UDP/IP, specify the destination port number as the rule condition.
            Protocols other than this will be ignored. When this parameter is not specified, the
            rules will apply to all port numbers. (``DestPortStart_u32``, number (uint32))
        dest_port_end: The End Value of the Destination Port Number Range. If the specified
            protocol is TCP/IP or UDP/IP, specify the destination port number as the rule condition.
            Protocols other than this will be ignored. When this parameter is not specified, the
            rules will apply to all port numbers. (``DestPortEnd_u32``, number (uint32))
        src_username: Source user name. You can apply this rule to only the packets sent by a
            user session of a user name that has been specified as a rule condition. In this case,
            specify the user name. (``SrcUsername_str``, string (ASCII))
        dest_username: Destination user name. You can apply this rule to only the packets
            received by a user session of a user name that has been specified as a rule condition.
            In this case, specify the user name. (``DestUsername_str``, string (ASCII))
        check_src_mac: Specify true if you want to check the source MAC address.
            (``CheckSrcMac_bool``, boolean)
        src_mac_address: Source MAC address (6 bytes), valid only if CheckSrcMac_bool == true.
            (``SrcMacAddress_bin``, string (Base64 binary))
        src_mac_mask: Source MAC address mask (6 bytes), valid only if CheckSrcMac_bool == true.
            (``SrcMacMask_bin``, string (Base64 binary))
        check_dst_mac: Specify true if you want to check the destination MAC address.
            (``CheckDstMac_bool``, boolean)
        dst_mac_address: Destination MAC address (6 bytes), valid only if CheckSrcMac_bool ==
            true. (``DstMacAddress_bin``, string (Base64 binary))
        dst_mac_mask: Destination MAC address mask (6 bytes), valid only if CheckSrcMac_bool ==
            true. (``DstMacMask_bin``, string (Base64 binary))
        check_tcp_state: Specify true if you want to check the state of the TCP connection.
            (``CheckTcpState_bool``, boolean)
        established: Valid only if CheckTcpState_bool == true. Set this field true to match only
            TCP-established packets. Set this field false to match only TCP-non established packets.
            (``Established_bool``, boolean)
        delay: Set this value to generate delays when packets is passing. Specify the delay
            period in milliseconds. Specify 0 means no delays to generate. The delays must be 10000
            milliseconds at most. (``Delay_u32``, number (uint32))
        jitter: Set this value to generate jitters when packets is passing. Specify the ratio of
            fluctuation of jitters within 0% to 100% range. Specify 0 means no jitters to generate.
            (``Jitter_u32``, number (uint32))
        loss: Set this value to generate packet losses when packets is passing. Specify the
            ratio of packet losses within 0% to 100% range. Specify 0 means no packet losses to
            generate. (``Loss_u32``, number (uint32))
        redirect_url: The specified URL will be mandatory replied to the client as a response
            for TCP connecting request packets which matches the conditions of this access list
            entry via this Virtual Hub. To use this setting, you can enforce the web browser of the
            VPN Client computer to show the specified web site when that web browser tries to access
            the specific IP address. (``RedirectUrl_str``, string (ASCII))

    Returns:
        A mapping suitable for the corresponding RPC parameter.
    """
    values: Dict[str, Any] = {
        "Id_u32": id,
        "Note_utf": note,
        "Active_bool": active,
        "Priority_u32": priority,
        "Discard_bool": discard,
        "IsIPv6_bool": is_ipv6,
        "SrcIpAddress_ip": src_ip_address,
        "SrcSubnetMask_ip": src_subnet_mask,
        "DestIpAddress_ip": dest_ip_address,
        "DestSubnetMask_ip": dest_subnet_mask,
        "SrcIpAddress6_bin": src_ip_address6,
        "SrcSubnetMask6_bin": src_subnet_mask6,
        "DestIpAddress6_bin": dest_ip_address6,
        "DestSubnetMask6_bin": dest_subnet_mask6,
        "Protocol_u32": protocol,
        "SrcPortStart_u32": src_port_start,
        "SrcPortEnd_u32": src_port_end,
        "DestPortStart_u32": dest_port_start,
        "DestPortEnd_u32": dest_port_end,
        "SrcUsername_str": src_username,
        "DestUsername_str": dest_username,
        "CheckSrcMac_bool": check_src_mac,
        "SrcMacAddress_bin": src_mac_address,
        "SrcMacMask_bin": src_mac_mask,
        "CheckDstMac_bool": check_dst_mac,
        "DstMacAddress_bin": dst_mac_address,
        "DstMacMask_bin": dst_mac_mask,
        "CheckTcpState_bool": check_tcp_state,
        "Established_bool": established,
        "Delay_u32": delay,
        "Jitter_u32": jitter,
        "Loss_u32": loss,
        "RedirectUrl_str": redirect_url,
    }
    return {k: v for k, v in values.items() if v is not None}


def admin_option(
    *,
    name: Optional[str] = None,
    value: Optional[int] = None,
    descrption: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one administration option.

    Every argument left at ``None`` is omitted, and the VPN Server then applies its own default
    for that field.

    Args:
        name: Name (``Name_str``, string (ASCII))
        value: Data (``Value_u32``, number (uint32))
        descrption: Descrption (``Descrption_utf``, string (UTF8))

    Returns:
        A mapping suitable for the corresponding RPC parameter.
    """
    values: Dict[str, Any] = {
        "Name_str": name,
        "Value_u32": value,
        "Descrption_utf": descrption,
    }
    return {k: v for k, v in values.items() if v is not None}


def ac_rule(
    *,
    id: Optional[int] = None,
    priority: Optional[int] = None,
    deny: Optional[bool] = None,
    masked: Optional[bool] = None,
    ip_address: Optional[str] = None,
    subnet_mask: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one source IP address limit rule.

    Every argument left at ``None`` is omitted, and the VPN Server then applies its own default
    for that field.

    Args:
        id: ID (``Id_u32``, number (uint32))
        priority: Priority (``Priority_u32``, number (uint32))
        deny: Deny access (``Deny_bool``, boolean)
        masked: Set true if you want to specify the SubnetMask_ip item. (``Masked_bool``,
            boolean)
        ip_address: IP address (``IpAddress_ip``, string (IP address))
        subnet_mask: Subnet mask, valid only if Masked_bool == true (``SubnetMask_ip``, string
            (IP address))

    Returns:
        A mapping suitable for the corresponding RPC parameter.
    """
    values: Dict[str, Any] = {
        "Id_u32": id,
        "Priority_u32": priority,
        "Deny_bool": deny,
        "Masked_bool": masked,
        "IpAddress_ip": ip_address,
        "SubnetMask_ip": subnet_mask,
    }
    return {k: v for k, v in values.items() if v is not None}


# ==========================================================================
# Argument packing
# ==========================================================================

def _normalise_struct(entry: Mapping[str, Any], struct: str, method: str, field: str,
                      strict: bool) -> Dict[str, Any]:
    """Validate and encode one element of a structure list.

    Keys may be given in wire form (``SrcIpAddress_ip``), without the type
    suffix (``SrcIpAddress``) or in snake_case (``src_ip_address``).
    """
    spec = _STRUCT_DEFS[struct]
    aliases = _STRUCT_ALIASES[struct]
    if not isinstance(entry, Mapping):
        raise _fail("each %s element must be a mapping, got %s"
                    % (struct, type(entry).__name__), method, field, entry)
    out: Dict[str, Any] = {}
    for key, value in entry.items():
        wire = key if key in spec else aliases.get(str(key).lower().replace("_", ""))
        if wire is None:
            if strict:
                raise _fail(
                    "unknown %s field %r (valid: %s)" % (struct, key, ", ".join(spec)),
                    method, field, entry,
                )
            continue
        if value is None:
            continue
        out[wire] = _ENCODERS[spec[wire]](value, method, "%s[].%s" % (field, wire))
    return out


def _pack(method: str, values: Mapping[str, Any], strict: bool = True) -> Dict[str, Any]:
    """Encode a method's keyword arguments into JSON-RPC ``params``.

    ``None`` values are dropped, every other value is validated and converted
    to its wire representation.
    """
    enums = _ENUM_FIELDS.get(method, {})
    params: Dict[str, Any] = {}
    for field, value in values.items():
        if value is None:
            continue
        struct = _STRUCT_FIELDS.get(field)
        if struct is not None:
            items = [value] if isinstance(value, Mapping) else value
            if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
                raise _fail("expected a list of %s mappings" % struct, method, field, value)
            params[field] = [_normalise_struct(i, struct, method, field, strict) for i in items]
            continue
        if field in _SCALAR_LIST_FIELDS:
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                raise _fail("expected a list of integers", method, field, value)
            limit = _SCALAR_LIST_FIELDS[field]
            if limit and len(value) > limit:
                raise _fail("at most %d elements are accepted, got %d" % (limit, len(value)),
                            method, field, value)
            items = [_enc_uint(i, 32, method, field) for i in value]
            element_enum = enums.get(field)
            if element_enum is not None and strict:
                valid = {int(m) for m in element_enum}
                for item in items:
                    if item not in valid:
                        raise _fail(
                            "%d is not a valid %s (valid: %s)"
                            % (item, element_enum.__name__,
                               ", ".join("%d=%s" % (int(m), m.name) for m in element_enum)),
                            method, field, value,
                        )
            params[field] = items
            continue
        enum_cls = enums.get(field)
        if enum_cls is not None and strict:
            probe = int(value) if isinstance(value, (int, IntEnum)) and not isinstance(value, bool) else value
            if not isinstance(probe, int):
                raise _fail("expected an integer or %s member" % enum_cls.__name__,
                            method, field, value)
            if probe not in {int(m) for m in enum_cls}:
                raise _fail(
                    "%d is not a valid %s (valid: %s)"
                    % (probe, enum_cls.__name__,
                       ", ".join("%d=%s" % (int(m), m.name) for m in enum_cls)),
                    method, field, value,
                )
            params[field] = probe
            continue
        kind = _field_kind(field)
        params[field] = _ENCODERS[kind](value, method, field)
    return params


def _field_kind(field: str) -> str:
    for suffix in ("_u32", "_u64", "_str", "_utf", "_bin", "_ip", "_bool", "_dt"):
        if field.endswith(suffix):
            return suffix[1:]
    return "str"


# ==========================================================================
# Client
# ==========================================================================

class SoftEtherClient:
    """Client for the SoftEther VPN Server JSON-RPC API.

    Args:
        host: Hostname or IP address of the VPN Server.
        port: Administration port.  A stock server listens on 443, 992 and
            5555; ``443`` is often taken by a web server, hence the 5555
            default.
        password: Administrator password.  With ``hub`` set this is the
            Virtual Hub administrator password, otherwise the whole-server
            administrator password.
        hub: Virtual Hub name to log in to in *Virtual Hub Admin Mode*.
            ``None`` or ``""`` logs in in *Entire VPN Server Admin Mode*.
        verify: Verify the server's TLS certificate.  SoftEther ships a
            self-signed certificate, so this defaults to ``False`` and an
            :class:`InsecureTLSWarning` is emitted once per client.  Set it to
            ``True`` (optionally with ``ca_file``) or pin ``fingerprint``.
        ca_file: PEM bundle used when ``verify=True``.
        ca_path: Directory of PEM certificates used when ``verify=True``.
        fingerprint: Expected SHA-256 fingerprint of the server certificate,
            hex, with or without separators.  Checked on every connection; makes
            a self-signed deployment safe without a CA.
        timeout: Socket timeout in seconds applied to connect, send and receive.
        retries: Number of automatic retries for transport-level failures.
            A reused keep-alive connection that dies before a single byte of
            the response arrived is always retried once, since the request
            provably never reached the server.  Further retries only happen for
            side-effect-free methods (``Get*``, ``Enum*``, ``Test``) unless
            ``retry_unsafe=True``.
        retry_backoff: Base for the exponential backoff between retries.
        retry_unsafe: Allow retrying methods that mutate server state.  Off by
            default: a retried ``CreateUser`` can raise "already exists" for a
            call that in fact succeeded.
        auth: ``"header"`` (default) uses the documented
            ``X-VPNADMIN-HUBNAME`` / ``X-VPNADMIN-PASSWORD`` headers;
            ``"basic"`` uses HTTP Basic authentication instead.
        strict: Validate enumeration values and reject unknown structure keys
            client-side.  Turn it off to talk to a server newer than this
            document.
        user_agent: Value of the ``User-Agent`` header.
        path: Entry point path.  Only change it if the API is reverse-proxied.
        suppress_insecure_warning: Do not emit :class:`InsecureTLSWarning`.

    The client is thread-safe: calls are serialised over a single keep-alive
    connection by an internal lock.  Use one client per thread if you need
    concurrency.
    """

    #: Methods that have no side effects and may be retried freely.
    SAFE_PREFIXES = ("Get", "Enum", "Test", "Read", "Make")

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        password: str = "",
        hub: Optional[str] = None,
        *,
        verify: bool = False,
        ca_file: Optional[str] = None,
        ca_path: Optional[str] = None,
        fingerprint: Optional[str] = None,
        timeout: float = 30.0,
        retries: int = 2,
        retry_backoff: float = 0.5,
        retry_unsafe: bool = False,
        auth: str = "header",
        strict: bool = True,
        user_agent: str = "softether-python/%s" % __version__,
        path: str = "/api/",
        suppress_insecure_warning: bool = False,
    ):
        if not host:
            raise ValueError("host is required")
        if auth not in ("header", "basic"):
            raise ValueError("auth must be 'header' or 'basic'")
        self.host = str(host)
        self.port = int(port)
        self.password = password or ""
        self.hub = hub or ""
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.retry_backoff = float(retry_backoff)
        self.retry_unsafe = bool(retry_unsafe)
        self.strict = bool(strict)
        self.auth = auth
        self.path = path if path.startswith("/") else "/" + path
        self.user_agent = user_agent
        self.fingerprint = fingerprint.replace(":", "").replace(" ", "").lower() if fingerprint else None
        self.verify = bool(verify)

        self._ca_file = ca_file
        self._ca_path = ca_path
        self._lock = threading.RLock()
        self._conn: Optional[http.client.HTTPSConnection] = None
        self._conn_used = False
        self._id = 0
        self._ssl_context = self._build_ssl_context()

        if not self.verify and not self.fingerprint and not suppress_insecure_warning:
            warnings.warn(
                "TLS certificate verification is disabled for %s:%s. This is the default "
                "because SoftEther ships a self-signed certificate; pass verify=True with a "
                "CA, or pin fingerprint=..., to authenticate the server."
                % (self.host, self.port),
                InsecureTLSWarning,
                stacklevel=2,
            )

    # -- construction helpers --------------------------------------------
    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> "SoftEtherClient":
        """Build a client from ``https://[password@]host:port/`` style URL.

        The user info part, when present, is used as the password (SoftEther
        has no administrator user name); ``user:pass`` is accepted too and the
        user part is then taken as the Virtual Hub name.
        """
        from urllib.parse import urlparse

        parsed = urlparse(url if "//" in url else "https://" + url)
        if parsed.hostname is None:
            raise ValueError("could not parse a host out of %r" % url)
        if parsed.password is not None:
            kwargs.setdefault("password", parsed.password)
            if parsed.username:
                kwargs.setdefault("hub", parsed.username)
        elif parsed.username:
            kwargs.setdefault("password", parsed.username)
        if parsed.path and parsed.path not in ("/", ""):
            kwargs.setdefault("path", parsed.path)
        return cls(parsed.hostname, parsed.port or DEFAULT_PORT, **kwargs)

    def _build_ssl_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context(cafile=self._ca_file, capath=self._ca_path)
        if not self.verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    # -- connection management -------------------------------------------
    def _connect(self) -> http.client.HTTPSConnection:
        conn = http.client.HTTPSConnection(
            self.host, self.port, timeout=self.timeout, context=self._ssl_context
        )
        try:
            conn.connect()
        except ssl.SSLCertVerificationError as exc:
            conn.close()
            raise TLSError(
                "certificate verification failed for %s:%s (%s). SoftEther uses a self-signed "
                "certificate by default: pass verify=False, or pin fingerprint=..., or supply "
                "ca_file=..." % (self.host, self.port, exc)
            ) from exc
        except ssl.SSLError as exc:
            conn.close()
            raise TLSError("TLS handshake with %s:%s failed: %s" % (self.host, self.port, exc)) from exc
        except socket.timeout as exc:
            conn.close()
            raise TimeoutError(
                "connecting to %s:%s timed out after %ss" % (self.host, self.port, self.timeout)
            ) from exc
        except OSError as exc:
            conn.close()
            raise ConnectionFailedError(
                "cannot connect to %s:%s: %s" % (self.host, self.port, exc)
            ) from exc

        if self.fingerprint:
            self._check_fingerprint(conn)
        return conn

    def _check_fingerprint(self, conn: http.client.HTTPSConnection) -> None:
        import hashlib

        sock = conn.sock
        der = sock.getpeercert(True) if sock is not None else None
        if not der:
            conn.close()
            raise TLSError("server presented no certificate; cannot check the fingerprint")
        actual = hashlib.sha256(der).hexdigest()
        if actual != self.fingerprint:
            conn.close()
            raise CertificateFingerprintError(self.fingerprint, actual)

    def _headers(self, body_len: int) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(body_len),
            "Accept": "application/json",
            "User-Agent": self.user_agent,
            "Connection": "keep-alive",
        }
        if self.auth == "basic":
            token = base64.b64encode(("%s:%s" % (self.hub, self.password)).encode("utf-8"))
            headers["Authorization"] = "Basic " + token.decode("ascii")
        else:
            headers["X-VPNADMIN-HUBNAME"] = self.hub
            headers["X-VPNADMIN-PASSWORD"] = self.password
        return headers

    def close(self) -> None:
        """Close the underlying keep-alive connection.  Safe to call twice."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # pragma: no cover - best effort
                    pass
                self._conn = None
                self._conn_used = False

    def __enter__(self) -> "SoftEtherClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "<SoftEtherClient %s:%s hub=%r auth=%s verify=%s>" % (
            self.host, self.port, self.hub or "(server admin)", self.auth, self.verify,
        )

    # -- the wire ---------------------------------------------------------
    def call(self, method: str, params: Optional[Mapping[str, Any]] = None) -> RpcResult:
        """Invoke *method* with an already-encoded ``params`` mapping.

        The escape hatch for RPCs this build does not wrap.  Values are sent
        as given, so encode ``_bin`` fields as base64 and ``_dt`` fields as
        ``YYYY-MM-DDTHH:MM:SS.mmm`` yourself.

        Raises:
            ValidationError: *method* is empty or *params* is not a mapping.
            TransportError: the request could not be exchanged.
            AuthenticationError: the password was rejected.
            RpcError: the server returned a JSON-RPC error.
        """
        if not method or not isinstance(method, str):
            raise ValidationError("method must be a non-empty string", field="method", value=method)
        if params is None:
            params = {}
        if not isinstance(params, Mapping):
            raise ValidationError("params must be a mapping", method=method,
                                  field="params", value=params)
        return self._transport(method, dict(params))

    def _invoke(self, method: str, values: Mapping[str, Any]) -> RpcResult:
        """Validate, encode and send one call.  Used by the generated methods."""
        return self._transport(method, _pack(method, values, self.strict))

    def _is_safe(self, method: str) -> bool:
        return method.startswith(self.SAFE_PREFIXES)

    def _transport(self, method: str, params: Dict[str, Any]) -> RpcResult:
        with self._lock:
            self._id += 1
            request_id = "%d" % self._id
            payload = json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")

            attempt = 0
            while True:
                reused = self._conn is not None and self._conn_used
                try:
                    status, reason, body = self._exchange(payload)
                    break
                except TransportError as exc:
                    self.close()
                    # A connection that was already used and died before any
                    # response byte arrived provably never delivered the
                    # request: retrying is always safe.
                    stale = reused and isinstance(exc, (ConnectionFailedError, TimeoutError))
                    allowed = self.retry_unsafe or self._is_safe(method) or stale
                    if attempt >= self.retries or not allowed:
                        raise
                    delay = self.retry_backoff * (2 ** attempt)
                    LOG.debug("softether: %s failed (%s), retry %d/%d in %.2fs",
                              method, exc, attempt + 1, self.retries, delay)
                    time.sleep(delay)
                    attempt += 1

            return self._parse(method, request_id, status, reason, body)

    def _exchange(self, payload: bytes):
        if self._conn is None:
            self._conn = self._connect()
            self._conn_used = False
        conn = self._conn
        try:
            conn.request("POST", self.path, body=payload, headers=self._headers(len(payload)))
            response = conn.getresponse()
            body = response.read()
            self._conn_used = True
            if response.will_close:
                self.close()
            return response.status, response.reason, body
        except socket.timeout as exc:
            raise TimeoutError("%s timed out after %ss" % (self.path, self.timeout)) from exc
        except (http.client.HTTPException, ssl.SSLEOFError, ConnectionError, OSError) as exc:
            raise ConnectionFailedError("request to %s:%s failed: %s" % (self.host, self.port, exc)) from exc

    def _parse(self, method: str, request_id: str, status: int, reason: str,
               body: bytes) -> RpcResult:
        text = body.decode("utf-8", "replace")
        if status in (401, 403):
            raise AuthenticationError(
                "the VPN Server rejected the credentials for %s (HTTP %d). Check the %s "
                "password%s." % (method, status,
                                 "Virtual Hub administrator" if self.hub else "server administrator",
                                 " for hub %r" % self.hub if self.hub else "")
            )
        if status == 404:
            raise ApiDisabledError(
                "%s://%s:%s%s returned HTTP 404. The JSON-RPC API is not enabled here: it needs "
                "SoftEther VPN Server from June 2019 or later with DisableJsonRpcWebApi unset."
                % ("https", self.host, self.port, self.path)
            )
        if status != 200:
            raise HTTPError(status, reason, text)

        try:
            data = json.loads(text) if text.strip() else None
        except ValueError as exc:
            raise ProtocolError(
                "%s: the server answered with %d bytes that are not JSON (%s)" % (method, len(body), exc),
                text,
            ) from exc
        if not isinstance(data, dict):
            raise ProtocolError("%s: expected a JSON object, got %s"
                                % (method, type(data).__name__), text)

        if "error" in data and data["error"] is not None:
            error = data["error"]
            if isinstance(error, Mapping):
                raise _rpc_error(error.get("code", -1), error.get("message", ""),
                                 method, error.get("data"))
            raise _rpc_error(-1, str(error), method)

        if "result" not in data:
            raise ProtocolError("%s: response carries neither 'result' nor 'error'" % method, text)

        answer_id = data.get("id")
        if answer_id is not None and str(answer_id) != request_id:
            raise ProtocolError(
                "%s: response id %r does not match request id %r" % (method, answer_id, request_id),
                text,
            )
        result = data["result"]
        if result is None:
            return RpcResult({})
        if not isinstance(result, Mapping):
            raise ProtocolError("%s: 'result' is %s, expected an object"
                                % (method, type(result).__name__), text)
        return RpcResult(result)

    # -- convenience ------------------------------------------------------
    def ping(self) -> bool:
        """Round-trip the ``Test`` RPC.  Returns ``True`` on success.

        Raises the same errors as any other call; a ``False`` return means the
        server answered but echoed the wrong value, which indicates a broken
        proxy in between.
        """
        probe = 0x5EA7
        return int(self.test(probe).get("IntValue_u32", -1)) == probe

    def __getattr__(self, item: str) -> Any:
        """Allow calling methods by their exact RPC name, e.g. ``client.CreateUser``."""
        target = _RPC_TO_PYTHON.get(item)
        if target is not None:
            return getattr(self, target)
        raise AttributeError("%r object has no attribute %r" % (type(self).__name__, item))

    # ======================================================================
    # Generated RPC methods
    # ======================================================================

    def test(
        self,
        int_value: int,
    ) -> RpcResult:
        """Test -- Test RPC function.

        Test RPC function. Input any integer value to the IntValue_u32 field. Then the server
        will convert the integer to the string, and return the string in the StrValue_str field.

        Args:
            int_value: A 32-bit integer field (``IntValue_u32``, number (uint32))

        Returns:
            RpcResult with the fields: ``IntValue_u32``, ``Int64Value_u64``, ``StrValue_str``,
            ``UniStrValue_utf``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("Test", {
            "IntValue_u32": int_value,
        })

    def get_server_info(
        self,
    ) -> RpcResult:
        """GetServerInfo -- Get server information.

        Get server information. This allows you to obtain the server information of the
        currently connected VPN Server or VPN Bridge. Included in the server information are the
        version number, build number and build information. You can also obtain information on
        the current server operation mode and the information of operating system that the
        server is operating on.

        Returns:
            RpcResult with the fields: ``ServerProductName_str``, ``ServerVersionString_str``,
            ``ServerBuildInfoString_str``, ``ServerVerInt_u32``, ``ServerBuildInt_u32``,
            ``ServerHostName_str``, ``ServerType_u32``, ``ServerBuildDate_dt``,
            ``ServerFamilyName_str``, ``OsType_u32``, ``OsServicePack_u32``,
            ``OsSystemName_str``, ``OsProductName_str``, ``OsVendorName_str``,
            ``OsVersion_str``, ``KernelName_str``, ``KernelVersion_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetServerInfo", {})

    def get_server_status(
        self,
    ) -> RpcResult:
        """GetServerStatus -- Get Current Server Status.

        Get Current Server Status. This allows you to obtain in real-time the current status of
        the currently connected VPN Server or VPN Bridge. You can get statistical information on
        data communication and the number of different kinds of objects that exist on the
        server. You can get information on how much memory is being used on the current computer
        by the OS.

        Returns:
            RpcResult with the fields: ``ServerType_u32``, ``NumTcpConnections_u32``,
            ``NumTcpConnectionsLocal_u32``, ``NumTcpConnectionsRemote_u32``,
            ``NumHubTotal_u32``, ``NumHubStandalone_u32``, ``NumHubStatic_u32``,
            ``NumHubDynamic_u32``, ``NumSessionsTotal_u32``, ``NumSessionsLocal_u32``,
            ``NumSessionsRemote_u32``, ``NumMacTables_u32``, ``NumIpTables_u32``,
            ``NumUsers_u32``, ``NumGroups_u32``, ``AssignedBridgeLicenses_u32``,
            ``AssignedClientLicenses_u32``, ``AssignedBridgeLicensesTotal_u32``,
            ``AssignedClientLicensesTotal_u32``, ``Recv.BroadcastBytes_u64``,
            ``Recv.BroadcastCount_u64``, ``Recv.UnicastBytes_u64``, ``Recv.UnicastCount_u64``,
            ``Send.BroadcastBytes_u64``, ``Send.BroadcastCount_u64``, ``Send.UnicastBytes_u64``,
            ``Send.UnicastCount_u64``, ``CurrentTime_dt``, ``CurrentTick_u64``,
            ``StartTime_dt``, ``TotalMemory_u64``, ``UsedMemory_u64``, ``FreeMemory_u64``,
            ``TotalPhys_u64``, ``UsedPhys_u64``, ``FreePhys_u64``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetServerStatus", {})

    def create_listener(
        self,
        port: int,
        *,
        enable: Optional[bool] = None,
    ) -> RpcResult:
        """CreateListener -- Create New TCP Listener.

        Create New TCP Listener. This allows you to create a new TCP Listener on the server. By
        creating the TCP Listener the server starts listening for a connection from clients at
        the specified TCP/IP port number. A TCP Listener that has been created can be deleted by
        the DeleteListener API. You can also get a list of TCP Listeners currently registered by
        using the EnumListener API. To execute this API, you must have VPN Server administrator
        privileges.

        .. note::
           Fields left at ``None`` are not sent, and the VPN Server applies its own default for
           each of them.

        Args:
            port: Port number (Range: 1 - 65535) (``Port_u32``, number (uint32))
            enable: Active state (``Enable_bool``, boolean)

        Returns:
            RpcResult with the fields: ``Port_u32``, ``Enable_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("CreateListener", {
            "Port_u32": port,
            "Enable_bool": enable,
        })

    def enum_listener(
        self,
    ) -> RpcResult:
        """EnumListener -- Get List of TCP Listeners.

        Get List of TCP Listeners. This allows you to get a list of TCP listeners registered on
        the current server. You can obtain information on whether the various TCP listeners have
        a status of operating or error. To call this API, you must have VPN Server administrator
        privileges.

        Returns:
            RpcResult with the fields: ``ListenerList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumListener", {})

    def delete_listener(
        self,
        port: int,
    ) -> RpcResult:
        """DeleteListener -- Delete TCP Listener.

        Delete TCP Listener. This allows you to delete a TCP Listener that's registered on the
        server. When the TCP Listener is in a state of operation, the listener will
        automatically be deleted when its operation stops. You can also get a list of TCP
        Listeners currently registered by using the EnumListener API. To call this API, you must
        have VPN Server administrator privileges.

        Args:
            port: Port number (Range: 1 - 65535) (``Port_u32``, number (uint32))

        Returns:
            RpcResult with the fields: ``Port_u32``, ``Enable_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("DeleteListener", {
            "Port_u32": port,
        })

    def enable_listener(
        self,
        port: int,
        *,
        enable: Optional[bool] = None,
    ) -> RpcResult:
        """EnableListener -- Enable / Disable TCP Listener.

        Enable / Disable TCP Listener. This starts or stops the operation of TCP Listeners
        registered on the current server. You can also get a list of TCP Listeners currently
        registered by using the EnumListener API. To call this API, you must have VPN Server
        administrator privileges.

        Args:
            port: Port number (Range: 1 - 65535) (``Port_u32``, number (uint32))
            enable: Active state (``Enable_bool``, boolean)

        Returns:
            RpcResult with the fields: ``Port_u32``, ``Enable_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnableListener", {
            "Port_u32": port,
            "Enable_bool": enable,
        })

    def set_server_password(
        self,
        plain_text_password: str,
    ) -> RpcResult:
        """SetServerPassword -- Set VPN Server Administrator Password.

        Set VPN Server Administrator Password. This sets the VPN Server administrator password.
        You can specify the password as a parameter. To call this API, you must have VPN Server
        administrator privileges.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            plain_text_password: The plaintext password (``PlainTextPassword_str``, string
                (ASCII))

        Returns:
            RpcResult with the fields: ``PlainTextPassword_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetServerPassword", {
            "PlainTextPassword_str": plain_text_password,
        })

    def set_farm_setting(
        self,
        *,
        server_type: Optional[Union[int, "ServerType"]] = None,
        num_port: Optional[int] = None,
        ports: Optional[Sequence[int]] = None,
        public_ip: Optional[str] = None,
        controller_name: Optional[str] = None,
        controller_port: Optional[int] = None,
        member_password_plaintext: Optional[str] = None,
        weight: Optional[int] = None,
        controller_only: Optional[bool] = None,
    ) -> RpcResult:
        """SetFarmSetting -- Set the VPN Server clustering configuration.

        Set the VPN Server clustering configuration. Use this to set the VPN Server type as
        Standalone Server, Cluster Controller Server or Cluster Member Server. Standalone server
        means a VPN Server that does not belong to any cluster in its current state. When VPN
        Server is installed, by default it will be in standalone server mode. Unless you have
        particular plans to configure a cluster, we recommend the VPN Server be operated in
        standalone mode. A cluster controller is the central computer of all member servers of a
        cluster in the case where a clustering environment is made up of multiple VPN Servers.
        Multiple cluster members can be added to the cluster as required. A cluster requires one
        computer to serve this role. The other cluster member servers that are configured in the
        same cluster begin operation as a cluster member by connecting to the cluster
        controller. To call this API, you must have VPN Server administrator privileges. Also,
        when this API is executed, VPN Server will automatically restart. This API cannot be
        called on VPN Bridge.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            server_type: Type of server (``ServerType_u32``, number (enum))
                Accepts an :class:`ServerType` member or its integer value: 0 = Stand-alone
                server; 1 = Farm controller server; 2 = Farm member server.
            num_port: Valid only for Cluster Member servers. Number of the Ports_u32 element.
                (``NumPort_u32``, number (uint32))
            ports: Valid only for Cluster Member servers. Specify the list of public port
                numbers on this server. The list must have at least one public port number set, and
                it is also possible to set multiple public port numbers. (``Ports_u32``, number[]
                (uint32))
            public_ip: Valid only for Cluster Member servers. Specify the public IP address of
                this server. If you wish to leave public IP address unspecified, specify the empty
                string. When a public IP address is not specified, the IP address of the network
                interface used when connecting to the cluster controller will be automatically used.
                (``PublicIp_ip``, string (IP address))
            controller_name: Valid only for Cluster Member servers. Specify the host name or IP
                address of the destination cluster controller. (``ControllerName_str``, string
                (ASCII))
            controller_port: Valid only for Cluster Member servers. Specify the TCP port number
                of the destination cluster controller. (``ControllerPort_u32``, number (uint32))
            member_password_plaintext: Valid only for Cluster Member servers. Specify the
                password required to connect to the destination controller. It needs to be the same
                as an administrator password on the destination controller.
                (``MemberPasswordPlaintext_str``, string (ASCII))
            weight: This sets a value for the performance standard ratio of this VPN Server.
                This is the standard value for when load balancing is performed in the cluster. For
                example, making only one machine 200 while the other members have a status of 100,
                will regulate that machine to receive twice as many connections as the other
                members. Specify 1 or higher for the value. If this parameter is left unspecified,
                100 will be used. (``Weight_u32``, number (uint32))
            controller_only: Valid only for Cluster Controller server. By specifying true, the
                VPN Server will operate only as a controller on the cluster and it will always
                distribute general VPN Client connections to members other than itself. This
                function is used in high-load environments. (``ControllerOnly_bool``, boolean)

        Returns:
            RpcResult with the fields: ``ServerType_u32``, ``NumPort_u32``, ``Ports_u32``,
            ``PublicIp_ip``, ``ControllerName_str``, ``ControllerPort_u32``,
            ``MemberPasswordPlaintext_str``, ``Weight_u32``, ``ControllerOnly_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetFarmSetting", {
            "ServerType_u32": server_type,
            "NumPort_u32": num_port,
            "Ports_u32": ports,
            "PublicIp_ip": public_ip,
            "ControllerName_str": controller_name,
            "ControllerPort_u32": controller_port,
            "MemberPasswordPlaintext_str": member_password_plaintext,
            "Weight_u32": weight,
            "ControllerOnly_bool": controller_only,
        })

    def get_farm_setting(
        self,
    ) -> RpcResult:
        """GetFarmSetting -- Get Clustering Configuration of Current VPN Server.

        Get Clustering Configuration of Current VPN Server. You can use this to acquire the
        clustering configuration of the current VPN Server. To call this API, you must have VPN
        Server administrator privileges.

        Returns:
            RpcResult with the fields: ``ServerType_u32``, ``NumPort_u32``, ``Ports_u32``,
            ``PublicIp_ip``, ``ControllerName_str``, ``ControllerPort_u32``,
            ``MemberPasswordPlaintext_str``, ``Weight_u32``, ``ControllerOnly_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetFarmSetting", {})

    def get_farm_info(
        self,
        id: int,
    ) -> RpcResult:
        """GetFarmInfo -- Get Cluster Member Information.

        Get Cluster Member Information. When the VPN Server is operating as a cluster
        controller, you can get information on cluster member servers on that cluster by
        specifying the IDs of the member servers. You can get the following information about
        the specified cluster member server: Server Type, Time Connection has been Established,
        IP Address, Host Name, Points, Public Port List, Number of Operating Virtual Hubs, First
        Virtual Hub, Number of Sessions and Number of TCP Connections. This API cannot be
        invoked on VPN Bridge.

        Args:
            id: ID (``Id_u32``, number (uint32))

        Returns:
            RpcResult with the fields: ``Id_u32``, ``Controller_bool``, ``ConnectedTime_dt``,
            ``Ip_ip``, ``Hostname_str``, ``Point_u32``, ``NumPort_u32``, ``Ports_u32``,
            ``ServerCert_bin``, ``NumFarmHub_u32``, ``HubsList``, ``NumSessions_u32``,
            ``NumTcpConnections_u32``, ``Weight_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetFarmInfo", {
            "Id_u32": id,
        })

    def enum_farm_member(
        self,
    ) -> RpcResult:
        """EnumFarmMember -- Get List of Cluster Members.

        Get List of Cluster Members. Use this API when the VPN Server is operating as a cluster
        controller to get a list of the cluster member servers on the same cluster, including
        the cluster controller itself. For each member, the following information is also
        listed: Type, Connection Start, Host Name, Points, Number of Session, Number of TCP
        Connections, Number of Operating Virtual Hubs, Using Client Connection License and Using
        Bridge Connection License. This API cannot be invoked on VPN Bridge.

        Returns:
            RpcResult with the fields: ``NumFarm_u32``, ``FarmMemberList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumFarmMember", {})

    def get_farm_connection_status(
        self,
    ) -> RpcResult:
        """GetFarmConnectionStatus -- Get Connection Status to Cluster Controller.

        Get Connection Status to Cluster Controller. Use this API when the VPN Server is
        operating as a cluster controller to get the status of connection to the cluster
        controller. You can get the following information: Controller IP Address, Port Number,
        Connection Status, Connection Start Time, First Connection Established Time, Current
        Connection Established Time, Number of Connection Attempts, Number of Successful
        Connections, Number of Failed Connections. This API cannot be invoked on VPN Bridge.

        Returns:
            RpcResult with the fields: ``Ip_ip``, ``Port_u32``, ``Online_bool``,
            ``LastError_u32``, ``StartedTime_dt``, ``FirstConnectedTime_dt``,
            ``CurrentConnectedTime_dt``, ``NumTry_u32``, ``NumConnected_u32``,
            ``NumFailed_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetFarmConnectionStatus", {})

    def set_server_cert(
        self,
        cert: Union[bytes, bytearray, str],
        key: Union[bytes, bytearray, str],
    ) -> RpcResult:
        """SetServerCert -- Set SSL Certificate and Private Key of VPN Server.

        Set SSL Certificate and Private Key of VPN Server. You can set the SSL certificate that
        the VPN Server provides to the connected client and the private key for that
        certificate. The certificate must be in X.509 format and the private key must be Base 64
        encoded format. To call this API, you must have VPN Server administrator privileges.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            cert: The body of the certificate (``Cert_bin``, string (Base64 binary))
            key: The body of the private key (``Key_bin``, string (Base64 binary))

        Returns:
            RpcResult with the fields: ``Cert_bin``, ``Key_bin``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetServerCert", {
            "Cert_bin": cert,
            "Key_bin": key,
        })

    def get_server_cert(
        self,
    ) -> RpcResult:
        """GetServerCert -- Get SSL Certificate and Private Key of VPN Server.

        Get SSL Certificate and Private Key of VPN Server. Use this to get the SSL certificate
        private key that the VPN Server provides to the connected client. To call this API, you
        must have VPN Server administrator privileges.

        Returns:
            RpcResult with the fields: ``Cert_bin``, ``Key_bin``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetServerCert", {})

    def get_server_cipher(
        self,
    ) -> RpcResult:
        """GetServerCipher -- Get the Encrypted Algorithm Used for VPN Communication.

        Get the Encrypted Algorithm Used for VPN Communication. Use this API to get the current
        setting of the algorithm used for the electronic signature and encrypted for SSL
        connection to be used for communication between the VPN Server and the connected client
        and the list of algorithms that can be used on the VPN Server.

        Returns:
            RpcResult with the fields: ``String_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetServerCipher", {})

    def set_server_cipher(
        self,
        string: str,
    ) -> RpcResult:
        """SetServerCipher -- Set the Encrypted Algorithm Used for VPN Communication.

        Set the Encrypted Algorithm Used for VPN Communication. Use this API to set the
        algorithm used for the electronic signature and encrypted for SSL connections to be used
        for communication between the VPN Server and the connected client. By specifying the
        algorithm name, the specified algorithm will be used later between the VPN Client and
        VPN Bridge connected to this server and the data will be encrypted. To call this API,
        you must have VPN Server administrator privileges.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            string: A string value (``String_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``String_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetServerCipher", {
            "String_str": string,
        })

    def create_hub(
        self,
        hub_name: str,
        admin_password_plain_text: str,
        *,
        online: Optional[bool] = None,
        max_session: Optional[int] = None,
        no_enum: Optional[bool] = None,
        hub_type: Optional[Union[int, "HubType"]] = None,
    ) -> RpcResult:
        """CreateHub -- Create New Virtual Hub.

        Create New Virtual Hub. Use this to create a new Virtual Hub on the VPN Server. The
        created Virtual Hub will begin operation immediately. When the VPN Server is operating
        on a cluster, this API is only valid for the cluster controller. Also, the new Virtual
        Hub will operate as a dynamic Virtual Hub. You can change it to a static Virtual Hub by
        using the SetHub API. To get a list of Virtual Hubs that are already on the VPN Server,
        use the EnumHub API. To call this API, you must have VPN Server administrator
        privileges. Also, this API does not operate on VPN Servers that are operating as a VPN
        Bridge or cluster member.

        .. note::
           Fields left at ``None`` are not sent, and the VPN Server applies its own default for
           each of them.

        Args:
            hub_name: Specify the name of the Virtual Hub to create / update. (``HubName_str``,
                string (ASCII))
            admin_password_plain_text: Specify an administrator password when the administrator
                password is going to be set for the Virtual Hub. On the update, leave it to empty
                string if you don't want to change the password. (``AdminPasswordPlainText_str``,
                string (ASCII))
            online: Online flag (``Online_bool``, boolean)
            max_session: Maximum number of VPN sessions (``MaxSession_u32``, number (uint32))
            no_enum: No Enum flag. By enabling this option, the VPN Client user will be unable
                to enumerate this Virtual Hub even if they send a Virtual Hub enumeration request to
                the VPN Server. (``NoEnum_bool``, boolean)
            hub_type: Type of the Virtual Hub (Valid only for Clustered VPN Servers)
                (``HubType_u32``, number (enum))
                Accepts an :class:`HubType` member or its integer value: 0 = Stand-alone HUB; 1
                = Static HUB; 2 = Dynamic HUB.

        Returns:
            RpcResult with the fields: ``HubName_str``, ``AdminPasswordPlainText_str``,
            ``Online_bool``, ``MaxSession_u32``, ``NoEnum_bool``, ``HubType_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("CreateHub", {
            "HubName_str": hub_name,
            "AdminPasswordPlainText_str": admin_password_plain_text,
            "Online_bool": online,
            "MaxSession_u32": max_session,
            "NoEnum_bool": no_enum,
            "HubType_u32": hub_type,
        })

    def set_hub(
        self,
        hub_name: str,
        *,
        admin_password_plain_text: Optional[str] = None,
        online: Optional[bool] = None,
        max_session: Optional[int] = None,
        no_enum: Optional[bool] = None,
        hub_type: Optional[Union[int, "HubType"]] = None,
    ) -> RpcResult:
        """SetHub -- Set the Virtual Hub configuration.

        Set the Virtual Hub configuration. You can call this API to change the configuration of
        the specified Virtual Hub. You can set the Virtual Hub online or offline. You can set
        the maximum number of sessions that can be concurrently connected to the Virtual Hub
        that is currently being managed. You can set the Virtual Hub administrator password. You
        can set other parameters for the Virtual Hub. Before call this API, you need to obtain
        the latest state of the Virtual Hub by using the GetHub API.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            hub_name: Specify the name of the Virtual Hub to create / update. (``HubName_str``,
                string (ASCII))
            admin_password_plain_text: Specify an administrator password when the administrator
                password is going to be set for the Virtual Hub. On the update, leave it to empty
                string if you don't want to change the password. (``AdminPasswordPlainText_str``,
                string (ASCII))
            online: Online flag (``Online_bool``, boolean)
            max_session: Maximum number of VPN sessions (``MaxSession_u32``, number (uint32))
            no_enum: No Enum flag. By enabling this option, the VPN Client user will be unable
                to enumerate this Virtual Hub even if they send a Virtual Hub enumeration request to
                the VPN Server. (``NoEnum_bool``, boolean)
            hub_type: Type of the Virtual Hub (Valid only for Clustered VPN Servers)
                (``HubType_u32``, number (enum))
                Accepts an :class:`HubType` member or its integer value: 0 = Stand-alone HUB; 1
                = Static HUB; 2 = Dynamic HUB.

        Returns:
            RpcResult with the fields: ``HubName_str``, ``AdminPasswordPlainText_str``,
            ``Online_bool``, ``MaxSession_u32``, ``NoEnum_bool``, ``HubType_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetHub", {
            "HubName_str": hub_name,
            "AdminPasswordPlainText_str": admin_password_plain_text,
            "Online_bool": online,
            "MaxSession_u32": max_session,
            "NoEnum_bool": no_enum,
            "HubType_u32": hub_type,
        })

    def get_hub(
        self,
        hub_name: str,
    ) -> RpcResult:
        """GetHub -- Get the Virtual Hub configuration.

        Get the Virtual Hub configuration. You can call this API to get the current
        configuration of the specified Virtual Hub. To change the configuration of the Virtual
        Hub, call the SetHub API.

        Args:
            hub_name: Specify the name of the Virtual Hub to create / update. (``HubName_str``,
                string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``AdminPasswordPlainText_str``,
            ``Online_bool``, ``MaxSession_u32``, ``NoEnum_bool``, ``HubType_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetHub", {
            "HubName_str": hub_name,
        })

    def enum_hub(
        self,
    ) -> RpcResult:
        """EnumHub -- Get List of Virtual Hubs.

        Get List of Virtual Hubs. Use this to get a list of existing Virtual Hubs on the VPN
        Server. For each Virtual Hub, you can get the following information: Virtual Hub Name,
        Status, Type, Number of Users, Number of Groups, Number of Sessions, Number of MAC
        Tables, Number of IP Tables, Number of Logins, Last Login, and Last Communication. Note
        that when connecting in Virtual Hub Admin Mode, if in the options of a Virtual Hub that
        you do not have administrator privileges for, the option Don't Enumerate this Virtual
        Hub for Anonymous Users is enabled then that Virtual Hub will not be enumerated. If you
        are connected in Server Admin Mode, then the list of all Virtual Hubs will be displayed.
        When connecting to and managing a non-cluster-controller cluster member of a clustering
        environment, only the Virtual Hub currently being hosted by that VPN Server will be
        displayed. When connecting to a cluster controller for administration purposes, all the
        Virtual Hubs will be displayed.

        Returns:
            RpcResult with the fields: ``NumHub_u32``, ``HubList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumHub", {})

    def delete_hub(
        self,
        hub_name: str,
    ) -> RpcResult:
        """DeleteHub -- Delete Virtual Hub.

        Delete Virtual Hub. Use this to delete an existing Virtual Hub on the VPN Server. If you
        delete the Virtual Hub, all sessions that are currently connected to the Virtual Hub
        will be disconnected and new sessions will be unable to connect to the Virtual Hub.
        Also, this will also delete all the Hub settings, user objects, group objects,
        certificates and Cascade Connections. Once you delete the Virtual Hub, it cannot be
        recovered. To call this API, you must have VPN Server administrator privileges. Also,
        this API does not operate on VPN Servers that are operating as a VPN Bridge or cluster
        member.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("DeleteHub", {
            "HubName_str": hub_name,
        })

    def get_hub_radius(
        self,
        hub_name: str,
    ) -> RpcResult:
        """GetHubRadius -- Get Setting of RADIUS Server Used for User Authentication.

        Get Setting of RADIUS Server Used for User Authentication. Use this to get the current
        settings for the RADIUS server used when a user connects to the currently managed
        Virtual Hub using RADIUS Server Authentication Mode. This API cannot be invoked on VPN
        Bridge. You cannot execute this API for Virtual Hubs of VPN Servers operating as a
        cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``RadiusServerName_str``,
            ``RadiusPort_u32``, ``RadiusSecret_str``, ``RadiusRetryInterval_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetHubRadius", {
            "HubName_str": hub_name,
        })

    def set_hub_radius(
        self,
        hub_name: str,
        radius_server_name: str,
        radius_port: int,
        radius_secret: str,
        *,
        radius_retry_interval: Optional[int] = None,
    ) -> RpcResult:
        """SetHubRadius -- Set RADIUS Server to use for User Authentication.

        Set RADIUS Server to use for User Authentication. To accept users to the currently
        managed Virtual Hub in RADIUS server authentication mode, you can specify an external
        RADIUS server that confirms the user name and password. (You can specify multiple
        hostname by splitting with comma or semicolon.) The RADIUS server must be set to receive
        requests from IP addresses of this VPN Server. Also, authentication by Password
        Authentication Protocol (PAP) must be enabled. This API cannot be invoked on VPN Bridge.
        You cannot execute this API for Virtual Hubs of VPN Servers operating as a cluster.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            radius_server_name: RADIUS server name (``RadiusServerName_str``, string (ASCII))
            radius_port: RADIUS port number (``RadiusPort_u32``, number (uint32))
            radius_secret: Secret key (``RadiusSecret_str``, string (ASCII))
            radius_retry_interval: Radius retry interval (``RadiusRetryInterval_u32``, number
                (uint32))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``RadiusServerName_str``,
            ``RadiusPort_u32``, ``RadiusSecret_str``, ``RadiusRetryInterval_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetHubRadius", {
            "HubName_str": hub_name,
            "RadiusServerName_str": radius_server_name,
            "RadiusPort_u32": radius_port,
            "RadiusSecret_str": radius_secret,
            "RadiusRetryInterval_u32": radius_retry_interval,
        })

    def enum_connection(
        self,
    ) -> RpcResult:
        """EnumConnection -- Get List of TCP Connections Connecting to the VPN Server.

        Get List of TCP Connections Connecting to the VPN Server. Use this to get a list of
        TCP/IP connections that are currently connecting to the VPN Server. It does not display
        the TCP connections that have been established as VPN sessions. To get the list of
        TCP/IP connections that have been established as VPN sessions, you can use the
        EnumSession API. You can get the following: Connection Name, Connection Source,
        Connection Start and Type. To call this API, you must have VPN Server administrator
        privileges.

        Returns:
            RpcResult with the fields: ``NumConnection_u32``, ``ConnectionList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumConnection", {})

    def disconnect_connection(
        self,
        name: str,
    ) -> RpcResult:
        """DisconnectConnection -- Disconnect TCP Connections Connecting to the VPN Server.

        Disconnect TCP Connections Connecting to the VPN Server. Use this to forcefully
        disconnect specific TCP/IP connections that are connecting to the VPN Server. To call
        this API, you must have VPN Server administrator privileges.

        Args:
            name: Connection name (``Name_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``Name_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("DisconnectConnection", {
            "Name_str": name,
        })

    def get_connection_info(
        self,
        name: str,
    ) -> RpcResult:
        """GetConnectionInfo -- Get Information of TCP Connections Connecting to the VPN Server.

        Get Information of TCP Connections Connecting to the VPN Server. Use this to get
        detailed information of a specific TCP/IP connection that is connecting to the VPN
        Server. You can get the following information: Connection Name, Connection Type, Source
        Hostname, Source IP Address, Source Port Number (TCP), Connection Start, Server Product
        Name, Server Version, Server Build Number, Client Product Name, Client Version, and
        Client Build Number. To call this API, you must have VPN Server administrator
        privileges.

        Args:
            name: Connection name (``Name_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``Name_str``, ``Type_u32``, ``Hostname_str``, ``Ip_ip``,
            ``Port_u32``, ``ConnectedTime_dt``, ``ServerStr_str``, ``ServerVer_u32``,
            ``ServerBuild_u32``, ``ClientStr_str``, ``ClientVer_u32``, ``ClientBuild_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetConnectionInfo", {
            "Name_str": name,
        })

    def set_hub_online(
        self,
        hub_name: str,
        *,
        online: Optional[bool] = None,
    ) -> RpcResult:
        """SetHubOnline -- Switch Virtual Hub to Online or Offline.

        Switch Virtual Hub to Online or Offline. Use this to set the Virtual Hub to online or
        offline. A Virtual Hub with an offline status cannot receive VPN connections from
        clients. When you set the Virtual Hub offline, all sessions will be disconnected. A
        Virtual Hub with an offline status cannot receive VPN connections from clients. This API
        cannot be invoked on VPN Bridge. You cannot execute this API for Virtual Hubs of VPN
        Servers operating as a cluster.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            online: Online / offline flag (``Online_bool``, boolean)

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Online_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetHubOnline", {
            "HubName_str": hub_name,
            "Online_bool": online,
        })

    def get_hub_status(
        self,
        hub_name: str,
    ) -> RpcResult:
        """GetHubStatus -- Get Current Status of Virtual Hub.

        Get Current Status of Virtual Hub. Use this to get the current status of the Virtual Hub
        currently being managed. You can get the following information: Virtual Hub Type, Number
        of Sessions, Number of Each Type of Object, Number of Logins, Last Login, Last
        Communication, and Communication Statistical Data.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Online_bool``, ``HubType_u32``,
            ``NumSessions_u32``, ``NumSessionsClient_u32``, ``NumSessionsBridge_u32``,
            ``NumAccessLists_u32``, ``NumUsers_u32``, ``NumGroups_u32``, ``NumMacTables_u32``,
            ``NumIpTables_u32``, ``Recv.BroadcastBytes_u64``, ``Recv.BroadcastCount_u64``,
            ``Recv.UnicastBytes_u64``, ``Recv.UnicastCount_u64``, ``Send.BroadcastBytes_u64``,
            ``Send.BroadcastCount_u64``, ``Send.UnicastBytes_u64``, ``Send.UnicastCount_u64``,
            ``SecureNATEnabled_bool``, ``LastCommTime_dt``, ``LastLoginTime_dt``,
            ``CreatedTime_dt``, ``NumLogin_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetHubStatus", {
            "HubName_str": hub_name,
        })

    def set_hub_log(
        self,
        hub_name: str,
        *,
        save_security_log: Optional[bool] = None,
        security_log_switch_type: Optional[Union[int, "LogSwitchType"]] = None,
        save_packet_log: Optional[bool] = None,
        packet_log_switch_type: Optional[Union[int, "LogSwitchType"]] = None,
        packet_log_config: Optional[Sequence[int]] = None,
    ) -> RpcResult:
        """SetHubLog -- Set the logging configuration of the Virtual Hub.

        Set the logging configuration of the Virtual Hub. Use this to enable or disable a
        security log or packet logs of the Virtual Hub currently being managed, set the save
        contents of the packet log for each type of packet to be saved, and set the log file
        switch cycle for the security log or packet log that the currently managed Virtual Hub
        saves. There are the following packet types: TCP Connection Log, TCP Packet Log, DHCP
        Packet Log, UDP Packet Log, ICMP Packet Log, IP Packet Log, ARP Packet Log, and Ethernet
        Packet Log. To get the current setting, you can use the LogGet API. The log file switch
        cycle can be changed to switch in every second, every minute, every hour, every day,
        every month or not switch. To get the current setting, you can use the GetHubLog API.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            save_security_log: The flag to enable / disable saving the security log
                (``SaveSecurityLog_bool``, boolean)
            security_log_switch_type: The log filename switching setting of the security log
                (``SecurityLogSwitchType_u32``, number (enum))
                Accepts an :class:`LogSwitchType` member or its integer value: 0 = No switching;
                1 = Secondly basis; 2 = Minutely basis; 3 = Hourly basis; 4 = Daily basis; 5 =
                Monthly basis.
            save_packet_log: The flag to enable / disable saving the security log
                (``SavePacketLog_bool``, boolean)
            packet_log_switch_type: The log filename switching settings of the packet logs
                (``PacketLogSwitchType_u32``, number (enum))
                Accepts an :class:`LogSwitchType` member or its integer value: 0 = No switching;
                1 = Secondly basis; 2 = Minutely basis; 3 = Hourly basis; 4 = Daily basis; 5 =
                Monthly basis.
            packet_log_config: Specify the save contents of the packet logs (uint * 16 array).
                The index numbers: TcpConnection = 0, TcpAll = 1, DHCP = 2, UDP = 3, ICMP = 4, IP =
                5, ARP = 6, Ethernet = 7. (``PacketLogConfig_u32``, number (enum))
                Each element accepts an :class:`PacketLogConfig` member or its integer value: 0
                = Not save; 1 = Only header; 2 = All payloads.

        Returns:
            RpcResult with the fields: ``HubName_str``, ``SaveSecurityLog_bool``,
            ``SecurityLogSwitchType_u32``, ``SavePacketLog_bool``, ``PacketLogSwitchType_u32``,
            ``PacketLogConfig_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetHubLog", {
            "HubName_str": hub_name,
            "SaveSecurityLog_bool": save_security_log,
            "SecurityLogSwitchType_u32": security_log_switch_type,
            "SavePacketLog_bool": save_packet_log,
            "PacketLogSwitchType_u32": packet_log_switch_type,
            "PacketLogConfig_u32": packet_log_config,
        })

    def get_hub_log(
        self,
        hub_name: str,
    ) -> RpcResult:
        """GetHubLog -- Get the logging configuration of the Virtual Hub.

        Get the logging configuration of the Virtual Hub. Use this to get the configuration for
        a security log or packet logs of the Virtual Hub currently being managed, get the
        setting for save contents of the packet log for each type of packet to be saved, and get
        the log file switch cycle for the security log or packet log that the currently managed
        Virtual Hub saves. To set the current setting, you can use the SetHubLog API.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``SaveSecurityLog_bool``,
            ``SecurityLogSwitchType_u32``, ``SavePacketLog_bool``, ``PacketLogSwitchType_u32``,
            ``PacketLogConfig_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetHubLog", {
            "HubName_str": hub_name,
        })

    def add_ca(
        self,
        hub_name: str,
        cert: Union[bytes, bytearray, str],
    ) -> RpcResult:
        """AddCa -- Add Trusted CA Certificate.

        Add Trusted CA Certificate. Use this to add a new certificate to a list of CA
        certificates trusted by the currently managed Virtual Hub. The list of certificate
        authority certificates that are registered is used to verify certificates when a VPN
        Client is connected in signed certificate authentication mode. To get a list of the
        current certificates you can use the EnumCa API. The certificate you add must be saved
        in the X.509 file format. This API cannot be invoked on VPN Bridge. You cannot execute
        this API for Virtual Hubs of VPN Servers operating as a member server on a cluster.

        .. note::
           Fields left at ``None`` are not sent, and the VPN Server applies its own default for
           each of them.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            cert: The body of the X.509 certificate (``Cert_bin``, string (Base64 binary))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Cert_bin``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("AddCa", {
            "HubName_str": hub_name,
            "Cert_bin": cert,
        })

    def enum_ca(
        self,
        hub_name: str,
    ) -> RpcResult:
        """EnumCa -- Get List of Trusted CA Certificates.

        Get List of Trusted CA Certificates. Here you can manage the certificate authority
        certificates that are trusted by this currently managed Virtual Hub. The list of
        certificate authority certificates that are registered is used to verify certificates
        when a VPN Client is connected in signed certificate authentication mode. This API
        cannot be invoked on VPN Bridge. You cannot execute this API for Virtual Hubs of VPN
        Servers operating as a member server on a cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``CAList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumCa", {
            "HubName_str": hub_name,
        })

    def get_ca(
        self,
        hub_name: str,
        key: int,
    ) -> RpcResult:
        """GetCa -- Get Trusted CA Certificate.

        Get Trusted CA Certificate. Use this to get an existing certificate from the list of CA
        certificates trusted by the currently managed Virtual Hub and save it as a file in X.509
        format. This API cannot be invoked on VPN Bridge. You cannot execute this API for
        Virtual Hubs of VPN Servers operating as a member server on a cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            key: The key id of the certificate (``Key_u32``, number (uint32))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Key_u32``, ``Cert_bin``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetCa", {
            "HubName_str": hub_name,
            "Key_u32": key,
        })

    def delete_ca(
        self,
        hub_name: str,
        key: int,
    ) -> RpcResult:
        """DeleteCa -- Delete Trusted CA Certificate.

        Delete Trusted CA Certificate. Use this to delete an existing certificate from the list
        of CA certificates trusted by the currently managed Virtual Hub. To get a list of the
        current certificates you can use the EnumCa API. This API cannot be invoked on VPN
        Bridge. You cannot execute this API for Virtual Hubs of VPN Servers operating as a
        member server on a cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            key: Certificate key id to be deleted (``Key_u32``, number (uint32))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Key_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("DeleteCa", {
            "HubName_str": hub_name,
            "Key_u32": key,
        })

    def create_link(
        self,
        hub_name_ex: str,
        account_name: str,
        hostname: str,
        port: int,
        hub_name: str,
        *,
        check_server_cert: Optional[bool] = None,
        proxy_type: Optional[Union[int, "ProxyType"]] = None,
        max_connection: Optional[int] = None,
        use_encrypt: Optional[bool] = None,
        use_compress: Optional[bool] = None,
        half_connection: Optional[bool] = None,
        additional_connection_interval: Optional[int] = None,
        connection_disconnect_span: Optional[int] = None,
        auth_type: Optional[Union[int, "ClientAuthType"]] = None,
        username: Optional[str] = None,
        hashed_password: Optional[Union[bytes, bytearray, str]] = None,
        plain_password: Optional[str] = None,
        client_x: Optional[Union[bytes, bytearray, str]] = None,
        client_k: Optional[Union[bytes, bytearray, str]] = None,
        policy_dhcp_filter: Optional[bool] = None,
        policy_dhcp_no_server: Optional[bool] = None,
        policy_dhcp_force: Optional[bool] = None,
        sec_pol_check_mac: Optional[bool] = None,
        sec_pol_check_ip: Optional[bool] = None,
        policy_arp_dhcp_only: Optional[bool] = None,
        policy_privacy_filter: Optional[bool] = None,
        policy_no_server: Optional[bool] = None,
        policy_no_broadcast_limiter: Optional[bool] = None,
        policy_max_mac: Optional[int] = None,
        policy_max_ip: Optional[int] = None,
        policy_max_upload: Optional[int] = None,
        policy_max_download: Optional[int] = None,
        policy_rs_and_ra_filter: Optional[bool] = None,
        sec_pol_ra_filter: Optional[bool] = None,
        policy_dhcpv6_filter: Optional[bool] = None,
        policy_dhcpv6_no_server: Optional[bool] = None,
        sec_pol_check_ipv6: Optional[bool] = None,
        policy_no_server_v6: Optional[bool] = None,
        policy_max_ipv6: Optional[int] = None,
        policy_filter_ipv4: Optional[bool] = None,
        policy_filter_ipv6: Optional[bool] = None,
        policy_filter_non_ip: Optional[bool] = None,
        policy_no_ipv6_default_router_in_ra: Optional[bool] = None,
        policy_vlan_id: Optional[int] = None,
        policy_ver3: Optional[bool] = None,
    ) -> RpcResult:
        """CreateLink -- Create New Cascade Connection.

        Create New Cascade Connection. Use this to create a new Cascade Connection on the
        currently managed Virtual Hub. By using a Cascade Connection, you can connect this
        Virtual Hub by Cascade Connection to another Virtual Hub that is operating on the same
        or a different computer. To create a Cascade Connection, you must specify the name of
        the Cascade Connection, destination server and destination Virtual Hub and user name.
        When a new Cascade Connection is created, the type of user authentication is initially
        set as Anonymous Authentication and the proxy server setting and the verification
        options of the server certificate is not set. To change these settings and other
        advanced settings after a Cascade Connection has been created, use the other APIs that
        include the name "Link". [Warning About Cascade Connections] By connecting using a
        Cascade Connection you can create a Layer 2 bridge between multiple Virtual Hubs but if
        the connection is incorrectly configured, a loopback Cascade Connection could
        inadvertently be created. When using a Cascade Connection function please design the
        network topology with care. You cannot execute this API for Virtual Hubs of VPN Servers
        operating as a cluster.

        .. note::
           Fields left at ``None`` are not sent, and the VPN Server applies its own default for
           each of them.

        Args:
            hub_name_ex: The Virtual Hub name (``HubName_Ex_str``, string (ASCII))
            account_name: Client Option Parameters: Specify the name of the Cascade Connection
                (``AccountName_utf``, string (UTF8))
            hostname: Client Option Parameters: Specify the hostname of the destination VPN
                Server. You can also specify by IP address. (``Hostname_str``, string (ASCII))
            port: Client Option Parameters: Specify the port number of the destination VPN
                Server. (``Port_u32``, number (uint32))
            hub_name: Client Option Parameters: The Virtual Hub on the destination VPN Server
                (``HubName_str``, string (ASCII))
            check_server_cert: The flag to enable validation for the server certificate
                (``CheckServerCert_bool``, boolean)
            proxy_type: Client Option Parameters: The type of the proxy server
                (``ProxyType_u32``, number (enum))
                Accepts an :class:`ProxyType` member or its integer value: 0 = Direct TCP
                connection; 1 = Connection via HTTP proxy server; 2 = Connection via SOCKS proxy
                server.
            max_connection: Client Option Parameters: Number of TCP Connections to Use in VPN
                Communication (``MaxConnection_u32``, number (uint32))
            use_encrypt: Client Option Parameters: The flag to enable the encryption on the
                communication (``UseEncrypt_bool``, boolean)
            use_compress: Client Option Parameters: Enable / Disable Data Compression when
                Communicating by Cascade Connection (``UseCompress_bool``, boolean)
            half_connection: Client Option Parameters: Specify true when enabling half duplex
                mode. When using two or more TCP connections for VPN communication, it is possible
                to use Half Duplex Mode. By enabling half duplex mode it is possible to
                automatically fix data transmission direction as half and half for each TCP
                connection. In the case where a VPN using 8 TCP connections is established, for
                example, when half-duplex is enabled, communication can be fixes so that 4 TCP
                connections are dedicated to the upload direction and the other 4 connections are
                dedicated to the download direction. (``HalfConnection_bool``, boolean)
            additional_connection_interval: Client Option Parameters: Connection attempt
                interval when additional connection will be established
                (``AdditionalConnectionInterval_u32``, number (uint32))
            connection_disconnect_span: Client Option Parameters: Connection Life of Each TCP
                Connection (0 for no keep-alive) (``ConnectionDisconnectSpan_u32``, number (uint32))
            auth_type: Authentication type (``AuthType_u32``, number (enum))
                Accepts an :class:`ClientAuthType` member or its integer value: 0 = Anonymous
                authentication; 1 = SHA-0 hashed password authentication; 2 = Plain password
                authentication; 3 = Certificate authentication.
            username: User name (``Username_str``, string (ASCII))
            hashed_password: SHA-0 Hashed password. Valid only if ClientAuth_AuthType_u32 ==
                SHA0_Hashed_Password (1). The SHA-0 hashed password must be caluclated by the
                SHA0(UpperCase(username_ascii_string) + password_ascii_string).
                (``HashedPassword_bin``, string (Base64 binary))
            plain_password: Plaintext Password. Valid only if ClientAuth_AuthType_u32 ==
                PlainPassword (2). (``PlainPassword_str``, string (ASCII))
            client_x: Client certificate. Valid only if ClientAuth_AuthType_u32 == Cert (3).
                (``ClientX_bin``, string (Base64 binary))
            client_k: Client private key of the certificate. Valid only if
                ClientAuth_AuthType_u32 == Cert (3). (``ClientK_bin``, string (Base64 binary))
            policy_dhcp_filter: Security policy: Filter DHCP Packets (IPv4). All IPv4 DHCP
                packets in sessions defined this policy will be filtered.
                (``policy:DHCPFilter_bool``, boolean)
            policy_dhcp_no_server: Security policy: Disallow DHCP Server Operation (IPv4).
                Computers connected to sessions that have this policy setting will not be allowed to
                become a DHCP server and distribute IPv4 addresses to DHCP clients.
                (``policy:DHCPNoServer_bool``, boolean)
            policy_dhcp_force: Security policy: Enforce DHCP Allocated IP Addresses (IPv4).
                Computers in sessions that have this policy setting will only be able to use IPv4
                addresses allocated by a DHCP server on the virtual network side.
                (``policy:DHCPForce_bool``, boolean)
            sec_pol_check_mac: Security policy: Prohibit the duplicate MAC address
                (``SecPol_CheckMac_bool``, boolean)
            sec_pol_check_ip: Security policy: Prohibit a duplicate IP address (IPv4)
                (``SecPol_CheckIP_bool``, boolean)
            policy_arp_dhcp_only: Security policy: Deny Non-ARP / Non-DHCP / Non-ICMPv6
                broadcasts. The sending or receiving of broadcast packets that are not ARP protocol,
                DHCP protocol, nor ICMPv6 on the virtual network will not be allowed for sessions
                with this policy setting. (``policy:ArpDhcpOnly_bool``, boolean)
            policy_privacy_filter: Security policy: Privacy Filter Mode. All direct
                communication between sessions with the privacy filter mode policy setting will be
                filtered. (``policy:PrivacyFilter_bool``, boolean)
            policy_no_server: Security policy: Deny Operation as TCP/IP Server (IPv4). Computers
                of sessions with this policy setting can't listen and accept TCP/IP connections in
                IPv4. (``policy:NoServer_bool``, boolean)
            policy_no_broadcast_limiter: Security policy: Unlimited Number of Broadcasts. If a
                computer of a session with this policy setting sends broadcast packets of a number
                unusually larger than what would be considered normal on the virtual network, there
                will be no automatic limiting. (``policy:NoBroadcastLimiter_bool``, boolean)
            policy_max_mac: Security policy: Maximum Number of MAC Addresses. For sessions with
                this policy setting, this limits the number of MAC addresses per session.
                (``policy:MaxMac_u32``, number (uint32))
            policy_max_ip: Security policy: Maximum Number of IP Addresses (IPv4). For sessions
                with this policy setting, this specifies the number of IPv4 addresses that can be
                registered for a single session. (``policy:MaxIP_u32``, number (uint32))
            policy_max_upload: Security policy: Upload Bandwidth. For sessions with this policy
                setting, this limits the traffic bandwidth that is in the inwards direction from
                outside to inside the Virtual Hub. (``policy:MaxUpload_u32``, number (uint32))
            policy_max_download: Security policy: Download Bandwidth. For sessions with this
                policy setting, this limits the traffic bandwidth that is in the outwards direction
                from inside the Virtual Hub to outside the Virtual Hub. (``policy:MaxDownload_u32``,
                number (uint32))
            policy_rs_and_ra_filter: Security policy: Filter RS / RA Packets (IPv6). All ICMPv6
                packets which the message-type is 133 (Router Solicitation) or 134 (Router
                Advertisement) in sessions defined this policy will be filtered. As a result, an
                IPv6 client will be unable to use IPv6 address prefix auto detection and IPv6
                default gateway auto detection. (``policy:RSandRAFilter_bool``, boolean)
            sec_pol_ra_filter: Security policy: Filter the router advertisement packet (IPv6)
                (``SecPol_RAFilter_bool``, boolean)
            policy_dhcpv6_filter: Security policy: Filter DHCP Packets (IPv6). All IPv6 DHCP
                packets in sessions defined this policy will be filtered.
                (``policy:DHCPv6Filter_bool``, boolean)
            policy_dhcpv6_no_server: Security policy: Disallow DHCP Server Operation (IPv6).
                Computers connected to sessions that have this policy setting will not be allowed to
                become a DHCP server and distribute IPv6 addresses to DHCP clients.
                (``policy:DHCPv6NoServer_bool``, boolean)
            sec_pol_check_ipv6: Security policy: Prohibit the duplicate IP address (IPv6)
                (``SecPol_CheckIPv6_bool``, boolean)
            policy_no_server_v6: Security policy: Deny Operation as TCP/IP Server (IPv6).
                Computers of sessions with this policy setting can't listen and accept TCP/IP
                connections in IPv6. (``policy:NoServerV6_bool``, boolean)
            policy_max_ipv6: Security policy: Maximum Number of IP Addresses (IPv6). For
                sessions with this policy setting, this specifies the number of IPv6 addresses that
                can be registered for a single session. (``policy:MaxIPv6_u32``, number (uint32))
            policy_filter_ipv4: Security policy: Filter All IPv4 Packets. All IPv4 and ARP
                packets in sessions defined this policy will be filtered.
                (``policy:FilterIPv4_bool``, boolean)
            policy_filter_ipv6: Security policy: Filter All IPv6 Packets. All IPv6 packets in
                sessions defined this policy will be filtered. (``policy:FilterIPv6_bool``, boolean)
            policy_filter_non_ip: Security policy: Filter All Non-IP Packets. All non-IP packets
                in sessions defined this policy will be filtered. "Non-IP packet" mean a packet
                which is not IPv4, ARP nor IPv6. Any tagged-VLAN packets via the Virtual Hub will be
                regarded as non-IP packets. (``policy:FilterNonIP_bool``, boolean)
            policy_no_ipv6_default_router_in_ra: Security policy: No Default-Router on IPv6 RA.
                In all VPN Sessions defines this policy, any IPv6 RA (Router Advertisement) packet
                with non-zero value in the router-lifetime will set to zero-value. This is effective
                to avoid the horrible behavior from the IPv6 routing confusion which is caused by
                the VPN client's attempts to use the remote-side IPv6 router as its local IPv6
                router. (``policy:NoIPv6DefaultRouterInRA_bool``, boolean)
            policy_vlan_id: Security policy: VLAN ID (IEEE802.1Q). You can specify the VLAN ID
                on the security policy. All VPN Sessions defines this policy, all Ethernet packets
                toward the Virtual Hub from the user will be inserted a VLAN tag (IEEE 802.1Q) with
                the VLAN ID. The user can also receive only packets with a VLAN tag which has the
                same VLAN ID. (Receiving process removes the VLAN tag automatically.) Any Ethernet
                packets with any other VLAN IDs or non-VLAN packets will not be received. All VPN
                Sessions without this policy definition can send / receive any kinds of Ethernet
                packets regardless of VLAN tags, and VLAN tags are not inserted or removed
                automatically. Any tagged-VLAN packets via the Virtual Hub will be regarded as
                non-IP packets. Therefore, tagged-VLAN packets are not subjects for IPv4 / IPv6
                security policies, access lists nor other IPv4 / IPv6 specific deep processing.
                (``policy:VLanId_u32``, number (uint32))
            policy_ver3: Security policy: Whether version 3.0 (must be true)
                (``policy:Ver3_bool``, boolean)

        Returns:
            RpcResult with the fields: ``HubName_Ex_str``, ``Online_bool``,
            ``CheckServerCert_bool``, ``ServerCert_bin``, ``AccountName_utf``, ``Hostname_str``,
            ``Port_u32``, ``ProxyType_u32``, ``ProxyName_str``, ``ProxyPort_u32``,
            ``ProxyUsername_str``, ``ProxyPassword_str``, ``HubName_str``,
            ``MaxConnection_u32``, ``UseEncrypt_bool``, ``UseCompress_bool``,
            ``HalfConnection_bool``, ``AdditionalConnectionInterval_u32``,
            ``ConnectionDisconnectSpan_u32``, ``DisableQoS_bool``, ``NoTls1_bool``,
            ``NoUdpAcceleration_bool``, ``AuthType_u32``, ``Username_str``,
            ``HashedPassword_bin``, ``PlainPassword_str``, ``ClientX_bin``, ``ClientK_bin``,
            ``policy:DHCPFilter_bool``, ``policy:DHCPNoServer_bool``, ``policy:DHCPForce_bool``,
            ``SecPol_CheckMac_bool``, ``SecPol_CheckIP_bool``, ``policy:ArpDhcpOnly_bool``,
            ``policy:PrivacyFilter_bool``, ``policy:NoServer_bool``,
            ``policy:NoBroadcastLimiter_bool``, ``policy:MaxMac_u32``, ``policy:MaxIP_u32``,
            ``policy:MaxUpload_u32``, ``policy:MaxDownload_u32``, ``policy:RSandRAFilter_bool``,
            ``SecPol_RAFilter_bool``, ``policy:DHCPv6Filter_bool``,
            ``policy:DHCPv6NoServer_bool``, ``SecPol_CheckIPv6_bool``,
            ``policy:NoServerV6_bool``, ``policy:MaxIPv6_u32``, ``policy:FilterIPv4_bool``,
            ``policy:FilterIPv6_bool``, ``policy:FilterNonIP_bool``,
            ``policy:NoIPv6DefaultRouterInRA_bool``, ``policy:VLanId_u32``,
            ``policy:Ver3_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("CreateLink", {
            "HubName_Ex_str": hub_name_ex,
            "AccountName_utf": account_name,
            "Hostname_str": hostname,
            "Port_u32": port,
            "HubName_str": hub_name,
            "CheckServerCert_bool": check_server_cert,
            "ProxyType_u32": proxy_type,
            "MaxConnection_u32": max_connection,
            "UseEncrypt_bool": use_encrypt,
            "UseCompress_bool": use_compress,
            "HalfConnection_bool": half_connection,
            "AdditionalConnectionInterval_u32": additional_connection_interval,
            "ConnectionDisconnectSpan_u32": connection_disconnect_span,
            "AuthType_u32": auth_type,
            "Username_str": username,
            "HashedPassword_bin": hashed_password,
            "PlainPassword_str": plain_password,
            "ClientX_bin": client_x,
            "ClientK_bin": client_k,
            "policy:DHCPFilter_bool": policy_dhcp_filter,
            "policy:DHCPNoServer_bool": policy_dhcp_no_server,
            "policy:DHCPForce_bool": policy_dhcp_force,
            "SecPol_CheckMac_bool": sec_pol_check_mac,
            "SecPol_CheckIP_bool": sec_pol_check_ip,
            "policy:ArpDhcpOnly_bool": policy_arp_dhcp_only,
            "policy:PrivacyFilter_bool": policy_privacy_filter,
            "policy:NoServer_bool": policy_no_server,
            "policy:NoBroadcastLimiter_bool": policy_no_broadcast_limiter,
            "policy:MaxMac_u32": policy_max_mac,
            "policy:MaxIP_u32": policy_max_ip,
            "policy:MaxUpload_u32": policy_max_upload,
            "policy:MaxDownload_u32": policy_max_download,
            "policy:RSandRAFilter_bool": policy_rs_and_ra_filter,
            "SecPol_RAFilter_bool": sec_pol_ra_filter,
            "policy:DHCPv6Filter_bool": policy_dhcpv6_filter,
            "policy:DHCPv6NoServer_bool": policy_dhcpv6_no_server,
            "SecPol_CheckIPv6_bool": sec_pol_check_ipv6,
            "policy:NoServerV6_bool": policy_no_server_v6,
            "policy:MaxIPv6_u32": policy_max_ipv6,
            "policy:FilterIPv4_bool": policy_filter_ipv4,
            "policy:FilterIPv6_bool": policy_filter_ipv6,
            "policy:FilterNonIP_bool": policy_filter_non_ip,
            "policy:NoIPv6DefaultRouterInRA_bool": policy_no_ipv6_default_router_in_ra,
            "policy:VLanId_u32": policy_vlan_id,
            "policy:Ver3_bool": policy_ver3,
        })

    def get_link(
        self,
        hub_name_ex: str,
        account_name: str,
    ) -> RpcResult:
        """GetLink -- Get the Cascade Connection Setting.

        Get the Cascade Connection Setting. Use this to get the Connection Setting of a Cascade
        Connection that is registered on the currently managed Virtual Hub. To change the
        Connection Setting contents of the Cascade Connection, use the APIs that include the
        name "Link" after creating the Cascade Connection. You cannot execute this API for
        Virtual Hubs of VPN Servers operating as a cluster.

        Args:
            hub_name_ex: The Virtual Hub name (``HubName_Ex_str``, string (ASCII))
            account_name: Client Option Parameters: Specify the name of the Cascade Connection
                (``AccountName_utf``, string (UTF8))

        Returns:
            RpcResult with the fields: ``HubName_Ex_str``, ``Online_bool``,
            ``CheckServerCert_bool``, ``ServerCert_bin``, ``AccountName_utf``, ``Hostname_str``,
            ``Port_u32``, ``ProxyType_u32``, ``ProxyName_str``, ``ProxyPort_u32``,
            ``ProxyUsername_str``, ``ProxyPassword_str``, ``HubName_str``,
            ``MaxConnection_u32``, ``UseEncrypt_bool``, ``UseCompress_bool``,
            ``HalfConnection_bool``, ``AdditionalConnectionInterval_u32``,
            ``ConnectionDisconnectSpan_u32``, ``DisableQoS_bool``, ``NoTls1_bool``,
            ``NoUdpAcceleration_bool``, ``AuthType_u32``, ``Username_str``,
            ``HashedPassword_bin``, ``PlainPassword_str``, ``ClientX_bin``, ``ClientK_bin``,
            ``policy:DHCPFilter_bool``, ``policy:DHCPNoServer_bool``, ``policy:DHCPForce_bool``,
            ``SecPol_CheckMac_bool``, ``SecPol_CheckIP_bool``, ``policy:ArpDhcpOnly_bool``,
            ``policy:PrivacyFilter_bool``, ``policy:NoServer_bool``,
            ``policy:NoBroadcastLimiter_bool``, ``policy:MaxMac_u32``, ``policy:MaxIP_u32``,
            ``policy:MaxUpload_u32``, ``policy:MaxDownload_u32``, ``policy:RSandRAFilter_bool``,
            ``SecPol_RAFilter_bool``, ``policy:DHCPv6Filter_bool``,
            ``policy:DHCPv6NoServer_bool``, ``SecPol_CheckIPv6_bool``,
            ``policy:NoServerV6_bool``, ``policy:MaxIPv6_u32``, ``policy:FilterIPv4_bool``,
            ``policy:FilterIPv6_bool``, ``policy:FilterNonIP_bool``,
            ``policy:NoIPv6DefaultRouterInRA_bool``, ``policy:VLanId_u32``,
            ``policy:Ver3_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetLink", {
            "HubName_Ex_str": hub_name_ex,
            "AccountName_utf": account_name,
        })

    def set_link(
        self,
        hub_name_ex: str,
        account_name: str,
        hostname: str,
        port: int,
        hub_name: str,
        *,
        check_server_cert: Optional[bool] = None,
        proxy_type: Optional[Union[int, "ProxyType"]] = None,
        max_connection: Optional[int] = None,
        use_encrypt: Optional[bool] = None,
        use_compress: Optional[bool] = None,
        half_connection: Optional[bool] = None,
        additional_connection_interval: Optional[int] = None,
        connection_disconnect_span: Optional[int] = None,
        auth_type: Optional[Union[int, "ClientAuthType"]] = None,
        username: Optional[str] = None,
        hashed_password: Optional[Union[bytes, bytearray, str]] = None,
        plain_password: Optional[str] = None,
        client_x: Optional[Union[bytes, bytearray, str]] = None,
        client_k: Optional[Union[bytes, bytearray, str]] = None,
        policy_dhcp_filter: Optional[bool] = None,
        policy_dhcp_no_server: Optional[bool] = None,
        policy_dhcp_force: Optional[bool] = None,
        sec_pol_check_mac: Optional[bool] = None,
        sec_pol_check_ip: Optional[bool] = None,
        policy_arp_dhcp_only: Optional[bool] = None,
        policy_privacy_filter: Optional[bool] = None,
        policy_no_server: Optional[bool] = None,
        policy_no_broadcast_limiter: Optional[bool] = None,
        policy_max_mac: Optional[int] = None,
        policy_max_ip: Optional[int] = None,
        policy_max_upload: Optional[int] = None,
        policy_max_download: Optional[int] = None,
        policy_rs_and_ra_filter: Optional[bool] = None,
        sec_pol_ra_filter: Optional[bool] = None,
        policy_dhcpv6_filter: Optional[bool] = None,
        policy_dhcpv6_no_server: Optional[bool] = None,
        sec_pol_check_ipv6: Optional[bool] = None,
        policy_no_server_v6: Optional[bool] = None,
        policy_max_ipv6: Optional[int] = None,
        policy_filter_ipv4: Optional[bool] = None,
        policy_filter_ipv6: Optional[bool] = None,
        policy_filter_non_ip: Optional[bool] = None,
        policy_no_ipv6_default_router_in_ra: Optional[bool] = None,
        policy_vlan_id: Optional[int] = None,
        policy_ver3: Optional[bool] = None,
    ) -> RpcResult:
        """SetLink -- Change Existing Cascade Connection.

        Change Existing Cascade Connection. Use this to alter the setting of an existing Cascade
        Connection on the currently managed Virtual Hub.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            hub_name_ex: The Virtual Hub name (``HubName_Ex_str``, string (ASCII))
            account_name: Client Option Parameters: Specify the name of the Cascade Connection
                (``AccountName_utf``, string (UTF8))
            hostname: Client Option Parameters: Specify the hostname of the destination VPN
                Server. You can also specify by IP address. (``Hostname_str``, string (ASCII))
            port: Client Option Parameters: Specify the port number of the destination VPN
                Server. (``Port_u32``, number (uint32))
            hub_name: Client Option Parameters: The Virtual Hub on the destination VPN Server
                (``HubName_str``, string (ASCII))
            check_server_cert: The flag to enable validation for the server certificate
                (``CheckServerCert_bool``, boolean)
            proxy_type: Client Option Parameters: The type of the proxy server
                (``ProxyType_u32``, number (enum))
                Accepts an :class:`ProxyType` member or its integer value: 0 = Direct TCP
                connection; 1 = Connection via HTTP proxy server; 2 = Connection via SOCKS proxy
                server.
            max_connection: Client Option Parameters: Number of TCP Connections to Use in VPN
                Communication (``MaxConnection_u32``, number (uint32))
            use_encrypt: Client Option Parameters: The flag to enable the encryption on the
                communication (``UseEncrypt_bool``, boolean)
            use_compress: Client Option Parameters: Enable / Disable Data Compression when
                Communicating by Cascade Connection (``UseCompress_bool``, boolean)
            half_connection: Client Option Parameters: Specify true when enabling half duplex
                mode. When using two or more TCP connections for VPN communication, it is possible
                to use Half Duplex Mode. By enabling half duplex mode it is possible to
                automatically fix data transmission direction as half and half for each TCP
                connection. In the case where a VPN using 8 TCP connections is established, for
                example, when half-duplex is enabled, communication can be fixes so that 4 TCP
                connections are dedicated to the upload direction and the other 4 connections are
                dedicated to the download direction. (``HalfConnection_bool``, boolean)
            additional_connection_interval: Client Option Parameters: Connection attempt
                interval when additional connection will be established
                (``AdditionalConnectionInterval_u32``, number (uint32))
            connection_disconnect_span: Client Option Parameters: Connection Life of Each TCP
                Connection (0 for no keep-alive) (``ConnectionDisconnectSpan_u32``, number (uint32))
            auth_type: Authentication type (``AuthType_u32``, number (enum))
                Accepts an :class:`ClientAuthType` member or its integer value: 0 = Anonymous
                authentication; 1 = SHA-0 hashed password authentication; 2 = Plain password
                authentication; 3 = Certificate authentication.
            username: User name (``Username_str``, string (ASCII))
            hashed_password: SHA-0 Hashed password. Valid only if ClientAuth_AuthType_u32 ==
                SHA0_Hashed_Password (1). The SHA-0 hashed password must be caluclated by the
                SHA0(UpperCase(username_ascii_string) + password_ascii_string).
                (``HashedPassword_bin``, string (Base64 binary))
            plain_password: Plaintext Password. Valid only if ClientAuth_AuthType_u32 ==
                PlainPassword (2). (``PlainPassword_str``, string (ASCII))
            client_x: Client certificate. Valid only if ClientAuth_AuthType_u32 == Cert (3).
                (``ClientX_bin``, string (Base64 binary))
            client_k: Client private key of the certificate. Valid only if
                ClientAuth_AuthType_u32 == Cert (3). (``ClientK_bin``, string (Base64 binary))
            policy_dhcp_filter: Security policy: Filter DHCP Packets (IPv4). All IPv4 DHCP
                packets in sessions defined this policy will be filtered.
                (``policy:DHCPFilter_bool``, boolean)
            policy_dhcp_no_server: Security policy: Disallow DHCP Server Operation (IPv4).
                Computers connected to sessions that have this policy setting will not be allowed to
                become a DHCP server and distribute IPv4 addresses to DHCP clients.
                (``policy:DHCPNoServer_bool``, boolean)
            policy_dhcp_force: Security policy: Enforce DHCP Allocated IP Addresses (IPv4).
                Computers in sessions that have this policy setting will only be able to use IPv4
                addresses allocated by a DHCP server on the virtual network side.
                (``policy:DHCPForce_bool``, boolean)
            sec_pol_check_mac: Security policy: Prohibit the duplicate MAC address
                (``SecPol_CheckMac_bool``, boolean)
            sec_pol_check_ip: Security policy: Prohibit a duplicate IP address (IPv4)
                (``SecPol_CheckIP_bool``, boolean)
            policy_arp_dhcp_only: Security policy: Deny Non-ARP / Non-DHCP / Non-ICMPv6
                broadcasts. The sending or receiving of broadcast packets that are not ARP protocol,
                DHCP protocol, nor ICMPv6 on the virtual network will not be allowed for sessions
                with this policy setting. (``policy:ArpDhcpOnly_bool``, boolean)
            policy_privacy_filter: Security policy: Privacy Filter Mode. All direct
                communication between sessions with the privacy filter mode policy setting will be
                filtered. (``policy:PrivacyFilter_bool``, boolean)
            policy_no_server: Security policy: Deny Operation as TCP/IP Server (IPv4). Computers
                of sessions with this policy setting can't listen and accept TCP/IP connections in
                IPv4. (``policy:NoServer_bool``, boolean)
            policy_no_broadcast_limiter: Security policy: Unlimited Number of Broadcasts. If a
                computer of a session with this policy setting sends broadcast packets of a number
                unusually larger than what would be considered normal on the virtual network, there
                will be no automatic limiting. (``policy:NoBroadcastLimiter_bool``, boolean)
            policy_max_mac: Security policy: Maximum Number of MAC Addresses. For sessions with
                this policy setting, this limits the number of MAC addresses per session.
                (``policy:MaxMac_u32``, number (uint32))
            policy_max_ip: Security policy: Maximum Number of IP Addresses (IPv4). For sessions
                with this policy setting, this specifies the number of IPv4 addresses that can be
                registered for a single session. (``policy:MaxIP_u32``, number (uint32))
            policy_max_upload: Security policy: Upload Bandwidth. For sessions with this policy
                setting, this limits the traffic bandwidth that is in the inwards direction from
                outside to inside the Virtual Hub. (``policy:MaxUpload_u32``, number (uint32))
            policy_max_download: Security policy: Download Bandwidth. For sessions with this
                policy setting, this limits the traffic bandwidth that is in the outwards direction
                from inside the Virtual Hub to outside the Virtual Hub. (``policy:MaxDownload_u32``,
                number (uint32))
            policy_rs_and_ra_filter: Security policy: Filter RS / RA Packets (IPv6). All ICMPv6
                packets which the message-type is 133 (Router Solicitation) or 134 (Router
                Advertisement) in sessions defined this policy will be filtered. As a result, an
                IPv6 client will be unable to use IPv6 address prefix auto detection and IPv6
                default gateway auto detection. (``policy:RSandRAFilter_bool``, boolean)
            sec_pol_ra_filter: Security policy: Filter the router advertisement packet (IPv6)
                (``SecPol_RAFilter_bool``, boolean)
            policy_dhcpv6_filter: Security policy: Filter DHCP Packets (IPv6). All IPv6 DHCP
                packets in sessions defined this policy will be filtered.
                (``policy:DHCPv6Filter_bool``, boolean)
            policy_dhcpv6_no_server: Security policy: Disallow DHCP Server Operation (IPv6).
                Computers connected to sessions that have this policy setting will not be allowed to
                become a DHCP server and distribute IPv6 addresses to DHCP clients.
                (``policy:DHCPv6NoServer_bool``, boolean)
            sec_pol_check_ipv6: Security policy: Prohibit the duplicate IP address (IPv6)
                (``SecPol_CheckIPv6_bool``, boolean)
            policy_no_server_v6: Security policy: Deny Operation as TCP/IP Server (IPv6).
                Computers of sessions with this policy setting can't listen and accept TCP/IP
                connections in IPv6. (``policy:NoServerV6_bool``, boolean)
            policy_max_ipv6: Security policy: Maximum Number of IP Addresses (IPv6). For
                sessions with this policy setting, this specifies the number of IPv6 addresses that
                can be registered for a single session. (``policy:MaxIPv6_u32``, number (uint32))
            policy_filter_ipv4: Security policy: Filter All IPv4 Packets. All IPv4 and ARP
                packets in sessions defined this policy will be filtered.
                (``policy:FilterIPv4_bool``, boolean)
            policy_filter_ipv6: Security policy: Filter All IPv6 Packets. All IPv6 packets in
                sessions defined this policy will be filtered. (``policy:FilterIPv6_bool``, boolean)
            policy_filter_non_ip: Security policy: Filter All Non-IP Packets. All non-IP packets
                in sessions defined this policy will be filtered. "Non-IP packet" mean a packet
                which is not IPv4, ARP nor IPv6. Any tagged-VLAN packets via the Virtual Hub will be
                regarded as non-IP packets. (``policy:FilterNonIP_bool``, boolean)
            policy_no_ipv6_default_router_in_ra: Security policy: No Default-Router on IPv6 RA.
                In all VPN Sessions defines this policy, any IPv6 RA (Router Advertisement) packet
                with non-zero value in the router-lifetime will set to zero-value. This is effective
                to avoid the horrible behavior from the IPv6 routing confusion which is caused by
                the VPN client's attempts to use the remote-side IPv6 router as its local IPv6
                router. (``policy:NoIPv6DefaultRouterInRA_bool``, boolean)
            policy_vlan_id: Security policy: VLAN ID (IEEE802.1Q). You can specify the VLAN ID
                on the security policy. All VPN Sessions defines this policy, all Ethernet packets
                toward the Virtual Hub from the user will be inserted a VLAN tag (IEEE 802.1Q) with
                the VLAN ID. The user can also receive only packets with a VLAN tag which has the
                same VLAN ID. (Receiving process removes the VLAN tag automatically.) Any Ethernet
                packets with any other VLAN IDs or non-VLAN packets will not be received. All VPN
                Sessions without this policy definition can send / receive any kinds of Ethernet
                packets regardless of VLAN tags, and VLAN tags are not inserted or removed
                automatically. Any tagged-VLAN packets via the Virtual Hub will be regarded as
                non-IP packets. Therefore, tagged-VLAN packets are not subjects for IPv4 / IPv6
                security policies, access lists nor other IPv4 / IPv6 specific deep processing.
                (``policy:VLanId_u32``, number (uint32))
            policy_ver3: Security policy: Whether version 3.0 (must be true)
                (``policy:Ver3_bool``, boolean)

        Returns:
            RpcResult with the fields: ``HubName_Ex_str``, ``Online_bool``,
            ``CheckServerCert_bool``, ``ServerCert_bin``, ``AccountName_utf``, ``Hostname_str``,
            ``Port_u32``, ``ProxyType_u32``, ``ProxyName_str``, ``ProxyPort_u32``,
            ``ProxyUsername_str``, ``ProxyPassword_str``, ``HubName_str``,
            ``MaxConnection_u32``, ``UseEncrypt_bool``, ``UseCompress_bool``,
            ``HalfConnection_bool``, ``AdditionalConnectionInterval_u32``,
            ``ConnectionDisconnectSpan_u32``, ``DisableQoS_bool``, ``NoTls1_bool``,
            ``NoUdpAcceleration_bool``, ``AuthType_u32``, ``Username_str``,
            ``HashedPassword_bin``, ``PlainPassword_str``, ``ClientX_bin``, ``ClientK_bin``,
            ``policy:DHCPFilter_bool``, ``policy:DHCPNoServer_bool``, ``policy:DHCPForce_bool``,
            ``SecPol_CheckMac_bool``, ``SecPol_CheckIP_bool``, ``policy:ArpDhcpOnly_bool``,
            ``policy:PrivacyFilter_bool``, ``policy:NoServer_bool``,
            ``policy:NoBroadcastLimiter_bool``, ``policy:MaxMac_u32``, ``policy:MaxIP_u32``,
            ``policy:MaxUpload_u32``, ``policy:MaxDownload_u32``, ``policy:RSandRAFilter_bool``,
            ``SecPol_RAFilter_bool``, ``policy:DHCPv6Filter_bool``,
            ``policy:DHCPv6NoServer_bool``, ``SecPol_CheckIPv6_bool``,
            ``policy:NoServerV6_bool``, ``policy:MaxIPv6_u32``, ``policy:FilterIPv4_bool``,
            ``policy:FilterIPv6_bool``, ``policy:FilterNonIP_bool``,
            ``policy:NoIPv6DefaultRouterInRA_bool``, ``policy:VLanId_u32``,
            ``policy:Ver3_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetLink", {
            "HubName_Ex_str": hub_name_ex,
            "AccountName_utf": account_name,
            "Hostname_str": hostname,
            "Port_u32": port,
            "HubName_str": hub_name,
            "CheckServerCert_bool": check_server_cert,
            "ProxyType_u32": proxy_type,
            "MaxConnection_u32": max_connection,
            "UseEncrypt_bool": use_encrypt,
            "UseCompress_bool": use_compress,
            "HalfConnection_bool": half_connection,
            "AdditionalConnectionInterval_u32": additional_connection_interval,
            "ConnectionDisconnectSpan_u32": connection_disconnect_span,
            "AuthType_u32": auth_type,
            "Username_str": username,
            "HashedPassword_bin": hashed_password,
            "PlainPassword_str": plain_password,
            "ClientX_bin": client_x,
            "ClientK_bin": client_k,
            "policy:DHCPFilter_bool": policy_dhcp_filter,
            "policy:DHCPNoServer_bool": policy_dhcp_no_server,
            "policy:DHCPForce_bool": policy_dhcp_force,
            "SecPol_CheckMac_bool": sec_pol_check_mac,
            "SecPol_CheckIP_bool": sec_pol_check_ip,
            "policy:ArpDhcpOnly_bool": policy_arp_dhcp_only,
            "policy:PrivacyFilter_bool": policy_privacy_filter,
            "policy:NoServer_bool": policy_no_server,
            "policy:NoBroadcastLimiter_bool": policy_no_broadcast_limiter,
            "policy:MaxMac_u32": policy_max_mac,
            "policy:MaxIP_u32": policy_max_ip,
            "policy:MaxUpload_u32": policy_max_upload,
            "policy:MaxDownload_u32": policy_max_download,
            "policy:RSandRAFilter_bool": policy_rs_and_ra_filter,
            "SecPol_RAFilter_bool": sec_pol_ra_filter,
            "policy:DHCPv6Filter_bool": policy_dhcpv6_filter,
            "policy:DHCPv6NoServer_bool": policy_dhcpv6_no_server,
            "SecPol_CheckIPv6_bool": sec_pol_check_ipv6,
            "policy:NoServerV6_bool": policy_no_server_v6,
            "policy:MaxIPv6_u32": policy_max_ipv6,
            "policy:FilterIPv4_bool": policy_filter_ipv4,
            "policy:FilterIPv6_bool": policy_filter_ipv6,
            "policy:FilterNonIP_bool": policy_filter_non_ip,
            "policy:NoIPv6DefaultRouterInRA_bool": policy_no_ipv6_default_router_in_ra,
            "policy:VLanId_u32": policy_vlan_id,
            "policy:Ver3_bool": policy_ver3,
        })

    def enum_link(
        self,
        hub_name: str,
    ) -> RpcResult:
        """EnumLink -- Get List of Cascade Connections.

        Get List of Cascade Connections. Use this to get a list of Cascade Connections that are
        registered on the currently managed Virtual Hub. By using a Cascade Connection, you can
        connect this Virtual Hub by Layer 2 Cascade Connection to another Virtual Hub that is
        operating on the same or a different computer. [Warning About Cascade Connections] By
        connecting using a Cascade Connection you can create a Layer 2 bridge between multiple
        Virtual Hubs but if the connection is incorrectly configured, a loopback Cascade
        Connection could inadvertently be created. When using a Cascade Connection function
        please design the network topology with care. You cannot execute this API for Virtual
        Hubs of VPN Servers operating as a cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``NumLink_u32``, ``LinkList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumLink", {
            "HubName_str": hub_name,
        })

    def set_link_online(
        self,
        hub_name: str,
        account_name: str,
    ) -> RpcResult:
        """SetLinkOnline -- Switch Cascade Connection to Online Status.

        Switch Cascade Connection to Online Status. When a Cascade Connection registered on the
        currently managed Virtual Hub is specified, use this to switch that Cascade Connection
        to online status. The Cascade Connection that is switched to online status begins the
        process of connecting to the destination VPN Server in accordance with the Connection
        Setting. The Cascade Connection that is switched to online status will establish normal
        connection to the VPN Server or continue to attempt connection until it is switched to
        offline status. You cannot execute this API for Virtual Hubs of VPN Servers operating as
        a cluster.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            account_name: The name of the cascade connection (``AccountName_utf``, string
                (UTF8))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``AccountName_utf``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetLinkOnline", {
            "HubName_str": hub_name,
            "AccountName_utf": account_name,
        })

    def set_link_offline(
        self,
        hub_name: str,
        account_name: str,
    ) -> RpcResult:
        """SetLinkOffline -- Switch Cascade Connection to Offline Status.

        Switch Cascade Connection to Offline Status. When a Cascade Connection registered on the
        currently managed Virtual Hub is specified, use this to switch that Cascade Connection
        to offline status. The Cascade Connection that is switched to offline will not connect
        to the VPN Server until next time it is switched to the online status using the
        SetLinkOnline API You cannot execute this API for Virtual Hubs of VPN Servers operating
        as a cluster.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            account_name: The name of the cascade connection (``AccountName_utf``, string
                (UTF8))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``AccountName_utf``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetLinkOffline", {
            "HubName_str": hub_name,
            "AccountName_utf": account_name,
        })

    def delete_link(
        self,
        hub_name: str,
        account_name: str,
    ) -> RpcResult:
        """DeleteLink -- Delete Cascade Connection Setting.

        Delete Cascade Connection Setting. Use this to delete a Cascade Connection that is
        registered on the currently managed Virtual Hub. If the specified Cascade Connection has
        a status of online, the connections will be automatically disconnected and then the
        Cascade Connection will be deleted. You cannot execute this API for Virtual Hubs of VPN
        Servers operating as a cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            account_name: The name of the cascade connection (``AccountName_utf``, string
                (UTF8))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``AccountName_utf``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("DeleteLink", {
            "HubName_str": hub_name,
            "AccountName_utf": account_name,
        })

    def rename_link(
        self,
        hub_name: str,
        old_account_name: str,
        new_account_name: str,
    ) -> RpcResult:
        """RenameLink -- Change Name of Cascade Connection.

        Change Name of Cascade Connection. When a Cascade Connection registered on the currently
        managed Virtual Hub is specified, use this to change the name of that Cascade
        Connection. You cannot execute this API for Virtual Hubs of VPN Servers operating as a
        cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            old_account_name: The old name of the cascade connection (``OldAccountName_utf``,
                string (UTF8))
            new_account_name: The new name of the cascade connection (``NewAccountName_utf``,
                string (UTF8))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``OldAccountName_utf``,
            ``NewAccountName_utf``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("RenameLink", {
            "HubName_str": hub_name,
            "OldAccountName_utf": old_account_name,
            "NewAccountName_utf": new_account_name,
        })

    def get_link_status(
        self,
        hub_name_ex: str,
        account_name: str,
    ) -> RpcResult:
        """GetLinkStatus -- Get Current Cascade Connection Status.

        Get Current Cascade Connection Status. When a Cascade Connection registered on the
        currently managed Virtual Hub is specified and that Cascade Connection is currently
        online, use this to get its connection status and other information. You cannot execute
        this API for Virtual Hubs of VPN Servers operating as a cluster.

        Args:
            hub_name_ex: The Virtual Hub name (``HubName_Ex_str``, string (ASCII))
            account_name: The name of the cascade connection (``AccountName_utf``, string
                (UTF8))

        Returns:
            RpcResult with the fields: ``HubName_Ex_str``, ``AccountName_utf``, ``Active_bool``,
            ``Connected_bool``, ``SessionStatus_u32``, ``ServerName_str``, ``ServerPort_u32``,
            ``ServerProductName_str``, ``ServerProductVer_u32``, ``ServerProductBuild_u32``,
            ``ServerX_bin``, ``ClientX_bin``, ``StartTime_dt``,
            ``FirstConnectionEstablisiedTime_dt``, ``CurrentConnectionEstablishTime_dt``,
            ``NumConnectionsEatablished_u32``, ``HalfConnection_bool``, ``QoS_bool``,
            ``MaxTcpConnections_u32``, ``NumTcpConnections_u32``,
            ``NumTcpConnectionsUpload_u32``, ``NumTcpConnectionsDownload_u32``,
            ``UseEncrypt_bool``, ``CipherName_str``, ``UseCompress_bool``,
            ``IsRUDPSession_bool``, ``UnderlayProtocol_str``, ``IsUdpAccelerationEnabled_bool``,
            ``IsUsingUdpAcceleration_bool``, ``SessionName_str``, ``ConnectionName_str``,
            ``SessionKey_bin``, ``TotalSendSize_u64``, ``TotalRecvSize_u64``,
            ``TotalSendSizeReal_u64``, ``TotalRecvSizeReal_u64``, ``IsBridgeMode_bool``,
            ``IsMonitorMode_bool``, ``VLanId_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetLinkStatus", {
            "HubName_Ex_str": hub_name_ex,
            "AccountName_utf": account_name,
        })

    def add_access(
        self,
        hub_name: str,
        access_list_single: Union[Mapping[str, Any], Sequence[Mapping[str, Any]]],
    ) -> RpcResult:
        """AddAccess -- Add Access List Rule.

        Add Access List Rule. Use this to add a new rule to the access list of the currently
        managed Virtual Hub. The access list is a set of packet file rules that are applied to
        packets that flow through the Virtual Hub. You can register multiple rules in an access
        list and you can also define an priority for each rule. All packets are checked for the
        conditions specified by the rules registered in the access list and based on the
        operation that is stipulated by the first matching rule, they either pass or are
        discarded. Packets that do not match any rule are implicitly allowed to pass. You can
        also use the access list to generate delays, jitters and packet losses. This API cannot
        be invoked on VPN Bridge. You cannot execute this API for Virtual Hubs of VPN Servers
        operating as a member server on a cluster.

        .. note::
           Fields left at ``None`` are not sent, and the VPN Server applies its own default for
           each of them.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            access_list_single: Access list (Must be a single item) (``AccessListSingle``, Array
                object)
                A mapping, or a sequence of mappings, built with :func:`access_rule`; keys may
                be wire names, names without the type suffix, or snake_case.

        Returns:
            RpcResult with the fields: ``HubName_str``, ``AccessListSingle``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("AddAccess", {
            "HubName_str": hub_name,
            "AccessListSingle": access_list_single,
        })

    def delete_access(
        self,
        hub_name: str,
        id: int,
    ) -> RpcResult:
        """DeleteAccess -- Delete Rule from Access List.

        Delete Rule from Access List. Use this to specify a packet filter rule registered on the
        access list of the currently managed Virtual Hub and delete it. To delete a rule, you
        must specify that rule's ID. You can display the ID by using the EnumAccess API. If you
        wish not to delete the rule but to only temporarily disable it, use the SetAccessList
        API to set the rule status to disable. This API cannot be invoked on VPN Bridge. You
        cannot execute this API for Virtual Hubs of VPN Servers operating as a member server on
        a cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            id: ID (``Id_u32``, number (uint32))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Id_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("DeleteAccess", {
            "HubName_str": hub_name,
            "Id_u32": id,
        })

    def enum_access(
        self,
        hub_name: str,
    ) -> RpcResult:
        """EnumAccess -- Get Access List Rule List.

        Get Access List Rule List. Use this to get a list of packet filter rules that are
        registered on access list of the currently managed Virtual Hub. The access list is a set
        of packet file rules that are applied to packets that flow through the Virtual Hub. You
        can register multiple rules in an access list and you can also define a priority for
        each rule. All packets are checked for the conditions specified by the rules registered
        in the access list and based on the operation that is stipulated by the first matching
        rule, they either pass or are discarded. Packets that do not match any rule are
        implicitly allowed to pass. This API cannot be invoked on VPN Bridge. You cannot execute
        this API for Virtual Hubs of VPN Servers operating as a member server on a cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``AccessList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumAccess", {
            "HubName_str": hub_name,
        })

    def set_access_list(
        self,
        hub_name: str,
        access_list: Union[Mapping[str, Any], Sequence[Mapping[str, Any]]],
    ) -> RpcResult:
        """SetAccessList -- Replace all access lists on a single bulk API call.

        Replace all access lists on a single bulk API call. This API removes all existing access
        list rules on the Virtual Hub, and replace them by new access list rules specified by
        the parameter.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            access_list: Access list (``AccessList``, Array object)
                A mapping, or a sequence of mappings, built with :func:`access_rule`; keys may
                be wire names, names without the type suffix, or snake_case.

        Returns:
            RpcResult with the fields: ``HubName_str``, ``AccessList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetAccessList", {
            "HubName_str": hub_name,
            "AccessList": access_list,
        })

    def create_user(
        self,
        hub_name: str,
        name: str,
        *,
        realname: Optional[str] = None,
        note: Optional[str] = None,
        expire_time: Optional[Union[datetime.datetime, str, int, float]] = None,
        auth_type: Optional[Union[int, "UserAuthType"]] = None,
        auth_password: Optional[str] = None,
        user_x: Optional[Union[bytes, bytearray, str]] = None,
        serial: Optional[Union[bytes, bytearray, str]] = None,
        common_name: Optional[str] = None,
        radius_username: Optional[str] = None,
        nt_username: Optional[str] = None,
        use_policy: Optional[bool] = None,
        policy_access: Optional[bool] = None,
        policy_dhcp_filter: Optional[bool] = None,
        policy_dhcp_no_server: Optional[bool] = None,
        policy_dhcp_force: Optional[bool] = None,
        policy_no_bridge: Optional[bool] = None,
        policy_no_routing: Optional[bool] = None,
        policy_check_mac: Optional[bool] = None,
        policy_check_ip: Optional[bool] = None,
        policy_arp_dhcp_only: Optional[bool] = None,
        policy_privacy_filter: Optional[bool] = None,
        policy_no_server: Optional[bool] = None,
        policy_no_broadcast_limiter: Optional[bool] = None,
        policy_monitor_port: Optional[bool] = None,
        policy_max_connection: Optional[int] = None,
        policy_time_out: Optional[int] = None,
        policy_max_mac: Optional[int] = None,
        policy_max_ip: Optional[int] = None,
        policy_max_upload: Optional[int] = None,
        policy_max_download: Optional[int] = None,
        policy_fix_password: Optional[bool] = None,
        policy_multi_logins: Optional[int] = None,
        policy_no_qos: Optional[bool] = None,
        policy_rs_and_ra_filter: Optional[bool] = None,
        policy_ra_filter: Optional[bool] = None,
        policy_dhcpv6_filter: Optional[bool] = None,
        policy_dhcpv6_no_server: Optional[bool] = None,
        policy_no_routing_v6: Optional[bool] = None,
        policy_check_ipv6: Optional[bool] = None,
        policy_no_server_v6: Optional[bool] = None,
        policy_max_ipv6: Optional[int] = None,
        policy_no_save_password: Optional[bool] = None,
        policy_auto_disconnect: Optional[int] = None,
        policy_filter_ipv4: Optional[bool] = None,
        policy_filter_ipv6: Optional[bool] = None,
        policy_filter_non_ip: Optional[bool] = None,
        policy_no_ipv6_default_router_in_ra: Optional[bool] = None,
        policy_no_ipv6_default_router_in_ra_when_ipv6: Optional[bool] = None,
        policy_vlan_id: Optional[int] = None,
        policy_ver3: Optional[bool] = None,
    ) -> RpcResult:
        """CreateUser -- Create a user.

        Create a user. Use this to create a new user in the security account database of the
        currently managed Virtual Hub. By creating a user, the VPN Client can connect to the
        Virtual Hub by using the authentication information of that user. Note that a user whose
        user name has been created as "" (a single asterisk character) will automatically be
        registered as a RADIUS authentication user. For cases where there are users with "" as
        the name, when a user, whose user name that has been provided when a client connected to
        a VPN Server does not match existing user names, is able to be authenticated by a RADIUS
        server or NT domain controller by inputting a user name and password, the authentication
        settings and security policy settings will follow the setting for the user "*". To
        change the user information of a user that has been created, use the SetUser API. This
        API cannot be invoked on VPN Bridge. You cannot execute this API for Virtual Hubs of VPN
        Servers operating as a member server on a cluster.

        .. note::
           Fields left at ``None`` are not sent, and the VPN Server applies its own default for
           each of them.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            name: Specify the user name of the user (``Name_str``, string (ASCII))
            realname: Optional real name (full name) of the user, allow using any Unicode
                characters (``Realname_utf``, string (UTF8))
            note: Optional User Description (``Note_utf``, string (UTF8))
            expire_time: Expiration date and time (``ExpireTime_dt``, Date)
            auth_type: Authentication method of the user (``AuthType_u32``, number (enum))
                Accepts an :class:`UserAuthType` member or its integer value: 0 = Anonymous
                authentication; 1 = Password authentication; 2 = User certificate
                authentication; 3 = Root certificate which is issued by trusted Certificate
                Authority; 4 = Radius authentication; 5 = Windows NT authentication.
            auth_password: User password, valid only if AuthType_u32 == Password(1). Valid only
                to create or set operations. (``Auth_Password_str``, string (ASCII))
            user_x: User certificate, valid only if AuthType_u32 == UserCert(2). (``UserX_bin``,
                string (Base64 binary))
            serial: Certificate Serial Number, optional, valid only if AuthType_u32 ==
                RootCert(3). (``Serial_bin``, string (Base64 binary))
            common_name: Certificate Common Name, optional, valid only if AuthType_u32 ==
                RootCert(3). (``CommonName_utf``, string (UTF8))
            radius_username: Username in RADIUS server, optional, valid only if AuthType_u32 ==
                Radius(4). (``RadiusUsername_utf``, string (UTF8))
            nt_username: Username in NT Domain server, optional, valid only if AuthType_u32 ==
                NT(5). (``NtUsername_utf``, string (UTF8))
            use_policy: The flag whether to use security policy (``UsePolicy_bool``, boolean)
            policy_access: Security policy: Allow Access. The users, which this policy value is
                true, have permission to make VPN connection to VPN Server. (``policy:Access_bool``,
                boolean)
            policy_dhcp_filter: Security policy: Filter DHCP Packets (IPv4). All IPv4 DHCP
                packets in sessions defined this policy will be filtered.
                (``policy:DHCPFilter_bool``, boolean)
            policy_dhcp_no_server: Security policy: Disallow DHCP Server Operation (IPv4).
                Computers connected to sessions that have this policy setting will not be allowed to
                become a DHCP server and distribute IPv4 addresses to DHCP clients.
                (``policy:DHCPNoServer_bool``, boolean)
            policy_dhcp_force: Security policy: Enforce DHCP Allocated IP Addresses (IPv4).
                Computers in sessions that have this policy setting will only be able to use IPv4
                addresses allocated by a DHCP server on the virtual network side.
                (``policy:DHCPForce_bool``, boolean)
            policy_no_bridge: Security policy: Deny Bridge Operation. Bridge-mode connections
                are denied for user sessions that have this policy setting. Even in cases when the
                Ethernet Bridge is configured in the client side, communication will not be
                possible. (``policy:NoBridge_bool``, boolean)
            policy_no_routing: Security policy: Deny Routing Operation (IPv4). IPv4 routing will
                be denied for sessions that have this policy setting. Even in the case where the IP
                router is operating on the user client side, communication will not be possible.
                (``policy:NoRouting_bool``, boolean)
            policy_check_mac: Security policy: Deny MAC Addresses Duplication. The use of
                duplicating MAC addresses that are in use by computers of different sessions cannot
                be used by sessions with this policy setting. (``policy:CheckMac_bool``, boolean)
            policy_check_ip: Security policy: Deny IP Address Duplication (IPv4). The use of
                duplicating IPv4 addresses that are in use by computers of different sessions cannot
                be used by sessions with this policy setting. (``policy:CheckIP_bool``, boolean)
            policy_arp_dhcp_only: Security policy: Deny Non-ARP / Non-DHCP / Non-ICMPv6
                broadcasts. The sending or receiving of broadcast packets that are not ARP protocol,
                DHCP protocol, nor ICMPv6 on the virtual network will not be allowed for sessions
                with this policy setting. (``policy:ArpDhcpOnly_bool``, boolean)
            policy_privacy_filter: Security policy: Privacy Filter Mode. All direct
                communication between sessions with the privacy filter mode policy setting will be
                filtered. (``policy:PrivacyFilter_bool``, boolean)
            policy_no_server: Security policy: Deny Operation as TCP/IP Server (IPv4). Computers
                of sessions with this policy setting can't listen and accept TCP/IP connections in
                IPv4. (``policy:NoServer_bool``, boolean)
            policy_no_broadcast_limiter: Security policy: Unlimited Number of Broadcasts. If a
                computer of a session with this policy setting sends broadcast packets of a number
                unusually larger than what would be considered normal on the virtual network, there
                will be no automatic limiting. (``policy:NoBroadcastLimiter_bool``, boolean)
            policy_monitor_port: Security policy: Allow Monitoring Mode. Users with this policy
                setting will be granted to connect to the Virtual Hub in Monitoring Mode. Sessions
                in Monitoring Mode are able to monitor (tap) all packets flowing through the Virtual
                Hub. (``policy:MonitorPort_bool``, boolean)
            policy_max_connection: Security policy: Maximum Number of TCP Connections. For
                sessions with this policy setting, this sets the maximum number of physical TCP
                connections consists in a physical VPN session. (``policy:MaxConnection_u32``,
                number (uint32))
            policy_time_out: Security policy: Time-out Period. For sessions with this policy
                setting, this sets, in seconds, the time-out period to wait before disconnecting a
                session when communication trouble occurs between the VPN Client / VPN Server.
                (``policy:TimeOut_u32``, number (uint32))
            policy_max_mac: Security policy: Maximum Number of MAC Addresses. For sessions with
                this policy setting, this limits the number of MAC addresses per session.
                (``policy:MaxMac_u32``, number (uint32))
            policy_max_ip: Security policy: Maximum Number of IP Addresses (IPv4). For sessions
                with this policy setting, this specifies the number of IPv4 addresses that can be
                registered for a single session. (``policy:MaxIP_u32``, number (uint32))
            policy_max_upload: Security policy: Upload Bandwidth. For sessions with this policy
                setting, this limits the traffic bandwidth that is in the inwards direction from
                outside to inside the Virtual Hub. (``policy:MaxUpload_u32``, number (uint32))
            policy_max_download: Security policy: Download Bandwidth. For sessions with this
                policy setting, this limits the traffic bandwidth that is in the outwards direction
                from inside the Virtual Hub to outside the Virtual Hub. (``policy:MaxDownload_u32``,
                number (uint32))
            policy_fix_password: Security policy: Deny Changing Password. The users which use
                password authentication with this policy setting are not allowed to change their own
                password from the VPN Client Manager or similar. (``policy:FixPassword_bool``,
                boolean)
            policy_multi_logins: Security policy: Maximum Number of Multiple Logins. Users with
                this policy setting are unable to have more than this number of concurrent logins.
                Bridge Mode sessions are not subjects to this policy. (``policy:MultiLogins_u32``,
                number (uint32))
            policy_no_qos: Security policy: Deny VoIP / QoS Function. Users with this security
                policy are unable to use VoIP / QoS functions in VPN connection sessions.
                (``policy:NoQoS_bool``, boolean)
            policy_rs_and_ra_filter: Security policy: Filter RS / RA Packets (IPv6). All ICMPv6
                packets which the message-type is 133 (Router Solicitation) or 134 (Router
                Advertisement) in sessions defined this policy will be filtered. As a result, an
                IPv6 client will be unable to use IPv6 address prefix auto detection and IPv6
                default gateway auto detection. (``policy:RSandRAFilter_bool``, boolean)
            policy_ra_filter: Security policy: Filter RA Packets (IPv6). All ICMPv6 packets
                which the message-type is 134 (Router Advertisement) in sessions defined this policy
                will be filtered. As a result, a malicious users will be unable to spread illegal
                IPv6 prefix or default gateway advertisements on the network.
                (``policy:RAFilter_bool``, boolean)
            policy_dhcpv6_filter: Security policy: Filter DHCP Packets (IPv6). All IPv6 DHCP
                packets in sessions defined this policy will be filtered.
                (``policy:DHCPv6Filter_bool``, boolean)
            policy_dhcpv6_no_server: Security policy: Disallow DHCP Server Operation (IPv6).
                Computers connected to sessions that have this policy setting will not be allowed to
                become a DHCP server and distribute IPv6 addresses to DHCP clients.
                (``policy:DHCPv6NoServer_bool``, boolean)
            policy_no_routing_v6: Security policy: Deny Routing Operation (IPv6). IPv6 routing
                will be denied for sessions that have this policy setting. Even in the case where
                the IP router is operating on the user client side, communication will not be
                possible. (``policy:NoRoutingV6_bool``, boolean)
            policy_check_ipv6: Security policy: Deny IP Address Duplication (IPv6). The use of
                duplicating IPv6 addresses that are in use by computers of different sessions cannot
                be used by sessions with this policy setting. (``policy:CheckIPv6_bool``, boolean)
            policy_no_server_v6: Security policy: Deny Operation as TCP/IP Server (IPv6).
                Computers of sessions with this policy setting can't listen and accept TCP/IP
                connections in IPv6. (``policy:NoServerV6_bool``, boolean)
            policy_max_ipv6: Security policy: Maximum Number of IP Addresses (IPv6). For
                sessions with this policy setting, this specifies the number of IPv6 addresses that
                can be registered for a single session. (``policy:MaxIPv6_u32``, number (uint32))
            policy_no_save_password: Security policy: Disallow Password Save in VPN Client. For
                users with this policy setting, when the user is using standard password
                authentication, the user will be unable to save the password in VPN Client. The user
                will be required to input passwords for every time to connect a VPN. This will
                improve the security. If this policy is enabled, VPN Client Version 2.0 will be
                denied to access. (``policy:NoSavePassword_bool``, boolean)
            policy_auto_disconnect: Security policy: VPN Client Automatic Disconnect. For users
                with this policy setting, a user's VPN session will be disconnected automatically
                after the specific period will elapse. In this case no automatic re-connection will
                be performed. This can prevent a lot of inactive VPN Sessions. If this policy is
                enabled, VPN Client Version 2.0 will be denied to access.
                (``policy:AutoDisconnect_u32``, number (uint32))
            policy_filter_ipv4: Security policy: Filter All IPv4 Packets. All IPv4 and ARP
                packets in sessions defined this policy will be filtered.
                (``policy:FilterIPv4_bool``, boolean)
            policy_filter_ipv6: Security policy: Filter All IPv6 Packets. All IPv6 packets in
                sessions defined this policy will be filtered. (``policy:FilterIPv6_bool``, boolean)
            policy_filter_non_ip: Security policy: Filter All Non-IP Packets. All non-IP packets
                in sessions defined this policy will be filtered. "Non-IP packet" mean a packet
                which is not IPv4, ARP nor IPv6. Any tagged-VLAN packets via the Virtual Hub will be
                regarded as non-IP packets. (``policy:FilterNonIP_bool``, boolean)
            policy_no_ipv6_default_router_in_ra: Security policy: No Default-Router on IPv6 RA.
                In all VPN Sessions defines this policy, any IPv6 RA (Router Advertisement) packet
                with non-zero value in the router-lifetime will set to zero-value. This is effective
                to avoid the horrible behavior from the IPv6 routing confusion which is caused by
                the VPN client's attempts to use the remote-side IPv6 router as its local IPv6
                router. (``policy:NoIPv6DefaultRouterInRA_bool``, boolean)
            policy_no_ipv6_default_router_in_ra_when_ipv6: Security policy: No Default-Router on
                IPv6 RA (physical IPv6). In all VPN Sessions defines this policy (only when the
                physical communication protocol between VPN Client / VPN Bridge and VPN Server is
                IPv6), any IPv6 RA (Router Advertisement) packet with non-zero value in the
                router-lifetime will set to zero-value. This is effective to avoid the horrible
                behavior from the IPv6 routing confusion which is caused by the VPN client's
                attempts to use the remote-side IPv6 router as its local IPv6 router.
                (``policy:NoIPv6DefaultRouterInRAWhenIPv6_bool``, boolean)
            policy_vlan_id: Security policy: VLAN ID (IEEE802.1Q). You can specify the VLAN ID
                on the security policy. All VPN Sessions defines this policy, all Ethernet packets
                toward the Virtual Hub from the user will be inserted a VLAN tag (IEEE 802.1Q) with
                the VLAN ID. The user can also receive only packets with a VLAN tag which has the
                same VLAN ID. (Receiving process removes the VLAN tag automatically.) Any Ethernet
                packets with any other VLAN IDs or non-VLAN packets will not be received. All VPN
                Sessions without this policy definition can send / receive any kinds of Ethernet
                packets regardless of VLAN tags, and VLAN tags are not inserted or removed
                automatically. Any tagged-VLAN packets via the Virtual Hub will be regarded as
                non-IP packets. Therefore, tagged-VLAN packets are not subjects for IPv4 / IPv6
                security policies, access lists nor other IPv4 / IPv6 specific deep processing.
                (``policy:VLanId_u32``, number (uint32))
            policy_ver3: Security policy: Whether version 3.0 (must be true)
                (``policy:Ver3_bool``, boolean)

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Name_str``, ``GroupName_str``,
            ``Realname_utf``, ``Note_utf``, ``CreatedTime_dt``, ``UpdatedTime_dt``,
            ``ExpireTime_dt``, ``AuthType_u32``, ``Auth_Password_str``, ``UserX_bin``,
            ``Serial_bin``, ``CommonName_utf``, ``RadiusUsername_utf``, ``NtUsername_utf``,
            ``NumLogin_u32``, ``Recv.BroadcastBytes_u64``, ``Recv.BroadcastCount_u64``,
            ``Recv.UnicastBytes_u64``, ``Recv.UnicastCount_u64``, ``Send.BroadcastBytes_u64``,
            ``Send.BroadcastCount_u64``, ``Send.UnicastBytes_u64``, ``Send.UnicastCount_u64``,
            ``UsePolicy_bool``, ``policy:Access_bool``, ``policy:DHCPFilter_bool``,
            ``policy:DHCPNoServer_bool``, ``policy:DHCPForce_bool``, ``policy:NoBridge_bool``,
            ``policy:NoRouting_bool``, ``policy:CheckMac_bool``, ``policy:CheckIP_bool``,
            ``policy:ArpDhcpOnly_bool``, ``policy:PrivacyFilter_bool``,
            ``policy:NoServer_bool``, ``policy:NoBroadcastLimiter_bool``,
            ``policy:MonitorPort_bool``, ``policy:MaxConnection_u32``, ``policy:TimeOut_u32``,
            ``policy:MaxMac_u32``, ``policy:MaxIP_u32``, ``policy:MaxUpload_u32``,
            ``policy:MaxDownload_u32``, ``policy:FixPassword_bool``, ``policy:MultiLogins_u32``,
            ``policy:NoQoS_bool``, ``policy:RSandRAFilter_bool``, ``policy:RAFilter_bool``,
            ``policy:DHCPv6Filter_bool``, ``policy:DHCPv6NoServer_bool``,
            ``policy:NoRoutingV6_bool``, ``policy:CheckIPv6_bool``, ``policy:NoServerV6_bool``,
            ``policy:MaxIPv6_u32``, ``policy:NoSavePassword_bool``,
            ``policy:AutoDisconnect_u32``, ``policy:FilterIPv4_bool``,
            ``policy:FilterIPv6_bool``, ``policy:FilterNonIP_bool``,
            ``policy:NoIPv6DefaultRouterInRA_bool``,
            ``policy:NoIPv6DefaultRouterInRAWhenIPv6_bool``, ``policy:VLanId_u32``,
            ``policy:Ver3_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("CreateUser", {
            "HubName_str": hub_name,
            "Name_str": name,
            "Realname_utf": realname,
            "Note_utf": note,
            "ExpireTime_dt": expire_time,
            "AuthType_u32": auth_type,
            "Auth_Password_str": auth_password,
            "UserX_bin": user_x,
            "Serial_bin": serial,
            "CommonName_utf": common_name,
            "RadiusUsername_utf": radius_username,
            "NtUsername_utf": nt_username,
            "UsePolicy_bool": use_policy,
            "policy:Access_bool": policy_access,
            "policy:DHCPFilter_bool": policy_dhcp_filter,
            "policy:DHCPNoServer_bool": policy_dhcp_no_server,
            "policy:DHCPForce_bool": policy_dhcp_force,
            "policy:NoBridge_bool": policy_no_bridge,
            "policy:NoRouting_bool": policy_no_routing,
            "policy:CheckMac_bool": policy_check_mac,
            "policy:CheckIP_bool": policy_check_ip,
            "policy:ArpDhcpOnly_bool": policy_arp_dhcp_only,
            "policy:PrivacyFilter_bool": policy_privacy_filter,
            "policy:NoServer_bool": policy_no_server,
            "policy:NoBroadcastLimiter_bool": policy_no_broadcast_limiter,
            "policy:MonitorPort_bool": policy_monitor_port,
            "policy:MaxConnection_u32": policy_max_connection,
            "policy:TimeOut_u32": policy_time_out,
            "policy:MaxMac_u32": policy_max_mac,
            "policy:MaxIP_u32": policy_max_ip,
            "policy:MaxUpload_u32": policy_max_upload,
            "policy:MaxDownload_u32": policy_max_download,
            "policy:FixPassword_bool": policy_fix_password,
            "policy:MultiLogins_u32": policy_multi_logins,
            "policy:NoQoS_bool": policy_no_qos,
            "policy:RSandRAFilter_bool": policy_rs_and_ra_filter,
            "policy:RAFilter_bool": policy_ra_filter,
            "policy:DHCPv6Filter_bool": policy_dhcpv6_filter,
            "policy:DHCPv6NoServer_bool": policy_dhcpv6_no_server,
            "policy:NoRoutingV6_bool": policy_no_routing_v6,
            "policy:CheckIPv6_bool": policy_check_ipv6,
            "policy:NoServerV6_bool": policy_no_server_v6,
            "policy:MaxIPv6_u32": policy_max_ipv6,
            "policy:NoSavePassword_bool": policy_no_save_password,
            "policy:AutoDisconnect_u32": policy_auto_disconnect,
            "policy:FilterIPv4_bool": policy_filter_ipv4,
            "policy:FilterIPv6_bool": policy_filter_ipv6,
            "policy:FilterNonIP_bool": policy_filter_non_ip,
            "policy:NoIPv6DefaultRouterInRA_bool": policy_no_ipv6_default_router_in_ra,
            "policy:NoIPv6DefaultRouterInRAWhenIPv6_bool": policy_no_ipv6_default_router_in_ra_when_ipv6,
            "policy:VLanId_u32": policy_vlan_id,
            "policy:Ver3_bool": policy_ver3,
        })

    def set_user(
        self,
        hub_name: str,
        name: str,
        *,
        group_name: Optional[str] = None,
        realname: Optional[str] = None,
        note: Optional[str] = None,
        expire_time: Optional[Union[datetime.datetime, str, int, float]] = None,
        auth_type: Optional[Union[int, "UserAuthType"]] = None,
        auth_password: Optional[str] = None,
        user_x: Optional[Union[bytes, bytearray, str]] = None,
        serial: Optional[Union[bytes, bytearray, str]] = None,
        common_name: Optional[str] = None,
        radius_username: Optional[str] = None,
        nt_username: Optional[str] = None,
        use_policy: Optional[bool] = None,
        policy_access: Optional[bool] = None,
        policy_dhcp_filter: Optional[bool] = None,
        policy_dhcp_no_server: Optional[bool] = None,
        policy_dhcp_force: Optional[bool] = None,
        policy_no_bridge: Optional[bool] = None,
        policy_no_routing: Optional[bool] = None,
        policy_check_mac: Optional[bool] = None,
        policy_check_ip: Optional[bool] = None,
        policy_arp_dhcp_only: Optional[bool] = None,
        policy_privacy_filter: Optional[bool] = None,
        policy_no_server: Optional[bool] = None,
        policy_no_broadcast_limiter: Optional[bool] = None,
        policy_monitor_port: Optional[bool] = None,
        policy_max_connection: Optional[int] = None,
        policy_time_out: Optional[int] = None,
        policy_max_mac: Optional[int] = None,
        policy_max_ip: Optional[int] = None,
        policy_max_upload: Optional[int] = None,
        policy_max_download: Optional[int] = None,
        policy_fix_password: Optional[bool] = None,
        policy_multi_logins: Optional[int] = None,
        policy_no_qos: Optional[bool] = None,
        policy_rs_and_ra_filter: Optional[bool] = None,
        policy_ra_filter: Optional[bool] = None,
        policy_dhcpv6_filter: Optional[bool] = None,
        policy_dhcpv6_no_server: Optional[bool] = None,
        policy_no_routing_v6: Optional[bool] = None,
        policy_check_ipv6: Optional[bool] = None,
        policy_no_server_v6: Optional[bool] = None,
        policy_max_ipv6: Optional[int] = None,
        policy_no_save_password: Optional[bool] = None,
        policy_auto_disconnect: Optional[int] = None,
        policy_filter_ipv4: Optional[bool] = None,
        policy_filter_ipv6: Optional[bool] = None,
        policy_filter_non_ip: Optional[bool] = None,
        policy_no_ipv6_default_router_in_ra: Optional[bool] = None,
        policy_no_ipv6_default_router_in_ra_when_ipv6: Optional[bool] = None,
        policy_vlan_id: Optional[int] = None,
        policy_ver3: Optional[bool] = None,
    ) -> RpcResult:
        """SetUser -- Change User Settings.

        Change User Settings. Use this to change user settings that is registered on the
        security account database of the currently managed Virtual Hub. The user settings that
        can be changed using this API are the three items that are specified when a new user is
        created using the CreateUser API: Group Name, Full Name, and Description. To get the
        list of currently registered users, use the EnumUser API. This API cannot be invoked on
        VPN Bridge. You cannot execute this API for Virtual Hubs of VPN Servers operating as a
        member server on a cluster.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            name: Specify the user name of the user (``Name_str``, string (ASCII))
            group_name: Assigned group name for the user (``GroupName_str``, string (ASCII))
            realname: Optional real name (full name) of the user, allow using any Unicode
                characters (``Realname_utf``, string (UTF8))
            note: Optional User Description (``Note_utf``, string (UTF8))
            expire_time: Expiration date and time (``ExpireTime_dt``, Date)
            auth_type: Authentication method of the user (``AuthType_u32``, number (enum))
                Accepts an :class:`UserAuthType` member or its integer value: 0 = Anonymous
                authentication; 1 = Password authentication; 2 = User certificate
                authentication; 3 = Root certificate which is issued by trusted Certificate
                Authority; 4 = Radius authentication; 5 = Windows NT authentication.
            auth_password: User password, valid only if AuthType_u32 == Password(1). Valid only
                to create or set operations. (``Auth_Password_str``, string (ASCII))
            user_x: User certificate, valid only if AuthType_u32 == UserCert(2). (``UserX_bin``,
                string (Base64 binary))
            serial: Certificate Serial Number, optional, valid only if AuthType_u32 ==
                RootCert(3). (``Serial_bin``, string (Base64 binary))
            common_name: Certificate Common Name, optional, valid only if AuthType_u32 ==
                RootCert(3). (``CommonName_utf``, string (UTF8))
            radius_username: Username in RADIUS server, optional, valid only if AuthType_u32 ==
                Radius(4). (``RadiusUsername_utf``, string (UTF8))
            nt_username: Username in NT Domain server, optional, valid only if AuthType_u32 ==
                NT(5). (``NtUsername_utf``, string (UTF8))
            use_policy: The flag whether to use security policy (``UsePolicy_bool``, boolean)
            policy_access: Security policy: Allow Access. The users, which this policy value is
                true, have permission to make VPN connection to VPN Server. (``policy:Access_bool``,
                boolean)
            policy_dhcp_filter: Security policy: Filter DHCP Packets (IPv4). All IPv4 DHCP
                packets in sessions defined this policy will be filtered.
                (``policy:DHCPFilter_bool``, boolean)
            policy_dhcp_no_server: Security policy: Disallow DHCP Server Operation (IPv4).
                Computers connected to sessions that have this policy setting will not be allowed to
                become a DHCP server and distribute IPv4 addresses to DHCP clients.
                (``policy:DHCPNoServer_bool``, boolean)
            policy_dhcp_force: Security policy: Enforce DHCP Allocated IP Addresses (IPv4).
                Computers in sessions that have this policy setting will only be able to use IPv4
                addresses allocated by a DHCP server on the virtual network side.
                (``policy:DHCPForce_bool``, boolean)
            policy_no_bridge: Security policy: Deny Bridge Operation. Bridge-mode connections
                are denied for user sessions that have this policy setting. Even in cases when the
                Ethernet Bridge is configured in the client side, communication will not be
                possible. (``policy:NoBridge_bool``, boolean)
            policy_no_routing: Security policy: Deny Routing Operation (IPv4). IPv4 routing will
                be denied for sessions that have this policy setting. Even in the case where the IP
                router is operating on the user client side, communication will not be possible.
                (``policy:NoRouting_bool``, boolean)
            policy_check_mac: Security policy: Deny MAC Addresses Duplication. The use of
                duplicating MAC addresses that are in use by computers of different sessions cannot
                be used by sessions with this policy setting. (``policy:CheckMac_bool``, boolean)
            policy_check_ip: Security policy: Deny IP Address Duplication (IPv4). The use of
                duplicating IPv4 addresses that are in use by computers of different sessions cannot
                be used by sessions with this policy setting. (``policy:CheckIP_bool``, boolean)
            policy_arp_dhcp_only: Security policy: Deny Non-ARP / Non-DHCP / Non-ICMPv6
                broadcasts. The sending or receiving of broadcast packets that are not ARP protocol,
                DHCP protocol, nor ICMPv6 on the virtual network will not be allowed for sessions
                with this policy setting. (``policy:ArpDhcpOnly_bool``, boolean)
            policy_privacy_filter: Security policy: Privacy Filter Mode. All direct
                communication between sessions with the privacy filter mode policy setting will be
                filtered. (``policy:PrivacyFilter_bool``, boolean)
            policy_no_server: Security policy: Deny Operation as TCP/IP Server (IPv4). Computers
                of sessions with this policy setting can't listen and accept TCP/IP connections in
                IPv4. (``policy:NoServer_bool``, boolean)
            policy_no_broadcast_limiter: Security policy: Unlimited Number of Broadcasts. If a
                computer of a session with this policy setting sends broadcast packets of a number
                unusually larger than what would be considered normal on the virtual network, there
                will be no automatic limiting. (``policy:NoBroadcastLimiter_bool``, boolean)
            policy_monitor_port: Security policy: Allow Monitoring Mode. Users with this policy
                setting will be granted to connect to the Virtual Hub in Monitoring Mode. Sessions
                in Monitoring Mode are able to monitor (tap) all packets flowing through the Virtual
                Hub. (``policy:MonitorPort_bool``, boolean)
            policy_max_connection: Security policy: Maximum Number of TCP Connections. For
                sessions with this policy setting, this sets the maximum number of physical TCP
                connections consists in a physical VPN session. (``policy:MaxConnection_u32``,
                number (uint32))
            policy_time_out: Security policy: Time-out Period. For sessions with this policy
                setting, this sets, in seconds, the time-out period to wait before disconnecting a
                session when communication trouble occurs between the VPN Client / VPN Server.
                (``policy:TimeOut_u32``, number (uint32))
            policy_max_mac: Security policy: Maximum Number of MAC Addresses. For sessions with
                this policy setting, this limits the number of MAC addresses per session.
                (``policy:MaxMac_u32``, number (uint32))
            policy_max_ip: Security policy: Maximum Number of IP Addresses (IPv4). For sessions
                with this policy setting, this specifies the number of IPv4 addresses that can be
                registered for a single session. (``policy:MaxIP_u32``, number (uint32))
            policy_max_upload: Security policy: Upload Bandwidth. For sessions with this policy
                setting, this limits the traffic bandwidth that is in the inwards direction from
                outside to inside the Virtual Hub. (``policy:MaxUpload_u32``, number (uint32))
            policy_max_download: Security policy: Download Bandwidth. For sessions with this
                policy setting, this limits the traffic bandwidth that is in the outwards direction
                from inside the Virtual Hub to outside the Virtual Hub. (``policy:MaxDownload_u32``,
                number (uint32))
            policy_fix_password: Security policy: Deny Changing Password. The users which use
                password authentication with this policy setting are not allowed to change their own
                password from the VPN Client Manager or similar. (``policy:FixPassword_bool``,
                boolean)
            policy_multi_logins: Security policy: Maximum Number of Multiple Logins. Users with
                this policy setting are unable to have more than this number of concurrent logins.
                Bridge Mode sessions are not subjects to this policy. (``policy:MultiLogins_u32``,
                number (uint32))
            policy_no_qos: Security policy: Deny VoIP / QoS Function. Users with this security
                policy are unable to use VoIP / QoS functions in VPN connection sessions.
                (``policy:NoQoS_bool``, boolean)
            policy_rs_and_ra_filter: Security policy: Filter RS / RA Packets (IPv6). All ICMPv6
                packets which the message-type is 133 (Router Solicitation) or 134 (Router
                Advertisement) in sessions defined this policy will be filtered. As a result, an
                IPv6 client will be unable to use IPv6 address prefix auto detection and IPv6
                default gateway auto detection. (``policy:RSandRAFilter_bool``, boolean)
            policy_ra_filter: Security policy: Filter RA Packets (IPv6). All ICMPv6 packets
                which the message-type is 134 (Router Advertisement) in sessions defined this policy
                will be filtered. As a result, a malicious users will be unable to spread illegal
                IPv6 prefix or default gateway advertisements on the network.
                (``policy:RAFilter_bool``, boolean)
            policy_dhcpv6_filter: Security policy: Filter DHCP Packets (IPv6). All IPv6 DHCP
                packets in sessions defined this policy will be filtered.
                (``policy:DHCPv6Filter_bool``, boolean)
            policy_dhcpv6_no_server: Security policy: Disallow DHCP Server Operation (IPv6).
                Computers connected to sessions that have this policy setting will not be allowed to
                become a DHCP server and distribute IPv6 addresses to DHCP clients.
                (``policy:DHCPv6NoServer_bool``, boolean)
            policy_no_routing_v6: Security policy: Deny Routing Operation (IPv6). IPv6 routing
                will be denied for sessions that have this policy setting. Even in the case where
                the IP router is operating on the user client side, communication will not be
                possible. (``policy:NoRoutingV6_bool``, boolean)
            policy_check_ipv6: Security policy: Deny IP Address Duplication (IPv6). The use of
                duplicating IPv6 addresses that are in use by computers of different sessions cannot
                be used by sessions with this policy setting. (``policy:CheckIPv6_bool``, boolean)
            policy_no_server_v6: Security policy: Deny Operation as TCP/IP Server (IPv6).
                Computers of sessions with this policy setting can't listen and accept TCP/IP
                connections in IPv6. (``policy:NoServerV6_bool``, boolean)
            policy_max_ipv6: Security policy: Maximum Number of IP Addresses (IPv6). For
                sessions with this policy setting, this specifies the number of IPv6 addresses that
                can be registered for a single session. (``policy:MaxIPv6_u32``, number (uint32))
            policy_no_save_password: Security policy: Disallow Password Save in VPN Client. For
                users with this policy setting, when the user is using standard password
                authentication, the user will be unable to save the password in VPN Client. The user
                will be required to input passwords for every time to connect a VPN. This will
                improve the security. If this policy is enabled, VPN Client Version 2.0 will be
                denied to access. (``policy:NoSavePassword_bool``, boolean)
            policy_auto_disconnect: Security policy: VPN Client Automatic Disconnect. For users
                with this policy setting, a user's VPN session will be disconnected automatically
                after the specific period will elapse. In this case no automatic re-connection will
                be performed. This can prevent a lot of inactive VPN Sessions. If this policy is
                enabled, VPN Client Version 2.0 will be denied to access.
                (``policy:AutoDisconnect_u32``, number (uint32))
            policy_filter_ipv4: Security policy: Filter All IPv4 Packets. All IPv4 and ARP
                packets in sessions defined this policy will be filtered.
                (``policy:FilterIPv4_bool``, boolean)
            policy_filter_ipv6: Security policy: Filter All IPv6 Packets. All IPv6 packets in
                sessions defined this policy will be filtered. (``policy:FilterIPv6_bool``, boolean)
            policy_filter_non_ip: Security policy: Filter All Non-IP Packets. All non-IP packets
                in sessions defined this policy will be filtered. "Non-IP packet" mean a packet
                which is not IPv4, ARP nor IPv6. Any tagged-VLAN packets via the Virtual Hub will be
                regarded as non-IP packets. (``policy:FilterNonIP_bool``, boolean)
            policy_no_ipv6_default_router_in_ra: Security policy: No Default-Router on IPv6 RA.
                In all VPN Sessions defines this policy, any IPv6 RA (Router Advertisement) packet
                with non-zero value in the router-lifetime will set to zero-value. This is effective
                to avoid the horrible behavior from the IPv6 routing confusion which is caused by
                the VPN client's attempts to use the remote-side IPv6 router as its local IPv6
                router. (``policy:NoIPv6DefaultRouterInRA_bool``, boolean)
            policy_no_ipv6_default_router_in_ra_when_ipv6: Security policy: No Default-Router on
                IPv6 RA (physical IPv6). In all VPN Sessions defines this policy (only when the
                physical communication protocol between VPN Client / VPN Bridge and VPN Server is
                IPv6), any IPv6 RA (Router Advertisement) packet with non-zero value in the
                router-lifetime will set to zero-value. This is effective to avoid the horrible
                behavior from the IPv6 routing confusion which is caused by the VPN client's
                attempts to use the remote-side IPv6 router as its local IPv6 router.
                (``policy:NoIPv6DefaultRouterInRAWhenIPv6_bool``, boolean)
            policy_vlan_id: Security policy: VLAN ID (IEEE802.1Q). You can specify the VLAN ID
                on the security policy. All VPN Sessions defines this policy, all Ethernet packets
                toward the Virtual Hub from the user will be inserted a VLAN tag (IEEE 802.1Q) with
                the VLAN ID. The user can also receive only packets with a VLAN tag which has the
                same VLAN ID. (Receiving process removes the VLAN tag automatically.) Any Ethernet
                packets with any other VLAN IDs or non-VLAN packets will not be received. All VPN
                Sessions without this policy definition can send / receive any kinds of Ethernet
                packets regardless of VLAN tags, and VLAN tags are not inserted or removed
                automatically. Any tagged-VLAN packets via the Virtual Hub will be regarded as
                non-IP packets. Therefore, tagged-VLAN packets are not subjects for IPv4 / IPv6
                security policies, access lists nor other IPv4 / IPv6 specific deep processing.
                (``policy:VLanId_u32``, number (uint32))
            policy_ver3: Security policy: Whether version 3.0 (must be true)
                (``policy:Ver3_bool``, boolean)

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Name_str``, ``GroupName_str``,
            ``Realname_utf``, ``Note_utf``, ``CreatedTime_dt``, ``UpdatedTime_dt``,
            ``ExpireTime_dt``, ``AuthType_u32``, ``Auth_Password_str``, ``UserX_bin``,
            ``Serial_bin``, ``CommonName_utf``, ``RadiusUsername_utf``, ``NtUsername_utf``,
            ``NumLogin_u32``, ``Recv.BroadcastBytes_u64``, ``Recv.BroadcastCount_u64``,
            ``Recv.UnicastBytes_u64``, ``Recv.UnicastCount_u64``, ``Send.BroadcastBytes_u64``,
            ``Send.BroadcastCount_u64``, ``Send.UnicastBytes_u64``, ``Send.UnicastCount_u64``,
            ``UsePolicy_bool``, ``policy:Access_bool``, ``policy:DHCPFilter_bool``,
            ``policy:DHCPNoServer_bool``, ``policy:DHCPForce_bool``, ``policy:NoBridge_bool``,
            ``policy:NoRouting_bool``, ``policy:CheckMac_bool``, ``policy:CheckIP_bool``,
            ``policy:ArpDhcpOnly_bool``, ``policy:PrivacyFilter_bool``,
            ``policy:NoServer_bool``, ``policy:NoBroadcastLimiter_bool``,
            ``policy:MonitorPort_bool``, ``policy:MaxConnection_u32``, ``policy:TimeOut_u32``,
            ``policy:MaxMac_u32``, ``policy:MaxIP_u32``, ``policy:MaxUpload_u32``,
            ``policy:MaxDownload_u32``, ``policy:FixPassword_bool``, ``policy:MultiLogins_u32``,
            ``policy:NoQoS_bool``, ``policy:RSandRAFilter_bool``, ``policy:RAFilter_bool``,
            ``policy:DHCPv6Filter_bool``, ``policy:DHCPv6NoServer_bool``,
            ``policy:NoRoutingV6_bool``, ``policy:CheckIPv6_bool``, ``policy:NoServerV6_bool``,
            ``policy:MaxIPv6_u32``, ``policy:NoSavePassword_bool``,
            ``policy:AutoDisconnect_u32``, ``policy:FilterIPv4_bool``,
            ``policy:FilterIPv6_bool``, ``policy:FilterNonIP_bool``,
            ``policy:NoIPv6DefaultRouterInRA_bool``,
            ``policy:NoIPv6DefaultRouterInRAWhenIPv6_bool``, ``policy:VLanId_u32``,
            ``policy:Ver3_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetUser", {
            "HubName_str": hub_name,
            "Name_str": name,
            "GroupName_str": group_name,
            "Realname_utf": realname,
            "Note_utf": note,
            "ExpireTime_dt": expire_time,
            "AuthType_u32": auth_type,
            "Auth_Password_str": auth_password,
            "UserX_bin": user_x,
            "Serial_bin": serial,
            "CommonName_utf": common_name,
            "RadiusUsername_utf": radius_username,
            "NtUsername_utf": nt_username,
            "UsePolicy_bool": use_policy,
            "policy:Access_bool": policy_access,
            "policy:DHCPFilter_bool": policy_dhcp_filter,
            "policy:DHCPNoServer_bool": policy_dhcp_no_server,
            "policy:DHCPForce_bool": policy_dhcp_force,
            "policy:NoBridge_bool": policy_no_bridge,
            "policy:NoRouting_bool": policy_no_routing,
            "policy:CheckMac_bool": policy_check_mac,
            "policy:CheckIP_bool": policy_check_ip,
            "policy:ArpDhcpOnly_bool": policy_arp_dhcp_only,
            "policy:PrivacyFilter_bool": policy_privacy_filter,
            "policy:NoServer_bool": policy_no_server,
            "policy:NoBroadcastLimiter_bool": policy_no_broadcast_limiter,
            "policy:MonitorPort_bool": policy_monitor_port,
            "policy:MaxConnection_u32": policy_max_connection,
            "policy:TimeOut_u32": policy_time_out,
            "policy:MaxMac_u32": policy_max_mac,
            "policy:MaxIP_u32": policy_max_ip,
            "policy:MaxUpload_u32": policy_max_upload,
            "policy:MaxDownload_u32": policy_max_download,
            "policy:FixPassword_bool": policy_fix_password,
            "policy:MultiLogins_u32": policy_multi_logins,
            "policy:NoQoS_bool": policy_no_qos,
            "policy:RSandRAFilter_bool": policy_rs_and_ra_filter,
            "policy:RAFilter_bool": policy_ra_filter,
            "policy:DHCPv6Filter_bool": policy_dhcpv6_filter,
            "policy:DHCPv6NoServer_bool": policy_dhcpv6_no_server,
            "policy:NoRoutingV6_bool": policy_no_routing_v6,
            "policy:CheckIPv6_bool": policy_check_ipv6,
            "policy:NoServerV6_bool": policy_no_server_v6,
            "policy:MaxIPv6_u32": policy_max_ipv6,
            "policy:NoSavePassword_bool": policy_no_save_password,
            "policy:AutoDisconnect_u32": policy_auto_disconnect,
            "policy:FilterIPv4_bool": policy_filter_ipv4,
            "policy:FilterIPv6_bool": policy_filter_ipv6,
            "policy:FilterNonIP_bool": policy_filter_non_ip,
            "policy:NoIPv6DefaultRouterInRA_bool": policy_no_ipv6_default_router_in_ra,
            "policy:NoIPv6DefaultRouterInRAWhenIPv6_bool": policy_no_ipv6_default_router_in_ra_when_ipv6,
            "policy:VLanId_u32": policy_vlan_id,
            "policy:Ver3_bool": policy_ver3,
        })

    def get_user(
        self,
        hub_name: str,
        name: str,
    ) -> RpcResult:
        """GetUser -- Get User Settings.

        Get User Settings. Use this to get user settings information that is registered on the
        security account database of the currently managed Virtual Hub. The information that you
        can get using this API are User Name, Full Name, Group Name, Expiration Date, Security
        Policy, and Auth Type, as well as parameters that are specified as auth type attributes
        and the statistical data of that user. To get the list of currently registered users,
        use the EnumUser API. This API cannot be invoked on VPN Bridge. You cannot execute this
        API for Virtual Hubs of VPN Servers operating as a member server on a cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            name: Specify the user name of the user (``Name_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Name_str``, ``GroupName_str``,
            ``Realname_utf``, ``Note_utf``, ``CreatedTime_dt``, ``UpdatedTime_dt``,
            ``ExpireTime_dt``, ``AuthType_u32``, ``Auth_Password_str``, ``UserX_bin``,
            ``Serial_bin``, ``CommonName_utf``, ``RadiusUsername_utf``, ``NtUsername_utf``,
            ``NumLogin_u32``, ``Recv.BroadcastBytes_u64``, ``Recv.BroadcastCount_u64``,
            ``Recv.UnicastBytes_u64``, ``Recv.UnicastCount_u64``, ``Send.BroadcastBytes_u64``,
            ``Send.BroadcastCount_u64``, ``Send.UnicastBytes_u64``, ``Send.UnicastCount_u64``,
            ``UsePolicy_bool``, ``policy:Access_bool``, ``policy:DHCPFilter_bool``,
            ``policy:DHCPNoServer_bool``, ``policy:DHCPForce_bool``, ``policy:NoBridge_bool``,
            ``policy:NoRouting_bool``, ``policy:CheckMac_bool``, ``policy:CheckIP_bool``,
            ``policy:ArpDhcpOnly_bool``, ``policy:PrivacyFilter_bool``,
            ``policy:NoServer_bool``, ``policy:NoBroadcastLimiter_bool``,
            ``policy:MonitorPort_bool``, ``policy:MaxConnection_u32``, ``policy:TimeOut_u32``,
            ``policy:MaxMac_u32``, ``policy:MaxIP_u32``, ``policy:MaxUpload_u32``,
            ``policy:MaxDownload_u32``, ``policy:FixPassword_bool``, ``policy:MultiLogins_u32``,
            ``policy:NoQoS_bool``, ``policy:RSandRAFilter_bool``, ``policy:RAFilter_bool``,
            ``policy:DHCPv6Filter_bool``, ``policy:DHCPv6NoServer_bool``,
            ``policy:NoRoutingV6_bool``, ``policy:CheckIPv6_bool``, ``policy:NoServerV6_bool``,
            ``policy:MaxIPv6_u32``, ``policy:NoSavePassword_bool``,
            ``policy:AutoDisconnect_u32``, ``policy:FilterIPv4_bool``,
            ``policy:FilterIPv6_bool``, ``policy:FilterNonIP_bool``,
            ``policy:NoIPv6DefaultRouterInRA_bool``,
            ``policy:NoIPv6DefaultRouterInRAWhenIPv6_bool``, ``policy:VLanId_u32``,
            ``policy:Ver3_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetUser", {
            "HubName_str": hub_name,
            "Name_str": name,
        })

    def delete_user(
        self,
        hub_name: str,
        name: str,
    ) -> RpcResult:
        """DeleteUser -- Delete a user.

        Delete a user. Use this to delete a user that is registered on the security account
        database of the currently managed Virtual Hub. By deleting the user, that user will no
        long be able to connect to the Virtual Hub. You can use the SetUser API to set the
        user's security policy to deny access instead of deleting a user, set the user to be
        temporarily denied from logging in. To get the list of currently registered users, use
        the EnumUser API. This API cannot be invoked on VPN Bridge. You cannot execute this API
        for Virtual Hubs of VPN Servers operating as a member server on a cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            name: User or group name (``Name_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Name_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("DeleteUser", {
            "HubName_str": hub_name,
            "Name_str": name,
        })

    def enum_user(
        self,
        hub_name: str,
    ) -> RpcResult:
        """EnumUser -- Get List of Users.

        Get List of Users. Use this to get a list of users that are registered on the security
        account database of the currently managed Virtual Hub. This API cannot be invoked on VPN
        Bridge. You cannot execute this API for Virtual Hubs of VPN Servers operating as a
        member server on a cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``UserList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumUser", {
            "HubName_str": hub_name,
        })

    def create_group(
        self,
        hub_name: str,
        name: str,
        *,
        realname: Optional[str] = None,
        note: Optional[str] = None,
        use_policy: Optional[bool] = None,
        policy_access: Optional[bool] = None,
        policy_dhcp_filter: Optional[bool] = None,
        policy_dhcp_no_server: Optional[bool] = None,
        policy_dhcp_force: Optional[bool] = None,
        policy_no_bridge: Optional[bool] = None,
        policy_no_routing: Optional[bool] = None,
        policy_check_mac: Optional[bool] = None,
        policy_check_ip: Optional[bool] = None,
        policy_arp_dhcp_only: Optional[bool] = None,
        policy_privacy_filter: Optional[bool] = None,
        policy_no_server: Optional[bool] = None,
        policy_no_broadcast_limiter: Optional[bool] = None,
        policy_monitor_port: Optional[bool] = None,
        policy_max_connection: Optional[int] = None,
        policy_time_out: Optional[int] = None,
        policy_max_mac: Optional[int] = None,
        policy_max_ip: Optional[int] = None,
        policy_max_upload: Optional[int] = None,
        policy_max_download: Optional[int] = None,
        policy_fix_password: Optional[bool] = None,
        policy_multi_logins: Optional[int] = None,
        policy_no_qos: Optional[bool] = None,
        policy_rs_and_ra_filter: Optional[bool] = None,
        policy_ra_filter: Optional[bool] = None,
        policy_dhcpv6_filter: Optional[bool] = None,
        policy_dhcpv6_no_server: Optional[bool] = None,
        policy_no_routing_v6: Optional[bool] = None,
        policy_check_ipv6: Optional[bool] = None,
        policy_no_server_v6: Optional[bool] = None,
        policy_max_ipv6: Optional[int] = None,
        policy_no_save_password: Optional[bool] = None,
        policy_auto_disconnect: Optional[int] = None,
        policy_filter_ipv4: Optional[bool] = None,
        policy_filter_ipv6: Optional[bool] = None,
        policy_filter_non_ip: Optional[bool] = None,
        policy_no_ipv6_default_router_in_ra: Optional[bool] = None,
        policy_no_ipv6_default_router_in_ra_when_ipv6: Optional[bool] = None,
        policy_vlan_id: Optional[int] = None,
        policy_ver3: Optional[bool] = None,
    ) -> RpcResult:
        """CreateGroup -- Create Group.

        Create Group. Use this to create a new group in the security account database of the
        currently managed Virtual Hub. You can register multiple users in a group. To register
        users in a group use the SetUser API. This API cannot be invoked on VPN Bridge. You
        cannot execute this API for Virtual Hubs of VPN Servers operating as a member server on
        a cluster.

        .. note::
           Fields left at ``None`` are not sent, and the VPN Server applies its own default for
           each of them.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            name: The group name (``Name_str``, string (ASCII))
            realname: Optional real name (full name) of the group, allow using any Unicode
                characters (``Realname_utf``, string (UTF8))
            note: Optional, specify a description of the group (``Note_utf``, string (UTF8))
            use_policy: The flag whether to use security policy (``UsePolicy_bool``, boolean)
            policy_access: Security policy: Allow Access. The users, which this policy value is
                true, have permission to make VPN connection to VPN Server. (``policy:Access_bool``,
                boolean)
            policy_dhcp_filter: Security policy: Filter DHCP Packets (IPv4). All IPv4 DHCP
                packets in sessions defined this policy will be filtered.
                (``policy:DHCPFilter_bool``, boolean)
            policy_dhcp_no_server: Security policy: Disallow DHCP Server Operation (IPv4).
                Computers connected to sessions that have this policy setting will not be allowed to
                become a DHCP server and distribute IPv4 addresses to DHCP clients.
                (``policy:DHCPNoServer_bool``, boolean)
            policy_dhcp_force: Security policy: Enforce DHCP Allocated IP Addresses (IPv4).
                Computers in sessions that have this policy setting will only be able to use IPv4
                addresses allocated by a DHCP server on the virtual network side.
                (``policy:DHCPForce_bool``, boolean)
            policy_no_bridge: Security policy: Deny Bridge Operation. Bridge-mode connections
                are denied for user sessions that have this policy setting. Even in cases when the
                Ethernet Bridge is configured in the client side, communication will not be
                possible. (``policy:NoBridge_bool``, boolean)
            policy_no_routing: Security policy: Deny Routing Operation (IPv4). IPv4 routing will
                be denied for sessions that have this policy setting. Even in the case where the IP
                router is operating on the user client side, communication will not be possible.
                (``policy:NoRouting_bool``, boolean)
            policy_check_mac: Security policy: Deny MAC Addresses Duplication. The use of
                duplicating MAC addresses that are in use by computers of different sessions cannot
                be used by sessions with this policy setting. (``policy:CheckMac_bool``, boolean)
            policy_check_ip: Security policy: Deny IP Address Duplication (IPv4). The use of
                duplicating IPv4 addresses that are in use by computers of different sessions cannot
                be used by sessions with this policy setting. (``policy:CheckIP_bool``, boolean)
            policy_arp_dhcp_only: Security policy: Deny Non-ARP / Non-DHCP / Non-ICMPv6
                broadcasts. The sending or receiving of broadcast packets that are not ARP protocol,
                DHCP protocol, nor ICMPv6 on the virtual network will not be allowed for sessions
                with this policy setting. (``policy:ArpDhcpOnly_bool``, boolean)
            policy_privacy_filter: Security policy: Privacy Filter Mode. All direct
                communication between sessions with the privacy filter mode policy setting will be
                filtered. (``policy:PrivacyFilter_bool``, boolean)
            policy_no_server: Security policy: Deny Operation as TCP/IP Server (IPv4). Computers
                of sessions with this policy setting can't listen and accept TCP/IP connections in
                IPv4. (``policy:NoServer_bool``, boolean)
            policy_no_broadcast_limiter: Security policy: Unlimited Number of Broadcasts. If a
                computer of a session with this policy setting sends broadcast packets of a number
                unusually larger than what would be considered normal on the virtual network, there
                will be no automatic limiting. (``policy:NoBroadcastLimiter_bool``, boolean)
            policy_monitor_port: Security policy: Allow Monitoring Mode. Users with this policy
                setting will be granted to connect to the Virtual Hub in Monitoring Mode. Sessions
                in Monitoring Mode are able to monitor (tap) all packets flowing through the Virtual
                Hub. (``policy:MonitorPort_bool``, boolean)
            policy_max_connection: Security policy: Maximum Number of TCP Connections. For
                sessions with this policy setting, this sets the maximum number of physical TCP
                connections consists in a physical VPN session. (``policy:MaxConnection_u32``,
                number (uint32))
            policy_time_out: Security policy: Time-out Period. For sessions with this policy
                setting, this sets, in seconds, the time-out period to wait before disconnecting a
                session when communication trouble occurs between the VPN Client / VPN Server.
                (``policy:TimeOut_u32``, number (uint32))
            policy_max_mac: Security policy: Maximum Number of MAC Addresses. For sessions with
                this policy setting, this limits the number of MAC addresses per session.
                (``policy:MaxMac_u32``, number (uint32))
            policy_max_ip: Security policy: Maximum Number of IP Addresses (IPv4). For sessions
                with this policy setting, this specifies the number of IPv4 addresses that can be
                registered for a single session. (``policy:MaxIP_u32``, number (uint32))
            policy_max_upload: Security policy: Upload Bandwidth. For sessions with this policy
                setting, this limits the traffic bandwidth that is in the inwards direction from
                outside to inside the Virtual Hub. (``policy:MaxUpload_u32``, number (uint32))
            policy_max_download: Security policy: Download Bandwidth. For sessions with this
                policy setting, this limits the traffic bandwidth that is in the outwards direction
                from inside the Virtual Hub to outside the Virtual Hub. (``policy:MaxDownload_u32``,
                number (uint32))
            policy_fix_password: Security policy: Deny Changing Password. The users which use
                password authentication with this policy setting are not allowed to change their own
                password from the VPN Client Manager or similar. (``policy:FixPassword_bool``,
                boolean)
            policy_multi_logins: Security policy: Maximum Number of Multiple Logins. Users with
                this policy setting are unable to have more than this number of concurrent logins.
                Bridge Mode sessions are not subjects to this policy. (``policy:MultiLogins_u32``,
                number (uint32))
            policy_no_qos: Security policy: Deny VoIP / QoS Function. Users with this security
                policy are unable to use VoIP / QoS functions in VPN connection sessions.
                (``policy:NoQoS_bool``, boolean)
            policy_rs_and_ra_filter: Security policy: Filter RS / RA Packets (IPv6). All ICMPv6
                packets which the message-type is 133 (Router Solicitation) or 134 (Router
                Advertisement) in sessions defined this policy will be filtered. As a result, an
                IPv6 client will be unable to use IPv6 address prefix auto detection and IPv6
                default gateway auto detection. (``policy:RSandRAFilter_bool``, boolean)
            policy_ra_filter: Security policy: Filter RA Packets (IPv6). All ICMPv6 packets
                which the message-type is 134 (Router Advertisement) in sessions defined this policy
                will be filtered. As a result, a malicious users will be unable to spread illegal
                IPv6 prefix or default gateway advertisements on the network.
                (``policy:RAFilter_bool``, boolean)
            policy_dhcpv6_filter: Security policy: Filter DHCP Packets (IPv6). All IPv6 DHCP
                packets in sessions defined this policy will be filtered.
                (``policy:DHCPv6Filter_bool``, boolean)
            policy_dhcpv6_no_server: Security policy: Disallow DHCP Server Operation (IPv6).
                Computers connected to sessions that have this policy setting will not be allowed to
                become a DHCP server and distribute IPv6 addresses to DHCP clients.
                (``policy:DHCPv6NoServer_bool``, boolean)
            policy_no_routing_v6: Security policy: Deny Routing Operation (IPv6). IPv6 routing
                will be denied for sessions that have this policy setting. Even in the case where
                the IP router is operating on the user client side, communication will not be
                possible. (``policy:NoRoutingV6_bool``, boolean)
            policy_check_ipv6: Security policy: Deny IP Address Duplication (IPv6). The use of
                duplicating IPv6 addresses that are in use by computers of different sessions cannot
                be used by sessions with this policy setting. (``policy:CheckIPv6_bool``, boolean)
            policy_no_server_v6: Security policy: Deny Operation as TCP/IP Server (IPv6).
                Computers of sessions with this policy setting can't listen and accept TCP/IP
                connections in IPv6. (``policy:NoServerV6_bool``, boolean)
            policy_max_ipv6: Security policy: Maximum Number of IP Addresses (IPv6). For
                sessions with this policy setting, this specifies the number of IPv6 addresses that
                can be registered for a single session. (``policy:MaxIPv6_u32``, number (uint32))
            policy_no_save_password: Security policy: Disallow Password Save in VPN Client. For
                users with this policy setting, when the user is using standard password
                authentication, the user will be unable to save the password in VPN Client. The user
                will be required to input passwords for every time to connect a VPN. This will
                improve the security. If this policy is enabled, VPN Client Version 2.0 will be
                denied to access. (``policy:NoSavePassword_bool``, boolean)
            policy_auto_disconnect: Security policy: VPN Client Automatic Disconnect. For users
                with this policy setting, a user's VPN session will be disconnected automatically
                after the specific period will elapse. In this case no automatic re-connection will
                be performed. This can prevent a lot of inactive VPN Sessions. If this policy is
                enabled, VPN Client Version 2.0 will be denied to access.
                (``policy:AutoDisconnect_u32``, number (uint32))
            policy_filter_ipv4: Security policy: Filter All IPv4 Packets. All IPv4 and ARP
                packets in sessions defined this policy will be filtered.
                (``policy:FilterIPv4_bool``, boolean)
            policy_filter_ipv6: Security policy: Filter All IPv6 Packets. All IPv6 packets in
                sessions defined this policy will be filtered. (``policy:FilterIPv6_bool``, boolean)
            policy_filter_non_ip: Security policy: Filter All Non-IP Packets. All non-IP packets
                in sessions defined this policy will be filtered. "Non-IP packet" mean a packet
                which is not IPv4, ARP nor IPv6. Any tagged-VLAN packets via the Virtual Hub will be
                regarded as non-IP packets. (``policy:FilterNonIP_bool``, boolean)
            policy_no_ipv6_default_router_in_ra: Security policy: No Default-Router on IPv6 RA.
                In all VPN Sessions defines this policy, any IPv6 RA (Router Advertisement) packet
                with non-zero value in the router-lifetime will set to zero-value. This is effective
                to avoid the horrible behavior from the IPv6 routing confusion which is caused by
                the VPN client's attempts to use the remote-side IPv6 router as its local IPv6
                router. (``policy:NoIPv6DefaultRouterInRA_bool``, boolean)
            policy_no_ipv6_default_router_in_ra_when_ipv6: Security policy: No Default-Router on
                IPv6 RA (physical IPv6). In all VPN Sessions defines this policy (only when the
                physical communication protocol between VPN Client / VPN Bridge and VPN Server is
                IPv6), any IPv6 RA (Router Advertisement) packet with non-zero value in the
                router-lifetime will set to zero-value. This is effective to avoid the horrible
                behavior from the IPv6 routing confusion which is caused by the VPN client's
                attempts to use the remote-side IPv6 router as its local IPv6 router.
                (``policy:NoIPv6DefaultRouterInRAWhenIPv6_bool``, boolean)
            policy_vlan_id: Security policy: VLAN ID (IEEE802.1Q). You can specify the VLAN ID
                on the security policy. All VPN Sessions defines this policy, all Ethernet packets
                toward the Virtual Hub from the user will be inserted a VLAN tag (IEEE 802.1Q) with
                the VLAN ID. The user can also receive only packets with a VLAN tag which has the
                same VLAN ID. (Receiving process removes the VLAN tag automatically.) Any Ethernet
                packets with any other VLAN IDs or non-VLAN packets will not be received. All VPN
                Sessions without this policy definition can send / receive any kinds of Ethernet
                packets regardless of VLAN tags, and VLAN tags are not inserted or removed
                automatically. Any tagged-VLAN packets via the Virtual Hub will be regarded as
                non-IP packets. Therefore, tagged-VLAN packets are not subjects for IPv4 / IPv6
                security policies, access lists nor other IPv4 / IPv6 specific deep processing.
                (``policy:VLanId_u32``, number (uint32))
            policy_ver3: Security policy: Whether version 3.0 (must be true)
                (``policy:Ver3_bool``, boolean)

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Name_str``, ``Realname_utf``,
            ``Note_utf``, ``Recv.BroadcastBytes_u64``, ``Recv.BroadcastCount_u64``,
            ``Recv.UnicastBytes_u64``, ``Recv.UnicastCount_u64``, ``Send.BroadcastBytes_u64``,
            ``Send.BroadcastCount_u64``, ``Send.UnicastBytes_u64``, ``Send.UnicastCount_u64``,
            ``UsePolicy_bool``, ``policy:Access_bool``, ``policy:DHCPFilter_bool``,
            ``policy:DHCPNoServer_bool``, ``policy:DHCPForce_bool``, ``policy:NoBridge_bool``,
            ``policy:NoRouting_bool``, ``policy:CheckMac_bool``, ``policy:CheckIP_bool``,
            ``policy:ArpDhcpOnly_bool``, ``policy:PrivacyFilter_bool``,
            ``policy:NoServer_bool``, ``policy:NoBroadcastLimiter_bool``,
            ``policy:MonitorPort_bool``, ``policy:MaxConnection_u32``, ``policy:TimeOut_u32``,
            ``policy:MaxMac_u32``, ``policy:MaxIP_u32``, ``policy:MaxUpload_u32``,
            ``policy:MaxDownload_u32``, ``policy:FixPassword_bool``, ``policy:MultiLogins_u32``,
            ``policy:NoQoS_bool``, ``policy:RSandRAFilter_bool``, ``policy:RAFilter_bool``,
            ``policy:DHCPv6Filter_bool``, ``policy:DHCPv6NoServer_bool``,
            ``policy:NoRoutingV6_bool``, ``policy:CheckIPv6_bool``, ``policy:NoServerV6_bool``,
            ``policy:MaxIPv6_u32``, ``policy:NoSavePassword_bool``,
            ``policy:AutoDisconnect_u32``, ``policy:FilterIPv4_bool``,
            ``policy:FilterIPv6_bool``, ``policy:FilterNonIP_bool``,
            ``policy:NoIPv6DefaultRouterInRA_bool``,
            ``policy:NoIPv6DefaultRouterInRAWhenIPv6_bool``, ``policy:VLanId_u32``,
            ``policy:Ver3_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("CreateGroup", {
            "HubName_str": hub_name,
            "Name_str": name,
            "Realname_utf": realname,
            "Note_utf": note,
            "UsePolicy_bool": use_policy,
            "policy:Access_bool": policy_access,
            "policy:DHCPFilter_bool": policy_dhcp_filter,
            "policy:DHCPNoServer_bool": policy_dhcp_no_server,
            "policy:DHCPForce_bool": policy_dhcp_force,
            "policy:NoBridge_bool": policy_no_bridge,
            "policy:NoRouting_bool": policy_no_routing,
            "policy:CheckMac_bool": policy_check_mac,
            "policy:CheckIP_bool": policy_check_ip,
            "policy:ArpDhcpOnly_bool": policy_arp_dhcp_only,
            "policy:PrivacyFilter_bool": policy_privacy_filter,
            "policy:NoServer_bool": policy_no_server,
            "policy:NoBroadcastLimiter_bool": policy_no_broadcast_limiter,
            "policy:MonitorPort_bool": policy_monitor_port,
            "policy:MaxConnection_u32": policy_max_connection,
            "policy:TimeOut_u32": policy_time_out,
            "policy:MaxMac_u32": policy_max_mac,
            "policy:MaxIP_u32": policy_max_ip,
            "policy:MaxUpload_u32": policy_max_upload,
            "policy:MaxDownload_u32": policy_max_download,
            "policy:FixPassword_bool": policy_fix_password,
            "policy:MultiLogins_u32": policy_multi_logins,
            "policy:NoQoS_bool": policy_no_qos,
            "policy:RSandRAFilter_bool": policy_rs_and_ra_filter,
            "policy:RAFilter_bool": policy_ra_filter,
            "policy:DHCPv6Filter_bool": policy_dhcpv6_filter,
            "policy:DHCPv6NoServer_bool": policy_dhcpv6_no_server,
            "policy:NoRoutingV6_bool": policy_no_routing_v6,
            "policy:CheckIPv6_bool": policy_check_ipv6,
            "policy:NoServerV6_bool": policy_no_server_v6,
            "policy:MaxIPv6_u32": policy_max_ipv6,
            "policy:NoSavePassword_bool": policy_no_save_password,
            "policy:AutoDisconnect_u32": policy_auto_disconnect,
            "policy:FilterIPv4_bool": policy_filter_ipv4,
            "policy:FilterIPv6_bool": policy_filter_ipv6,
            "policy:FilterNonIP_bool": policy_filter_non_ip,
            "policy:NoIPv6DefaultRouterInRA_bool": policy_no_ipv6_default_router_in_ra,
            "policy:NoIPv6DefaultRouterInRAWhenIPv6_bool": policy_no_ipv6_default_router_in_ra_when_ipv6,
            "policy:VLanId_u32": policy_vlan_id,
            "policy:Ver3_bool": policy_ver3,
        })

    def set_group(
        self,
        hub_name: str,
        name: str,
        *,
        realname: Optional[str] = None,
        note: Optional[str] = None,
        use_policy: Optional[bool] = None,
        policy_access: Optional[bool] = None,
        policy_dhcp_filter: Optional[bool] = None,
        policy_dhcp_no_server: Optional[bool] = None,
        policy_dhcp_force: Optional[bool] = None,
        policy_no_bridge: Optional[bool] = None,
        policy_no_routing: Optional[bool] = None,
        policy_check_mac: Optional[bool] = None,
        policy_check_ip: Optional[bool] = None,
        policy_arp_dhcp_only: Optional[bool] = None,
        policy_privacy_filter: Optional[bool] = None,
        policy_no_server: Optional[bool] = None,
        policy_no_broadcast_limiter: Optional[bool] = None,
        policy_monitor_port: Optional[bool] = None,
        policy_max_connection: Optional[int] = None,
        policy_time_out: Optional[int] = None,
        policy_max_mac: Optional[int] = None,
        policy_max_ip: Optional[int] = None,
        policy_max_upload: Optional[int] = None,
        policy_max_download: Optional[int] = None,
        policy_fix_password: Optional[bool] = None,
        policy_multi_logins: Optional[int] = None,
        policy_no_qos: Optional[bool] = None,
        policy_rs_and_ra_filter: Optional[bool] = None,
        policy_ra_filter: Optional[bool] = None,
        policy_dhcpv6_filter: Optional[bool] = None,
        policy_dhcpv6_no_server: Optional[bool] = None,
        policy_no_routing_v6: Optional[bool] = None,
        policy_check_ipv6: Optional[bool] = None,
        policy_no_server_v6: Optional[bool] = None,
        policy_max_ipv6: Optional[int] = None,
        policy_no_save_password: Optional[bool] = None,
        policy_auto_disconnect: Optional[int] = None,
        policy_filter_ipv4: Optional[bool] = None,
        policy_filter_ipv6: Optional[bool] = None,
        policy_filter_non_ip: Optional[bool] = None,
        policy_no_ipv6_default_router_in_ra: Optional[bool] = None,
        policy_no_ipv6_default_router_in_ra_when_ipv6: Optional[bool] = None,
        policy_vlan_id: Optional[int] = None,
        policy_ver3: Optional[bool] = None,
    ) -> RpcResult:
        """SetGroup -- Set group settings.

        Set group settings. Use this to set group settings that is registered on the security
        account database of the currently managed Virtual Hub. To get the list of currently
        registered groups, use the EnumGroup API. This API cannot be invoked on VPN Bridge. You
        cannot execute this API for Virtual Hubs of VPN Servers operating as a member server on
        a cluster.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            name: The group name (``Name_str``, string (ASCII))
            realname: Optional real name (full name) of the group, allow using any Unicode
                characters (``Realname_utf``, string (UTF8))
            note: Optional, specify a description of the group (``Note_utf``, string (UTF8))
            use_policy: The flag whether to use security policy (``UsePolicy_bool``, boolean)
            policy_access: Security policy: Allow Access. The users, which this policy value is
                true, have permission to make VPN connection to VPN Server. (``policy:Access_bool``,
                boolean)
            policy_dhcp_filter: Security policy: Filter DHCP Packets (IPv4). All IPv4 DHCP
                packets in sessions defined this policy will be filtered.
                (``policy:DHCPFilter_bool``, boolean)
            policy_dhcp_no_server: Security policy: Disallow DHCP Server Operation (IPv4).
                Computers connected to sessions that have this policy setting will not be allowed to
                become a DHCP server and distribute IPv4 addresses to DHCP clients.
                (``policy:DHCPNoServer_bool``, boolean)
            policy_dhcp_force: Security policy: Enforce DHCP Allocated IP Addresses (IPv4).
                Computers in sessions that have this policy setting will only be able to use IPv4
                addresses allocated by a DHCP server on the virtual network side.
                (``policy:DHCPForce_bool``, boolean)
            policy_no_bridge: Security policy: Deny Bridge Operation. Bridge-mode connections
                are denied for user sessions that have this policy setting. Even in cases when the
                Ethernet Bridge is configured in the client side, communication will not be
                possible. (``policy:NoBridge_bool``, boolean)
            policy_no_routing: Security policy: Deny Routing Operation (IPv4). IPv4 routing will
                be denied for sessions that have this policy setting. Even in the case where the IP
                router is operating on the user client side, communication will not be possible.
                (``policy:NoRouting_bool``, boolean)
            policy_check_mac: Security policy: Deny MAC Addresses Duplication. The use of
                duplicating MAC addresses that are in use by computers of different sessions cannot
                be used by sessions with this policy setting. (``policy:CheckMac_bool``, boolean)
            policy_check_ip: Security policy: Deny IP Address Duplication (IPv4). The use of
                duplicating IPv4 addresses that are in use by computers of different sessions cannot
                be used by sessions with this policy setting. (``policy:CheckIP_bool``, boolean)
            policy_arp_dhcp_only: Security policy: Deny Non-ARP / Non-DHCP / Non-ICMPv6
                broadcasts. The sending or receiving of broadcast packets that are not ARP protocol,
                DHCP protocol, nor ICMPv6 on the virtual network will not be allowed for sessions
                with this policy setting. (``policy:ArpDhcpOnly_bool``, boolean)
            policy_privacy_filter: Security policy: Privacy Filter Mode. All direct
                communication between sessions with the privacy filter mode policy setting will be
                filtered. (``policy:PrivacyFilter_bool``, boolean)
            policy_no_server: Security policy: Deny Operation as TCP/IP Server (IPv4). Computers
                of sessions with this policy setting can't listen and accept TCP/IP connections in
                IPv4. (``policy:NoServer_bool``, boolean)
            policy_no_broadcast_limiter: Security policy: Unlimited Number of Broadcasts. If a
                computer of a session with this policy setting sends broadcast packets of a number
                unusually larger than what would be considered normal on the virtual network, there
                will be no automatic limiting. (``policy:NoBroadcastLimiter_bool``, boolean)
            policy_monitor_port: Security policy: Allow Monitoring Mode. Users with this policy
                setting will be granted to connect to the Virtual Hub in Monitoring Mode. Sessions
                in Monitoring Mode are able to monitor (tap) all packets flowing through the Virtual
                Hub. (``policy:MonitorPort_bool``, boolean)
            policy_max_connection: Security policy: Maximum Number of TCP Connections. For
                sessions with this policy setting, this sets the maximum number of physical TCP
                connections consists in a physical VPN session. (``policy:MaxConnection_u32``,
                number (uint32))
            policy_time_out: Security policy: Time-out Period. For sessions with this policy
                setting, this sets, in seconds, the time-out period to wait before disconnecting a
                session when communication trouble occurs between the VPN Client / VPN Server.
                (``policy:TimeOut_u32``, number (uint32))
            policy_max_mac: Security policy: Maximum Number of MAC Addresses. For sessions with
                this policy setting, this limits the number of MAC addresses per session.
                (``policy:MaxMac_u32``, number (uint32))
            policy_max_ip: Security policy: Maximum Number of IP Addresses (IPv4). For sessions
                with this policy setting, this specifies the number of IPv4 addresses that can be
                registered for a single session. (``policy:MaxIP_u32``, number (uint32))
            policy_max_upload: Security policy: Upload Bandwidth. For sessions with this policy
                setting, this limits the traffic bandwidth that is in the inwards direction from
                outside to inside the Virtual Hub. (``policy:MaxUpload_u32``, number (uint32))
            policy_max_download: Security policy: Download Bandwidth. For sessions with this
                policy setting, this limits the traffic bandwidth that is in the outwards direction
                from inside the Virtual Hub to outside the Virtual Hub. (``policy:MaxDownload_u32``,
                number (uint32))
            policy_fix_password: Security policy: Deny Changing Password. The users which use
                password authentication with this policy setting are not allowed to change their own
                password from the VPN Client Manager or similar. (``policy:FixPassword_bool``,
                boolean)
            policy_multi_logins: Security policy: Maximum Number of Multiple Logins. Users with
                this policy setting are unable to have more than this number of concurrent logins.
                Bridge Mode sessions are not subjects to this policy. (``policy:MultiLogins_u32``,
                number (uint32))
            policy_no_qos: Security policy: Deny VoIP / QoS Function. Users with this security
                policy are unable to use VoIP / QoS functions in VPN connection sessions.
                (``policy:NoQoS_bool``, boolean)
            policy_rs_and_ra_filter: Security policy: Filter RS / RA Packets (IPv6). All ICMPv6
                packets which the message-type is 133 (Router Solicitation) or 134 (Router
                Advertisement) in sessions defined this policy will be filtered. As a result, an
                IPv6 client will be unable to use IPv6 address prefix auto detection and IPv6
                default gateway auto detection. (``policy:RSandRAFilter_bool``, boolean)
            policy_ra_filter: Security policy: Filter RA Packets (IPv6). All ICMPv6 packets
                which the message-type is 134 (Router Advertisement) in sessions defined this policy
                will be filtered. As a result, a malicious users will be unable to spread illegal
                IPv6 prefix or default gateway advertisements on the network.
                (``policy:RAFilter_bool``, boolean)
            policy_dhcpv6_filter: Security policy: Filter DHCP Packets (IPv6). All IPv6 DHCP
                packets in sessions defined this policy will be filtered.
                (``policy:DHCPv6Filter_bool``, boolean)
            policy_dhcpv6_no_server: Security policy: Disallow DHCP Server Operation (IPv6).
                Computers connected to sessions that have this policy setting will not be allowed to
                become a DHCP server and distribute IPv6 addresses to DHCP clients.
                (``policy:DHCPv6NoServer_bool``, boolean)
            policy_no_routing_v6: Security policy: Deny Routing Operation (IPv6). IPv6 routing
                will be denied for sessions that have this policy setting. Even in the case where
                the IP router is operating on the user client side, communication will not be
                possible. (``policy:NoRoutingV6_bool``, boolean)
            policy_check_ipv6: Security policy: Deny IP Address Duplication (IPv6). The use of
                duplicating IPv6 addresses that are in use by computers of different sessions cannot
                be used by sessions with this policy setting. (``policy:CheckIPv6_bool``, boolean)
            policy_no_server_v6: Security policy: Deny Operation as TCP/IP Server (IPv6).
                Computers of sessions with this policy setting can't listen and accept TCP/IP
                connections in IPv6. (``policy:NoServerV6_bool``, boolean)
            policy_max_ipv6: Security policy: Maximum Number of IP Addresses (IPv6). For
                sessions with this policy setting, this specifies the number of IPv6 addresses that
                can be registered for a single session. (``policy:MaxIPv6_u32``, number (uint32))
            policy_no_save_password: Security policy: Disallow Password Save in VPN Client. For
                users with this policy setting, when the user is using standard password
                authentication, the user will be unable to save the password in VPN Client. The user
                will be required to input passwords for every time to connect a VPN. This will
                improve the security. If this policy is enabled, VPN Client Version 2.0 will be
                denied to access. (``policy:NoSavePassword_bool``, boolean)
            policy_auto_disconnect: Security policy: VPN Client Automatic Disconnect. For users
                with this policy setting, a user's VPN session will be disconnected automatically
                after the specific period will elapse. In this case no automatic re-connection will
                be performed. This can prevent a lot of inactive VPN Sessions. If this policy is
                enabled, VPN Client Version 2.0 will be denied to access.
                (``policy:AutoDisconnect_u32``, number (uint32))
            policy_filter_ipv4: Security policy: Filter All IPv4 Packets. All IPv4 and ARP
                packets in sessions defined this policy will be filtered.
                (``policy:FilterIPv4_bool``, boolean)
            policy_filter_ipv6: Security policy: Filter All IPv6 Packets. All IPv6 packets in
                sessions defined this policy will be filtered. (``policy:FilterIPv6_bool``, boolean)
            policy_filter_non_ip: Security policy: Filter All Non-IP Packets. All non-IP packets
                in sessions defined this policy will be filtered. "Non-IP packet" mean a packet
                which is not IPv4, ARP nor IPv6. Any tagged-VLAN packets via the Virtual Hub will be
                regarded as non-IP packets. (``policy:FilterNonIP_bool``, boolean)
            policy_no_ipv6_default_router_in_ra: Security policy: No Default-Router on IPv6 RA.
                In all VPN Sessions defines this policy, any IPv6 RA (Router Advertisement) packet
                with non-zero value in the router-lifetime will set to zero-value. This is effective
                to avoid the horrible behavior from the IPv6 routing confusion which is caused by
                the VPN client's attempts to use the remote-side IPv6 router as its local IPv6
                router. (``policy:NoIPv6DefaultRouterInRA_bool``, boolean)
            policy_no_ipv6_default_router_in_ra_when_ipv6: Security policy: No Default-Router on
                IPv6 RA (physical IPv6). In all VPN Sessions defines this policy (only when the
                physical communication protocol between VPN Client / VPN Bridge and VPN Server is
                IPv6), any IPv6 RA (Router Advertisement) packet with non-zero value in the
                router-lifetime will set to zero-value. This is effective to avoid the horrible
                behavior from the IPv6 routing confusion which is caused by the VPN client's
                attempts to use the remote-side IPv6 router as its local IPv6 router.
                (``policy:NoIPv6DefaultRouterInRAWhenIPv6_bool``, boolean)
            policy_vlan_id: Security policy: VLAN ID (IEEE802.1Q). You can specify the VLAN ID
                on the security policy. All VPN Sessions defines this policy, all Ethernet packets
                toward the Virtual Hub from the user will be inserted a VLAN tag (IEEE 802.1Q) with
                the VLAN ID. The user can also receive only packets with a VLAN tag which has the
                same VLAN ID. (Receiving process removes the VLAN tag automatically.) Any Ethernet
                packets with any other VLAN IDs or non-VLAN packets will not be received. All VPN
                Sessions without this policy definition can send / receive any kinds of Ethernet
                packets regardless of VLAN tags, and VLAN tags are not inserted or removed
                automatically. Any tagged-VLAN packets via the Virtual Hub will be regarded as
                non-IP packets. Therefore, tagged-VLAN packets are not subjects for IPv4 / IPv6
                security policies, access lists nor other IPv4 / IPv6 specific deep processing.
                (``policy:VLanId_u32``, number (uint32))
            policy_ver3: Security policy: Whether version 3.0 (must be true)
                (``policy:Ver3_bool``, boolean)

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Name_str``, ``Realname_utf``,
            ``Note_utf``, ``Recv.BroadcastBytes_u64``, ``Recv.BroadcastCount_u64``,
            ``Recv.UnicastBytes_u64``, ``Recv.UnicastCount_u64``, ``Send.BroadcastBytes_u64``,
            ``Send.BroadcastCount_u64``, ``Send.UnicastBytes_u64``, ``Send.UnicastCount_u64``,
            ``UsePolicy_bool``, ``policy:Access_bool``, ``policy:DHCPFilter_bool``,
            ``policy:DHCPNoServer_bool``, ``policy:DHCPForce_bool``, ``policy:NoBridge_bool``,
            ``policy:NoRouting_bool``, ``policy:CheckMac_bool``, ``policy:CheckIP_bool``,
            ``policy:ArpDhcpOnly_bool``, ``policy:PrivacyFilter_bool``,
            ``policy:NoServer_bool``, ``policy:NoBroadcastLimiter_bool``,
            ``policy:MonitorPort_bool``, ``policy:MaxConnection_u32``, ``policy:TimeOut_u32``,
            ``policy:MaxMac_u32``, ``policy:MaxIP_u32``, ``policy:MaxUpload_u32``,
            ``policy:MaxDownload_u32``, ``policy:FixPassword_bool``, ``policy:MultiLogins_u32``,
            ``policy:NoQoS_bool``, ``policy:RSandRAFilter_bool``, ``policy:RAFilter_bool``,
            ``policy:DHCPv6Filter_bool``, ``policy:DHCPv6NoServer_bool``,
            ``policy:NoRoutingV6_bool``, ``policy:CheckIPv6_bool``, ``policy:NoServerV6_bool``,
            ``policy:MaxIPv6_u32``, ``policy:NoSavePassword_bool``,
            ``policy:AutoDisconnect_u32``, ``policy:FilterIPv4_bool``,
            ``policy:FilterIPv6_bool``, ``policy:FilterNonIP_bool``,
            ``policy:NoIPv6DefaultRouterInRA_bool``,
            ``policy:NoIPv6DefaultRouterInRAWhenIPv6_bool``, ``policy:VLanId_u32``,
            ``policy:Ver3_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetGroup", {
            "HubName_str": hub_name,
            "Name_str": name,
            "Realname_utf": realname,
            "Note_utf": note,
            "UsePolicy_bool": use_policy,
            "policy:Access_bool": policy_access,
            "policy:DHCPFilter_bool": policy_dhcp_filter,
            "policy:DHCPNoServer_bool": policy_dhcp_no_server,
            "policy:DHCPForce_bool": policy_dhcp_force,
            "policy:NoBridge_bool": policy_no_bridge,
            "policy:NoRouting_bool": policy_no_routing,
            "policy:CheckMac_bool": policy_check_mac,
            "policy:CheckIP_bool": policy_check_ip,
            "policy:ArpDhcpOnly_bool": policy_arp_dhcp_only,
            "policy:PrivacyFilter_bool": policy_privacy_filter,
            "policy:NoServer_bool": policy_no_server,
            "policy:NoBroadcastLimiter_bool": policy_no_broadcast_limiter,
            "policy:MonitorPort_bool": policy_monitor_port,
            "policy:MaxConnection_u32": policy_max_connection,
            "policy:TimeOut_u32": policy_time_out,
            "policy:MaxMac_u32": policy_max_mac,
            "policy:MaxIP_u32": policy_max_ip,
            "policy:MaxUpload_u32": policy_max_upload,
            "policy:MaxDownload_u32": policy_max_download,
            "policy:FixPassword_bool": policy_fix_password,
            "policy:MultiLogins_u32": policy_multi_logins,
            "policy:NoQoS_bool": policy_no_qos,
            "policy:RSandRAFilter_bool": policy_rs_and_ra_filter,
            "policy:RAFilter_bool": policy_ra_filter,
            "policy:DHCPv6Filter_bool": policy_dhcpv6_filter,
            "policy:DHCPv6NoServer_bool": policy_dhcpv6_no_server,
            "policy:NoRoutingV6_bool": policy_no_routing_v6,
            "policy:CheckIPv6_bool": policy_check_ipv6,
            "policy:NoServerV6_bool": policy_no_server_v6,
            "policy:MaxIPv6_u32": policy_max_ipv6,
            "policy:NoSavePassword_bool": policy_no_save_password,
            "policy:AutoDisconnect_u32": policy_auto_disconnect,
            "policy:FilterIPv4_bool": policy_filter_ipv4,
            "policy:FilterIPv6_bool": policy_filter_ipv6,
            "policy:FilterNonIP_bool": policy_filter_non_ip,
            "policy:NoIPv6DefaultRouterInRA_bool": policy_no_ipv6_default_router_in_ra,
            "policy:NoIPv6DefaultRouterInRAWhenIPv6_bool": policy_no_ipv6_default_router_in_ra_when_ipv6,
            "policy:VLanId_u32": policy_vlan_id,
            "policy:Ver3_bool": policy_ver3,
        })

    def get_group(
        self,
        hub_name: str,
        name: str,
    ) -> RpcResult:
        """GetGroup -- Get Group Setting (Sync mode).

        Get Group Setting (Sync mode). Use this to get the setting of a group that is registered
        on the security account database of the currently managed Virtual Hub. To get the list
        of currently registered groups, use the EnumGroup API. This API cannot be invoked on VPN
        Bridge. You cannot execute this API for Virtual Hubs of VPN Servers operating as a
        member server on a cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            name: The group name (``Name_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Name_str``, ``Realname_utf``,
            ``Note_utf``, ``Recv.BroadcastBytes_u64``, ``Recv.BroadcastCount_u64``,
            ``Recv.UnicastBytes_u64``, ``Recv.UnicastCount_u64``, ``Send.BroadcastBytes_u64``,
            ``Send.BroadcastCount_u64``, ``Send.UnicastBytes_u64``, ``Send.UnicastCount_u64``,
            ``UsePolicy_bool``, ``policy:Access_bool``, ``policy:DHCPFilter_bool``,
            ``policy:DHCPNoServer_bool``, ``policy:DHCPForce_bool``, ``policy:NoBridge_bool``,
            ``policy:NoRouting_bool``, ``policy:CheckMac_bool``, ``policy:CheckIP_bool``,
            ``policy:ArpDhcpOnly_bool``, ``policy:PrivacyFilter_bool``,
            ``policy:NoServer_bool``, ``policy:NoBroadcastLimiter_bool``,
            ``policy:MonitorPort_bool``, ``policy:MaxConnection_u32``, ``policy:TimeOut_u32``,
            ``policy:MaxMac_u32``, ``policy:MaxIP_u32``, ``policy:MaxUpload_u32``,
            ``policy:MaxDownload_u32``, ``policy:FixPassword_bool``, ``policy:MultiLogins_u32``,
            ``policy:NoQoS_bool``, ``policy:RSandRAFilter_bool``, ``policy:RAFilter_bool``,
            ``policy:DHCPv6Filter_bool``, ``policy:DHCPv6NoServer_bool``,
            ``policy:NoRoutingV6_bool``, ``policy:CheckIPv6_bool``, ``policy:NoServerV6_bool``,
            ``policy:MaxIPv6_u32``, ``policy:NoSavePassword_bool``,
            ``policy:AutoDisconnect_u32``, ``policy:FilterIPv4_bool``,
            ``policy:FilterIPv6_bool``, ``policy:FilterNonIP_bool``,
            ``policy:NoIPv6DefaultRouterInRA_bool``,
            ``policy:NoIPv6DefaultRouterInRAWhenIPv6_bool``, ``policy:VLanId_u32``,
            ``policy:Ver3_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetGroup", {
            "HubName_str": hub_name,
            "Name_str": name,
        })

    def delete_group(
        self,
        hub_name: str,
        name: str,
    ) -> RpcResult:
        """DeleteGroup -- Delete User from Group.

        Delete User from Group. Use this to delete a specified user from the group that is
        registered on the security account database of the currently managed Virtual Hub. By
        deleting a user from the group, that user becomes unassigned. To get the list of
        currently registered groups, use the EnumGroup API. This API cannot be invoked on VPN
        Bridge. You cannot execute this API for Virtual Hubs of VPN Servers operating as a
        member server on a cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            name: User or group name (``Name_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Name_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("DeleteGroup", {
            "HubName_str": hub_name,
            "Name_str": name,
        })

    def enum_group(
        self,
        hub_name: str,
    ) -> RpcResult:
        """EnumGroup -- Get List of Groups.

        Get List of Groups. Use this to get a list of groups that are registered on the security
        account database of the currently managed Virtual Hub. This API cannot be invoked on VPN
        Bridge. You cannot execute this API for Virtual Hubs of VPN Servers operating as a
        member server on a cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``GroupList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumGroup", {
            "HubName_str": hub_name,
        })

    def enum_session(
        self,
        hub_name: str,
    ) -> RpcResult:
        """EnumSession -- Get List of Connected VPN Sessions.

        Get List of Connected VPN Sessions. Use this to get a list of the sessions connected to
        the Virtual Hub currently being managed. In the list of sessions, the following
        information will be obtained for each connection: Session Name, Session Site, User Name,
        Source Host Name, TCP Connection, Transfer Bytes and Transfer Packets. If the currently
        connected VPN Server is a cluster controller and the currently managed Virtual Hub is a
        static Virtual Hub, you can get an all-linked-together list of all sessions connected to
        that Virtual Hub on all cluster members. In all other cases, only the list of sessions
        that are actually connected to the currently managed VPN Server will be obtained.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``SessionList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumSession", {
            "HubName_str": hub_name,
        })

    def get_session_status(
        self,
        hub_name: str,
        name: str,
    ) -> RpcResult:
        """GetSessionStatus -- Get Session Status.

        Get Session Status. Use this to specify a session currently connected to the currently
        managed Virtual Hub and get the session information. The session status includes the
        following: source host name and user name, version information, time information, number
        of TCP connections, communication parameters, session key, statistical information on
        data transferred, and other client and server information. To get the list of currently
        connected sessions, use the EnumSession API.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            name: VPN session name (``Name_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Name_str``, ``Username_str``,
            ``RealUsername_str``, ``GroupName_str``, ``LinkMode_bool``,
            ``Client_Ip_Address_ip``, ``SessionStatus_ClientHostName_str``, ``Active_bool``,
            ``Connected_bool``, ``SessionStatus_u32``, ``ServerName_str``, ``ServerPort_u32``,
            ``ServerProductName_str``, ``ServerProductVer_u32``, ``ServerProductBuild_u32``,
            ``StartTime_dt``, ``FirstConnectionEstablisiedTime_dt``,
            ``CurrentConnectionEstablishTime_dt``, ``NumConnectionsEatablished_u32``,
            ``HalfConnection_bool``, ``QoS_bool``, ``MaxTcpConnections_u32``,
            ``NumTcpConnections_u32``, ``NumTcpConnectionsUpload_u32``,
            ``NumTcpConnectionsDownload_u32``, ``UseEncrypt_bool``, ``CipherName_str``,
            ``UseCompress_bool``, ``IsRUDPSession_bool``, ``UnderlayProtocol_str``,
            ``IsUdpAccelerationEnabled_bool``, ``IsUsingUdpAcceleration_bool``,
            ``SessionName_str``, ``ConnectionName_str``, ``SessionKey_bin``,
            ``TotalSendSize_u64``, ``TotalRecvSize_u64``, ``TotalSendSizeReal_u64``,
            ``TotalRecvSizeReal_u64``, ``IsBridgeMode_bool``, ``IsMonitorMode_bool``,
            ``VLanId_u32``, ``ClientProductName_str``, ``ClientProductVer_u32``,
            ``ClientProductBuild_u32``, ``ClientOsName_str``, ``ClientOsVer_str``,
            ``ClientOsProductId_str``, ``ClientHostname_str``, ``UniqueId_bin``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetSessionStatus", {
            "HubName_str": hub_name,
            "Name_str": name,
        })

    def delete_session(
        self,
        hub_name: str,
        name: str,
    ) -> RpcResult:
        """DeleteSession -- Disconnect Session.

        Disconnect Session. Use this to specify a session currently connected to the currently
        managed Virtual Hub and forcefully disconnect that session using manager privileges.
        Note that when communication is disconnected by settings on the source client side and
        the automatically reconnect option is enabled, it is possible that the client will
        reconnect. To get the list of currently connected sessions, use the EnumSession API.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            name: Session name (``Name_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Name_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("DeleteSession", {
            "HubName_str": hub_name,
            "Name_str": name,
        })

    def enum_mac_table(
        self,
        hub_name: str,
    ) -> RpcResult:
        """EnumMacTable -- Get the MAC Address Table Database.

        Get the MAC Address Table Database. Use this to get the MAC address table database that
        is held by the currently managed Virtual Hub. The MAC address table database is a table
        that the Virtual Hub requires to perform the action of switching Ethernet frames and the
        Virtual Hub decides the sorting destination session of each Ethernet frame based on the
        MAC address table database. The MAC address database is built by the Virtual Hub
        automatically analyzing the contents of the communication.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``MacTable``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumMacTable", {
            "HubName_str": hub_name,
        })

    def delete_mac_table(
        self,
        hub_name: str,
        key: int,
    ) -> RpcResult:
        """DeleteMacTable -- Delete MAC Address Table Entry.

        Delete MAC Address Table Entry. Use this API to operate the MAC address table database
        held by the currently managed Virtual Hub and delete a specified MAC address table entry
        from the database. To get the contents of the current MAC address table database use the
        EnumMacTable API.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            key: Key ID (``Key_u32``, number (uint32))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Key_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("DeleteMacTable", {
            "HubName_str": hub_name,
            "Key_u32": key,
        })

    def enum_ip_table(
        self,
        hub_name: str,
    ) -> RpcResult:
        """EnumIpTable -- Get the IP Address Table Database.

        Get the IP Address Table Database. Use this to get the IP address table database that is
        held by the currently managed Virtual Hub. The IP address table database is a table that
        is automatically generated by analyzing the contents of communication so that the
        Virtual Hub can always know which session is using which IP address and it is frequently
        used by the engine that applies the Virtual Hub security policy. By specifying the
        session name you can get the IP address table entry that has been associated with that
        session.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``IpTable``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumIpTable", {
            "HubName_str": hub_name,
        })

    def delete_ip_table(
        self,
        hub_name: str,
        key: int,
    ) -> RpcResult:
        """DeleteIpTable -- Delete IP Address Table Entry.

        Delete IP Address Table Entry. Use this API to operate the IP address table database
        held by the currently managed Virtual Hub and delete a specified IP address table entry
        from the database. To get the contents of the current IP address table database use the
        EnumIpTable API.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            key: Key ID (``Key_u32``, number (uint32))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Key_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("DeleteIpTable", {
            "HubName_str": hub_name,
            "Key_u32": key,
        })

    def set_keep(
        self,
        *,
        use_keep_connect: Optional[bool] = None,
        keep_connect_host: Optional[str] = None,
        keep_connect_port: Optional[int] = None,
        keep_connect_protocol: Optional[Union[int, "KeepConnectProtocol"]] = None,
        keep_connect_interval: Optional[int] = None,
    ) -> RpcResult:
        """SetKeep -- Set the Keep Alive Internet Connection Function.

        Set the Keep Alive Internet Connection Function. Use this to set the destination host
        name etc. of the Keep Alive Internet Connection Function. For network connection
        environments where connections will automatically be disconnected where there are
        periods of no communication that are longer than a set period, by using the Keep Alive
        Internet Connection Function, it is possible to keep alive the Internet connection by
        sending packets to a nominated server on the Internet at set intervals. When using this
        API, you can specify the following: Host Name, Port Number, Packet Send Interval, and
        Protocol. Packets sent to keep alive the Internet connection will have random content
        and personal information that could identify a computer or user is not sent. You can use
        the SetKeep API to enable/disable the Keep Alive Internet Connection Function. To
        execute this API on a VPN Server or VPN Bridge, you must have administrator privileges.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            use_keep_connect: The flag to enable keep-alive to the Internet
                (``UseKeepConnect_bool``, boolean)
            keep_connect_host: Specify the host name or IP address of the destination
                (``KeepConnectHost_str``, string (ASCII))
            keep_connect_port: Specify the port number of the destination
                (``KeepConnectPort_u32``, number (uint32))
            keep_connect_protocol: Protocol type (``KeepConnectProtocol_u32``, number (enum))
                Accepts an :class:`KeepConnectProtocol` member or its integer value: 0 = TCP; 1
                = UDP.
            keep_connect_interval: Interval Between Packets Sends (Seconds)
                (``KeepConnectInterval_u32``, number (uint32))

        Returns:
            RpcResult with the fields: ``UseKeepConnect_bool``, ``KeepConnectHost_str``,
            ``KeepConnectPort_u32``, ``KeepConnectProtocol_u32``, ``KeepConnectInterval_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetKeep", {
            "UseKeepConnect_bool": use_keep_connect,
            "KeepConnectHost_str": keep_connect_host,
            "KeepConnectPort_u32": keep_connect_port,
            "KeepConnectProtocol_u32": keep_connect_protocol,
            "KeepConnectInterval_u32": keep_connect_interval,
        })

    def get_keep(
        self,
    ) -> RpcResult:
        """GetKeep -- Get the Keep Alive Internet Connection Function.

        Get the Keep Alive Internet Connection Function. Use this to get the current setting
        contents of the Keep Alive Internet Connection Function. In addition to the
        destination's Host Name, Port Number, Packet Send Interval and Protocol, you can obtain
        the current enabled/disabled status of the Keep Alive Internet Connection Function.

        Returns:
            RpcResult with the fields: ``UseKeepConnect_bool``, ``KeepConnectHost_str``,
            ``KeepConnectPort_u32``, ``KeepConnectProtocol_u32``, ``KeepConnectInterval_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetKeep", {})

    def enable_securenat(
        self,
        hub_name: str,
    ) -> RpcResult:
        """EnableSecureNAT -- Enable the Virtual NAT and DHCP Server Function (SecureNAT Function).

        Enable the Virtual NAT and DHCP Server Function (SecureNAT Function). Use this to enable
        the Virtual NAT and DHCP Server function (SecureNAT Function) on the currently managed
        Virtual Hub and begin its operation. Before executing this API, you must first check the
        setting contents of the current Virtual NAT function and DHCP Server function using the
        SetSecureNATOption API and GetSecureNATOption API. By enabling the SecureNAT function,
        you can virtually operate a NAT router (IP masquerade) and the DHCP Server function on a
        virtual network on the Virtual Hub. [Warning about SecureNAT Function] The SecureNAT
        function is recommended only for system administrators and people with a detailed
        knowledge of networks. If you use the SecureNAT function correctly, it is possible to
        achieve a safe form of remote access via a VPN. However when used in the wrong way, it
        can put the entire network in danger. Anyone who does not have a thorough knowledge of
        networks and anyone who does not have the network administrator's permission must not
        enable the SecureNAT function. For a detailed explanation of the SecureNAT function,
        please refer to the VPN Server's manual and online documentation. You cannot execute
        this API for Virtual Hubs of VPN Servers operating as a cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnableSecureNAT", {
            "HubName_str": hub_name,
        })

    def disable_securenat(
        self,
        hub_name: str,
    ) -> RpcResult:
        """DisableSecureNAT -- Disable the Virtual NAT and DHCP Server Function (SecureNAT Function).

        Disable the Virtual NAT and DHCP Server Function (SecureNAT Function). Use this to
        disable the Virtual NAT and DHCP Server function (SecureNAT Function) on the currently
        managed Virtual Hub. By executing this API the Virtual NAT function immediately stops
        operating and the Virtual DHCP Server function deletes the DHCP lease database and stops
        the service. You cannot execute this API for Virtual Hubs of VPN Servers operating as a
        cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("DisableSecureNAT", {
            "HubName_str": hub_name,
        })

    def set_securenat_option(
        self,
        rpc_hub_name: str,
        *,
        mac_address: Optional[Union[bytes, bytearray, str]] = None,
        ip: Optional[str] = None,
        mask: Optional[str] = None,
        use_nat: Optional[bool] = None,
        mtu: Optional[int] = None,
        nat_tcp_timeout: Optional[int] = None,
        nat_udp_timeout: Optional[int] = None,
        use_dhcp: Optional[bool] = None,
        dhcp_lease_ip_start: Optional[str] = None,
        dhcp_lease_ip_end: Optional[str] = None,
        dhcp_subnet_mask: Optional[str] = None,
        dhcp_expire_time_span: Optional[int] = None,
        dhcp_gateway_address: Optional[str] = None,
        dhcp_dns_server_address: Optional[str] = None,
        dhcp_dns_server_address2: Optional[str] = None,
        dhcp_domain_name: Optional[str] = None,
        save_log: Optional[bool] = None,
        apply_dhcp_push_routes: Optional[bool] = None,
        dhcp_push_routes: Optional[str] = None,
    ) -> RpcResult:
        """SetSecureNATOption -- Change Settings of SecureNAT Function.

        Change Settings of SecureNAT Function. Use this to change and save the virtual host
        network interface settings, virtual NAT function settings and virtual DHCP server
        settings of the Virtual NAT and DHCP Server function (SecureNAT function) on the
        currently managed Virtual Hub. The SecureNAT function holds one virtual network adapter
        on the L2 segment inside the Virtual Hub and it has been assigned a MAC address and an
        IP address. By doing this, another host connected to the same L2 segment is able to
        communicate with the SecureNAT virtual host as if it is an actual IP host existing on
        the network. [Warning about SecureNAT Function] The SecureNAT function is recommended
        only for system administrators and people with a detailed knowledge of networks. If you
        use the SecureNAT function correctly, it is possible to achieve a safe form of remote
        access via a VPN. However when used in the wrong way, it can put the entire network in
        danger. Anyone who does not have a thorough knowledge of networks and anyone who does
        not have the network administrators permission must not enable the SecureNAT function.
        For a detailed explanation of the SecureNAT function, please refer to the VPN Server's
        manual and online documentation. You cannot execute this API for Virtual Hubs of VPN
        Servers operating as a cluster.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            rpc_hub_name: Target Virtual HUB name (``RpcHubName_str``, string (ASCII))
            mac_address: MAC address (``MacAddress_bin``, string (Base64 binary))
            ip: IP address (``Ip_ip``, string (IP address))
            mask: Subnet mask (``Mask_ip``, string (IP address))
            use_nat: Use flag of the Virtual NAT function (``UseNat_bool``, boolean)
            mtu: MTU value (Standard: 1500) (``Mtu_u32``, number (uint32))
            nat_tcp_timeout: NAT TCP timeout in seconds (``NatTcpTimeout_u32``, number (uint32))
            nat_udp_timeout: NAT UDP timeout in seconds (``NatUdpTimeout_u32``, number (uint32))
            use_dhcp: Using flag of DHCP function (``UseDhcp_bool``, boolean)
            dhcp_lease_ip_start: Specify the start point of the address band to be distributed
                to the client. (Example: 192.168.30.10) (``DhcpLeaseIPStart_ip``, string (IP
                address))
            dhcp_lease_ip_end: Specify the end point of the address band to be distributed to
                the client. (Example: 192.168.30.200) (``DhcpLeaseIPEnd_ip``, string (IP address))
            dhcp_subnet_mask: Specify the subnet mask to be specified for the client. (Example:
                255.255.255.0) (``DhcpSubnetMask_ip``, string (IP address))
            dhcp_expire_time_span: Specify the expiration date in second units for leasing an IP
                address to a client. (``DhcpExpireTimeSpan_u32``, number (uint32))
            dhcp_gateway_address: Specify the IP address of the default gateway to be notified
                to the client. You can specify a SecureNAT Virtual Host IP address for this when the
                SecureNAT Function's Virtual NAT Function has been enabled and is being used also.
                If you specify 0 or none, then the client will not be notified of the default
                gateway. (``DhcpGatewayAddress_ip``, string (IP address))
            dhcp_dns_server_address: Specify the IP address of the primary DNS Server to be
                notified to the client. You can specify a SecureNAT Virtual Host IP address for this
                when the SecureNAT Function's Virtual NAT Function has been enabled and is being
                used also. If you specify empty, then the client will not be notified of the DNS
                Server address. (``DhcpDnsServerAddress_ip``, string (IP address))
            dhcp_dns_server_address2: Specify the IP address of the secondary DNS Server to be
                notified to the client. You can specify a SecureNAT Virtual Host IP address for this
                when the SecureNAT Function's Virtual NAT Function has been enabled and is being
                used also. If you specify empty, then the client will not be notified of the DNS
                Server address. (``DhcpDnsServerAddress2_ip``, string (IP address))
            dhcp_domain_name: Specify the domain name to be notified to the client. If you
                specify none, then the client will not be notified of the domain name.
                (``DhcpDomainName_str``, string (ASCII))
            save_log: Specify whether or not to save the Virtual DHCP Server operation in the
                Virtual Hub security log. Specify true to save it. This value is interlinked with
                the Virtual NAT Function log save setting. (``SaveLog_bool``, boolean)
            apply_dhcp_push_routes: The flag to enable the DhcpPushRoutes_str field.
                (``ApplyDhcpPushRoutes_bool``, boolean)
            dhcp_push_routes: Specify the static routing table to push. Example:
                "192.168.5.0/255.255.255.0/192.168.4.254, 10.0.0.0/255.0.0.0/192.168.4.253" Split
                multiple entries (maximum: 64 entries) by comma or space characters. Each entry must
                be specified in the "IP network address/subnet mask/gateway IP address" format. This
                Virtual DHCP Server can push the classless static routes (RFC 3442) with DHCP reply
                messages to VPN clients. Whether or not a VPN client can recognize the classless
                static routes (RFC 3442) depends on the target VPN client software. SoftEther VPN
                Client and OpenVPN Client are supporting the classless static routes. On L2TP/IPsec
                and MS-SSTP protocols, the compatibility depends on the implementation of the client
                software. You can realize the split tunneling if you clear the default gateway field
                on the Virtual DHCP Server options. On the client side, L2TP/IPsec and MS-SSTP
                clients need to be configured not to set up the default gateway for the split
                tunneling usage. You can also push the classless static routes (RFC 3442) by your
                existing external DHCP server. In that case, disable the Virtual DHCP Server
                function on SecureNAT, and you need not to set up the classless routes on this API.
                See the RFC 3442 to understand the classless routes. (``DhcpPushRoutes_str``, string
                (ASCII))

        Returns:
            RpcResult with the fields: ``RpcHubName_str``, ``MacAddress_bin``, ``Ip_ip``,
            ``Mask_ip``, ``UseNat_bool``, ``Mtu_u32``, ``NatTcpTimeout_u32``,
            ``NatUdpTimeout_u32``, ``UseDhcp_bool``, ``DhcpLeaseIPStart_ip``,
            ``DhcpLeaseIPEnd_ip``, ``DhcpSubnetMask_ip``, ``DhcpExpireTimeSpan_u32``,
            ``DhcpGatewayAddress_ip``, ``DhcpDnsServerAddress_ip``,
            ``DhcpDnsServerAddress2_ip``, ``DhcpDomainName_str``, ``SaveLog_bool``,
            ``ApplyDhcpPushRoutes_bool``, ``DhcpPushRoutes_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetSecureNATOption", {
            "RpcHubName_str": rpc_hub_name,
            "MacAddress_bin": mac_address,
            "Ip_ip": ip,
            "Mask_ip": mask,
            "UseNat_bool": use_nat,
            "Mtu_u32": mtu,
            "NatTcpTimeout_u32": nat_tcp_timeout,
            "NatUdpTimeout_u32": nat_udp_timeout,
            "UseDhcp_bool": use_dhcp,
            "DhcpLeaseIPStart_ip": dhcp_lease_ip_start,
            "DhcpLeaseIPEnd_ip": dhcp_lease_ip_end,
            "DhcpSubnetMask_ip": dhcp_subnet_mask,
            "DhcpExpireTimeSpan_u32": dhcp_expire_time_span,
            "DhcpGatewayAddress_ip": dhcp_gateway_address,
            "DhcpDnsServerAddress_ip": dhcp_dns_server_address,
            "DhcpDnsServerAddress2_ip": dhcp_dns_server_address2,
            "DhcpDomainName_str": dhcp_domain_name,
            "SaveLog_bool": save_log,
            "ApplyDhcpPushRoutes_bool": apply_dhcp_push_routes,
            "DhcpPushRoutes_str": dhcp_push_routes,
        })

    def get_securenat_option(
        self,
        rpc_hub_name: str,
    ) -> RpcResult:
        """GetSecureNATOption -- Get Settings of SecureNAT Function.

        Get Settings of SecureNAT Function. This API get the registered settings for the
        SecureNAT function which is set by the SetSecureNATOption API.

        Args:
            rpc_hub_name: Target Virtual HUB name (``RpcHubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``RpcHubName_str``, ``MacAddress_bin``, ``Ip_ip``,
            ``Mask_ip``, ``UseNat_bool``, ``Mtu_u32``, ``NatTcpTimeout_u32``,
            ``NatUdpTimeout_u32``, ``UseDhcp_bool``, ``DhcpLeaseIPStart_ip``,
            ``DhcpLeaseIPEnd_ip``, ``DhcpSubnetMask_ip``, ``DhcpExpireTimeSpan_u32``,
            ``DhcpGatewayAddress_ip``, ``DhcpDnsServerAddress_ip``,
            ``DhcpDnsServerAddress2_ip``, ``DhcpDomainName_str``, ``SaveLog_bool``,
            ``ApplyDhcpPushRoutes_bool``, ``DhcpPushRoutes_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetSecureNATOption", {
            "RpcHubName_str": rpc_hub_name,
        })

    def enum_nat(
        self,
        hub_name: str,
    ) -> RpcResult:
        """EnumNAT -- Get Virtual NAT Function Session Table of SecureNAT Function.

        Get Virtual NAT Function Session Table of SecureNAT Function. Use this to get the table
        of TCP and UDP sessions currently communicating via the Virtual NAT (NAT table) in cases
        when the Virtual NAT function is operating on the currently managed Virtual Hub. You
        cannot execute this API for Virtual Hubs of VPN Servers operating as a cluster.

        Args:
            hub_name: Virtual Hub Name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``NatTable``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumNAT", {
            "HubName_str": hub_name,
        })

    def enum_dhcp(
        self,
        hub_name: str,
    ) -> RpcResult:
        """EnumDHCP -- Get Virtual DHCP Server Function Lease Table of SecureNAT Function.

        Get Virtual DHCP Server Function Lease Table of SecureNAT Function. Use this to get the
        lease table of IP addresses, held by the Virtual DHCP Server, that are assigned to
        clients in cases when the Virtual NAT function is operating on the currently managed
        Virtual Hub. You cannot execute this API for Virtual Hubs of VPN Servers operating as a
        cluster.

        Args:
            hub_name: Virtual Hub Name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``DhcpTable``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumDHCP", {
            "HubName_str": hub_name,
        })

    def get_securenat_status(
        self,
        hub_name: str,
    ) -> RpcResult:
        """GetSecureNATStatus -- Get the Operating Status of the Virtual NAT and DHCP Server Function (SecureNAT Function).

        Get the Operating Status of the Virtual NAT and DHCP Server Function (SecureNAT
        Function). Use this to get the operating status of the Virtual NAT and DHCP Server
        function (SecureNAT Function) when it is operating on the currently managed Virtual Hub.
        You cannot execute this API for Virtual Hubs of VPN Servers operating as a cluster.

        Args:
            hub_name: Virtual Hub Name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``NumTcpSessions_u32``,
            ``NumUdpSessions_u32``, ``NumIcmpSessions_u32``, ``NumDnsSessions_u32``,
            ``NumDhcpClients_u32``, ``IsKernelMode_bool``, ``IsRawIpMode_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetSecureNATStatus", {
            "HubName_str": hub_name,
        })

    def enum_ethernet(
        self,
    ) -> RpcResult:
        """EnumEthernet -- Get List of Network Adapters Usable as Local Bridge.

        Get List of Network Adapters Usable as Local Bridge. Use this to get a list of Ethernet
        devices (network adapters) that can be used as a bridge destination device as part of a
        Local Bridge connection. If possible, network connection name is displayed. You can use
        a device displayed here by using the AddLocalBridge API. To call this API, you must have
        VPN Server administrator privileges.

        Returns:
            RpcResult with the fields: ``EthList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumEthernet", {})

    def add_local_bridge(
        self,
        device_name: str,
        hub_name_lb: str,
    ) -> RpcResult:
        """AddLocalBridge -- Create Local Bridge Connection.

        Create Local Bridge Connection. Use this to create a new Local Bridge connection on the
        VPN Server. By using a Local Bridge, you can configure a Layer 2 bridge connection
        between a Virtual Hub operating on this VPN server and a physical Ethernet Device
        (Network Adapter). You can create a tap device (virtual network interface) on the system
        and connect a bridge between Virtual Hubs (the tap device is only supported by Linux
        versions). It is possible to establish a bridge to an operating network adapter of your
        choice for the bridge destination Ethernet device (network adapter), but in high load
        environments, we recommend you prepare a network adapter dedicated to serve as a bridge.
        To call this API, you must have VPN Server administrator privileges.

        .. note::
           Fields left at ``None`` are not sent, and the VPN Server applies its own default for
           each of them.

        Args:
            device_name: Physical Ethernet device name (``DeviceName_str``, string (ASCII))
            hub_name_lb: The Virtual Hub name (``HubNameLB_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``DeviceName_str``, ``HubNameLB_str``, ``Online_bool``,
            ``Active_bool``, ``TapMode_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("AddLocalBridge", {
            "DeviceName_str": device_name,
            "HubNameLB_str": hub_name_lb,
        })

    def delete_local_bridge(
        self,
        device_name: str,
        hub_name_lb: str,
    ) -> RpcResult:
        """DeleteLocalBridge -- Delete Local Bridge Connection.

        Delete Local Bridge Connection. Use this to delete an existing Local Bridge connection.
        To get a list of current Local Bridge connections use the EnumLocalBridge API. To call
        this API, you must have VPN Server administrator privileges.

        Args:
            device_name: Physical Ethernet device name (``DeviceName_str``, string (ASCII))
            hub_name_lb: The Virtual Hub name (``HubNameLB_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``DeviceName_str``, ``HubNameLB_str``, ``Online_bool``,
            ``Active_bool``, ``TapMode_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("DeleteLocalBridge", {
            "DeviceName_str": device_name,
            "HubNameLB_str": hub_name_lb,
        })

    def enum_local_bridge(
        self,
    ) -> RpcResult:
        """EnumLocalBridge -- Get List of Local Bridge Connection.

        Get List of Local Bridge Connection. Use this to get a list of the currently defined
        Local Bridge connections. You can get the Local Bridge connection Virtual Hub name and
        the bridge destination Ethernet device (network adapter) name or tap device name, as
        well as the operating status.

        Returns:
            RpcResult with the fields: ``LocalBridgeList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumLocalBridge", {})

    def get_bridge_support(
        self,
    ) -> RpcResult:
        """GetBridgeSupport -- Get whether the localbridge function is supported on the current system.

        Get whether the localbridge function is supported on the current system.

        Returns:
            RpcResult with the fields: ``IsBridgeSupportedOs_bool``, ``IsWinPcapNeeded_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetBridgeSupport", {})

    def reboot_server(
        self,
    ) -> RpcResult:
        """RebootServer -- Reboot VPN Server Service.

        Reboot VPN Server Service. Use this to restart the VPN Server service. When you restart
        the VPN Server, all currently connected sessions and TCP connections will be
        disconnected and no new connections will be accepted until the restart process has
        completed. By using this API, only the VPN Server service program will be restarted and
        the physical computer that VPN Server is operating on does not restart. This management
        session will also be disconnected, so you will need to reconnect to continue management.
        Also, by specifying the "IntValue" parameter to "1", the contents of the configuration
        file (.config) held by the current VPN Server will be initialized. To call this API, you
        must have VPN Server administrator privileges.

        Returns:
            RpcResult with the fields: ``IntValue_u32``, ``Int64Value_u64``, ``StrValue_str``,
            ``UniStrValue_utf``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("RebootServer", {})

    def get_caps(
        self,
    ) -> RpcResult:
        """GetCaps -- Get List of Server Functions / Capability.

        Get List of Server Functions / Capability. Use this get a list of functions and
        capability of the VPN Server currently connected and being managed. The function and
        capability of VPN Servers are different depending on the operating VPN server's edition
        and version. Using this API, you can find out the capability of the target VPN Server
        and report it.

        Returns:
            RpcResult with the fields: ``CapsList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetCaps", {})

    def get_config(
        self,
    ) -> RpcResult:
        """GetConfig -- Get the current configuration of the VPN Server.

        Get the current configuration of the VPN Server. Use this to get a text file (.config
        file) that contains the current configuration contents of the VPN server. You can get
        the status on the VPN Server at the instant this API is executed. You can edit the
        configuration file by using a regular text editor. To write an edited configuration to
        the VPN Server, use the SetConfig API. To call this API, you must have VPN Server
        administrator privileges.

        Returns:
            RpcResult with the fields: ``FileName_str``, ``FileData_bin``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetConfig", {})

    def set_config(
        self,
        file_data: Union[bytes, bytearray, str],
    ) -> RpcResult:
        """SetConfig -- Write Configuration File to VPN Server.

        Write Configuration File to VPN Server. Use this to write the configuration file to the
        VPN Server. By executing this API, the contents of the specified configuration file will
        be applied to the VPN Server and the VPN Server program will automatically restart and
        upon restart, operate according to the new configuration contents. Because it is
        difficult for an administrator to write all the contents of a configuration file, we
        recommend you use the GetConfig API to get the current contents of the VPN Server
        configuration and save it to file. You can then edit these contents in a regular text
        editor and then use the SetConfig API to rewrite the contents to the VPN Server. This
        API is for people with a detailed knowledge of the VPN Server and if an incorrectly
        configured configuration file is written to the VPN Server, it not only could cause
        errors, it could also result in the lost of the current setting data. Take special care
        when carrying out this action. To call this API, you must have VPN Server administrator
        privileges.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            file_data: File data (``FileData_bin``, string (Base64 binary))

        Returns:
            RpcResult with the fields: ``FileName_str``, ``FileData_bin``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetConfig", {
            "FileData_bin": file_data,
        })

    def get_default_hub_admin_options(
        self,
        hub_name: str,
    ) -> RpcResult:
        """GetDefaultHubAdminOptions -- Get Virtual Hub Administration Option default values.

        Get Virtual Hub Administration Option default values.

        Args:
            hub_name: Virtual HUB name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``AdminOptionList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetDefaultHubAdminOptions", {
            "HubName_str": hub_name,
        })

    def get_hub_admin_options(
        self,
        hub_name: str,
    ) -> RpcResult:
        """GetHubAdminOptions -- Get List of Virtual Hub Administration Options.

        Get List of Virtual Hub Administration Options. Use this to get a list of Virtual Hub
        administration options that are set on the currently managed Virtual Hub. The purpose of
        the Virtual Hub administration options is for the VPN Server Administrator to set limits
        for the setting ranges when the administration of the Virtual Hub is to be trusted to
        each Virtual Hub administrator. Only an administrator with administration privileges for
        this entire VPN Server is able to add, edit and delete the Virtual Hub administration
        options. The Virtual Hub administrators are unable to make changes to the administration
        options, however they are able to view them. There is an exception however. If
        allow_hub_admin_change_option is set to "1", even Virtual Hub administrators are able to
        edit the administration options. This API cannot be invoked on VPN Bridge. You cannot
        execute this API for Virtual Hubs of VPN Servers operating as a cluster member.

        Args:
            hub_name: Virtual HUB name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``AdminOptionList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetHubAdminOptions", {
            "HubName_str": hub_name,
        })

    def set_hub_admin_options(
        self,
        hub_name: str,
        admin_option_list: Union[Mapping[str, Any], Sequence[Mapping[str, Any]]],
    ) -> RpcResult:
        """SetHubAdminOptions -- Set Values of Virtual Hub Administration Options.

        Set Values of Virtual Hub Administration Options. Use this to change the values of
        Virtual Hub administration options that are set on the currently managed Virtual Hub.
        The purpose of the Virtual Hub administration options is for the VPN Server
        Administrator to set limits for the setting ranges when the administration of the
        Virtual Hub is to be trusted to each Virtual Hub administrator. Only an administrator
        with administration privileges for this entire VPN Server is able to add, edit and
        delete the Virtual Hub administration options. The Virtual Hub administrators are unable
        to make changes to the administration options, however they are able to view them. There
        is an exception however. If allow_hub_admin_change_option is set to "1", even Virtual
        Hub administrators are able to edit the administration options. This API cannot be
        invoked on VPN Bridge. You cannot execute this API for Virtual Hubs of VPN Servers
        operating as a cluster member.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            hub_name: Virtual HUB name (``HubName_str``, string (ASCII))
            admin_option_list: List data (``AdminOptionList``, Array object)
                A mapping, or a sequence of mappings, built with :func:`admin_option`; keys may
                be wire names, names without the type suffix, or snake_case.

        Returns:
            RpcResult with the fields: ``HubName_str``, ``AdminOptionList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetHubAdminOptions", {
            "HubName_str": hub_name,
            "AdminOptionList": admin_option_list,
        })

    def get_hub_ext_options(
        self,
        hub_name: str,
    ) -> RpcResult:
        """GetHubExtOptions -- Get List of Virtual Hub Extended Options.

        Get List of Virtual Hub Extended Options. Use this to get a Virtual Hub Extended Options
        List that is set on the currently managed Virtual Hub. Virtual Hub Extended Option
        enables you to configure more detail settings of the Virtual Hub. By default, both VPN
        Server's global administrators and individual Virtual Hub's administrators can modify
        the Virtual Hub Extended Options. However, if the deny_hub_admin_change_ext_option is
        set to 1 on the Virtual Hub Admin Options, the individual Virtual Hub's administrators
        cannot modify the Virtual Hub Extended Options. This API cannot be invoked on VPN
        Bridge. You cannot execute this API for Virtual Hubs of VPN Servers operating as a
        cluster member.

        Args:
            hub_name: Virtual HUB name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``AdminOptionList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetHubExtOptions", {
            "HubName_str": hub_name,
        })

    def set_hub_ext_options(
        self,
        hub_name: str,
        admin_option_list: Union[Mapping[str, Any], Sequence[Mapping[str, Any]]],
    ) -> RpcResult:
        """SetHubExtOptions -- Set a Value of Virtual Hub Extended Options.

        Set a Value of Virtual Hub Extended Options. Use this to set a value in the Virtual Hub
        Extended Options List that is set on the currently managed Virtual Hub. Virtual Hub
        Extended Option enables you to configure more detail settings of the Virtual Hub. By
        default, both VPN Server's global administrators and individual Virtual Hub's
        administrators can modify the Virtual Hub Extended Options. However, if the
        deny_hub_admin_change_ext_option is set to 1 on the Virtual Hub Admin Options, the
        individual Virtual Hub's administrators cannot modify the Virtual Hub Extended Options.
        This API cannot be invoked on VPN Bridge. You cannot execute this API for Virtual Hubs
        of VPN Servers operating as a cluster member.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            hub_name: Virtual HUB name (``HubName_str``, string (ASCII))
            admin_option_list: List data (``AdminOptionList``, Array object)
                A mapping, or a sequence of mappings, built with :func:`admin_option`; keys may
                be wire names, names without the type suffix, or snake_case.

        Returns:
            RpcResult with the fields: ``HubName_str``, ``AdminOptionList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetHubExtOptions", {
            "HubName_str": hub_name,
            "AdminOptionList": admin_option_list,
        })

    def add_l3_switch(
        self,
        name: str,
    ) -> RpcResult:
        """AddL3Switch -- Define New Virtual Layer 3 Switch.

        Define New Virtual Layer 3 Switch. Use this to define a new Virtual Layer 3 Switch on
        the VPN Server. To call this API, you must have VPN Server administrator privileges.
        Also, this API does not operate on VPN Bridge. [Explanation on Virtual Layer 3 Switch
        Function] You can define Virtual Layer 3 Switches between multiple Virtual Hubs
        operating on this VPN Server and configure routing between different IP networks.
        [Caution about the Virtual Layer 3 Switch Function] The Virtual Layer 3 Switch functions
        are provided for network administrators and other people who know a lot about networks
        and IP routing. If you are using the regular VPN functions, you do not need to use the
        Virtual Layer 3 Switch functions. If the Virtual Layer 3 Switch functions are to be
        used, the person who configures them must have sufficient knowledge of IP routing and be
        perfectly capable of not impacting the network.

        .. note::
           Fields left at ``None`` are not sent, and the VPN Server applies its own default for
           each of them.

        Args:
            name: Layer-3 Switch name (``Name_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``Name_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("AddL3Switch", {
            "Name_str": name,
        })

    def del_l3_switch(
        self,
        name: str,
    ) -> RpcResult:
        """DelL3Switch -- Delete Virtual Layer 3 Switch.

        Delete Virtual Layer 3 Switch. Use this to delete an existing Virtual Layer 3 Switch
        that is defined on the VPN Server. When the specified Virtual Layer 3 Switch is
        operating, it will be automatically deleted after operation stops. To get a list of
        existing Virtual Layer 3 Switches, use the EnumL3Switch API. To call this API, you must
        have VPN Server administrator privileges. Also, this API does not operate on VPN Bridge.

        Args:
            name: Layer-3 Switch name (``Name_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``Name_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("DelL3Switch", {
            "Name_str": name,
        })

    def enum_l3_switch(
        self,
    ) -> RpcResult:
        """EnumL3Switch -- Get List of Virtual Layer 3 Switches.

        Get List of Virtual Layer 3 Switches. Use this to define a new Virtual Layer 3 Switch on
        the VPN Server. To call this API, you must have VPN Server administrator privileges.
        Also, this API does not operate on VPN Bridge. [Explanation on Virtual Layer 3 Switch
        Function] You can define Virtual Layer 3 Switches between multiple Virtual Hubs
        operating on this VPN Server and configure routing between different IP networks.
        [Caution about the Virtual Layer 3 Switch Function] The Virtual Layer 3 Switch functions
        are provided for network administrators and other people who know a lot about networks
        and IP routing. If you are using the regular VPN functions, you do not need to use the
        Virtual Layer 3 Switch functions. If the Virtual Layer 3 Switch functions are to be
        used, the person who configures them must have sufficient knowledge of IP routing and be
        perfectly capable of not impacting the network.

        Returns:
            RpcResult with the fields: ``L3SWList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumL3Switch", {})

    def start_l3_switch(
        self,
        name: str,
    ) -> RpcResult:
        """StartL3Switch -- Start Virtual Layer 3 Switch Operation.

        Start Virtual Layer 3 Switch Operation. Use this to start the operation of an existing
        Virtual Layer 3 Switch defined on the VPN Server whose operation is currently stopped.
        To get a list of existing Virtual Layer 3 Switches, use the EnumL3Switch API. To call
        this API, you must have VPN Server administrator privileges. Also, this API does not
        operate on VPN Bridge. [Explanation on Virtual Layer 3 Switch Function] You can define
        Virtual Layer 3 Switches between multiple Virtual Hubs operating on this VPN Server and
        configure routing between different IP networks. [Caution about the Virtual Layer 3
        Switch Function] The Virtual Layer 3 Switch functions are provided for network
        administrators and other people who know a lot about networks and IP routing. If you are
        using the regular VPN functions, you do not need to use the Virtual Layer 3 Switch
        functions. If the Virtual Layer 3 Switch functions are to be used, the person who
        configures them must have sufficient knowledge of IP routing and be perfectly capable of
        not impacting the network.

        Args:
            name: Layer-3 Switch name (``Name_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``Name_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("StartL3Switch", {
            "Name_str": name,
        })

    def stop_l3_switch(
        self,
        name: str,
    ) -> RpcResult:
        """StopL3Switch -- Stop Virtual Layer 3 Switch Operation.

        Stop Virtual Layer 3 Switch Operation. Use this to stop the operation of an existing
        Virtual Layer 3 Switch defined on the VPN Server whose operation is currently operating.
        To get a list of existing Virtual Layer 3 Switches, use the EnumL3Switch API. To call
        this API, you must have VPN Server administrator privileges.

        Args:
            name: Layer-3 Switch name (``Name_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``Name_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("StopL3Switch", {
            "Name_str": name,
        })

    def add_l3_if(
        self,
        name: str,
        hub_name: str,
        ip_address: str,
        subnet_mask: str,
    ) -> RpcResult:
        """AddL3If -- Add Virtual Interface to Virtual Layer 3 Switch.

        Add Virtual Interface to Virtual Layer 3 Switch. Use this to add to a specified Virtual
        Layer 3 Switch, a virtual interface that connects to a Virtual Hub operating on the same
        VPN Server. You can define multiple virtual interfaces and routing tables for a single
        Virtual Layer 3 Switch. A virtual interface is associated to a virtual Hub and operates
        as a single IP host on the Virtual Hub when that Virtual Hub is operating. When multiple
        virtual interfaces that respectively belong to a different IP network of a different
        Virtual Hub are defined, IP routing will be automatically performed between these
        interfaces. You must define the IP network space that the virtual interface belongs to
        and the IP address of the interface itself. Also, you must specify the name of the
        Virtual Hub that the interface will connect to. You can specify a Virtual Hub that
        currently doesn't exist for the Virtual Hub name. The virtual interface must have one IP
        address in the Virtual Hub. You also must specify the subnet mask of an IP network that
        the IP address belongs to. Routing via the Virtual Layer 3 Switches of IP spaces of
        multiple virtual Hubs operates based on the IP address is specified here. To call this
        API, you must have VPN Server administrator privileges. Also, this API does not operate
        on VPN Bridge. To execute this API, the target Virtual Layer 3 Switch must be stopped.
        If it is not stopped, first use the StopL3Switch API to stop it and then execute this
        API.

        .. note::
           Fields left at ``None`` are not sent, and the VPN Server applies its own default for
           each of them.

        Args:
            name: L3 switch name (``Name_str``, string (ASCII))
            hub_name: Virtual HUB name (``HubName_str``, string (ASCII))
            ip_address: IP address (``IpAddress_ip``, string (IP address))
            subnet_mask: Subnet mask (``SubnetMask_ip``, string (IP address))

        Returns:
            RpcResult with the fields: ``Name_str``, ``HubName_str``, ``IpAddress_ip``,
            ``SubnetMask_ip``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("AddL3If", {
            "Name_str": name,
            "HubName_str": hub_name,
            "IpAddress_ip": ip_address,
            "SubnetMask_ip": subnet_mask,
        })

    def del_l3_if(
        self,
        name: str,
        hub_name: str,
    ) -> RpcResult:
        """DelL3If -- Delete Virtual Interface of Virtual Layer 3 Switch.

        Delete Virtual Interface of Virtual Layer 3 Switch. Use this to delete a virtual
        interface already defined in the specified Virtual Layer 3 Switch. You can get a list of
        the virtual interfaces currently defined, by using the EnumL3If API. To call this API,
        you must have VPN Server administrator privileges. Also, this API does not operate on
        VPN Bridge. To execute this API, the target Virtual Layer 3 Switch must be stopped. If
        it is not stopped, first use the StopL3Switch API to stop it and then execute this API.

        Args:
            name: L3 switch name (``Name_str``, string (ASCII))
            hub_name: Virtual HUB name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``Name_str``, ``HubName_str``, ``IpAddress_ip``,
            ``SubnetMask_ip``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("DelL3If", {
            "Name_str": name,
            "HubName_str": hub_name,
        })

    def enum_l3_if(
        self,
        name: str,
    ) -> RpcResult:
        """EnumL3If -- Get List of Interfaces Registered on the Virtual Layer 3 Switch.

        Get List of Interfaces Registered on the Virtual Layer 3 Switch. Use this to get a list
        of virtual interfaces when virtual interfaces have been defined on a specified Virtual
        Layer 3 Switch. You can define multiple virtual interfaces and routing tables for a
        single Virtual Layer 3 Switch. A virtual interface is associated to a virtual Hub and
        operates as a single IP host on the Virtual Hub when that Virtual Hub is operating. When
        multiple virtual interfaces that respectively belong to a different IP network of a
        different Virtual Hub are defined, IP routing will be automatically performed between
        these interfaces. To call this API, you must have VPN Server administrator privileges.
        Also, this API does not operate on VPN Bridge.

        Args:
            name: L3 switch name (``Name_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``Name_str``, ``L3IFList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumL3If", {
            "Name_str": name,
        })

    def add_l3_table(
        self,
        name: str,
        network_address: str,
        subnet_mask: str,
        gateway_address: str,
        *,
        metric: Optional[int] = None,
    ) -> RpcResult:
        """AddL3Table -- Add Routing Table Entry for Virtual Layer 3 Switch.

        Add Routing Table Entry for Virtual Layer 3 Switch. Here you can add a new routing table
        entry to the routing table of the specified Virtual Layer 3 Switch. If the destination
        IP address of the IP packet does not belong to any IP network that belongs to a virtual
        interface, the IP routing engine of the Virtual Layer 3 Switch will reference the
        routing table and execute routing. You must specify the contents of the routing table
        entry to be added to the Virtual Layer 3 Switch. You must specify any IP address that
        belongs to the same IP network in the virtual interface of this Virtual Layer 3 Switch
        as the gateway address. To call this API, you must have VPN Server administrator
        privileges. Also, this API does not operate on VPN Bridge. To execute this API, the
        target Virtual Layer 3 Switch must be stopped. If it is not stopped, first use the
        StopL3Switch API to stop it and then execute this API.

        .. note::
           Fields left at ``None`` are not sent, and the VPN Server applies its own default for
           each of them.

        Args:
            name: L3 switch name (``Name_str``, string (ASCII))
            network_address: Network address (``NetworkAddress_ip``, string (IP address))
            subnet_mask: Subnet mask (``SubnetMask_ip``, string (IP address))
            gateway_address: Gateway address (``GatewayAddress_ip``, string (IP address))
            metric: Metric (``Metric_u32``, number (uint32))

        Returns:
            RpcResult with the fields: ``Name_str``, ``NetworkAddress_ip``, ``SubnetMask_ip``,
            ``GatewayAddress_ip``, ``Metric_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("AddL3Table", {
            "Name_str": name,
            "NetworkAddress_ip": network_address,
            "SubnetMask_ip": subnet_mask,
            "GatewayAddress_ip": gateway_address,
            "Metric_u32": metric,
        })

    def del_l3_table(
        self,
        name: str,
        network_address: str,
        subnet_mask: str,
        gateway_address: str,
        *,
        metric: Optional[int] = None,
    ) -> RpcResult:
        """DelL3Table -- Delete Routing Table Entry of Virtual Layer 3 Switch.

        Delete Routing Table Entry of Virtual Layer 3 Switch. Use this to delete a routing table
        entry that is defined in the specified Virtual Layer 3 Switch. You can get a list of the
        already defined routing table entries by using the EnumL3Table API. To call this API,
        you must have VPN Server administrator privileges. Also, this API does not operate on
        VPN Bridge. To execute this API, the target Virtual Layer 3 Switch must be stopped. If
        it is not stopped, first use the StopL3Switch API to stop it and then execute this API.

        Args:
            name: L3 switch name (``Name_str``, string (ASCII))
            network_address: Network address (``NetworkAddress_ip``, string (IP address))
            subnet_mask: Subnet mask (``SubnetMask_ip``, string (IP address))
            gateway_address: Gateway address (``GatewayAddress_ip``, string (IP address))
            metric: Metric (``Metric_u32``, number (uint32))

        Returns:
            RpcResult with the fields: ``Name_str``, ``NetworkAddress_ip``, ``SubnetMask_ip``,
            ``GatewayAddress_ip``, ``Metric_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("DelL3Table", {
            "Name_str": name,
            "NetworkAddress_ip": network_address,
            "SubnetMask_ip": subnet_mask,
            "GatewayAddress_ip": gateway_address,
            "Metric_u32": metric,
        })

    def enum_l3_table(
        self,
        name: str,
    ) -> RpcResult:
        """EnumL3Table -- Get List of Routing Tables of Virtual Layer 3 Switch.

        Get List of Routing Tables of Virtual Layer 3 Switch. Use this to get a list of routing
        tables when routing tables have been defined on a specified Virtual Layer 3 Switch. If
        the destination IP address of the IP packet does not belong to any IP network that
        belongs to a virtual interface, the IP routing engine of the Virtual Layer 3 Switch will
        reference this routing table and execute routing. To call this API, you must have VPN
        Server administrator privileges. Also, this API does not operate on VPN Bridge.

        Args:
            name: L3 switch name (``Name_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``Name_str``, ``L3Table``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumL3Table", {
            "Name_str": name,
        })

    def enum_crl(
        self,
        hub_name: str,
    ) -> RpcResult:
        """EnumCrl -- Get List of Certificates Revocation List.

        Get List of Certificates Revocation List. Use this to get a Certificates Revocation List
        that is set on the currently managed Virtual Hub. By registering certificates in the
        Certificates Revocation List, the clients who provide these certificates will be unable
        to connect to this Virtual Hub using certificate authentication mode. Normally with this
        function, in cases where the security of a private key has been compromised or where a
        person holding a certificate has been stripped of their privileges, by registering that
        certificate as invalid on the Virtual Hub, it is possible to deny user authentication
        when that certificate is used by a client to connect to the Virtual Hub. This API cannot
        be invoked on VPN Bridge. You cannot execute this API for Virtual Hubs of VPN Servers
        operating as a cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``CRLList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumCrl", {
            "HubName_str": hub_name,
        })

    def add_crl(
        self,
        hub_name: str,
        *,
        common_name: Optional[str] = None,
        organization: Optional[str] = None,
        unit: Optional[str] = None,
        country: Optional[str] = None,
        state: Optional[str] = None,
        local: Optional[str] = None,
        serial: Optional[Union[bytes, bytearray, str]] = None,
        digest_md5: Optional[Union[bytes, bytearray, str]] = None,
        digest_sha1: Optional[Union[bytes, bytearray, str]] = None,
    ) -> RpcResult:
        """AddCrl -- Add a Revoked Certificate.

        Add a Revoked Certificate. Use this to add a new revoked certificate definition in the
        Certificate Revocation List that is set on the currently managed Virtual Hub. Specify
        the contents to be registered in the Certificate Revocation List by using the parameters
        of this API. When a user connects to a Virtual Hub in certificate authentication mode
        and that certificate matches 1 or more of the contents registered in the certificates
        revocation list, the user is denied connection. A certificate that matches all the
        conditions that are defined by the parameters specified by this API will be judged as
        invalid. The items that can be set are as follows: Name (CN), Organization (O),
        Organization Unit (OU), Country (C), State (ST), Locale (L), Serial Number
        (hexadecimal), MD5 Digest Value (hexadecimal, 128 bit), and SHA-1 Digest Value
        (hexadecimal, 160 bit). For the specification of a digest value (hash value) a
        certificate is optionally specified depending on the circumstances. Normally when a MD5
        or SHA-1 digest value is input, it is not necessary to input the other items. This API
        cannot be invoked on VPN Bridge. You cannot execute this API for Virtual Hubs of VPN
        Servers operating as a cluster.

        .. note::
           Fields left at ``None`` are not sent, and the VPN Server applies its own default for
           each of them.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            common_name: CN, optional (``CommonName_utf``, string (UTF8))
            organization: O, optional (``Organization_utf``, string (UTF8))
            unit: OU, optional (``Unit_utf``, string (UTF8))
            country: C, optional (``Country_utf``, string (UTF8))
            state: ST, optional (``State_utf``, string (UTF8))
            local: L, optional (``Local_utf``, string (UTF8))
            serial: Serial, optional (``Serial_bin``, string (Base64 binary))
            digest_md5: MD5 Digest, optional (``DigestMD5_bin``, string (Base64 binary))
            digest_sha1: SHA1 Digest, optional (``DigestSHA1_bin``, string (Base64 binary))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Key_u32``, ``CommonName_utf``,
            ``Organization_utf``, ``Unit_utf``, ``Country_utf``, ``State_utf``, ``Local_utf``,
            ``Serial_bin``, ``DigestMD5_bin``, ``DigestSHA1_bin``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("AddCrl", {
            "HubName_str": hub_name,
            "CommonName_utf": common_name,
            "Organization_utf": organization,
            "Unit_utf": unit,
            "Country_utf": country,
            "State_utf": state,
            "Local_utf": local,
            "Serial_bin": serial,
            "DigestMD5_bin": digest_md5,
            "DigestSHA1_bin": digest_sha1,
        })

    def del_crl(
        self,
        hub_name: str,
        key: int,
    ) -> RpcResult:
        """DelCrl -- Delete a Revoked Certificate.

        Delete a Revoked Certificate. Use this to specify and delete a revoked certificate
        definition from the certificate revocation list that is set on the currently managed
        Virtual Hub. To get the list of currently registered revoked certificate definitions,
        use the EnumCrl API. This API cannot be invoked on VPN Bridge. You cannot execute this
        API for Virtual Hubs of VPN Servers operating as a cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            key: Key ID (``Key_u32``, number (uint32))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Key_u32``, ``CommonName_utf``,
            ``Organization_utf``, ``Unit_utf``, ``Country_utf``, ``State_utf``, ``Local_utf``,
            ``Serial_bin``, ``DigestMD5_bin``, ``DigestSHA1_bin``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("DelCrl", {
            "HubName_str": hub_name,
            "Key_u32": key,
        })

    def get_crl(
        self,
        hub_name: str,
        key: int,
    ) -> RpcResult:
        """GetCrl -- Get a Revoked Certificate.

        Get a Revoked Certificate. Use this to specify and get the contents of a revoked
        certificate definition from the Certificates Revocation List that is set on the
        currently managed Virtual Hub. To get the list of currently registered revoked
        certificate definitions, use the EnumCrl API. This API cannot be invoked on VPN Bridge.
        You cannot execute this API for Virtual Hubs of VPN Servers operating as a cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            key: Key ID (``Key_u32``, number (uint32))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Key_u32``, ``CommonName_utf``,
            ``Organization_utf``, ``Unit_utf``, ``Country_utf``, ``State_utf``, ``Local_utf``,
            ``Serial_bin``, ``DigestMD5_bin``, ``DigestSHA1_bin``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetCrl", {
            "HubName_str": hub_name,
            "Key_u32": key,
        })

    def set_crl(
        self,
        hub_name: str,
        key: int,
        *,
        common_name: Optional[str] = None,
        organization: Optional[str] = None,
        unit: Optional[str] = None,
        country: Optional[str] = None,
        state: Optional[str] = None,
        local: Optional[str] = None,
        serial: Optional[Union[bytes, bytearray, str]] = None,
        digest_md5: Optional[Union[bytes, bytearray, str]] = None,
        digest_sha1: Optional[Union[bytes, bytearray, str]] = None,
    ) -> RpcResult:
        """SetCrl -- Change Existing CRL (Certificate Revocation List) Entry.

        Change Existing CRL (Certificate Revocation List) Entry. Use this to alter an existing
        revoked certificate definition in the Certificate Revocation List that is set on the
        currently managed Virtual Hub. Specify the contents to be registered in the Certificate
        Revocation List by using the parameters of this API. When a user connects to a Virtual
        Hub in certificate authentication mode and that certificate matches 1 or more of the
        contents registered in the certificates revocation list, the user is denied connection.
        A certificate that matches all the conditions that are defined by the parameters
        specified by this API will be judged as invalid. The items that can be set are as
        follows: Name (CN), Organization (O), Organization Unit (OU), Country (C), State (ST),
        Locale (L), Serial Number (hexadecimal), MD5 Digest Value (hexadecimal, 128 bit), and
        SHA-1 Digest Value (hexadecimal, 160 bit). For the specification of a digest value (hash
        value) a certificate is optionally specified depending on the circumstances. Normally
        when a MD5 or SHA-1 digest value is input, it is not necessary to input the other items.
        This API cannot be invoked on VPN Bridge. You cannot execute this API for Virtual Hubs
        of VPN Servers operating as a cluster.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Note: the published document shows an empty parameter object for this call; the fields
        below are the ones it documents in the result and the ones the server expects.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            key: Key ID (``Key_u32``, number (uint32))
            common_name: CN, optional (``CommonName_utf``, string (UTF8))
            organization: O, optional (``Organization_utf``, string (UTF8))
            unit: OU, optional (``Unit_utf``, string (UTF8))
            country: C, optional (``Country_utf``, string (UTF8))
            state: ST, optional (``State_utf``, string (UTF8))
            local: L, optional (``Local_utf``, string (UTF8))
            serial: Serial, optional (``Serial_bin``, string (Base64 binary))
            digest_md5: MD5 Digest, optional (``DigestMD5_bin``, string (Base64 binary))
            digest_sha1: SHA1 Digest, optional (``DigestSHA1_bin``, string (Base64 binary))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Key_u32``, ``CommonName_utf``,
            ``Organization_utf``, ``Unit_utf``, ``Country_utf``, ``State_utf``, ``Local_utf``,
            ``Serial_bin``, ``DigestMD5_bin``, ``DigestSHA1_bin``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetCrl", {
            "HubName_str": hub_name,
            "Key_u32": key,
            "CommonName_utf": common_name,
            "Organization_utf": organization,
            "Unit_utf": unit,
            "Country_utf": country,
            "State_utf": state,
            "Local_utf": local,
            "Serial_bin": serial,
            "DigestMD5_bin": digest_md5,
            "DigestSHA1_bin": digest_sha1,
        })

    def set_ac_list(
        self,
        hub_name: str,
        ac_list: Union[Mapping[str, Any], Sequence[Mapping[str, Any]]],
    ) -> RpcResult:
        """SetAcList -- Add Rule to Source IP Address Limit List.

        Add Rule to Source IP Address Limit List. Use this to add a new rule to the Source IP
        Address Limit List that is set on the currently managed Virtual Hub. The items set here
        will be used to decide whether to allow or deny connection from a VPN Client when this
        client attempts connection to the Virtual Hub. You can specify a client IP address, or
        IP address or mask to match the rule as the contents of the rule item. By specifying an
        IP address only, there will only be one specified computer that will match the rule, but
        by specifying an IP net mask address or subnet mask address, all the computers in the
        range of that subnet will match the rule. You can specify the priority for the rule. You
        can specify an integer of 1 or greater for the priority and the smaller the number, the
        higher the priority. To get a list of the currently registered Source IP Address Limit
        List, use the GetAcList API. This API cannot be invoked on VPN Bridge. You cannot
        execute this API for Virtual Hubs of VPN Servers operating as a cluster.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            ac_list: Source IP Address Limit List (``ACList``, Array object)
                A mapping, or a sequence of mappings, built with :func:`ac_rule`; keys may be
                wire names, names without the type suffix, or snake_case.

        Returns:
            RpcResult with the fields: ``HubName_str``, ``ACList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetAcList", {
            "HubName_str": hub_name,
            "ACList": ac_list,
        })

    def get_ac_list(
        self,
        hub_name: str,
    ) -> RpcResult:
        """GetAcList -- Get List of Rule Items of Source IP Address Limit List.

        Get List of Rule Items of Source IP Address Limit List. Use this to get a list of Source
        IP Address Limit List rules that is set on the currently managed Virtual Hub. You can
        allow or deny VPN connections to this Virtual Hub according to the client computer's
        source IP address. You can define multiple rules and set a priority for each rule. The
        search proceeds from the rule with the highest order or priority and based on the action
        of the rule that the IP address first matches, the connection from the client is either
        allowed or denied. This API cannot be invoked on VPN Bridge. You cannot execute this API
        for Virtual Hubs of VPN Servers operating as a cluster.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``ACList``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetAcList", {
            "HubName_str": hub_name,
        })

    def enum_log_file(
        self,
    ) -> RpcResult:
        """EnumLogFile -- Get List of Log Files.

        Get List of Log Files. Use this to display a list of log files outputted by the VPN
        Server that have been saved on the VPN Server computer. By specifying a log file file
        name displayed here and calling it using the ReadLogFile API you can download the
        contents of the log file. If you are connected to the VPN Server in server admin mode,
        you can display or download the packet logs and security logs of all Virtual Hubs and
        the server log of the VPN Server. When connected in Virtual Hub Admin Mode, you are able
        to view or download only the packet log and security log of the Virtual Hub that is the
        target of management.

        Returns:
            RpcResult with the fields: ``LogFiles``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumLogFile", {})

    def read_log_file(
        self,
        file_path: str,
    ) -> RpcResult:
        """ReadLogFile -- Download a part of Log File.

        Download a part of Log File. Use this to download the log file that is saved on the VPN
        Server computer. To download the log file first get the list of log files using the
        EnumLogFile API and then download the log file using the ReadLogFile API. If you are
        connected to the VPN Server in server admin mode, you can display or download the packet
        logs and security logs of all Virtual Hubs and the server log of the VPN Server. When
        connected in Virtual Hub Admin Mode, you are able to view or download only the packet
        log and security log of the Virtual Hub that is the target of management.

        Args:
            file_path: File Path (``FilePath_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``ServerName_str``, ``FilePath_str``, ``Offset_u32``,
            ``Buffer_bin``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("ReadLogFile", {
            "FilePath_str": file_path,
        })

    def set_sys_log(
        self,
        port: int,
        *,
        save_type: Optional[Union[int, "SysLogSaveType"]] = None,
        hostname: Optional[str] = None,
    ) -> RpcResult:
        """SetSysLog -- Set syslog Send Function.

        Set syslog Send Function. Use this to set the usage of syslog send function and which
        syslog server to use.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            port: Specify the port number of the syslog server (``Port_u32``, number (uint32))
            save_type: The behavior of the syslog function (``SaveType_u32``, number (enum))
                Accepts an :class:`SysLogSaveType` member or its integer value: 0 = Do not use
                syslog; 1 = Only server log; 2 = Server and Virtual HUB security log; 3 =
                Server, Virtual HUB security, and packet log.
            hostname: Specify the host name or IP address of the syslog server
                (``Hostname_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``SaveType_u32``, ``Hostname_str``, ``Port_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetSysLog", {
            "Port_u32": port,
            "SaveType_u32": save_type,
            "Hostname_str": hostname,
        })

    def get_sys_log(
        self,
    ) -> RpcResult:
        """GetSysLog -- Get syslog Send Function.

        Get syslog Send Function. This allows you to get the current setting contents of the
        syslog send function. You can get the usage setting of the syslog function and the host
        name and port number of the syslog server to use.

        Returns:
            RpcResult with the fields: ``SaveType_u32``, ``Hostname_str``, ``Port_u32``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetSysLog", {})

    def set_hub_msg(
        self,
        hub_name: str,
        msg: Union[bytes, bytearray, str],
    ) -> RpcResult:
        """SetHubMsg -- Set Today's Message of Virtual Hub.

        Set Today's Message of Virtual Hub. The message will be displayed on VPN Client UI when
        a user will establish a connection to the Virtual Hub.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))
            msg: Message (Unicode strings acceptable) (``Msg_bin``, string (Base64 binary))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Msg_bin``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetHubMsg", {
            "HubName_str": hub_name,
            "Msg_bin": msg,
        })

    def get_hub_msg(
        self,
        hub_name: str,
    ) -> RpcResult:
        """GetHubMsg -- Get Today's Message of Virtual Hub.

        Get Today's Message of Virtual Hub. The message will be displayed on VPN Client UI when
        a user will establish a connection to the Virtual Hub.

        Args:
            hub_name: The Virtual Hub name (``HubName_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Msg_bin``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetHubMsg", {
            "HubName_str": hub_name,
        })

    def crash(
        self,
    ) -> RpcResult:
        """Crash -- Raise a vital error on the VPN Server / Bridge to terminate the process forcefully.

        Raise a vital error on the VPN Server / Bridge to terminate the process forcefully. This
        API will raise a fatal error (memory access violation) on the VPN Server / Bridge
        running process in order to crash the process. As the result, VPN Server / Bridge will
        be terminated and restarted if it is running as a service mode. If the VPN Server is
        running as a user mode, the process will not automatically restarted. This API is for a
        situation when the VPN Server / Bridge is under a non-recoverable error or the process
        is in an infinite loop. This API will disconnect all VPN Sessions on the VPN Server /
        Bridge. All unsaved settings in the memory of VPN Server / Bridge will be lost. Before
        run this API, call the Flush API to try to save volatile data to the configuration file.
        To execute this API, you must have VPN Server / VPN Bridge administrator privileges.

        Returns:
            RpcResult with the fields: ``IntValue_u32``, ``Int64Value_u64``, ``StrValue_str``,
            ``UniStrValue_utf``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("Crash", {})

    def get_admin_msg(
        self,
    ) -> RpcResult:
        """GetAdminMsg -- Get the message for administrators.

        Get the message for administrators.

        Returns:
            RpcResult with the fields: ``HubName_str``, ``Msg_bin``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetAdminMsg", {})

    def flush(
        self,
    ) -> RpcResult:
        """Flush -- Save All Volatile Data of VPN Server / Bridge to the Configuration File.

        Save All Volatile Data of VPN Server / Bridge to the Configuration File. The number of
        configuration file bytes will be returned as the "IntValue" parameter. Normally, the VPN
        Server / VPN Bridge retains the volatile configuration data in memory. It is flushed to
        the disk as vpn_server.config or vpn_bridge.config periodically. The period is 300
        seconds (5 minutes) by default. (The period can be altered by modifying the
        AutoSaveConfigSpan item in the configuration file.) The data will be saved on the timing
        of shutting down normally of the VPN Server / Bridge. Execute the Flush API to make the
        VPN Server / Bridge save the settings to the file immediately. The setting data will be
        stored on the disk drive of the server computer. Use the Flush API in a situation that
        you do not have an enough time to shut down the server process normally. To call this
        API, you must have VPN Server administrator privileges. To execute this API, you must
        have VPN Server / VPN Bridge administrator privileges.

        Returns:
            RpcResult with the fields: ``IntValue_u32``, ``Int64Value_u64``, ``StrValue_str``,
            ``UniStrValue_utf``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("Flush", {})

    def set_ipsec_services(
        self,
        *,
        l2tp_raw: Optional[bool] = None,
        l2tp_ipsec: Optional[bool] = None,
        etherip_ipsec: Optional[bool] = None,
        ipsec_secret: Optional[str] = None,
        l2tp_default_hub: Optional[str] = None,
    ) -> RpcResult:
        """SetIPsecServices -- Enable or Disable IPsec VPN Server Function.

        Enable or Disable IPsec VPN Server Function. Enable or Disable IPsec VPN Server Function
        on the VPN Server. If you enable this function, Virtual Hubs on the VPN Server will be
        able to accept Remote-Access VPN connections from L2TP-compatible PCs, Mac OS X and
        Smartphones, and also can accept EtherIP Site-to-Site VPN Connection. VPN Connections
        from Smartphones suchlike iPhone, iPad and Android, and also from native VPN Clients on
        Mac OS X and Windows can be accepted. To call this API, you must have VPN Server
        administrator privileges. This API cannot be invoked on VPN Bridge. You cannot execute
        this API for Virtual Hubs of VPN Servers operating as a cluster.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            l2tp_raw: Enable or Disable the L2TP Server Function (Raw L2TP with No Encryptions).
                To accept special VPN clients, enable this option. (``L2TP_Raw_bool``, boolean)
            l2tp_ipsec: Enable or Disable the L2TP over IPsec Server Function. To accept VPN
                connections from iPhone, iPad, Android, Windows or Mac OS X, enable this option.
                (``L2TP_IPsec_bool``, boolean)
            etherip_ipsec: Enable or Disable the EtherIP / L2TPv3 over IPsec Server Function
                (for site-to-site VPN Server function). Router Products which are compatible with
                EtherIP over IPsec can connect to Virtual Hubs on the VPN Server and establish
                Layer-2 (Ethernet) Bridging. (``EtherIP_IPsec_bool``, boolean)
            ipsec_secret: Specify the IPsec Pre-Shared Key. An IPsec Pre-Shared Key is also
                called as "PSK" or "secret". Specify it equal or less than 8 letters, and distribute
                it to every users who will connect to the VPN Server. Please note: Google Android
                4.0 has a bug which a Pre-Shared Key with 10 or more letters causes a unexpected
                behavior. For that reason, the letters of a Pre-Shared Key should be 9 or less
                characters. (``IPsec_Secret_str``, string (ASCII))
            l2tp_default_hub: Specify the default Virtual HUB in a case of omitting the name of
                HUB on the Username. Users should specify their username such as "Username@Target
                Virtual HUB Name" to connect this L2TP Server. If the designation of the Virtual Hub
                is omitted, the above HUB will be used as the target. (``L2TP_DefaultHub_str``,
                string (ASCII))

        Returns:
            RpcResult with the fields: ``L2TP_Raw_bool``, ``L2TP_IPsec_bool``,
            ``EtherIP_IPsec_bool``, ``IPsec_Secret_str``, ``L2TP_DefaultHub_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetIPsecServices", {
            "L2TP_Raw_bool": l2tp_raw,
            "L2TP_IPsec_bool": l2tp_ipsec,
            "EtherIP_IPsec_bool": etherip_ipsec,
            "IPsec_Secret_str": ipsec_secret,
            "L2TP_DefaultHub_str": l2tp_default_hub,
        })

    def get_ipsec_services(
        self,
    ) -> RpcResult:
        """GetIPsecServices -- Get the Current IPsec VPN Server Settings.

        Get the Current IPsec VPN Server Settings. Get and view the current IPsec VPN Server
        settings on the VPN Server. To call this API, you must have VPN Server administrator
        privileges. This API cannot be invoked on VPN Bridge. You cannot execute this API for
        Virtual Hubs of VPN Servers operating as a cluster.

        Returns:
            RpcResult with the fields: ``L2TP_Raw_bool``, ``L2TP_IPsec_bool``,
            ``EtherIP_IPsec_bool``, ``IPsec_Secret_str``, ``L2TP_DefaultHub_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetIPsecServices", {})

    def add_ether_ip_id(
        self,
        id: str,
        hub_name: str,
        user_name: str,
        password: str,
    ) -> RpcResult:
        """AddEtherIpId -- Add New EtherIP / L2TPv3 over IPsec Client Setting to Accept EthreIP / L2TPv3 Client Devices.

        Add New EtherIP / L2TPv3 over IPsec Client Setting to Accept EthreIP / L2TPv3 Client
        Devices. Add a new setting entry to enable the EtherIP / L2TPv3 over IPsec Server
        Function to accept client devices. In order to accept connections from routers by the
        EtherIP / L2TPv3 over IPsec Server Function, you have to define the relation table
        between an IPsec Phase 1 string which is presented by client devices of EtherIP / L2TPv3
        over IPsec compatible router, and the designation of the destination Virtual Hub. After
        you add a definition entry by AddEtherIpId API, the defined connection setting to the
        Virtual Hub will be applied on the login-attepting session from an EtherIP / L2TPv3 over
        IPsec client device. The username and password in an entry must be registered on the
        Virtual Hub. An EtherIP / L2TPv3 client will be regarded as it connected the Virtual HUB
        with the identification of the above user information. To call this API, you must have
        VPN Server administrator privileges. This API cannot be invoked on VPN Bridge. You
        cannot execute this API for Virtual Hubs of VPN Servers operating as a cluster.

        .. note::
           Fields left at ``None`` are not sent, and the VPN Server applies its own default for
           each of them.

        Args:
            id: Specify an ISAKMP Phase 1 ID. The ID must be exactly same as a ID in the
                configuration of the EtherIP / L2TPv3 Client. You can specify IP address as well as
                characters as ID, if the EtherIP Client uses IP address as Phase 1 ID. If you
                specify '*' (asterisk), it will be a wildcard to match any clients which doesn't
                match other explicit rules. (``Id_str``, string (ASCII))
            hub_name: Specify the name of the Virtual Hub to connect. (``HubName_str``, string
                (ASCII))
            user_name: Specify the username to login to the destination Virtual Hub.
                (``UserName_str``, string (ASCII))
            password: Specify the password to login to the destination Virtual Hub.
                (``Password_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``Id_str``, ``HubName_str``, ``UserName_str``,
            ``Password_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("AddEtherIpId", {
            "Id_str": id,
            "HubName_str": hub_name,
            "UserName_str": user_name,
            "Password_str": password,
        })

    def get_ether_ip_id(
        self,
        id: str,
    ) -> RpcResult:
        """GetEtherIpId -- Get the Current List of EtherIP / L2TPv3 Client Device Entry Definitions.

        Get the Current List of EtherIP / L2TPv3 Client Device Entry Definitions. This API gets
        and shows the list of entries to accept VPN clients by EtherIP / L2TPv3 over IPsec
        Function. To call this API, you must have VPN Server administrator privileges. This API
        cannot be invoked on VPN Bridge. You cannot execute this API for Virtual Hubs of VPN
        Servers operating as a cluster.

        Args:
            id: Specify an ISAKMP Phase 1 ID. The ID must be exactly same as a ID in the
                configuration of the EtherIP / L2TPv3 Client. You can specify IP address as well as
                characters as ID, if the EtherIP Client uses IP address as Phase 1 ID. If you
                specify '*' (asterisk), it will be a wildcard to match any clients which doesn't
                match other explicit rules. (``Id_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``Id_str``, ``HubName_str``, ``UserName_str``,
            ``Password_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetEtherIpId", {
            "Id_str": id,
        })

    def delete_ether_ip_id(
        self,
        id: str,
    ) -> RpcResult:
        """DeleteEtherIpId -- Delete an EtherIP / L2TPv3 over IPsec Client Setting.

        Delete an EtherIP / L2TPv3 over IPsec Client Setting. This API deletes an entry to
        accept VPN clients by EtherIP / L2TPv3 over IPsec Function. To call this API, you must
        have VPN Server administrator privileges. This API cannot be invoked on VPN Bridge. You
        cannot execute this API for Virtual Hubs of VPN Servers operating as a cluster.

        Args:
            id: Specify an ISAKMP Phase 1 ID. The ID must be exactly same as a ID in the
                configuration of the EtherIP / L2TPv3 Client. You can specify IP address as well as
                characters as ID, if the EtherIP Client uses IP address as Phase 1 ID. If you
                specify '*' (asterisk), it will be a wildcard to match any clients which doesn't
                match other explicit rules. (``Id_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``Id_str``, ``HubName_str``, ``UserName_str``,
            ``Password_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("DeleteEtherIpId", {
            "Id_str": id,
        })

    def enum_ether_ip_id(
        self,
    ) -> RpcResult:
        """EnumEtherIpId -- Get the Current List of EtherIP / L2TPv3 Client Device Entry Definitions.

        Get the Current List of EtherIP / L2TPv3 Client Device Entry Definitions. This API gets
        and shows the list of entries to accept VPN clients by EtherIP / L2TPv3 over IPsec
        Function. To call this API, you must have VPN Server administrator privileges. This API
        cannot be invoked on VPN Bridge. You cannot execute this API for Virtual Hubs of VPN
        Servers operating as a cluster.

        Returns:
            RpcResult with the fields: ``Settings``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("EnumEtherIpId", {})

    def set_openvpn_sstp_config(
        self,
        *,
        enable_openvpn: Optional[bool] = None,
        openvpn_port_list: Optional[str] = None,
        enable_sstp: Optional[bool] = None,
    ) -> RpcResult:
        """SetOpenVpnSstpConfig -- Set Settings for OpenVPN Clone Server Function.

        Set Settings for OpenVPN Clone Server Function. The VPN Server has the clone functions
        of OpenVPN software products by OpenVPN Technologies, Inc. Any OpenVPN Clients can
        connect to this VPN Server. The manner to specify a username to connect to the Virtual
        Hub, and the selection rule of default Hub by using this clone server functions are same
        to the IPsec Server functions. To call this API, you must have VPN Server administrator
        privileges. This API cannot be invoked on VPN Bridge. You cannot execute this API for
        Virtual Hubs of VPN Servers operating as a cluster.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            enable_openvpn: Specify true to enable the OpenVPN Clone Server Function. Specify
                false to disable. (``EnableOpenVPN_bool``, boolean)
            openvpn_port_list: Specify UDP ports to listen for OpenVPN. Multiple UDP ports can
                be specified with splitting by space or comma letters, for example: "1194, 2001,
                2010, 2012". The default port for OpenVPN is UDP 1194. You can specify any other UDP
                ports. (``OpenVPNPortList_str``, string (ASCII))
            enable_sstp: pecify true to enable the Microsoft SSTP VPN Clone Server Function.
                Specify false to disable. (``EnableSSTP_bool``, boolean)

        Returns:
            RpcResult with the fields: ``EnableOpenVPN_bool``, ``OpenVPNPortList_str``,
            ``EnableSSTP_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetOpenVpnSstpConfig", {
            "EnableOpenVPN_bool": enable_openvpn,
            "OpenVPNPortList_str": openvpn_port_list,
            "EnableSSTP_bool": enable_sstp,
        })

    def get_openvpn_sstp_config(
        self,
    ) -> RpcResult:
        """GetOpenVpnSstpConfig -- Get the Current Settings of OpenVPN Clone Server Function.

        Get the Current Settings of OpenVPN Clone Server Function. Get and show the current
        settings of OpenVPN Clone Server Function. To call this API, you must have VPN Server
        administrator privileges. This API cannot be invoked on VPN Bridge. You cannot execute
        this API for Virtual Hubs of VPN Servers operating as a cluster.

        Returns:
            RpcResult with the fields: ``EnableOpenVPN_bool``, ``OpenVPNPortList_str``,
            ``EnableSSTP_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetOpenVpnSstpConfig", {})

    def get_ddns_client_status(
        self,
    ) -> RpcResult:
        """GetDDnsClientStatus -- Show the Current Status of Dynamic DNS Function.

        Show the Current Status of Dynamic DNS Function. Get and show the current status of the
        Dynamic DNS function. The Dynamic DNS assigns a unique and permanent DNS hostname for
        this VPN Server. You can use that hostname to specify this VPN Server on the settings
        for VPN Client and VPN Bridge. You need not to register and keep a domain name. Also, if
        your ISP assignes you a dynamic (not-fixed) IP address, the corresponding IP address of
        your Dynamic DNS hostname will be automatically changed. It enables you to keep running
        the VPN Server by using only a dynamic IP address. Therefore, you need not any longer to
        keep static global IP addresses with expenses monthly costs. [Caution] To disable the
        Dynamic DNS Function, modify the configuration file of VPN Server. The "declare root"
        directive has the "declare DDnsClient" directive. In this directive, you can switch
        "bool Disable" from false to true, and reboot the VPN Server, then the Dynamic DNS
        Function will be disabled. To call this API, you must have VPN Server administrator
        privileges. This API cannot be invoked on VPN Bridge.

        Returns:
            RpcResult with the fields: ``Err_IPv4_u32``, ``ErrStr_IPv4_utf``, ``Err_IPv6_u32``,
            ``ErrStr_IPv6_utf``, ``CurrentHostName_str``, ``CurrentFqdn_str``,
            ``DnsSuffix_str``, ``CurrentIPv4_str``, ``CurrentIPv6_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetDDnsClientStatus", {})

    def change_ddns_client_hostname(
        self,
        str_value: str,
    ) -> RpcResult:
        """ChangeDDnsClientHostname -- Set the Dynamic DNS Hostname.

        Set the Dynamic DNS Hostname. You must specify the new hostname on the StrValue_str
        field. You can use this API to change the hostname assigned by the Dynamic DNS function.
        The currently assigned hostname can be showen by the GetDDnsClientStatus API. The
        Dynamic DNS assigns a unique and permanent DNS hostname for this VPN Server. You can use
        that hostname to specify this VPN Server on the settings for VPN Client and VPN Bridge.
        You need not to register and keep a domain name. Also, if your ISP assignes you a
        dynamic (not-fixed) IP address, the corresponding IP address of your Dynamic DNS
        hostname will be automatically changed. It enables you to keep running the VPN Server by
        using only a dynamic IP address. Therefore, you need not any longer to keep static
        global IP addresses with expenses monthly costs. [Caution] To disable the Dynamic DNS
        Function, modify the configuration file of VPN Server. The "declare root" directive has
        the "declare DDnsClient" directive. In this directive, you can switch "bool Disable"
        from false to true, and reboot the VPN Server, then the Dynamic DNS Function will be
        disabled. To call this API, you must have VPN Server administrator privileges. This API
        cannot be invoked on VPN Bridge.

        Args:
            str_value: An Ascii string field (``StrValue_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``IntValue_u32``, ``Int64Value_u64``, ``StrValue_str``,
            ``UniStrValue_utf``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("ChangeDDnsClientHostname", {
            "StrValue_str": str_value,
        })

    def regenerate_server_cert(
        self,
        str_value: str,
    ) -> RpcResult:
        """RegenerateServerCert -- Generate New Self-Signed Certificate with Specified CN (Common Name) and Register on VPN Server.

        Generate New Self-Signed Certificate with Specified CN (Common Name) and Register on VPN
        Server. You can specify the new CN (common name) value on the StrValue_str field. You
        can use this API to replace the current certificate on the VPN Server to a new
        self-signed certificate which has the CN (Common Name) value in the fields. This API is
        convenient if you are planning to use Microsoft SSTP VPN Clone Server Function. Because
        of the value of CN (Common Name) on the SSL certificate of VPN Server must match to the
        hostname specified on the SSTP VPN client. This API will delete the existing SSL
        certificate of the VPN Server. It is recommended to backup the current SSL certificate
        and private key by using the GetServerCert API beforehand. To call this API, you must
        have VPN Server administrator privileges. This API cannot be invoked on VPN Bridge. You
        cannot execute this API for Virtual Hubs of VPN Servers operating as a cluster.

        Args:
            str_value: An Ascii string field (``StrValue_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``IntValue_u32``, ``Int64Value_u64``, ``StrValue_str``,
            ``UniStrValue_utf``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("RegenerateServerCert", {
            "StrValue_str": str_value,
        })

    def make_openvpn_config_file(
        self,
    ) -> RpcResult:
        """MakeOpenVpnConfigFile -- Generate a Sample Setting File for OpenVPN Client.

        Generate a Sample Setting File for OpenVPN Client. Originally, the OpenVPN Client
        requires a user to write a very difficult configuration file manually. This API helps
        you to make a useful configuration sample. What you need to generate the configuration
        file for the OpenVPN Client is to run this API. To call this API, you must have VPN
        Server administrator privileges. This API cannot be invoked on VPN Bridge. You cannot
        execute this API for Virtual Hubs of VPN Servers operating as a cluster.

        Returns:
            RpcResult with the fields: ``ServerName_str``, ``FilePath_str``, ``Offset_u32``,
            ``Buffer_bin``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("MakeOpenVpnConfigFile", {})

    def set_special_listener(
        self,
        *,
        vpn_over_icmp_listener: Optional[bool] = None,
        vpn_over_dns_listener: Optional[bool] = None,
    ) -> RpcResult:
        """SetSpecialListener -- Enable / Disable the VPN over ICMP / VPN over DNS Server Function.

        Enable / Disable the VPN over ICMP / VPN over DNS Server Function. You can establish a
        VPN only with ICMP or DNS packets even if there is a firewall or routers which blocks
        TCP/IP communications. You have to enable the following functions beforehand. Warning:
        Use this function for emergency only. It is helpful when a firewall or router is
        misconfigured to blocks TCP/IP, but either ICMP or DNS is not blocked. It is not for
        long-term stable using. To call this API, you must have VPN Server administrator
        privileges. This API cannot be invoked on VPN Bridge.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            vpn_over_icmp_listener: The flag to activate the VPN over ICMP server function
                (``VpnOverIcmpListener_bool``, boolean)
            vpn_over_dns_listener: The flag to activate the VPN over DNS function
                (``VpnOverDnsListener_bool``, boolean)

        Returns:
            RpcResult with the fields: ``VpnOverIcmpListener_bool``,
            ``VpnOverDnsListener_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetSpecialListener", {
            "VpnOverIcmpListener_bool": vpn_over_icmp_listener,
            "VpnOverDnsListener_bool": vpn_over_dns_listener,
        })

    def get_special_listener(
        self,
    ) -> RpcResult:
        """GetSpecialListener -- Get Current Setting of the VPN over ICMP / VPN over DNS Function.

        Get Current Setting of the VPN over ICMP / VPN over DNS Function. Get and show the
        current VPN over ICMP / VPN over DNS Function status. To call this API, you must have
        VPN Server administrator privileges. This API cannot be invoked on VPN Bridge.

        Returns:
            RpcResult with the fields: ``VpnOverIcmpListener_bool``,
            ``VpnOverDnsListener_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetSpecialListener", {})

    def get_azure_status(
        self,
    ) -> RpcResult:
        """GetAzureStatus -- Show the current status of VPN Azure function.

        Show the current status of VPN Azure function. Get and show the current status of the
        VPN Azure function. VPN Azure makes it easier to establish a VPN Session from your home
        PC to your office PC. While a VPN connection is established, you can access to any other
        servers on the private network of your company. You don't need a global IP address on
        the office PC (VPN Server). It can work behind firewalls or NATs. No network
        administrator's configuration required. You can use the built-in SSTP-VPN Client of
        Windows in your home PC. VPN Azure is a cloud VPN service operated by SoftEther
        Corporation. VPN Azure is free of charge and available to anyone. Visit
        http://www.vpnazure.net/ to see details and how-to-use instructions. The VPN Azure
        hostname is same to the hostname of the Dynamic DNS setting, but altering the domain
        suffix to "vpnazure.net". To change the hostname use the ChangeDDnsClientHostname API.
        To call this API, you must have VPN Server administrator privileges. This API cannot be
        invoked on VPN Bridge. You cannot execute this API for Virtual Hubs of VPN Servers
        operating as a cluster.

        Returns:
            RpcResult with the fields: ``IsEnabled_bool``, ``IsConnected_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetAzureStatus", {})

    def set_azure_status(
        self,
        *,
        is_enabled: Optional[bool] = None,
    ) -> RpcResult:
        """SetAzureStatus -- Enable / Disable VPN Azure Function.

        Enable / Disable VPN Azure Function. Enable or disable the VPN Azure function. VPN Azure
        makes it easier to establish a VPN Session from your home PC to your office PC. While a
        VPN connection is established, you can access to any other servers on the private
        network of your company. You don't need a global IP address on the office PC (VPN
        Server). It can work behind firewalls or NATs. No network administrator's configuration
        required. You can use the built-in SSTP-VPN Client of Windows in your home PC. VPN Azure
        is a cloud VPN service operated by SoftEther Corporation. VPN Azure is free of charge
        and available to anyone. Visit http://www.vpnazure.net/ to see details and how-to-use
        instructions. The VPN Azure hostname is same to the hostname of the Dynamic DNS setting,
        but altering the domain suffix to "vpnazure.net". To change the hostname use the
        ChangeDDnsClientHostname API. To call this API, you must have VPN Server administrator
        privileges. This API cannot be invoked on VPN Bridge. You cannot execute this API for
        Virtual Hubs of VPN Servers operating as a cluster.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            is_enabled: Whether VPN Azure Function is Enabled (``IsEnabled_bool``, boolean)

        Returns:
            RpcResult with the fields: ``IsEnabled_bool``, ``IsConnected_bool``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetAzureStatus", {
            "IsEnabled_bool": is_enabled,
        })

    def get_ddns_internet_settng(
        self,
    ) -> RpcResult:
        """GetDDnsInternetSettng -- Get the Proxy Settings for Connecting to the DDNS server.

        Get the Proxy Settings for Connecting to the DDNS server.

        Returns:
            RpcResult with the fields: ``ProxyType_u32``, ``ProxyHostName_str``,
            ``ProxyPort_u32``, ``ProxyUsername_str``, ``ProxyPassword_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("GetDDnsInternetSettng", {})

    def set_ddns_internet_settng(
        self,
        *,
        proxy_type: Optional[Union[int, "ProxyType"]] = None,
        proxy_host_name: Optional[str] = None,
        proxy_port: Optional[int] = None,
        proxy_username: Optional[str] = None,
        proxy_password: Optional[str] = None,
    ) -> RpcResult:
        """SetDDnsInternetSettng -- Set the Proxy Settings for Connecting to the DDNS server.

        Set the Proxy Settings for Connecting to the DDNS server.

        .. warning::
           This RPC replaces the whole record on the server rather than patching it. Any field
           you leave at ``None`` is reset to the VPN Server default instead of being preserved,
           so read the current values with the matching ``get_*`` / ``enum_*`` call and pass
           them back unless you mean to clear them.

        Args:
            proxy_type: Type of proxy server (``ProxyType_u32``, number (enum))
                Accepts an :class:`ProxyType` member or its integer value: 0 = Direct TCP
                connection; 1 = Connection via HTTP proxy server; 2 = Connection via SOCKS proxy
                server.
            proxy_host_name: Proxy server host name (``ProxyHostName_str``, string (ASCII))
            proxy_port: Proxy server port number (``ProxyPort_u32``, number (uint32))
            proxy_username: Proxy server user name (``ProxyUsername_str``, string (ASCII))
            proxy_password: Proxy server password (``ProxyPassword_str``, string (ASCII))

        Returns:
            RpcResult with the fields: ``ProxyType_u32``, ``ProxyHostName_str``,
            ``ProxyPort_u32``, ``ProxyUsername_str``, ``ProxyPassword_str``.

        Raises:
            ValidationError: An argument is out of range or malformed; nothing was sent.
            AuthenticationError: The administrator password was rejected.
            RpcError: The VPN Server refused the call (see ``code``/``name``).
            TransportError: The request could not be exchanged with the server.
        """
        return self._invoke("SetDDnsInternetSettng", {
            "ProxyType_u32": proxy_type,
            "ProxyHostName_str": proxy_host_name,
            "ProxyPort_u32": proxy_port,
            "ProxyUsername_str": proxy_username,
            "ProxyPassword_str": proxy_password,
        })


    # -- documented in the table of contents, absent from the reference body --

    def get_vgs_config(self) -> RpcResult:
        """GetVgsConfig -- Get the VPN Gate Server Configuration.

        The suite document lists this RPC in its table of contents but ships no
        section for it, so no parameter list could be generated.  The call takes
        no arguments and returns the VPN Gate Server configuration of a VPN
        Server built with the VPN Gate option; a stock build answers with an
        error.

        Returns:
            RpcResult with whatever fields this build of the server reports.

        Raises:
            RpcError: The VPN Server refused the call, typically because it was
                not built with VPN Gate support.
            TransportError: The request could not be exchanged with the server.
        """
        return self.call("GetVgsConfig", {})

    def set_vgs_config(self, **params: Any) -> RpcResult:
        """SetVgsConfig -- Set the VPN Gate Server Configuration.

        The suite document lists this RPC in its table of contents but ships no
        section for it, so its parameters cannot be validated or documented
        here.  Keyword arguments are passed through as wire field names, exactly
        as :meth:`call` would, and must therefore already be encoded (base64 for
        ``_bin`` fields, ``YYYY-MM-DDTHH:MM:SS.mmm`` for ``_dt`` fields).

        Args:
            **params: Wire fields to send, e.g. ``IsEnabled_bool=True``.

        Returns:
            RpcResult with whatever fields this build of the server reports.

        Raises:
            RpcError: The VPN Server refused the call, typically because it was
                not built with VPN Gate support.
            TransportError: The request could not be exchanged with the server.
        """
        return self.call("SetVgsConfig", params)


    # -- aliases for RPC names that are misspelled upstream --
    get_ddns_internet_setting = get_ddns_internet_settng
    set_ddns_internet_setting = set_ddns_internet_settng


# ==========================================================================
# Public surface
# ==========================================================================

#: ``TimeoutError`` and ``PermissionError`` deliberately mirror the names used
#: by the standard library, which is convenient when catching them explicitly
#: but would shadow the builtins on ``from softether import *``.  The star
#: export therefore carries these aliases instead; both names always refer to
#: the same class.
RpcTimeoutError = TimeoutError
AccessDeniedError = PermissionError

__all__ = [
    "SoftEtherClient",
    "RpcResult",
    "DEFAULT_PORT",
    "RPC_METHODS",
    "__version__",
    # errors
    "SoftEtherError",
    "ValidationError",
    "TransportError",
    "ConnectionFailedError",
    "RpcTimeoutError",
    "TLSError",
    "CertificateFingerprintError",
    "HTTPError",
    "AuthenticationError",
    "ApiDisabledError",
    "ProtocolError",
    "RpcError",
    "NotFoundError",
    "AlreadyExistsError",
    "NotSupportedError",
    "AccessDeniedError",
    "BusyError",
    "InvalidParameterError",
    "InsecureTLSWarning",
    # enumerations
    "ServerType",
    "OsType",
    "HubType",
    "ConnectionType",
    "LogSwitchType",
    "PacketLogConfig",
    "PacketLogIndex",
    "ProxyType",
    "ClientAuthType",
    "UserAuthType",
    "SessionStatus",
    "AccessProtocol",
    "NatProtocol",
    "NatTcpStatus",
    "KeepConnectProtocol",
    "SysLogSaveType",
    # structure builders
    "access_rule",
    "admin_option",
    "ac_rule",
]


# ==========================================================================
# Command line interface
# ==========================================================================

def _cli(argv: Optional[Sequence[str]] = None) -> int:
    """Call any RPC from a shell.  ``python -m softether --help`` for usage."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="softether",
        description="Call a SoftEther VPN Server JSON-RPC method and print the JSON result.",
        epilog="example: softether -H 127.0.0.1 -p secret GetServerStatus",
    )
    parser.add_argument("method", nargs="?", help="RPC method name, e.g. EnumHub")
    parser.add_argument("params", nargs="?", default="{}",
                        help='parameters as a JSON object, e.g. \'{"HubName_str":"VPN"}\'')
    parser.add_argument("-H", "--host", default="127.0.0.1", help="VPN Server host")
    parser.add_argument("-P", "--port", type=int, default=DEFAULT_PORT, help="administration port")
    parser.add_argument("-p", "--password", default="", help="administrator password")
    parser.add_argument("-b", "--hub", default="", help="Virtual Hub for hub-admin mode")
    parser.add_argument("--verify", action="store_true", help="verify the TLS certificate")
    parser.add_argument("--ca-file", default=None, help="CA bundle used with --verify")
    parser.add_argument("--fingerprint", default=None,
                        help="expected SHA-256 fingerprint of the server certificate")
    parser.add_argument("--timeout", type=float, default=30.0, help="socket timeout in seconds")
    parser.add_argument("--list", action="store_true", help="list every known RPC method and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="log the exchange")
    args = parser.parse_args(argv)

    if args.list:
        for name in RPC_METHODS:
            print("%-28s %s" % (name, _RPC_TO_PYTHON[name]))
        return 0
    if not args.method:
        parser.error("a method name is required (or use --list)")

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    try:
        params = json.loads(args.params)
    except ValueError as exc:
        print("params is not valid JSON: %s" % exc)
        return 2
    if not isinstance(params, dict):
        print("params must be a JSON object")
        return 2

    client = SoftEtherClient(
        args.host, args.port, args.password, args.hub or None,
        verify=args.verify, ca_file=args.ca_file, fingerprint=args.fingerprint,
        timeout=args.timeout, suppress_insecure_warning=True,
    )
    try:
        result = client.call(args.method, params)
    except SoftEtherError as exc:
        print("%s: %s" % (type(exc).__name__, exc))
        return 1
    finally:
        client.close()

    print(json.dumps(result.raw, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
