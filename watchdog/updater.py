"""Aktionen des Watchdogs: prüfen, aktualisieren, neu starten, zurückrollen.

Der Update-Ablauf ist so gebaut, dass ein fehlerhafter Push nie den einzigen
Fernzugriff kappt:

  1. Vorprüfungen (richtiger Branch, keine lokalen Änderungen), dann `git fetch`.
     Bis hierhin wurde nichts angehalten, ein Netzfehler ist also harmlos.
  2. Programm stoppen, Fast-Forward einspielen, bei geänderter requirements.txt
     Abhängigkeiten installieren, Programm starten.
  3. Gesundheitsprüfung: Dienst aktiv UND Health-URL antwortet UND bleibt kurz stabil.
  4. Schlägt etwas fehl: zurück auf den vorherigen Commit, neu starten, prüfen. Der
     fehlerhafte Stand wird als "blocked" gemerkt und nicht erneut automatisch versucht
     (erst NACH der Wiederherstellung, damit ein Schreibfehler sie nie verhindert).
  5. Vor dem Stoppen steht {"pending": {prev, target}} in state.json. Wird der Watchdog oder der
     Pi mitten im Update neu gestartet, stellt `startup()` den vorherigen Stand wieder her.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import shlex
import subprocess
import threading
import time
import traceback
import urllib.request
from pathlib import Path
from typing import Callable

from . import __version__, gitops
from .config import Config
from .gitops import GitError
from .logstore import JobRecorder, LogStore
from .managers import STOP_MARGIN_S, Manager, ManagerError

log = logging.getLogger("watchdog")

JOB_LOG_LIMIT = 300
MAX_JOB_S = 1800          # ein Lauf, der länger „läuft“, gilt als hängengeblieben und blockiert nichts mehr
POLL_S = 0.5


def max_job_s(cfg) -> float:
    """Zeit, nach der ein Lauf als hängengeblieben gilt: mindestens MAX_JOB_S, aber immer länger als der schlimmste Fall eines Updates
    MIT Zurückspielen (je zweimal Stoppen, Abhängigkeiten installieren, Startprüfung, dazu Git-Aufrufe und Reserve). Wer z. B.
    `install_timeout_s` hochsetzt, bekommt so nie einen zweiten Lauf parallel zu einem noch arbeitenden ersten."""
    try:
        stop = max(60.0, float(cfg.stop_timeout_s) + STOP_MARGIN_S)
        worst = 2 * (stop + float(cfg.install_timeout_s) + float(cfg.health_timeout_s)) + 6 * float(cfg.git_timeout_s) + 120
        return max(float(MAX_JOB_S), worst)
    except Exception:                         # Notbetrieb/kaputte Werte: fester Wert
        return float(MAX_JOB_S)
HEALTH_POLL_IDLE_S = 30.0    # Gesundheits-URL des Hauptprogramms nur so oft wirklich abfragen (reine Status-Anzeige)
HEALTH_POLL_BUSY_S = 2.0     # ... außer während eines laufenden Vorgangs (check/update/restart): dann öfter
MAX_RECOVER_TRIES = 3        # so oft wird ein unterbrochenes Update beim Start wiederhergestellt, dann aufgegeben (kein Neustart-Kreislauf)


class OpError(Exception):
    """Erwarteter Fehler einer Aktion (wird dem Nutzer als Meldung gezeigt)."""


def _resolve_exe(command: str, cwd: str) -> list[str]:
    parts = shlex.split(command)
    if parts and "/" in parts[0] and not os.path.isabs(parts[0]):
        parts[0] = os.path.join(cwd, parts[0])
    return parts


class Updater:
    def __init__(self, cfg: Config, manager: Manager,
                 health: Callable[[], bool] | None = None):
        self.cfg = cfg
        self.mgr = manager
        self.version_info: dict | None = None       # letzte Antwort von /api/version (z. B. maintenance_until)
        self.version_info_at = 0.0                   # wann (time.monotonic) diese Antwort kam
        self._health = health or self._http_health
        self._last_health_at = 0.0
        self._last_health_ok = False
        self._was_active = False
        self._was_job_running = False
        self._lock = threading.Lock()
        self.job: dict = {"running": False, "name": None, "started": None,
                          "finished": None, "ok": None, "message": "", "log": []}
        self._state_path = Path(cfg.data_dir) / "state.json"
        self._lock_path = Path(cfg.data_dir) / "op.lock"
        self._lockfh = None
        # Jeder Lauf bekommt eine Nummer: ein als hängend freigegebener Lauf, der später doch endet, darf den
        # neuen Lauf weder beenden noch dessen Sperre lösen noch Zeilen hineinschreiben (siehe _finish/_release/_say).
        self._gen = 0
        self._started_mono = time.monotonic()      # Laufzeituhr: die Wanduhr des Pi springt beim Start (keine Echtzeituhr)
        self._tls = threading.local()
        Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
        self.state: dict = self._load_state()
        # Protokoll in SQLite: übersteht Neustarts; Fehler dort dürfen nie etwas anderes stören
        self.rec = JobRecorder(self._open_logstore(cfg))
        restored = self.rec.restore()
        if restored:
            self.job = restored

    @staticmethod
    def _open_logstore(cfg: Config) -> LogStore | None:
        try:
            return LogStore(Path(cfg.data_dir) / "watchdog.db")
        except Exception as e:                    # sollte nie passieren, LogStore fängt selbst alles ab
            log.warning("Protokoll-Datenbank nicht verfügbar: %s", e)
            return None

    # ------------------------------------------------------------------ Zustand
    def _load_state(self) -> dict:
        state = {"last_good": None, "previous_good": None, "blocked": None, "update_info": None, "pending": None}
        try:
            data = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return state
        if isinstance(data, dict):
            state.update(data)
        else:                                        # gültiges JSON, aber kein Objekt (z. B. von Hand bearbeitet)
            log.warning("state.json enthält kein JSON-Objekt – wird ignoriert")
        return state

    def _set_state(self, **changes) -> bool:
        """Ändert den Zustand im Speicher und schreibt ihn nach state.json. Wirft nie: Ist die Platte voll, gilt die Änderung
        trotzdem für diesen Lauf, und der Ablauf (z. B. die Wiederherstellung nach einem Fehlschlag) geht weiter. False = nicht gespeichert."""
        with self._lock:
            self.state.update(changes)
            try:
                tmp = self._state_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.state, default=str))
                os.replace(tmp, self._state_path)      # atomar: nie eine halbe Datei
                return True
            except Exception as e:
                log.error("Zustand (state.json) nicht gespeichert: %s", e)
                return False

    def _mark_good(self) -> None:
        """HEAD als letzten guten Stand merken. Der bisherige letzte gute Stand wird zu `previous_good`, damit „Zurück“ auch nach
        einem Update hilft, das zwar läuft, aber unerwünscht ist. Ein als fehlerhaft markierter Stand wird nie `previous_good`. Wirft nie."""
        try:
            head = gitops.head(self.cfg.repo_dir)
            old = self.state.get("last_good")
            if head == old:
                return
            prev = old if old and old != self.state.get("blocked") else self.state.get("previous_good")
            if prev == head:
                prev = None
            self._set_state(last_good=head, previous_good=prev)
        except Exception as e:
            log.warning("Letzter guter Stand nicht gemerkt: %s", e)

    # ------------------------------------------------------------------ Jobs
    def _say(self, text: str) -> None:
        log.info(text)
        now = time.time()
        gen = getattr(self._tls, "gen", None)
        with self._lock:
            if gen is not None and gen != self._gen:
                return                               # Zeile eines freigegebenen Laufs: nur ins Log, nicht in den neuen Lauf
            self.job["log"].append({"ts": now, "text": text})
            del self.job["log"][:-JOB_LOG_LIMIT]
        self.rec.line(now, text)

    def start(self, name: str, **kwargs) -> bool:
        """Startet eine Aktion im Hintergrund. False, wenn schon eine läuft (oder kein Thread startbar ist)."""
        gen = self._begin(name)
        if not gen:
            return False
        try:
            threading.Thread(target=self._execute, args=(name, kwargs, gen), name=f"job-{name}", daemon=True).start()
        except Exception as e:                       # z. B. keine Threads mehr möglich: Sperre sofort wieder frei
            log.error("Lauf %r nicht startbar: %s", name, e)
            self._finish(False, f"Lauf nicht startbar: {type(e).__name__}: {e}", gen)
            return False
        return True

    def run(self, name: str, **kwargs) -> tuple[bool, str]:
        """Führt eine Aktion synchron aus (Tests, Kommandozeile)."""
        gen = self._begin(name)
        if not gen:
            return False, "Es läuft bereits eine Aktion"
        return self._execute(name, kwargs, gen)

    def _begin(self, name: str) -> int | bool:
        """Belegt die Sperre. Gibt die Nummer des neuen Laufs zurück (immer ≥ 1) oder False."""
        with self._lock:
            if self.job["running"]:
                limit = max_job_s(self.cfg)
                if time.monotonic() - self._started_mono < limit:
                    return False
                # Hängengebliebener Lauf (Thread tot oder festgefahren): freigeben, damit der Watchdog
                # nie dauerhaft „es läuft bereits eine Aktion“ meldet
                log.error("Lauf %r hängt seit über %d s – wird freigegeben", self.job.get("name"), limit)
                self.job.update(running=False, finished=time.time(), ok=False,
                                message="Abgebrochen: der Lauf hat nicht mehr reagiert")
                self._release_locked()
            # Dateisperre: verhindert auch parallele Aktionen aus einem zweiten Prozess
            # (z. B. Kommandozeile während der Server läuft).
            try:
                fh = open(self._lock_path, "w")
            except OSError as e:                     # z. B. Platte voll: ohne Dateisperre weiterarbeiten
                log.warning("Sperrdatei nicht anlegbar (%s) – arbeite ohne Dateisperre", e)
                fh = None
            if fh is not None:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    fh.close()
                    return False
            self._gen += 1
            gen = self._gen
            self._lockfh = fh                        # Dateisperre gehört zu diesem Lauf (Nummer gen)
            self._started_mono = time.monotonic()
            self.job = {"running": True, "name": name, "started": time.time(),
                        "finished": None, "ok": None, "message": "", "log": []}
            started = self.job["started"]
        self.rec.begin(name, started)
        return gen

    def _release(self, gen: int | None = None) -> None:
        """Gibt die Sperre frei, aber nur, wenn `gen` noch der aktuelle Lauf ist (None = aktueller Lauf)."""
        with self._lock:
            if gen is not None and gen != self._gen:
                return
            self._release_locked()

    def _release_locked(self) -> None:
        fh, self._lockfh = self._lockfh, None
        if fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                fh.close()
            except OSError:
                pass

    def _execute(self, name: str, kwargs: dict, gen: int | None = None) -> tuple[bool, str]:
        ops = {"check": self.op_check, "update": self.op_update, "restart": self.op_restart,
               "start": self.op_start, "stop": self.op_stop, "rollback": self.op_rollback,
               "recover": self.op_recover}
        ok, message = False, "Abgebrochen"
        self._tls.gen = gen
        try:
            try:
                ok, message = ops[name](**kwargs)
            except (OpError, GitError, ManagerError) as e:
                ok, message = False, str(e)
            except BaseException as e:               # auch SystemExit/KeyboardInterrupt: Lauf sauber beenden
                log.error("Unerwarteter Fehler in %s:\n%s", name, traceback.format_exc())
                ok, message = False, f"Unerwarteter Fehler: {type(e).__name__}: {e}"
                if not isinstance(e, Exception):
                    self._finish(ok, message, gen)
                    raise
            self._finish(ok, message, gen)
            return ok, message
        finally:
            self._tls.gen = None

    def _finish(self, ok: bool, message: str, gen: int | None = None) -> None:
        """Schließt den Lauf ab. Jeder Schritt einzeln geschützt, damit die Sperre garantiert frei wird.
        Ist `gen` nicht mehr der aktuelle Lauf (wurde als hängend freigegeben), bleibt der neue Lauf unberührt."""
        with self._lock:
            stale = gen is not None and gen != self._gen
        if stale:
            log.warning("Später Abschluss eines freigegebenen Laufs ignoriert: %s", message)
            return
        try:
            self._say(("✓ " if ok else "✗ ") + message)
        except Exception:
            log.exception("Abschlusszeile konnte nicht geschrieben werden")
        finished = time.time()
        try:
            with self._lock:
                self.job.update(running=False, finished=finished, ok=ok, message=message)
        except Exception:
            self.job["running"] = False
        try:
            self.rec.end(finished, ok, message)
        finally:
            self._release(gen)

    # ------------------------------------------------------------------ Hilfen
    def _http_health(self) -> bool:
        url = self.cfg.health_url
        if not url:
            return True
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                ok = 200 <= r.status < 300
                try:
                    data = json.loads(r.read(65536) or b"{}")
                    self.version_info = data if isinstance(data, dict) else None
                except Exception:
                    self.version_info = None
                self.version_info_at = time.monotonic()
                return ok
        except Exception:
            # keine Antwort: alte Angaben (z. B. maintenance_until) nicht weiter als aktuell ausgeben
            self.version_info, self.version_info_at = None, time.monotonic()
            return False

    def _active(self) -> bool:
        try:
            return self.mgr.is_active()
        except ManagerError:
            return False

    def wait_healthy(self) -> tuple[bool, str]:
        """Wartet, bis das Programm läuft, antwortet und kurz stabil bleibt."""
        cfg = self.cfg
        deadline = time.monotonic() + cfg.health_timeout_s
        while time.monotonic() < deadline:
            if not self._active():
                if self.mgr.name == "process":     # beim Prozess ist "beendet" endgültig
                    return False, "Programm hat sich beendet" + self._log_hint()
            elif self._health():
                if self._stays_healthy(cfg.stable_s):
                    return True, ""
            time.sleep(POLL_S)
        return False, f"keine gültige Antwort innerhalb von {cfg.health_timeout_s:.0f} s" + self._log_hint()

    def _stays_healthy(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            time.sleep(min(POLL_S, max(0.0, end - time.monotonic())))
            if not (self._active() and self._health()):
                return False
        return True

    def _log_hint(self) -> str:
        tail = getattr(self.mgr, "tail", None)
        lines = tail(3) if tail else []
        return f" – letzte Ausgabe: {' | '.join(lines)}" if lines else ""

    def _preflight(self, need_clean: bool = True) -> None:
        cfg = self.cfg
        if not cfg.repo_dir or not (Path(cfg.repo_dir) / ".git").exists():
            raise OpError(f"repo_dir „{cfg.repo_dir}“ ist kein Git-Verzeichnis")
        branch = gitops.current_branch(cfg.repo_dir)
        if branch != cfg.branch:
            raise OpError(f"Im Projekt ist Branch „{branch}“ ausgecheckt, erwartet „{cfg.branch}“")
        if not gitops.clear_stale_index_lock(cfg.repo_dir):
            raise OpError("Git ist im Projekt gerade gesperrt (.git/index.lock, läuft ein anderer git-Befehl?). "
                          "Es wurde nichts gestoppt; in ein paar Minuten erneut versuchen.")
        if need_clean and gitops.is_dirty(cfg.repo_dir):
            raise OpError("Im Projekt gibt es lokale Änderungen an nachverfolgten Dateien. "
                          "Aus Sicherheit wird nichts überschrieben und nichts gestoppt.")

    def _fetch(self) -> None:
        try:
            gitops.fetch(self.cfg.repo_dir, self.cfg.remote, self.cfg.git_timeout_s)
        except GitError as e:
            raise OpError(f"Repository nicht erreichbar (offline?): {e}")

    def _install(self) -> None:
        cfg = self.cfg
        self._say("Installiere Abhängigkeiten …")
        try:
            p = subprocess.run(_resolve_exe(cfg.install_command, cfg.repo_dir), cwd=cfg.repo_dir,
                               capture_output=True, text=True, timeout=cfg.install_timeout_s)
        except subprocess.TimeoutExpired:
            raise OpError("Installation der Abhängigkeiten: Zeitüberschreitung")
        except OSError as e:
            raise OpError(f"Installation der Abhängigkeiten nicht startbar: {e}")
        if p.returncode != 0:
            tail = " | ".join((p.stderr or p.stdout).strip().splitlines()[-3:])
            raise OpError(f"Installation der Abhängigkeiten fehlgeschlagen: {tail}")

    def _needs_install(self, old: str, new: str) -> bool:
        if not self.cfg.install_command:
            return False
        return bool(gitops.changed_files(self.cfg.repo_dir, old, new) & set(self.cfg.deps_files))

    # ------------------------------------------------------------------ Aktionen
    def op_check(self) -> tuple[bool, str]:
        cfg = self.cfg
        self._preflight(need_clean=False)
        self._say("Hole Änderungen …")
        self._fetch()
        behind = gitops.behind_count(cfg.repo_dir, cfg.remote, cfg.branch)
        remote_head = gitops.rev(cfg.repo_dir, f"{cfg.remote}/{cfg.branch}")
        info = {"checked_at": time.time(), "behind": behind, "remote_head": remote_head,
                "subjects": gitops.subjects(cfg.repo_dir, f"HEAD..{cfg.remote}/{cfg.branch}") if behind else []}
        self._set_state(update_info=info)
        if behind == 0:
            return True, "Bereits aktuell"
        msg = f"{behind} neue Änderung(en) verfügbar"
        if self.state.get("blocked") == remote_head:
            msg += " – dieser Stand ist als fehlerhaft markiert"
        return True, msg

    def op_update(self, force: bool = False) -> tuple[bool, str]:
        cfg, repo = self.cfg, self.cfg.repo_dir
        self._preflight()
        prev = gitops.head(repo)
        self._say("Hole Änderungen …")
        self._fetch()
        behind = gitops.behind_count(repo, cfg.remote, cfg.branch)
        if behind == 0:
            return True, "Bereits aktuell – nichts zu tun"
        target = gitops.rev(repo, f"{cfg.remote}/{cfg.branch}")
        if not force and self.state.get("blocked") == target:
            return False, (f"Stand {gitops.short(target)} ist als fehlerhaft markiert (ein früherer Versuch "
                           f"ist gescheitert). Auf einen neuen Commit warten oder erzwingen.")
        if not gitops.is_ancestor(repo, prev, target):
            raise OpError("Der Verlauf weicht ab (lokale Commits im Projekt?). "
                          "Ein automatisches Update ist nicht möglich.")
        # Würde git beim Einspielen nicht nachverfolgte Dateien überschreiben, bricht es ab. Das VOR dem Stoppen prüfen:
        # sonst stünde das Programm, und der (unschuldige) neue Stand würde als fehlerhaft gesperrt.
        clash = gitops.untracked_collisions(repo, prev, target)
        if clash:
            more = " …" if len(clash) > 5 else ""
            raise OpError(f"Nicht nachverfolgte Dateien im Projekt würden vom Update überschrieben: {', '.join(clash[:5])}{more}. "
                          "Bitte verschieben oder löschen. Es wurde nichts gestoppt.")
        # Ignorierte Dateien (config.toml, data/…) würde git beim Fast-Forward stillschweigend überschreiben, und ein Rollback
        # (reset --hard) löschte sie danach. Checkt der neue Stand so eine Datei ein (meist ein Versehen upstream): ablehnen.
        ignored = gitops.ignored_collisions(repo, prev, target)
        if ignored:
            more = " …" if len(ignored) > 5 else ""
            raise OpError(f"Das Update würde lokale, ignorierte Dateien überschreiben: {', '.join(ignored[:5])}{more} "
                          "(im neuen Stand eingecheckt, vermutlich ein Versehen). Zum Schutz der lokalen Daten wird nicht "
                          "aktualisiert. Es wurde nichts gestoppt.")
        need_deps = self._needs_install(prev, target)
        self._say(f"Update {gitops.short(prev)} → {gitops.short(target)} ({behind} Änderung(en))")
        # Merker für einen Abbruch mittendrin (Neustart von Watchdog oder Pi): startup() stellt dann `prev` wieder her
        self._set_state(pending={"prev": prev, "target": target, "since": time.time()})
        try:
            return self._apply_update(prev, target, need_deps)
        finally:
            self._set_state(pending=None)

    def _apply_update(self, prev: str, target: str, need_deps: bool) -> tuple[bool, str]:
        cfg, repo = self.cfg, self.cfg.repo_dir
        applied = False                               # erst ab hier kann ein Fehlschlag am neuen Stand liegen
        try:
            self._say("Stoppe Programm …")
            self.mgr.stop(cfg.stop_timeout_s)         # im try: auch eine Zeitüberschreitung beim Stoppen führt zur Wiederherstellung
            gitops.merge_ff(repo, cfg.remote, cfg.branch)
            applied = True
            self._say("Neuer Stand eingespielt")
            if need_deps:
                self._install()
            self._say("Starte Programm …")
            self.mgr.start()
            ok, why = self.wait_healthy()
        except Exception as e:                        # jeder Fehler (auch unerwartete): immer wiederherstellen
            ok, why = False, (str(e) if isinstance(e, (GitError, ManagerError, OpError)) else f"{type(e).__name__}: {e}")

        if ok:
            self._mark_good()
            # Direkt als aktuell zeigen: wir haben gerade genau bis zu `target` (dem damaligen Fernstand) eingespielt,
            # ein erneutes "Auf Updates prüfen" ist dafür nicht nötig. Sonst bliebe die alte Anzeige "N neue Änderungen"
            # stehen, bis von Hand oder durch die nächste automatische Prüfung aktualisiert wird.
            self._set_state(blocked=None, update_info={
                "checked_at": time.time(), "behind": 0, "remote_head": target, "subjects": []})
            return True, f"Update auf {gitops.short(target)} installiert, Programm läuft"

        self._say(f"Fehler: {why}")
        try:
            ok2, why2 = self._restore(prev)           # zuerst wiederherstellen, ...
        except Exception as e:
            ok2, why2 = False, f"{type(e).__name__}: {e}"
        if applied:
            self._set_state(blocked=target)           # ... dann erst den Stand als fehlerhaft merken (darf die Wiederherstellung nie verhindern)
        note = "" if applied else " Der neue Stand wurde nicht eingespielt und nicht als fehlerhaft markiert."
        if ok2:
            return False, (f"Update fehlgeschlagen ({why}). Der vorherige Stand {gitops.short(prev)} "
                           f"wurde wiederhergestellt und läuft.{note}")
        return False, (f"Update fehlgeschlagen ({why}) UND die Wiederherstellung ist fehlgeschlagen "
                       f"({why2}). Eingriff nötig!")

    def _restore(self, commit: str) -> tuple[bool, str]:
        """Zurück auf `commit`, Programm neu starten und prüfen. Setzt Arbeitsverzeichnis und Index IMMER hart zurück,
        auch wenn HEAD schon stimmt (z. B. nach einem halben Checkout bei voller Platte)."""
        cfg, repo = self.cfg, self.cfg.repo_dir
        self._say(f"Stelle Stand {gitops.short(commit)} wieder her …")
        try:
            self.mgr.stop(cfg.stop_timeout_s)
        except ManagerError as e:                   # z. B. Zeitüberschreitung: trotzdem zurücksetzen und starten
            self._say(f"Warnung beim Stoppen: {e}")
        git_err = None
        try:
            current = gitops.head(repo)
            reinstall = current != commit and self._needs_install(current, commit)
            try:
                gitops.reset_hard(repo, commit)
            except GitError:
                if not gitops.clear_stale_index_lock(repo):   # z. B. index.lock von einem Stromausfall mitten im Update
                    raise
                gitops.reset_hard(repo, commit)
            if reinstall:
                try:
                    self._install()
                except OpError as e:               # alter Stand läuft oft trotzdem (Pakete sind noch da)
                    self._say(f"Warnung: {e} – starte trotzdem")
        except (GitError, OpError) as e:
            git_err = str(e)
            self._say(f"Zurücksetzen fehlgeschlagen ({git_err}) – starte das Programm trotzdem")
        # Das Programm darf nie gestoppt bleiben: auch wenn git scheitert (gesperrt, Platte voll), wird gestartet.
        try:
            self._say("Starte Programm …")
            self.mgr.start()
            ok, why = self.wait_healthy()
        except (ManagerError, OpError) as e:
            ok, why = False, str(e)
        if git_err:
            state = "läuft wieder" if ok else f"startet nicht ({why})"
            return False, f"Zurücksetzen auf {gitops.short(commit)} fehlgeschlagen ({git_err}); Programm mit vorhandenem Stand gestartet, {state}"
        if ok:
            self._mark_good()
        return ok, why

    def op_recover(self, prev: str, target: str | None = None) -> tuple[bool, str]:
        """Ein Update wurde mittendrin abgebrochen (Watchdog oder Pi neu gestartet): vorherigen Stand wiederherstellen,
        den neuen als fehlerhaft merken (ein späteres Update ist mit „erzwingen“ weiter möglich)."""
        repo = self.cfg.repo_dir
        self._say(f"Unterbrochenes Update erkannt ({gitops.short(prev)} → {gitops.short(target)}): "
                  f"stelle {gitops.short(prev)} wieder her")
        try:
            branch = gitops.current_branch(repo)
            if branch != self.cfg.branch:
                raise OpError(f"Im Projekt ist Branch „{branch}“ ausgecheckt, erwartet „{self.cfg.branch}“ – nichts verändert")
            ok, why = self._restore(prev)
        finally:
            self._set_state(pending=None)
        if target and target != prev:
            self._set_state(blocked=target)
        if ok:
            return True, f"Unterbrochenes Update: Stand {gitops.short(prev)} wiederhergestellt, Programm läuft"
        return False, f"Unterbrochenes Update: Wiederherstellung fehlgeschlagen ({why}). Eingriff nötig!"

    def op_restart(self) -> tuple[bool, str]:
        self._say("Stoppe Programm …")
        try:
            self.mgr.stop(self.cfg.stop_timeout_s)
        except ManagerError as e:                    # z. B. systemctl-Zeitüberschreitung: trotzdem starten, damit es nie aus bleibt
            self._say(f"Warnung beim Stoppen: {e} – starte trotzdem")
        self._say("Starte Programm …")
        self.mgr.start()
        ok, why = self.wait_healthy()
        if ok:
            self._mark_good()
            return True, "Programm neu gestartet und läuft"
        return False, f"Neustart fehlgeschlagen: {why}"

    def op_start(self) -> tuple[bool, str]:
        if self._active() and self._health():
            return True, "Programm läuft bereits"
        self.mgr.start()
        ok, why = self.wait_healthy()
        if ok:
            self._mark_good()
            return True, "Programm gestartet und läuft"
        return False, f"Start fehlgeschlagen: {why}"

    def op_stop(self) -> tuple[bool, str]:
        self.mgr.stop(self.cfg.stop_timeout_s)
        return True, "Programm gestoppt"

    def op_rollback(self) -> tuple[bool, str]:
        repo = self.cfg.repo_dir
        self._preflight()
        target = self.state.get("last_good")
        if not target:
            raise OpError("Es ist noch kein letzter funktionierender Stand gespeichert")
        current = gitops.head(repo)
        if current == target:
            # Ein Update kann gesund laufen und trotzdem unerwünscht sein: dann zurück auf den guten Stand davor
            prev_good = self.state.get("previous_good")
            if not prev_good or prev_good in (current, self.state.get("blocked")):
                return True, f"Bereits auf dem letzten funktionierenden Stand {gitops.short(target)}"
            self._say(f"{gitops.short(current)} ist der letzte gute Stand – zurück auf den davor ({gitops.short(prev_good)})")
            target = prev_good
        # Der Stand, den wir verlassen, soll nicht sofort wieder automatisch eingespielt werden
        remote_head = None
        try:
            remote_head = gitops.rev(repo, f"{self.cfg.remote}/{self.cfg.branch}")
        except GitError:
            pass
        if remote_head == current:
            self._set_state(blocked=current)
        ok, why = self._restore(target)
        if ok:
            return True, f"Zurückgesetzt auf {gitops.short(target)}, Programm läuft"
        return False, f"Zurücksetzen fehlgeschlagen: {why}"

    # ------------------------------------------------------------------ Start des Watchdogs
    def startup(self) -> None:
        """Beim Start: letzten guten Stand merken, ggf. Programm starten."""
        try:
            pending = self.state.get("pending")
            if isinstance(pending, dict) and pending.get("prev"):
                self._recover_pending(pending)
                return
            if self._active() and self._health():
                if not self.state.get("last_good"):
                    self._mark_good()
            elif self.cfg.autostart_app and not self._active():
                self.start("start")
        except Exception:
            log.exception("Fehler beim Start-Check")

    def _recover_pending(self, pending: dict) -> None:
        """Wiederherstellung nach einem unterbrochenen Update als normalen Lauf starten (höchstens MAX_RECOVER_TRIES Mal)."""
        try:
            tries = int(pending.get("tries") or 0) + 1
        except (TypeError, ValueError):
            tries = 1
        if tries > MAX_RECOVER_TRIES:
            log.error("Unterbrochenes Update: Wiederherstellung %d-mal gescheitert – gebe auf, Eingriff nötig", MAX_RECOVER_TRIES)
            self._set_state(pending=None)
            return
        self._set_state(pending={**pending, "tries": tries})
        log.warning("Unterbrochenes Update erkannt – stelle %s wieder her", gitops.short(pending.get("prev")))
        if not self.start("recover", prev=str(pending["prev"]), target=pending.get("target")):
            log.error("Wiederherstellung nach unterbrochenem Update nicht startbar (läuft schon eine Aktion?)")

    # ------------------------------------------------------------------ Status
    def job_view(self) -> dict:
        with self._lock:
            return {**self.job, "log": list(self.job["log"][-80:])}

    def status(self) -> dict:
        """Jeder Abschnitt ist einzeln geschützt: ein Fehler wird als Hinweis in `problems` gemeldet,
        der Rest des Status bleibt gültig."""
        cfg = self.cfg
        problems: list[str] = []
        try:
            from .maintenance import app_base
            app_url = cfg.app_link_url or app_base(cfg.app_url, cfg.health_url)
        except Exception:
            app_url = ""
        info: dict = {"version": __version__, "manager": self.mgr.name, "repo_dir": cfg.repo_dir,
                      "branch": cfg.branch, "needs_token": bool(cfg.token), "app_url": app_url}
        try:
            info["commit"] = gitops.head(cfg.repo_dir)
            info["dirty"] = gitops.is_dirty(cfg.repo_dir)
            info["current_branch"] = gitops.current_branch(cfg.repo_dir)
        except GitError as e:
            info["git_error"] = str(e)
        except Exception as e:
            info["git_error"] = f"{type(e).__name__}: {e}"
        try:
            active = self._active()
            now = time.monotonic()
            if active and not self._was_active:
                self._last_health_at = 0.0      # gerade erst wieder aktiv: sofort prüfen, nicht bis zum Intervall warten
            self._was_active = active
            job_running = bool(self.job.get("running"))
            if not job_running and self._was_job_running:
                self._last_health_at = 0.0      # Vorgang gerade beendet: letzten Stand sofort bestätigen
            self._was_job_running = job_running
            poll_s = HEALTH_POLL_BUSY_S if job_running else HEALTH_POLL_IDLE_S
            if active and now - self._last_health_at >= poll_s:
                self._last_health_ok = self._health()
                self._last_health_at = now
            if not active:
                self.version_info = None          # gestoppt: keine alten Angaben (z. B. maintenance_until) weitergeben
            healthy = active and self._last_health_ok
            info["app"] = "running" if healthy else ("unresponsive" if active else "stopped")
        except Exception as e:
            info["app"] = "unknown"
            problems.append(f"Zustand des Programms nicht ermittelbar ({type(e).__name__}: {e})")
        try:
            with self._lock:
                info["last_good"] = self.state.get("last_good")
                info["previous_good"] = self.state.get("previous_good")
                info["blocked"] = self.state.get("blocked")
                info["update_info"] = self.state.get("update_info")
                info["pending"] = self.state.get("pending")
        except Exception as e:
            problems.append(f"Gespeicherter Zustand nicht lesbar ({e})")
        hints: list[str] = []                       # Hinweise: kein Fehler, nur Empfehlung
        if not cfg.token:
            hints.append("Kein token gesetzt: Wer diese Seite erreicht (z. B. über tailscale serve im ganzen Tailnet), "
                         "kann Aktionen auslösen. Empfohlen: token in config.toml setzen.")
        info["hints"] = hints
        info["job"] = self.job_view()
        store = getattr(getattr(self, "rec", None), "store", None)
        if store is not None and not store.available:
            problems.append("Die Protokoll-Datenbank ist gerade nicht nutzbar (Ablauf wird nur im Speicher gehalten).")
        info["problems"] = problems
        return info

    def log_tail(self, lines: int = 100, since: str = "", until: str = "") -> list[str]:
        tail = getattr(self.mgr, "tail", None)
        if not tail:
            return []
        cap = 500 if not (since or until) else 20000    # mit Zeitraum: auch ein ganzer Tag soll passen
        return tail(max(1, min(lines, cap)), since=since, until=until)
