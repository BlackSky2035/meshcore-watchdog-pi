"""Wartungsmodus im Watchdog (nur Standardbibliothek).

Der Watchdog hat einen eigenen Schalter, weil er auch dann erreichbar ist, wenn das Hauptprogramm hängt. Während der Wartung
schweigt seine USB-Warnung. Der Schalter gibt den Wunsch außerdem an das Hauptprogramm weiter (POST/DELETE /api/maintenance mit
`app_control_token`), das dann den Anschluss freigibt und Senden/Meldungen ruhen lässt. Die Weitergabe läuft in einem Hintergrund-Thread
(nie im Anfragepfad); Erfolg oder Fehler steht in der Anzeige. Wird die Wartung im Hauptprogramm gestartet, übernimmt der Watchdog das über
`maintenance_until` aus /api/version. Höchstens 24 Stunden. Nicht an echter Hardware getestet.

Nach eigenem Starten/Beenden zählen nur Antworten des Hauptprogramms, die DANACH kamen (`seen_at`, Laufzeituhr), und keine, solange
die eigene Weitergabe noch unterwegs ist. Sonst schaltete eine veraltete Antwort die gerade beendete Wartung wieder ein.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("watchdog.maintenance")
MIN_MIN, MAX_MIN = 5, 1440
TRIES, RETRY_S, TIMEOUT_S = 3, 5.0, 6.0


def app_base(app_url: str, health_url: str) -> str:
    if app_url:
        return app_url.rstrip("/")
    u = urlparse(health_url or "")
    return f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else ""


class Maintenance:
    def __init__(self, data_dir: str, base_url: str = "", token: str = "", clock=time.time, opener=urllib.request.urlopen, retry_s: float = RETRY_S):
        self._file = Path(data_dir) / "maintenance.json"
        self.base, self.token, self._clock, self._open, self._retry_s = base_url, token, clock, opener, retry_s
        self._lock = threading.Lock()
        self.until: float | None = None
        self.source = "watchdog"
        self.app_synced: bool | None = None
        self.app_error = ""
        self._threads: list[threading.Thread] = []
        self._own_at = float("-inf")               # time.monotonic() des letzten eigenen start()/end()
        self._syncing = 0                          # laufende Weitergaben an das Hauptprogramm
        self._load()

    # ---- Zustand -------------------------------------------------------------
    def _load(self) -> None:
        try:
            d = json.loads(self._file.read_text())
            if isinstance(d.get("until"), (int, float)):
                self.until = float(d["until"])
        except Exception:
            pass

    def _save(self) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(json.dumps({"until": self.until}))
        except Exception:
            log.warning("Wartungszustand nicht gespeichert")

    def active(self) -> bool:
        return self.until is not None and self._clock() < self.until

    def view(self) -> dict:
        a = self.active()
        return {"active": a, "until": self.until if a else None, "remaining_s": max(0, int(self.until - self._clock())) if a else 0,
                "source": self.source, "app_synced": self.app_synced, "app_error": self.app_error, "can_sync": bool(self.base and self.token)}

    # ---- Schalten ---------------------------------------------------------------
    def start(self, minutes: int) -> dict:
        minutes = max(MIN_MIN, min(int(minutes), MAX_MIN))
        with self._lock:
            self.until, self.source = self._clock() + minutes * 60, "watchdog"
            self._own_at = time.monotonic()
            self._save()
        self._sync("POST", {"minutes": minutes})
        return self.view()

    def end(self) -> dict:
        with self._lock:
            self.until, self.source = None, "watchdog"
            self._own_at = time.monotonic()
            self._save()
        self._sync("DELETE", None)
        return self.view()

    def observe(self, app_until, seen_at: float | None = None) -> None:
        """Vom Statusthread: übernimmt die Wartung, die im Hauptprogramm gestartet/beendet wurde. `seen_at` = wann (time.monotonic)
        das Hauptprogramm diesen Wert gemeldet hat; ältere Werte als das letzte eigene Starten/Beenden werden ignoriert."""
        try:
            with self._lock:
                if self._syncing > 0:
                    return                           # eigene Änderung ist noch unterwegs: Antworten des Hauptprogramms sind veraltet
                if seen_at is not None and seen_at <= self._own_at:
                    return
                if isinstance(app_until, (int, float)) and app_until > self._clock():
                    if self.until is None or abs(self.until - app_until) > 2:
                        self.until, self.source = float(app_until), "app"
                        self._save()
                elif app_until is None and self.source == "app" and self.until is not None:
                    self.until = None
                    self._save()
        except Exception:
            log.exception("Wartung konnte nicht übernommen werden")

    # ---- Weitergabe an das Hauptprogramm (Hintergrund) ------------------------------------
    def _sync(self, method: str, body: dict | None) -> None:
        if not (self.base and self.token):
            self.app_synced, self.app_error = None, "Hauptprogramm wird nicht benachrichtigt (app_control_token fehlt)"
            return
        self.app_synced, self.app_error = None, "wird weitergegeben …"
        t = threading.Thread(target=self._sync_worker, args=(method, body), daemon=True, name="maintenance-sync")
        self._threads.append(t)
        with self._lock:
            self._syncing += 1
        try:
            t.start()
        except Exception as e:
            with self._lock:
                self._syncing -= 1
            self.app_synced, self.app_error = False, f"Weitergabe nicht startbar ({e})"

    def _sync_worker(self, method: str, body: dict | None) -> None:
        try:
            self._sync_attempts(method, body)
        finally:
            with self._lock:
                self._syncing -= 1
                self._own_at = time.monotonic()      # erst Antworten nach der Weitergabe zählen

    def _sync_attempts(self, method: str, body: dict | None) -> None:
        err = ""
        for attempt in range(TRIES):
            try:
                req = urllib.request.Request(self.base + "/api/maintenance", method=method, data=json.dumps(body).encode() if body is not None else None,
                                             headers={"X-Control-Token": self.token, "Content-Type": "application/json"})
                with self._open(req, timeout=TIMEOUT_S) as r:
                    if 200 <= r.status < 300:
                        self.app_synced, self.app_error = True, ""
                        return
                    err = f"HTTP {r.status}"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"[:160]
            if attempt < TRIES - 1:
                time.sleep(self._retry_s)
        self.app_synced, self.app_error = False, f"Hauptprogramm nicht erreicht ({err})"

    def join(self, timeout: float = 5.0) -> None:
        for t in list(self._threads):
            t.join(timeout)
