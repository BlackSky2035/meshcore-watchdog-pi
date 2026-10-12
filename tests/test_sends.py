"""Unterspannung neben Sendezeiten legen (sends.py) und die Entlastung (Takt-Bremse, Abfrage nur nach Einbrüchen)."""
import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from watchdog import sysmon
from watchdog.power import PowerWatch
from watchdog.sends import SendCorrelator, nearest, BEFORE_S, KEEP


def flags(now=False, ever=False):
    return sysmon.parse_throttled(f"throttled={hex((1 if now else 0) | (0x10000 if ever else 0))}")


def until(cond, timeout=3.0):
    end = time.time() + timeout
    while not cond() and time.time() < end:
        time.sleep(0.01)
    return cond()


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_opener(sends=None, raises=None, raw=None, log=None):
    def opener(req, timeout=None):
        if log is not None:
            log.append((req.full_url, dict(req.header_items()), timeout))
        if raises:
            raise raises
        return Resp(raw if raw is not None else json.dumps({"now": 1, "capacity": 300, "sends": sends or []}).encode())
    return opener


# ---- nearest -------------------------------------------------------------------------------------
def test_nearest_window_order_and_limit():
    sends = [{"ts": 100 - 1.0, "kind": "Advert"}, {"ts": 100 - 15.0, "kind": "Ping"}, {"ts": 100 - 60, "kind": "zu alt"},
             {"ts": 100 + 2.0, "kind": "danach"}, {"ts": 100 + 30, "kind": "zu spät"}, {"ts": 100 - 3.0, "kind": "x"}, {"ts": 100 - 4.0, "kind": "y"}]
    out = nearest(sends, 100.0)
    assert [s["kind"] for s in out] == ["Advert", "danach", "x"] and len(out) == KEEP     # nächste zuerst, höchstens KEEP
    assert out[0]["dt"] == -1.0 and out[1]["dt"] == 2.0
    assert all(-BEFORE_S <= s["dt"] for s in out)


def test_nearest_ignores_garbage():
    assert nearest([{"kind": "ohne ts"}, "quatsch", None, {"ts": "x"}, {"ts": 99.0, "kind": "ok"}], 100.0)[0]["kind"] == "ok"
    assert nearest(None, 100.0) == []


# ---- Abgleich ------------------------------------------------------------------------------------
def test_one_request_fetches_once_and_annotates():
    got, calls = [], []
    c = SendCorrelator("http://app:8000", "tok", lambda ts, s, e: got.append((ts, s, e)),
                       opener=fake_opener([{"ts": 99.0, "kind": "Advert"}], log=calls), settle_s=0.05, min_gap_s=0.05)
    c.request(100.0)
    assert until(lambda: got)
    assert got == [(100.0, [{"dt": -1.0, "kind": "Advert"}], None)]
    url, headers, timeout = calls[0]
    assert url.startswith("http://app:8000/api/sends?since=") and headers["X-control-token"] == "tok" and timeout == 3.0


def test_nothing_is_fetched_without_an_event():
    calls = []
    SendCorrelator("http://app:8000", "", lambda *a: None, opener=fake_opener(log=calls), settle_s=0.01, min_gap_s=0.01)
    time.sleep(0.1)
    assert calls == []                                  # nie im Takt, nur nach einem Einbruch


def test_events_in_a_burst_share_one_fetch():
    got, calls = [], []
    c = SendCorrelator("http://app:8000", "", lambda ts, s, e: got.append(ts), opener=fake_opener(log=calls),
                       settle_s=0.15, min_gap_s=0.15)
    for ts in (100.0, 101.0, 102.0):
        c.request(ts)
    assert until(lambda: len(got) == 3)
    assert sorted(got) == [100.0, 101.0, 102.0] and len(calls) == 1      # drei Einbrüche, EINE Abfrage


def test_min_gap_between_fetches():
    stamps, got = [], []

    def opener(req, timeout=None):
        stamps.append(time.monotonic())
        return Resp(json.dumps({"sends": []}).encode())
    c = SendCorrelator("http://app:8000", "", lambda ts, s, e: got.append(ts), opener=opener, settle_s=0.02, min_gap_s=0.3)
    c.request(1.0)
    assert until(lambda: len(got) == 1)
    c.request(2.0)
    assert until(lambda: len(got) == 2, timeout=3)
    assert stamps[1] - stamps[0] >= 0.28                # zweite Abfrage frühestens nach dem Mindestabstand


