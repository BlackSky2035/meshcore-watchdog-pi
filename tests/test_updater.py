import json
import time

import pytest

from conftest import git


def started(env):
    ok, msg = env.up.run("start")
    assert ok, msg


def test_start_stop(env):
    started(env)
    assert env.version() == "1"
    assert env.up.status()["app"] == "running"
    ok, _ = env.up.run("stop")
    assert ok and env.up.status()["app"] == "stopped"
    assert env.up.status()["last_good"] == env.head()


def test_status_includes_app_url(env):
    assert env.up.status()["app_url"] == f"http://127.0.0.1:{env.port}"


def test_health_check_is_throttled_when_idle_but_fast_when_busy(env, monkeypatch):
    calls = [0]
    orig = env.up._health
    env.up._health = lambda: (calls.__setitem__(0, calls[0] + 1), orig())[1]
    started(env)
    calls[0] = 0
    t = [1000.0]
    monkeypatch.setattr("watchdog.updater.time.monotonic", lambda: t[0])
    env.up._last_health_at = t[0]           # so tun, als wäre gerade eben erst geprüft worden
    env.up._was_active = True
    env.up.status()
    assert calls[0] == 0                     # noch innerhalb der 30 s: keine neue Prüfung
    t[0] += 31
    env.up.status()
    assert calls[0] == 1                     # 30 s um: wieder geprüft
    env.up.job["running"] = True
    t[0] += 3                                # über dem „beschäftigt“-Schwellenwert (2 s), aber unter 30 s
    env.up.status()
    assert calls[0] == 2


def test_health_check_runs_immediately_after_becoming_active_again(env, monkeypatch):
    calls = [0]
    orig = env.up._health
    env.up._health = lambda: (calls.__setitem__(0, calls[0] + 1), orig())[1]
    started(env)
    calls[0] = 0
    t = [2000.0]
    monkeypatch.setattr("watchdog.updater.time.monotonic", lambda: t[0])
    env.up._last_health_at = t[0]
    env.up._was_active = False               # z. B. gerade eben erst wieder gestartet
    env.up.status()
    assert calls[0] == 1                     # sofort geprüft, nicht bis zu 30 s gewartet


def test_app_link_url_overrides_derived_app_url(env):
    env.cfg.app_link_url = "https://node-pi.example.ts.net"
    assert env.up.status()["app_url"] == "https://node-pi.example.ts.net"


def test_check_no_update(env):
    ok, msg = env.up.run("check")
    assert ok and "aktuell" in msg.lower()
    assert env.up.state["update_info"]["behind"] == 0


def test_check_reports_update(env):
    env.push("feature x", VERSION="2\n")
    ok, msg = env.up.run("check")
    assert ok and "1 neue" in msg
    info = env.up.state["update_info"]
    assert info["behind"] == 1 and [x.split(" ", 1)[1] for x in info["subjects"]] == ["feature x"]


def test_successful_update(env):
    started(env)
    new = env.push("v2", VERSION="2\n")
    ok, msg = env.up.run("update")
    assert ok, msg
    assert env.head() == new
    assert env.version() == "2"
    assert env.up.state["last_good"] == new
    assert env.up.state["blocked"] is None
    assert not (env.app / "INSTALLED").exists()        # requirements.txt unverändert


def test_successful_update_clears_the_pending_update_notice(env):
    """Nach einem erfolgreichen Update soll sofort "aktuell" angezeigt werden, ohne erneutes "Prüfen"."""
    started(env)
    env.push("v2", VERSION="2\n")
    ok, _ = env.up.run("check")
    assert ok and env.up.state["update_info"]["behind"] == 1
    ok, msg = env.up.run("update")
    assert ok, msg
    assert env.up.state["update_info"]["behind"] == 0
    assert env.up.state["update_info"]["subjects"] == []


def test_update_nothing_to_do(env):
    started(env)
    ok, msg = env.up.run("update")
    assert ok and "aktuell" in msg.lower()


