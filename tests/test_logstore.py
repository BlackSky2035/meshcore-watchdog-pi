import os
import sqlite3
import stat

import pytest

from watchdog import demo
from watchdog.demo import DemoUpdater
from watchdog.logstore import JobRecorder, LogStore


def test_roundtrip(tmp_path):
    s = LogStore(tmp_path / "w.db")
    jid = s.start_job("update", 100.0)
    s.add_line(jid, 101.0, "eins")
    s.add_line(jid, 102.0, "zwei")
    s.finish_job(jid, 103.0, True, "fertig")
    s.close()
    job = LogStore(tmp_path / "w.db").last_job()
    assert job["name"] == "update" and job["ok"] is True and job["message"] == "fertig"
    assert [l["text"] for l in job["log"]] == ["eins", "zwei"]


def test_list_jobs_and_job_lines(tmp_path):
    s = LogStore(tmp_path / "w.db")
    j1 = s.start_job("check", 10.0)
    s.add_line(j1, 10.5, "nichts Neues")
    s.finish_job(j1, 11.0, True, "aktuell")
    j2 = s.start_job("update", 20.0)
    s.add_line(j2, 20.5, "eins")
    s.add_line(j2, 20.7, "zwei")
    s.finish_job(j2, 21.0, True, "erledigt")
    jobs = s.list_jobs()
    assert [j["id"] for j in jobs] == [j2, j1]          # neueste zuerst
    assert jobs[0]["name"] == "update" and jobs[0]["ok"] is True
    assert [l["text"] for l in s.job_lines(j2)] == ["eins", "zwei"]
    assert s.job_lines(999) == []                        # unbekannte ID: leer, kein Fehler


def test_list_jobs_unusable_store_returns_empty(tmp_path):
    s = LogStore(tmp_path / "w.db")
    s._db = None
    s._retry_at = float("inf")
    assert s.list_jobs() == [] and s.job_lines(1) == []


def test_interrupted_job_is_marked(tmp_path):
    s = LogStore(tmp_path / "w.db")
    jid = s.start_job("update", 1.0)
    s.add_line(jid, 2.0, "läuft …")
    s.close()                                     # kein finish_job: Strom weg
    rec = JobRecorder(LogStore(tmp_path / "w.db"))
    job = rec.restore()
    assert job["running"] is False and job["ok"] is False and "Unterbrochen" in job["message"]
    assert job["log"][0]["text"] == "läuft …"
    # beim nächsten Start ist der Lauf als beendet gespeichert
    assert JobRecorder(LogStore(tmp_path / "w.db")).restore()["message"].startswith("Unterbrochen")


def test_corrupt_file_is_set_aside_and_recreated(tmp_path):
    path = tmp_path / "w.db"
    path.write_bytes(b"das ist keine sqlite datei" * 100)
    s = LogStore(path)
    assert s.available
    assert any(".corrupt-" in p.name for p in tmp_path.iterdir())
    jid = s.start_job("check", 1.0)
    s.add_line(jid, 2.0, "ok")
    assert s.last_job()["log"][0]["text"] == "ok"


def test_unusable_location_never_raises(tmp_path):
    blocker = tmp_path / "datei"
    blocker.write_text("x")
    s = LogStore(blocker / "unter" / "w.db")      # Elternteil ist eine Datei: nicht anlegbar
    assert not s.available
    assert s.start_job("x", 1.0) is None
    s.add_line(None, 1.0, "egal")
    s.finish_job(None, 1.0, True, "egal")
    assert s.last_job() is None
    rec = JobRecorder(s)
    rec.begin("x", 1.0); rec.line(1.0, "t"); rec.end(2.0, True, "m")
    assert rec.restore() is None


def test_no_store_at_all():
    rec = JobRecorder(None)
    rec.begin("x", 1.0); rec.line(1.0, "t"); rec.end(2.0, True, "m")
    assert rec.restore() is None


def test_errors_during_use_are_swallowed_then_recover(tmp_path, monkeypatch):
    s = LogStore(tmp_path / "w.db")
    jid = s.start_job("a", 1.0)
    s._db.close()                                 # Verbindung stirbt mitten im Betrieb
    s.add_line(jid, 2.0, "verloren")              # darf nicht werfen
    assert not s.available
    s._retry_at = 0                               # Wartezeit abgelaufen: neuer Versuch klappt
    s.add_line(s.start_job("b", 3.0), 4.0, "wieder da")
    assert s.available and s.last_job()["name"] == "b"


def test_bad_rows_are_skipped_on_load(tmp_path):
    s = LogStore(tmp_path / "w.db")
    jid = s.start_job("a", 1.0)
    s.add_line(jid, 2.0, "gut")
    s.close()
    db = sqlite3.connect(tmp_path / "w.db")
    db.execute("INSERT INTO job_log(job_id, ts, text) VALUES(?,?,?)", (jid, "kaputt", "böse"))
    db.commit(); db.close()
    job = LogStore(tmp_path / "w.db").last_job()
    assert [l["text"] for l in job["log"]] == ["gut"]


def test_old_jobs_are_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr("watchdog.logstore.KEEP_JOBS", 3)
    s = LogStore(tmp_path / "w.db")
    for i in range(6):
        jid = s.start_job(f"j{i}", float(i))
        s.add_line(jid, float(i), "x")
    db = sqlite3.connect(tmp_path / "w.db")
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(DISTINCT job_id) FROM job_log").fetchone()[0] <= 3


def test_updater_log_survives_restart(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(demo, "STEP_S", 0)
    cfg = SimpleNamespace(data_dir=str(tmp_path))
    d = DemoUpdater(cfg)
    d.run("check")
    d2 = DemoUpdater(cfg)                         # „Gerät neu gestartet“
    st = d2.status()["job"]
    assert st["name"] == "check" and st["running"] is False and st["ok"] is True
    assert any("Hole Änderungen" in l["text"] for l in st["log"])


def test_app_runs_even_if_db_breaks_midway(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(demo, "STEP_S", 0)
    d = DemoUpdater(SimpleNamespace(data_dir=str(tmp_path)))
    d.rec.store._db.close()                       # Datenbank bricht weg
    ok, msg = d.run("check")
    assert ok and "neue" in msg                   # Aktion läuft trotzdem sauber durch