def test_unreachable_app_is_noted_once_not_retried():
    got, calls = [], []
    c = SendCorrelator("http://app:8000", "", lambda ts, s, e: got.append((ts, s, e)),
                       opener=fake_opener(raises=ConnectionRefusedError("aus"), log=calls), settle_s=0.02, min_gap_s=0.02)
    c.request(5.0)
    assert until(lambda: got)
    time.sleep(0.15)
    assert len(calls) == 1 and got[0][1] is None and "ConnectionRefusedError" in got[0][2]


def test_bad_responses_are_handled():
    for raw in (b"kein json", b'{"sends": "nein"}', b"[1,2]"):
        got = []
        c = SendCorrelator("http://app:8000", "", lambda ts, s, e: got.append((s, e)), opener=fake_opener(raw=raw),
                           settle_s=0.01, min_gap_s=0.01)
        c.request(1.0)
        assert until(lambda: got) and got[0][0] is None and got[0][1]


def test_missing_address_is_noted():
    got = []
    c = SendCorrelator("", "", lambda ts, s, e: got.append((s, e)), opener=fake_opener(), settle_s=0.01, min_gap_s=0.01)
    c.request(1.0)
    assert until(lambda: got) and got[0][0] is None


def test_annotate_failure_does_not_kill_the_worker():
    seen = []

    def annotate(ts, s, e):
        seen.append(ts)
        raise RuntimeError("Datei weg")
    c = SendCorrelator("http://app:8000", "", annotate, opener=fake_opener(), settle_s=0.02, min_gap_s=0.02)
    c.request(1.0)
    assert until(lambda: seen == [1.0])
    c.request(2.0)                                       # Worker läuft trotz des Fehlers weiter bzw. startet neu
    assert until(lambda: seen == [1.0, 2.0])


def test_pending_is_bounded():
    c = SendCorrelator("http://app:8000", "", lambda *a: None, opener=fake_opener(), settle_s=5, min_gap_s=5)
    for i in range(500):
        c.request(float(i))
    assert len(c._pending) <= 20


