"""Simulierter Watchdog zum Ausprobieren der Oberfläche (`python -m watchdog --demo`).

Es gibt kein Git, keinen Prozess und keine Dateisperre: alles passiert nur im Speicher.
Darum braucht es kein Git, kein systemd und kein fcntl (unter Windows nicht getestet). Die Oberfläche und die HTTP-Schnittstelle sind dieselben
wie im echten Betrieb (gleiche `status()`-Form, gleiche Aktionen).

Ablauf zum Durchklicken:
  1. „Auf Updates prüfen“  -> 2 neue Änderungen
  2. „Jetzt aktualisieren“ -> klappt, Programm läuft danach auf dem neuen Stand
  3. Prüfen -> 1 weitere Änderung; Aktualisieren schlägt hier absichtlich fehl (Programm startet nicht),
     der Watchdog rollt automatisch zurück und markiert den Stand als fehlerhaft
  4. „Trotzdem versuchen“ / „Zurücksetzen“ / Stoppen / Starten / Neustarten ausprobieren
"""
from __future__ import annotations

import threading
import time

from pathlib import Path

from . import __version__
from .logstore import JobRecorder, LogStore

STEP_S = 0.9          # Pause zwischen den simulierten Schritten
JOB_LOG_LIMIT = 300

# (Hash, Titel, startet nicht?)
REMOTE = [
    ("a1b2c3d4e5f6", "Akku-Seite mit Diagramm", False),
    ("b2c3d4e5f6a1", "Auto-Antwort auch in Kanälen", False),
    ("c3d4e5f6a1b2", "Experiment: neuer Funk-Treiber (fehlerhaft)", True),
]


class _Mgr:
    name = "demo"


