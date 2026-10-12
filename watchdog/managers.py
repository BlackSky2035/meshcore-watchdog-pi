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

    def _pid(self) -> int | None:
        try:
            return int(self.pidfile.read_text().strip())
        except (OSError, ValueError):
            return None

    def is_active(self) -> bool:
        if self._proc is not None:
            if self._proc.poll() is None:
                return True
            self._proc = None                      # beendet und abgeholt (kein Zombie)
            self.pidfile.unlink(missing_ok=True)
            return False
        pid = self._pid()                          # vom Watchdog früher gestartet, jetzt "adoptiert"
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            self.pidfile.unlink(missing_ok=True)
            return False
        except PermissionError:
            return True

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
        self.pidfile.write_text(str(self._proc.pid))

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
class SystemdManager(Manager):
    name = "systemd"

    def __init__(self, service: str, systemctl: str = "systemctl", timeout: float = 60):
        self.service = service
        self.cmd = shlex.split(systemctl)
        self.timeout = timeout

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([*self.cmd, *args, self.service], capture_output=True,
                                  text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise ManagerError(f"systemctl {args[0]}: Zeitüberschreitung")
        except FileNotFoundError:
            raise ManagerError("systemctl nicht gefunden (läuft das auf einem systemd-System?)")

    def is_active(self) -> bool:
        p = self._run("is-active")
        return p.returncode == 0 and p.stdout.strip() == "active"

    def start(self) -> None:
        p = self._run("start")
        if p.returncode != 0:
            raise ManagerError(f"systemctl start: {(p.stderr or p.stdout).strip()}")

    def stop(self, timeout: float) -> None:
        p = self._run("stop")
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
            p = subprocess.run(args, capture_output=True, text=True, timeout=15)
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
        return SystemdManager(cfg.service_name, cfg.systemctl_command)
    if kind == "process":
        return ProcessManager(cfg.start_command, cfg.repo_dir, cfg.data_dir)
    raise ManagerError(f"Unbekannter manager: {cfg.manager}")