def test_broken_update_rolls_back(env):
    started(env)
    good = env.head()
    bad = env.push("kaputt", BROKEN="1")
    ok, msg = env.up.run("update")
    assert not ok and "wiederhergestellt" in msg
    assert env.head() == good
    assert env.version() == "1"
    assert env.up.state["blocked"] == bad
    assert env.up.state["last_good"] == good


def test_blocked_commit_not_retried_without_force(env):
    started(env)
    env.push("kaputt", BROKEN="1")
    env.up.run("update")
    ok, msg = env.up.run("update")
    assert not ok and "fehlerhaft markiert" in msg
    assert env.version() == "1"


def test_fix_after_blocked_is_installed(env):
    started(env)
    env.push("kaputt", BROKEN="1")
    env.up.run("update")
    (env.seed / "BROKEN").unlink()
    env.push("repariert", VERSION="3\n")
    ok, msg = env.up.run("update")
    assert ok, msg
    assert env.version() == "3" and env.up.state["blocked"] is None


def test_crash_after_start_is_detected(env):
    """Startet kurz fehlerfrei, stürzt dann ab: die Stabilitätsprüfung muss das fangen."""
    env.cfg.stable_s = 4.0
    started_ok, _ = env.up.run("start")
    assert started_ok
    good = env.head()
    env.push("crasht", CRASH="1")
    ok, msg = env.up.run("update")
    assert not ok and "wiederhergestellt" in msg
    assert env.head() == good and env.version() == "1"


def test_dirty_tree_refused_and_nothing_stopped(env):
    started(env)
    (env.app / "VERSION").write_text("lokal geändert\n")
    env.push("v2", VERSION="2\n")
    ok, msg = env.up.run("update")
    assert not ok and "lokale Änderungen" in msg
    assert env.up.status()["app"] == "running"          # wurde nicht angehalten


def test_untracked_files_do_not_block(env):
    started(env)
    (env.app / "notizen.txt").write_text("egal")
    env.push("v2", VERSION="2\n")
    ok, msg = env.up.run("update")
    assert ok, msg


def test_requirements_change_runs_install(env):
    started(env)
    env.push("neue dep", **{"requirements.txt": "# neu\n"})
    ok, msg = env.up.run("update")
    assert ok, msg
    assert (env.app / "INSTALLED").exists()


def test_install_failure_rolls_back(env):
    started(env)
    env.cfg.install_command = "false"
    good = env.head()
    env.push("neue dep", **{"requirements.txt": "# neu\n"})
    ok, msg = env.up.run("update")
    assert not ok and "Abhängigkeiten" in msg
    assert env.head() == good and env.version() == "1"


def test_diverged_history_refused(env):
    started(env)
    (env.app / "lokal.txt").write_text("x")
    git(env.app, "add", "lokal.txt")
    git(env.app, "commit", "-q", "-m", "lokal")
    env.push("v2", VERSION="2\n")
    ok, msg = env.up.run("update")
    assert not ok and "weicht ab" in msg
    assert env.up.status()["app"] == "running"


def test_wrong_branch_refused(env):
    git(env.app, "checkout", "-q", "-b", "andere")
    ok, msg = env.up.run("check")
    assert not ok and "Branch" in msg


def test_offline_fetch_leaves_app_running(env):
    started(env)
    git(env.app, "remote", "set-url", "origin", str(env.tmp / "gibt-es-nicht.git"))
    ok, msg = env.up.run("update")
    assert not ok and "nicht erreichbar" in msg
    assert env.up.status()["app"] == "running"


def test_manual_rollback(env):
    started(env)
    good = env.head()
    env.push("v2 (läuft, aber unerwünscht)", VERSION="2\n")
    assert env.up.run("update")[0]
    assert env.version() == "2"
    # last_good ist jetzt v2; wir simulieren, dass der Nutzer auf v1 zurück will
    env.up._set_state(last_good=good)
    ok, msg = env.up.run("rollback")
    assert ok, msg
    assert env.head() == good and env.version() == "1"
    assert env.up.state["blocked"] == git(env.seed, "rev-parse", "HEAD")   # nicht sofort wieder einspielen


