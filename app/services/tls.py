"""The panel's own domain: its certificate, and the listeners that use it.

Give the panel a hostname and it does the rest: obtains a certificate for the
name from Let's Encrypt (:mod:`app.services.acme`), serves HTTPS on the port
the operator chose, answers plain HTTP on port 80 with the validation token
during issuance and a redirect to HTTPS the rest of the time, and renews the
certificate thirty days before it expires. The plain-HTTP bind port the panel
has always listened on keeps working exactly as before.

Everything lives in the data directory (``<data>/tls``): the account key,
the certificate and its private key, and a small state file, so a backup of
the data directory carries the certificate with it and the service's
``ProtectSystem=full`` sandbox never has to be loosened.

One thread owns all of it. Requests only read the status and set flags; the
thread does the network work, so a Let's Encrypt outage can never stall a
page. The listeners are uvicorn servers run on their own event loops in their
own threads, serving the same ASGI application the main listener serves.

What the operator sees is the point of the module: every stage -- the domain,
its DNS, port 80, the certificate, the renewal, the HTTPS listener -- reports
its own state and its own error, in words, so a wrong DNS record and a
firewall blocking port 80 are told apart on the Settings page instead of
both reading "failed".
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from cryptography import x509
from cryptography.x509.oid import NameOID

from ..config import settings
from . import acme

logger = logging.getLogger(__name__)

#: Renew this long before the certificate expires. Let's Encrypt issues for
#: 90 days and recommends renewing at 60; a month of margin means a broken
#: renewal has weeks of retries before anybody notices.
RENEW_BEFORE = timedelta(days=30)
#: The supervision tick: listeners are checked this often.
TICK_SECONDS = 20
#: After a failed issuance, wait this long before trying again on our own.
#: Doubles per consecutive failure, up to the cap; a manual "issue now"
#: ignores it. Let's Encrypt allows five failed validations per hostname per
#: hour, so an hour is the shortest sensible automatic retry.
RETRY_BASE = timedelta(hours=1)
RETRY_CAP = timedelta(hours=6)
#: Port 80 is where HTTP-01 validation arrives; it is not a choice.
HTTP_PORT = 80
EVENT_LIMIT = 12

_HOSTNAME_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: Optional[datetime]) -> str:
    return when.replace(microsecond=0).isoformat() if when else ""


def _parse_iso(text: str) -> Optional[datetime]:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --- the domain itself ------------------------------------------------------


def normalise_domain(value: str) -> str:
    """A hostname as the panel stores it: lower-case, IDNA-encoded, no dot at
    the end. Raises ``ValueError`` with the reason when it is not one."""
    name = (value or "").strip().strip(".").lower()
    if not name:
        return ""
    try:
        name = name.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("That is not a valid hostname.") from exc
    if len(name) > 253:
        raise ValueError("A hostname cannot be longer than 253 characters.")
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        raise ValueError("A domain name is needed, not an IP address -- a certificate cannot be issued for an address.")
    labels = name.split(".")
    if len(labels) < 2:
        raise ValueError("A public domain name has at least two parts, like panel.example.com.")
    for label in labels:
        if not _HOSTNAME_LABEL.match(label):
            raise ValueError(f"'{label}' is not a valid part of a hostname.")
    if labels[-1].isdigit():
        raise ValueError("The last part of a domain name cannot be a number.")
    if "*" in name:
        raise ValueError("Wildcard names cannot be validated over HTTP; name the host itself.")
    return name


# --- local network facts ----------------------------------------------------


def local_addresses() -> set[str]:
    """Every address this machine answers on, best effort.

    ``ip -j addr`` is the truthful source on Linux; the outbound-socket trick
    adds the address the default route leaves through, which on a NAT'd host
    is private but on a cloud VM is the public one. Loopback and link-local
    are left out because nothing on the internet resolves to them.
    """
    found: set[str] = set()
    try:
        out = subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True, timeout=5, check=False)
        if out.returncode == 0:
            for interface in json.loads(out.stdout or "[]"):
                for entry in interface.get("addr_info", []):
                    address = str(entry.get("local", ""))
                    if address:
                        found.add(address)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    for family, probe in ((socket.AF_INET, ("1.1.1.1", 53)), (socket.AF_INET6, ("2606:4700:4700::1111", 53))):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.connect(probe)
                found.add(sock.getsockname()[0])
        except OSError:
            pass
    cleaned: set[str] = set()
    for address in found:
        try:
            parsed = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            continue
        if parsed.is_loopback or parsed.is_link_local:
            continue
        cleaned.add(str(parsed))
    return cleaned


def resolve(domain: str) -> list[str]:
    """The addresses the name resolves to, IPv4 and IPv6, in DNS order."""
    seen: list[str] = []
    for info in socket.getaddrinfo(domain, None, proto=socket.IPPROTO_TCP):
        address = str(info[4][0])
        if address not in seen:
            seen.append(address)
    return seen


def port_holder(port: int) -> str:
    """Who is listening on a port, as ``ss`` reports it -- so "port 443 is in
    use" can say "by vpnserver" when the panel runs as root."""
    try:
        out = subprocess.run(
            ["ss", "-Hltnp", f"sport = :{port}"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r'users:\(\("([^"]+)"', out.stdout or "")
    return match.group(1) if match else ""


def _reuse(sock: socket.socket) -> None:
    """Let a restarted listener take its port back while old connections
    linger in TIME_WAIT -- without letting a second listener share a port
    that is live. On Linux SO_REUSEADDR means exactly that; on Windows it
    means the opposite, so the exclusive flag is used there instead."""
    if os.name == "nt":
        flag = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if flag is not None:
            sock.setsockopt(socket.SOL_SOCKET, flag, 1)
        return
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def bind_sockets(port: int) -> list[socket.socket]:
    """Listening sockets for a port on every interface: one dual-stack IPv6
    socket where the host allows it, a plain IPv4 one otherwise. The caller
    gets the ``OSError`` a busy port raises, with the port holder named."""
    sockets: list[socket.socket] = []
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        _reuse(sock)
        try:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        sock.bind(("::", port))
        sock.listen(1024)
        sock.setblocking(False)
        sockets.append(sock)
        return sockets
    except OSError as exc:
        # No IPv6 on this host, or v6-only sockets -- fall back to IPv4. A
        # port that is busy raises the same way, so the v4 attempt below is
        # what turns that into the answer.
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass
        last = exc
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _reuse(sock)
        sock.bind(("0.0.0.0", port))
        sock.listen(1024)
        sock.setblocking(False)
        sockets.append(sock)
        return sockets
    except OSError as exc:
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass
        last = exc
    holder = port_holder(port)
    detail = f"port {port} is already in use" + (f" by {holder}" if holder else "")
    raise OSError(f"{detail} ({last.strerror or last})")


# --- the listeners -----------------------------------------------------------


class Listener:
    """A uvicorn server on its own thread and event loop.

    The sockets are bound here, before uvicorn sees them, so a busy port is
    an exception this code can report rather than a log line and a silent
    ``sys.exit`` inside a thread.
    """

    def __init__(
        self,
        name: str,
        app: Any,
        port: int,
        *,
        certfile: Optional[str] = None,
        keyfile: Optional[str] = None,
    ) -> None:
        self.name = name
        self.app = app
        self.port = port
        self.certfile = certfile
        self.keyfile = keyfile
        self._server: Any = None
        self._thread: Optional[threading.Thread] = None
        self._sockets: list[socket.socket] = []
        self.error = ""
        self.started_at: Optional[datetime] = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        import uvicorn

        self.error = ""
        self._sockets = bind_sockets(self.port)
        config = uvicorn.Config(
            self.app,
            host="0.0.0.0",
            port=self.port,
            lifespan="off",
            log_config=None,
            access_log=False,
            proxy_headers=True,
            timeout_graceful_shutdown=5,
            ssl_certfile=self.certfile,
            ssl_keyfile=self.keyfile,
        )
        server = uvicorn.Server(config)
        self._server = server
        sockets = self._sockets

        def run() -> None:
            try:
                asyncio.run(server.serve(sockets=sockets))
            except SystemExit:
                self.error = self.error or "the listener refused to start"
            except Exception as exc:  # noqa: BLE001 - reported, not raised across threads
                self.error = str(exc)
                logger.exception("%s listener on port %s died", self.name, self.port)

        self._thread = threading.Thread(target=run, daemon=True, name=f"tls-{self.name}")
        self._thread.start()
        self.started_at = _now()

    def stop(self) -> None:
        server, thread = self._server, self._thread
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)
        for sock in self._sockets:
            try:
                sock.close()
            except OSError:
                pass
        self._sockets = []
        self._server = None
        self._thread = None


