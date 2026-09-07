"""The panel about itself: health, version, settings, updates, audit."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import hostguard
from ..audit import record
from ..config import settings
from ..db import get_db
from ..deps import CurrentUser
from ..services import tls, update_service
from ..settings_store import all_settings, get_setting, set_setting
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
    quota_enforcement_enabled: Optional[bool] = None
    quota_interval_seconds: Optional[int] = Field(default=None, ge=10, le=3600)
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


# --- the panel's domain and certificate ----------------------------------------


class DomainIn(BaseModel):
    """Every field optional: a PUT changes what it names and keeps the rest."""

    domain: Optional[str] = Field(default=None, max_length=253)
    https_port: Optional[int] = Field(default=None, ge=1, le=65535)
    acme_email: Optional[str] = Field(default=None, max_length=254)
    acme_staging: Optional[bool] = None
    domain_only: Optional[bool] = None


def _domain_state(request: Request) -> dict[str, Any]:
    """The status, plus what this request tells us: the name it came in on,
    and therefore whether the operator is already using the domain -- the
    fact the domain-only switch is gated on."""
    status = tls.manager.status()
    came_in_on = hostguard.request_host(request.scope.get("headers", ()))
    status["request_host"] = came_in_on
    status["via_domain"] = bool(status["domain"]) and came_in_on == status["domain"]
    status["via_loopback"] = hostguard.is_loopback(request.scope)
    status["bind_port"] = settings.bind_port
    status["web_path"] = str(get_setting("web_path") or "").strip().strip("/")
    return status


@router.get("/domain")
def get_domain(request: Request, user: dict = CurrentUser) -> dict[str, Any]:
    return _domain_state(request)


@router.put("/domain")
def put_domain(body: DomainIn, request: Request, user: dict = CurrentUser) -> dict[str, Any]:
    changed: list[str] = []
    current_domain = str(get_setting("domain") or "")
    next_domain = current_domain

    if body.domain is not None:
        try:
            next_domain = tls.normalise_domain(body.domain)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    if body.https_port is not None:
        if body.https_port == tls.HTTP_PORT:
            raise HTTPException(
                status_code=422,
                detail="Port 80 carries the certificate validation and the redirect to HTTPS; choose another port for HTTPS itself.",
            )
        if body.https_port == settings.bind_port:
            raise HTTPException(
                status_code=422,
                detail=f"The panel already serves plain HTTP on port {settings.bind_port}; HTTPS needs a port of its own.",
            )

    if body.acme_email is not None:
        email = body.acme_email.strip()
        if email and ("@" not in email or " " in email or email.startswith("@") or email.endswith("@")):
            raise HTTPException(status_code=422, detail="That does not look like an email address.")

    # The lock-out guard: domain-only can only be switched on by somebody
    # who is already reaching the panel through the domain it names. A
    # request from the machine itself does not count -- it would still get
    # in afterwards, which proves nothing about anyone else.
    wants_only = body.domain_only if body.domain_only is not None else bool(get_setting("domain_only"))
    came_in_on = hostguard.request_host(request.scope.get("headers", ()))
    if body.domain_only and not bool(get_setting("domain_only")):
        if not next_domain:
            raise HTTPException(status_code=422, detail="Set a domain before restricting access to it.")
        if came_in_on != next_domain:
            raise HTTPException(
                status_code=422,
                detail=f"Open the panel through https://{next_domain}/ first and turn this on from there; "
                f"this request came in as {came_in_on or 'an unnamed host'}, and the restriction would have locked you out.",
            )

    if body.domain is not None and next_domain != current_domain:
        set_setting("domain", next_domain)
        changed.append("domain")
        # A restriction naming a domain that no longer applies is a lock-out
        # in waiting; it does not survive the change.
        if wants_only and body.domain_only is None:
            set_setting("domain_only", False)
            changed.append("domain_only")
        tls.manager.forget(current_domain)
    if body.https_port is not None and body.https_port != int(get_setting("https_port") or 443):
        set_setting("https_port", body.https_port)
        changed.append("https_port")
    if body.acme_email is not None and body.acme_email.strip() != str(get_setting("acme_email") or ""):
        set_setting("acme_email", body.acme_email.strip())
        changed.append("acme_email")
    if body.acme_staging is not None and body.acme_staging != bool(get_setting("acme_staging")):
        set_setting("acme_staging", body.acme_staging)
        changed.append("acme_staging")
        tls.manager.forget(next_domain)
    if body.domain_only is not None and body.domain_only != bool(get_setting("domain_only")):
        if not next_domain and body.domain_only:
            raise HTTPException(status_code=422, detail="Set a domain before restricting access to it.")
        set_setting("domain_only", body.domain_only)
        changed.append("domain_only")

    if changed:
        # Installed immediately, not at the manager's next tick: a request
        # that turned the restriction on must already be bound by it.
        hostguard.configure(str(get_setting("domain") or ""), bool(get_setting("domain_only")))
        tls.manager.apply()
        record(user, "domain.updated", "panel", str(get_setting("domain") or ""), ", ".join(changed))
    out = _domain_state(request)
    out["changed"] = changed
    return out


@router.delete("/domain")
def delete_domain(request: Request, user: dict = CurrentUser) -> dict[str, Any]:
    """Forget the domain: the listeners stop, the restriction lifts, the
    certificate files stay on disk in case the same name comes back."""
    previous = str(get_setting("domain") or "")
    set_setting("domain", "")
    set_setting("domain_only", False)
    hostguard.configure("", False)
    tls.manager.forget(previous)
    tls.manager.apply()
    record(user, "domain.removed", "panel", previous, "")
    return _domain_state(request)


@router.post("/domain/issue")
def issue_domain_certificate(request: Request, user: dict = CurrentUser) -> dict[str, Any]:
    """Obtain (or renew) the certificate now rather than on the schedule."""
    if not str(get_setting("domain") or ""):
        raise HTTPException(status_code=422, detail="Set a domain first.")
    if not tls.manager.request_issue():
        raise HTTPException(
            status_code=409,
            detail="A certificate request is already running; watch this one rather than starting another.",
        )
    record(user, "domain.certificate_requested", "panel", str(get_setting("domain") or ""), "")
    out = _domain_state(request)
    out["busy"] = True
    out["phase"] = "issuing" if not out["certificate"].get("present") else "renewing"
    return out


@router.post("/domain/check")
def check_domain(request: Request, user: dict = CurrentUser) -> dict[str, Any]:
    """Re-run the DNS lookup and the listener supervision, and report."""
    tls.manager.check_now()
    return _domain_state(request)


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
