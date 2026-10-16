"""VM + bot health (admin only): alarms every 10 min, a section in /status, old /health alias."""
from __future__ import annotations

import asyncio
import html
import os
import time
from pathlib import Path

from app.features.vmhealth import vmhealth
from app.registry import Alias, Feature, Job

PROCESS_START = time.time()


def snapshot(s, with_oci: bool = True) -> vmhealth.Metrics:
    """Blocking."""
    from app.services import BACKUP_DIR
    m = vmhealth.collect(s.db, Path(os.environ.get("NARI_DATA_DIR", "/app/data")), BACKUP_DIR, PROCESS_START)
    if with_oci and s.cfg.settings.get("vm_health", {}).get("oci", True):
        m.oci = vmhealth.oci_metrics()
    return m


async def check(s, force: bool = False) -> None:
    """Alarm once when a threshold is crossed, once when it recovers. Silent otherwise."""
    admins = s.access.recipients("vmhealth")
    if not admins:
        return
    try:
        m = await asyncio.to_thread(snapshot, s, True)
        changes = vmhealth.transitions(s.db, vmhealth.conditions(m, s.cfg.settings["vm_health"]["thresholds"]))
        for key, firing, desc in changes:
            text = (f"🚨 <b>VM alarm</b>: {html.escape(desc)}" if firing else
                    f"✅ <b>Recovered</b>: {html.escape(key)} back to normal")
            for pid in admins:
                await s.send_isolated(pid, text)
        if force and not changes:
            for pid in admins:
                await s.send_isolated(pid, "🩺 Health check: no alarm changes.")
    except Exception as e:  # noqa: BLE001
        vmhealth.record_failure(s.db, "health_check", str(e))


async def status_section(s, pid: str) -> list[str]:
    m = await asyncio.to_thread(snapshot, s, True)
    return [vmhealth.format_health(m, s.cfg.settings["vm_health"]["thresholds"])]


async def _health_alias(bot, update, ctx) -> None:
    await bot.status(update, ctx)


FEATURE = Feature(
    name="vmhealth",
    description="VM health alarms + /status (admin only)",
    admin_only=True,
    aliases=(Alias("health", _health_alias, "/status"),),
    jobs=(Job("health", check,
              lambda st: st.get("vm_health", {}).get("schedule", {"minute": "*/10"})
              if st.get("vm_health", {}).get("enabled", True) else None),),
    status_section=status_section,
)
