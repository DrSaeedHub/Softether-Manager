"""The panel about itself: health, version, settings, updates, audit."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..audit import record
from ..config import settings
from ..db import get_db
from ..deps import CurrentUser
from ..services import update_service
from ..settings_store import all_settings, set_setting
from ..version import get_version

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/health")
def health() -> dict[str, Any]:
    """Unauthenticated on purpose: it is what the installer polls to decide
    the service came up, before any account exists."""
    return {"ok": True, "version": get_version()}


@router.get("/resources")
def resources(user: dict = CurrentUser) -> dict[str, Any]:
    """The machine's own health: CPU, memory, swap, disks, network -- the
    latest reading plus the sparkline history."""
    from ..services.resources import sampler as resource_sampler

    return resource_sampler.snapshot()


class VpnTemplateIn(BaseModel):
    options: Optional[dict[str, Any]] = None
    account_name_template: Optional[str] = Field(default=None, max_length=200)
    filename_template: Optional[str] = Field(default=None, max_length=200)
    embed_password_default: Optional[bool] = None


def _vpn_template_state() -> dict[str, Any]:
    from .. import vpnfile
    from ..settings_store import get_setting

    options = vpnfile.normalize_options(get_setting("vpn_template"))
    return {
        "options": options,
        "defaults": vpnfile.DEFAULT_OPTIONS,
        "account_name_template": str(get_setting("vpn_account_name_template") or vpnfile.DEFAULT_ACCOUNT_NAME_TEMPLATE),
        "filename_template": str(get_setting("vpn_filename_template") or vpnfile.DEFAULT_FILENAME_TEMPLATE),
        "embed_password_default": bool(get_setting("vpn_embed_password_default")),
        "template": vpnfile.template_text(options),
    }


@router.get("/vpn-template")
def get_vpn_template(user: dict = CurrentUser) -> dict[str, Any]:
    """How .vpn connection files are built: the editable options, the naming
    templates, and the resulting document with per-user {fields} visible."""
    return _vpn_template_state()


@router.put("/vpn-template")
def put_vpn_template(body: VpnTemplateIn, user: dict = CurrentUser) -> dict[str, Any]:
    from .. import vpnfile

    if body.options is not None:
        set_setting("vpn_template", vpnfile.normalize_options(body.options))
    if body.account_name_template is not None:
        set_setting("vpn_account_name_template", body.account_name_template.strip() or vpnfile.DEFAULT_ACCOUNT_NAME_TEMPLATE)
    if body.filename_template is not None:
        set_setting("vpn_filename_template", body.filename_template.strip() or vpnfile.DEFAULT_FILENAME_TEMPLATE)
    if body.embed_password_default is not None:
        set_setting("vpn_embed_password_default", body.embed_password_default)
    record(user, "settings.vpn_template_updated", "panel", "", "")
    return _vpn_template_state()


@router.get("/info")
def info(user: dict = CurrentUser) -> dict[str, Any]:
    return {
        "version": get_version(),
        "release_repo": update_service.installed_repository(),
        "service": update_service.service_status(),
        "env_file": settings.env_file_path,
    }


class SettingsIn(BaseModel):
    web_path: Optional[str] = Field(default=None, max_length=64)
    resource_monitor_enabled: Optional[bool] = None
    resource_interval_seconds: Optional[int] = Field(default=None, ge=1, le=3600)
    resource_history_points: Optional[int] = Field(default=None, ge=10, le=2000)
    traffic_monitor_enabled: Optional[bool] = None
    sample_interval_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    sample_retention_days: Optional[int] = Field(default=None, ge=1, le=3650)
    session_monitor_enabled: Optional[bool] = None
    session_interval_seconds: Optional[int] = Field(default=None, ge=5, le=3600)
    session_traffic_enabled: Optional[bool] = None
    session_history_retention_days: Optional[int] = Field(default=None, ge=1, le=3650)
    ui_live_seconds: Optional[int] = Field(default=None, ge=1, le=3600)
    ui_detail_seconds: Optional[int] = Field(default=None, ge=1, le=3600)
    ui_list_seconds: Optional[int] = Field(default=None, ge=1, le=3600)
    update_check_enabled: Optional[bool] = None
    update_check_interval_hours: Optional[int] = Field(default=None, ge=1, le=168)


@router.get("/settings")
def get_panel_settings(user: dict = CurrentUser) -> dict[str, Any]:
    return all_settings()


@router.put("/settings")
def put_panel_settings(body: SettingsIn, user: dict = CurrentUser) -> dict[str, Any]:
    changed: list[str] = []
    for key, value in body.model_dump(exclude_none=True).items():
        if key == "web_path":
            value = str(value).strip().strip("/")
            if value and not all(c.isalnum() or c in "._~-" for c in value):
                raise HTTPException(
                    status_code=422,
                    detail="The web path may contain only letters, digits, dot, underscore, "
                    "tilde and hyphen.",
                )
        set_setting(key, value)
        changed.append(key)
    if changed:
        record(user, "settings.updated", "panel", "", ", ".join(changed))
    out = all_settings()
    out["restart_required"] = "web_path" in changed
    return out


# --- updates -----------------------------------------------------------------


@router.get("/update")
def update_status(user: dict = CurrentUser) -> dict[str, Any]:
    from ..settings_store import get_setting

    enabled = bool(get_setting("update_check_enabled"))
    interval = int(get_setting("update_check_interval_hours"))
    out = update_service.checker.status(enabled=enabled, interval_hours=interval)
    out["can_apply"] = update_service.applier.unavailable_reason() is None
    out["unavailable_reason"] = update_service.applier.unavailable_reason() or ""
    return out


@router.post("/update/check")
def update_check(user: dict = CurrentUser) -> dict[str, Any]:
    out = update_service.checker.refresh()
    out["can_apply"] = update_service.applier.unavailable_reason() is None
    out["unavailable_reason"] = update_service.applier.unavailable_reason() or ""
    return out


class UpdateApplyIn(BaseModel):
    version: str = ""


@router.post("/update/apply")
def update_apply(body: UpdateApplyIn, user: dict = CurrentUser) -> dict[str, Any]:
    try:
        state = update_service.applier.start(body.version or None, started_by=user["Username"])
    except update_service.UpdateUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except update_service.UpdateAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    record(user, "panel.update_started", "panel", state.get("target_version", "latest"))
    return state


@router.get("/update/state")
def update_state(user: dict = CurrentUser) -> dict[str, Any]:
    return update_service.applier.state()


@router.post("/restart")
def restart(user: dict = CurrentUser) -> dict[str, Any]:
    try:
        update_service.restart_service()
    except update_service.UpdateUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    record(user, "panel.restarted", "panel")
    return {"ok": True, "detail": "Restarting in a moment."}


# --- audit ---------------------------------------------------------------------


@router.get("/audit")
def audit_log(limit: int = 100, before_id: int = 0, user: dict = CurrentUser) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    where = 'WHERE "AuditLogID" < :before' if before_id else ""
    rows = get_db().query_all(
        f'SELECT "AuditLogID" AS id, "Username" AS username, "Action" AS action, '
        f'"TargetType" AS target_type, "TargetKey" AS target_key, "Detail" AS detail, '
        f'"CreatedDate" AS created_date FROM "AuditLog" {where} '
        f'ORDER BY "AuditLogID" DESC LIMIT :limit',
        {"limit": limit, "before": before_id},
    )
    return rows
