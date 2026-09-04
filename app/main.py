"""The application: one process serving the API and the built web UI.

The URL space:

    /<web-path>/api/v1/...   the JSON API
    /<web-path>/...          the built frontend (app/web/out)

The web path is a secret prefix read from the database (seeded from the
environment file on first start); empty serves everything at the root. It is
applied by mounting the real application inside an outer shell, so the
application itself never has to know its own prefix.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import APP_ROOT, settings
from .db import get_db
from .routers import auth, connection, quota, se_hub, se_rpc, se_server, system, users
from .services import sampler
from .services.resources import sampler as resource_sampler
from .version import get_version

logger = logging.getLogger(__name__)

WEB_DIST = APP_ROOT / "app" / "web" / "out"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    get_db()  # creates the schema (and migrations) on first start
    sampler.start()
    resource_sampler.start()
    yield
    sampler.stop()
    resource_sampler.stop()


def _build_core() -> FastAPI:
    app = FastAPI(
        title="SoftEther Manager",
        version=get_version(),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for router in (
        auth.router,
        system.router,
        connection.router,
        se_server.router,
        se_hub.router,
        users.router,
        quota.router,
        se_rpc.router,
    ):
        app.include_router(router, prefix="/api/v1")

    if WEB_DIST.is_dir():
        app.mount("/", _Frontend(directory=str(WEB_DIST), html=True), name="web")
    else:

        @app.get("/")
        def _no_frontend() -> JSONResponse:  # pragma: no cover - dev convenience
            return JSONResponse(
                {
                    "detail": "The web UI has not been built. Run `npm run build` in app/web, "
                    "or use the dev server on port 3000.",
                    "version": get_version(),
                }
            )

    return app


class _Frontend(StaticFiles):
    """Static files with one addition: an unknown path renders the 404 page
    the frontend exported, so a stale deep link gets the app's own answer
    rather than a bare JSON error."""

    async def get_response(self, path: str, scope):  # type: ignore[override]
        try:
            return await super().get_response(path, scope)
        except Exception:
            not_found = Path(self.directory) / "404.html"  # type: ignore[arg-type]
            if not_found.is_file():
                return FileResponse(not_found, status_code=404)
            raise


def _read_web_path() -> str:
    """The live web path. Read directly so a database that cannot be opened
    falls back to the environment seed instead of refusing to serve at all."""
    try:
        from .settings_store import get_setting

        return str(get_setting("web_path") or "").strip().strip("/")
    except Exception:  # noqa: BLE001 - the seed is the fallback
        return settings.normalised_web_path


def create_app() -> FastAPI:
    core = _build_core()
    web_path = _read_web_path()
    if not web_path:
        return core

    # A mounted application's lifespan is not run by the host, so the shell
    # carries the same one; it is idempotent (get_db caches, the sampler
    # refuses a second start).
    shell = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)

    @shell.get("/")
    def _root() -> JSONResponse:
        # Deliberately anonymous: the path is the secret, so the root neither
        # confirms what is running here nor where it is served.
        return JSONResponse({"detail": "Not found."}, status_code=404)

    shell.mount(f"/{web_path}", core)
    return shell


app = create_app()
