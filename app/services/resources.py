"""The server's own health, read from /proc and /sys.

The panel manages the machine it runs on, so the dashboard opens with what
that machine is doing: CPU, memory, swap, disks, network. The kernel's own
files are parsed rather than adding a system-stats dependency, and everything
is bytes and percentages -- unit rendering is presentation and belongs to the
frontend.

A background thread samples every few seconds and keeps a short ring buffer,
which is what the dashboard's sparklines draw. Utilisation is only ever a
difference between two readings: cumulative counters since boot say nothing
about what the machine is doing *now*.

On a platform without /proc (the Windows development checkout), the snapshot
says so instead of reporting zeroes as measurements.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional

#: Fallbacks only. Both are settings -- see ``_settings`` below -- and are
#: re-read every tick, so changing either takes effect without a restart.
SAMPLE_SECONDS = 3.0
HISTORY_LENGTH = 100  # ~5 minutes of sparkline at the default interval

_PSEUDO_FS = {
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
    "securityfs", "pstore", "efivarfs", "bpf", "tracefs", "debugfs", "mqueue",
    "hugetlbfs", "fusectl", "configfs", "ramfs", "autofs", "binfmt_misc",
    "squashfs", "overlay", "nsfs", "rpc_pipefs",
}


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# parsers
# ---------------------------------------------------------------------------


def read_cpu_times() -> list[dict[str, Any]]:
    """One entry per /proc/stat cpu line: cumulative jiffies per state.

    The first entry is the aggregate ("cpu"), the rest per core. Guest time is
    already included in user/nice, so it is not added again.
    """
    out = []
    for line in _read("/proc/stat").splitlines():
        fields = line.split()
        if not fields or not fields[0].startswith("cpu"):
            continue
        values = [int(v) if v.isdigit() else 0 for v in fields[1:11]]
        values += [0] * (10 - len(values))
        user, nice, system, idle, iowait, irq, softirq, steal = values[:8]
        total = user + nice + system + idle + iowait + irq + softirq + steal
        out.append(
            {
                "name": fields[0],
                "user": user + nice,
                "system": system + irq + softirq,
                "idle": idle,
                "iowait": iowait,
                "steal": steal,
                "total": total,
                "busy": total - idle - iowait,
            }
        )
    return out


def cpu_delta(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Utilisation between two readings, as percentages."""
    before = {p["name"]: p for p in previous}
    out = []
    for now in current:
        past = before.get(now["name"])
        if past is None:
            continue
        total = now["total"] - past["total"]
        if total <= 0:
            continue
        share = lambda key: max(0.0, (now[key] - past[key]) / total * 100)  # noqa: E731
        out.append(
            {
                "name": now["name"],
                "usage_percent": share("busy"),
                "user_percent": share("user"),
                "system_percent": share("system"),
                "iowait_percent": share("iowait"),
                "steal_percent": share("steal"),
            }
        )
    return out


def read_load() -> dict[str, Any]:
    fields = _read("/proc/loadavg").split()
    running, total = 0, 0
    if len(fields) >= 4 and "/" in fields[3]:
        r, _, t = fields[3].partition("/")
        running, total = int(r), int(t)
    return {
        "one": float(fields[0]),
        "five": float(fields[1]),
        "fifteen": float(fields[2]),
        "running": running,
        "total": total,
    }


def read_memory() -> tuple[dict[str, Any], dict[str, Any]]:
    values: dict[str, int] = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if not fields:
            continue
        value = int(fields[0]) if fields[0].isdigit() else 0
        if len(fields) > 1 and fields[1].lower() == "kb":
            value *= 1024
        values[key.strip()] = value

    total = values.get("MemTotal", 0)
    free = values.get("MemFree", 0)
    # MemAvailable is what an operator means by "free": total-minus-free would
    # count the page cache and report a healthy machine as nearly out.
    available = values.get("MemAvailable") or (free + values.get("Buffers", 0) + values.get("Cached", 0))
    used = max(0, total - available)
    memory = {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": used,
        "free_bytes": free,
        "buffers_bytes": values.get("Buffers", 0),
        "cached_bytes": values.get("Cached", 0),
        "used_percent": (used / total * 100) if total else 0.0,
    }
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    swap = {
        "configured": swap_total > 0,
        "total_bytes": swap_total,
        "used_bytes": max(0, swap_total - swap_free),
        "used_percent": ((swap_total - swap_free) / swap_total * 100) if swap_total else 0.0,
    }
    return memory, swap


