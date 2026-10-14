"""Module D: per-user cards, rotating categories, targeted offers."""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date
from typing import Any


def quarter_key(d: date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def cards_for(cards: dict[str, Any], profile: str) -> dict[str, Any]:
    return {cid: c for cid, c in cards.items() if c.get("owner") == profile}


def override_key(card_id: str, qkey: str) -> str:
    return f"rotating:{card_id}:{qkey}"


def load_overrides(db: Any, today: date) -> dict[str, list[str]]:
    """Per-card categories set via /setcats for the current quarter, keyed by card id."""
    import json

    qkey, out = quarter_key(today), {}
    for k, v in db.kv_like(f"rotating:%:{qkey}"):
        out[k.split(":")[1]] = json.loads(v)
    return out


def rotating_categories(card: dict[str, Any], qkey: str, override: list[str] | None = None) -> list[str] | None:
    """Categories for the quarter, [] if card rotates but quarter not filled in, None if non-rotating.

    A /setcats override takes precedence over cards.yaml.
    """
    rot = card.get("rotating")
    if not rot:
        return None
    if override:
        return list(override)
    return list((rot.get("quarters") or {}).get(qkey) or [])


def quarterly_message(cards: dict[str, Any], profile: str, profile_name: str, today: date,
                      overrides: dict[str, list[str]] | None = None) -> str | None:
    overrides = overrides or {}
    mine = cards_for(cards, profile)
    if not mine:
        return None
    qkey = quarter_key(today)
    lines = [f"💳 <b>{qkey} card plan for {html.escape(profile_name)}</b>"]
    for cid, c in mine.items():
        unit = c.get("unit", "%")
        lines.append("")
        lines.append(f"<b>{html.escape(c['name'])}</b> <code>{cid}</code>")
        cats = rotating_categories(c, qkey, overrides.get(cid))
        if cats is not None:
            rot = c["rotating"]
            cap = f" (up to ${rot['cap_per_quarter']:,} spend)" if rot.get("cap_per_quarter") else ""
            if cats:
                lines.append(f"• {rot['rate']}{unit}{cap}: {html.escape(', '.join(cats))}")
            else:
                url = rot.get("calendar_url", "")
                lines.append(f"• ⚠️ {qkey} rotating categories not set — check {html.escape(url)}, "
                             f"then send /setcats {cid} Category A, Category B")
            if rot.get("requires_activation"):
                lines.append("• 👉 <b>Activate this quarter's bonus</b> in the issuer app")
        for cat, rate in (c.get("rates") or {}).items():
            label = "everything else" if cat == "other" else cat
            lines.append(f"• {rate}{unit} {html.escape(label)}")
    return "\n".join(lines)


def best_card(cards: dict[str, Any], profile: str, category: str, today: date,
              overrides: dict[str, list[str]] | None = None) -> list[tuple[float, str, str]]:
    """Rank the profile's cards for a category: list of (rate, card name, reason)."""
    qkey = quarter_key(today)
    cat = category.strip().lower()
    overrides = overrides or {}
    ranked: list[tuple[float, str, str]] = []
    for cid, c in cards_for(cards, profile).items():
        rates = {k.lower(): float(v) for k, v in (c.get("rates") or {}).items()}
        rate, reason = rates.get(cat, rates.get("other", 0.0)), "base" if cat in rates else "everything else"
        cats = [x.lower() for x in (rotating_categories(c, qkey, overrides.get(cid)) or [])]
        if any(cat in x or x in cat for x in cats) and float(c["rotating"]["rate"]) > rate:
            rate, reason = float(c["rotating"]["rate"]), f"{qkey} rotating"
        ranked.append((rate, c["name"], reason))
    return sorted(ranked, reverse=True)


# ---- offers -------------------------------------------------------------------

@dataclass(frozen=True)
class OfferInput:
    card_id: str
    merchant: str
    description: str
    expires: date | None


class OfferParseError(ValueError):
    pass


OFFER_USAGE = ("Usage: /offer add &lt;card_id&gt; | &lt;merchant&gt; | &lt;description&gt; [| YYYY-MM-DD]\n"
               "Example: /offer add discover_it | Dell | 10% back up to $50 | 2026-11-30")


def parse_offer_args(text: str, allowed_cards: dict[str, Any]) -> OfferInput:
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 3 or not all(parts[:3]):
        raise OfferParseError(OFFER_USAGE)
    card_id, merchant, desc = parts[:3]
    if card_id not in allowed_cards:
        raise OfferParseError(f"Unknown card '{html.escape(card_id)}'. Your cards: "
                              + (", ".join(allowed_cards) or "none"))
    expires = None
    if len(parts) > 3 and parts[3]:
        try:
            expires = date.fromisoformat(parts[3])
        except ValueError as e:
            raise OfferParseError("Expiry must be YYYY-MM-DD") from e
    return OfferInput(card_id, merchant, desc, expires)


def format_offers(rows: list[Any], cards: dict[str, Any]) -> str:
    if not rows:
        return "No active offers. Add one with /offer add."
    lines = ["🎯 <b>Active offers</b>"]
    for r in rows:
        card = cards.get(r["card_id"], {}).get("name", r["card_id"])
        exp = f" · exp {r['expires']}" if r["expires"] else ""
        lines.append(f"#{r['id']} <b>{html.escape(r['merchant'])}</b>: {html.escape(r['description'])} "
                     f"<i>({html.escape(card)}{exp})</i>")
    lines.append("Remove with /offer done &lt;id&gt;")
    return "\n".join(lines)


def expiring_offers(rows: list[Any], today: date, days: list[int]) -> list[tuple[Any, int]]:
    out = []
    for r in rows:
        if not r["expires"]:
            continue
        left = (date.fromisoformat(r["expires"]) - today).days
        if left in days:
            out.append((r, left))
    return out
