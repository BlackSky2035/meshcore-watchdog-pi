import subprocess
import sys

from watchdog import demo
from watchdog.demo import DemoUpdater


def make(monkeypatch):
    monkeypatch.setattr(demo, "STEP_S", 0)
    return DemoUpdater()


def test_full_scenario(monkeypatch):
    d = make(monkeypatch)
    ok, msg = d.run("check")
    assert ok and "2 neue" in msg
    ok, msg = d.run("update")
    assert ok and d.status()["last_good"] == demo.REMOTE[1][0]
    ok, msg = d.run("check")                       # jetzt taucht der fehlerhafte Commit auf
    assert ok and "1 neue" in msg
    ok, msg = d.run("update")
    assert not ok and "zurückgesetzt" in msg
    st = d.status()
    assert st["app"] == "running" and st["blocked"] == demo.REMOTE[2][0]
    assert st["commit"] == demo.REMOTE[1][0]       # zurück auf den guten Stand


def test_stop_start_restart_rollback(monkeypatch):
    d = make(monkeypatch)
    d.run("stop")
    assert d.status()["app"] == "stopped"
    d.run("start")
    assert d.status()["app"] == "running"
    assert d.run("restart")[0]
    assert d.run("rollback") == (False, "Schon auf dem letzten guten Stand")


def test_blocked_needs_force(monkeypatch):
    d = make(monkeypatch)
    d.run("update")
    d.run("update")
    ok, msg = d.run("update")
    assert not ok and "fehlerhaft" in msg
    ok, msg = d.run("update", force=True)
    assert not ok and "zurückgesetzt" in msg


def test_only_one_job_at_a_time(monkeypatch):
    monkeypatch.setattr(demo, "STEP_S", 0.2)
    d = DemoUpdater()
    assert d.start("check") is True
    assert d.start("check") is False


def test_demo_runs_without_fcntl():
    """Unter Windows gibt es fcntl nicht; der Demo-Modus darf es nicht importieren."""
    code = ("import sys; sys.modules['fcntl'] = None\n"
            "import watchdog.__main__, watchdog.demo, watchdog.server\n"
            "from watchdog.demo import DemoUpdater; print(DemoUpdater().status()['app'])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and out.stdout.strip() == "running", out.stderr
