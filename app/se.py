"""The panel's one door to its SoftEther server.

This panel manages the SoftEther VPN Server on the machine it is installed on
-- one server, one connection. The connection facts live in the settings
store: the management host (127.0.0.1 unless deliberately pointed elsewhere),
the port, and the administrator password, Fernet-encrypted. One
:class:`SoftEtherClient` is kept alive per process (the library holds a
keep-alive HTTPS connection and serialises calls, so sharing it between
requests is both safe and much faster than reconnecting), and the library's
error taxonomy is translated into HTTP answers the frontend can act on.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from fastapi import HTTPException

from Library import softether

from .secrets_store import decrypt, encrypt
from .settings_store import get_setting, set_setting

_client: Optional[softether.SoftEtherClient] = None
_client_lock = threading.Lock()


def connection() -> dict[str, Any]:
    """The stored connection facts. The password stays out of this."""
    return {
        "host": str(get_setting("se_host") or "127.0.0.1"),
        "port": int(get_setting("se_port") or 5555),
        "configured": bool(get_setting("se_password")),
    }


def set_connection(host: str, port: int, password: Optional[str]) -> None:
    """Store new connection facts. ``password=None`` keeps the stored one."""
    set_setting("se_host", host.strip() or "127.0.0.1")
    set_setting("se_port", int(port))
    if password is not None:
        set_setting("se_password", encrypt(password))
    drop_client()


def stored_password() -> str:
    return decrypt(str(get_setting("se_password") or ""))


def build_client(host: str, port: int, password: str, timeout: float = 15.0) -> softether.SoftEtherClient:
    return softether.SoftEtherClient(
        host,
        port,
        password=password,
        verify=False,
        timeout=timeout,
        retries=2,
        suppress_insecure_warning=True,
    )


def get_client() -> softether.SoftEtherClient:
    global _client
    with _client_lock:
        if _client is not None:
            return _client
    info = connection()
    if not info["configured"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "The panel is not connected to SoftEther yet. Set the management "
                "port and administrator password first.",
                "se_error": "NotConfigured",
                "se_code": None,
            },
        )
    client = build_client(info["host"], info["port"], stored_password())
    with _client_lock:
        if _client is None:
            _client = client
        return _client


def drop_client() -> None:
    """Forget the cached client after the connection facts changed."""
    global _client
    with _client_lock:
        client, _client = _client, None
    if client is not None:
        try:
            client.close()
        except Exception:  # noqa: BLE001 - closing is best-effort
            pass


def rpc(method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Call one RPC on the managed server and return the wire-format result.

    ``RpcResult.raw`` is the server's own JSON, untouched -- datetimes stay
    ISO strings, binaries stay base64 -- which is exactly what a JSON API
    should hand to the browser.
    """
    client = get_client()
    try:
        return client.call(method, params or {}).raw
    except softether.SoftEtherError as exc:
        raise to_http_error(exc) from exc


def to_http_error(exc: softether.SoftEtherError) -> HTTPException:
    """The library's taxonomy, translated once.

    The panel is a gateway in front of the VPN server, so transport and
    authentication failures against *it* are 502s -- the browser's request was
    fine; the upstream was not. The server refusing an operation keeps its own
    meaning: 404 for a missing object, 409 for a duplicate, 403 for denied.
    """
    if isinstance(exc, softether.ValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, softether.AuthenticationError):
        return _http(502, "The SoftEther server rejected the stored administrator password.", exc)
    if isinstance(exc, softether.ApiDisabledError):
        return _http(
            502,
            "The SoftEther server answered 404 for /api/: its JSON-RPC interface is disabled "
            "or the server predates it (4.27 is the first build that has it).",
            exc,
        )
    if isinstance(exc, softether.TransportError):
        return _http(502, f"The SoftEther server could not be reached: {exc}", exc)
    if isinstance(exc, softether.NotFoundError):
        return _http(404, str(exc), exc)
    if isinstance(exc, softether.AlreadyExistsError):
        return _http(409, str(exc), exc)
    if isinstance(exc, softether.PermissionError):
        return _http(403, str(exc), exc)
    if isinstance(exc, softether.InvalidParameterError):
        return _http(400, str(exc), exc)
    if isinstance(exc, softether.RpcError):
        return _http(400, exc.message or exc.name or str(exc), exc)
    return _http(502, str(exc), exc)


def _http(status: int, detail: str, exc: softether.SoftEtherError) -> HTTPException:
    name = getattr(exc, "name", "") or type(exc).__name__
    code = getattr(exc, "code", None)
    return HTTPException(status_code=status, detail={"message": detail, "se_error": name, "se_code": code})
