"""Expense storage: imported statements and their transactions (tables in app/db.py)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.db import DB
from app.features.expenses import categories as C
from app.features.expenses.statements import Statement

SPEND_KINDS = ("purchase", "credit", "fee", "interest")     # payments to the card aren't spending


def statement_id(owner: str, st: Statement) -> str:
    return f"{owner}:{st.bank}:{st.last4}:{st.end.isoformat()}"


def imported_at(db: DB, sid: str) -> str | None:
    r = db.rows("SELECT imported FROM expense_statements WHERE id=?", (sid,))
    return r[0]["imported"] if r else None


TIP_SLACK = 0.30            # an alert can show the pre-tip amount; the statement may be up to 30% higher
DATE_SLACK = 4              # days between the alert and the statement's transaction date


def match_alerts(alerts: list[tuple[str, date, int]], txns: list[tuple[date, int, str]]) -> dict[str, int]:
    """Pair alert charges (id, date, cents) with statement lines (date, cents, category): exact amount first,
    then a tip-adjusted one (Dining, statement up to 30% higher). Counts: confirmed / tip / dropped (an
    alert with no statement line: declined or reversed)."""
    free = list(range(len(txns)))
    counts = {"confirmed": 0, "tip": 0, "dropped": 0}
    for _id, d, amount in sorted(alerts, key=lambda a: a[1]):
        near = [i for i in free if abs((txns[i][0] - d).days) <= DATE_SLACK]
        exact = next((i for i in near if txns[i][1] == amount), None)
        tip = None if exact is not None else next(
            (i for i in near if txns[i][2] == "Dining" and amount <= txns[i][1] <= amount * (1 + TIP_SLACK)), None)
        if (hit := exact if exact is not None else tip) is not None:
            free.remove(hit)
            counts["confirmed" if exact is not None else "tip"] += 1
        else:
            counts["dropped"] += 1
    return counts


def alerts_in(db: DB, owner: str, st: Statement) -> list:
    start = (st.start or st.end - timedelta(days=31)) - timedelta(days=DATE_SLACK)
    return db.rows("SELECT id, date, amount, category FROM expense_txns WHERE owner=? AND bank=? AND last4=? AND source='alert' "
                   "AND date>=? AND date<=?", (owner, st.bank, st.last4, start.isoformat(), st.end.isoformat()))


def preview_matches(db: DB, owner: str, st: Statement) -> dict[str, int]:
    rows = alerts_in(db, owner, st)
    if not rows:
        return {}
    cat = lambda t: C.resolve(db, owner, t.description, t.bank_category)  # noqa: E731
    return match_alerts([(r["id"], date.fromisoformat(r["date"]), r["amount"]) for r in rows],
                        [(t.date, t.amount_cents, (cat(t) or ("", ""))[0]) for t in st.txns if t.kind == "purchase"])


def import_statement(db: DB, owner: str, st: Statement) -> str:
    """The statement is authoritative for its card and period: it replaces an earlier import of itself and
    any alert-email charges in that window (see match_alerts for how they line up). Only balanced statements
    get here."""
    sid = statement_id(owner, st)
    stale_alerts = [r["id"] for r in alerts_in(db, owner, st)]
    with db.tx() as c:
        c.executemany("DELETE FROM expense_txns WHERE id=?", [(i,) for i in stale_alerts])
        c.execute("DELETE FROM expense_txns WHERE statement=?", (sid,))
        c.execute("DELETE FROM expense_statements WHERE id=?", (sid,))
        c.execute("INSERT INTO expense_statements VALUES (?,?,?,?,?,?,?,?,?)",
                  (sid, owner, st.bank, st.last4, st.start.isoformat() if st.start else None, st.end.isoformat(),
                   st.previous_cents, st.new_cents, datetime.now(UTC).isoformat()))
        c.executemany("INSERT INTO expense_txns(id, owner, bank, last4, date, amount, description, kind, bank_category, "
                      "source, statement, merchant) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      [(f"{sid}:{i}", owner, st.bank, st.last4, t.date.isoformat(), t.amount_cents, t.description, t.kind,
                        t.bank_category, "statement", sid, C.merchant_key(t.description)) for i, t in enumerate(st.txns)])
    return sid


FIXED_KIND_CATEGORY = {"fee": "Fees & interest", "interest": "Fees & interest", "cash_advance": "Other",
                       "balance_transfer": "Other"}


def categorize(db: DB, llm=None, owner: str | None = None) -> dict[str, int]:
    """Fill in missing categories (all people, or one). Rules and learned map first; the merchants still
    unknown go to Gemini in one batch (names only). Returns counts by how they were resolved."""
    sql = "SELECT id, owner, description, kind, bank_category, merchant FROM expense_txns WHERE category='' AND kind!='payment'"
    rows = db.rows(sql + (" AND owner=?" if owner else ""), (owner,) if owner else ())
    counts: dict[str, int] = {}
    todo: dict[str, list] = {}
    updates = []
    for r in rows:
        key = r["merchant"] or C.merchant_key(r["description"])
        if cat := FIXED_KIND_CATEGORY.get(r["kind"]):
            hit = (cat, "kind")
        else:
            hit = C.resolve(db, r["owner"], r["description"], r["bank_category"])
        if hit:
            updates.append((hit[0], key, r["id"]))
            counts[hit[1]] = counts.get(hit[1], 0) + 1
        else:
            todo.setdefault(key, []).append(r)
    if todo:
        answers = C.ask_ai(sorted(todo), llm)
        for key, rs in todo.items():
            if cat := answers.get(key):
                C.learn(db, "", key, cat, "ai")
                updates += [(cat, key, r["id"]) for r in rs]
                counts["ai"] = counts.get("ai", 0) + len(rs)
            else:
                counts["unknown"] = counts.get("unknown", 0) + len(rs)
    with db.tx() as c:
        c.executemany("UPDATE expense_txns SET category=?, merchant=? WHERE id=?", updates)
    return counts


def set_category(db: DB, owner: str, key: str, category: str) -> int:
    """The person's own fix: remembered for future charges and applied to their past ones."""
    C.learn(db, owner, key, category, "user")
    with db.tx() as c:
        return c.execute("UPDATE expense_txns SET category=? WHERE owner=? AND merchant=? AND kind!='payment'",
                         (category, owner, key)).rowcount