class ChallengeResponder:
    """The port-80 application: validation tokens, then a redirect to HTTPS.

    Anything that is not a challenge is answered with a redirect once HTTPS
    is up, and with the same anonymous 404 as the panel's root until then.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}
        self._lock = threading.Lock()
        self.redirect_to: str = ""  # "https://host[:port]" while HTTPS is up

    def publish(self, token: str, key_authorization: str) -> None:
        with self._lock:
            self._tokens[token] = key_authorization

    def withdraw(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token, None)

    def lookup(self, token: str) -> Optional[str]:
        with self._lock:
            return self._tokens.get(token)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            if scope.get("type") == "lifespan":
                while True:
                    message = await receive()
                    if message["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif message["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
            return
        path = str(scope.get("path", ""))
        if path.startswith("/.well-known/acme-challenge/"):
            token = path[len("/.well-known/acme-challenge/"):]
            answer = self.lookup(token)
            if answer is not None:
                body = answer.encode("ascii")
                await _respond(send, 200, [(b"content-type", b"text/plain")], body)
                return
            await _respond(send, 404, [(b"content-type", b"text/plain")], b"not found\n")
            return
        target = self.redirect_to
        if target:
            query = bytes(scope.get("query_string", b"") or b"")
            location = target + quote(path, safe="/%")
            if query:
                location += "?" + query.decode("latin-1")
            await _respond(send, 308, [(b"location", location.encode("latin-1", "replace"))], b"")
            return
        await _respond(send, 404, [(b"content-type", b"application/json")], b'{"detail":"Not found."}')


async def _respond(send: Any, status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [*headers, (b"content-length", str(len(body)).encode("ascii")), (b"cache-control", b"no-store")],
        }
    )
    await send({"type": "http.response.body", "body": body})


# --- the certificate on disk -------------------------------------------------


class Certificate:
    """What is on disk for a domain, read from the certificate itself."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.fullchain = directory / "fullchain.pem"
        self.privkey = directory / "privkey.pem"
        self.meta_path = directory / "meta.json"
        self.domain = ""
        self.not_before: Optional[datetime] = None
        self.not_after: Optional[datetime] = None
        self.issuer = ""
        self.serial = ""
        self.staging = False
        self.issued_at: Optional[datetime] = None

    @property
    def present(self) -> bool:
        return self.not_after is not None and self.fullchain.is_file() and self.privkey.is_file()

    def load(self) -> "Certificate":
        try:
            pem = self.fullchain.read_bytes()
            cert = x509.load_pem_x509_certificate(pem)
        except (OSError, ValueError):
            return self
        try:
            names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            dns_names = names.get_values_for_type(x509.DNSName)
        except x509.ExtensionNotFound:
            dns_names = []
        self.domain = (dns_names[0] if dns_names else _common_name(cert)).lower()
        self.not_before = _cert_time(cert, "not_valid_before")
        self.not_after = _cert_time(cert, "not_valid_after")
        self.issuer = _common_name(cert, issuer=True)
        self.serial = format(cert.serial_number, "x")
        try:
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            self.staging = bool(meta.get("staging"))
            self.issued_at = _parse_iso(str(meta.get("issued_at", "")))
        except (OSError, ValueError):
            self.staging = "staging" in self.issuer.lower() or "fake" in self.issuer.lower()
        return self

    def days_left(self) -> Optional[float]:
        if self.not_after is None:
            return None
        return (self.not_after - _now()).total_seconds() / 86400

    def renew_at(self) -> Optional[datetime]:
        return (self.not_after - RENEW_BEFORE) if self.not_after else None

    def public(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "domain": self.domain,
            "issued_at": _iso(self.issued_at or self.not_before),
            "not_before": _iso(self.not_before),
            "expires_at": _iso(self.not_after),
            "days_left": round(self.days_left(), 1) if self.days_left() is not None else None,
            "renew_at": _iso(self.renew_at()),
            "issuer": self.issuer,
            "serial": self.serial,
            "staging": self.staging,
            "expired": bool(self.not_after and self.not_after <= _now()),
        }


