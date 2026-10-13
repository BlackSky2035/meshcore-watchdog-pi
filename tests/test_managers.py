import os
import stat
import sys

import pytest

from watchdog.managers import ManagerError, ProcessManager, SystemdManager, make_manager
from watchdog.config import Config


def test_process_manager_lifecycle(tmp_path):
    m = ProcessManager(f"{sys.executable} -c \"import time; time.sleep(60)\"", str(tmp_path), str(tmp_path / "d"))
    assert not m.is_active()
    m.start()
    assert m.is_active()
    m.start()                                              # doppelt starten ist harmlos
    m.stop(5)
    assert not m.is_active()


def test_process_manager_kills_stubborn_process(tmp_path):
    code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    m = ProcessManager(f"{sys.executable} -c \"{code}\"", str(tmp_path), str(tmp_path / "d"))
    m.start()
    import time; time.sleep(0.5)
    m.stop(1)
    assert not m.is_active()


def test_process_manager_adopts_via_pidfile(tmp_path):
    cmd = f"{sys.executable} -c \"import time; time.sleep(60)\""
    a = ProcessManager(cmd, str(tmp_path), str(tmp_path / "d"))
    a.start()
    b = ProcessManager(cmd, str(tmp_path), str(tmp_path / "d"))
    assert b.is_active()
    # Im Betrieb übernimmt init die Rolle des Eltern-Prozesses und räumt auf; im Test macht das ein Thread.
    import threading
    threading.Thread(target=a._proc.wait, daemon=True).start()
    b.stop(5)
    assert not b.is_active()


def test_process_manager_bad_command(tmp_path):
    m = ProcessManager("/gibt/es/nicht", str(tmp_path), str(tmp_path / "d"))
    with pytest.raises(ManagerError):
        m.start()


def test_process_exit_detected_and_tail(tmp_path):
    m = ProcessManager(f"{sys.executable} -c \"print('hallo'); raise SystemExit(2)\"", str(tmp_path), str(tmp_path / "d"))
    m.start()
    import time; time.sleep(0.8)
    assert not m.is_active()
    assert "hallo" in m.tail()[-1]


@pytest.fixture
def fake_systemctl(tmp_path):
    state = tmp_path / "state"
    script = tmp_path / "systemctl"
    script.write_text(f"""#!/bin/sh
case "$1" in
  is-active) if [ -f {state} ]; then echo active; exit 0; else echo inactive; exit 3; fi ;;
  start) [ -f {tmp_path}/fail ] && {{ echo "boom" >&2; exit 1; }}; touch {state} ;;
  stop) rm -f {state} ;;
esac
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_systemd_manager_with_fake_systemctl(fake_systemctl, tmp_path):
    m = SystemdManager("meshcore-remote", str(fake_systemctl))
    assert not m.is_active()
    m.start()
    assert m.is_active()
    m.stop(5)
    assert not m.is_active()
    (tmp_path / "fail").write_text("")
    with pytest.raises(ManagerError, match="boom"):
        m.start()


def test_systemd_manager_tail_via_fake_journalctl(tmp_path, monkeypatch):
    script = tmp_path / "journalctl"
    script.write_text("#!/bin/sh\necho 'line one'\necho 'line two'\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    m = SystemdManager("meshcore-remote")
    assert m.tail(10) == ["line one", "line two"]


def test_systemd_manager_tail_passes_since_until_to_journalctl(tmp_path, monkeypatch):
    script = tmp_path / "journalctl"
    script.write_text('#!/bin/sh\necho "$@" > ' + str(tmp_path / "args.txt") + '\necho "eine Zeile"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    m = SystemdManager("meshcore-remote")
    assert m.tail(10, since="2025-01-01 12:00", until="2025-01-01 13:00") == ["eine Zeile"]
    call = (tmp_path / "args.txt").read_text()
    assert "--since 2025-01-01 12:00" in call and "--until 2025-01-01 13:00" in call


def test_systemd_manager_tail_missing_journalctl_is_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))       # journalctl gibt es hier nicht
    m = SystemdManager("meshcore-remote")
    assert m.tail() == []


def test_systemd_manager_tail_failure_is_empty_not_raising(tmp_path, monkeypatch):
    script = tmp_path / "journalctl"
    script.write_text("#!/bin/sh\necho 'kein Zugriff' >&2\nexit 1\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(tmp_path))
    m = SystemdManager("meshcore-remote")
    assert m.tail() == []


def test_systemd_missing_binary():
    with pytest.raises(ManagerError):
        SystemdManager("x", "/gibt/es/nicht").is_active()


def test_make_manager_explicit(tmp_path):
    assert make_manager(Config(manager="process", repo_dir=str(tmp_path), data_dir=str(tmp_path / "d"))).name == "process"
    assert make_manager(Config(manager="systemd")).name == "systemd"
    with pytest.raises(ManagerError):
        make_manager(Config(manager="quatsch"))


def test_systemd_stop_waits_longer_than_the_unit_stop_timeout(tmp_path):
    """systemctl stop darf nicht vor dem Ablauf von TimeoutStopSec abgebrochen werden (sonst: Fehler, aber Dienst wird später doch beendet)."""
    script = tmp_path / "systemctl"
    script.write_text("#!/bin/sh\n[ \"$1\" = stop ] && sleep 1.5\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    m = SystemdManager("meshcore-remote", str(script), timeout=1)
    m.stop(5)                                            # 1,5 s Stoppzeit < 5 s erlaubte Stoppzeit: kein Fehler


def test_systemd_tail_has_short_timeout_and_keeps_partial_output(tmp_path, monkeypatch):
    import time
    script = tmp_path / "journalctl"
    script.write_text("#!/bin/sh\necho 'erste Zeile'\nsleep 20\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    m = SystemdManager("meshcore-remote")
    t0 = time.monotonic()
    lines = m.tail(10, since="1 day ago")
    assert time.monotonic() - t0 < 8
    assert lines and lines[0] == "erste Zeile"


def _sleeper():
    import subprocess
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)


def test_process_manager_ignores_reused_pid_from_old_pidfile(tmp_path):
    """Nach einem Neustart des Rechners kann die PID aus app.pid zu einem fremden Prozess gehören: nie als App behandeln, nie beenden."""
    foreign = _sleeper()
    try:
        d = tmp_path / "d"
        d.mkdir()
        (d / "app.pid").write_text(f"{foreign.pid} 1")              # Startzeit passt nicht
        m = ProcessManager(f"{sys.executable} -m app.main", str(tmp_path), str(d))
        assert not m.is_active()
        m.stop(1)
        assert foreign.poll() is None                               # fremder Prozess lebt noch
        (d / "app.pid").write_text(f"{foreign.pid}")                # altes Format ohne Startzeit, fremde Kommandozeile
        m2 = ProcessManager(f"{sys.executable} -m app.main", str(tmp_path), str(d))
        assert not m2.is_active()
        m2.stop(1)
        assert foreign.poll() is None
    finally:
        foreign.kill()
        foreign.wait()
