"""Domain-only access: refuse every request that did not come in by name.

With a domain configured, the operator can insist the panel is reached only
through it. Then a request whose ``Host`` header is anything else -- the bare
IP address a scanner typed, a name that happens to point here, an IPv6
literal -- is answered with the same anonymous 404 the panel's root already
gives, so nothing about the panel is confirmed to whoever asked.

Two kinds of request are always let through, because the restriction is
about the world outside and they are not part of it:

* anything arriving **from the machine itself** (a loopback client address):
  the installer's readiness probe, ``sem``, a local reverse proxy;
* the **ACME validation path**, ``/.well-known/acme-challenge/``, so turning
  the restriction on can never break the certificate renewal that keeps the
  domain working. The certificate authority does send the domain as the
  Host, but the renewal is too important to depend on that.

This is a pure ASGI wrapper around the whole application rather than a
Starlette middleware so it sits outside the mount that carries the secret web
path and covers the static frontend as well as the API.
"""
from __future__ import annotations

import threading
from typing import Any, Awaitable, Callable

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]

ACME_PREFIX = "/.well-known/acme-challenge/"

_lock = threading.Lock()
_enabled = False
_hosts: frozenset[str] = frozenset()


def configure(domain: str, enabled: bool) -> None:
    """Install the policy. ``enabled`` with no domain is a no-op: there is
    nothing to compare a Host against, and a rule that lets nobody in is a
    lock-out, not a restriction."""
    global _enabled, _hosts
    name = (domain or "").strip().lower().rstrip(".")
    with _lock:
        _hosts = frozenset({name}) if name else frozenset()
        _enabled = bool(enabled and name)


def load_from_settings() -> None:
    """Read the stored policy at startup. A database that cannot be read
    leaves the guard off -- the panel must still be reachable to fix it."""
    try:
        from .settings_store import get_setting

        configure(str(get_setting("domain") or ""), bool(get_setting("domain_only")))
    except Exception:  # noqa: BLE001 - see the docstring
        configure("", False)


def policy() -> dict[str, Any]:
    with _lock:
        return {"enabled": _enabled, "hosts": sorted(_hosts)}


def host_of(headers: Any) -> str:
    """The hostname part of the Host header, lower-cased, port and brackets
    removed. Empty when there is none."""
    raw = ""
    for name, value in headers:
        if name == b"host":
            raw = value.decode("latin-1", "replace").strip().lower()
            break
    if not raw:
        return ""
    if raw.startswith("["):
        # An IPv6 literal, with or without a port: never a domain name.
        end = raw.find("]")
        return raw[1:end] if end > 0 else raw
    return raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw


def is_loopback(scope: Scope) -> bool:
    client = scope.get("client")
    if not client:
        return False
    address = str(client[0])
    return address == "::1" or address.startswith("127.") or address.startswith("::ffff:127.")


def allowed(scope: Scope) -> bool:
    with _lock:
        enabled, hosts = _enabled, _hosts
    if not enabled:
        return True
    if str(scope.get("path", "")).startswith(ACME_PREFIX):
        return True
    if is_loopback(scope):
        return True
    return host_of(scope.get("headers", ())).rstrip(".") in hosts


class HostGuard:
    """The wrapper itself. Lifespan and any scope type it does not understand
    pass straight through."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") not in ("http", "websocket") or allowed(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = b'{"detail":"Not found."}'
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def request_host(headers: Any) -> str:
    """For the API: the name a request came in on, so the Settings page can
    tell whether the operator is already using the domain."""
    return host_of(headers)


__all__ = ["HostGuard", "configure", "load_from_settings", "policy", "request_host", "allowed"]
