"""Unterspannung neben Sendezeiten legen. Nur Standardbibliothek.

Passiert ein Einbruch, fragt der Watchdog EINMAL beim Hauptprogramm nach den letzten Sendezeiten (`GET /api/sends`) und hängt die nächstliegenden
Sendungen an den Einbruch (`PowerWatch.add_note`). So sieht man, ob die Einbrüche beim Senden des Nodes auftreten.

Bewusst sparsam, damit der Pi nicht belastet wird:
- Abfrage nur NACH einem Einbruch (nie im Takt), nach `SETTLE_S` Wartezeit, damit auch Sendungen kurz danach erfasst sind.
- Höchstens eine Abfrage je `MIN_GAP_S`; Einbrüche in dieser Zeit werden in EINER Abfrage zusammengefasst.
- Ein Thread, Zeitlimit 3 s, Antwort höchstens 64 KB, nie im Anfragepfad der Weboberfläche. Fehler (Hauptprogramm aus, falsches Token) werden
  einmal je Einbruch als Notiz festgehalten, nicht wiederholt.
- Wartezeiten und Mindestabstand laufen über die Laufzeituhr (time.monotonic): springt die Wanduhr (Pi ohne Echtzeituhr, Zeit kommt erst
  per Netz), darf der Thread weder ewig schlafen noch zu früh abfragen.
Nicht an echter Hardware getestet.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import Callable

log = logging.getLogger("watchdog.sends")

SETTLE_S = 12.0          # so lange nach dem Einbruch warten, dann erst abfragen
MIN_GAP_S = 30.0         # zwischen zwei Abfragen mindestens so lange
BEFORE_S = 20.0          # Sendungen bis so viele Sekunden VOR dem Einbruch gelten als „dabei“
AFTER_S = 5.0            # … und bis so viele danach (Uhr/Messraster)
LOOKBACK_S = 90.0        # so weit zurück wird das Hauptprogramm gefragt
MAX_BYTES = 65536
TIMEOUT_S = 3.0
MAX_PENDING = 20
KEEP = 3                 # höchstens so viele nächste Sendungen je Einbruch


def nearest(sends: list[dict], start: float) -> list[dict]:
    """Sendungen im Fenster um `start`, nächste zuerst: [{dt, kind}], dt < 0 = davor."""
    out = []
    for s in sends or []:
        try:
            dt = float(s["ts"]) - start
            if -BEFORE_S <= dt <= AFTER_S:
                out.append({"dt": round(dt, 1), "kind": str(s.get("kind", "?"))[:40]})
        except Exception:
            continue
    out.sort(key=lambda x: abs(x["dt"]))
    return out[:KEEP]


class SendCorrelator:
    def __init__(self, base_url: str, token: str, annotate: Callable[[float, list[dict] | None, str | None], None],
                 clock=time.time, opener=urllib.request.urlopen, settle_s: float = SETTLE_S, min_gap_s: float = MIN_GAP_S,
                 mono=time.monotonic):
        self.base, self.token, self.annotate = base_url, token, annotate
        self._clock, self._open = clock, opener      # `clock` (Wanduhr) nur noch für Kompatibilität; Wartezeiten über `mono`
        self._mono = mono
        self.settle_s, self.min_gap_s = settle_s, min_gap_s
        self._pending: list[float] = []
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._last_fetch = float("-inf")             # Laufzeituhr der letzten Abfrage
        self.fetches = 0

    def request(self, start_ts: float) -> None:
        """Einen Einbruch zur Zuordnung vormerken. Kehrt sofort zurück, wirft nie."""
        try:
            with self._lock:
                if len(self._pending) < MAX_PENDING:
                    self._pending.append(start_ts)
                if self._worker is None or not self._worker.is_alive():
                    self._worker = threading.Thread(target=self._run, name="send-correlate", daemon=True)
                    self._worker.start()
        except Exception:
            log.exception("Sendezeiten: Vormerken fehlgeschlagen")

    def _run(self) -> None:
        try:
            while True:
                wait = max(self.settle_s, self.min_gap_s - (self._mono() - self._last_fetch))
                time.sleep(min(wait, self.settle_s + self.min_gap_s))     # nie länger als nötig, auch bei unerwarteten Werten
                with self._lock:
                    batch, self._pending = self._pending, []
                    if not batch:                 # Abmelden unter derselben Sperre: ein jetzt eintreffender Einbruch startet einen neuen Thread
                        self._worker = None
                        return
                self._process(batch)
                with self._lock:
                    if not self._pending:
                        self._worker = None
                        return
        except BaseException:
            log.exception("Sendezeiten: unerwarteter Fehler")
            with self._lock:
                self._worker = None

    def _fetch(self, since: float) -> list[dict]:
        req = urllib.request.Request(f"{self.base}/api/sends?since={since:.2f}", headers={"X-Control-Token": self.token})
        with self._open(req, timeout=TIMEOUT_S) as r:
            data = json.loads(r.read(MAX_BYTES).decode("utf-8"))
        sends = data.get("sends") if isinstance(data, dict) else None
        if not isinstance(sends, list):
            raise ValueError("unerwartete Antwort")
        return sends

    def _process(self, batch: list[float]) -> None:
        self._last_fetch = self._mono()
        self.fetches += 1
        try:
            if not self.base:
                raise ValueError("keine Adresse des Hauptprogramms")
            sends = self._fetch(min(batch) - LOOKBACK_S)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:120]
            for ts in batch:
                self._safe(ts, None, err)
            return
        for ts in batch:
            self._safe(ts, nearest(sends, ts), None)

    def _safe(self, ts: float, sends, err) -> None:
        try:
            self.annotate(ts, sends, err)
        except Exception:
            log.exception("Sendezeiten: Notiz nicht gespeichert")
