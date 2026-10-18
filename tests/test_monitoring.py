"""Monitoring tests: run ledger, proof of work, job / missed-run alarms, reminders, /status and /health."""
import asyncio
import json
import time

from helpers import _bot, _call, _cmd, _RecNotifier

from app import monitoring


def _setup(tmp_path, monkeypatch):
    b, _ = _bot(tmp_path, monkeypatch)
    b.services.n = _RecNotifier(b.db)
    b.services.features = [f for f in b.services.features if f.name != "vmhealth"]   # no host metrics in tests
    return b, b.services


def test_run_ledger_records_work_and_swallowed_errors(tmp_path, monkeypatch):
    b, s = _setup(tmp_path, monkeypatch)

    async def good(s, force=False):
        monitoring.note("3 new emails")
        monitoring.count_sent(2)

    async def swallows(s, force=False):
        try:
            raise RuntimeError("IMAP login failed")
        except RuntimeError as e:
            await s.report_error("gmail", e)          # the job "handled" it — the run must still count as failed

    asyncio.run(monitoring.monitored("good", good)(s))
    asyncio.run(monitoring.monitored("bad", swallows)(s))
    g, = s.db.recent_runs("good")
    assert g["ok"] == 1 and g["summary"] == "3 new emails · 2 message(s) sent" and g["ended"] >= g["started"]
    bad, = s.db.recent_runs("bad")
    assert bad["ok"] == 0 and bad["error"] == "gmail: IMAP login failed"
    assert s.n.sent == [("admin", "⚠️ <b>Alarm</b>: Job 'bad' failed: gmail: IMAP login failed")]   # immediately, admin only


def test_job_alarm_escalates_reminds_and_recovers(tmp_path, monkeypatch):
    b, s = _setup(tmp_path, monkeypatch)
    fail = [True]

    async def job(s, force=False):
        if fail[0]:
            raise RuntimeError("yahoo 500")
    run = monitoring.monitored("market", job)
    for _ in range(3):
        asyncio.run(run(s))
    assert [t for _, t in s.n.sent] == ["⚠️ <b>Alarm</b>: Job 'market' failed: yahoo 500",
                                         "🚨 <b>Escalated</b>: Job 'market' failed 3× in a row: yahoo 500"]
    assert not asyncio.run(monitoring.remind(s))                              # notified just now
    assert asyncio.run(monitoring.remind(s, now=time.time() + 25 * 3600))     # a day later: one reminder
    assert s.n.sent[-1][1] == "⏰ <b>Still firing</b>"
    assert not asyncio.run(monitoring.remind(s, now=time.time() + 26 * 3600))  # not again the same day
    fail[0] = False
    asyncio.run(run(s))
    assert s.n.sent[-1][1].startswith("✅ <b>Recovered</b>: Job 'market' failed 3× in a row")
    assert "✅ Job 'market'" in "\n".join(monitoring.format_alarms(s))          # resolved in the last 24h


def test_missed_scheduled_run_alarms(tmp_path, monkeypatch):
    """What the coroutine bug looked like: the scheduled time passed and the job never started."""
    b, s = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(monitoring, "PROCESS_START", time.time() - 5 * 3600)
    monkeypatch.setattr(s, "scheduled_jobs", lambda: [("mail", None, {}, {"minute": 0})])   # hourly
    (a,) = monitoring.missed_alarms(s)
    assert a.key == "missed:mail" and a.firing and "never started" in a.desc
    s.db.run_started("mail", time.time() - 60, False)                           # it ran a minute ago
    (a,) = monitoring.missed_alarms(s)
    assert not a.firing
    monkeypatch.setattr(monitoring, "PROCESS_START", time.time() - 600)        # just deployed: nothing due yet
    s.db._conn.execute("DELETE FROM job_runs")
    assert not monitoring.missed_alarms(s)[0].firing


def test_old_alarm_state_does_not_realert(tmp_path, monkeypatch):
    b, s = _setup(tmp_path, monkeypatch)
    s.db.set_kv("alarm:disk", "1")                                             # firing before the upgrade
    assert asyncio.run(monitoring.apply(s, [monitoring.Alarm("disk", True, "Disk 91% used")])) == []
    assert json.loads(s.db.get_kv("alarm:disk"))["firing"]


def test_error_outside_a_job_still_alerts_once(tmp_path, monkeypatch):
    b, s = _setup(tmp_path, monkeypatch)
    asyncio.run(s.report_error("brief command", RuntimeError("boom")))
    asyncio.run(s.report_error("brief command", RuntimeError("boom")))
    assert [t for _, t in s.n.sent] == ["⚠️ Job <b>brief command</b> failed: boom"]


def test_every_scheduled_job_is_in_the_ledger(tmp_path, monkeypatch):
    b, s = _setup(tmp_path, monkeypatch)
    for job_id, fn, _kw, _spec in s.scheduled_jobs():
        assert fn.__name__.startswith("monitored_"), job_id


def test_status_is_about_the_app_and_health_is_its_own_command(tmp_path, monkeypatch):
    b, s = _setup(tmp_path, monkeypatch)
    asyncio.run(s.run_job("cleanup", force=False))
    asyncio.run(monitoring.apply(s, [monitoring.Alarm("missed:mail", True, "Job 'mail' was due Fri 19:00 but never started")]))
    out = []
    _call(b, asyncio, b.status, 1, "/status", out)
    text = "\n".join(t for t, _ in out)
    assert "🔴 Job 'mail' was due Fri 19:00 but never started" in text
    assert "✅ cleanup:" in text and "pruned 0 row(s)" in text and "<b>People</b>" in text
    assert "VM health" not in text and "Disk" not in text
    assert _cmd(b, "health")                                                    # dedicated /health
    out = []
    _call(b, asyncio, _cmd(b, "health"), 2, "/health", out)                     # not for non-admins
    assert out[-1][0] == "That feature isn't enabled for you."
