"""Systemprüfung, Netzprotokoll und Datenbanksicherung des Watchdogs."""
import json
import sqlite3
import time

from watchdog import sysmon
from watchdog.backup import Backup
from watchdog.health import StatusCache
from tests.test_resilience import FakeUpdater


def watch(tmp_path, clock, **probes):
    base = {"disk": lambda p: {"path": p, "total_mb": 1000, "free_mb": 800, "used_pct": 20.0},
            "throttled": lambda: None, "temp": lambda: 45.0, "time": lambda: True,
            "internet": lambda: True, "tailscale": lambda: None, "boot": lambda: None}
    return sysmon.SysWatch(str(tmp_path), "", "", clock=clock, probes={**base, **probes})


def test_parse_throttled_bits():
    assert sysmon.parse_throttled("throttled=0x0")["undervolt_ever"] is False
    p = sysmon.parse_throttled("throttled=0x50005")
    assert p["undervolt_now"] and p["throttled_now"] and p["undervolt_ever"] and p["throttled_ever"]
    assert sysmon.parse_throttled("quatsch") is None and sysmon.parse_throttled(None) is None


def test_problems_for_disk_power_temp_time(tmp_path):
    w = watch(tmp_path, lambda: 1000.0,
              disk=lambda p: {"path": p, "total_mb": 1000, "free_mb": 50, "used_pct": 95.0},
              throttled=lambda: sysmon.parse_throttled("throttled=0x1"), temp=lambda: 85.0, time=lambda: False)
    w.refresh()
    text = " | ".join(w.view()["problems"])
    for part in ("Speicher fast voll", "Unterspannung", "heiß", "Uhrzeit"):
        assert part in text


def test_each_probe_failure_is_isolated(tmp_path):
    def boom(*a):
        raise RuntimeError("kaputt")
    w = watch(tmp_path, lambda: 1.0, disk=boom, temp=boom, internet=boom, tailscale=boom, time=boom, throttled=boom)
    w.refresh()                                         # darf nicht werfen
    v = w.view()
    assert v["disk"] is None and v["temp_c"] is None and v["internet"]["up"] is False


def test_network_outage_is_recorded_and_survives_restart(tmp_path):
    now = [1000.0]
    up = [True]
    w = watch(tmp_path, lambda: now[0], internet=lambda: up[0])
    w.refresh()
    up[0] = False
    now[0] += 100
    w.refresh()
    up[0] = True
    now[0] += 300
    w.refresh()
    assert w.view()["internet"]["outages"] == [{"start": 1100.0, "end": 1400.0}]
    again = watch(tmp_path, lambda: now[0])             # „Neustart“: Protokoll wird aus der Datei geladen
    assert again.netlog.outages() == [{"start": 1100.0, "end": 1400.0}]


def test_internet_problem_only_when_expected_and_after_grace(tmp_path):
    now = [0.0]
    w = watch(tmp_path, lambda: now[0], internet=lambda: False)
    w.expect_internet = True
    w.refresh()
    assert not w.view()["problems"]
    now[0] += 120
    w.refresh()
    assert any("Kein Internet" in p for p in w.view()["problems"])
    w.expect_internet = False
    w.refresh()
    assert not w.view()["problems"]


def test_undervolt_ever_message_includes_last_recorded_time(tmp_path):
    now = [1000.0]
    states = [sysmon.parse_throttled("throttled=0x10001"), sysmon.parse_throttled("throttled=0x10000")]
    w = watch(tmp_path, lambda: now[0], throttled=lambda: states.pop(0))
    w.refresh()                                          # jetzt gerade Unterspannung
    now[0] += 30
    w.refresh()                                          # vorbei, aber „ever“ bleibt gesetzt
    text = " | ".join(w.view()["problems"])
    assert "zuletzt aufgezeichnet" in text and "vor Beginn" not in text


def test_undervolt_ever_before_recording_says_so(tmp_path):
    w = watch(tmp_path, lambda: 1.0, throttled=lambda: sysmon.parse_throttled("throttled=0x10000"))
    w.refresh()                                          # „ever“ von Anfang an gesetzt, aber nie selbst aufgezeichnet
    assert "vor Beginn der Aufzeichnung" in " | ".join(w.view()["problems"])


