"""Who gets which feature.

settings.yaml `profiles.<id>.features` = what a person is ALLOWED ("all" or a list; default all).
Within that, each person can pause/resume features from the bot (stored in the DB), which stops
scheduled messages for that feature without changing config.
"""
from __future__ import annotations

import json
from typing import Any

from app.db import DB

FEATURES: dict[str, str] = {
    "market": "Morning market brief (/brief, /portfolio, /watch)",
    "realestate": "Real estate listings + mortgage rates",
    "cards": "Card plans, rotating categories, offer reminders",
    "placeholder": "Monthly parking placeholder image",
}


class Access:
    def __init__(self, profiles: dict[str, dict[str, Any]], db: DB):
        self.profiles, self.db = profiles, db

    def allowed(self, pid: str) -> list[str]:
        spec = (self.profiles.get(pid) or {}).get("features", "all")
        if spec in (None, "all"):
            return list(FEATURES)
        return [f for f in FEATURES if f in spec]

    def paused(self, pid: str) -> list[str]:
        raw = self.db.get_kv(f"paused:{pid}")
        return json.loads(raw) if raw else []

    def receives(self, pid: str, feature: str) -> bool:
        """Scheduled messages for `feature` go to `pid`."""
        return feature in self.allowed(pid) and feature not in self.paused(pid)

    def recipients(self, feature: str) -> list[str]:
        return [pid for pid in self.profiles if self.receives(pid, feature)]

    def set_paused(self, pid: str, feature: str, paused: bool) -> None:
        cur = set(self.paused(pid))
        cur = cur | {feature} if paused else cur - {feature}
        self.db.set_kv(f"paused:{pid}", json.dumps(sorted(cur)))

    def describe(self, pid: str) -> str:
        allowed, paused = self.allowed(pid), self.paused(pid)
        lines = []
        for f, label in FEATURES.items():
            if f not in allowed:
                continue
            state = "⏸️ paused" if f in paused else "✅ on"
            lines.append(f"{state} — <b>{f}</b>: {label}")
        return "\n".join(lines) or "No features enabled for you."
