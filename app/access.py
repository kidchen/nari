"""Who gets which feature.

settings.yaml `profiles.<id>.features` = what a person is ALLOWED ("all" or a list; default all).
Within that, each person can pause/resume features from the bot (stored in the DB), which stops
scheduled messages for that feature without changing config.
"""
from __future__ import annotations

import json
from typing import Any

from app.db import DB


def catalog() -> dict[str, tuple[str, bool]]:
    """{feature name: (description, admin_only)} from the feature registry, in menu order."""
    from app.features import ALL  # lazy: features import services, which imports this module
    return {f.name: (f.description, f.admin_only) for f in ALL}


class Access:
    def __init__(self, profiles: dict[str, dict[str, Any]], db: DB):
        self.profiles, self.db = profiles, db
        self.admin = next((pid for pid, p in profiles.items() if p.get("primary")), next(iter(profiles), None))

    def allowed(self, pid: str) -> list[str]:
        """Admin-only features exist only for the primary profile, whatever other profiles' `features` say."""
        cat = catalog()
        spec = (self.profiles.get(pid) or {}).get("features", "all")
        names = list(cat) if spec in (None, "all") else [f for f in cat if f in spec]
        return [f for f in names if not cat[f][1] or pid == self.admin]

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
        for f, (label, _admin) in catalog().items():
            if f not in allowed:
                continue
            state = "⏸️ paused" if f in paused else "✅ on"
            lines.append(f"{state} — <b>{f}</b>: {label}")
        return "\n".join(lines) or "No features enabled for you."
