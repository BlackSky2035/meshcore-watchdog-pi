"""Ablaufprotokoll in einer SQLite-Datei, damit es einen Neustart des Geräts übersteht.

Oberste Regel: **Die Datenbank darf den Watchdog nie zum Absturz bringen oder blockieren.**
Darum
- fängt jede Methode alle Fehler ab (Platte voll, gesperrt, kaputte Datei, falsche Typen, …),
- ist der Speicher nur eine Zugabe: ohne Datenbank läuft alles wie bisher im Arbeitsspeicher,
- wird eine beschädigte Datei zur Seite gelegt (`*.corrupt-<zeit>`) und neu angelegt,
- wird nach Fehlern höchstens alle 60 s ein neuer Versuch gemacht (kein Dauerfeuer, kein Log-Flut),
- warten Schreibzugriffe höchstens 2 s auf eine Sperre,
- werden gelesene Zeilen defensiv geprüft (kaputte Zeilen werden übersprungen).
Nur Standardbibliothek.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("watchdog.logstore")

KEEP_JOBS = 50            # so viele Läufe bleiben gespeichert
MAX_LINES_PER_JOB = 300
MAX_TEXT = 2000           # längere Zeilen werden gekürzt
RETRY_S = 60.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, started REAL NOT NULL, finished REAL, ok INTEGER, message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS job_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL, ts REAL NOT NULL, text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_log_job ON job_log(job_id);
"""


class LogStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._retry_at = 0.0
        self._warned = False
        with self._lock:
            self._connect()

    # ---- Verbindung ------------------------------------------------------
    @property
    def available(self) -> bool:
        return self._db is not None

    def _fail(self, what: str, err: Exception) -> None:
        if not self._warned:                       # nur einmal melden, nicht bei jeder Zeile
            log.warning("Protokoll-Datenbank: %s fehlgeschlagen (%s) – weiter ohne Speicherung", what, err)
            self._warned = True
        try:
            if self._db is not None:
                self._db.close()
        except Exception:
            pass
        self._db = None
        self._retry_at = time.monotonic() + RETRY_S

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.path), timeout=2.0, check_same_thread=False)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA busy_timeout=2000")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("quick_check meldet Fehler")
            db.executescript(SCHEMA)
            db.commit()
            return db
        except Exception:
            db.close()
            raise

    def _quarantine(self) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(self.path) + suffix)
            try:
                if src.exists():
                    src.rename(Path(f"{self.path}{suffix}.corrupt-{stamp}"))
            except OSError:
                try:
                    src.unlink()
                except OSError:
                    pass
        log.warning("Protokoll-Datenbank war beschädigt und wurde zur Seite gelegt (%s.corrupt-%s)", self.path, stamp)

    def _connect(self) -> None:
        """Öffnet die Datenbank; bei einer kaputten Datei einmal neu anlegen. Wirft nie."""
        for attempt in (1, 2):
            try:
                self._db = self._open()
                self._warned = False
                return
            except sqlite3.DatabaseError as e:     # kaputte Datei: aussortieren und neu versuchen
                if attempt == 1:
                    self._quarantine()
                    continue
                self._fail("Öffnen", e)
                return
            except Exception as e:                 # Platte voll, keine Rechte, …
                self._fail("Öffnen", e)
                return

    def _usable(self) -> bool:
        if self._db is not None:
            return True
        if time.monotonic() >= self._retry_at:
            self._connect()
        return self._db is not None

    def _run(self, what: str, fn, default=None):
        """Führt fn(db) geschützt aus. Jeder Fehler wird geschluckt und ergibt `default`."""
        try:
            with self._lock:
                if not self._usable():
                    return default
                try:
                    return fn(self._db)
                except Exception as e:
                    self._fail(what, e)
                    return default
        except Exception:                          # sogar das Sperren darf nichts zerstören
            return default

    # ---- Schreiben -------------------------------------------------------
    def start_job(self, name: str, started: float) -> int | None:
        def go(db):
            cur = db.execute("INSERT INTO jobs(name, started) VALUES(?,?)", (str(name)[:60], float(started)))
            job_id = cur.lastrowid
            db.execute("DELETE FROM job_log WHERE job_id NOT IN "
                       "(SELECT id FROM jobs ORDER BY id DESC LIMIT ?)", (KEEP_JOBS,))
            db.execute("DELETE FROM jobs WHERE id NOT IN (SELECT id FROM jobs ORDER BY id DESC LIMIT ?)", (KEEP_JOBS,))
            db.commit()
            return job_id
        return self._run("Lauf anlegen", go)

    def add_line(self, job_id: int | None, ts: float, text: str) -> None:
        if job_id is None:
            return

        def go(db):
            db.execute("INSERT INTO job_log(job_id, ts, text) VALUES(?,?,?)", (job_id, float(ts), str(text)[:MAX_TEXT]))
            db.execute("DELETE FROM job_log WHERE job_id=? AND id NOT IN "
                       "(SELECT id FROM job_log WHERE job_id=? ORDER BY id DESC LIMIT ?)",
                       (job_id, job_id, MAX_LINES_PER_JOB))
            db.commit()
        self._run("Zeile schreiben", go)

    def finish_job(self, job_id: int | None, finished: float, ok: bool, message: str) -> None:
        if job_id is None:
            return

        def go(db):
            db.execute("UPDATE jobs SET finished=?, ok=?, message=? WHERE id=?",
                       (float(finished), int(bool(ok)), str(message)[:MAX_TEXT], job_id))
            db.commit()
        self._run("Lauf abschließen", go)

    # ---- Lesen -----------------------------------------------------------
    def last_job(self) -> dict | None:
        """Der zuletzt gespeicherte Lauf samt Zeilen, im Format von `Updater.job`; sonst None."""
        def go(db):
            row = db.execute("SELECT id, name, started, finished, ok, message FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return None
            job_id, name, started, finished, ok, message = row
            lines = []
            for ts, text in db.execute("SELECT ts, text FROM job_log WHERE job_id=? ORDER BY id DESC LIMIT ?",
                                       (job_id, MAX_LINES_PER_JOB)).fetchall()[::-1]:
                try:
                    lines.append({"ts": float(ts), "text": str(text)})
                except (TypeError, ValueError):
                    continue                       # kaputte Zeile überspringen
            return {"id": job_id, "name": str(name), "started": float(started),
                    "finished": None if finished is None else float(finished),
                    "ok": None if ok is None else bool(ok), "message": str(message or ""), "log": lines}
        return self._run("Lesen", go)

    def list_jobs(self, limit: int = KEEP_JOBS) -> list[dict]:
        """Zusammenfassung der letzten Läufe (ohne Zeilen), neueste zuerst – für eine scrollbare Liste."""
        def go(db):
            rows = db.execute("SELECT id, name, started, finished, ok, message FROM jobs ORDER BY id DESC LIMIT ?",
                              (max(1, min(limit, KEEP_JOBS)),)).fetchall()
            out = []
            for job_id, name, started, finished, ok, message in rows:
                try:
                    out.append({"id": int(job_id), "name": str(name), "started": float(started),
                               "finished": None if finished is None else float(finished),
                               "ok": None if ok is None else bool(ok), "message": str(message or "")})
                except (TypeError, ValueError):
                    continue
            return out
        return self._run("Liste lesen", go, default=[])

    def job_lines(self, job_id: int, limit: int = MAX_LINES_PER_JOB) -> list[dict]:
        """Zeilen eines einzelnen vergangenen Laufs."""
        def go(db):
            lines = []
            for ts, text in db.execute("SELECT ts, text FROM job_log WHERE job_id=? ORDER BY id DESC LIMIT ?",
                                       (job_id, max(1, min(limit, MAX_LINES_PER_JOB)))).fetchall()[::-1]:
                try:
                    lines.append({"ts": float(ts), "text": str(text)})
                except (TypeError, ValueError):
                    continue
            return lines
        return self._run("Zeilen lesen", go, default=[])

    def close(self) -> None:
        try:
            with self._lock:
                if self._db is not None:
                    self._db.close()
                self._db = None
        except Exception:
            pass


class JobRecorder:
    """Verbindet einen laufenden Job mit der Datenbank. Alle Methoden sind ausfallsicher."""

    def __init__(self, store: LogStore | None):
        self.store = store
        self.job_id: int | None = None

    def restore(self) -> dict | None:
        """Letzten Lauf laden. War er unterbrochen (kein Ende gespeichert), wird das vermerkt."""
        try:
            job = self.store.last_job() if self.store else None
            if not job:
                return None
            if job["finished"] is None:
                msg = "Unterbrochen: der Watchdog oder das Gerät wurde währenddessen neu gestartet"
                job.update(finished=time.time(), ok=False, message=msg)
                self.store.finish_job(job["id"], job["finished"], False, msg)
            job.pop("id", None)
            job["running"] = False
            return job
        except Exception as e:
            log.warning("Letzten Lauf laden fehlgeschlagen: %s", e)
            return None

    def begin(self, name: str, started: float) -> None:
        try:
            self.job_id = self.store.start_job(name, started) if self.store else None
        except Exception:
            self.job_id = None

    def line(self, ts: float, text: str) -> None:
        try:
            if self.store:
                self.store.add_line(self.job_id, ts, text)
        except Exception:
            pass

    def end(self, finished: float, ok: bool, message: str) -> None:
        try:
            if self.store:
                self.store.finish_job(self.job_id, finished, ok, message)
        except Exception:
            pass
