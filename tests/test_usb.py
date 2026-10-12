"""USB-Anzeige des Watchdogs: Erkennung, Karenzzeit, Fehlerfestigkeit."""
import os

from watchdog import usb
from watchdog.health import StatusCache
from tests.test_resilience import FakeUpdater


def test_scan_finds_devices_and_dedups_by_id(tmp_path):
    real = tmp_path / "ttyACM0"
    real.write_text("")
    byid = tmp_path / "usb-Heltec_V4-if00"
    os.symlink(real, byid)

    def globber(pattern):
        return {"/dev/serial/by-id/*": [str(byid)], "/dev/ttyACM*": [str(real)]}.get(pattern, [])
    r = usb.scan(True, globber=globber)
    assert len(r["devices"]) == 1                                   # dasselbe Gerät unter zwei Namen
    assert r["devices"][0]["name"] == "usb-Heltec_V4-if00"


def test_scan_never_raises():
    def boom(_):
        raise OSError("kaputt")
    r = usb.scan(True, globber=boom)
    assert r["devices"] == [] and "nicht möglich" in r["notes"][0]


def test_problem_only_after_grace_and_only_when_expected():
    now = [1000.0]
    w = usb.UsbWatch(True, scanner=lambda e: {"devices": [], "expect": e, "notes": [], "checked": now[0]}, clock=lambda: now[0])
    w.refresh()
    assert w.problem() is None                                      # eben erst verschwunden: kurz Ruhe
    now[0] += usb.GRACE_S + 1
    assert "Kein USB-Node" in w.problem()
    quiet = usb.UsbWatch(False, scanner=lambda e: {"devices": [], "expect": e, "notes": [], "checked": 0}, clock=lambda: now[0])
    quiet.refresh()
    now[0] += 999
    assert quiet.problem() is None                                  # nicht erwartet: nur Anzeige


def test_recovery_clears_problem():
    now = [0.0]
    present = [False]
    w = usb.UsbWatch(True, scanner=lambda e: {"devices": [{"path": "/dev/x"}] if present[0] else [], "expect": e,
                                              "notes": [], "checked": 0}, clock=lambda: now[0])
    w.refresh()
    now[0] += 100
    assert w.problem()
    present[0] = True
    w.refresh()
    assert w.problem() is None and w.missing_since is None


def test_broken_scanner_does_not_break_watchdog():
    def boom(_):
        raise RuntimeError("scanner kaputt")
    c = StatusCache(FakeUpdater(), usb_watch=usb.UsbWatch(True, scanner=boom))
    c.refresh()
    snap = c.get()
    assert snap["app"] and "problems" in snap                       # Status kommt trotzdem


def test_usb_problem_appears_in_status_problems():
    now = [0.0]
    w = usb.UsbWatch(True, scanner=lambda e: {"devices": [], "expect": e, "notes": [], "checked": 0}, clock=lambda: now[0])
    c = StatusCache(FakeUpdater(), usb_watch=w)
    c.refresh()
    now[0] += 100
    snap = c.get()
    assert any("USB-Node" in p for p in snap["problems"]) and snap["usb"]["devices"] == []
