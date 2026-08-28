"""The local-development launcher: `python run.py`.

Production runs uvicorn from the systemd unit; this exists so a checkout
starts with one command and no setup.
"""
from __future__ import annotations

import uvicorn

from app.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.bind_host,
        port=settings.bind_port,
        log_level="info",
    )
