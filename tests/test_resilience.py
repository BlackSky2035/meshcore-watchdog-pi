"""Der Watchdog muss erreichbar bleiben, was auch kaputtgeht."""
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from watchdog import __main__ as wmain
from watchdog import server as wserver
from watchdog.config import Config, load_config_safe
from watchdog.health import FallbackUpdater, Heartbeat, StatusCache, sd_notify, selfcheck
from watchdog.server import create_server


def call(url, method="GET", headers=None, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}") if r.headers.get_content_type() == "application/json" else r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {}


class Boom(Exception):
    pass


class FakeUpdater:
    unavailable = None
    cfg = Config()

    def __init__(self):
        self.job = {"running": False, "name": None, "started": None, "finished": None, "ok": None,
                    "message": "", "log": []}
        self.fail_status = False
        self.slow = 0.0
        self.started = []

    def status(self):
        if self.slow:
            time.sleep(self.slow)
        if self.fail_status:
            raise Boom("git kaputt")
        return {"version": "t", "app": "running", "manager": "fake", "problems": []}

    def job_view(self):
        return dict(self.job)

    def start(self, name, **kw):
        self.started.append(name)
        return True

    def log_tail(self, n=100):
        raise Boom("Logdatei weg")


@pytest.fixture
def srv():
    made = []

    def make(updater=None, **kw):
        up = updater or FakeUpdater()
        cfg = Config(host="127.0.0.1", port=0)
        s = create_server(cfg, up, **kw)
        threading.Thread(target=s.serve_forever, daemon=True).start()
        made.append(s)
        return f"http://127.0.0.1:{s.server_address[1]}", up, s
    yield make
    for s in made:
        s.shutdown()
        s.server_close()


def test_ping_never_touches_updater(srv):
    up = FakeUpdater()
    up.fail_status = True
    base, _, _ = srv(up)
    code, body = call(base + "/api/ping")
    assert code == 200 and body["ok"] is True


def test_status_survives_failing_status_function(srv):
    up = FakeUpdater()
    up.fail_status = True
    base, _, _ = srv(up)
    code, body = call(base + "/api/status")
    assert code == 200
    assert any("fehlgeschlagen" in p or "ermittelt" in p for p in body["problems"])
    assert body["job"]["running"] is False             # Lauf-Anzeige funktioniert trotzdem


def test_status_does_not_wait_for_slow_git(srv):
    up = FakeUpdater()
    base, _, s = srv(up)
    assert call(base + "/api/status")[0] == 200         # erster Stand
    up.slow = 3.0
    s.status_cache.poke()
    time.sleep(0.3)                                     # Berechnung hängt jetzt in „Git“
    t = time.monotonic()
    code, body = call(base + "/api/status")
    assert code == 200 and time.monotonic() - t < 1.0   # sofort, mit altem Stand
    assert body["stale_s"] is not None


def test_error_in_log_endpoint_becomes_message_not_crash(srv):
    base, _, _ = srv()
    code, body = call(base + "/api/log")
    assert code == 200 and "nicht lesbar" in body["lines"][0]
    assert call(base + "/api/ping")[0] == 200


def test_unexpected_exception_in_handler_gives_500_and_server_lives(srv, monkeypatch):
    base, up, s = srv()
    monkeypatch.setattr(s.status_cache, "get", lambda: (_ for _ in ()).throw(Boom("kaputt")))
    code, body = call(base + "/api/status")
    assert code == 500 and "Interner Fehler" in body["error"]
    assert call(base + "/api/ping")[0] == 200


def test_bad_content_length_and_non_object_json(srv):
    base, _, _ = srv()
    h = {"X-Watchdog": "1"}
    assert call(base + "/api/check", "POST", {**h, "Content-Length": "abc"}, raw=b"")[0] in (400, 411)
    assert call(base + "/api/check", "POST", h, raw=b"[1,2]")[0] == 400
    assert call(base + "/api/check", "POST", h, raw=b"{}")[0] == 202


def test_broken_client_connection_does_not_hurt(srv):
    base, _, s = srv()
    port = s.server_address[1]
    for payload in (b"GET /api/status HTTP/1.1\r\n", b"\x00\xff\xfe garbage\r\n\r\n", b"POST /api/check HTTP/1.1\r\nContent-Length: 99999\r\n\r\nab"):
        c = socket.create_connection(("127.0.0.1", port))
        c.sendall(payload)
        c.close()
    time.sleep(0.2)
    assert call(base + "/api/ping")[0] == 200


def test_connection_limit_gives_503_not_hang(srv, monkeypatch):
    monkeypatch.setattr(wserver, "SOCKET_TIMEOUT_S", 5.0)
    base, _, s = srv(max_connections=2)
    port = s.server_address[1]
    holders = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]   # belegen beide Plätze
    time.sleep(0.3)
    c = socket.create_connection(("127.0.0.1", port))
    c.settimeout(3)
    data = c.recv(200)
    assert b"503" in data
    for h in holders + [c]:
        h.close()


