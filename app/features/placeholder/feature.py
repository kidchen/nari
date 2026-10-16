"""Monthly parking placeholder image (admin only)."""
from __future__ import annotations

import asyncio

from app.features.placeholder import placeholder
from app.registry import Feature, Job


async def run(s, force: bool = False) -> None:
    """Defaults from settings.placeholder, overridden by profiles.<id>.placeholder (label, amount)."""
    today = s.today()
    for pid in s.access.recipients("placeholder"):
        c = {**s.cfg.settings["placeholder"], **(s.cfg.profiles[pid].get("placeholder") or {})}
        try:
            png = await asyncio.to_thread(placeholder.render, today.replace(day=1), c["label"], c["amount"])
            await s.n.send_document([pid], png, f"parking-placeholder-{today:%Y-%m}.png",
                                    f"{c['label']} — {today:%B %Y} placeholder (not a receipt). "
                                    "Remember to download the official receipt.")
        except Exception as e:  # noqa: BLE001
            await s.report_error(f"placeholder for {pid}", e)


FEATURE = Feature(
    name="placeholder",
    description="Monthly parking placeholder image (admin only)",
    admin_only=True,
    jobs=(Job("placeholder", run,
              lambda st: st["placeholder"]["schedule"] if st.get("placeholder", {}).get("enabled") else None),),
)
