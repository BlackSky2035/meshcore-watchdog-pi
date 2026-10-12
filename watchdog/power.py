"""Unterspannung genau erfassen. Nur Standardbibliothek.

Die normale Systemprüfung misst nur alle 30 s und kennt sonst nur das Merkbit „seit dem Start“. Kurze Einbrüche (z. B. wenn der
Node sendet) gehen so unter. `PowerWatch` fragt deshalb in einem eigenen Thread alle paar Sekunden nur die Strom-Flags ab und hält jeden
Einbruch mit Beginn und Dauer fest, dauerhaft in `data/power_events.jsonl` (überlebt Neustarts, auch einen durch Unterspannung).

Last: Eine Messung ist ein kurzer `vcgencmd`-Aufruf. Wird er langsam (Pi überlastet), verlängert sich der Takt selbstständig bis auf `MAX_INTERVAL_S`
und kehrt zurück, sobald es wieder schnell geht. Zu jedem neuen Einbruch kann ein Rückruf (`on_event`) gesetzt werden; der Watchdog legt damit
die Sendezeiten des Hauptprogramms daneben (`sends.py`, Notizen in derselben Datei).

Grenzen: Auch der schnelle Takt kann Einbrüche verpassen, die kürzer als das Intervall sind. Das Merkbit „seit dem Start“ verrät
zumindest das ERSTE verpasste Ereignis (Art „missed“, ohne Dauer). Gemessen wird nur, was der Pi selbst meldet (`vcgencmd get_throttled`):
Eine Spannung in Volt gibt es beim Pi 3 nicht. Der Thread läuft nie im Anfragepfad und wirft nie. Nicht an echter Hardware getestet.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("watchdog.power")

POWER_INTERVAL_S = 2.0
MAX_INTERVAL_S = 10.0        # so weit darf sich der Takt bei Überlastung strecken
SLOW_PROBE_S = 0.5           # eine Messung, die länger dauert, gilt als „Pi ist ausgelastet“
NO_PROBE_RETRY_S = 300.0     # gibt es kein vcgencmd (kein Pi), nur selten nachsehen statt alle paar Sekunden
MAX_EVENTS = 2000            # Wechsel (je Einbruch zwei), danach weichen die ältesten
DAY_S = 24 * 3600


class PowerWatch:
    def __init__(self, path: str | Path | None, probe: Callable[[], dict | None], interval: float = POWER_INTERVAL_S,
                 clock: Callable[[], float] = time.time, on_event: Callable[[float], None] | None = None,
                 slow_s: float = SLOW_PROBE_S, max_interval: float = MAX_INTERVAL_S):
        self.path = Path(path) if path else None
        self.probe, self.interval, self._clock, self.on_event = probe, interval, clock, on_event
        self.base_interval, self.slow_s, self.max_interval = interval, slow_s, max_interval
        self.events: list[dict] = []
        self._load()
        last = next((e for e in reversed(self.events) if e.get("kind") != "note"), None)
        self._on = bool(last and last.get("kind") == "undervolt" and last.get("on"))
        if self._on:                      # beim letzten Mal lief gerade ein Einbruch, dann endete der Prozess (Neustart durch Unterspannung?)
            self._add({"ts": self._clock(), "kind": "undervolt", "on": False, "lost": True})
            self._on = False
        self._prev_ever: bool | None = None
        self._probe_ok = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- Datei ---------------------------------------------------------------
    def _load(self) -> None:
        try:
            if self.path and self.path.is_file():
                for line in self.path.read_text(encoding="utf-8").splitlines()[-MAX_EVENTS:]:
                    try:
                        e = json.loads(line)
                        if isinstance(e, dict) and isinstance(e.get("ts"), (int, float)) and e.get("kind") in ("undervolt", "missed", "note"):
                            self.events.append(e)
                    except Exception:
                        continue
        except Exception as ex:
            log.warning("Strom-Protokoll nicht lesbar: %s", ex)

    def _add(self, e: dict) -> None:
        self.events.append(e)
        if len(self.events) > MAX_EVENTS:
            self.events = self.events[-MAX_EVENTS // 2:]
            self._rewrite()
            return
        try:
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(e) + "\n")
        except Exception as ex:
            log.warning("Strom-Protokoll nicht schreibbar: %s", ex)

    def _rewrite(self) -> None:
        try:
            if self.path:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text("".join(json.dumps(e) + "\n" for e in self.events), encoding="utf-8")
                tmp.replace(self.path)
        except Exception as ex:
            log.warning("Strom-Protokoll nicht schreibbar: %s", ex)

    # ---- Messen --------------------------------------------------------------
    def sample(self) -> None:
        """Eine Messung auswerten. Wirft nie."""
        try:
            p = self.probe()
        except Exception:
            p = None
        if not p:
            return
        self._probe_ok = True
        now = self._clock()
        with self._lock:
            now_on, ever = bool(p.get("undervolt_now")), bool(p.get("undervolt_ever"))
            new_event = False
            if now_on and not self._on:
                self._add({"ts": now, "kind": "undervolt", "on": True, "raw": p.get("raw"),
                           "throttled": bool(p.get("throttled_now") or p.get("capped_now"))})
                new_event = True
            elif not now_on and self._on:
                self._add({"ts": now, "kind": "undervolt", "on": False})
            elif ever and self._prev_ever is False and not now_on:
                # Merkbit ist neu gesetzt, aber wir haben nie „an“ gesehen: kurzer Einbruch zwischen zwei Messungen
                self._add({"ts": now, "kind": "missed", "raw": p.get("raw")})
                new_event = True
            self._on, self._prev_ever = now_on, ever
        if new_event and self.on_event:
            try:
                self.on_event(now)
            except Exception:
                log.exception("Strom: Rückruf fehlgeschlagen")

    def add_note(self, start_ts: float, sends: list[dict] | None, error: str | None = None) -> None:
        """Zu einem Einbruch (Beginn `start_ts`) die nächstliegenden Sendungen notieren; `sends=None` = nicht abrufbar."""
        with self._lock:
            self._add({"ts": start_ts, "kind": "note", "sends": sends, "error": error})

    # ---- Auswertung ------------------------------------------------------------
    def episodes(self) -> list[dict]:
        """Einbrüche: {start, end (None = läuft noch), duration_s, lost, kind}. Neueste zuletzt."""
        out: list[dict] = []
        cur: dict | None = None
        notes: dict[float, dict] = {}
        with self._lock:
            events = list(self.events)
        for e in events:
            if e["kind"] == "note":
                notes[e["ts"]] = e
            elif e["kind"] == "missed":
                out.append({"start": e["ts"], "end": e["ts"], "duration_s": None, "lost": False, "kind": "missed"})
            elif e.get("on"):
                cur = {"start": e["ts"], "end": None, "duration_s": None, "lost": False, "kind": "undervolt",
                       "throttled": bool(e.get("throttled"))}
                out.append(cur)
            elif cur is not None:
                cur["end"] = e["ts"]
                cur["lost"] = bool(e.get("lost"))
                cur["duration_s"] = None if cur["lost"] else round(e["ts"] - cur["start"], 1)
                cur = None
        for ep in out:                                   # Sendungen rund um den Einbruch (None = nicht bekannt/abrufbar)
            n = notes.get(ep["start"])
            ep["sends_near"] = n.get("sends") if n else None
            ep["sends_error"] = n.get("error") if n else None
        return out

    def summary(self, now: float | None = None, recent: int = 8) -> dict:
        """Zahlen für die Anzeige; wirft nie."""
        try:
            now = self._clock() if now is None else now
            eps = self.episodes()

            def within(span: float, e: dict) -> bool:
                return now - e["start"] <= span

            e24 = [e for e in eps if within(DAY_S, e)]
            e7d = [e for e in eps if within(7 * DAY_S, e)]
            durs = [e["duration_s"] for e in e24 if e["duration_s"] is not None]
            known = [e for e in e24 if e.get("sends_near") is not None]
            return {"sample_s": self.interval, "active": self._probe_ok,
                    "open": bool(eps and eps[-1]["kind"] == "undervolt" and eps[-1]["end"] is None),
                    "count_24h": len(e24), "count_7d": len(e7d), "count_total": len(eps),
                    "missed_24h": sum(1 for e in e24 if e["kind"] == "missed"),
                    "longest_s_24h": max(durs) if durs else None, "total_s_24h": round(sum(durs), 1) if durs else 0.0,
                    "send_known_24h": len(known), "send_near_24h": sum(1 for e in known if e["sends_near"]),
                    "last": eps[-1] if eps else None, "recent": eps[-recent:][::-1]}
        except Exception:
            log.exception("Strom-Auswertung fehlgeschlagen")
            return {"sample_s": self.interval, "active": False, "open": False, "count_24h": 0, "count_7d": 0,
                    "count_total": 0, "missed_24h": 0, "longest_s_24h": None, "total_s_24h": 0.0,
                    "send_known_24h": 0, "send_near_24h": 0, "last": None, "recent": []}

    # ---- Thread ----------------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="powerwatch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.sample()
            except BaseException:
                log.exception("Strommessung: unerwarteter Fehler")
            took = time.monotonic() - t0
            # Pi ausgelastet (Messung dauert lange)? Takt strecken, bei schneller Messung wieder verkürzen
            if took > self.slow_s:
                self.interval = min(self.interval * 2, self.max_interval)
            elif self.interval > self.base_interval:
                self.interval = max(self.base_interval, self.interval / 2)
            self._stop.wait(self.interval if self._probe_ok else NO_PROBE_RETRY_S)
