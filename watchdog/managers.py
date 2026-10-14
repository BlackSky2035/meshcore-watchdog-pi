"""Starten, Stoppen und Prüfen des verwalteten Programms.

ProcessManager: startet das Programm als normalen Prozess (Tests, Notfälle).
SystemdManager: steuert einen systemd-Dienst (Raspberry Pi).
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path

from .config import Config


class ManagerError(Exception):
    pass


STOP_MARGIN_S = 30.0        # systemctl stop wartet bis zu TimeoutStopSec der Unit (deploy: 20 s); so viel Luft obendrauf
LOG_TIMEOUT_S = 3.0         # journalctl im Anfragepfad: kurz halten, Teilausgabe wird trotzdem gezeigt


def _proc_starttime(pid: int) -> str | None:
    """Startzeitpunkt des Prozesses (Feld 22 in /proc/<pid>/stat, Takte seit dem Systemstart) oder None (kein /proc, Prozess weg).
    Zusammen mit der PID eindeutig: nach einem Neustart kann dieselbe PID zu einem fremden Prozess gehören."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
        return data.rsplit(")", 1)[1].split()[19]      # Feld 2 (Name) kann Leerzeichen/Klammern enthalten
    except (OSError, IndexError):
        return None


def _proc_cmdline(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


class Manager(ABC):
    name = "base"

    @abstractmethod
    def is_active(self) -> bool: ...

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self, timeout: float) -> None: ...


