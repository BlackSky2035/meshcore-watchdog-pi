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
