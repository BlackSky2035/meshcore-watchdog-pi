import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from watchdog.server import create_server


@pytest.fixture
def web(env):
    def make(token=""):
        env.cfg.token = token
        srv = create_server(env.cfg, env.up)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        env.srv = srv
        return f"http://127.0.0.1:{srv.server_address[1]}"
    yield make
    if hasattr(env, "srv"):
        env.srv.shutdown()
        env.srv.server_close()


def call(url, method="GET", headers=None, body=None):
    req = urllib.request.Request(url, method=method, headers=headers or {},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_page_and_status(web):
    base = web()
    code, body = call(base + "/")
    assert code == 200 and b"MeshCore Watchdog" in body
    code, body = call(base + "/api/status")
    s = json.loads(body)
    assert code == 200 and s["app"] == "stopped" and s["manager"] == "process"


def test_post_requires_header(web):
    base = web()
    assert call(base + "/api/check", "POST")[0] == 403
    assert call(base + "/api/check", "POST", {"X-Watchdog": "1"})[0] == 202


def test_token(web):
    base = web(token="geheim")
    h = {"X-Watchdog": "1"}
    assert call(base + "/api/check", "POST", h)[0] == 401
    assert call(base + "/api/check", "POST", {**h, "X-Token": "falsch"})[0] == 401
    assert call(base + "/api/check", "POST", {**h, "X-Token": "geheim"})[0] == 202
    assert call(base + "/api/status")[0] == 200            # Lesen bleibt offen


@pytest.mark.parametrize("token", ["geheim", "Bär", "grün🙂"])
def test_token_with_umlauts_and_emoji(web, token):
    """Früher: Umlaut im Token → TypeError im Vergleich → jeder POST 500. Die Oberfläche schickt das Token als UTF-8-Bytes."""
    base = web(token=token)
    h = {"X-Watchdog": "1"}
    as_ui = token.encode("utf-8").decode("latin-1")          # so kommt es von der Oberfläche (TextEncoder) und von curl an
    assert call(base + "/api/check", "POST", {**h, "X-Token": as_ui})[0] == 202
    env_up_wait(web)
    assert call(base + "/api/check", "POST", {**h, "X-Token": "falsch"})[0] == 401
    try:
        latin = token.encode("latin-1").decode("latin-1")    # älterer Weg: „ä“ als ein Latin-1-Byte
    except UnicodeEncodeError:
        return
    assert call(base + "/api/check", "POST", {**h, "X-Token": latin})[0] in (202, 409)


def env_up_wait(_web, timeout=5.0):
    time.sleep(0.3)                                          # „check“ läuft im Hintergrund; danach ist die Sperre wieder frei


def test_metrics_limit_is_capped(web, env):
    from watchdog import server as S
    base = web()
    asked = []
    env.srv.status_cache.sys.history = lambda n: asked.append(n) or []
    for q in ("limit=0", "limit=-5", "limit=100000", "limit=200", "limit=abc"):
        assert call(base + "/api/metrics?" + q)[0] == 200
    assert asked == [200, 200, S.METRICS_MAX_POINTS, 200, 200]


def test_metrics_endpoint(web):
    base = web()
    code, body = call(base + "/api/metrics")
    data = json.loads(body)
    assert code == 200 and isinstance(data["samples"], list)


def test_log_endpoint_accepts_since_until(web):
    base = web()
    code, body = call(base + "/api/log?since=2025-01-01+12:00&until=2025-01-01+13:00")
    assert code == 200 and isinstance(json.loads(body)["lines"], list)


def test_runs_endpoint_lists_and_shows_a_past_run(web, env):
    base = web()
    env.up.run("check")
    code, body = call(base + "/api/runs")
    data = json.loads(body)
    assert code == 200 and len(data["jobs"]) >= 1
    jid = data["jobs"][0]["id"]
    code, body = call(base + f"/api/runs/{jid}")
    data = json.loads(body)
    assert code == 200 and data["id"] == jid and isinstance(data["lines"], list)


def test_runs_endpoint_bad_id(web):
    base = web()
    assert call(base + "/api/runs/quatsch")[0] == 400
    code, body = call(base + "/api/runs/999999")
    assert code == 200 and json.loads(body)["lines"] == []


def test_conflict_while_running(web, env):
    base = web()
    assert env.up._begin("check")
    try:
        assert call(base + "/api/restart", "POST", {"X-Watchdog": "1"})[0] == 409
    finally:
        env.up._release()
        env.up.job["running"] = False


def test_bad_requests(web):
    base = web()
    h = {"X-Watchdog": "1"}
    assert call(base + "/api/unbekannt", "POST", h)[0] == 404
    assert call(base + "/api/status", "POST", h)[0] == 404
    assert call(base + "/nope")[0] == 404
    req = urllib.request.Request(base + "/api/update", method="POST", headers=h, data=b"{kaputt")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 400
    big = urllib.request.Request(base + "/api/update", method="POST", headers=h, data=b"x" * 5000)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(big, timeout=5)
    assert e.value.code == 413


def test_update_via_api(web, env):
    base = web()
    h = {"X-Watchdog": "1"}
    env.up.run("start")
    env.push("v2", VERSION="2\n")
    assert call(base + "/api/update", "POST", h, {})[0] == 202
    for _ in range(100):
        s = json.loads(call(base + "/api/status")[1])
        if not s["job"]["running"]:
            break
        time.sleep(0.2)
    assert s["job"]["ok"] is True and env.version() == "2"


def test_log_endpoint_allows_only_one_journal_read_at_a_time(web, env, monkeypatch):
    """journalctl kann dauern: höchstens ein Aufruf gleichzeitig, weitere bekommen sofort 429 statt Verbindungen zu belegen."""
    gate = threading.Event()
    monkeypatch.setattr(env.up, "log_tail", lambda n, since="", until="": (gate.wait(5), ["x"])[1])
    base = web()
    first = {}
    t = threading.Thread(target=lambda: first.setdefault("r", call(base + "/api/log")), daemon=True)
    t.start()
    time.sleep(0.3)
    t0 = time.monotonic()
    code, body = call(base + "/api/log")
    took = time.monotonic() - t0
    gate.set()
    t.join(5)
    assert code == 429 and "Protokoll" in json.loads(body)["error"] and took < 2
    assert first["r"][0] == 200
    assert call(base + "/api/log")[0] == 200               # danach wieder frei


def test_post_with_foreign_host_header_is_rejected(web, env):
    """Schutz vor DNS-Rebinding: ändernde Aufrufe nur mit bekanntem Host-Namen."""
    base = web()
    h = {"X-Watchdog": "1"}
    assert call(base + "/api/check", "POST", {**h, "Host": "boese.example.com"})[0] == 403
    assert call(base + "/api/status", headers={"Host": "boese.example.com"})[0] == 403     # Lesen auch geschützt
    _wait_idle(env)
    assert call(base + "/api/check", "POST", {**h, "Host": "station.tail1234.ts.net"})[0] == 202
    _wait_idle(env)
    assert call(base + "/api/check", "POST", {**h, "Host": "localhost:8100"})[0] == 202
    _wait_idle(env)
    assert call(base + "/api/check", "POST", {**h, "Host": "192.168.1.20:8100"})[0] == 202
    _wait_idle(env)
    assert call(base + "/api/check", "POST", {**h, "Host": "[::1]:8100"})[0] == 202
    _wait_idle(env)
    import socket
    assert call(base + "/api/check", "POST", {**h, "Host": socket.gethostname() + ".local"})[0] == 202
    _wait_idle(env)
    env.cfg.allowed_hosts = ("watchdog.heim.lan",)
    assert call(base + "/api/check", "POST", {**h, "Host": "watchdog.heim.lan:8100"})[0] == 202


def _wait_idle(env):
    for _ in range(100):
        if not env.up.job["running"]:
            return
        time.sleep(0.1)


def test_get_with_foreign_host_header_is_rejected_except_ping(web, env):
    """DNS-Rebinding: auch Lesen (Status, Protokoll, Läufe, Messwerte, Startseite) nur mit bekanntem Host. /api/ping bleibt offen."""
    base = web()
    for path in ("/", "/index.html", "/api/status", "/api/log", "/api/runs", "/api/runs/1", "/api/metrics", "/gibtsnicht"):
        code, body = call(base + path, headers={"Host": "evil.example"})
        assert code == 403 and "DNS-Rebinding" in json.loads(body)["error"], path
    assert call(base + "/api/ping", headers={"Host": "evil.example"})[0] == 200
    for host in ("127.0.0.1:8100", "localhost", "x.ts.net", "[::1]:8100"):
        assert call(base + "/api/status", headers={"Host": host})[0] == 200, host
        assert call(base + "/", headers={"Host": host})[0] == 200, host
    env.cfg.allowed_hosts = (".heim.lan",)
    assert call(base + "/api/status", headers={"Host": "watchdog.heim.lan"})[0] == 200
