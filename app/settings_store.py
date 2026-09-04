"""Panel settings that live in the database.

The environment file under /etc seeds these on first start and is never written
again -- the service runs with ProtectSystem=full and could not write it if it
tried. Anything an operator can change from the Settings page therefore lives
here, in the one place the panel can always write: its own database.
"""
from __future__ import annotations

import json
from typing import Any

from .config import settings
from .db import get_db, utc_now

# Every key the panel knows, with its seed. Unknown keys are refused so a typo
# in a PUT cannot create an orphan nobody reads.
DEFAULTS: dict[str, Any] = {
    "web_path": None,  # seeded from the environment on first run
    # --- monitoring -----------------------------------------------------
    # Every sampler is a tick and a switch: how often it reads, and whether
    # it reads at all. Turning one off stops its thread doing work and its
    # charts filling; the data already collected stays until retention
    # prunes it.
    "resource_monitor_enabled": None,
    "resource_interval_seconds": None,
    "resource_history_points": None,
    "traffic_monitor_enabled": None,
    "sample_interval_minutes": None,
    "sample_retention_days": None,
    "session_monitor_enabled": None,
    # Sessions tick on their own clock, in seconds -- a login that lasts half
    # a minute deserves to be seen, and the byte counters above do not need
    # that resolution.
    "session_interval_seconds": None,
    # The per-session series costs one extra RPC per live session per tick,
    # so it is switchable on its own.
    "session_traffic_enabled": None,
    # How often the open page re-reads, by tier: live views, detail screens,
    # and the slower lists.
    "ui_live_seconds": None,
    "ui_detail_seconds": None,
    "ui_list_seconds": None,
    # How long a user's session log (login time, client IP, bytes moved) is
    # kept before pruning -- separate from the traffic-chart retention above,
    # since a security log and a usage graph are kept for different reasons.
    "session_history_retention_days": None,
    # Traffic quotas: whether the ceilings on hubs and configs are acted on,
    # and how often. This tick is what cuts a subject off, so it runs on its
    # own (seconds) clock rather than the traffic sampler's minutes -- and
    # costs nothing at all while no quota exists.
    "quota_enforcement_enabled": None,
    "quota_interval_seconds": None,
    "update_check_enabled": None,
    "update_check_interval_hours": None,
    # The managed SoftEther server: this machine's own instance. The password
    # is stored Fernet-encrypted; empty means "not connected yet".
    "se_host": None,
    "se_port": None,
    "se_password": None,
    # How .vpn connection files are generated: the ClientOption values, the
    # naming templates ({hub}, {username}, {host}, {port}), and whether the
    # download dialog embeds the credential by default.
    "vpn_template": None,
    "vpn_account_name_template": None,
    "vpn_filename_template": None,
    "vpn_embed_password_default": None,
}


def _seed(key: str) -> Any:
    return {
        "web_path": settings.normalised_web_path,
        "resource_monitor_enabled": True,
        "resource_interval_seconds": 3,
        "resource_history_points": 100,
        "traffic_monitor_enabled": True,
        "sample_interval_minutes": settings.sample_interval_minutes,
        "sample_retention_days": settings.sample_retention_days,
        "session_monitor_enabled": True,
        "session_interval_seconds": 60,
        "session_traffic_enabled": True,
        "ui_live_seconds": 5,
        "ui_detail_seconds": 15,
        "ui_list_seconds": 30,
        "session_history_retention_days": 30,
        "quota_enforcement_enabled": True,
        "quota_interval_seconds": 60,
        "update_check_enabled": settings.update_check_enabled,
        "update_check_interval_hours": settings.update_check_interval_hours,
        "se_host": "127.0.0.1",
        "se_port": 5555,
        "se_password": "",
        "vpn_template": dict(_vpnfile().DEFAULT_OPTIONS),
        "vpn_account_name_template": _vpnfile().DEFAULT_ACCOUNT_NAME_TEMPLATE,
        "vpn_filename_template": _vpnfile().DEFAULT_FILENAME_TEMPLATE,
        "vpn_embed_password_default": True,
    }[key]


def _vpnfile():
    from . import vpnfile

    return vpnfile


def get_setting(key: str) -> Any:
    if key not in DEFAULTS:
        raise KeyError(key)
    row = get_db().query_one(
        'SELECT "SettingValue" FROM "Setting" WHERE "SettingKey" = :key', {"key": key}
    )
    if row is None:
        return _seed(key)
    return json.loads(row["SettingValue"])


def set_setting(key: str, value: Any) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    get_db().execute(
        'INSERT INTO "Setting"("SettingKey", "SettingValue", "UpdatedDate") '
        "VALUES (:key, :value, :now) "
        'ON CONFLICT("SettingKey") DO UPDATE SET "SettingValue" = :value, "UpdatedDate" = :now',
        {"key": key, "value": json.dumps(value), "now": utc_now()},
    )


def all_settings() -> dict[str, Any]:
    return {key: get_setting(key) for key in DEFAULTS}
