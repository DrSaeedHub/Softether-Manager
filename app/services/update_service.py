"""Checking for a newer release, and installing one from the panel.

Neither half does the work itself. The check reads the release host's own
index over HTTPS; the install goes through ``sem update``, which goes through
the installer -- the same path an operator would take from a shell, so a panel
that updated itself and a panel updated by hand end up in exactly the same
state.

What this module adds is the plumbing that makes that possible from inside a
request:

* a cached check that never blocks the caller on the network, because this is
  read every time a page is opened and the panel is often on a server with no
  outbound access at all, where every check ends in a timeout;
* a launcher that starts the installer **outside** the panel's own service.
  That is not a nicety. The installer restarts ``softether-manager.service``,
  and anything this process forked would be a child in that service's cgroup,
  killed by the restart halfway through replacing itself. A transient systemd
  unit belongs to systemd, not to the panel, so it survives.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings
from ..version import get_version, is_newer, parse_version

logger = logging.getLogger(__name__)

#: The transient unit an update runs in. Fixed rather than generated, so a
#: panel that restarted in the middle of an update -- which is what an update
#: does -- can find the run it started before it was replaced.
UPDATE_UNIT = "softether-manager-update"
#: The transient unit that restarts the service on demand.
RESTART_UNIT = "softether-manager-restart"

CHECK_TIMEOUT_SECONDS = 15
MAX_RUN_SECONDS = 30 * 60
LOG_TAIL_LINES = 200
NOTES_LIMIT = 8000


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def installed_repository() -> str:
    """The repository this host was installed from -- recorded, not assumed,
    so a fork installed from its own releases checks its own releases."""
    recorded = _read_env_file(settings.cli_env_path).get("RELEASE_REPO", "").strip()
    return recorded or settings.release_repo


def under_systemd() -> bool:
    return Path("/run/systemd/system").is_dir()


def _run(argv: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, ValueError) as exc:
        return 127, str(exc)
    except subprocess.TimeoutExpired:
        return 124, "the command did not finish in time"
    return out.returncode, (out.stdout or "") + (out.stderr or "")


# ---------------------------------------------------------------------------
# checking
# ---------------------------------------------------------------------------


class UpdateChecker:
    """Holds the last answer from the release host and decides when to ask again.

    Nothing here ever blocks a request on the network unless waiting was asked
    for -- a stale answer is refreshed in a background thread and the caller is
    told a check is running.
    """

    def __init__(self, repo: str | None = None) -> None:
        self._repo = repo or installed_repository()
        self._lock = threading.Lock()
        self._checking = False
        self._checked_at: float | None = None
        self._checked_wall = ""
        self._latest: dict[str, str] = {}
        self._error = ""

    @property
    def api_url(self) -> str:
        return f"https://api.github.com/repos/{self._repo}/releases/latest"

    def status(self, *, enabled: bool, interval_hours: int) -> dict[str, Any]:
        if enabled and self._stale(interval_hours) and self._begin():
            threading.Thread(target=self._fetch, daemon=True, name="update-check").start()
        return self._snapshot(enabled=enabled)

    def refresh(self, *, enabled: bool = True) -> dict[str, Any]:
        """Ask the release host now and wait: the "check again" button."""
        if self._begin():
            self._fetch()
        else:
            # A check is already in flight; wait briefly for it to finish.
            for _ in range(CHECK_TIMEOUT_SECONDS * 4):
                with self._lock:
                    if not self._checking:
                        break
                time.sleep(0.25)
        return self._snapshot(enabled=enabled)

    # -- internals ----------------------------------------------------------

    def _stale(self, interval_hours: int) -> bool:
        with self._lock:
            if self._checking:
                return False
            if self._checked_at is None:
                return True
            return (time.monotonic() - self._checked_at) >= max(1, interval_hours) * 3600

    def _begin(self) -> bool:
        with self._lock:
            if self._checking:
                return False
            self._checking = True
            return True

    def _fetch(self) -> None:
        try:
            release = self._lookup()
        except Exception as exc:  # noqa: BLE001 - the reason is shown to the operator
            with self._lock:
                self._error = str(exc)
        else:
            with self._lock:
                self._error = ""
                self._latest = release
        finally:
            with self._lock:
                self._checking = False
                self._checked_at = time.monotonic()
                self._checked_wall = _now_iso()

    def _lookup(self) -> dict[str, str]:
        request = urllib.request.Request(
            self.api_url,
            headers={
                "Accept": "application/vnd.github+json",
                # GitHub refuses a request with no User-Agent outright.
                "User-Agent": f"softether-manager/{get_version()}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=CHECK_TIMEOUT_SECONDS) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
            if exc.code == 404:
                # A private repository answers 404 to an anonymous request.
                # The gh CLI, when present and authenticated, can still ask --
                # the same fallback the installer uses to download the assets.
                body = self._lookup_via_gh()
                if body is None:
                    raise RuntimeError(
                        f"{self._repo} has no releases, or is not a repository this panel can see."
                    ) from exc
            elif exc.code in (403, 429):
                raise RuntimeError(
                    "The release host is rate limiting this address; the next check will try again."
                ) from exc
            else:
                raise RuntimeError(f"The release host answered HTTP {exc.code}.") from exc
        except OSError as exc:
            raise RuntimeError(f"The release host could not be reached: {exc}") from exc

        tag = str(body.get("tag_name") or "").strip()
        if not tag:
            raise RuntimeError("The release host named no version.")
        notes = str(body.get("body") or "")
        if len(notes) > NOTES_LIMIT:
            notes = notes[:NOTES_LIMIT] + "\n..."
        return {
            "version": tag,
            "name": str(body.get("name") or "").strip(),
            "url": str(body.get("html_url") or ""),
            "published_at": str(body.get("published_at") or ""),
            "notes": notes,
        }

    def _lookup_via_gh(self) -> dict[str, Any] | None:
        if shutil.which("gh") is None:
            return None
        # First directly, then -- because the panel's own unit runs with
        # ProtectHome=yes, which hides the /root/.config/gh credentials from
        # it -- through a transient unit outside the sandbox, the same escape
        # hatch the updater itself uses.
        attempts: list[list[str]] = [["gh", "api", f"repos/{self._repo}/releases/latest"]]
        if under_systemd() and shutil.which("systemd-run") is not None:
            attempts.append(
                [
                    "systemd-run", "--wait", "--pipe", "--collect", "--quiet",
                    "-p", "User=root",
                    "gh", "api", f"repos/{self._repo}/releases/latest",
                ]
            )
        env = dict(os.environ)
        env.setdefault("HOME", "/root")
        for argv in attempts:
            # stdout only -- gh prints upgrade notices to stderr, which must
            # not end up concatenated into the JSON.
            try:
                out = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=CHECK_TIMEOUT_SECONDS,
                    check=False,
                    env=env,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if out.returncode != 0:
                logger.debug("gh release lookup failed: %s", (out.stderr or "").strip()[:300])
                continue
            try:
                parsed = json.loads(out.stdout or "")
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    def _snapshot(self, *, enabled: bool) -> dict[str, Any]:
        current = get_version()
        with self._lock:
            latest = dict(self._latest)
            out: dict[str, Any] = {
                "current_version": current,
                "latest": {
                    "version": latest.get("version", ""),
                    "name": latest.get("name", ""),
                    "url": latest.get("url", ""),
                    "published_at": latest.get("published_at", ""),
                    "notes": latest.get("notes", ""),
                },
                "update_available": False,
                "checked_at": self._checked_wall,
                "checking": self._checking,
                "error": self._error,
                "note": "",
                "source": self._repo,
                "enabled": enabled,
            }
        latest_version = out["latest"]["version"]
        if not latest_version:
            return out
        out["update_available"] = is_newer(current, latest_version)
        if not out["update_available"]:
            running = parse_version(current)
            if running is None or not running.is_release:
                out["note"] = (
                    "This build did not come from a release, so it cannot be compared against one."
                )
        return out


# ---------------------------------------------------------------------------
# applying
# ---------------------------------------------------------------------------


class UpdateUnavailable(RuntimeError):
    """This installation cannot update itself, carrying the reason why."""


class UpdateAlreadyRunning(RuntimeError):
    pass


class UpdateApplier:
    """Starts an update, and reports on the one it started.

    The state of a run cannot be held in memory: the process that starts an
    update is not the process that reports on it, because the update replaces
    it. So it is written to a file before the launch, and the outcome is read
    back from the transient unit -- which outlives the restart -- or, when that
    unit is gone entirely, inferred from whether the running version changed.
    """

    def __init__(self) -> None:
        self._data_dir = Path(settings.data_dir)
        self._state_path = self._data_dir / "update-state.json"
        self._log_path = self._data_dir / "update.log"
        self._lock = threading.Lock()

    # -- capability ---------------------------------------------------------

    def unavailable_reason(self) -> str | None:
        if not under_systemd():
            return (
                "This panel is not running under systemd, so it cannot restart itself into a "
                "new version. Update it the way it was installed."
            )
        if shutil.which("systemd-run") is None:
            return (
                "systemd-run was not found on this host, and the update has to run outside the "
                "panel's own service to survive the restart in the middle of it."
            )
        if not Path(settings.cli_path).exists():
            return (
                f"The {Path(settings.cli_path).name} command-line tool is not installed, and it "
                "is what runs the installer. Reinstall it, or update the panel from a shell."
            )
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            return "The panel is not running as root, so it cannot install a new version."
        if not os.access(self._data_dir, os.W_OK):
            return f"{self._data_dir} is not writable, so an update could not be recorded."
        return None

    # -- state --------------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"stage": "idle"}

    def _write(self, state: dict[str, Any]) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(state), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - a read-only data dir
            logger.warning("could not record the update state: %s", exc)

    def _log_tail(self) -> list[str]:
        try:
            lines = self._log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return lines[-LOG_TAIL_LINES:]

    def state(self) -> dict[str, Any]:
        with self._lock:
            resolved = dict(self._resolve(self._read()))
        resolved["log"] = self._log_tail()
        return resolved

    # -- starting -----------------------------------------------------------

    def start(self, version: str | None, started_by: str) -> dict[str, Any]:
        reason = self.unavailable_reason()
        if reason:
            raise UpdateUnavailable(reason)

        with self._lock:
            current = self._resolve(self._read())
            if current.get("stage") == "running":
                raise UpdateAlreadyRunning(
                    "An update is already running. Watch this one rather than starting a second."
                )

            target = (version or "").strip() or "latest"
            if target != "latest" and parse_version(target) is None:
                raise ValueError(f"{target} is not a version this panel can install.")

            self._clear_unit()
            self._truncate_log()

            state = {
                "stage": "running",
                "target_version": target,
                "from_version": get_version(),
                "started_at": _now_iso(),
                "finished_at": "",
                "error": "",
                "unit": UPDATE_UNIT,
                "started_by": started_by,
            }
            # Written before the launch, not after: the installer replaces this
            # process, and a state file written afterwards can lose that race.
            self._write(state)

            try:
                self._launch(target)
            except Exception as exc:  # noqa: BLE001
                state["stage"] = "failed"
                state["finished_at"] = _now_iso()
                state["error"] = str(exc)
                self._write(state)
                raise

            out = dict(state)
            out["log"] = self._log_tail()
            return out

    def _truncate_log(self) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            self._log_path.write_text("", encoding="utf-8")
        except OSError:
            pass

    def _launch(self, target: str) -> None:
        """Start the transient unit, with the installer's output captured.

        Redirecting a transient unit's output to a file needs systemd 240; on
        anything older the properties are refused outright, so the run is
        retried without them and the journal becomes the only record.
        """
        install_argv = [settings.cli_path, "update", "--yes", "--version", target]

        code, output = _run([*self._unit_argv(redirect=True), *install_argv])
        if code == 0:
            return
        logger.warning("starting the update with redirected output failed: %s", output.strip())

        self._clear_unit()
        code, output = _run([*self._unit_argv(redirect=False), *install_argv])
        if code != 0:
            raise RuntimeError(
                f"The update could not be started: {output.strip() or 'systemd-run failed'}"
            )

    def _unit_argv(self, *, redirect: bool) -> list[str]:
        argv = [
            "systemd-run",
            f"--unit={UPDATE_UNIT}",
            "--description=SoftEther Manager update",
            # The unit stays loaded after the command exits, which is what lets
            # the panel -- restarted by that very command -- come back and read
            # whether it worked.
            "--remain-after-exit",
        ]
        if redirect:
            argv += [
                f"--property=StandardOutput=append:{self._log_path}",
                f"--property=StandardError=append:{self._log_path}",
            ]
        return argv

    def _clear_unit(self) -> None:
        _run(["systemctl", "stop", UPDATE_UNIT])
        _run(["systemctl", "reset-failed", UPDATE_UNIT])

    # -- resolving ----------------------------------------------------------

    def _resolve(self, state: dict[str, Any]) -> dict[str, Any]:
        """Decide what became of a run that is still marked running."""
        if state.get("stage") != "running":
            return state

        unit = self._unit_status()
        if unit["loaded"] and unit["active"]:
            if self._started_long_ago(state):
                return self._finish(
                    state,
                    "failed",
                    "The update has been running for longer than half an hour. Check the log "
                    "below, and the panel's service.",
                )
            return state

        if unit["loaded"] and unit["failed"]:
            detail = unit["result"] or "the installer exited non-zero"
            return self._finish(state, "failed", f"The installer did not finish: {detail}.")

        if unit["loaded"] and unit["exited"]:
            if unit["status"] == 0:
                return self._finish(state, "succeeded", "")
            return self._finish(
                state, "failed", f"The installer exited with status {unit['status']}."
            )

        # The unit is not there at all: it never started, the host rebooted, or
        # somebody reset it. The version is the only evidence left, and it is
        # good evidence -- this process is the one the installer would have
        # replaced.
        if get_version() != state.get("from_version"):
            return self._finish(state, "succeeded", "")
        if self._started_long_ago(state):
            return self._finish(
                state,
                "failed",
                "The update service is no longer there and the panel is still on the same version.",
            )
        return state

    def _started_long_ago(self, state: dict[str, Any]) -> bool:
        try:
            started = datetime.fromisoformat(str(state.get("started_at")))
        except (TypeError, ValueError):
            return False
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - started).total_seconds() > MAX_RUN_SECONDS

    def _finish(self, state: dict[str, Any], stage: str, reason: str) -> dict[str, Any]:
        state = dict(state)
        state["stage"] = stage
        state["finished_at"] = _now_iso()
        state["error"] = "" if stage == "succeeded" else reason
        self._write(state)
        return state

    def _unit_status(self) -> dict[str, Any]:
        code, output = _run(
            [
                "systemctl",
                "show",
                UPDATE_UNIT,
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=Result",
                "--property=ExecMainStatus",
            ]
        )
        values: dict[str, str] = {}
        if code == 0:
            for line in output.splitlines():
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
        load = values.get("LoadState", "")
        active = values.get("ActiveState", "")
        sub = values.get("SubState", "")
        result = values.get("Result", "")
        try:
            status = int(values.get("ExecMainStatus") or 0)
        except ValueError:
            status = 0
        return {
            "loaded": load == "loaded",
            "active": active == "active" and sub != "exited",
            "exited": sub == "exited" or active == "inactive",
            "failed": active == "failed",
            "result": "" if result in ("", "success") else result,
            "status": status,
        }


def restart_service(delay_seconds: int = 2) -> None:
    """Restart the panel's own service, from inside it.

    Same problem as an update and the same answer: a restart issued as a child
    of this service would be killed by the restart it asked for. The delay is
    what gives the response carrying "restarting now" time to reach the browser
    before the connection carrying it is closed.
    """
    if not under_systemd():
        raise UpdateUnavailable("This panel is not running under systemd, so it cannot restart itself.")
    if shutil.which("systemd-run") is None:
        raise UpdateUnavailable("systemd-run was not found on this host, so the panel cannot restart itself.")

    _run(["systemctl", "reset-failed", RESTART_UNIT])
    code, output = _run(
        [
            "systemd-run",
            f"--unit={RESTART_UNIT}",
            "--collect",
            f"--on-active={max(1, delay_seconds)}s",
            "--description=SoftEther Manager restart",
            "systemctl",
            "restart",
            settings.service_name,
        ]
    )
    if code != 0:
        raise UpdateUnavailable(
            f"The restart could not be scheduled: {output.strip() or 'systemd-run failed'}"
        )


def service_status() -> dict[str, Any]:
    """What systemd says about the panel's own unit, for the Settings page."""
    if not under_systemd():
        return {"managed": False, "active": "", "sub": "", "since": "", "unit": settings.service_name}
    code, output = _run(
        [
            "systemctl",
            "show",
            settings.service_name,
            "--property=ActiveState",
            "--property=SubState",
            "--property=ActiveEnterTimestamp",
        ]
    )
    values: dict[str, str] = {}
    if code == 0:
        for line in output.splitlines():
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return {
        "managed": bool(values.get("ActiveState")),
        "active": values.get("ActiveState", ""),
        "sub": values.get("SubState", ""),
        "since": values.get("ActiveEnterTimestamp", ""),
        "unit": settings.service_name,
    }


#: One checker and one applier per process. Both hold state that must not be
#: duplicated: the cache, and the lock that stops two operators starting two
#: installers at the same moment.
checker = UpdateChecker()
applier = UpdateApplier()
