"""VM health (admin only): /health on demand; disk/memory/load/backup/OCI alarms through app/monitoring.py."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from app.features.vmhealth import vmhealth
from app.monitoring import CRITICAL, PROCESS_START, WARNING, Alarm
from app.registry import Command, Feature


def snapshot(s, with_oci: bool = True) -> vmhealth.Metrics:
    """Blocking."""
    from app.services import BACKUP_DIR
    m = vmhealth.collect(s.db, Path(os.environ.get("NARI_DATA_DIR", "/app/data")), BACKUP_DIR, PROCESS_START)
    if with_oci and s.cfg.settings.get("vm_health", {}).get("oci", True):
        m.oci = vmhealth.oci_metrics()
    return m


async def alarms(s) -> list[Alarm]:
    """Checked by the `alarms` job every 10 min. Keys keep their old names, so firing state carries over."""
    vh = s.cfg.settings.get("vm_health", {})
    if not vh.get("enabled", True):
        return []
    m = await asyncio.to_thread(snapshot, s, True)
    return [Alarm(key, firing, desc, WARNING if key in vmhealth.WARNING_ONLY else CRITICAL)
            for key, firing, desc in vmhealth.conditions(m, vh["thresholds"])]


async def cmd_health(bot, update, ctx) -> None:
    if not await bot._feature_profile(update, "vmhealth"):
        return
    await bot._reply(update, "Checking… (OCI metrics take a few seconds)")
    m = await asyncio.to_thread(snapshot, bot.services, True)
    await bot._reply(update, vmhealth.format_health(m, bot.cfg.settings["vm_health"]["thresholds"]))


FEATURE = Feature(
    name="vmhealth",
    description="VM health: /health on demand, alarms when a threshold is crossed (admin only)",
    admin_only=True,
    commands=(Command("health", "VM + OCI health: disk, memory, load, backups", cmd_health),),
    alarms=alarms,
)
