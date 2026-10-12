"""Wartungsmodus im Watchdog: Zustand, Weitergabe im Hintergrund, USB-Warnung schweigt, Server-Endpunkte."""
import json
import threading
import time

import pytest

from watchdog import usb
from watchdog.health import StatusCache
from watchdog.maintenance import Maintenance, app_base
from tests.test_resilience import FakeUpdater
from tests.test_server import call


class Resp:
    def __init__(self, status=200): self.status = status
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_app_base():
    assert app_base("", "http://127.0.0.1:8000/api/version") == "http://127.0.0.1:8000"
    assert app_base("https://x.ts.net/", "http://127.0.0.1:8000/api/version") == "https://x.ts.net"
    assert app_base("", "") == ""


def test_start_end_limits_and_persistence(tmp_path):
    now = [1000.0]
    m = Maintenance(str(tmp_path), clock=lambda: now[0])
    assert m.start(99999)["remaining_s"] == 1440 * 60 and m.active()
    assert Maintenance(str(tmp_path), clock=lambda: now[0]).active()               # überlebt Neustart
    now[0] += 1440 * 60 + 1
    assert not m.active() and m.view()["until"] is None                            # läuft von selbst ab
    m.start(10); m.end()
    assert not Maintenance(str(tmp_path), clock=lambda: now[0]).active()


def test_sync_to_app_success_retry_and_failure(tmp_path):
    calls = []

    def opener(req, timeout):
        calls.append((req.method, req.full_url, req.headers.get("X-control-token"), req.data))
        if len(calls) < 2:
            raise OSError("noch nicht da")
        return Resp(200)
    m = Maintenance(str(tmp_path), "http://app", "ctl", opener=opener, retry_s=0)
    m.start(30); m.join()
    assert m.view()["app_synced"] is True and calls[-1][0] == "POST" and calls[-1][2] == "ctl" and json.loads(calls[-1][3]) == {"minutes": 30}
    m.end(); m.join()
    assert calls[-1][0] == "DELETE"
    bad = Maintenance(str(tmp_path), "http://app", "ctl", opener=lambda r, timeout: (_ for _ in ()).throw(OSError("aus")), retry_s=0)
    bad.start(30); bad.join()
    assert bad.view()["app_synced"] is False and "nicht erreicht" in bad.view()["app_error"] and bad.active()      # lokal gilt die Wartung trotzdem


def test_no_token_means_no_sync(tmp_path):
    m = Maintenance(str(tmp_path), "http://app", "", opener=lambda *a, **k: pytest.fail("darf nicht aufgerufen werden"))
    m.start(30)
    assert m.view()["can_sync"] is False and "app_control_token" in m.view()["app_error"]


def test_observe_adopts_app_maintenance_and_ends_it(tmp_path):
    now = [1000.0]
    m = Maintenance(str(tmp_path), clock=lambda: now[0])
    m.observe(now[0] + 600)
    assert m.active() and m.view()["source"] == "app"
    m.observe(None)
    assert not m.active()
    m.start(10)
    m.observe(None)                                      # selbst gestartet: App-Antwort ohne Wartung beendet sie nicht
    assert m.active()
    m.observe("müll"); m.observe(now[0] - 5)             # kaputte/abgelaufene Werte schaden nicht
    assert m.active()


def test_usb_warning_is_silent_during_maintenance(tmp_path):
    now = [1000.0]
    m = Maintenance(str(tmp_path), clock=lambda: now[0])
    w = usb.UsbWatch(True, scanner=lambda e: {"devices": [], "expect": e, "notes": [], "checked": now[0]}, clock=lambda: now[0])
    w.maintenance = m.active
    w.refresh(); now[0] += usb.GRACE_S + 1
    assert w.problem() is not None
    m.start(10)
    assert w.problem() is None
    now[0] += 700
    assert w.problem() is not None                       # nach Ablauf wieder
    w.maintenance = lambda: 1 / 0
    assert w.problem() is not None                       # kaputte Prüfung schluckt keine Warnung


def test_status_contains_maintenance_and_survives_failure(tmp_path):
    up = FakeUpdater()
    up.version_info = {"maintenance_until": time.time() + 600}
    m = Maintenance(str(tmp_path))
    c = StatusCache(up, maintenance=m)
    c.refresh()
    assert c.get()["maintenance"]["active"] and c.get()["maintenance"]["source"] == "app"
    m.view = lambda: 1 / 0
    assert "app" in c.get()                              # Fehler in der Anzeige kippt den Status nicht


@pytest.fixture
def web2(env):
    def make(token=""):
        env.cfg.token = token
        env.cfg.data_dir = str(env.cfg.data_dir)
        from watchdog.server import create_server
        srv = create_server(env.cfg, env.up)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        env.srv = srv
        return f"http://127.0.0.1:{srv.server_address[1]}"
    yield make
    if hasattr(env, "srv"):
        env.srv.shutdown(); env.srv.server_close()


def test_server_endpoints(web2):
    base = web2(token="geheim")
    H = {"X-Watchdog": "1", "X-Token": "geheim"}
    assert call(base + "/api/maintenance", "POST", {"X-Watchdog": "1"}, {"minutes": 30})[0] == 401
    assert call(base + "/api/maintenance", "POST", {"X-Token": "geheim"}, {"minutes": 30})[0] == 403
    for bad in ({"minutes": 1}, {"minutes": "x"}, {"minutes": True}, {"minutes": 99999}):
        assert call(base + "/api/maintenance", "POST", H, bad)[0] == 400
    code, body = call(base + "/api/maintenance", "POST", H, {"minutes": 30})
    assert code == 200 and json.loads(body)["active"]
    assert json.loads(call(base + "/api/status")[1])["maintenance"]["active"]
    code, body = call(base + "/api/maintenance-end", "POST", H, {})
    assert code == 200 and not json.loads(body)["active"]
