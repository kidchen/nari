"""Credit card statement PDFs -> transactions, checked against the statement's own balances.

Works on positioned words (text, x0, top, page), so tests can feed synthetic layouts and only
`extract_words()` touches pdfplumber. Every bank prints previous and new balance; an import is
accepted only if previous + all parsed activity == new balance, to the cent, so a parsing mistake
can't silently skew reports.

Layouts (from real statements, 2026):
  boa       MM/DD MM/DD <description> <ref 4> <account 4> <amount>   (transaction + posting date)
  chase     MM/DD <description> <amount>
  discover  MM/DD <description> <Discover category> <amount>, plus a rewards box on the right (cropped)
  amex      MM/DD/YY[*] <description> <amount>; purchases under "New Charges"; period = closing date − billing days
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

Word = dict[str, Any]           # {"text", "x0", "top", "page"}

BANKS: dict[str, dict[str, Any]] = {
    # order matters: detection uses the first marker found on page 1
    "boa": {"name": "Bank of America", "marker": r"bank of america", "two_dates": True},
    "amex": {"name": "American Express", "marker": r"american express|americanexpress"},
    "discover": {"name": "Discover", "marker": r"discover", "x_max": 405, "category_x": 250},
    "chase": {"name": "Chase", "marker": r"chase\.com|\bchase\b"},       # \b: not "purchase"
}
DATE_RE = re.compile(r"^\d{2}/\d{2}(/\d{2})?\*?$")         # Amex adds the year; '*' = posted later
AMOUNT_RE = re.compile(r"^[-+]?\$?(\d[\d,]*)?\.\d{2}$")           # Chase prints 50 cents as ".50"
# Bill payments (not refunds) in the payments/credits section. BoA's online payment says just "ONLINE/MOBILE …".
PAYMENT_RE = re.compile(r"PAYMENT|DIRECTPAY|AUTOPAY|AUTOMATIC PAY|THANK YOU|ONLINE/MOBILE|ELECTRONIC", re.I)
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}


@dataclass
class Txn:
    date: date
    amount_cents: int            # as printed: charges positive, payments/credits negative
    description: str
    kind: str                    # purchase | credit | payment | fee | interest | cash_advance | balance_transfer
    bank_category: str = ""


@dataclass
class Statement:
    bank: str
    last4: str = ""
    start: date | None = None
    end: date | None = None
    previous_cents: int | None = None
    new_cents: int | None = None
    txns: list[Txn] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def activity_cents(self) -> int:
        return sum(t.amount_cents for t in self.txns)

    @property
    def balanced(self) -> bool:
        return (self.previous_cents is not None and self.new_cents is not None
                and self.previous_cents + self.activity_cents == self.new_cents)

    @property
    def ok(self) -> bool:
        return self.balanced and not self.problems and self.end is not None and bool(self.last4)

    def total(self, *kinds: str) -> int:
        return sum(t.amount_cents for t in self.txns if t.kind in kinds)


# ---- PDF -> words (the only pdfplumber use) ------------------------------------------------

def extract_words(path: str) -> tuple[list[Word], str]:
    """Positioned words of every page + page-1 text. Blocking."""
    import pdfplumber
    words: list[Word] = []
    with pdfplumber.open(path) as pdf:
        first = (pdf.pages[0].extract_text(x_tolerance=1.5) or "") if pdf.pages else ""
        for i, page in enumerate(pdf.pages):
            for w in page.extract_words(x_tolerance=1.5):
                words.append({"text": w["text"], "x0": float(w["x0"]), "top": float(w["top"]), "page": i})
    return words, first


# ---- parsing ---------------------------------------------------------------------------------

def cents(text: str) -> int:
    neg = text.strip().startswith("-")
    digits = re.sub(r"[^\d.]", "", text)
    value = int(round(float(digits) * 100))
    return -value if neg else value


def detect_bank(first_page: str) -> str | None:
    low = first_page.lower()
    return next((b for b, spec in BANKS.items() if re.search(spec["marker"], low)), None)


def lines(words: list[Word], x_max: float | None = None, tol: float = 2.5) -> list[list[Word]]:
    """Words grouped into visual lines (same page, |top| within tol), left to right."""
    out: list[list[Word]] = []
    for w in sorted((w for w in words if x_max is None or w["x0"] < x_max), key=lambda w: (w["page"], w["top"], w["x0"])):
        if out and out[-1][0]["page"] == w["page"] and abs(out[-1][0]["top"] - w["top"]) <= tol:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(line, key=lambda w: w["x0"]) for line in out]


def _parse_date(s: str) -> date | None:
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def period(text: str) -> tuple[date | None, date | None]:
    """'09/10/26 - 10/09/26', 'DATE:09/12/2026 -10/11/2026' or 'September 9 - October 9, 2026'."""
    m = re.search(r"(\d{2}/\d{2}/\d{2,4})\s*-\s*(\d{2}/\d{2}/\d{2,4})", text)
    if m:
        return _parse_date(m.group(1)), _parse_date(m.group(2))
    m = re.search(r"([A-Z][a-z]+) (\d{1,2})(?:, (\d{4}))? - ([A-Z][a-z]+) (\d{1,2}), (\d{4})", text)
    if m and m.group(1).lower() in MONTHS and m.group(4).lower() in MONTHS:
        end = date(int(m.group(6)), MONTHS[m.group(4).lower()], int(m.group(5)))
        start_year = int(m.group(3)) if m.group(3) else end.year - (1 if MONTHS[m.group(1).lower()] > end.month else 0)
        return date(start_year, MONTHS[m.group(1).lower()], int(m.group(2))), end
    return None, None


def last4(text: str) -> str:
    # Amex prints the last 5 digits ("Account Ending 1-11007"); the others print 4.
    for pat in (r"Account Ending\s*\d-(\d{5})", r"ending in\s*(\d{4})", r"Account Number:?\s*(?:[X*•]{4}\s*){3}(\d{4})",
                r"Account\s*(?:Number|#)\s*:?\s*\d{4}\s\d{4}\s\d{4}\s(\d{4})"):
        if m := re.search(pat, text, re.I):
            return m.group(1)
    return ""


def balance(text: str, label: str) -> int | None:
    m = re.search(label + r"[ \t:]+(-?\$?-?[\d,]+\.\d{2})", text)
    return cents(m.group(1)) if m else None


def _section(text: str) -> str | None:
    t = text.upper()
    if t.startswith("TOTAL") or len(t.split()) > 8:
        return None                                     # totals rows and legal prose aren't headings
    if "PAYMENT" in t and "CREDIT" in t:
        return "credits"
    if "CASH ADVANCE" in t:
        return "cash_advance"
    if "BALANCE TRANSFER" in t:
        return "balance_transfer"
    if "FEE" in t and "INTEREST" not in t:
        return "fee"
    if "INTEREST CHARGED" in t or t == "INTEREST":
        return "interest"
    if "PURCHASE" in t or "NEW CHARGES" in t:
        return "purchase"
    if t in ("CREDITS", "OTHER CREDITS"):
        return "credits"
    return None


def _txn_date(mmdd: str, end: date) -> date:
    if (full := _parse_date(mmdd.rstrip("*"))) and mmdd.count("/") == 2:
        return full
    m, d = (int(x) for x in mmdd.rstrip("*").split("/")[:2])
    candidate = date(end.year, m, d)
    return date(end.year - 1, m, d) if candidate > end + timedelta(days=7) else candidate   # Dec items on a Jan statement


def parse(words: list[Word], first_page: str) -> Statement:
    bank = detect_bank(first_page)
    if not bank:
        return Statement("?", problems=["Unknown bank: only Chase, Discover, Bank of America and American Express "
                                        "statements are supported."])
    spec = BANKS[bank]
    full = "\n".join(" ".join(w["text"] for w in ln) for ln in lines(words))
    st = Statement(bank, last4=last4(first_page + "\n" + full))
    start, end = period(first_page)
    st.start, st.end = (start, end) if end else period(full)
    both = first_page + "\n" + full
    if st.end is None and (m := re.search(r"Closing Date\s+(\d{2}/\d{2}/\d{2,4})", both)):   # Amex
        st.end = _parse_date(m.group(1))
        if st.end and (days := re.search(r"Days in Billing Period:?\s*(\d+)", both)):
            st.start = st.end - timedelta(days=int(days.group(1)) - 1)
    st.previous_cents = balance(first_page, r"Previous Balance")
    st.new_cents = balance(first_page, r"New Balance(?: Total)?")
    if st.end is None:
        st.problems.append("Couldn't find the statement period.")
        return st
    section = None
    for ln in lines(words, spec.get("x_max")):
        texts = [w["text"] for w in ln]
        is_txn = len(ln) >= 3 and DATE_RE.match(texts[0]) and AMOUNT_RE.match(texts[-1])
        if not is_txn:
            section = _section(" ".join(texts)) or section
            continue
        body = ln[1:-1]
        if spec.get("two_dates") and body and DATE_RE.match(body[0]["text"]):
            body = body[1:]                              # posting date
        if spec.get("two_dates") and len(body) >= 3 and all(re.fullmatch(r"\d{4}", w["text"]) for w in body[-2:]):
            body = body[:-2]                             # reference + account number columns
        category = ""
        if cx := spec.get("category_x"):
            category = " ".join(w["text"] for w in body if w["x0"] >= cx)
            body = [w for w in body if w["x0"] < cx]
        desc = " ".join(w["text"] for w in body).strip()
        amount = cents(texts[-1])
        kind = section or "purchase"
        if kind == "credits":
            kind = "payment" if PAYMENT_RE.search(desc) else "credit"
        elif kind == "purchase" and amount < 0:
            kind = "credit"                              # returns listed among purchases
        st.txns.append(Txn(_txn_date(texts[0], st.end), amount, desc, kind, category))
    if not st.last4:
        st.problems.append("Couldn't find the card's last 4 digits.")
    if not st.txns:
        st.problems.append("No transactions found.")
    if not st.balanced:
        st.problems.append("Parsed activity doesn't add up to the statement's new balance.")
    return st


def parse_pdf(path: str) -> Statement:
    """Blocking."""
    words, first = extract_words(path)
    return parse(words, first)
