"""Monitoring: a ledger of every job run, missed-run detection, and one alarm engine for everything.

- Every scheduled (and /run) job goes through `monitored()`, which records start, end, what it did
  ("proof of work": messages sent + notes like "3 new emails") and errors. Jobs catch their own
  exceptions and call `services.report_error`, which marks the *current run* failed (a ContextVar),
  so a run that swallowed an error still shows up as failed.
- Alarm sources: job alarms (last run failed; 🔴 after N failures in a row), missed runs (a cron time
  passed but the job never started — what the 'lambda returns a coroutine' bug looked like), and
  feature alarms (`Feature.alarms`, e.g. VM health).
- The engine notifies the primary profile when an alarm fires or recovers (any hour), and once a day
  with a reminder while anything is still firing.
"""
from __future__ import annotations

import html
import json
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from apscheduler.triggers.cron import CronTrigger

if TYPE_CHECKING:
    from app.registry import JobFn, Schedule
    from app.services import Services

log = logging.getLogger(__name__)
PROCESS_START = time.time()
CRITICAL, WARNING = "critical", "warning"
ICON = {CRITICAL: "🔴", WARNING: "🟡"}
NOT_MISSABLE = {"alarms"}       # the alarm job can't report on itself; the host watchdog covers a dead bot


# ---- run ledger -------------------------------------------------------------------------------

@dataclass
class Run:
    job: str
    started: float
    manual: bool = False
    sent: int = 0
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = list(self.notes)
        if self.sent:
            parts.append(f"{self.sent} message(s) sent")
        return " · ".join(parts)


_current: ContextVar[Run | None] = ContextVar("nari_run", default=None)


def note(text: str) -> None:
    """Record what a job did (shown in /status). No-op outside a monitored run."""
    if r := _current.get():
        r.notes.append(text)


def count_sent(n: int) -> None:
    if (r := _current.get()) and n:
        r.sent += n


def record_error(where: str, msg: str) -> bool:
    """Mark the current run failed. False if not inside a monitored run (e.g. a command handler)."""
    r = _current.get()
    if r is None:
        return False
    r.errors.append(msg if where == r.job else f"{where}: {msg}")
    return True


def make_trigger(spec: Schedule, tz: Any) -> CronTrigger:
    return CronTrigger.from_crontab(spec, timezone=tz) if isinstance(spec, str) else CronTrigger(timezone=tz, **spec)


def monitored(job_id: str, fn: JobFn) -> JobFn:
    """Wrap a job so every run lands in the ledger. Must stay `async def`: APScheduler only awaits
    coroutine functions."""
    async def run(s: Services, force: bool = False, **kwargs: Any) -> None:
        r = Run(job_id, time.time(), manual=force)
        row = s.db.run_started(job_id, r.started, force)
        token = _current.set(r)
        try:
            await fn(s, force=force, **kwargs)
        except Exception as e:  # noqa: BLE001 — a job that raised must still be recorded
            log.exception("Job %s raised", job_id)
            r.errors.append(str(e))
        finally:
            _current.reset(token)
        s.db.run_finished(row, time.time(), not r.errors, r.summary(), "; ".join(r.errors)[:500])
        log.info("Job %s %s in %.1fs: %s", job_id, "failed" if r.errors else "ok", time.time() - r.started,
                 r.summary() or "-")
        await apply(s, [job_alarm(s, job_id)])               # fire / recover right away, not 10 min later
    run.__name__ = run.__qualname__ = f"monitored_{job_id.replace(':', '_')}"
    return run


# ---- alarms ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Alarm:
    key: str
    firing: bool
    desc: str
    severity: str = CRITICAL