def test_rollback_without_last_good(env):
    ok, msg = env.up.run("rollback")
    assert not ok and "noch kein" in msg


def test_force_retries_blocked(env):
    started(env)
    env.push("kaputt", BROKEN="1")
    env.up.run("update")
    ok, msg = env.up.run("update", force=True)
    assert not ok and "wiederhergestellt" in msg         # versucht, scheitert erneut, rollt zurück
    assert env.version() == "1"


def test_state_persists(env):
    started(env)
    from watchdog.updater import Updater
    again = Updater(env.cfg, env.mgr)
    assert again.state["last_good"] == env.head()


def test_concurrent_job_rejected(env):
    assert env.up._begin("check")
    try:
        assert env.up.start("restart") is False
        ok, msg = env.up.run("check")
        assert not ok and "bereits" in msg
    finally:
        env.up._release()
        env.up.job["running"] = False


def test_file_lock_blocks_second_process(env):
    from watchdog.updater import Updater
    other = Updater(env.cfg, env.mgr)
    assert env.up._begin("check")
    try:
        assert other._begin("check") is False
    finally:
        env.up._release()
        env.up.job["running"] = False
    assert other._begin("check")
    other._release()


def test_async_start_and_status(env):
    assert env.up.start("start")
    for _ in range(100):
        if not env.up.status()["job"]["running"]:
            break
        time.sleep(0.2)
    s = env.up.status()
    assert s["job"]["ok"] is True and s["app"] == "running"
    assert json.dumps(s)                                  # serialisierbar


def test_startup_adopts_running_app(env):
    started(env)
    from watchdog.updater import Updater
    from watchdog.managers import ProcessManager
    mgr2 = ProcessManager(env.cfg.start_command, env.cfg.repo_dir, env.cfg.data_dir)   # "adoptiert" per PID-Datei
    env.cfg.data_dir  # gleiche Daten
    up2 = Updater(env.cfg, mgr2)
    up2.state["last_good"] = None
    up2.startup()
    assert up2.state["last_good"] == env.head()
    import threading
    threading.Thread(target=env.mgr._proc.wait, daemon=True).start()   # im Betrieb räumt init auf
    mgr2.stop(5)
    assert not mgr2.is_active()


def test_job_log_survives_restart_and_broken_db_does_not_matter(env):
    from watchdog.updater import Updater
    env.up.run("check")
    again = Updater(env.cfg, env.mgr)               # neuer Prozess nach Neustart des Geräts
    job = again.status()["job"]
    assert job["name"] == "check" and job["running"] is False and job["ok"] is True and job["log"]
    # Datenbank mutwillig zerstören: der nächste Lauf muss trotzdem klappen
    again.rec.store._db.close()
    ok, msg = again.run("check")
    assert ok


def test_stuck_job_is_released_after_timeout(env):
    env.up.job.update(running=True, name="update", started=time.time() - 4000)   # „hängt“ seit über einer Stunde
    ok, msg = env.up.run("check")
    assert ok                                         # statt „Es läuft bereits eine Aktion“


def test_recent_running_job_still_blocks(env):
    env.up.job.update(running=True, name="update", started=time.time())
    assert env.up.run("check") == (False, "Es läuft bereits eine Aktion")
    env.up.job["running"] = False


def test_lock_is_released_even_if_operation_raises_baseexception(env, monkeypatch):
    def boom(**kw):
        raise SystemExit(5)
    monkeypatch.setattr(env.up, "op_check", boom)
    with pytest.raises(SystemExit):
        env.up.run("check")
    assert env.up.job["running"] is False and env.up._lockfh is None
    monkeypatch.undo()
    assert env.up.run("check")[0]


def test_status_isolates_failing_sections(env, monkeypatch):
    monkeypatch.setattr(env.up, "_active", lambda: (_ for _ in ()).throw(RuntimeError("systemctl hängt")))
    st = env.up.status()
    assert st["app"] == "unknown" and any("systemctl hängt" in p for p in st["problems"])
    assert "manager" in st and "job" in st
