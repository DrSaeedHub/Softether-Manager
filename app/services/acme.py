"""A small ACME client (RFC 8555): one hostname, HTTP-01, Let's Encrypt.

The panel gets its own certificate rather than asking the operator to install
certbot and a cron job, because everything certbot would need -- a place to
keep the account key, a way to answer the validation request on port 80, a
place to put the certificate, and something to renew it -- the panel already
has or already is. What is left is the protocol, and the protocol is small:
sign JSON with an account key, post it, poll.

Only what a single hostname needs is here. No wildcards (they need DNS-01),
no revocation, no key roll-over. The account key is an EC P-256 key signed
with ES256, the certificate key a fresh P-256 key per issuance, both produced
with the ``cryptography`` package the panel already depends on.

Every failure raises :class:`AcmeError` carrying the certificate authority's
own explanation where there is one -- "DNS problem: NXDOMAIN looking up A for
example.com" is far more useful than "validation failed", and it is what the
Settings page shows.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)

PRODUCTION_DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"
STAGING_DIRECTORY = "https://acme-staging-v02.api.letsencrypt.org/directory"

#: How long to wait for the CA to validate a challenge, and to sign an order.
VALIDATION_TIMEOUT = 120
FINALIZE_TIMEOUT = 120


class AcmeError(RuntimeError):
    """The certificate authority said no, or could not be asked.

    ``type`` is the ACME problem type (``urn:ietf:params:acme:error:...``)
    when the CA answered with one, so callers can tell a rate limit from a
    failed validation without parsing prose.
    """

    def __init__(self, message: str, *, type: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.type = type
        self.status = status

    @property
    def short_type(self) -> str:
        return self.type.rsplit(":", 1)[-1] if self.type else ""


# --- keys and encodings ------------------------------------------------------


def b64url(data: bytes) -> str:
    """Base64url without padding, as JWS wants it."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def key_to_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def key_from_pem(pem: bytes) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise AcmeError("The stored account key is not an EC key.")
    return key


def jwk_of(key: ec.EllipticCurvePrivateKey) -> dict[str, str]:
    numbers = key.public_key().public_numbers()
    return {
        "crv": "P-256",
        "kty": "EC",
        "x": b64url(numbers.x.to_bytes(32, "big")),
        "y": b64url(numbers.y.to_bytes(32, "big")),
    }


def thumbprint_of(key: ec.EllipticCurvePrivateKey) -> str:
    """RFC 7638: the SHA-256 of the JWK's required members in lexicographic
    order with no whitespace -- what an HTTP-01 key authorization ends in."""
    canonical = json.dumps(jwk_of(key), sort_keys=True, separators=(",", ":")).encode("ascii")
    return b64url(hashlib.sha256(canonical).digest())


def _sign_es256(key: ec.EllipticCurvePrivateKey, data: bytes) -> bytes:
    """ES256 as JWS wants it: the raw R || S pair, not the DER envelope."""
    der = key.sign(data, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def make_csr(domain: str, key: ec.EllipticCurvePrivateKey) -> bytes:
    """A DER-encoded CSR for one hostname, in the CN and as a SAN."""
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)]))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.DER)


# --- the client ------------------------------------------------------------------


@dataclass
class Order:
    url: str
    finalize_url: str
    authorization_urls: list[str]
    status: str = "pending"
    certificate_url: str = ""


@dataclass
class Http01:
    """One HTTP-01 challenge: what to serve, where, and where to say so."""

    url: str
    token: str
    key_authorization: str
    authorization_url: str
    #: Everything the CA said about the authorization, for diagnostics.
    raw: dict[str, Any] = field(default_factory=dict)


