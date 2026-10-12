"""Genaue Erfassung von Unterspannung (power.py) und ihre Einbindung in die Systemprüfung."""
import json
import time

from watchdog import sysmon
from watchdog.power import PowerWatch, DAY_S


def flags(now=False, ever=False, throttled=False):
    raw = (1 if now else 0) | (4 if throttled else 0) | (0x10000 if ever else 0)
    return sysmon.parse_throttled(f"throttled={hex(raw)}")


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, seq, clock=None, name="power_events.jsonl"):
    """PowerWatch mit einer vorgegebenen Folge von Messwerten (None = Messung fehlgeschlagen)."""
    clock = clock or Clock()
    it = iter(seq)
    w = PowerWatch(tmp_path / name if tmp_path else None, lambda: next(it), clock=clock)
    return w, clock


def run(w, clock, steps, dt=2.0):
    for _ in steps:
        w.sample()
        clock.t += dt


def test_episode_has_start_and_duration(tmp_path):
    w, c = make(tmp_path, [flags(), flags(now=True, ever=True, throttled=True), flags(now=True, ever=True), flags(ever=True)])
    run(w, c, range(4))
    eps = w.episodes()
    assert len(eps) == 1 and eps[0]["kind"] == "undervolt"
    assert eps[0]["start"] == 1_000_002.0 and eps[0]["end"] == 1_000_006.0 and eps[0]["duration_s"] == 4.0
    assert eps[0]["throttled"] is True


def test_open_episode_has_no_end_yet(tmp_path):
    w, c = make(tmp_path, [flags(), flags(now=True, ever=True)])
    run(w, c, range(2))
    s = w.summary(c.t)
    assert s["open"] is True and s["count_24h"] == 1 and w.episodes()[0]["end"] is None


def test_short_dip_between_samples_is_recorded_as_missed(tmp_path):
    # Merkbit „seit Start“ wird gesetzt, ohne dass „jetzt“ je zu sehen war
    w, c = make(tmp_path, [flags(), flags(), flags(ever=True), flags(ever=True)])
    run(w, c, range(4))
    eps = w.episodes()
    assert len(eps) == 1 and eps[0]["kind"] == "missed" and eps[0]["duration_s"] is None
    assert w.summary(c.t)["missed_24h"] == 1


def test_observed_episode_is_not_also_counted_as_missed(tmp_path):
    w, c = make(tmp_path, [flags(), flags(now=True, ever=True), flags(ever=True)])
    run(w, c, range(3))
    assert [e["kind"] for e in w.episodes()] == ["undervolt"]


def test_already_set_sticky_bit_at_start_is_not_a_new_event(tmp_path):
    w, c = make(tmp_path, [flags(ever=True), flags(ever=True)])      # war schon vor dem Start gesetzt
    run(w, c, range(2))
    assert w.episodes() == []


def test_events_survive_restart(tmp_path):
    w, c = make(tmp_path, [flags(), flags(now=True, ever=True), flags(ever=True)])
    run(w, c, range(3))
    w2, _ = make(tmp_path, [], clock=Clock(c.t + 100))
    assert len(w2.episodes()) == 1 and w2.episodes()[0]["duration_s"] == 2.0


def test_episode_open_at_restart_is_closed_as_lost(tmp_path):
    # Der Pi startete mitten im Einbruch neu: Ende unbekannt, keine erfundene Dauer
    w, c = make(tmp_path, [flags(), flags(now=True, ever=True)])
    run(w, c, range(2))
    w2, _ = make(tmp_path, [], clock=Clock(c.t + 60))
    eps = w2.episodes()
    assert len(eps) == 1 and eps[0]["lost"] is True and eps[0]["duration_s"] is None and eps[0]["end"] is not None
    assert w2.summary(c.t + 60)["open"] is False


def test_summary_counts_and_longest(tmp_path):
    c = Clock(2 * DAY_S)
    w, _ = make(tmp_path, [], clock=c)

    def episode(at, seconds):
        """Einbruch, der bei `at` beginnt und `seconds` dauert."""
        c.t = at
        w.probe = lambda: flags(now=True, ever=True)
        w.sample()
        c.t = at + seconds
        w.probe = lambda: flags(ever=True)
        w.sample()

    episode(2 * DAY_S, 4)                  # 8 Tage her: nur im Gesamtzähler
    episode(7 * DAY_S, 5)                  # 3 Tage her: zählt für 7 Tage
    episode(10 * DAY_S - 500, 3)           # heute
    episode(10 * DAY_S - 100, 7)           # heute
    now = 10 * DAY_S
    s = w.summary(now)
    assert s["count_total"] == 4 and s["count_24h"] == 2 and s["count_7d"] == 3
    assert s["longest_s_24h"] == 7.0 and s["total_s_24h"] == 10.0
    assert [e["duration_s"] for e in s["recent"]] == [7.0, 3.0, 5.0, 4.0]          # neueste zuerst


def test_failing_probe_never_raises(tmp_path):
    def boom():
        raise RuntimeError("kaputt")
    w = PowerWatch(tmp_path / "p.jsonl", boom, clock=Clock())
    w.sample()                                         # darf nicht werfen
    assert w.summary()["active"] is False and w.episodes() == []


def test_probe_without_pi_returns_none(tmp_path):
    w, c = make(tmp_path, [None, None])
    run(w, c, range(2))
    assert w.summary(c.t)["active"] is False


def test_corrupt_file_is_ignored(tmp_path):
    f = tmp_path / "power_events.jsonl"
    f.write_text('quatsch\n{"ts": "kein Zahl", "kind": "undervolt", "on": true}\n{"ts": 5.0, "kind": "undervolt", "on": true}\n'
                 '{"ts": 9.0, "kind": "undervolt", "on": false}\n[1,2]\n', encoding="utf-8")
    w = PowerWatch(f, lambda: None, clock=Clock())
    eps = w.episodes()
    assert len(eps) == 1 and eps[0]["duration_s"] == 4.0


