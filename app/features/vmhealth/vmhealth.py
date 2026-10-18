"""VM health (admin only): local metrics, optional OCI metrics, threshold alarms. (Job health is app/monitoring.py.)

Local metrics come from the container's view of the host (/proc and statvfs reflect the VM: Docker
doesn't namespace them here). OCI metrics use an *instance principal* — the VM authenticates as itself,
so no API key is stored — and need a dynamic group + read policy (see README). A dead bot can't alarm,
so `deploy/watchdog.sh` on the host covers "container down/unhealthy".
"""
from __future__ import annotations

import html
import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class Metrics:
    load1: float = 0.0
    load5: float = 0.0
    load15: float = 0.0
    cores: int = 1
    mem_total_mb: int = 0
    mem_avail_mb: int = 0
    swap_total_mb: int = 0
    swap_free_mb: int = 0
    disk_total_gb: float = 0.0
    disk_used_pct: float = 0.0
    host_uptime_h: float = 0.0
    bot_uptime_h: float = 0.0
    restarts_24h: int = 0
    data_mb: float = 0.0
    backups_mb: float = 0.0
    newest_backup_age_d: float | None = None
    oci: dict[str, Any] | None = None

    @property
    def mem_used_pct(self) -> float:
        return 100 * (1 - self.mem_avail_mb / self.mem_total_mb) if self.mem_total_mb else 0.0

    @property
    def swap_used_pct(self) -> float:
        return 100 * (1 - self.swap_free_mb / self.swap_total_mb) if self.swap_total_mb else 0.0


def _meminfo() -> dict[str, int]:
    out = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, _, v = line.partition(":")
        out[k] = int(v.split()[0]) // 1024  # kB -> MB
    return out


def _dir_mb(path: Path) -> float:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1e6


def collect(db: Any, data_dir: Path, backup_dir: Path, process_start: float, now: float | None = None) -> Metrics:
    now = now or time.time()
    m = Metrics()
    m.load1, m.load5, m.load15 = (float(x) for x in Path("/proc/loadavg").read_text().split()[:3])
    m.cores = os.cpu_count() or 1
    mi = _meminfo()
    m.mem_total_mb, m.mem_avail_mb = mi.get("MemTotal", 0), mi.get("MemAvailable", 0)
    m.swap_total_mb, m.swap_free_mb = mi.get("SwapTotal", 0), mi.get("SwapFree", 0)
    st = os.statvfs(data_dir)
    m.disk_total_gb = st.f_blocks * st.f_frsize / 1e9
    m.disk_used_pct = 100 * (1 - st.f_bavail / st.f_blocks) if st.f_blocks else 0.0
    m.host_uptime_h = float(Path("/proc/uptime").read_text().split()[0]) / 3600
    m.bot_uptime_h = (now - process_start) / 3600
    m.restarts_24h = restarts_24h(db, now)
    m.data_mb, m.backups_mb = _dir_mb(data_dir), _dir_mb(backup_dir) if backup_dir.exists() else 0.0
    backups = sorted(backup_dir.glob("nari-*.db")) if backup_dir.exists() else []
    m.newest_backup_age_d = (now - backups[-1].stat().st_mtime) / 86400 if backups else None
    return m


def _starts(db: Any, now: float) -> list[list]:
    raw = json.loads(db.get_kv("health:starts") or "[]")
    return [s for s in raw if isinstance(s, list) and s[0] > now - 86400]   # [timestamp, container id]


def record_start(db: Any, now: float | None = None, container: str | None = None) -> None:
    now = now or time.time()
    starts = _starts(db, now) + [[now, container or socket.gethostname()]]
    db.set_kv("health:starts", json.dumps(starts[-50:]))


def restarts_24h(db: Any, now: float | None = None) -> int:
    """Unplanned restarts: Docker restarting the SAME container (crash). A deploy creates a new container
    (new hostname = container id), so it doesn't count."""
    per_container: dict[str, int] = {}
    for _, cid in _starts(db, now or time.time()):
        per_container[cid] = per_container.get(cid, 0) + 1
    return sum(n - 1 for n in per_container.values())


# ---- OCI (instance principal) ---------------------------------------------------------------

METADATA = "http://169.254.169.254/opc/v2/instance/"


def counter_delta(points: list[float]) -> float:
    """Increase of a cumulative counter over the readings. OCI's aggregated readings sometimes dip
    slightly (ignored); only a drop to under half the previous value is a reboot reset."""
    total = 0.0
    for a, b in zip(points, points[1:], strict=False):   # consecutive pairs
        if b >= a:
            total += b - a
        elif b < a / 2:
            total += b              # counter reset: everything since the reset is new traffic
    return total


