"""Tägliche Sicherung der Datenbank des Hauptprogramms (SQLite) – nur Standardbibliothek, wirft nie.

- Nutzt die SQLite-Sicherungsfunktion (konsistent auch bei laufendem Programm, WAL-fähig); die Quelle wird nur gelesen.
- Ziel: `backup_dir` (Standard data/backups), optional zusätzlich `backup_extra_dir` (z. B. USB-Stick). Fehlt das zweite Ziel
  (Stick nicht eingesteckt), ist das nur ein Hinweis, die erste Kopie zählt.
- Läuft in einem eigenen Thread; prüft stündlich, ob die letzte Sicherung älter als `interval_s` ist.
- Es bleiben die letzten `keep` Dateien je Ziel.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("watchdog.backup")

CHECK_EVERY_S = 3600.0
STALE_AFTER_S = 2 * 86400


def copy_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=5)
    try:
        d = sqlite3.connect(tmp)
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()
    tmp.replace(dst)


class Backup:
    def __init__(self, src: str, backup_dir: str, extra_dir: str = "", keep: int = 7, interval_s: float = 86400.0,
                 clock=time.time):
        self.src, self.dir, self.extra = Path(src) if src else None, Path(backup_dir), (Path(extra_dir) if extra_dir else None)
        self.keep, self.interval_s, self._clock = max(1, int(keep)), interval_s, clock
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self.notes: list[str] = []
        self.files: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._scan_existing()

    def _scan_existing(self) -> None:
        try:
            found = sorted(self.dir.glob("meshcore-*.db"))
            if found:
                self.last_ok = found[-1].stat().st_mtime
                self.files = [f.name for f in found][-self.keep:]
        except Exception:
            pass

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="backup", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        self._stop.wait(60)                                  # nach dem Start nicht sofort, das Programm fährt gerade hoch
        while not self._stop.is_set():
            try:
                self.run_if_due()
            except BaseException:
                log.exception("Sicherung: unerwarteter Fehler")
            self._stop.wait(CHECK_EVERY_S)

    def run_if_due(self) -> bool:
        if self.last_ok is not None and self._clock() - self.last_ok < self.interval_s:
            return False
        return self.run()

    def run(self) -> bool:
        """Eine Sicherung jetzt. Gibt True zurück, wenn mindestens das Hauptziel geklappt hat."""
        self.notes = []
        try:
            if self.src is None or not self.src.is_file():
                self.last_error = None
                self.notes.append("Noch keine Datenbank des Hauptprogramms vorhanden (nichts zu sichern).")
                return False
            name = f"meshcore-{datetime.fromtimestamp(self._clock()):%Y%m%d-%H%M%S}.db"
            copy_db(self.src, self.dir / name)
            self._prune(self.dir)
            self.last_ok, self.last_error = self._clock(), None
            self.files = sorted(p.name for p in self.dir.glob("meshcore-*.db"))[-self.keep:]
            if self.extra:
                try:
                    if self.extra.is_dir():
                        copy_db(self.src, self.extra / name)
                        self._prune(self.extra)
                    else:
                        self.notes.append(f"Zweites Ziel {self.extra} nicht vorhanden (Stick nicht eingesteckt?).")
                except Exception as e:
                    self.notes.append(f"Zweites Ziel fehlgeschlagen: {type(e).__name__}: {e}")
            return True
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning("Sicherung fehlgeschlagen: %s", self.last_error)
            return False

    def _prune(self, folder: Path) -> None:
        try:
            for old in sorted(folder.glob("meshcore-*.db"))[:-self.keep]:
                old.unlink()
        except Exception:
            pass

    def view(self) -> dict:
        return {"last_ok": self.last_ok, "last_error": self.last_error, "notes": list(self.notes), "files": list(self.files),
                "dir": str(self.dir), "extra_dir": str(self.extra) if self.extra else None}

    def problem(self) -> str | None:
        if self.last_error:
            return f"Datenbanksicherung fehlgeschlagen: {self.last_error}"
        if self.last_ok is not None and self._clock() - self.last_ok > STALE_AFTER_S:
            return "Die letzte Datenbanksicherung ist über 2 Tage alt."
        return None