def test_metrics_recorded_and_survive_restart(tmp_path):
    now = [1000.0]
    w = watch(tmp_path, lambda: now[0], temp=lambda: 40.0, throttled=lambda: sysmon.parse_throttled("throttled=0x0"))
    w.refresh()
    now[0] += 30
    w.refresh()
    hist = w.history()
    assert len(hist) == 2
    assert hist[-1]["temp_c"] == 40.0 and hist[-1]["undervolt_now"] is False
    again = watch(tmp_path, lambda: now[0])             # „Neustart“: Protokoll wird aus der Datei geladen
    assert len(again.history()) == 2


def test_boot_time_reads_proc_uptime(tmp_path):
    f = tmp_path / "uptime"
    f.write_text("123.45 456.78\n")
    before = time.time() - 123.45
    bt = sysmon.boot_time(str(f))
    assert bt is not None and abs(bt - before) < 1.0
    assert sysmon.boot_time(str(tmp_path / "gibtsnicht")) is None


def test_downsample_keeps_temperature_spikes_and_any_undervolt():
    events = [{"ts": i, "temp_c": 40.0, "undervolt_now": False} for i in range(10)]
    events[7] = {"ts": 7, "temp_c": 90.0, "undervolt_now": True}        # kurzer Ausschlag mitten in einem Bucket
    out = sysmon.downsample(events, 2)
    assert len(out) == 2
    assert any(e["temp_c"] == 90.0 and e["undervolt_now"] for e in out)    # geht beim Verdichten nicht verloren
    assert sysmon.downsample(events, 20) == events                        # weniger Punkte gefordert als vorhanden: unverändert


def test_metricslog_prune_before_drops_entries_from_an_earlier_boot(tmp_path):
    f = tmp_path / "metrics.jsonl"
    m = sysmon.MetricsLog(f, limit=100)
    for ts in (100, 200, 300, 400):
        m.add({"ts": ts})
    m.prune_before(250)
    assert [e["ts"] for e in m.events] == [300, 400]
    reloaded = sysmon.MetricsLog(f, limit=100)                # Datei wurde beim Kürzen neu geschrieben
    assert [e["ts"] for e in reloaded.events] == [300, 400]


def test_syswatch_drops_samples_from_before_this_boot(tmp_path):
    now = [1000.0]
    w = watch(tmp_path, lambda: now[0], boot=lambda: 1000.0)
    w.refresh()
    now[0] += 30
    w.refresh()
    assert len(w.history()) == 2
    # „Neustart“ des Rechners: ein neuer Boot-Zeitpunkt nach den alten Messwerten verwirft diese beim Laden.
    restarted = watch(tmp_path, lambda: now[0] + 60, boot=lambda: now[0] + 60)
    assert restarted.history() == []


def test_history_reports_the_full_uptime_downsampled(tmp_path):
    now = [0.0]
    w = watch(tmp_path, lambda: now[0], boot=lambda: 0.0)
    for _ in range(50):
        w.refresh()
        now[0] += 30
    assert len(w.metricslog.events) == 50
    hist = w.history(max_points=10)
    assert len(hist) == 10
    assert hist[0]["ts"] < hist[-1]["ts"]                       # deckt weiterhin die ganze Zeitspanne ab


def test_metricslog_ignores_garbage_and_caps(tmp_path):
    f = tmp_path / "metrics.jsonl"
    f.write_text('kaputt\n{"ts": 5, "temp_c": 10}\n[1,2]\n')
    m = sysmon.MetricsLog(f, limit=4)
    assert len(m.events) == 1
    for i in range(6):
        m.add({"ts": i})
    assert len(m.events) <= 4
    bad = sysmon.MetricsLog(tmp_path / "datei" / "x")
    (tmp_path / "datei").write_text("ich bin eine Datei")   # Elternpfad ist eine Datei → Schreiben schlägt fehl
    bad.add({"ts": 1.0})                                     # darf trotzdem nicht werfen
    assert bad.recent() == [{"ts": 1.0}]


