"""Print where the panel serves, as JSON, for the installer and the CLI.

The port is an environment fact; the web path lives in the database (seeded
from the environment on first start). Shell tooling needs both without
guessing, and without starting the whole application:

    python -m app.address   ->  {"host": "0.0.0.0", "port": 8000, "web_path": "abc"}
"""
from __future__ import annotations

import json

from .config import settings


def main() -> None:
    web_path = settings.normalised_web_path
    try:
        from .settings_store import get_setting

        web_path = str(get_setting("web_path") or "").strip().strip("/")
    except Exception:  # noqa: BLE001 - an unreadable database falls back to the seed
        pass
    print(
        json.dumps(
            {"host": settings.bind_host, "port": settings.bind_port, "web_path": web_path}
        )
    )


if __name__ == "__main__":
    main()
