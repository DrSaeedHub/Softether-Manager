"""Application configuration.

There is no configuration file to write. Everything here has a working default,
so a fresh checkout and a fresh install both start with nothing but the command
that started them.

What *can* be set comes from real environment variables, which is what systemd
hands the service from ``/etc/softether-manager.env``. That file holds only
deployment facts -- where the data lives, what the service is called, which
repository to follow -- and never a secret or a password: those are generated on
first start (see :mod:`app.secrets_store`) or changed from the panel and the
``sem`` command, both of which write to the database.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

# app/config.py -> app -> <application root>
APP_ROOT = Path(__file__).resolve().parents[1]


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


class Settings:
    """Runtime settings, every one of them optional."""

    def __init__(self) -> None:
        # --- storage ---
        # Everything the panel owns lives in one directory, and the database is
        # one file inside it. Backing the panel up is copying that directory.
        # The default is inside the checkout, which is what makes
        # `python run.py` work with no setup; the installer overrides it.
        self.data_dir: str = _env("SEM_DATA_DIR", str(APP_ROOT / "data"))
        # A database URL. The scheme picks the implementation in app.db.factory;
        # empty means "sqlite file inside the data directory", which is what
        # every real installation uses today. A future PostgreSQL deployment
        # sets SEM_DATABASE_URL=postgresql://... and implements the same
        # Database interface -- nothing above the factory changes.
        self.database_url: str = _env("SEM_DATABASE_URL", "")

        # --- where the panel serves ---
        self.bind_host: str = _env("SEM_BIND_HOST", "0.0.0.0")
        self.bind_port: int = _env_int("SEM_BIND_PORT", 8000)
        # A secret URL prefix the whole application is mounted under; empty
        # serves it at the root. This is a *seed*: the live value lives in the
        # database, because the service runs with ProtectSystem=full and cannot
        # write /etc, so a change made from the Settings page could never be
        # written back here.
        self.web_path: str = _env("SEM_WEB_PATH", "")

        # --- security ---
        # Both of these are normally empty and generated on first start. They
        # exist as settings only so a developer can pin them, and so an operator
        # restoring a backup can supply the passphrase it was encrypted under.
        self.secret_key: str = _env("SEM_SECRET_KEY", "")
        self.encryption_passphrase: str = _env("SEM_ENCRYPTION_PASSPHRASE", "")
        self.session_expire_minutes: int = _env_int("SEM_SESSION_EXPIRE_MINUTES", 60 * 24 * 7)

        # --- installed layout (set by the installer; harmless for a checkout) ---
        self.service_name: str = _env("SEM_SERVICE_NAME", "softether-manager")
        self.cli_path: str = _env("SEM_CLI_PATH", "/usr/local/bin/sem")
        self.cli_env_path: str = _env("SEM_CLI_ENV_PATH", "/usr/local/share/softether-manager/cli.env")
        self.env_file_path: str = _env("SEM_ENV_FILE", "/etc/softether-manager.env")
        self.release_repo: str = _env("SEM_RELEASE_REPO", "DrSaeedHub/Softether-Manager")

        # --- behaviour ---
        self.cors_origins: str = _env("SEM_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
        self.update_check_enabled: bool = _env_bool("SEM_UPDATE_CHECK", True)
        self.update_check_interval_hours: int = _env_int("SEM_UPDATE_CHECK_HOURS", 6)
        # How often the background sampler snapshots per-user and per-hub
        # traffic counters, and how long the samples are kept. Both can be
        # changed from the Settings page; these are the first-start defaults.
        self.sample_interval_minutes: int = _env_int("SEM_SAMPLE_INTERVAL_MINUTES", 5)
        self.sample_retention_days: int = _env_int("SEM_SAMPLE_RETENTION_DAYS", 90)

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser()

    @property
    def effective_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_path / 'manager.db').as_posix()}"

    @property
    def normalised_web_path(self) -> str:
        """The seed web path with any surrounding slashes removed."""
        return self.web_path.strip().strip("/")

    @property
    def cors_origin_list(self) -> list[str]:
        origins = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        return list(dict.fromkeys(origins))


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
