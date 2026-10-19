"""Subscriptions: the same merchant charging a similar amount on a steady monthly or yearly rhythm.

Groceries, dining and gas are excluded (frequent, irregular amounts). Detection needs 3 monthly charges
or 2 yearly ones, so it gets better as more statements are imported.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

SKIP_CATEGORIES = {"Groceries", "Dining", "Gas", "Transport", "Fees & interest"}
CADENCES = {"monthly": (24, 38, 3, 30.4), "yearly": (340, 390, 2, 365.0)}   # min gap, max gap, min charges, days


@dataclass
class Subscription:
    merchant: str                  # merchant key
    cadence: str                   # monthly | yearly
    amount_cents: int              # latest charge
    previous_cents: int            # the one before
    first: date
    last: date

    @property
    def monthly_cents(self) -> int:
        return self.amount_cents if self.cadence == "monthly" else round(self.amount_cents / 12)

    @property
    def yearly_cents(self) -> int:
        return self.amount_cents * 12 if self.cadence == "monthly" else self.amount_cents

    @property
    def increased(self) -> bool:
        return self.amount_cents - self.previous_cents > max(50, self.previous_cents * 0.02)

    def next_due(self) -> date:
        return self.last + timedelta(days=round(CADENCES[self.cadence][3]))


def _similar(amounts: list[int]) -> bool:
    """Steady price, allowing one change (a price increase shouldn't hide the subscription)."""
    m = median(amounts)
    off = [a for a in amounts if abs(a - m) > max(100, 0.25 * m)]
    return len(off) <= 1


def detect(rows: list, today: date) -> list[Subscription]:
    """rows: purchases with merchant, category, date (ISO), amount (cents). Only still-active ones are kept."""
    by_merchant: dict[str, list] = {}
    for r in rows:
        if r["amount"] > 0 and r["category"] not in SKIP_CATEGORIES:
            by_merchant.setdefault(r["merchant"], []).append(r)
    out = []
    for key, rs in by_merchant.items():
        charges = sorted((date.fromisoformat(r["date"]), r["amount"]) for r in rs)
        for cadence, (lo, hi, need, days) in CADENCES.items():
            if len(charges) < need:
                continue
            gaps = [(b[0] - a[0]).days for a, b in zip(charges, charges[1:], strict=False)]
            steady = [g for g in gaps if lo <= g <= hi]
            if len(steady) >= need - 1 and len(steady) >= 0.7 * len(gaps) and _similar([a for _, a in charges]):
                if (today - charges[-1][0]).days <= days * 1.5:           # still active
                    out.append(Subscription(key, cadence, charges[-1][1], charges[-2][1], charges[0][0], charges[-1][0]))
                break
    return sorted(out, key=lambda s: -s.monthly_cents)
