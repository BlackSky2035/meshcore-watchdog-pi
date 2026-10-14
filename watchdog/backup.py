"""Tägliche Sicherung der Datenbank des Hauptprogramms (SQLite) – nur Standardbibliothek, wirft nie.

- Nutzt die SQLite-Sicherungsfunktion (konsistent auch bei laufendem Programm, WAL-fähig); die Quelle wird nur gelesen.
- Ziel: `backup_dir` (Standard data/backups), optional zusätzlich `backup_extra_dir` (z. B. USB-Stick). Fehlt das zweite Ziel
  (Stick nicht eingesteckt), ist das nur ein Hinweis, die erste Kopie zählt.
- Läuft in einem eigenen Thread; prüft stündlich, ob die letzte Sicherung älter als `interval_s` ist.
- Es bleiben die letzten `keep` Dateien je Ziel.
- Halbfertige Kopien (`meshcore-*.tmp`) werden bei einem Fehler sofort, beim Start und vor jeder Sicherung gelöscht.
- Vor der Sicherung wird der freie Platz geprüft (frei ≥ 2 × Datenbankgröße + `MIN_FREE_EXTRA`); reicht er nicht, wird die
  Sicherung ausgelassen und als Problem gemeldet (nie geworfen), damit die Sicherung nicht die Karte vollschreibt.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("watchdog.backup")

CHECK_EVERY_S = 3600.0
STALE_AFTER_S = 2 * 86400
SKIPPED = "Sicherung ausgelassen: "
MIN_FREE_EXTRA = 200 * 1024 * 1024        # so viel muss nach der Kopie (Reserve) mindestens noch frei sein


def copy_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    try:
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
    except BaseException:
        try:                                                # halbe Kopie nie liegen lassen (sie wurde sonst nie aufgeräumt)
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def remove_stale_tmp(folder: Path) -> int:
    """Löscht übrig gebliebene halbfertige Kopien (meshcore-*.tmp, z. B. nach Stromausfall). Wirft nie."""
    n = 0
    try:
        for f in folder.glob("meshcore-*.tmp"):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    except Exception:
        pass
    return n


def space_problem(src: Path, folder: Path, disk_usage=shutil.disk_usage) -> str | None:
    """None, wenn genug Platz für eine Kopie ist, sonst ein Satz für die Anzeige. Wirft nie: Ist der Platz nicht messbar,
    wird die Sicherung versucht (der Kopierfehler würde dann gemeldet)."""
    try:
        size = src.stat().st_size
        for extra in ("-wal",):                              # die WAL-Datei gehört mit zur Datenbank
            try:
                size += Path(str(src) + extra).stat().st_size
            except OSError:
                pass
        probe = folder
        while not probe.exists() and probe != probe.parent:   # Ziel gibt es noch nicht: auf dem Elternordner messen
            probe = probe.parent
        free = disk_usage(probe).free
    except Exception as e:
        log.warning("Freier Platz für die Sicherung nicht messbar: %s", e)
        return None
    need = 2 * size + MIN_FREE_EXTRA
    if free < need:
        mb = 1024 * 1024
        return (f"zu wenig freier Platz in {folder} ({free // mb} MB frei, nötig {need // mb} MB: "
                f"2 × Datenbank + {MIN_FREE_EXTRA // mb} MB Reserve)")
    return None


class Backup:
    def __init__(self, src: str, backup_dir: str, extra_dir: str = "", keep: int = 7, interval_s: float = 86400.0,
                 clock=time.time, disk_usage=shutil.disk_usage):
        self.src, self.dir, self.extra = Path(src) if src else None, Path(backup_dir), (Path(extra_dir) if extra_dir else None)
        self.keep, self.interval_s, self._clock = max(1, int(keep)), interval_s, clock
        self._disk_usage = disk_usage
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self.notes: list[str] = []
        self.files: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._scan_existing()

    def _scan_existing(self) -> None:
        for folder in (self.dir, self.extra):               # Reste eines abgebrochenen Laufs (Stromausfall) beim Start löschen
            if folder is not None:
                remove_stale_tmp(folder)
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
            remove_stale_tmp(self.dir)
            short = space_problem(self.src, self.dir, self._disk_usage)
            if short:
                self.last_error = f"{SKIPPED}{short}"
                log.warning("Sicherung ausgelassen: %s", short)
                return False
            copy_db(self.src, self.dir / name)
            self._prune(self.dir)
            self.last_ok, self.last_error = self._clock(), None
            self.files = sorted(p.name for p in self.dir.glob("meshcore-*.db"))[-self.keep:]
            if self.extra:
                try:
                    if self.extra.is_dir():
                        remove_stale_tmp(self.extra)
                        short = space_problem(self.src, self.extra, self._disk_usage)
                        if short:
                            self.notes.append(f"Zweites Ziel ausgelassen: {short}")
                        else:
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
        if self.last_error and self.last_error.startswith(SKIPPED):
            return f"Datenbanksicherung ausgelassen: {self.last_error[len(SKIPPED):]}"
        if self.last_error:
            return f"Datenbanksicherung fehlgeschlagen: {self.last_error}"
        if self.last_ok is not None and self._clock() - self.last_ok > STALE_AFTER_S:
            return "Die letzte Datenbanksicherung ist über 2 Tage alt."
        return None