def test_netlog_ignores_garbage_and_unwritable(tmp_path):
    f = tmp_path / "netlog.jsonl"
    f.write_text('kaputt\n{"ts": 5, "up": false}\n[1,2]\n')
    n = sysmon.NetLog(f)
    assert len(n.events) == 1
    bad = sysmon.NetLog(tmp_path / "gibtsnicht" / "x" / "log")      # anlegbar; darf trotzdem nie werfen
    bad.path = tmp_path / "datei" / "x"
    (tmp_path / "datei").write_text("ich bin eine Datei")           # Elternpfad ist eine Datei → Schreiben schlägt fehl
    bad.add(1.0, True)
    assert bad.last_up() is True


# ---- Sicherung -------------------------------------------------------------------------
def make_db(path):
    d = sqlite3.connect(path)
    d.execute("CREATE TABLE t(x)")
    d.execute("INSERT INTO t VALUES (42)")
    d.commit()
    d.close()


def test_backup_copies_consistently_and_prunes(tmp_path):
    src = tmp_path / "app.db"
    make_db(src)
    t = [1_700_000_000.0]
    b = Backup(str(src), str(tmp_path / "bk"), keep=2, clock=lambda: t[0])
    for _ in range(4):
        t[0] += 86400
        assert b.run()
    files = sorted((tmp_path / "bk").glob("meshcore-*.db"))
    assert len(files) == 2
    assert sqlite3.connect(files[-1]).execute("SELECT x FROM t").fetchone() == (42,)
    assert b.problem() is None


def test_backup_due_logic_and_staleness(tmp_path):
    src = tmp_path / "app.db"
    make_db(src)
    t = [1_700_000_000.0]
    b = Backup(str(src), str(tmp_path / "bk"), clock=lambda: t[0])
    assert b.run_if_due() is True
    t[0] += 3600
    assert b.run_if_due() is False                      # noch nicht fällig
    t[0] += 3 * 86400
    assert "über 2 Tage" in b.problem()


def test_backup_second_target_missing_is_only_a_note(tmp_path):
    src = tmp_path / "app.db"
    make_db(src)
    b = Backup(str(src), str(tmp_path / "bk"), extra_dir=str(tmp_path / "kein-stick"))
    assert b.run() and b.problem() is None
    assert any("nicht vorhanden" in n for n in b.notes)
    (tmp_path / "stick").mkdir()
    b2 = Backup(str(src), str(tmp_path / "bk2"), extra_dir=str(tmp_path / "stick"))
    assert b2.run() and list((tmp_path / "stick").glob("meshcore-*.db"))


def test_backup_never_raises_on_broken_source_or_target(tmp_path):
    bad = tmp_path / "kaputt.db"
    bad.write_bytes(b"das ist keine datenbank" * 50)
    b = Backup(str(bad), str(tmp_path / "bk"))
    assert b.run() is False and b.last_error and "fehlgeschlagen" in b.problem()
    assert Backup("", str(tmp_path / "bk3")).run() is False          # keine Quelle konfiguriert
    missing = Backup(str(tmp_path / "nix.db"), str(tmp_path / "bk4"))
    assert missing.run() is False and missing.problem() is None      # noch keine Datenbank: kein Alarm


def test_backup_does_not_modify_source(tmp_path):
    src = tmp_path / "app.db"
    make_db(src)
    before = src.read_bytes()
    Backup(str(src), str(tmp_path / "bk")).run()
    assert src.read_bytes() == before


# ---- Einbindung -------------------------------------------------------------------------
def test_status_includes_system_and_backup_problems(tmp_path):
    w = watch(tmp_path, lambda: 1.0, time=lambda: False)
    w.refresh()
    c = StatusCache(FakeUpdater(), sys_watch=w, backup=Backup("", str(tmp_path / "bk")))
    c.refresh()
    snap = c.get()
    assert snap["system"]["time_synced"] is False and "backup" in snap
    assert any("Uhrzeit" in p for p in snap["problems"])


def test_broken_sys_watch_does_not_break_status(tmp_path):
    class Broken:
        def view(self):
            raise RuntimeError("kaputt")
        def start(self): raise RuntimeError("kein Start")
        def stop(self): raise RuntimeError("kein Stopp")
    c = StatusCache(FakeUpdater(), sys_watch=Broken(), backup=Broken())
    c.start()
    c.refresh()
    assert c.get()["app"]
    c.stop()


def test_helper_commands_do_not_inherit_systemd_notify_socket(monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
    out = sysmon.run_cmd(["env"])
    assert out is not None and "NOTIFY_SOCKET" not in out