def read_disks() -> list[dict[str, Any]]:
    """Every real mounted filesystem, via statvfs."""
    seen_devices: set[str] = set()
    out = []
    try:
        mounts = _read("/proc/mounts").splitlines()
    except OSError:
        return out
    for line in mounts:
        fields = line.split()
        if len(fields) < 3:
            continue
        device, mount_point, fs_type = fields[0], fields[1], fields[2]
        if fs_type in _PSEUDO_FS or device in seen_devices:
            continue
        if not device.startswith(("/dev/", "//", "zfs")) and fs_type not in ("nfs", "nfs4"):
            continue
        seen_devices.add(device)
        try:
            stat = os.statvfs(mount_point)
        except OSError:
            continue
        total = stat.f_blocks * stat.f_frsize
        if total == 0:
            continue
        available = stat.f_bavail * stat.f_frsize
        used = total - stat.f_bfree * stat.f_frsize
        inodes_used_percent = 0.0
        if stat.f_files:
            inodes_used_percent = (stat.f_files - stat.f_ffree) / stat.f_files * 100
        out.append(
            {
                "device": device,
                "mount_point": mount_point,
                "fs_type": fs_type,
                "total_bytes": total,
                "used_bytes": used,
                "available_bytes": available,
                "used_percent": used / (used + available) * 100 if used + available else 0.0,
                "inodes_used_percent": inodes_used_percent,
            }
        )
    out.sort(key=lambda d: d["mount_point"])
    return out


def read_net_counters() -> dict[str, dict[str, int]]:
    """Per-interface cumulative rx/tx bytes and packets from /proc/net/dev."""
    out: dict[str, dict[str, int]] = {}
    for line in _read("/proc/net/dev").splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        fields = rest.split()
        if len(fields) < 16 or name == "lo":
            continue
        out[name] = {
            "rx_bytes": int(fields[0]),
            "rx_packets": int(fields[1]),
            "tx_bytes": int(fields[8]),
            "tx_packets": int(fields[9]),
        }
    return out


# ---------------------------------------------------------------------------
# the sampler
# ---------------------------------------------------------------------------


