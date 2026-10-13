"""Konfiguration: config.toml, Umgebungsvariablen (WD_<FELDNAME>) haben Vorrang.

Nur Standardbibliothek: der Watchdog darf von nichts abhängen, was ein Update des
Hauptprogramms kaputtmachen könnte (z. B. dessen virtuelle Umgebung).
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    # --- das verwaltete Programm ---
    repo_dir: str = ""                         # Git-Klon des Hauptprogramms (Pflicht)
    remote: str = "origin"
    branch: str = "main"
    manager: str = "auto"                      # auto | process | systemd
    service_name: str = "meshcore-remote"      # systemd
    systemctl_command: str = "systemctl"       # z. B. "sudo systemctl"
    start_command: str = "venv/bin/python -m app.main"   # process-Manager, relativ zu repo_dir
    health_url: str = "http://127.0.0.1:8000/api/version"
    health_timeout_s: float = 45.0             # so lange auf den Start warten
    stable_s: float = 5.0                      # so lange muss es nach dem Start stabil laufen
    stop_timeout_s: float = 15.0
    install_command: str = "venv/bin/python -m pip install -r requirements.txt"
    deps_files: tuple = ("requirements.txt",)  # bei Änderung wird install_command ausgeführt
    install_timeout_s: float = 600.0
    git_timeout_s: float = 90.0
    autostart_app: bool = False                # App beim Start des Watchdogs starten, falls aus
    expect_internet: bool = False              # true: fehlendes Internet/Tailscale wird als Problem gemeldet (sonst nur Anzeige)
    app_db_path: str = ""                      # Datenbank des Hauptprogramms; leer = <repo_dir>/data/meshcore.db
    backup_dir: str = ""                       # leer = <data_dir>/backups
    backup_extra_dir: str = ""                 # optional zweites Ziel, z. B. USB-Stick (/media/pi/STICK/meshcore)
    backup_keep: int = 7                       # so viele Sicherungen bleiben je Ziel
    app_url: str = ""                         # Adresse des Hauptprogramms für den Wartungsmodus; leer = aus health_url abgeleitet
    app_link_url: str = ""                    # Adresse für den Link "Hauptprogramm ↗" im Browser (z. B. Tailscale-Adresse);
                                                # leer = wie app_url/health_url (dann 127.0.0.1 – im Browser meist falsch!)
    app_control_token: str = ""               # control_token des Hauptprogramms (leer = Wartung wird dort nicht ausgelöst)
    expect_usb: bool = False                   # true: fehlender USB-Node wird als Problem angezeigt (sonst nur Anzeige)
    # --- der Watchdog selbst ---
    host: str = "127.0.0.1"
    port: int = 8100
    token: str = ""                            # optional: schützt alle ändernden Aktionen
    allowed_hosts: tuple = ()                  # zusätzliche Host-Namen für ändernde Aufrufe (Schutz vor DNS-Rebinding);
                                                # ".heim.lan" = alle Namen, die so enden. Immer erlaubt: IP-Adressen, localhost,
                                                # *.ts.net, Rechnername und <Rechnername>.local
    data_dir: str = str(ROOT / "data")


def _cast(value, like):
    if isinstance(like, bool):
        return str(value).lower() in ("1", "true", "yes")
    if isinstance(like, int):
        return int(value)
    if isinstance(like, float):
        return float(value)
    if isinstance(like, tuple):
        if isinstance(value, str):
            return tuple(v.strip() for v in value.split(",") if v.strip())
        return tuple(value)
    return str(value)


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg = Config()
    cfg_path = Path(path or os.environ.get("WD_CONFIG", ROOT / "config.toml"))
    if cfg_path.is_file():
        with cfg_path.open("rb") as f:
            data = tomllib.load(f)
        flat = {}
        for block in data.values():
            if isinstance(block, dict):
                flat.update(block)
        flat.update({k: v for k, v in data.items() if not isinstance(v, dict)})
        for f_ in fields(cfg):
            if f_.name in flat:
                setattr(cfg, f_.name, _cast(flat[f_.name], getattr(cfg, f_.name)))
    for f_ in fields(cfg):
        env_value = os.environ.get(f"WD_{f_.name.upper()}")
        if env_value is not None:
            setattr(cfg, f_.name, _cast(env_value, getattr(cfg, f_.name)))
    return cfg


def load_config_safe(path: str | os.PathLike | None = None) -> tuple[Config, str | None]:
    """Wie load_config, wirft aber nie: bei kaputter Datei oder ungültigen Werten kommt die Standard-
    konfiguration zurück plus eine Fehlermeldung (der Watchdog startet dann im Notbetrieb und zeigt sie an)."""
    try:
        return load_config(path), None
    except Exception as e:
        cfg = Config()
        try:                                        # Port und Adresse aus der Umgebung beachten, damit er auffindbar bleibt
            for name in ("host", "port"):
                v = os.environ.get(f"WD_{name.upper()}")
                if v is not None:
                    setattr(cfg, name, _cast(v, getattr(cfg, name)))
        except Exception:
            pass
        return cfg, f"Konfiguration nicht lesbar: {type(e).__name__}: {e}"