class AcmeClient:
    """One account against one directory. Not thread-safe; one issuance at a
    time is all the panel ever does."""

    def __init__(
        self,
        directory_url: str,
        account_key: ec.EllipticCurvePrivateKey,
        *,
        contact_email: str = "",
        user_agent: str = "softether-manager",
        timeout: float = 30.0,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.directory_url = directory_url
        self.key = account_key
        self.contact_email = contact_email.strip()
        self.user_agent = user_agent
        self.timeout = timeout
        self._log = log or (lambda message: logger.info("acme: %s", message))
        self._directory: dict[str, Any] = {}
        self._nonces: list[str] = []
        self.account_url = ""

    # -- transport --------------------------------------------------------------

    def _request(
        self, method: str, url: str, body: Optional[bytes] = None, headers: Optional[dict[str, str]] = None
    ) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("User-Agent", self.user_agent)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw_headers = {k.lower(): v for k, v in response.headers.items()}
                return response.status, raw_headers, response.read()
        except urllib.error.HTTPError as exc:
            raw_headers = {k.lower(): v for k, v in exc.headers.items()}
            return exc.code, raw_headers, exc.read()
        except urllib.error.URLError as exc:
            raise AcmeError(
                f"The certificate authority could not be reached: {exc.reason}", type="transport"
            ) from exc
        except OSError as exc:
            raise AcmeError(f"The certificate authority could not be reached: {exc}", type="transport") from exc

    def _remember_nonce(self, headers: dict[str, str]) -> None:
        nonce = headers.get("replay-nonce")
        if nonce:
            self._nonces.append(nonce)

    def directory(self) -> dict[str, Any]:
        if not self._directory:
            status, headers, body = self._request("GET", self.directory_url)
            if status != 200:
                raise AcmeError(
                    f"The ACME directory at {self.directory_url} answered HTTP {status}.", status=status
                )
            self._directory = json.loads(body)
            self._remember_nonce(headers)
        return self._directory

    def _nonce(self) -> str:
        if self._nonces:
            return self._nonces.pop()
        status, headers, _ = self._request("HEAD", self.directory()["newNonce"])
        nonce = headers.get("replay-nonce")
        if not nonce:
            raise AcmeError(f"The certificate authority gave no nonce (HTTP {status}).", status=status)
        return nonce

    def _post(
        self, url: str, payload: Optional[dict[str, Any]], *, use_jwk: bool = False
    ) -> tuple[int, dict[str, str], Any]:
        """A signed POST (or POST-as-GET when ``payload`` is None).

        A ``badNonce`` answer is retried once with the nonce the refusal
        carried, which is the protocol's own recovery for it.
        """
        for attempt in range(3):
            protected: dict[str, Any] = {"alg": "ES256", "nonce": self._nonce(), "url": url}
            if use_jwk or not self.account_url:
                protected["jwk"] = jwk_of(self.key)
            else:
                protected["kid"] = self.account_url
            protected64 = b64url(json.dumps(protected, separators=(",", ":")).encode("utf-8"))
            payload64 = "" if payload is None else b64url(
                json.dumps(payload, separators=(",", ":")).encode("utf-8")
            )
            signature = _sign_es256(self.key, f"{protected64}.{payload64}".encode("ascii"))
            body = json.dumps(
                {"protected": protected64, "payload": payload64, "signature": b64url(signature)}
            ).encode("utf-8")
            status, headers, raw = self._request(
                "POST", url, body, {"Content-Type": "application/jose+json"}
            )
            self._remember_nonce(headers)
            content_type = headers.get("content-type", "")
            parsed: Any
            if "json" in content_type:
                try:
                    parsed = json.loads(raw) if raw else {}
                except ValueError:
                    parsed = {}
            else:
                parsed = raw
            if status >= 400:
                problem = parsed if isinstance(parsed, dict) else {}
                problem_type = str(problem.get("type", ""))
                if problem_type.endswith("badNonce") and attempt < 2:
                    continue
                raise AcmeError(self._explain(problem, status), type=problem_type, status=status)
            return status, headers, parsed
        raise AcmeError("The certificate authority kept rejecting the request nonce.")

    @staticmethod
    def _explain(problem: dict[str, Any], status: int) -> str:
        detail = str(problem.get("detail") or "").strip()
        problem_type = str(problem.get("type") or "").rsplit(":", 1)[-1]
        if problem_type == "rateLimited":
            return f"Let's Encrypt rate limit: {detail or 'too many requests'}"
        if detail:
            return detail if not problem_type else f"{detail} ({problem_type})"
        return f"The certificate authority answered HTTP {status}."

    # -- the protocol --------------------------------------------------------------

    def register(self) -> str:
        """Create the account, or find the one this key already has."""
        payload: dict[str, Any] = {"termsOfServiceAgreed": True}
        if self.contact_email:
            payload["contact"] = [f"mailto:{self.contact_email}"]
        self._log("registering the account")
        status, headers, _ = self._post(self.directory()["newAccount"], payload, use_jwk=True)
        location = headers.get("location", "")
        if not location:
            raise AcmeError(f"The account was not created (HTTP {status}).", status=status)
        self.account_url = location
        return location

    def new_order(self, domain: str) -> Order:
        self._log(f"ordering a certificate for {domain}")
        _, headers, body = self._post(
            self.directory()["newOrder"], {"identifiers": [{"type": "dns", "value": domain}]}
        )
        if not isinstance(body, dict) or not body.get("finalize"):
            raise AcmeError("The order was accepted but carries no finalize URL.")
        return Order(
            url=headers.get("location", ""),
            finalize_url=str(body["finalize"]),
            authorization_urls=[str(u) for u in body.get("authorizations", [])],
            status=str(body.get("status", "pending")),
            certificate_url=str(body.get("certificate", "") or ""),
        )

    def http01(self, authorization_url: str) -> Optional[Http01]:
        """The HTTP-01 challenge of an authorization, or None when the
        authorization is already valid (a recent order for the same name)."""
        _, _, body = self._post(authorization_url, None)
        if not isinstance(body, dict):
            raise AcmeError("The authorization could not be read.")
        if body.get("status") == "valid":
            return None
        for challenge in body.get("challenges", []):
            if challenge.get("type") == "http-01":
                token = str(challenge.get("token", ""))
                return Http01(
                    url=str(challenge.get("url", "")),
                    token=token,
                    key_authorization=f"{token}.{thumbprint_of(self.key)}",
                    authorization_url=authorization_url,
                    raw=body,
                )
        raise AcmeError("The certificate authority offered no HTTP-01 challenge for this name.")

    def answer(self, challenge: Http01) -> None:
        self._log("telling the certificate authority the token is in place")
        self._post(challenge.url, {})

    def wait_valid(self, authorization_url: str, timeout: float = VALIDATION_TIMEOUT) -> None:
        """Poll until the authorization is valid; raise with the CA's reason
        when it is not."""
        deadline = time.monotonic() + timeout
        delay = 2.0
        while True:
            _, headers, body = self._post(authorization_url, None)
            status = str(body.get("status", "")) if isinstance(body, dict) else ""
            if status == "valid":
                return
            if status in ("invalid", "revoked", "deactivated", "expired"):
                raise AcmeError(self._challenge_failure(body), type="validation")
            if time.monotonic() >= deadline:
                raise AcmeError(
                    "The certificate authority did not finish validating in time; try again in a minute."
                )
            retry_after = headers.get("retry-after")
            try:
                delay = min(10.0, max(1.0, float(retry_after))) if retry_after else min(delay * 1.5, 10.0)
            except ValueError:
                delay = min(delay * 1.5, 10.0)
            time.sleep(delay)

    @staticmethod
    def _challenge_failure(authorization: Any) -> str:
        if not isinstance(authorization, dict):
            return "Validation failed."
        for challenge in authorization.get("challenges", []):
            error = challenge.get("error")
            if isinstance(error, dict) and error.get("detail"):
                return f"Validation failed: {error['detail']}"
        return "Validation failed; the certificate authority gave no reason."

    def finalize(self, order: Order, csr_der: bytes, timeout: float = FINALIZE_TIMEOUT) -> str:
        """Submit the CSR and wait for the certificate URL."""
        self._log("asking for the certificate to be signed")
        _, _, body = self._post(order.finalize_url, {"csr": b64url(csr_der)})
        if isinstance(body, dict) and body.get("certificate"):
            return str(body["certificate"])
        if not order.url:
            raise AcmeError("The order has no URL to poll for the certificate.")
        deadline = time.monotonic() + timeout
        delay = 2.0
        while True:
            _, headers, body = self._post(order.url, None)
            status = str(body.get("status", "")) if isinstance(body, dict) else ""
            if status == "valid" and isinstance(body, dict) and body.get("certificate"):
                return str(body["certificate"])
            if status == "invalid":
                error = body.get("error") if isinstance(body, dict) else None
                detail = error.get("detail") if isinstance(error, dict) else ""
                raise AcmeError(f"The order was rejected: {detail or 'no reason given'}")
            if time.monotonic() >= deadline:
                raise AcmeError("The certificate was not signed in time; try again in a minute.")
            retry_after = headers.get("retry-after")
            try:
                delay = min(10.0, max(1.0, float(retry_after))) if retry_after else min(delay * 1.5, 10.0)
            except ValueError:
                delay = min(delay * 1.5, 10.0)
            time.sleep(delay)

    def download(self, certificate_url: str) -> bytes:
        self._log("downloading the certificate")
        status, _, body = self._post(certificate_url, None)
        if isinstance(body, dict):
            raise AcmeError(f"The certificate download answered with JSON (HTTP {status}).")
        pem = bytes(body)
        if b"-----BEGIN CERTIFICATE-----" not in pem:
            raise AcmeError("The certificate download did not contain a PEM certificate.")
        return pem