def _ping_from(port, src="127.0.0.1"):
    """GET /api/ping über eine eigene Verbindung (optional von einer bestimmten lokalen Adresse)."""
    c = socket.create_connection(("127.0.0.1", port), timeout=3, source_address=(src, 0))
    try:
        c.sendall(b"GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        return c.recv(200)
    finally:
        c.close()


def test_slowloris_cannot_block_own_ping(srv, monkeypatch):
    """Viele tröpfelnde Verbindungen von EINER fremden Adresse: Je Adresse gibt es nur wenige Plätze, Plätze für den Rechner selbst
    sind reserviert. Der eigene Herzschlag (/api/ping von 127.0.0.1) bekommt weiter 200."""
    monkeypatch.setattr(wserver, "REQUEST_DEADLINE_S", 30.0)
    base, _, s = srv(max_connections=10, max_per_ip=3, local_reserved=2)
    port = s.server_address[1]
    # 127.0.0.2 ist auch Loopback; damit eine „fremde“ Adresse entsteht, gilt im Test nur 127.0.0.1 als lokal
    monkeypatch.setattr(wserver, "is_local_peer", lambda ip, own=None: ip == "127.0.0.1")
    attackers = []
    try:
        for i in range(12):                              # mehr als Plätze je Adresse und insgesamt
            src = f"127.0.0.{2 + i % 4}"                 # 4 „fremde“ Adressen, je 3 erlaubt → 8 Plätze (10 - 2 reserviert)
            c = socket.create_connection(("127.0.0.1", port), timeout=3, source_address=(src, 0))
            c.sendall(b"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1\r\n")    # Kopf nie fertig
            attackers.append(c)
        time.sleep(0.5)
        assert s._total <= 10 - 2                        # die Reserve bleibt frei
        assert all(n <= 3 for ip, n in s._per_ip.items() if ip != "127.0.0.1")
        assert b"200" in _ping_from(port).split(b"\r\n", 1)[0]
        assert b"200" in _ping_from(port).split(b"\r\n", 1)[0]
    finally:
        for c in attackers:
            c.close()


def test_dribbling_connection_is_closed_after_total_deadline(srv, monkeypatch):
    """Eine Verbindung, die alle 0,3 s eine Kopfzeile nachschiebt (jeder einzelne Lesevorgang ist schnell), wird nach der
    Gesamtfrist geschlossen und gibt ihren Platz frei."""
    monkeypatch.setattr(wserver, "REQUEST_DEADLINE_S", 1.5)
    base, _, s = srv()
    port = s.server_address[1]
    c = socket.create_connection(("127.0.0.1", port), timeout=5)
    t0 = time.monotonic()
    closed_after = None
    try:
        c.sendall(b"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1\r\n")
        for i in range(40):                              # bis 12 s lang tröpfeln
            time.sleep(0.3)
            try:
                c.sendall(f"X-Slow-{i}: a\r\n".encode())
            except OSError:
                closed_after = time.monotonic() - t0
                break
            c.settimeout(0.01)
            try:
                if c.recv(100) == b"":
                    closed_after = time.monotonic() - t0
                    break
            except (socket.timeout, BlockingIOError):
                pass
            except OSError:
                closed_after = time.monotonic() - t0
                break
    finally:
        c.close()
    assert closed_after is not None and closed_after < 5, closed_after
    for _ in range(20):
        if s._total == 0:
            break
        time.sleep(0.1)
    assert s._total == 0
    assert call(base + "/api/ping")[0] == 200


def test_slow_but_complete_request_within_deadline_still_works(srv, monkeypatch):
    """Die Frist stört normale (auch etwas langsame) Anfragen nicht, auch POST mit Body."""
    monkeypatch.setattr(wserver, "REQUEST_DEADLINE_S", 3.0)
    base, up, s = srv()
    port = s.server_address[1]
    c = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        for part in (b"POST /api/check HTTP/1.1\r\n", b"Host: 127.0.0.1\r\nX-Watchdog: 1\r\n",
                     b"Content-Length: 2\r\n\r\n", b"{}"):
            c.sendall(part)
            time.sleep(0.3)
        assert b"202" in c.recv(200).split(b"\r\n", 1)[0]
    finally:
        c.close()


def test_slow_body_is_cut_off_by_deadline(srv, monkeypatch):
    """Auch der Body zählt zur Frist: ein Body, der nie fertig wird, belegt den Platz nicht länger als die Frist."""
    monkeypatch.setattr(wserver, "REQUEST_DEADLINE_S", 1.0)
    base, up, s = srv()
    port = s.server_address[1]
    c = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        c.sendall(b"POST /api/check HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Watchdog: 1\r\nContent-Length: 100\r\n\r\n{")
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5 and s._total:
            try:
                c.sendall(b" ")
            except OSError:
                break
            time.sleep(0.2)
        assert time.monotonic() - t0 < 4
    finally:
        c.close()
    assert up.started == []                              # nichts ausgeführt


def test_ping_is_open_for_any_host_but_status_is_not(srv):
    base, _, _ = srv()
    assert call(base + "/api/ping", headers={"Host": "evil.example"})[0] == 200
    assert call(base + "/api/status", headers={"Host": "evil.example"})[0] == 403
    assert call(base + "/", headers={"Host": "evil.example"})[0] == 403


def test_missing_index_html_has_fallback(srv, monkeypatch, tmp_path):
    monkeypatch.setattr(wserver, "STATIC", tmp_path)
    base, _, _ = srv()
    code, body = call(base + "/")
    assert code == 200 and b"fehlt" in body


def test_fallback_updater_serves_ui_but_refuses_actions(srv):
    base, up, _ = srv(FallbackUpdater("repo_dir ist nicht gesetzt"))
    code, body = call(base + "/api/status")
    assert code == 200 and any("Notbetrieb" in p for p in body["problems"])
    code, body = call(base + "/api/update", "POST", {"X-Watchdog": "1"})
    assert code == 503 and "repo_dir" in body["error"]


def test_status_cache_marks_old_data(monkeypatch):
    up = FakeUpdater()
    c = StatusCache(up)
    c.refresh()
    c._at -= 100
    assert any("alt" in p for p in c.get()["problems"])


# ---- Konfiguration ----
def test_broken_config_file_falls_back(tmp_path):
    bad = tmp_path / "c.toml"
    bad.write_text("das ist [ kein toml")
    cfg, err = load_config_safe(bad)
    assert err and "nicht lesbar" in err and cfg.port == 8100


def test_invalid_value_falls_back(tmp_path):
    bad = tmp_path / "c.toml"
    bad.write_text('[server]\nport = "abc"\n')
    cfg, err = load_config_safe(bad)
    assert err and cfg.port == 8100


def test_build_updater_degrades_instead_of_raising(tmp_path):
    assert isinstance(wmain.build_updater(Config(), "kaputt"), FallbackUpdater)
    assert isinstance(wmain.build_updater(Config(repo_dir=""), None), FallbackUpdater)
    u = wmain.build_updater(Config(repo_dir=str(tmp_path), manager="gibtsnicht", data_dir=str(tmp_path / "d")), None)
    assert isinstance(u, FallbackUpdater)


# ---- Server-Schleife ----
def test_serve_retries_when_port_is_taken(monkeypatch):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    cfg = Config(host="127.0.0.1", port=port)
    stop = threading.Event()
    t = threading.Thread(target=wmain.serve, args=(cfg, FakeUpdater(), stop), kwargs={"retry_min": 0.1, "retry_max": 0.2}, daemon=True)
    t.start()
    time.sleep(0.5)
    assert wmain.serve.current is None                  # Port belegt: noch kein Server, aber kein Absturz
    blocker.close()
    for _ in range(40):
        if wmain.serve.current is not None:
            break
        time.sleep(0.1)
    assert wmain.serve.current is not None
    assert call(f"http://127.0.0.1:{port}/api/ping")[0] == 200
    stop.set()
    wmain.serve.current.shutdown()
    t.join(5)
    assert not t.is_alive()


def test_serve_restarts_after_server_crash():
    cfg = Config(host="127.0.0.1", port=0)
    stop = threading.Event()
    calls = []

    class Crashy:
        server_address = ("127.0.0.1", 1)

        def serve_forever(self):
            calls.append(1)
            if len(calls) < 3:
                raise Boom("Absturz")
            stop.set()

        def server_close(self):
            pass

        def shutdown(self):
            pass
    wmain.serve(cfg, FakeUpdater(), stop, make_server=lambda c, u: Crashy(), retry_min=0.01, retry_max=0.02)
    assert len(calls) == 3


# ---- systemd-Herzschlag ----
@pytest.mark.skipif(sys.platform == "win32", reason="Unix-Sockets")
def test_sd_notify_and_heartbeat(tmp_path, monkeypatch, srv):
    path = str(tmp_path / "notify.sock")
    rx = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    rx.bind(path)
    rx.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    assert sd_notify("READY=1") and rx.recv(100) == b"READY=1"
    base, _, s = srv()
    port = s.server_address[1]
    assert selfcheck("127.0.0.1", port)
    hb = Heartbeat(lambda: port, "127.0.0.1")
    assert hb.beat() and rx.recv(100) == b"WATCHDOG=1"
    # Server antwortet nicht mehr -> KEIN Lebenszeichen (systemd greift ein)
    hb2 = Heartbeat(lambda: 1, "127.0.0.1")
    rx.settimeout(0.3)
    assert not hb2.beat() and hb2.failures == 1
    with pytest.raises(socket.timeout):
        rx.recv(100)


def test_sd_notify_without_systemd_is_harmless(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sd_notify("WATCHDOG=1") is False
    monkeypatch.setenv("NOTIFY_SOCKET", "/gibt/es/nicht")
    assert sd_notify("WATCHDOG=1") is False