def job_alarm(s: Services, job_id: str) -> Alarm:
    runs = s.db.recent_runs(job_id, 20)
    finished = [r for r in runs if r["ended"] is not None]
    fails = 0
    for r in finished:                   # newest first: consecutive failures
        if r["ok"]:
            break
        fails += 1
    limit = s.cfg.settings.get("monitoring", {}).get("job_failures_critical", 3)
    err = finished[0]["error"] if finished else ""
    desc = f"Job '{job_id}' failed" + (f" {fails}× in a row" if fails > 1 else "") + f": {err[:200]}"
    return Alarm(f"job:{job_id}", fails > 0, desc, CRITICAL if fails >= limit else WARNING)


def missed_alarms(s: Services, now: float | None = None) -> list[Alarm]:
    """A scheduled time passed (plus grace) without the job starting."""
    now = now or time.time()
    grace = s.cfg.settings.get("monitoring", {}).get("missed_grace_minutes", 30) * 60
    last = s.db.last_starts()
    out = []
    for job_id, _fn, _kw, spec in s.scheduled_jobs():
        if job_id in NOT_MISSABLE:
            continue
        baseline = max(last.get(job_id, 0.0), PROCESS_START)
        due = make_trigger(spec, s.cfg.tz).get_next_fire_time(None, datetime.fromtimestamp(baseline, s.cfg.tz))
        missed = due is not None and due.timestamp() < now - grace
        out.append(Alarm(f"missed:{job_id}", missed,
                         f"Job '{job_id}' was due {due:%a %H:%M} but never started" if missed else f"Job '{job_id}' on schedule"))
    return out


def _load(s: Services, key: str) -> dict:
    raw = s.db.get_kv(f"alarm:{key}")
    if raw in (None, "0"):
        return {"firing": False}
    if raw == "1":                                        # pre-engine format: firing, already notified
        return {"firing": True, "since": time.time(), "notified": time.time(), "desc": key, "severity": CRITICAL}
    return json.loads(raw)


def _save(s: Services, key: str, st: dict) -> None:
    s.db.set_kv(f"alarm:{key}", json.dumps(st))


async def _notify(s: Services, text: str) -> None:
    try:
        await s.n.send_text([s.cfg.primary_profile], text)
    except Exception as e:  # noqa: BLE001 — never let alarm delivery raise into the alarm engine
        log.error("Could not deliver alarm message: %s", e)