def _cert_time(cert: x509.Certificate, attribute: str) -> Optional[datetime]:
    value = getattr(cert, attribute + "_utc", None)
    if value is None:
        value = getattr(cert, attribute, None)
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
    return value


def _common_name(cert: x509.Certificate, issuer: bool = False) -> str:
    name = cert.issuer if issuer else cert.subject
    try:
        values = name.get_attributes_for_oid(NameOID.COMMON_NAME)
    except ValueError:
        return ""
    return str(values[0].value) if values else ""


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


# --- the manager ----------------------------------------------------------------


class Config:
    """The stored settings the manager acts on, read once per tick."""

    def __init__(self) -> None:
        from ..settings_store import get_setting

        self.domain = ""
        try:
            self.domain = normalise_domain(str(get_setting("domain") or ""))
        except ValueError:
            self.domain = ""
        self.https_port = int(get_setting("https_port") or 443)
        self.email = str(get_setting("acme_email") or "").strip()
        self.staging = bool(get_setting("acme_staging"))
        self.domain_only = bool(get_setting("domain_only"))
        self.web_path = str(get_setting("web_path") or "").strip().strip("/")

    @property
    def directory_url(self) -> str:
        return acme.STAGING_DIRECTORY if self.staging else acme.PRODUCTION_DIRECTORY

    def origin(self) -> str:
        suffix = "" if self.https_port == 443 else f":{self.https_port}"
        return f"https://{self.domain}{suffix}"

    def url(self) -> str:
        return self.origin() + ("/" if not self.web_path else f"/{self.web_path}/")