def test_unwritable_path_keeps_working_in_memory(tmp_path):
    blocker = tmp_path / "datei"
    blocker.write_text("x")                            # „Ordner“ ist eine Datei: Schreiben schlägt fehl
    w, c = make(None, [flags(), flags(now=True, ever=True), flags(ever=True)])
    w.path = blocker / "unten" / "p.jsonl"
    run(w, c, range(3))
    assert len(w.episodes()) == 1


def test_file_is_trimmed_not_unbounded(tmp_path, monkeypatch):
    from watchdog import power
    monkeypatch.setattr(power, "MAX_EVENTS", 10)
    seq = []
    for _ in range(20):
        seq += [flags(now=True, ever=True), flags(ever=True)]
    w, c = make(tmp_path, seq)
    run(w, c, range(40))
    assert len(w.events) <= 10
    assert len((tmp_path / "power_events.jsonl").read_text().splitlines()) <= 11


def test_thread_starts_and_stops(tmp_path):
    n = {"calls": 0}

    def probe():
        n["calls"] += 1
        return flags()
    w = PowerWatch(tmp_path / "p.jsonl", probe, interval=0.02)
    w.start()
    deadline = time.time() + 2
    while n["calls"] < 3 and time.time() < deadline:
        time.sleep(0.02)
    w.stop()
    assert n["calls"] >= 3


# ---- Einbindung in SysWatch ---------------------------------------------------
def watch(tmp_path, clock, throttled):
    base = {"disk": lambda p: {"path": p, "total_mb": 1000, "free_mb": 800, "used_pct": 20.0}, "temp": lambda: 45.0,
            "time": lambda: True, "internet": lambda: True, "tailscale": lambda: None, "boot": lambda: None,
            "throttled": throttled}
    return sysmon.SysWatch(str(tmp_path), "", "", clock=clock, probes=base)


def test_snapshot_contains_power_events_and_warning_mentions_them(tmp_path):
    c = Clock(time.time())
    state = {"p": flags()}
    w = watch(tmp_path, c, lambda: state["p"])
    w.power.sample()
    c.t += 2
    state["p"] = flags(now=True, ever=True)
    w.power.sample()
    c.t += 5
    state["p"] = flags(ever=True)
    w.power.sample()
    w.refresh()
    v = w.view()
    assert v["power_events"]["count_24h"] == 1 and v["power_events"]["longest_s_24h"] == 5.0
    text = " | ".join(v["problems"])
    assert "Unterspannung" in text and "Aufgezeichnet: 1× in 24 h" in text and "längster 5.0 s" in text


def test_snapshot_survives_broken_power_summary(tmp_path):
    w = watch(tmp_path, lambda: 1000.0, lambda: flags())
    w.power.episodes = lambda: (_ for _ in ()).throw(RuntimeError("kaputt"))
    w.refresh()                                        # darf nicht werfen
    assert w.view()["power_events"]["count_24h"] == 0


def test_events_file_is_json_lines(tmp_path):
    w, c = make(tmp_path, [flags(), flags(now=True, ever=True), flags(ever=True)])
    run(w, c, range(3))
    lines = [json.loads(x) for x in (tmp_path / "power_events.jsonl").read_text().splitlines()]
    assert [x["on"] for x in lines] == [True, False] and all(x["kind"] == "undervolt" for x in lines)


def test_warning_uses_precise_time_not_coarse_30s_value(tmp_path):
    """Die Meldung nennt NUR die genaue Zeit (2-s-Messung), nicht zusätzlich die grobe „zuletzt aufgezeichnet“ aus dem 30-s-Takt."""
    c = Clock(time.time())
    state = {"p": flags(now=True, ever=True)}
    w = watch(tmp_path, c, lambda: state["p"])
    w.refresh()                                          # grobe Messung sieht „an“ (so entsteht der alte Zeitwert)
    c.t += 3600
    state["p"] = flags()                                 # genaue Messung: neuer Einbruch eine Stunde später
    w.power.sample()
    c.t += 5
    state["p"] = flags(now=True, ever=True)
    w.power.sample()
    c.t += 5
    state["p"] = flags(ever=True)
    w.power.sample()
    w.refresh()
    text = " | ".join(w.view()["problems"])
    precise = time.strftime("%H:%M:%S", time.localtime(c.t - 5))
    assert "Aufgezeichnet: 1× in 24 h, zuletzt " + precise in text
    assert "zuletzt aufgezeichnet" not in text and "vor Beginn" not in text      # kein zweiter, widersprüchlicher Zeitwert


def test_warning_for_event_older_than_24h(tmp_path):
    c = Clock(time.time())
    state = {"p": flags()}
    w = watch(tmp_path, c, lambda: state["p"])
    w.power.sample()
    c.t += 2
    state["p"] = flags(now=True, ever=True)
    w.power.sample()
    c.t += 3
    state["p"] = flags(ever=True)
    w.power.sample()
    c.t += 2 * DAY_S                                     # zwei Tage später: nur noch im Gesamtzähler
    w.refresh()
    text = " | ".join(w.view()["problems"])
    assert "Letzter aufgezeichneter Einbruch:" in text and "in 24 h" not in text


def test_warning_without_precise_data_keeps_coarse_fallback(tmp_path):
    w = watch(tmp_path, lambda: 1000.0, lambda: flags(ever=True))
    w.refresh()                                          # nie ein Einbruch aufgezeichnet, aber „seit Start“ gesetzt
    assert "vor Beginn der Aufzeichnung" in " | ".join(w.view()["problems"])