def merchants_like(db: DB, owner: str, text: str, limit: int = 8) -> list:
    return db.rows("SELECT merchant, category, count(*) n, sum(amount) total FROM expense_txns WHERE owner=? AND kind!='payment' "
                   "AND (merchant LIKE ? OR description LIKE ?) GROUP BY merchant ORDER BY total DESC LIMIT ?",
                   (owner, f"%{text}%", f"%{text}%", limit))


def spending(db: DB, owner: str, start: date, end: date) -> list:
    q = ",".join("?" * len(SPEND_KINDS))
    return db.rows(f"SELECT * FROM expense_txns WHERE owner=? AND date>=? AND date<=? AND kind IN ({q}) ORDER BY date",
                   (owner, start.isoformat(), end.isoformat(), *SPEND_KINDS))


def statements(db: DB, owner: str) -> list:
    return db.rows("SELECT * FROM expense_statements WHERE owner=? ORDER BY end DESC", (owner,))


def add_alert(db: DB, owner: str, alert, message_id: str) -> str:
    """'added', 'covered' (a statement already covers that card and day: it's authoritative) or 'duplicate'."""
    covered = db.rows("SELECT 1 FROM expense_statements WHERE owner=? AND bank=? AND last4=? AND start<=? AND end>=?",
                      (owner, alert.bank, alert.last4, alert.date.isoformat(), alert.date.isoformat()))
    if covered:
        return "covered"
    with db.tx() as c:
        n = c.execute("INSERT OR IGNORE INTO expense_txns(id, owner, bank, last4, date, amount, description, kind, "
                      "bank_category, source, merchant) VALUES (?,?,?,?,?,?,?,?,'','alert',?)",
                      (f"alert:{message_id}", owner, alert.bank, alert.last4, alert.date.isoformat(), alert.amount_cents,
                       alert.merchant, alert.kind, C.merchant_key(alert.merchant))).rowcount
    return "added" if n else "duplicate"