class DemoUpdater:
    def __init__(self, cfg=None):
        self.cfg = cfg
        self.mgr = _Mgr()
        self._lock = threading.Lock()
        self.applied = 0                      # wie viele Remote-Commits eingespielt sind
        self.visible = 2                      # so viele sind schon auf „GitHub“ (der dritte kommt später)
        self.base = "9f8e7d6c5b4a"
        self.running = True
        self.last_good = self.base
        self.previous_good: str | None = None   # letzter guter Stand davor (Ziel von „Zurück“, wenn der aktuelle schon der letzte gute ist)
        self.blocked: str | None = None
        self.update_info: dict | None = None
        self.job = {"running": False, "name": None, "started": None, "finished": None,
                    "ok": None, "message": "", "log": []}
        self._logs = ["[demo] Demo-Programm gestartet"]
        # Protokoll wie im echten Betrieb in SQLite (eigene Datei, damit Demo und echte Daten getrennt bleiben)
        store = None
        try:
            if cfg is not None and getattr(cfg, "data_dir", None):
                store = LogStore(Path(cfg.data_dir) / "demo.db")
        except Exception:
            store = None
        self.rec = JobRecorder(store)
        restored = self.rec.restore()
        if restored:
            self.job = restored

    # ---- Hilfen ----------------------------------------------------------
    @property
    def commit(self) -> str:
        return self.base if self.applied == 0 else REMOTE[self.applied - 1][0]

    def _say(self, text: str) -> None:
        with self._lock:
            now = time.time()
            self.job["log"].append({"ts": now, "text": text})
            del self.job["log"][:-JOB_LOG_LIMIT]
            self._logs.append(f"[demo] {text}")
        self.rec.line(now, text)

    def _pending(self) -> list[tuple]:
        return REMOTE[self.applied:self.visible]

    # ---- Aktionen --------------------------------------------------------
    def start(self, name: str, **kwargs) -> bool:
        with self._lock:
            if self.job["running"]:
                return False
            self.job = {"running": True, "name": name, "started": time.time(), "finished": None,
                        "ok": None, "message": "", "log": []}
            started = self.job["started"]
        self.rec.begin(name, started)
        threading.Thread(target=self._execute, args=(name, kwargs), daemon=True).start()
        return True

    def run(self, name: str, **kwargs):
        if not self.start(name, **kwargs):
            return False, "Es läuft bereits eine Aktion"
        while self.job["running"]:
            time.sleep(0.05)
        return self.job["ok"], self.job["message"]

    def _execute(self, name: str, kwargs: dict) -> None:
        try:
            ok, msg = getattr(self, f"op_{name}")(**kwargs)
        except Exception as e:                     # pragma: no cover
            ok, msg = False, f"Unerwarteter Fehler: {e}"
        finished = time.time()
        with self._lock:
            self.job.update(running=False, finished=finished, ok=ok, message=msg)
        self.rec.end(finished, ok, msg)

    def _wait(self, text: str) -> None:
        self._say(text)
        time.sleep(STEP_S)

    def op_check(self):
        self._wait("Hole Änderungen (simuliert) …")
        pend = self._pending()
        self.update_info = {"checked_at": time.time(), "behind": len(pend),
                            "remote_head": pend[-1][0] if pend else self.commit,
                            "subjects": [f"{h[:7]} {t}" for h, t, _ in reversed(pend)]}
        if not pend:
            return True, "Bereits aktuell"
        msg = f"{len(pend)} neue Änderung(en) verfügbar"
        if self.blocked == pend[-1][0]:
            msg += " – dieser Stand ist als fehlerhaft markiert"
        return True, msg

    def op_update(self, force: bool = False):
        pend = self._pending()
        if not pend:
            return True, "Bereits aktuell"
        target = pend[-1]
        if self.blocked == target[0] and not force:
            return False, "Dieser Stand ist als fehlerhaft markiert (zum Erzwingen „Trotzdem versuchen“)"
        old, old_applied = self.commit, self.applied
        self._wait("Vorprüfung: richtiger Branch, keine lokalen Änderungen – ok")
        self._wait("Programm wird gestoppt …"); self.running = False
        self._wait(f"Spiele {len(pend)} Änderung(en) ein: {old[:7]} → {target[0][:7]}")
        self.applied = self.visible
        self._wait("Programm wird gestartet …")
        if target[2]:
            self._wait("Gesundheitsprüfung: keine Antwort – Update fehlgeschlagen")
            self._wait(f"Setze zurück auf {old[:7]} …")
            self.applied = old_applied
            self.running = True
            self.blocked = target[0]
            self._wait("Gesundheitsprüfung nach Rücksetzen: ok")
            self.update_info = None
            return False, f"Update fehlgeschlagen, auf {old[:7]} zurückgesetzt; {target[0][:7]} als fehlerhaft markiert"
        self.running = True
        self._wait("Gesundheitsprüfung: ok")
        if self.commit != self.last_good:
            self.previous_good = self.last_good
        self.last_good = self.commit
        self.blocked = None
        self.visible = len(REMOTE)             # jetzt erscheint der fehlerhafte dritte Commit
        self.update_info = None
        return True, f"Aktualisiert auf {self.commit[:7]}"

    def op_restart(self):
        self._wait("Programm wird gestoppt …"); self.running = False
        self._wait("Programm wird gestartet …"); self.running = True
        self._wait("Gesundheitsprüfung: ok")
        return True, "Neu gestartet"

    def op_start(self):
        if self.running:
            return True, "Läuft bereits"
        self._wait("Programm wird gestartet …"); self.running = True
        self._wait("Gesundheitsprüfung: ok")
        return True, "Gestartet"

    def op_stop(self):
        self._wait("Programm wird gestoppt …"); self.running = False
        return True, "Gestoppt"

    def op_rollback(self):
        target = self.last_good
        if self.commit == self.last_good:
            if not self.previous_good:
                return False, "Schon auf dem letzten guten Stand"
            target = self.previous_good
        self._wait(f"Setze auf {target[:7]} zurück …")
        self.applied = 0 if target == self.base else \
            1 + [r[0] for r in REMOTE].index(target)
        self.running = True
        self._wait("Gesundheitsprüfung: ok")
        self.last_good, self.previous_good = target, None
        return True, f"Zurückgesetzt auf {target[:7]}"

    # ---- Status ----------------------------------------------------------
    def job_view(self) -> dict:
        with self._lock:
            return {**self.job, "log": list(self.job["log"][-80:])}

    def startup(self) -> None:
        pass

    def status(self) -> dict:
        with self._lock:
            return {"version": __version__, "manager": "demo (simuliert)", "repo_dir": "(Demo – nichts wird verändert)",
                    "branch": "main", "needs_token": False, "app_url": "http://127.0.0.1:8000",
                    "commit": self.commit, "dirty": False,
                    "current_branch": "main", "app": "running" if self.running else "stopped",
                    "last_good": self.last_good, "previous_good": self.previous_good, "blocked": self.blocked,
                    "update_info": self.update_info, "pending": None,
                    "job": {**self.job, "log": list(self.job["log"][-80:])}, "problems": [],
                    "hints": ["Demo: kein token gesetzt (im echten Betrieb empfohlen)."],
                    "usb": {"devices": [{"path": "/dev/ttyACM0", "name": "usb-Heltec_V4-demo (Demo)", "readable": True}],
                            "expect": True, "notes": [], "checked": time.time(), "missing_since": None,
                            "last_seen": time.time(), "problem": None},
                    "system": {"checked": time.time(), "disk": {"path": "/", "total_mb": 29000, "free_mb": 21000, "used_pct": 27.6},
                               "app_db_mb": 3.4, "power": {"undervolt_now": False, "undervolt_ever": False, "throttled_now": False,
                                                           "capped_now": False, "throttled_ever": False, "raw": "0x0"},
                               "power_events": {"sample_s": 2.0, "active": True, "open": False, "count_24h": 3, "count_7d": 5, "count_total": 5,
                                                "missed_24h": 1, "longest_s_24h": 6.0, "total_s_24h": 9.0, "send_known_24h": 3, "send_near_24h": 2,
                                                "last": None,
                                                "recent": [{"start": time.time() - 900, "end": time.time() - 900, "duration_s": None, "lost": False, "kind": "missed", "sends_near": []},
                                                           {"start": time.time() - 5400, "end": time.time() - 5394, "duration_s": 6.0, "lost": False, "kind": "undervolt", "throttled": True,
                                                            "sends_near": [{"dt": -1.4, "kind": "Advert"}]},
                                                           {"start": time.time() - 20000, "end": time.time() - 19997, "duration_s": 3.0, "lost": False, "kind": "undervolt", "throttled": False,
                                                            "sends_near": [{"dt": -0.6, "kind": "Statusabfrage"}, {"dt": -4.2, "kind": "Ping"}]}]},
                               "temp_c": 48.2, "time_synced": True, "tailscale": {"state": "Running", "ok": True},
                               "internet": {"up": True, "down_since": None,
                                            "outages": [{"start": None, "end": time.time() - 30000, "duration_s": 150.0, "clock_jump": True},
                                                        {"start": time.time() - 7200, "end": time.time() - 6000}]},
                               "problems": []},
                    "backup": {"last_ok": time.time() - 3600, "last_error": None, "notes": [], "dir": "data/backups",
                               "extra_dir": None, "files": ["meshcore-demo.db"]}}

    def log_tail(self, lines: int = 100, since: str = "", until: str = "") -> list[str]:
        return self._logs[-lines:]