def _ago(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m}m" if m < 120 else f"{m // 60}h" if m < 48 * 60 else f"{m // 1440}d"


async def apply(s: Services, alarms: list[Alarm], now: float | None = None) -> list[str]:
    """Compare with stored state; message on fire, escalation and recovery. Returns the lines sent."""
    now = now or time.time()
    lines = []
    for a in alarms:
        st = _load(s, a.key)
        if a.firing and not st["firing"]:
            st = {"firing": True, "since": now, "notified": now, "desc": a.desc, "severity": a.severity}
            lines.append(f"{'🚨' if a.severity == CRITICAL else '⚠️'} <b>Alarm</b>: {html.escape(a.desc, quote=False)}")
        elif a.firing:
            if a.severity == CRITICAL and st.get("severity") != CRITICAL:
                lines.append(f"🚨 <b>Escalated</b>: {html.escape(a.desc, quote=False)}")
                st["notified"] = now
            st.update(desc=a.desc, severity=a.severity)
        elif st["firing"]:
            st = {"firing": False, "resolved": now, "desc": st.get("desc", a.key), "severity": st.get("severity", CRITICAL),
                  "since": st.get("since", now)}
            lines.append(f"✅ <b>Recovered</b>: {html.escape(st['desc'], quote=False)} (after {_ago(now - st['since'])})")
        else:
            continue
        _save(s, a.key, st)
    if lines:
        await _notify(s, "\n".join(lines))
    return lines


def state(s: Services) -> dict[str, dict]:
    return {k.split(":", 1)[1]: _load(s, k.split(":", 1)[1]) for k, _ in s.db.kv_like("alarm:%")}


async def remind(s: Services, now: float | None = None) -> bool:
    """Once a day per alarm while it keeps firing; batched into one message."""
    now = now or time.time()
    every = s.cfg.settings.get("monitoring", {}).get("reminder_hours", 24) * 3600
    due = {k: st for k, st in state(s).items() if st["firing"] and now - st.get("notified", 0) >= every}
    if not due:
        return False
    lines = ["⏰ <b>Still firing</b>"] + [f"{ICON.get(st.get('severity'), '🔴')} {html.escape(st.get('desc', k), quote=False)} "
                                         f"(for {_ago(now - st.get('since', now))})" for k, st in sorted(due.items())]
    for k, st in due.items():
        st["notified"] = now
        _save(s, k, st)
    await _notify(s, "\n".join(lines))
    return True


async def evaluate(s: Services, force: bool = False) -> None:
    """The `alarms` job (every 10 min): every source, then reminders."""
    alarms = missed_alarms(s) + [job_alarm(s, j) for j in s.db.run_jobs()]
    for f in s.features:
        if f.alarms and f.name in s.access.allowed(s.cfg.primary_profile):
            try:
                alarms += await f.alarms(s)
            except Exception as e:  # noqa: BLE001
                await s.report_error(f"{f.name} alarms", e)
    sent = await apply(s, alarms)
    reminded = await remind(s)
    s.db.set_kv("alarms:last_eval", str(time.time()))
    firing = [k for k, st in state(s).items() if st["firing"]]
    note(f"{len(alarms)} checks, {len(firing)} firing")
    if force and not sent and not reminded:
        await _notify(s, "🩺 Alarm check: " + (f"{len(firing)} still firing — see /status" if firing else "all clear"))


# ---- /status ----------------------------------------------------------------------------------

def _when(ts: float, tz: Any, now: float) -> str:
    return f"{datetime.fromtimestamp(ts, tz):%a %H:%M} ({_ago(now - ts)} ago)"


def format_alarms(s: Services, now: float | None = None) -> list[str]:
    now = now or time.time()
    st = state(s)
    firing = sorted(((k, v) for k, v in st.items() if v["firing"]), key=lambda kv: (kv[1].get("severity") != CRITICAL, kv[0]))
    resolved = sorted(((k, v) for k, v in st.items() if not v["firing"] and now - v.get("resolved", 0) < 86400),
                      key=lambda kv: -kv[1]["resolved"])
    last = float(s.db.get_kv("alarms:last_eval") or 0)
    lines = ["<b>Alarms</b>" + (f" <i>(checked {_ago(now - last)} ago)</i>" if last else " <i>(not checked yet)</i>")]
    lines += [f"{ICON.get(v.get('severity'), '🔴')} {html.escape(v.get('desc', k), quote=False)} · for {_ago(now - v.get('since', now))}"
              for k, v in firing] or ["🟢 Nothing firing"]
    lines += [f"✅ {html.escape(v.get('desc', k), quote=False)} · resolved {_ago(now - v['resolved'])} ago" for k, v in resolved]
    return lines


def format_jobs(s: Services, next_runs: dict[str, datetime], now: float | None = None) -> list[str]:
    now = now or time.time()
    lines = ["<b>Jobs</b>"]
    scheduled = {j for j, *_ in s.scheduled_jobs()}
    for job in sorted(scheduled | set(s.db.run_jobs())):
        runs = s.db.recent_runs(job, 1)
        if job not in scheduled and (not runs or now - runs[0]["started"] > 86400):
            continue                     # manual-only jobs: only if run in the last day
        nxt = f" · next {next_runs[job]:%a %H:%M}" if job in next_runs else ""
        if not runs:
            lines.append(f"⚪ {html.escape(job, quote=False)}: never run{nxt}")
            continue
        r = runs[0]
        if r["ended"] is None:
            icon, what = "⏳", "running"
        elif r["ok"]:
            icon, what = "✅", r["summary"] or "ok"
        else:
            icon, what = "❌", r["error"][:120]
        lines.append(f"{icon} {html.escape(job, quote=False)}: {_when(r['started'], s.cfg.tz, now)}"
                     f"{' (manual)' if r['manual'] else ''} · {html.escape(what, quote=False)}{nxt}")
    return lines
