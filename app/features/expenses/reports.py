"""Spending reports (per person): weekly, monthly, quarterly and year-end.

Spending = purchases + refunds + fees + interest (payments to the card are not spending). Each report
says how complete its data is: a card's statement may not cover the whole period yet.
Gemini sees only aggregated category totals for the commentary, never transactions or merchants.
"""
from __future__ import annotations

import html
import io
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.db import DB
from app.features.expenses import categories as C
from app.features.expenses import recurring, store

log = logging.getLogger(__name__)
KINDS = ("week", "month", "quarter", "year")


def _esc(s: str) -> str:
    return html.escape(s, quote=False)


def money(c: int, cents: bool = True) -> str:
    v = abs(c) / 100
    return f"{'-' if c < 0 else ''}${v:,.2f}" if cents else f"{'-' if c < 0 else ''}${v:,.0f}"


def change(cur: int, prev: int | None, label: str) -> str:
    if not prev:
        return ""
    pct = (cur - prev) / abs(prev) * 100
    return f"{'▲' if pct > 0 else '▼'} {abs(pct):.0f}% vs {label}" if abs(pct) >= 1 else f"≈ {label}"


# ---- periods ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Period:
    kind: str
    start: date
    end: date

    @property
    def label(self) -> str:
        if self.kind == "week":
            return f"Week of {self.start:%b %d}–{self.end:%b %d}" if self.start.month == self.end.month \
                else f"Week of {self.start:%b %d} – {self.end:%b %d}"
        if self.kind == "month":
            return f"{self.start:%B %Y}"
        if self.kind == "quarter":
            return f"Q{(self.start.month - 1) // 3 + 1} {self.start.year}"
        return f"{self.start.year}"

    def previous(self) -> Period:
        return period(self.kind, self.start)        # the complete period before this one (week: the 7 days before)


