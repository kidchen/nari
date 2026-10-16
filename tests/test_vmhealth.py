"""Vmhealth tests."""
from pathlib import Path

from helpers import (
    _thresholds,
)

from app.db import DB


def test_health_alarms_fire_once_and_recover():
    from app.features.vmhealth import vmhealth as vh
    db = DB(":memory:")
    now = 1_800_000_000.0
    m = vh.Metrics(load15=0.3, cores=1, mem_total_mb=6000, mem_avail_mb=4000, disk_used_pct=40.0, newest_backup_age_d=1)
    assert vh.transitions(db, vh.conditions(m, _thresholds(), now)) == []          # healthy: silent
    m.disk_used_pct = 91.0
    (key, firing, desc), = vh.transitions(db, vh.conditions(m, _thresholds(), now))
    assert (key, firing) == ("disk", True) and "Disk 91% used" in desc
    assert vh.transitions(db, vh.conditions(m, _thresholds(), now)) == []          # still firing: no repeat
    m.disk_used_pct = 50.0
    assert vh.transitions(db, vh.conditions(m, _thresholds(), now)) == [("disk", False, "Disk 50% used (alarm ≥ 85%)")]


def test_health_job_failure_alarm_and_format():
    from app.features.vmhealth import vmhealth as vh
    db = DB(":memory:")
    now = 1_800_000_000.0
    for i in range(3):
        vh.record_failure(db, "gmail", "IMAP login failed", now - 600 * i)
    vh.record_failure(db, "market", "yahoo 500", now - 20 * 3600)               # old: outside the 6h window
    m = vh.Metrics(cores=1, mem_total_mb=6000, mem_avail_mb=4000, disk_total_gb=30, disk_used_pct=40.0)
    m.job_failures = {"gmail": [now - 600 * i for i in range(3)], "market": [now - 20 * 3600]}
    m.last_errors = {"gmail": "IMAP login failed", "market": "yahoo 500"}
    firing = {k for k, f, _ in vh.conditions(m, _thresholds(), now) if f}
    assert firing == {"job:gmail"}
    m.oci = {"error": "not permitted — create the dynamic group + policy (README → VM health)"}
    text = vh.format_health(m, _thresholds(), now)
    assert "🟢 Disk 40% of 30 GB" in text and "🔴 gmail: 3×" in text and "🟢 market: 1×" in text
    assert "OCI: not permitted" in text


def test_vmhealth_is_admin_only():
    from app.access import Access
    a = Access({"admin": {"primary": True}, "other": {"features": "all"}}, DB(":memory:"))
    assert a.recipients("vmhealth") == ["admin"]


def test_watchdog_alerts_once_and_recovers(tmp_path):
    import os
    import stat
    import subprocess
    bin_dir, state = tmp_path / "bin", tmp_path / "state"
    bin_dir.mkdir()
    status_file, sent_file = tmp_path / "status", tmp_path / "sent"
    (bin_dir / "docker").write_text(f'#!/bin/sh\ncat {status_file}\n')
    (bin_dir / "curl").write_text(f'#!/bin/sh\ncat >/dev/null\nfor a in "$@"; do case "$a" in text=*) echo "$a" >> {sent_file};; esac; done\n')
    (bin_dir / "hostname").write_text("#!/bin/sh\necho vm\n")
    for f in bin_dir.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    env_file, chat = tmp_path / ".env", tmp_path / "admin_chat_id"
    env_file.write_text("TELEGRAM_BOT_TOKEN=123:abc\n")
    chat.write_text("42")
    script = (Path(__file__).parent.parent / "deploy" / "watchdog.sh").read_text()
    script = script.replace("/var/lib/nari-watchdog", str(state)).replace(
        "/var/lib/docker/volumes/nari-data/_data/admin_chat_id", str(chat))
    (tmp_path / "wd.sh").write_text(script)
    run = lambda s: (status_file.write_text(s), subprocess.run(  # noqa: E731
        ["bash", str(tmp_path / "wd.sh"), str(env_file)], env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        check=True))
    sent = lambda: sent_file.read_text().splitlines() if sent_file.exists() else []  # noqa: E731
    run("running/healthy"); assert sent() == []
    run("exited/none"); assert sent() == []                         # 1st bad check: no page (deploy restarts)
    run("running/starting"); assert sent() == []                    # starting: ignored
    run("exited/none"); assert len(sent()) == 1 and "DOWN" in sent()[0]
    run("exited/none"); assert len(sent()) == 1                     # still down: no repeat
    run("running/healthy"); assert len(sent()) == 2 and "back" in sent()[1]


def test_counter_delta_handles_reset():
    from app.features.vmhealth.vmhealth import counter_delta
    assert counter_delta([100, 150, 400]) == 300
    assert counter_delta([100, 150, 20, 70]) == 50 + 20 + 50       # reboot reset the counter
    assert counter_delta([1000, 1100, 1090, 1200]) == 100 + 110     # small aggregation dip: not a reset
    assert counter_delta([5]) == 0


def test_restarts_count_crashes_not_deploys():
    from app.features.vmhealth import vmhealth as vh
    db = DB(":memory:")
    now = 1_800_000_000.0
    for i, cid in enumerate(["c1", "c2", "c3"]):                    # three deploys: new container each time
        vh.record_start(db, now - 3600 * (3 - i), cid)
    assert vh.restarts_24h(db, now) == 0
    vh.record_start(db, now - 60, "c3")                             # Docker restarted the same container
    vh.record_start(db, now - 30, "c3")
    assert vh.restarts_24h(db, now) == 2
    db.set_kv("health:starts", "[1799999000.0, 1799999500.0]")     # old format (plain timestamps) is ignored
    assert vh.restarts_24h(db, now) == 0
