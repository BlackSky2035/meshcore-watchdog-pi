"""Ausfallsicherheit des Watchdogs: Status-Zwischenspeicher, Notbetrieb, systemd-Herzschlag.

Ziel: **Die Weboberfläche des Watchdogs bleibt immer erreichbar**, auch wenn Git hängt, der Dienst-Manager
nicht antwortet, die Konfiguration kaputt ist oder eine Funktion einen Fehler wirft. Fehler werden als
Meldung angezeigt statt die Seite zu verlieren.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
import urllib.request

from . import __version__
from .usb import UsbWatch

log = logging.getLogger("watchdog.health")


class StatusCache:
    """Berechnet den Status im Hintergrund. Anfragen an die Weboberfläche warten nie auf Git,
    systemctl oder die Gesundheits-URL, sondern bekommen sofort den letzten Stand (mit Alter)."""

    STALE_WARN_S = 30.0

    def __init__(self, updater, interval: float = 3.0, usb_expect: bool = False, usb_watch=None, sys_watch=None, backup=None, maintenance=None):
        self.updater = updater
        self.usb = usb_watch if usb_watch is not None else UsbWatch(usb_expect)
        self.maint = maintenance
        if maintenance is not None:
            self.usb.maintenance = maintenance.active
        self.sys, self.backup = sys_watch, backup           # eigene Threads (siehe sysmon.py/backup.py); optional
        self.interval = interval
        self._snap: dict | None = None
        self._at = 0.0
        self._error: str | None = None
        self._first_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        for extra in (self.sys, self.backup):
            try:
                if extra:
                    extra.start()
            except Exception:
                log.exception("Hintergrundprüfung konnte nicht starten")
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="status-cache", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for extra in (self.sys, self.backup):
            try:
                if extra:
                    extra.stop()
            except Exception:
                pass

    def poke(self) -> None:
        """Nach einer Aktion sofort neu berechnen."""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except BaseException:                       # nichts darf diesen Thread beenden
                log.exception("Status-Zwischenspeicher: unerwarteter Fehler")
            self._wake.wait(self.interval)
            self._wake.clear()

    def refresh(self) -> None:
        try:
            self.usb.refresh()                          # eigene Sicherung: ein USB-Fehler darf den Status nicht kippen
        except Exception:
            log.exception("USB-Prüfung fehlgeschlagen")
        try:
            if self.maint is not None:
                info = getattr(self.updater, "version_info", None)
                self.maint.observe(info.get("maintenance_until") if isinstance(info, dict) else None)
        except Exception:
            log.exception("Wartung konnte nicht abgeglichen werden")
        try:
            snap = self.updater.status()
            self._snap, self._at, self._error = snap, time.time(), None
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
            log.warning("Status konnte nicht ermittelt werden: %s", self._error)

    def get(self) -> dict:
        if self._snap is None:                          # allererste Anfrage: kurz auf einen Stand warten
            with self._first_lock:
                if self._snap is None:
                    t = threading.Thread(target=self.refresh, daemon=True)
                    t.start()
                    t.join(5.0)
        snap = dict(self._snap) if self._snap else {
            "version": __version__, "app": "unknown", "manager": "?", "repo_dir": "", "branch": "",
            "needs_token": False, "last_good": None, "blocked": None, "update_info": None}
        problems = list(snap.get("problems") or [])
        age = (time.time() - self._at) if self._snap else None
        if self._error:
            problems.append(f"Statusabfrage fehlgeschlagen ({self._error})")
        if age is not None and age > self.STALE_WARN_S:
            problems.append(f"Die Statusdaten sind {int(age)} s alt: Git oder die Dienstverwaltung antwortet nicht.")
        if self._snap is None and not self._error:
            problems.append("Status wird noch ermittelt …")
        try:
            usb_view = snap.get("usb") or self.usb.view()   # (die Demo bringt ihre eigene Anzeige mit)
            if usb_view:
                snap["usb"] = usb_view
                if usb_view.get("problem"):
                    problems.append(usb_view["problem"])
        except Exception:
            log.exception("USB-Anzeige fehlgeschlagen")
        try:
            sysv = snap.get("system") or (self.sys.view() if self.sys else None)      # (die Demo bringt eigene Werte mit)
            if sysv:
                snap["system"] = sysv
                problems.extend(sysv.get("problems") or [])
            if self.backup and "backup" not in snap:
                snap["backup"] = self.backup.view()
                bp = self.backup.problem()
                if bp:
                    problems.append(bp)
        except Exception:
            log.exception("Systemanzeige fehlgeschlagen")
        try:
            if self.maint is not None:
                snap["maintenance"] = self.maint.view()
        except Exception:
            log.exception("Wartungsanzeige fehlgeschlagen")
        snap["problems"] = problems
        snap["stale_s"] = None if age is None else round(age, 1)
        try:                                            # der Lauf (Ablauf-Anzeige) ist immer aktuell
            snap["job"] = self.updater.job_view()
        except Exception:
            snap.setdefault("job", {"running": False, "name": None, "started": None, "finished": None,
                                    "ok": None, "message": "", "log": []})
        return snap


class FallbackUpdater:
    """Notbetrieb: Der Watchdog läuft und zeigt, was fehlt, kann aber nichts ausführen.
    Wird benutzt, wenn Konfiguration oder Verwaltung nicht aufgebaut werden konnten."""

    def __init__(self, reason: str):
        self.unavailable = reason
        self.cfg = None
        self.mgr = type("M", (), {"name": "nicht verfügbar"})()
        self.job = {"running": False, "name": None, "started": None, "finished": None,
                    "ok": False, "message": reason, "log": []}

    def start(self, name: str, **kw) -> bool:
        return False

    def run(self, name: str, **kw):
        return False, self.unavailable

    def startup(self) -> None:
        pass

    def job_view(self) -> dict:
        return dict(self.job)

    def log_tail(self, lines: int = 100, since: str = "", until: str = "") -> list[str]:
        return []

    def status(self) -> dict:
        return {"version": __version__, "manager": "nicht verfügbar", "repo_dir": "", "branch": "",
                "needs_token": False, "app": "unknown", "app_url": "", "last_good": None, "blocked": None,
                "update_info": None, "job": self.job_view(),
                "problems": [f"Notbetrieb: {self.unavailable}"]}


# ---- systemd-Herzschlag ----------------------------------------------------
def sd_notify(message: str) -> bool:
    """Sendet eine Nachricht an systemd (nur wenn NOTIFY_SOCKET gesetzt ist). Wirft nie."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return False
    try:
        if path.startswith("@"):
            path = "\0" + path[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.settimeout(1.0)
            s.connect(path)
            s.sendall(message.encode())
        return True
    except Exception:
        return False


def selfcheck(host: str, port: int, timeout: float = 5.0) -> bool:
    """Fragt den eigenen Server wirklich per HTTP. Ist er hängengeblieben, schlägt das fehl."""
    host = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/ping", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


class Heartbeat(threading.Thread):
    """Meldet systemd alle `interval` s „ich lebe“, aber NUR wenn die Selbstprüfung klappt.
    Antwortet der Server nicht mehr, bleibt das Signal aus und systemd startet den Watchdog neu
    (Unit-Einstellung WatchdogSec). Ohne systemd passiert nichts außer einer Logzeile."""

    def __init__(self, get_port, host: str, interval: float = 15.0):
        super().__init__(name="heartbeat", daemon=True)
        self.get_port, self.host, self.interval = get_port, host, interval
        self.failures = 0
        self._stop_evt = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    def beat(self) -> bool:
        ok = False
        try:
            ok = selfcheck(self.host, self.get_port())
        except Exception:
            ok = False
        if ok:
            self.failures = 0
            sd_notify("WATCHDOG=1")
        else:
            self.failures += 1
            log.error("Selbstprüfung fehlgeschlagen (%d in Folge) – systemd startet den Watchdog gleich neu, "
                      "wenn das so bleibt", self.failures)
        return ok

    def run(self) -> None:
        while not self._stop_evt.wait(self.interval):
            try:
                self.beat()
            except BaseException:
                log.exception("Herzschlag: unerwarteter Fehler")
