"""Netzausfälle bei springender Uhr: Der Pi hat keine Echtzeituhr und stellt seine Zeit nach dem Start per Netz neu.

Beispiel: Ein Ausfall mit Beginn 12:00:00 und Ende 11:58:00, also das Ende 120 s VOR dem Beginn (die Uhr wurde nach dem Start per Netz neu gestellt).
"""
import json

from watchdog import sysmon
from watchdog.sysmon import NetLog

BOOT_A, BOOT_B = "boot-a", "boot-b"


def log_of(tmp_path, events):
    f = tmp_path / "netlog.jsonl"
    f.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return NetLog(f)


def test_clock_jump_end_before_start_is_flagged(tmp_path):
    n = log_of(tmp_path, [
        {"ts": 1_000_000.0, "up": True},
        {"ts": 1_054_000.0, "up": False},
        {"ts": 1_054_168.0, "up": True},
        {"ts": 1_085_000.0, "up": False},
        {"ts": 1_084_880.0, "up": True},          # 120 s früher als der Vorgänger
    ])
    out = n.outages()
    assert out[0] == {"start": 1_054_000.0, "end": 1_054_168.0}                       # normaler Ausfall bleibt, wie er war
    assert out[1] == {"start": None, "end": 1_084_880.0, "duration_s": None, "clock_jump": True}
    assert all(o["start"] is None or o["end"] is None or o["end"] >= o["start"] for o in out)       # nie ein Ende vor dem Beginn


def test_normal_outage_keeps_old_shape(tmp_path):
    n = log_of(tmp_path, [{"ts": 100.0, "up": False, "mono": 50.0, "boot": BOOT_A}, {"ts": 400.0, "up": True, "mono": 350.0, "boot": BOOT_A}])
    assert n.outages() == [{"start": 100.0, "end": 400.0}]                                           # Wanduhr und Laufzeit stimmen überein


def test_clock_step_within_one_boot_uses_uptime_for_duration(tmp_path):
    # Wanduhr springt um 3 Minuten zurück, in Wirklichkeit vergingen 40 s
    n = log_of(tmp_path, [{"ts": 5000.0, "up": False, "mono": 100.0, "boot": BOOT_A},
                          {"ts": 4860.0, "up": True, "mono": 140.0, "boot": BOOT_A}])
    o = n.outages()[0]
    assert o["clock_jump"] is True and o["duration_s"] == 40.0
    assert o["end"] == 4860.0 and o["start"] == 4820.0                                               # Beginn aus der (korrigierten) Endzeit abgeleitet


def test_forward_clock_step_is_also_detected(tmp_path):
    # Wanduhr springt um 1 h VORWÄRTS (NTP nach dem Start), die Laufzeit zeigt: 20 s
    n = log_of(tmp_path, [{"ts": 1000.0, "up": False, "mono": 10.0, "boot": BOOT_A},
                          {"ts": 4620.0, "up": True, "mono": 30.0, "boot": BOOT_A}])
    o = n.outages()[0]
    assert o["clock_jump"] is True and o["duration_s"] == 20.0


def test_small_differences_are_not_clock_jumps(tmp_path):
    n = log_of(tmp_path, [{"ts": 100.0, "up": False, "mono": 10.0, "boot": BOOT_A}, {"ts": 130.0, "up": True, "mono": 36.0, "boot": BOOT_A}])
    assert "clock_jump" not in n.outages()[0]                                                       # 4 s Abweichung: Messrauschen


def test_across_reboot_positive_wall_time_is_trusted(tmp_path):
    n = log_of(tmp_path, [{"ts": 100.0, "up": False, "mono": 900.0, "boot": BOOT_A}, {"ts": 400.0, "up": True, "mono": 5.0, "boot": BOOT_B}])
    assert n.outages() == [{"start": 100.0, "end": 400.0}]                                           # Laufzeiten verschiedener Starts nie vergleichen


def test_across_reboot_negative_wall_time_is_flagged_without_duration(tmp_path):
    n = log_of(tmp_path, [{"ts": 900.0, "up": False, "mono": 900.0, "boot": BOOT_A}, {"ts": 400.0, "up": True, "mono": 5.0, "boot": BOOT_B}])
    o = n.outages()[0]
    assert o["clock_jump"] is True and o["duration_s"] is None and o["start"] is None


def test_garbage_mono_values_do_not_break(tmp_path):
    n = log_of(tmp_path, [{"ts": 100.0, "up": False, "mono": "kaputt", "boot": BOOT_A}, {"ts": 130.0, "up": True, "mono": None, "boot": BOOT_A}])
    assert n.outages() == [{"start": 100.0, "end": 130.0}]


def test_open_outage_unchanged(tmp_path):
    n = log_of(tmp_path, [{"ts": 100.0, "up": False, "mono": 1.0, "boot": BOOT_A}])
    assert n.outages() == [{"start": 100.0, "end": None}]


def test_add_stores_mono_and_boot_and_survives_restart(tmp_path):
    f = tmp_path / "netlog.jsonl"
    n = NetLog(f)
    n.add(100.0, False, mono=50.0, boot=BOOT_A)
    n.add(90.0, True, mono=70.0, boot=BOOT_A)             # Uhr zurückgesprungen
    again = NetLog(f)
    assert again.events[0]["mono"] == 50.0 and again.events[0]["boot"] == BOOT_A
    assert again.outages()[0]["duration_s"] == 20.0


def test_syswatch_records_uptime_and_boot_id(tmp_path):
    t = [1000.0]
    up = [True]
    base = {"disk": lambda p: None, "throttled": lambda: None, "temp": lambda: 40.0, "time": lambda: True,
            "internet": lambda: up[0], "tailscale": lambda: None, "boot": lambda: None}
    w = sysmon.SysWatch(str(tmp_path), "", "", clock=lambda: t[0], probes=base)
    w.refresh()
    t[0] += 30
    up[0] = False
    w.refresh()
    t[0] += 120
    up[0] = True
    w.refresh()
    assert all("mono" in e for e in w.netlog.events)
    assert w.view()["internet"]["outages"] == [{"start": 1030.0, "end": 1150.0}]                      # Testuhr = Laufzeit: kein falscher Sprung


def test_broken_timestamps_never_raise(tmp_path):
    n = log_of(tmp_path, [{"ts": "kaputt", "up": False}, {"ts": 5.0, "up": True}, {"ts": 9.0, "up": False}, {"ts": None, "up": True}])
    out = n.outages()                                    # darf nicht werfen
    assert len(out) == 2
