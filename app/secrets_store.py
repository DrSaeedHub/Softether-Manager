"""The two internal secrets the panel needs, generated and kept for it.

Neither of these is something an operator should have to invent, paste into a
file, or even see:

* the **session key** signs login tokens; if it changes, everyone is signed
  out, which is a nuisance and nothing worse;
* the **encryption passphrase** derives the key that every stored SoftEther
  administrator password and SSH credential is encrypted with. If *it* changes,
  all of that becomes unreadable -- so it must be generated once and then never
  move.

So they are generated on first start and written to the data directory beside
the database, with owner-only permissions. Backing the panel up is copying that
directory; the secrets travel with the data they protect, which is exactly what
keeps the encrypted columns readable after a restore.

An environment variable still wins when it is set, for two reasons only: a
developer pinning a value, and an operator restoring a backup who needs to
supply the passphrase it was encrypted under. Nothing writes these to the
environment file.
"""
from __future__ import annotations

import base64
import hashlib
import secrets as _secrets
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

_SESSION_KEY_FILE = "session.key"
_ENCRYPTION_KEY_FILE = "encryption.key"


def _read_or_create(path: Path, override: str) -> str:
    """Return an override if given, else the file's contents, else a new secret.

    The write is best-effort: a read-only data directory is a real deployment
    (a container with the secret injected as an env var), and it should fall
    back to a generated in-memory value rather than refuse to start.
    """
    if override:
        return override
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass

    value = _secrets.token_hex(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass  # Windows and some filesystems do not support it; harmless.
    except OSError:
        pass
    return value


@lru_cache
def session_key() -> str:
    return _read_or_create(settings.data_path / _SESSION_KEY_FILE, settings.secret_key)


@lru_cache
def encryption_passphrase() -> str:
    return _read_or_create(settings.data_path / _ENCRYPTION_KEY_FILE, settings.encryption_passphrase)


@lru_cache
def _fernet() -> Fernet:
    digest = hashlib.sha256(encryption_passphrase().encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plain: str) -> str:
    """Encrypt a credential for storage. Empty stays empty, so an optional
    column does not turn into ciphertext of nothing."""
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Decrypt a stored credential.

    An unreadable value raises rather than returning garbage: it means the
    encryption key changed underneath the database, and every caller should
    say that instead of sending an empty password somewhere.
    """
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError(
            "A stored credential could not be decrypted. The encryption key in the data "
            "directory does not match the one this value was written with."
        ) from exc