def _month_start(d: date, back: int = 0) -> date:
    m = d.month - 1 - back
    return date(d.year + m // 12, m % 12 + 1, 1)


def period(kind: str, today: date, current: bool = False) -> Period:
    """The last complete period before `today` (or the one containing it, to date, if `current`)."""
    if kind == "week":                     # the 7 days ending yesterday (today, if `current`)
        end = today if current else today - timedelta(days=1)
        return Period(kind, end - timedelta(days=6), end)
    if kind == "month":
        start = _month_start(today, 0 if current else 1)
        end = _month_start(start, -1) - timedelta(days=1)
    elif kind == "quarter":
        q0 = date(today.year, (today.month - 1) // 3 * 3 + 1, 1)
        start = q0 if current else _month_start(q0, 3)
        end = _month_start(start, -3) - timedelta(days=1)
    else:
        start = date(today.year if current else today.year - 1, 1, 1)
        end = date(start.year, 12, 31)
    return Period(kind, start, min(end, today) if current else end)


# ---- data -------------------------------------------------------------------------------------

@dataclass
class Totals:
    total: int = 0
    count: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    by_merchant: dict[str, list[int]] = field(default_factory=dict)      # key -> [cents, count]
    rows: list = field(default_factory=list)


def totals(db: DB, owner: str, p: Period) -> Totals:
    t = Totals(rows=store.spending(db, owner, p.start, p.end))
    cat: dict[str, int] = defaultdict(int)
    for r in t.rows:
        t.total += r["amount"]
        t.count += 1
        cat[r["category"] or "Uncategorized"] += r["amount"]
        if r["kind"] == "purchase":
            m = t.by_merchant.setdefault(r["merchant"] or C.merchant_key(r["description"]), [0, 0])
            m[0] += r["amount"]
            m[1] += 1
    t.by_category = dict(sorted(cat.items(), key=lambda kv: -kv[1]))
    return t


BANK_LABEL = {"boa": "BoA", "chase": "Chase", "discover": "Discover", "amex": "Amex"}


ALERT_SILENT_DAYS = 30        # a card that usually sends alerts and then goes quiet this long: check its setting
ALERT_HABIT = 3               # ... "usually" = at least this many alerts in the 60 days before the silence


def card_data(db: DB, owner: str) -> dict[tuple[str, str], dict]:
    """Per card: latest statement end + its closing balance, alert dates."""
    cards: dict[tuple[str, str], dict] = {}
    for r in db.rows("SELECT bank, last4, end, new FROM expense_statements s WHERE owner=? AND end = "
                     "(SELECT max(end) FROM expense_statements t WHERE t.owner=s.owner AND t.bank=s.bank AND t.last4=s.last4)",
                     (owner,)):
        cards[(r["bank"], r["last4"])] = {"stmt": r["end"], "balance": r["new"], "alerts": []}
    for r in db.rows("SELECT bank, last4, date FROM expense_txns WHERE owner=? AND source='alert' ORDER BY date", (owner,)):
        cards.setdefault((r["bank"], r["last4"]), {"alerts": []})["alerts"].append(date.fromisoformat(r["date"]))
    return cards


def label(bank: str, last4: str) -> str:
    return f"{BANK_LABEL.get(bank, bank)} ··{last4}"


def notes(db: DB, owner: str, p: Period, today: date) -> list[str]:
    """Statements are optional: alerts are the main source. Flags only what needs attention:
    alerts that stopped on a card that usually sends them, and (monthly and longer) cards with no alerts,
    whose spending after their last statement isn't counted. Idle cards ($0 last statement) are skipped."""
    silent, uncounted = [], []
    for (bank, last4), c in sorted(card_data(db, owner).items()):
        alerts = c["alerts"]
        if alerts:
            last = alerts[-1]
            habit = [d for d in alerts if last - timedelta(days=60) <= d <= last]
            if (today - last).days >= ALERT_SILENT_DAYS and len(habit) >= ALERT_HABIT:
                silent.append(f"{label(bank, last4)} since {last:%b %d}")
        elif c.get("stmt") and c["stmt"] < p.end.isoformat() and c.get("balance") != 0:
            uncounted.append(label(bank, last4))
    out = []
    if silent:
        out.append(f"⚠️ No alerts from {_esc(', '.join(silent))}. Check that card's alert setting.")
    if uncounted and p.kind != "week":
        out.append(f"<i>Not counted after their last statement (no alerts): {_esc(', '.join(uncounted))}.</i>")
    if any(r["source"] == "alert" for r in store.spending(db, owner, p.start, p.end)):
        out.append("<i>From card alerts: tips and refunds may be missing until a statement is imported.</i>")
    return out


def stale_statements(db: DB, owner: str, today: date, months: int = 3) -> list[str]:
    """Cards still in use (alerts in the last 90 days, or a statement with a balance) whose newest statement
    is older than `months` (or that never had one). For the statement reminder."""
    cutoff = today - timedelta(days=30 * months)
    out = []
    for (bank, last4), c in sorted(card_data(db, owner).items()):
        in_use = any(d >= cutoff for d in c["alerts"]) or (c.get("stmt") and c.get("balance") != 0)
        if in_use and (c.get("stmt") or "") < cutoff.isoformat():
            out.append(label(bank, last4))
    return out


def subscriptions(db: DB, owner: str, today: date) -> list[recurring.Subscription]:
    rows = store.spending(db, owner, today - timedelta(days=400), today)
    return recurring.detect([r for r in rows if r["kind"] == "purchase"], today)


# ---- 5% check (quarterly): purchases that a different card would have earned more on --------------

CARD_TERMS = {"Dining": ["dining", "restaurants"], "Groceries": ["grocery", "groceries", "supermarkets", "wholesale clubs"],
              "Gas": ["gas", "gas stations", "ev charging"], "Travel": ["travel", "hotels", "airlines"],
              "Home": ["home improvement"], "Health": ["drugstores", "pharmacies"], "Subscriptions": ["streaming"],
              "Transport": ["transit"], "Entertainment": ["entertainment", "movie theaters"], "Shopping": ["department stores"]}
ONLINE = ["online shopping"]            # e.g. BoA Customized Cash's 3% choice
MERCHANT_TERMS = {"AMAZON": ["amazon", *ONLINE], "TARGET": ["target"], "WALMART": ["walmart"],
                  "COSTCO": ["wholesale clubs", "costco"], "PAYPAL": ["paypal"], "APPLE STORE": ["apple"], "BEST BUY": ["best buy"],
                  "EBAY": ONLINE, "ETSY": ONLINE, "TEMU": ONLINE, "SHEIN": ONLINE, "COSTCO.COM": ONLINE, "WAYFAIR": ONLINE}


def card_check(db: DB, cards_cfg: dict, owner: str, p: Period) -> tuple[list[str], int]:
    """(lines, missed cents): estimate, ignoring caps. Needs `last4` on the person's cards in the cards config."""
    from app.features.cards import cards as K
    mine = K.cards_for(cards_cfg, owner)
    by_last4 = any(c.get("last4") for c in mine.values())
    if len(mine) < 2:
        return [], 0
    if not by_last4:
        return ["<i>Add <code>last4</code> to your cards in the cards config to see which card earned the most.</i>"], 0
    missed: dict[tuple[str, str], int] = defaultdict(int)
    ranking_cache: dict[tuple[str, date], list] = {}
    for r in store.spending(db, owner, p.start, p.end):
        used = (store.find_card(mine, r["bank"], r["last4"]) or {}).get("name")
        if r["kind"] != "purchase" or not used:
            continue
        terms = MERCHANT_TERMS.get(r["merchant"], []) + CARD_TERMS.get(r["category"], []) or ["other"]
        day = date.fromisoformat(r["date"])
        best, used_rate = (0.0, "", ""), 0.0
        for term in terms:
            key = (term, date(day.year, (day.month - 1) // 3 * 3 + 1, 1))
            if key not in ranking_cache:
                ranking_cache[key] = K.best_card(cards_cfg, owner, term, day, db)
            ranked = ranking_cache[key]
            if ranked and ranked[0][0] > best[0]:
                best = ranked[0]
            used_rate = max(used_rate, next((rate for rate, name, _ in ranked if name == used), 0.0))
        if best[1] and best[1] != used and best[0] > used_rate:
            missed[(best[1], best[2])] += round(r["amount"] * (best[0] - used_rate) / 100)
    total = sum(missed.values())
    if not total:
        return ["✅ Every purchase went on the best card."], 0
    lines = [f"💡 About {money(total)} more rewards possible (estimate, caps ignored):"]
    lines += [f"• {_esc(name)} ({_esc(reason)}): {money(v)}" for (name, reason), v in sorted(missed.items(), key=lambda kv: -kv[1])[:5]]
    return lines, total


# ---- chart (Pillow; fonts as in the placeholder feature) ------------------------------------------

PALETTE = ["#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F", "#EDC948", "#B07AA1", "#FF9DA7", "#9C755F",
           "#BAB0AC", "#86BCB6", "#D37295", "#A0CBE8", "#FFBE7D", "#8CD17D", "#B6992D", "#79706E"]


def chart(title: str, slices: dict[str, int], total: int) -> bytes:
    """Donut chart of category shares with a legend. Refunds-only (negative) categories are left out."""
    from PIL import Image, ImageDraw

    from app.features.placeholder.placeholder import _font
    positive = sorted(((k, v) for k, v in slices.items() if v > 0), key=lambda kv: -kv[1])
    items = positive[:8]
    if rest := sum(v for _, v in positive[8:]):
        items.append(("Everything else", rest))
    w, h = 1000, max(620, 140 + 52 * len(items))
    img = Image.new("RGB", (w, h), "#FFFFFF")
    d = ImageDraw.Draw(img)
    d.text((40, 28), title, fill="#222222", font=_font(34, bold=True))
    top = (h - 480) // 2 + 30
    box = (40, top, 520, top + 480)
    pos = sum(v for _, v in items) or 1
    angle = -90.0
    for i, (_, v) in enumerate(items):
        sweep = 360 * v / pos
        d.pieslice(box, angle, angle + sweep, fill=PALETTE[i % len(PALETTE)])
        angle += sweep
    d.ellipse((160, top + 120, 400, top + 360), fill="#FFFFFF")         # donut hole
    t = money(total, cents=False)
    tw = d.textlength(t, font=_font(36, bold=True))
    d.text((280 - tw / 2, top + 218), t, fill="#222222", font=_font(36, bold=True))
    y = top + 240 - 26 * len(items)
    for i, (k, v) in enumerate(items):
        d.rectangle((570, y + 6, 592, y + 28), fill=PALETTE[i % len(PALETTE)])
        d.text((606, y + 2), k, fill="#222222", font=_font(24))
        amount = f"{money(v, cents=False)} · {v / pos * 100:.0f}%"
        d.text((960 - d.textlength(amount, font=_font(22)), y + 4), amount, fill="#666666", font=_font(22))
        y += 52
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---- AI commentary: aggregated numbers only ---------------------------------------------------------

PROMPT = """You are a concise personal finance assistant. Below are one household member's card spending totals
by category (US dollars) for {label}, the previous period, and the average of the previous three periods.
Write 2-3 short bullet points (each under 25 words): what stands out, what changed, one practical tip.
No greetings. Plain text bullets starting with "• ".

{rows}
Subscriptions: {subs} active, {up} with a recent price increase."""


def commentary(llm, label: str, cur: Totals, prev: Totals | None, avg: dict[str, int], subs: list) -> str | None:
    if not llm or not cur.total:
        return None
    cats = sorted(set(cur.by_category) | set(prev.by_category if prev else {}) | set(avg))
    rows = "\n".join(f"{c}: {cur.by_category.get(c, 0) / 100:.0f} (previous {((prev.by_category if prev else {}).get(c, 0)) / 100:.0f}"
                     f", 3-period average {avg.get(c, 0) / 100:.0f})" for c in cats)
    rows += f"\nTotal: {cur.total / 100:.0f} (previous {(prev.total if prev else 0) / 100:.0f})"
    try:
        out = llm(PROMPT.format(label=label, rows=rows, subs=len(subs), up=sum(s.increased for s in subs)))
    except Exception as e:  # noqa: BLE001 — commentary is optional
        log.warning("Spending commentary failed: %s", type(e).__name__)
        return None
    lines = [l.strip() for l in (out or "").splitlines() if l.strip().startswith(("•", "-", "*"))]
    return "\n".join("• " + l.lstrip("•-* ").strip() for l in lines[:3]) or None


# ---- report text ---------------------------------------------------------------------------------

@dataclass
class Report:
    text: str
    chart: bytes | None = None
    caption: str = ""
    empty: bool = False


def _cat_lines(cur: Totals, prev: Totals | None, label: str) -> list[str]:
    lines = ["<b>By category</b>" + (f" (vs {label})" if prev and prev.total else "")]
    for c, v in cur.by_category.items():
        share = f" ({v / cur.total * 100:.0f}%)" if cur.total > 0 and v > 0 else ""
        delta = ""
        if prev and prev.total:
            dv = v - prev.by_category.get(c, 0)
            delta = f" {'▲' if dv > 0 else '▼'} {money(abs(dv), cents=False)}" if abs(dv) >= 500 else ""
        lines.append(f"• {_esc(c)} {money(v)}{share}{delta}")
    return lines


def _merchant_lines(db: DB, owner: str, cur: Totals, n: int = 5) -> list[str]:
    top = sorted(cur.by_merchant.items(), key=lambda kv: -kv[1][0])[:n]
    return ["<b>Top merchants</b>"] + [f"• {_esc(C.display_name(db, owner, k))} {money(v)} ({c}×)" for k, (v, c) in top]


def _sub_lines(db: DB, owner: str, subs: list[recurring.Subscription], p: Period, yearly: bool = False) -> list[str]:
    if not subs:
        return []
    per = sum(s.yearly_cents for s in subs) if yearly else sum(s.monthly_cents for s in subs)
    lines = [f"<b>Subscriptions</b> · {money(per)}{'/yr' if yearly else '/mo'}"]
    for s in subs[:8]:
        name = _esc(C.display_name(db, owner, s.merchant))
        tag = " 🆕" if s.first >= p.start else ""
        up = f" ⚠️ up from {money(s.previous_cents)}" if s.increased else ""
        lines.append(f"• {name} {money(s.amount_cents)}{'/yr' if s.cadence == 'yearly' else '/mo'}{up}{tag}")
    return lines


def _data_line(db: DB, owner: str, p: Period, today: date) -> list[str]:
    return notes(db, owner, p, today)


def build(db: DB, owner: str, kind: str, today: date, cards_cfg: dict | None = None, llm=None,
          current: bool = False) -> Report:
    p = period(kind, today, current)
    cur = totals(db, owner, p)
    if not cur.count:
        latest = db.rows("SELECT max(end) e FROM expense_statements WHERE owner=?", (owner,))[0]["e"]
        hint = f" Latest statement ends {date.fromisoformat(latest):%b %d}." if latest else " Send a statement PDF to start."
        return Report("\n".join([f"💳 <b>{p.label}</b>: no card activity recorded.{hint}"] + _data_line(db, owner, p, today)),
                      empty=True)
    prev = totals(db, owner, p.previous())
    head = f"<b>{p.label}</b> · <b>{money(cur.total)}</b> ({cur.count} items)"
    if kind == "week":
        biggest = sorted((r for r in cur.rows if r["kind"] == "purchase"), key=lambda r: -r["amount"])[:3]
        lines = [f"📅 {head}" + (f" · {change(cur.total, prev.total, 'prior week')}" if prev.total else ""),
                 "Top: " + " · ".join(f"{_esc(c)} {money(v, cents=False)}" for c, v in list(cur.by_category.items())[:3]),
                 "Biggest: " + " · ".join(f"{_esc(C.display_name(db, owner, r['merchant']))} {money(r['amount'])} "
                                          f"({date.fromisoformat(r['date']):%a})" for r in biggest)]
        return Report("\n".join(lines + _data_line(db, owner, p, today)))
    # month / quarter / year
    avg_periods = [prev] + [totals(db, owner, x) for x in _earlier(p, 2)]
    with_data = [t for t in avg_periods if t.count]
    avg = {c: sum(t.by_category.get(c, 0) for t in with_data) // len(with_data) for c in
           {c for t in with_data for c in t.by_category}} if with_data else {}
    avg_total = sum(t.total for t in with_data) // len(with_data) if with_data else 0
    prev_label = {"month": f"{p.previous().start:%b}", "quarter": p.previous().label, "year": p.previous().label}[kind]
    deltas = [x for x in (change(cur.total, prev.total, prev_label) if prev.count else "",
                          change(cur.total, avg_total, f"{len(with_data)}-{kind} avg") if len(with_data) > 1 else "") if x]
    icon = {"month": "🧾", "quarter": "📊", "year": "🎉"}[kind]
    lines = [f"{icon} {head}" + ("".join(f" · {d}" for d in deltas))]
    if kind == "year":
        lines += ["", "<b>By month</b>"] + _month_bars(db, owner, p)
    if kind == "quarter":
        lines += ["", "<b>By month</b>"] + _month_bars(db, owner, p)
    lines += [""] + _cat_lines(cur, prev if prev.count else None, prev_label)
    lines += [""] + _merchant_lines(db, owner, cur, 10 if kind == "year" else 5)
    subs = subscriptions(db, owner, p.end)
    if s := _sub_lines(db, owner, subs, p, yearly=kind == "year"):
        lines += [""] + s
    if kind in ("quarter", "year") and cards_cfg is not None:
        check, _missed = card_check(db, cards_cfg, owner, p)
        if check:
            lines += ["", "<b>Best card check</b>"] + check
    if kind == "year":
        big = sorted((r for r in cur.rows if r["kind"] == "purchase"), key=lambda r: -r["amount"])[:3]
        lines += ["", "<b>Biggest charges</b>"] + [f"• {_esc(C.display_name(db, owner, r['merchant']))} {money(r['amount'])} "
                                                  f"({date.fromisoformat(r['date']):%b %d})" for r in big]
    if note := commentary(llm, p.label, cur, prev if prev.count else None, avg, subs):
        lines += ["", "<b>Takeaways</b>", _esc(note)]
    if data := _data_line(db, owner, p, today):
        lines += [""] + data
    return Report("\n".join(lines).strip(), chart(f"{p.label} · by category", cur.by_category, cur.total),
                  f"{icon} {p.label} · {money(cur.total)}")


def _earlier(p: Period, n: int) -> list[Period]:
    out, x = [], p.previous()
    for _ in range(n):
        x = x.previous()
        out.append(x)
    return out


def _month_bars(db: DB, owner: str, p: Period) -> list[str]:
    months, d = [], p.start
    while d <= p.end:
        nxt = _month_start(d, -1)
        months.append((d, totals(db, owner, Period("month", d, min(nxt - timedelta(days=1), p.end))).total))
        d = nxt
    top = max((v for _, v in months), default=0) or 1
    return [f"<code>{m:%b} {'█' * max(0, round(v / top * 12)):<12}</code> {money(v, cents=False)}" for m, v in months]
