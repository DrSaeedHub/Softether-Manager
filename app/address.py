"""Print where the panel serves, as JSON, for the installer and the CLI.

The port is an environment fact; the web path lives in the database (seeded
from the environment on first start). Shell tooling needs both without
guessing, and without starting the whole application:

    python -m app.address   ->  {"host": "0.0.0.0", "port": 8000, "web_path": "abc",
                                 "domain": "panel.example.com", "https_port": 443,
                                 "domain_only": false, "https_ready": true}

``domain`` is the panel's own hostname when one is configured, ``https_ready``
whether a valid certificate for it is on disk -- so ``sem`` can print the
HTTPS address when there is one to print.
"""
from __future__ import annotations

import json

from .config import settings


def main() -> None:
    web_path = settings.normalised_web_path
    domain = ""
    https_port = 443
    domain_only = False
    https_ready = False
    try:
        from .settings_store import get_setting

        web_path = str(get_setting("web_path") or "").strip().strip("/")
        domain = str(get_setting("domain") or "")
        https_port = int(get_setting("https_port") or 443)
        domain_only = bool(get_setting("domain_only"))
    except Exception:  # noqa: BLE001 - an unreadable database falls back to the seed
        pass
    if domain:
        try:
            from .services.tls import https_ready as _ready

            https_ready = _ready(domain)
        except Exception:  # noqa: BLE001 - a missing dependency must not break the CLI
            https_ready = False
    print(
        json.dumps(
            {
                "host": settings.bind_host,
                "port": settings.bind_port,
                "web_path": web_path,
                "domain": domain,
                "https_port": https_port,
                "domain_only": domain_only,
                "https_ready": https_ready,
            }
        )
    )


if __name__ == "__main__":
    main()