def oci_metrics(timeout: float = 20) -> dict[str, Any]:
    """Instance state + OCI agent metrics (last hour). {'error': ...} if not set up / not permitted."""
    try:
        import urllib.request
        from datetime import datetime, timedelta

        import oci

        req = urllib.request.Request(METADATA, headers={"Authorization": "Bearer Oracle"})
        meta = json.load(urllib.request.urlopen(req, timeout=5))
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        cfg = {"region": meta["canonicalRegionName"]}
        out: dict[str, Any] = {"shape": meta.get("shape"), "region": meta["canonicalRegionName"]}
        compute = oci.core.ComputeClient(cfg, signer=signer, timeout=timeout)
        out["state"] = compute.get_instance(meta["id"]).data.lifecycle_state
        mon = oci.monitoring.MonitoringClient(cfg, signer=signer, timeout=timeout)
        end = datetime.now(UTC)
        for metric, q in (("cpu_pct", "CpuUtilization[5m]{resourceId = \"%s\"}.mean()"),
                          ("mem_pct", "MemoryUtilization[5m]{resourceId = \"%s\"}.mean()"),
                          # Network bytes are cumulative counters since boot: last hour = last - first reading.
                          ("net_in_mb", "NetworksBytesIn[1m]{resourceId = \"%s\"}.max()"),
                          ("net_out_mb", "NetworksBytesOut[1m]{resourceId = \"%s\"}.max()")):
            details = oci.monitoring.models.SummarizeMetricsDataDetails(
                namespace="oci_computeagent", query=q % meta["id"], start_time=end - timedelta(hours=1), end_time=end)
            res = mon.summarize_metrics_data(meta["compartmentId"], details).data
            series = [[p.value for p in sorted(r.aggregated_datapoints, key=lambda p: p.timestamp)] for r in res]
            points = [v for ser in series for v in ser]
            if points:
                if metric.startswith("net"):        # one counter per network interface: delta each, then add
                    out[metric] = sum(counter_delta(ser) for ser in series) / 1e6
                else:
                    out[metric] = sum(points) / len(points)
                    out[metric + "_max"] = max(points)
        return out
    except Exception as e:  # noqa: BLE001 — optional; show why in /health
        msg = str(e)
        if "NotAuthorized" in msg or "404" in msg or "401" in msg:
            msg = "not permitted — create the dynamic group + policy (README → VM health)"
        return {"error": msg[:160]}


# ---- alarms -------------------------------------------------------------------------------

WARNING_ONLY = {"load", "swap"}     # 🟡: worth knowing, not urgent


def conditions(m: Metrics, t: dict[str, Any]) -> list[tuple[str, bool, str]]:
    """(alarm key, firing?, description) for every rule; firing state is compared with the last check."""
    out = [
        ("disk", m.disk_used_pct >= t["disk_pct"], f"Disk {m.disk_used_pct:.0f}% used (alarm ≥ {t['disk_pct']}%)"),
        ("memory", m.mem_used_pct >= t["mem_pct"], f"Memory {m.mem_used_pct:.0f}% used (alarm ≥ {t['mem_pct']}%)"),
        ("load", m.load15 / m.cores >= t["load_per_core"],
         f"Load {m.load15:.2f} (15-min) on {m.cores} core(s) (alarm ≥ {t['load_per_core']}/core)"),
        ("swap", m.swap_total_mb > 0 and m.swap_used_pct >= t["swap_pct"],
         f"Swap {m.swap_used_pct:.0f}% used (alarm ≥ {t['swap_pct']}%)"),
        ("restarts", m.restarts_24h >= t["restarts_24h"], f"Bot restarted {m.restarts_24h}× in 24h (alarm ≥ {t['restarts_24h']})"),
        ("backup", m.newest_backup_age_d is not None and m.newest_backup_age_d > t["backup_max_age_days"],
         f"Newest backup is {m.newest_backup_age_d or 0:.0f} days old (alarm > {t['backup_max_age_days']})"),
    ]
    if m.oci and "error" not in m.oci:
        out.append(("oci_state", m.oci.get("state") not in (None, "RUNNING"), f"OCI instance state {m.oci.get('state')}"))
    return out


# ---- formatting -----------------------------------------------------------------------------

def _icon(bad: bool) -> str:
    return "🔴" if bad else "🟢"


def format_health(m: Metrics, t: dict[str, Any]) -> str:
    firing = {k for k, f, _ in conditions(m, t) if f}
    lines = ["🩺 <b>VM health</b>"]
    lines.append(f"{_icon('disk' in firing)} Disk {m.disk_used_pct:.0f}% of {m.disk_total_gb:.0f} GB")
    lines.append(f"{_icon('memory' in firing)} Memory {m.mem_used_pct:.0f}% of {m.mem_total_mb / 1024:.1f} GB"
                 + (f" · swap {m.swap_used_pct:.0f}%" if m.swap_total_mb else " · no swap"))
    lines.append(f"{_icon('load' in firing)} Load {m.load1:.2f} / {m.load5:.2f} / {m.load15:.2f} on {m.cores} core(s)")
    lines.append(f"⏱️ VM up {m.host_uptime_h / 24:.1f} d · bot up {m.bot_uptime_h:.1f} h"
                 + (f" · {_icon('restarts' in firing)} {m.restarts_24h} restart(s) in 24h" if m.restarts_24h else ""))
    age = f"{m.newest_backup_age_d:.1f} d ago" if m.newest_backup_age_d is not None else "none yet"
    lines.append(f"{_icon('backup' in firing)} Data {m.data_mb:.2f} MB · backups {m.backups_mb:.2f} MB (newest {age})")
    if m.oci is not None:
        if "error" in m.oci:
            lines.append(f"☁️ OCI: {html.escape(m.oci['error'])}")
        else:
            o = m.oci
            parts = [f"{o.get('state')}"]
            if "cpu_pct" in o:
                parts.append(f"CPU {o['cpu_pct']:.0f}% avg / {o['cpu_pct_max']:.0f}% max")
            if "mem_pct" in o:
                parts.append(f"mem {o['mem_pct']:.0f}%")
            if "net_in_mb" in o:
                parts.append(f"net ↓{o['net_in_mb']:.1f} ↑{o.get('net_out_mb', 0):.1f} MB")
            lines.append(f"{_icon('oci_state' in firing)} OCI (1h): " + " · ".join(parts))
    return "\n".join(lines)