def test_real_http_server_roundtrip():
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"], seen["token"] = self.path, self.headers.get("X-Control-Token")
            body = json.dumps({"now": 1, "capacity": 300, "sends": [{"ts": 98.0, "kind": "Ping"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        got = []
        c = SendCorrelator(f"http://127.0.0.1:{srv.server_port}", "geheim", lambda ts, s, e: got.append((s, e)),
                           settle_s=0.02, min_gap_s=0.02)
        c.request(100.0)
        assert until(lambda: got)
        assert got[0] == ([{"dt": -2.0, "kind": "Ping"}], None)
        assert seen["path"].startswith("/api/sends?since=") and seen["token"] == "geheim"
    finally:
        srv.shutdown()


# ---- Strom-Ereignisse + Notizen --------------------------------------------------------------------
class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_callback_only_for_new_events(tmp_path):
    ev, clock = [], Clock()
    seq = iter([flags(), flags(now=True, ever=True), flags(now=True, ever=True), flags(ever=True), flags(ever=True)])
    w = PowerWatch(tmp_path / "p.jsonl", lambda: next(seq), clock=clock, on_event=ev.append)
    for _ in range(5):
        w.sample()
        clock.t += 2
    assert ev == [1002.0]                                # nur beim Beginn, nicht während und nicht beim Ende


def test_callback_for_missed_and_failing_callback_is_harmless(tmp_path):
    clock = Clock()
    seq = iter([flags(), flags(ever=True)])

    def boom(ts):
        raise RuntimeError("kaputt")
    w = PowerWatch(tmp_path / "p.jsonl", lambda: next(seq), clock=clock, on_event=boom)
    w.sample()
    clock.t += 2
    w.sample()                                           # darf nicht werfen
    assert [e["kind"] for e in w.episodes()] == ["missed"]


def test_note_is_attached_and_counted(tmp_path):
    clock = Clock(2000.0)
    seq = iter([flags(), flags(now=True, ever=True), flags(ever=True)])
    w = PowerWatch(tmp_path / "p.jsonl", lambda: next(seq), clock=clock)
    for _ in range(3):
        w.sample()
        clock.t += 2
    w.add_note(2002.0, [{"dt": -1.2, "kind": "Advert"}])
    ep = w.episodes()[0]
    assert ep["sends_near"] == [{"dt": -1.2, "kind": "Advert"}] and ep["sends_error"] is None
    s = w.summary(clock.t)
    assert s["send_known_24h"] == 1 and s["send_near_24h"] == 1
    w.add_note(2002.0, [])                               # späteres Ergebnis „keine Sendung“ ersetzt das alte
    assert w.summary(clock.t)["send_near_24h"] == 0 and w.summary(clock.t)["send_known_24h"] == 1


def test_note_with_error_is_unknown_not_zero(tmp_path):
    clock = Clock(3000.0)
    seq = iter([flags(), flags(now=True, ever=True), flags(ever=True)])
    w = PowerWatch(tmp_path / "p.jsonl", lambda: next(seq), clock=clock)
    for _ in range(3):
        w.sample()
        clock.t += 2
    w.add_note(3002.0, None, "ConnectionRefusedError: aus")
    s = w.summary(clock.t)
    assert s["send_known_24h"] == 0 and w.episodes()[0]["sends_error"].startswith("Connection")


def test_notes_survive_restart_and_do_not_confuse_open_detection(tmp_path):
    clock = Clock(4000.0)
    seq = iter([flags(), flags(now=True, ever=True)])
    w = PowerWatch(tmp_path / "p.jsonl", lambda: next(seq), clock=clock)
    for _ in range(2):
        w.sample()
        clock.t += 2
    w.add_note(4002.0, [{"dt": -0.5, "kind": "Ping"}])   # Notiz steht NACH dem „an“ in der Datei
    w2 = PowerWatch(tmp_path / "p.jsonl", lambda: None, clock=Clock(4100.0))
    eps = w2.episodes()
    assert len(eps) == 1 and eps[0]["lost"] is True      # trotz Notiz am Ende als abgebrochen erkannt
    assert eps[0]["sends_near"] == [{"dt": -0.5, "kind": "Ping"}]


# ---- Entlastung: Takt-Bremse -------------------------------------------------------------------------
def test_interval_backs_off_when_probe_is_slow_and_recovers(tmp_path):
    slow = {"on": True}

    def probe():
        if slow["on"]:
            time.sleep(0.03)
        return flags()
    w = PowerWatch(tmp_path / "p.jsonl", probe, interval=0.01, slow_s=0.015, max_interval=0.08)
    w.start()
    assert until(lambda: w.interval >= 0.08, timeout=5)  # streckt sich bis zur Obergrenze, nicht darüber
    assert w.interval == 0.08
    slow["on"] = False
    assert until(lambda: w.interval <= 0.01, timeout=5)  # kehrt zum normalen Takt zurück
    w.stop()


# ---- Einbindung in SysWatch -------------------------------------------------------------------------------
def test_syswatch_fetches_sends_after_an_event(tmp_path):
    clock = Clock(time.time())
    state = {"p": flags()}
    base = {"disk": lambda p: {"path": p, "total_mb": 1000, "free_mb": 800, "used_pct": 20.0}, "temp": lambda: 45.0,
            "time": lambda: True, "internet": lambda: True, "tailscale": lambda: None, "boot": lambda: None,
            "throttled": lambda: state["p"]}
    w = sysmon.SysWatch(str(tmp_path), "", "", clock=clock, probes=base, app_base_url="http://app:8000", app_control_token="t")
    calls = []
    w.sends._open = fake_opener([{"ts": clock.t + 1.5, "kind": "Advert"}], log=calls)
    w.sends.settle_s, w.sends.min_gap_s = 0.02, 0.02
    w.power.sample()
    clock.t += 3
    state["p"] = flags(now=True, ever=True)
    w.power.sample()                                     # Einbruch → Abfrage wird vorgemerkt
    assert until(lambda: w.power.summary(clock.t)["send_known_24h"] == 1)
    assert len(calls) == 1
    assert w.power.episodes()[0]["sends_near"][0]["kind"] == "Advert"


def test_syswatch_without_app_address_has_no_correlator(tmp_path):
    w = sysmon.SysWatch(str(tmp_path), "", "", probes={"throttled": lambda: None})
    assert w.sends is None and w.power.on_event is None
