"""Unabhängige USB-Anzeige des Watchdogs (nur Standardbibliothek).

Der Watchdog schaut selbst nach, ob ein serielles USB-Gerät (der Node) am Rechner hängt, ohne das Hauptprogramm zu fragen.
So sieht man auch dann, ob das Kabel/der Anschluss das Problem ist, wenn das Hauptprogramm hängt oder kaputt ist.

Läuft nur im Hintergrund-Thread des `StatusCache`, nie im Anfragepfad, und wirft nie.
Nicht an echter Hardware getestet (Gerätenamen unter Linux).
"""
from __future__ import annotations

import glob
import os
import sys
import time

PATTERNS = (
    "/dev/serial/by-id/*",                       # stabile Namen (Linux), zeigen auf ttyACM*/ttyUSB*
    "/dev/ttyACM*", "/dev/ttyUSB*",              # Linux
    "/dev/cu.usbmodem*", "/dev/cu.usbserial*",   # macOS
)
GRACE_S = 30.0       # so lange darf das Gerät fehlen (Neu-Anmeldung am USB-Bus), bevor es als Problem gilt


def scan(expect: bool = False, globber=glob.glob, exists=os.path.exists, access=os.access) -> dict:
    """Liefert {devices, expect, notes, checked}. Wirft nie."""
    devices: dict[str, dict] = {}
    notes: list[str] = []
    try:
        for pattern in PATTERNS:
            for path in sorted(globber(pattern)):
                real = os.path.realpath(path) if "by-id" in pattern else path
                d = devices.setdefault(real, {"path": real, "name": None, "readable": True})
                if "by-id" in pattern:
                    d["name"] = os.path.basename(path)
        if sys.platform.startswith(("linux", "darwin")):
            for d in devices.values():
                try:
                    d["readable"] = bool(access(d["path"], os.R_OK | os.W_OK))
                except Exception:
                    d["readable"] = False
            if devices and not any(d["readable"] for d in devices.values()):
                notes.append("Der Benutzer des Watchdogs darf den Anschluss nicht öffnen (Linux: Gruppe „dialout“). "
                             "Das Hauptprogramm kann unter einem anderen Benutzer laufen und trotzdem Zugriff haben.")
    except Exception as e:
        notes.append(f"USB-Prüfung nicht möglich ({type(e).__name__}: {e})")
    return {"devices": list(devices.values()), "expect": bool(expect), "notes": notes, "checked": time.time()}


class UsbWatch:
    """Merkt sich, seit wann das Gerät fehlt, und macht daraus (nur wenn erwartet) einen Hinweis für `problems`."""

    def __init__(self, expect: bool = False, scanner=scan, clock=time.time):
        self.expect, self._scan, self._clock = expect, scanner, clock
        self.missing_since: float | None = None
        self.last_seen: float | None = None
        self.last: dict | None = None
        self.maintenance = None               # optional: Funktion, die True liefert, solange Wartung ist (dann keine Warnung)

    def refresh(self) -> None:
        now = self._clock()
        try:
            snap = self._scan(self.expect)
        except Exception as e:                                   # auch ein kaputter Scanner stoppt nichts
            snap = {"devices": [], "expect": self.expect, "notes": [f"USB-Prüfung fehlgeschlagen ({e})"], "checked": now}
            self.last = snap
            return
        if snap["devices"]:
            self.missing_since, self.last_seen = None, now
        elif self.missing_since is None:
            self.missing_since = now
        self.last = snap

    def view(self) -> dict | None:
        if self.last is None:
            return None
        out = dict(self.last)
        out["missing_since"] = self.missing_since
        out["last_seen"] = self.last_seen
        out["problem"] = self.problem()
        return out

    def problem(self) -> str | None:
        if not self.expect or self.last is None or self.missing_since is None:
            return None
        try:
            if self.maintenance and self.maintenance():
                return None
        except Exception:
            pass
        gone = self._clock() - self.missing_since
        if gone < GRACE_S:
            return None
        return (f"Kein USB-Node am Rechner erkannt (seit {int(gone)} s). Kabel, Anschluss und Stromversorgung prüfen; "
                f"das Hauptprogramm verbindet sich nach dem Einstecken von selbst wieder.")