class ResourceSampler:
    """Samples on a timer; serves the latest snapshot plus sparkline history."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._available = os.path.exists("/proc/stat")
        self._snapshot: dict[str, Any] = {"available": self._available}
        self._history: list[dict[str, Any]] = []
        self._previous_cpu: list[dict[str, Any]] = []
        self._previous_net: dict[str, dict[str, int]] = {}
        self._previous_at = 0.0

    def start(self) -> None:
        if not self._available or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="resource-sampler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _settings() -> tuple[bool, float, int]:
        """(enabled, interval seconds, history points), from the database.

        Read per tick rather than cached: an operator who turns monitoring
        off, or slows it down, should not have to restart the panel to be
        obeyed. A database that cannot be read yet leaves the defaults.
        """
        try:
            from ..settings_store import get_setting

            return (
                bool(get_setting("resource_monitor_enabled")),
                float(max(1, int(get_setting("resource_interval_seconds")))),
                int(max(10, int(get_setting("resource_history_points")))),
            )
        except Exception:  # noqa: BLE001 - before the database exists, use the defaults
            return True, SAMPLE_SECONDS, HISTORY_LENGTH

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            out = dict(self._snapshot)
            out["history"] = list(self._history)
        if not self._available:
            out["reason"] = "This host has no /proc; resource monitoring runs on Linux."
        elif not self._settings()[0]:
            # Off is a state, not a failure: the card says so rather than
            # showing the last reading as though it were current.
            out["available"] = False
            out["reason"] = "Resource monitoring is switched off in Settings."
        return out

    # -- internals ----------------------------------------------------------

    def _loop(self) -> None:
        # Prime the counters so the first published reading is a real delta.
        try:
            self._previous_cpu = read_cpu_times()
            self._previous_net = read_net_counters()
            self._previous_at = time.monotonic()
        except OSError:
            pass
        self._stop.wait(1.0)
        while not self._stop.is_set():
            enabled, interval, _ = self._settings()
            if enabled:
                try:
                    self._sample()
                except Exception:  # noqa: BLE001 - a bad read skips one tick
                    pass
            else:
                # Keep the counters fresh so the first tick after being
                # switched back on is a real delta, not one covering the
                # whole time it was off.
                try:
                    self._previous_cpu = read_cpu_times()
                    self._previous_net = read_net_counters()
                    self._previous_at = time.monotonic()
                except OSError:
                    pass
            self._stop.wait(interval)

    def _sample(self) -> None:
        errors: list[str] = []
        now = time.monotonic()
        interval = max(0.001, now - self._previous_at)

        cpu: list[dict[str, Any]] = []
        try:
            current_cpu = read_cpu_times()
            cpu = cpu_delta(self._previous_cpu, current_cpu)
            self._previous_cpu = current_cpu
        except OSError as exc:
            errors.append(f"cpu: {exc}")

        try:
            load = read_load()
        except OSError as exc:
            load = {}
            errors.append(f"load: {exc}")

        try:
            memory, swap = read_memory()
        except OSError as exc:
            memory, swap = {}, {}
            errors.append(f"memory: {exc}")

        try:
            disks = read_disks()
        except OSError as exc:
            disks = []
            errors.append(f"disks: {exc}")

        interfaces: list[dict[str, Any]] = []
        rx_rate = tx_rate = 0.0
        try:
            current_net = read_net_counters()
            for name, counters in current_net.items():
                past = self._previous_net.get(name)
                entry: dict[str, Any] = {"name": name, **counters}
                if past:
                    entry["rx_bytes_per_second"] = max(0.0, (counters["rx_bytes"] - past["rx_bytes"]) / interval)
                    entry["tx_bytes_per_second"] = max(0.0, (counters["tx_bytes"] - past["tx_bytes"]) / interval)
                else:
                    entry["rx_bytes_per_second"] = 0.0
                    entry["tx_bytes_per_second"] = 0.0
                rx_rate += entry["rx_bytes_per_second"]
                tx_rate += entry["tx_bytes_per_second"]
                interfaces.append(entry)
            interfaces.sort(key=lambda i: -(i["rx_bytes"] + i["tx_bytes"]))
            self._previous_net = current_net
        except OSError as exc:
            errors.append(f"network: {exc}")
        self._previous_at = now

        overall = next((c for c in cpu if c["name"] == "cpu"), None)
        point = {
            "t": time.time(),
            "cpu": round(overall["usage_percent"], 2) if overall else 0.0,
            "memory": round(memory.get("used_percent", 0.0), 2),
            "rx": round(rx_rate, 1),
            "tx": round(tx_rate, 1),
        }

        snapshot = {
            "available": True,
            "at": time.time(),
            "interval_seconds": round(interval, 3),
            "cpu": {
                "overall": overall,
                "cores": [c for c in cpu if c["name"] != "cpu"],
                "count": max(1, len(cpu) - 1),
            },
            "load": load,
            "memory": memory,
            "swap": swap,
            "disks": disks,
            "network": {
                "interfaces": interfaces,
                "rx_bytes_per_second": rx_rate,
                "tx_bytes_per_second": tx_rate,
            },
            "errors": errors,
        }
        with self._lock:
            self._snapshot = snapshot
            self._history.append(point)
            keep = self._settings()[2]
            if len(self._history) > keep:
                del self._history[: len(self._history) - keep]


#: One sampler per process.
sampler = ResourceSampler()