class TlsManager:
    def __init__(self) -> None:
        self.base = Path(settings.data_dir) / "tls"
        self.state_path = self.base / "state.json"
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._app: Any = None
        self.responder = ChallengeResponder()
        self._http: Optional[Listener] = None
        self._https: Optional[Listener] = None
        self._https_cert_serial = ""
        self._issue_requested = False
        self._busy = False
        self._step = ""
        self._phase = "idle"
        self._dns: dict[str, Any] = {}
        self._state = self._read_state()
        self._config: Optional[Config] = None
        self._config_error = ""

    # -- lifecycle --------------------------------------------------------------

    def start(self, app: Any) -> None:
        if self._thread is not None:
            return
        self._app = app
        self._thread = threading.Thread(target=self._loop, daemon=True, name="tls-manager")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=15)
        self._stop_listeners()

    def apply(self) -> None:
        """Settings changed: act on them now rather than at the next tick."""
        self._wake.set()

    def request_issue(self) -> bool:
        """Start an issuance (or renewal) as soon as the thread is free.
        False when one is already running -- the caller must not queue a
        second."""
        with self._lock:
            if self._busy:
                return False
            self._issue_requested = True
        self._wake.set()
        return True

    # -- state file ---------------------------------------------------------------

    def _read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {"events": []}

    def _write_state(self) -> None:
        try:
            self.base.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, indent=1), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            logger.warning("could not write the TLS state: %s", exc)

    def _event(self, kind: str, message: str) -> None:
        events = list(self._state.get("events") or [])
        events.insert(0, {"at": _iso(_now()), "kind": kind, "message": message[:600]})
        self._state["events"] = events[:EVENT_LIMIT]

    # -- the loop --------------------------------------------------------------------

    def _loop(self) -> None:
        # A short first wait lets the main listener finish starting before
        # the extra ones join it.
        self._wake.wait(2)
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - the supervisor must never die
                logger.exception("TLS manager tick failed")
            self._wake.wait(TICK_SECONDS)

    def _tick(self) -> None:
        try:
            config = Config()
            self._config_error = ""
        except Exception as exc:  # noqa: BLE001 - the database is unavailable
            self._config_error = str(exc)
            return
        self._config = config

        from .. import hostguard

        hostguard.configure(config.domain, config.domain_only)

        if not config.domain:
            if self._http or self._https:
                self._stop_listeners()
                self._event("listener", "Domain removed; the HTTPS and port-80 listeners were stopped.")
                self._write_state()
            with self._lock:
                self._phase = "idle"
                self._step = ""
                self._issue_requested = False
            return

        self._ensure_http()
        cert = self.certificate(config.domain)

        stale = (
            not cert.present
            or cert.domain != config.domain
            or cert.staging != config.staging
            or (cert.not_after is not None and cert.not_after <= _now())
        )
        due_for_renewal = cert.present and not stale and cert.renew_at() is not None and _now() >= cert.renew_at()  # type: ignore[operator]

        with self._lock:
            requested = self._issue_requested
            self._issue_requested = False
        if requested or ((stale or due_for_renewal) and self._retry_allowed()):
            self._issue(config, renewing=cert.present and not stale)
            cert = self.certificate(config.domain)
            stale = not cert.present or cert.domain != config.domain

        self._ensure_https(config, cert if not stale else None)

        with self._lock:
            if self._busy:
                pass
            elif self._state.get("last_error") and (stale or (cert.present and due_for_renewal)):
                self._phase = "failed"
            elif stale:
                self._phase = "waiting"
            else:
                self._phase = "ok"

    def _retry_allowed(self) -> bool:
        next_at = _parse_iso(str(self._state.get("next_attempt_at", "")))
        return next_at is None or _now() >= next_at

    # -- listeners ----------------------------------------------------------------------

    def _ensure_http(self) -> None:
        if self._http is not None and self._http.alive:
            return
        listener = Listener("http", self.responder, HTTP_PORT)
        try:
            listener.start()
        except OSError as exc:
            message = str(exc)
            if self._state.get("http_error") != message:
                self._state["http_error"] = message
                self._event("listener", f"Port 80 could not be opened: {message}")
                self._write_state()
            self._http = None
            return
        self._http = listener
        if self._state.get("http_error"):
            self._state["http_error"] = ""
            self._write_state()

    def _ensure_https(self, config: Config, cert: Optional[Certificate]) -> None:
        if cert is None or not cert.present:
            if self._https is not None:
                self._https.stop()
                self._https = None
                self.responder.redirect_to = ""
            return
        current = self._https
        wanted_serial = cert.serial
        if (
            current is not None
            and current.alive
            and current.port == config.https_port
            and self._https_cert_serial == wanted_serial
        ):
            return
        if current is not None:
            current.stop()
            self._https = None
        listener = Listener(
            "https", self._app, config.https_port, certfile=str(cert.fullchain), keyfile=str(cert.privkey)
        )
        try:
            listener.start()
        except OSError as exc:
            message = str(exc)
            if self._state.get("https_error") != message:
                self._state["https_error"] = message
                self._event("listener", f"The HTTPS listener could not open port {config.https_port}: {message}")
                self._write_state()
            self.responder.redirect_to = ""
            return
        self._https = listener
        self._https_cert_serial = wanted_serial
        self.responder.redirect_to = config.origin()
        if self._state.get("https_error"):
            self._state["https_error"] = ""
        self._event("listener", f"HTTPS is being served on port {config.https_port} for {config.domain}.")
        self._write_state()

    def _stop_listeners(self) -> None:
        for listener in (self._https, self._http):
            if listener is not None:
                try:
                    listener.stop()
                except Exception:  # noqa: BLE001
                    logger.debug("stopping a listener failed", exc_info=True)
        self._https = None
        self._http = None
        self._https_cert_serial = ""
        self.responder.redirect_to = ""

    # -- certificates -----------------------------------------------------------------

    def cert_dir(self, domain: str) -> Path:
        return self.base / "certs" / domain

    def certificate(self, domain: str) -> Certificate:
        return Certificate(self.cert_dir(domain)).load()

    def _account_key(self, staging: bool) -> Any:
        path = self.base / ("account-staging.key" if staging else "account.key")
        try:
            return acme.key_from_pem(path.read_bytes())
        except (OSError, acme.AcmeError, ValueError):
            key = acme.generate_key()
            _write_private(path, acme.key_to_pem(key))
            return key

    def _set_step(self, step: str) -> None:
        with self._lock:
            self._step = step
        logger.info("tls: %s", step)

    def _issue(self, config: Config, *, renewing: bool) -> None:
        with self._lock:
            if self._busy:
                return
            self._busy = True
            self._phase = "renewing" if renewing else "issuing"
        started = _now()
        self._state["last_attempt_at"] = _iso(started)
        self._state["last_attempt_kind"] = "renewal" if renewing else "issuance"
        self._write_state()
        token = ""
        try:
            self._set_step("checking the DNS record")
            dns = self.check_dns(config.domain)
            if dns.get("error"):
                raise acme.AcmeError(f"DNS: {dns['error']}", type="dns")
            self._set_step("checking port 80")
            self._ensure_http()
            if self._http is None or not self._http.alive:
                raise acme.AcmeError(
                    f"Port 80 is not available to answer the validation: {self._state.get('http_error') or 'the listener is not running'}",
                    type="port80",
                )
            from ..version import get_version

            client = acme.AcmeClient(
                config.directory_url,
                self._account_key(config.staging),
                contact_email=config.email,
                user_agent=f"softether-manager/{get_version()}",
                log=self._set_step,
            )
            client.register()
            order = client.new_order(config.domain)
            for authorization_url in order.authorization_urls:
                self._set_step("fetching the validation challenge")
                challenge = client.http01(authorization_url)
                if challenge is None:
                    continue
                token = challenge.token
                self.responder.publish(challenge.token, challenge.key_authorization)
                client.answer(challenge)
                self._set_step("waiting for Let's Encrypt to validate the domain over port 80")
                client.wait_valid(authorization_url)
                self.responder.withdraw(challenge.token)
                token = ""
            self._set_step("generating the certificate key")
            key = acme.generate_key()
            csr = acme.make_csr(config.domain, key)
            certificate_url = client.finalize(order, csr)
            pem = client.download(certificate_url)
            directory = self.cert_dir(config.domain)
            _write_private(directory / "privkey.pem", acme.key_to_pem(key))
            _write_private(directory / "fullchain.pem", pem)
            _write_private(
                directory / "meta.json",
                json.dumps(
                    {
                        "domain": config.domain,
                        "staging": config.staging,
                        "issued_at": _iso(_now()),
                        "directory": config.directory_url,
                    }
                ).encode("utf-8"),
            )
            cert = self.certificate(config.domain)
            self._state.update(
                {
                    "last_success_at": _iso(_now()),
                    "last_error": "",
                    "last_error_type": "",
                    "last_error_at": "",
                    "consecutive_failures": 0,
                    "next_attempt_at": "",
                }
            )
            self._event(
                "renewed" if renewing else "issued",
                f"{'Renewed' if renewing else 'Issued'} a certificate for {config.domain}"
                + (f", valid until {_iso(cert.not_after)}" if cert.not_after else "")
                + (" (staging -- not trusted by browsers)" if config.staging else "")
                + ".",
            )
            self._set_step("")
        except acme.AcmeError as exc:
            self._record_failure(str(exc), exc.short_type or exc.type, renewing)
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            logger.exception("certificate issuance failed")
            self._record_failure(f"{type(exc).__name__}: {exc}", "internal", renewing)
        finally:
            if token:
                self.responder.withdraw(token)
            with self._lock:
                self._busy = False
                self._step = ""
            self._write_state()

    def _record_failure(self, message: str, kind: str, renewing: bool) -> None:
        failures = int(self._state.get("consecutive_failures") or 0) + 1
        delay = min(RETRY_CAP, RETRY_BASE * (2 ** (failures - 1)))
        self._state.update(
            {
                "last_error": message,
                "last_error_type": kind,
                "last_error_at": _iso(_now()),
                "consecutive_failures": failures,
                "next_attempt_at": _iso(_now() + delay),
            }
        )
        self._event("failed", f"{'Renewal' if renewing else 'Issuance'} failed: {message}")
        with self._lock:
            self._phase = "failed"

    # -- checks -------------------------------------------------------------------------

    def check_dns(self, domain: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "checked_at": _iso(_now()),
            "domain": domain,
            "addresses": [],
            "local_addresses": [],
            "match": None,
            "error": "",
        }
        try:
            result["addresses"] = resolve(domain)
        except socket.gaierror as exc:
            result["error"] = (
                f"{domain} does not resolve: {exc.strerror or exc}. Add an A record (and an AAAA record for IPv6) pointing at this server, then wait for it to propagate."
            )
        except OSError as exc:
            result["error"] = f"{domain} could not be resolved: {exc}"
        if not result["error"] and not result["addresses"]:
            result["error"] = f"{domain} has no address record."
        local = local_addresses()
        result["local_addresses"] = sorted(local)
        if result["addresses"]:
            result["match"] = any(address in local for address in result["addresses"])
        with self._lock:
            self._dns = result
        return result

    def check_now(self) -> dict[str, Any]:
        """The Re-check button: DNS now, listeners now, and the answer."""
        try:
            config = Config()
        except Exception as exc:  # noqa: BLE001
            self._config_error = str(exc)
            return self.status()
        self._config = config
        if config.domain:
            self.check_dns(config.domain)
        self._wake.set()
        return self.status()

    # -- what the API shows ---------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        try:
            config = Config()
        except Exception:  # noqa: BLE001 - fall back to the last tick's copy
            config = self._config
        with self._lock:
            busy = self._busy
            step = self._step
            phase = self._phase
            dns = dict(self._dns)
            state = json.loads(json.dumps(self._state))
        domain = config.domain if config else ""
        cert = self.certificate(domain) if domain else Certificate(self.base / "certs" / "none")
        http = self._http
        https = self._https
        https_port = config.https_port if config else 443
        stale = bool(domain) and (
            not cert.present
            or cert.domain != domain
            or (config is not None and cert.staging != config.staging)
            or bool(cert.not_after and cert.not_after <= _now())
        )
        renew_at = cert.renew_at() if cert.present else None
        return {
            "configured": bool(domain),
            "domain": domain,
            "https_port": https_port,
            "acme_email": config.email if config else "",
            "acme_staging": bool(config.staging) if config else False,
            "domain_only": bool(config.domain_only) if config else False,
            "config_error": self._config_error,
            "phase": phase if domain else "idle",
            "busy": busy,
            "step": step,
            "dns": dns if dns.get("domain") == domain else {},
            "http": {
                "port": HTTP_PORT,
                "listening": bool(http and http.alive),
                "error": "" if (http and http.alive) else str(state.get("http_error") or ""),
                "redirecting": bool(self.responder.redirect_to),
            },
            "https": {
                "port": https_port,
                "listening": bool(https and https.alive and https.port == https_port),
                "error": "" if (https and https.alive) else str(state.get("https_error") or ""),
                "since": _iso(https.started_at) if https and https.alive else "",
            },
            "certificate": {**cert.public(), "stale": stale, "matches_domain": cert.present and cert.domain == domain},
            "renewal": {
                "automatic": True,
                "renew_before_days": RENEW_BEFORE.days,
                "renew_at": _iso(renew_at),
                "due": bool(renew_at and _now() >= renew_at),
                "last_attempt_at": str(state.get("last_attempt_at") or ""),
                "last_attempt_kind": str(state.get("last_attempt_kind") or ""),
                "last_success_at": str(state.get("last_success_at") or ""),
                "last_error": str(state.get("last_error") or ""),
                "last_error_type": str(state.get("last_error_type") or ""),
                "last_error_at": str(state.get("last_error_at") or ""),
                "next_attempt_at": str(state.get("next_attempt_at") or ""),
                "consecutive_failures": int(state.get("consecutive_failures") or 0),
            },
            "url": config.url() if (config and domain) else "",
            "origin": config.origin() if (config and domain) else "",
            "events": list(state.get("events") or []),
        }

    def forget(self, domain: str) -> None:
        """Drop the stored failure so a re-added domain starts clean; the
        certificate files stay -- a still-valid one is reused at once."""
        self._state.update(
            {"last_error": "", "last_error_type": "", "last_error_at": "", "consecutive_failures": 0, "next_attempt_at": ""}
        )
        with self._lock:
            self._dns = {}
        self._write_state()


#: One manager per process: it owns the sockets.
manager = TlsManager()


def https_ready(domain: str) -> bool:
    """For shell tooling: whether a valid certificate for the domain exists on
    disk -- the panel would serve HTTPS with it once started."""
    try:
        name = normalise_domain(domain)
    except ValueError:
        return False
    if not name:
        return False
    cert = Certificate(Path(settings.data_dir) / "tls" / "certs" / name).load()
    return cert.present and cert.domain == name and bool(cert.not_after and cert.not_after > _now())


__all__ = [
    "manager",
    "normalise_domain",
    "https_ready",
    "local_addresses",
    "resolve",
    "Certificate",
    "TlsManager",
    "RENEW_BEFORE",
    "HTTP_PORT",
]