# --------------------------------------------------------------------------- Prozess
class ProcessManager(Manager):
    name = "process"

    def __init__(self, command: str, cwd: str, data_dir: str):
        parts = shlex.split(command)
        if not parts:
            raise ManagerError("start_command ist leer")
        # Relative Programmpfade (venv/bin/python) beziehen sich auf das Projektverzeichnis
        if "/" in parts[0] and not os.path.isabs(parts[0]):
            parts[0] = os.path.join(cwd, parts[0])
        self.parts = parts
        self.cwd = cwd
        self.data = Path(data_dir)
        self.data.mkdir(parents=True, exist_ok=True)
        self.pidfile = self.data / "app.pid"
        self.logfile = self.data / "app.log"
        self._proc: subprocess.Popen | None = None

    def _read_pidfile(self) -> tuple[int, str | None] | None:
        """PID-Datei: „<pid> <startzeit>“ (älteres Format: nur „<pid>“)."""
        try:
            parts = self.pidfile.read_text().split()
            return int(parts[0]), (parts[1] if len(parts) > 1 else None)
        except (OSError, ValueError, IndexError):
            return None

    def _is_ours(self, pid: int, start: str | None) -> bool:
        """Gehört `pid` wirklich zu dem Programm, das wir gestartet haben? Ohne /proc (macOS) bleibt nur die PID."""
        actual = _proc_starttime(pid)
        if actual is None:
            return not Path("/proc/self/stat").exists()    # kein /proc: nicht prüfbar, wie bisher vertrauen
        if start is not None:
            return actual == start
        cmd = _proc_cmdline(pid)                           # altes Format ohne Startzeit: Kommandozeile vergleichen
        if not cmd:
            return False
        want = self.parts[1:]
        return cmd[-len(want):] == want if want else os.path.basename(cmd[0]) == os.path.basename(self.parts[0])

    def _pid(self) -> int | None:
        entry = self._read_pidfile()
        if entry is None:
            return None
        pid, start = entry
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            self.pidfile.unlink(missing_ok=True)
            return None
        except PermissionError:
            pass
        if not self._is_ours(pid, start):                  # PID wurde (z. B. nach einem Neustart) neu vergeben
            self.pidfile.unlink(missing_ok=True)
            return None
        return pid

    def is_active(self) -> bool:
        if self._proc is not None:
            if self._proc.poll() is None:
                return True
            self._proc = None                      # beendet und abgeholt (kein Zombie)
            self.pidfile.unlink(missing_ok=True)
            return False
        return self._pid() is not None             # vom Watchdog früher gestartet, jetzt "adoptiert" (PID + Startzeit geprüft)

    def start(self) -> None:
        if self.is_active():
            return
        try:
            with open(self.logfile, "ab") as log:
                self._proc = subprocess.Popen(
                    self.parts, cwd=self.cwd, stdout=log, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            raise ManagerError(f"Start fehlgeschlagen: {e}")
        pid = self._proc.pid
        start = _proc_starttime(pid)
        try:
            self.pidfile.write_text(f"{pid} {start}" if start else str(pid))
        except OSError:
            pass                                   # ohne PID-Datei läuft es trotzdem, nur „adoptieren“ geht dann nicht

    def stop(self, timeout: float) -> None:
        pid = self._proc.pid if self._proc else self._pid()
        if pid is None or not self.is_active():
            return
        self._signal(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_active():
                return
            time.sleep(0.1)
        self._signal(pid, signal.SIGKILL)          # hat nicht auf SIGTERM reagiert
        for _ in range(50):
            if not self.is_active():
                return
            time.sleep(0.1)
        raise ManagerError("Prozess lässt sich nicht beenden")

    def tail(self, lines: int = 20, since: str = "", until: str = "") -> list[str]:
        """Letzte Zeilen der Programmausgabe (app.log). since/until werden hier nicht ausgewertet
        (nur beim systemd-Manager auf dem Pi sinnvoll; der Prozess-Modus ist nur zum Testen)."""
        try:
            data = self.logfile.read_bytes()[-20000:].decode("utf-8", "replace")
        except OSError:
            return []
        return data.strip().splitlines()[-lines:]

    @staticmethod
    def _signal(pid: int, sig: int) -> None:
        try:
            os.killpg(pid, sig)                    # ganze Prozessgruppe (eigene Session)
        except ProcessLookupError:
            pass
        except PermissionError as e:
            raise ManagerError(f"Keine Berechtigung, Prozess {pid} zu beenden: {e}")


# --------------------------------------------------------------------------- systemd
def query_command(cmd: list[str]) -> list[str]:
    """Befehl für reine Abfragen (is-active): der konfigurierte systemctl-Befehl OHNE vorangestelltes sudo. Abfragen brauchen
    kein root, und `sudo` schriebe bei der Statusabfrage alle 3 s Zeilen ins Journal. Erkannt wird „sudo [Optionen] …systemctl …“;
    passt das nicht (anderes Hilfsprogramm), bleibt der Befehl unverändert."""
    if not cmd or os.path.basename(cmd[0]) != "sudo":
        return list(cmd)
    for i, part in enumerate(cmd[1:], start=1):
        if os.path.basename(part) == "systemctl":
            return list(cmd[i:])
    return list(cmd)


class SystemdManager(Manager):
    name = "systemd"

    def __init__(self, service: str, systemctl: str = "systemctl", timeout: float = 60):
        self.service = service
        self.cmd = shlex.split(systemctl)
        self.query_cmd = query_command(self.cmd)
        self.timeout = timeout

    def _run(self, *args: str, timeout: float | None = None, query: bool = False) -> subprocess.CompletedProcess:
        cmd = self.query_cmd if query else self.cmd
        try:
            return subprocess.run([*cmd, *args, self.service], capture_output=True,
                                  text=True, timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired:
            raise ManagerError(f"systemctl {args[0]}: Zeitüberschreitung")
        except FileNotFoundError:
            raise ManagerError("systemctl nicht gefunden (läuft das auf einem systemd-System?)")

    def is_active(self) -> bool:
        p = self._run("is-active", query=True)          # reine Abfrage: ohne sudo (läuft alle 3 s)
        return p.returncode == 0 and p.stdout.strip() == "active"

    def start(self) -> None:
        p = self._run("start")
        if p.returncode != 0:
            raise ManagerError(f"systemctl start: {(p.stderr or p.stdout).strip()}")

    def stop(self, timeout: float) -> None:
        # systemctl stop kehrt erst zurück, wenn der Dienst aus ist (spätestens nach TimeoutStopSec + SIGKILL). Vorher abbrechen
        # hieße: Fehler melden, obwohl der Dienst gleich darauf doch beendet wird.
        p = self._run("stop", timeout=max(self.timeout, float(timeout) + STOP_MARGIN_S))
        if p.returncode != 0:
            raise ManagerError(f"systemctl stop: {(p.stderr or p.stdout).strip()}")

    def tail(self, lines: int = 20, since: str = "", until: str = "") -> list[str]:
        """Zeilen aus dem systemd-Journal des Dienstes. Ohne since/until: die letzten `lines` Zeilen.
        Mit since/until (alles, was journalctl --since/--until versteht, z. B. "2025-01-01 12:00" oder
        "1 hour ago"): alle Zeilen in dem Zeitraum, mit `lines` nur als Sicherheitsobergrenze.
        Wirft nie: eine leere Liste heißt nur, dass gerade nichts zu lesen war (journalctl fehlt,
        kein Zugriff, Zeitüberschreitung, ungültiger Zeitraum)."""
        args = ["journalctl", "-u", self.service, "--no-pager", "--output=short-iso"]
        if since:
            args += ["--since", since]
        if until:
            args += ["--until", until]
        cap = max(1, min(lines, 500)) if not (since or until) else max(1, min(lines, 20000))
        args += ["-n", str(cap)]
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=LOG_TIMEOUT_S)
        except subprocess.TimeoutExpired as e:             # langsam (großer Zeitraum, Pi ausgelastet): Teilausgabe zeigen
            out = e.stdout or e.output or ""
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            lines = out.strip().splitlines()
            return lines + ["(Zeitüberschreitung: Ausgabe unvollständig – kleineren Zeitraum wählen)"] if lines else []
        except Exception:
            return []
        if p.returncode != 0:
            return []
        return p.stdout.strip().splitlines()


def make_manager(cfg: Config) -> Manager:
    kind = cfg.manager
    if kind == "auto":
        import shutil
        kind = "systemd" if shutil.which(cfg.systemctl_command.split()[-1]) and os.path.isdir("/run/systemd/system") \
            else "process"
    if kind == "systemd":
        return SystemdManager(cfg.service_name, cfg.systemctl_command,
                              timeout=max(60.0, float(cfg.stop_timeout_s) + STOP_MARGIN_S))
    if kind == "process":
        return ProcessManager(cfg.start_command, cfg.repo_dir, cfg.data_dir)
    raise ManagerError(f"Unbekannter manager: {cfg.manager}")
