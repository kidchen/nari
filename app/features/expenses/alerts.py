"""Bank transaction alert emails -> charges, between statements.

Each bank's alert has its own layout; add a parser to PARSERS when a new one shows up. Alerts arrive
directly (bank -> bot's Gmail) or forwarded from someone's mailbox (subject "Fw: ...", the original in
the body), so parsers work on the body text, not headers. Statements stay authoritative: on import
they replace the alerts for their card and period (store.import_statement).

BoA ("Credit card transaction exceeds alert limit you set", 2026):
    <card name> ending in 1234 / Amount: $10.89 / Date: October 19, 2026 / Where: <merchant>
Chase ("You made a $28.37 transaction with <merchant>", 2026): labels on their own line, no colon:
    Account / Chase Freedom Visa (...1234) / Date / Oct 24, 2026 at 2:54 PM ET / Merchant / <name> / Amount / $28.37
"""
from __future__ import annotations

import html as htmlmod
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class Alert:
    bank: str
    last4: str
    card_name: str
    date: date
    amount_cents: int
    merchant: str
    kind: str = "purchase"


def text_lines(html_or_text: str) -> list[str]:
    t = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", html_or_text)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|td|th|li|h\d|table)>", "\n", t)
    t = htmlmod.unescape(re.sub(r"<[^>]+>", " ", t))
    return [ln for ln in (re.sub(r"[\s\xa0‌͏­‍]+", " ", x).strip() for x in t.splitlines()) if ln]


def _field(lines: list[str], label: str) -> str | None:
    """'Amount:' followed by its value on the same line or the next one."""
    for i, ln in enumerate(lines):
        if m := re.match(rf"{label}\s*:\s*(.*)$", ln, re.I):
            return m.group(1).strip() or (lines[i + 1].strip() if i + 1 < len(lines) else None)
    return None


def _after(lines: list[str], label: str) -> str | None:
    """The line after a line that is exactly `label` (Chase puts each label on its own line)."""
    for i, ln in enumerate(lines[:-1]):
        if ln.strip().lower() == label.lower():
            return lines[i + 1].strip()
    return None


def _cents(s: str) -> int | None:
    m = re.search(r"-?\$\s*([\d,]+\.\d{2})", s or "")
    return int(round(float(m.group(1).replace(",", "")) * 100)) if m else None


def _date(s: str) -> date | None:
    s = re.sub(r"\s+at\s+.*$", "", (s or "").strip())          # "Oct 24, 2026 at 2:54 PM ET"
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_boa(lines: list[str]) -> Alert | None:
    text = "\n".join(lines)
    if not re.search(r"credit card transaction\s+exceeds alert limit", text, re.I):
        return None                                      # other BoA alerts (payments, balances): not charges
    card = re.search(r"^(.*?)\s*ending in\s*(\d{4})\b", text, re.I | re.M)
    amount, when, where = _cents(_field(lines, "Amount") or ""), _date(_field(lines, "Date") or ""), _field(lines, "Where")
    if not (card and amount and when and where):
        return None
    return Alert("boa", card.group(2), card.group(1).strip(), when, amount, where)


def parse_chase(lines: list[str]) -> Alert | None:
    text = "\n".join(lines)
    if not re.search(r"^Transaction alert$", text, re.I | re.M) or not re.search(r"You made an? \$", text):
        return None                                      # other Chase alerts (payments, balances): not charges
    account = _after(lines, "Account") or ""
    card = re.search(r"^(.*?)\s*\(\.{2,3}(\d{4})\)", account)
    amount, when, where = _cents(_after(lines, "Amount") or ""), _date(_after(lines, "Date") or ""), _after(lines, "Merchant")
    if not (card and amount and when and where):
        return None
    return Alert("chase", card.group(2), card.group(1).strip(), when, amount, where)


PARSERS: dict[str, tuple[str, Callable[[list[str]], Alert | None]]] = {
    # bank: (marker in the body text, parser)
    "boa": (r"bank of america", parse_boa),
    "chase": (r"\bchase\b|jpmorgan", parse_chase),       # \b: not the "chase" in "purchase"
}
# IMAP subject searches (forwards keep the subject after "Fw:"), one per supported alert type.
SUBJECTS = ["transaction exceeds alert limit",           # BoA
            "transaction with"]                          # Chase: "You made a $28.37 transaction with …"


def parse(html_or_text: str) -> Alert | None:
    lines = text_lines(html_or_text)
    low = "\n".join(lines).lower()
    for marker, parser in PARSERS.values():
        if re.search(marker, low) and (alert := parser(lines)):
            return alert
    return None


def imap_criteria(subjects: list[str]) -> list[str]:
    """OR of SUBJECT searches (IMAP OR takes two operands, so nest them)."""
    terms = [["SUBJECT", f'"{s}"'] for s in subjects]
    out = terms[-1]
    for t in reversed(terms[:-1]):
        out = ["OR", *t, *out]
    return out
