"""Zustand des Rechners: Netz, Speicherplatz, Spannung/Temperatur (Pi), Uhrzeit. Nur Standardbibliothek.

Läuft in einem eigenen Hintergrund-Thread (alle 30 s), nie im Anfragepfad, und wirft nie. Jede Messung ist einzeln
abgesichert: fällt eine aus, fehlt nur ihr Wert. Befehle (vcgencmd, timedatectl, tailscale) haben ein Zeitlimit und
werden nur benutzt, wenn es sie gibt.

Netzausfälle werden dauerhaft in `data/netlog.jsonl` festgehalten, damit nach einer Rückkehr sichtbar ist, WANN der Rechner
offline war. Melden kann der Rechner das während des Ausfalls selbst nicht (siehe README).
Nicht an echter Hardware getestet (vcgencmd, timedatectl, tailscale).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from .power import PowerWatch
from .sends import SendCorrelator

log = logging.getLogger("watchdog.sysmon")

INTERVAL_S = 30.0
NET_GRACE_S = 60.0          # so lange darf das Netz weg sein, bevor es (falls erwartet) als Problem gilt
DISK_WARN_PCT = 90.0
DISK_WARN_FREE_MB = 200
TEMP_WARN_C = 80.0
NET_HOSTS = (("1.1.1.1", 443), ("8.8.8.8", 53))
MAX_EVENTS = 500
CLOCK_JUMP_S = 10.0         # so viel dürfen Wanduhr und Laufzeit zwischen zwei Einträgen auseinanderlaufen, sonst ist die Uhr gesprungen
MAX_METRIC_EVENTS = 40000   # bei 30 s Takt gut zwei Wochen Betriebszeit, bevor die ältesten Werte weichen


def run_cmd(args: list[str], timeout: float = 3.0) -> str | None:
    """Befehl ausführen; None bei jedem Fehler (nicht vorhanden, Zeitüberschreitung, Rückgabe ≠ 0)."""
    try:
        if shutil.which(args[0]) is None:
            return None
        env = {k: v for k, v in os.environ.items() if k != "NOTIFY_SOCKET"}   # Hilfsprogramme dürfen systemd nichts melden
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def disk(path: str) -> dict | None:
    try:
        p = Path(path)
        while not p.exists() and p != p.parent:        # Ordner gibt es evtl. noch nicht: den nächsten vorhandenen messen
            p = p.parent
        u = shutil.disk_usage(p)
        return {"path": path, "total_mb": u.total // 2**20, "free_mb": u.free // 2**20,
                "used_pct": round(100 * (u.total - u.free) / u.total, 1) if u.total else None}
    except Exception:
        return None


def parse_throttled(text: str | None) -> dict | None:
    """`vcgencmd get_throttled` → throttled=0x50005. Bits: 0 Unterspannung, 1 Takt begrenzt, 2 gedrosselt, 3 Temp-Grenze;
    16–19 dasselbe „seit dem Start aufgetreten“."""
    try:
        v = int(text.split("=")[1], 16)       # type: ignore[union-attr]
    except Exception:
        return None
    return {"undervolt_now": bool(v & 1), "capped_now": bool(v & 2), "throttled_now": bool(v & 4),
            "undervolt_ever": bool(v & 0x10000), "throttled_ever": bool(v & 0x40000), "raw": hex(v)}


def cpu_temp(path: str = "/sys/class/thermal/thermal_zone0/temp") -> float | None:
    try:
        return round(int(Path(path).read_text().strip()) / 1000, 1)
    except Exception:
        return None


def boot_time(path: str = "/proc/uptime") -> float | None:
    """Zeitpunkt, seit dem der Rechner läuft (nicht der Watchdog-Prozess). Nur Linux; sonst None."""
    try:
        secs = float(Path(path).read_text().split()[0])
        return time.time() - secs
    except Exception:
        return None


def time_synced() -> bool | None:
    out = run_cmd(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    return None if out is None else out.lower() == "yes"


def boot_id() -> str | None:
    """Kennung des laufenden Starts des Rechners (ändert sich bei jedem Neustart). Nur Linux; sonst None."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except Exception:
        return None


def internet_up(hosts=NET_HOSTS, timeout: float = 2.0) -> bool:
    for host, port in hosts:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except Exception:
            continue
    return False


def tailscale_state() -> dict | None:
    out = run_cmd(["tailscale", "status", "--json"], timeout=4.0)
    if out is None:
        return None
    try:
        state = json.loads(out).get("BackendState")
    except Exception:
        return {"state": "unbekannt", "ok": False}
    return {"state": state, "ok": state == "Running"}


def power_detail(ev: dict | None) -> str:
    """Satz mit den genau aufgezeichneten Einbrüchen für die Warnmeldung (leer, wenn es keine gibt). Die genaue Messung (power.py, alle 2 s)
    ersetzt die grobe Angabe „zuletzt aufgezeichnet“ aus dem 30-s-Takt, die kurze Einbrüche meist verpasst."""
    try:
        if not ev:
            return ""
        last = ev.get("last") or {}
        if not last.get("start"):
            return ""
        if ev.get("count_24h"):
            at = time.strftime("%H:%M:%S", time.localtime(last["start"]))
            longest = ev.get("longest_s_24h")
            return (f" Aufgezeichnet: {ev['count_24h']}× in 24 h, zuletzt {at}"
                    + (f", längster {longest} s" if longest is not None else "") + ".")
        return f" Letzter aufgezeichneter Einbruch: {time.strftime('%d.%m. %H:%M:%S', time.localtime(last['start']))}."
    except Exception:
        return ""


class NetLog:
    """Zustandswechsel des Netzes, dauerhaft als JSON-Zeilen. Wirft nie; ohne Datei läuft alles im Speicher."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.events: list[dict] = []
        try:
            if self.path and self.path.is_file():
                for line in self.path.read_text(encoding="utf-8").splitlines()[-MAX_EVENTS:]:
                    try:
                        e = json.loads(line)
                        if isinstance(e, dict) and "ts" in e and "up" in e:
                            self.events.append(e)
                    except Exception:
                        continue
        except Exception as e:
            log.warning("Netzprotokoll nicht lesbar: %s", e)

    def add(self, ts: float, up: bool, mono: float | None = None, boot: str | None = None) -> None:
        """`mono` (Laufzeit seit dem Start, springt bei Uhrkorrekturen nicht) und `boot` (Kennung des Starts) erlauben es, die Dauer auch dann
        richtig zu bestimmen, wenn die Uhr des Pi (keine Echtzeituhr, wird nach dem Start per Netz gestellt) zwischen zwei Einträgen springt."""
        e = {"ts": ts, "up": bool(up)}
        if mono is not None:
            e["mono"] = mono
        if boot:
            e["boot"] = boot
        self.events.append(e)
        if len(self.events) > MAX_EVENTS:
            self.events = self.events[-MAX_EVENTS // 2:]
            self._rewrite()
            return
        try:
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(e) + "\n")
        except Exception as ex:
            log.warning("Netzprotokoll nicht schreibbar: %s", ex)

    def _rewrite(self) -> None:
        try:
            if self.path:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text("".join(json.dumps(e) + "\n" for e in self.events), encoding="utf-8")
                tmp.replace(self.path)
        except Exception as ex:
            log.warning("Netzprotokoll nicht schreibbar: %s", ex)

    def last_up(self) -> bool | None:
        return self.events[-1]["up"] if self.events else None

    @staticmethod
    def _pair(down: dict, up: dict) -> dict:
        """Ein beendeter Ausfall. Normalfall: {start, end}. Ist die Uhr dazwischen gesprungen (z. B. Ende VOR Beginn, weil der Pi nach dem Start
        seine Zeit neu stellt), kommt `clock_jump: True` dazu: `end` ist die Zeit des späteren Eintrags (nach der Korrektur meist die richtige),
        die Dauer stammt aus der Laufzeit, falls beide Einträge im selben Start liegen, sonst ist sie unbekannt (`duration_s: None`, `start: None`)."""
        wall = up["ts"] - down["ts"]
        same_boot = bool(down.get("boot")) and down.get("boot") == up.get("boot")
        mono = None
        if same_boot:
            try:
                mono = float(up["mono"]) - float(down["mono"])
                if mono < 0:
                    mono = None
            except Exception:
                mono = None
        jumped = wall < 0 or (mono is not None and abs(wall - mono) > CLOCK_JUMP_S)
        if not jumped:
            return {"start": down["ts"], "end": up["ts"]}
        dur = round(mono, 1) if mono is not None else None
        return {"start": (up["ts"] - dur) if dur is not None else None, "end": up["ts"], "duration_s": dur, "clock_jump": True}

    def outages(self, limit: int = 10) -> list[dict]:
        """Ausfälle (Beginn, Ende oder None wenn noch offen), neueste zuletzt."""
        out, down = [], None
        for e in self.events:
            if not e["up"] and down is None:
                down = e
            elif e["up"] and down is not None:
                try:
                    out.append(self._pair(down, e))
                except Exception:                      # kaputter Eintrag (z. B. Zeitstempel kein Zahlenwert): einfache Form, nie werfen
                    out.append({"start": down.get("ts"), "end": e.get("ts")})
                down = None
        if down is not None:
            out.append({"start": down["ts"], "end": None})
        return out[-limit:]


def downsample(events: list[dict], max_points: int) -> list[dict]:
    """Verdichtet lange Reihen auf höchstens `max_points`, ohne kurze Ausschläge zu verlieren:
    je Bucket der höchste Temperaturwert und „an“, wenn irgendeine Probe im Bucket Unterspannung zeigte."""
    n = len(events)
    if max_points <= 0 or n <= max_points:
        return events
    bucket = n / max_points
    out, i = [], 0.0
    while round(i) < n:
        chunk = events[round(i):round(i + bucket)] or [events[min(round(i), n - 1)]]
        temps = [e["temp_c"] for e in chunk if e.get("temp_c") is not None]
        out.append({
            "ts": chunk[-1]["ts"],
            "temp_c": max(temps) if temps else None,
            "undervolt_now": any(e.get("undervolt_now") for e in chunk),
            "throttled_now": any(e.get("throttled_now") for e in chunk),
            "free_mb": chunk[-1].get("free_mb"),
        })
        i += bucket
    return out


class MetricsLog:
    """Regelmäßige Messwerte (Temperatur, Spannung, Speicher), dauerhaft als JSON-Zeilen, damit man
    nachträglich sieht, WANN genau etwas auffällig war – nicht nur „seit dem Start“. Wirft nie; ohne
    Datei läuft alles im Speicher."""

    def __init__(self, path: str | Path | None, limit: int = MAX_METRIC_EVENTS):
        self.path = Path(path) if path else None
        self.limit = limit
        self.events: list[dict] = []
        try:
            if self.path and self.path.is_file():
                for line in self.path.read_text(encoding="utf-8").splitlines()[-limit:]:
                    try:
                        e = json.loads(line)
                        if isinstance(e, dict) and "ts" in e:
                            self.events.append(e)
                    except Exception:
                        continue
        except Exception as e:
            log.warning("Messwert-Protokoll nicht lesbar: %s", e)

    def add(self, sample: dict) -> None:
        self.events.append(sample)
        if len(self.events) > self.limit:
            self.events = self.events[-self.limit // 2:]
            self._rewrite()
            return
        try:
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(sample) + "\n")
        except Exception as ex:
            log.warning("Messwert-Protokoll nicht schreibbar: %s", ex)

    def _rewrite(self) -> None:
        try:
            if self.path:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text("".join(json.dumps(e) + "\n" for e in self.events), encoding="utf-8")
                tmp.replace(self.path)
        except Exception as ex:
            log.warning("Messwert-Protokoll nicht schreibbar: %s", ex)

    def recent(self, limit: int = 200) -> list[dict]:
        return self.events[-limit:]

    def prune_before(self, ts: float) -> None:
        """Verwirft Messwerte von vor einem Neustart des Rechners (gehören zu einem früheren Betrieb)."""
        kept = [e for e in self.events if e.get("ts", 0) >= ts]
        if len(kept) != len(self.events):
            self.events = kept
            self._rewrite()

    def since_boot(self, max_points: int) -> list[dict]:
        return downsample(self.events, max_points)


class SysWatch:
    def __init__(self, data_dir: str = "", app_dir: str = "", app_db: str = "", expect_internet: bool = False,
                 interval: float = INTERVAL_S, clock=time.time, probes: dict | None = None,
                 app_base_url: str = "", app_control_token: str = ""):
        self.data_dir, self.app_dir, self.app_db = data_dir, app_dir, app_db
        self.expect_internet, self.interval, self._clock = expect_internet, interval, clock
        p = {"disk": disk, "throttled": lambda: parse_throttled(run_cmd(["vcgencmd", "get_throttled"])),
             "temp": cpu_temp, "time": time_synced, "internet": internet_up, "tailscale": tailscale_state,
             "boot": boot_time}
        self.probes = {**p, **(probes or {})}
        self.netlog = NetLog(Path(data_dir) / "netlog.jsonl" if data_dir else None)
        # Laufzeit seit dem Start: springt bei Uhrkorrekturen nicht. Mit einer Test-Uhr (nicht time.time) dient diese selbst, damit beides zusammenpasst.
        self._mono = time.monotonic if clock is time.time else clock
        self._boot = boot_id()
        self.metricslog = MetricsLog(Path(data_dir) / "metrics.jsonl" if data_dir else None)
        self.boot_ts = self._try("boot")
        if self.boot_ts is not None:
            self.metricslog.prune_before(self.boot_ts)   # Werte von vor diesem Start gehören nicht mehr zur Anzeige
        # eigener, schneller Takt nur für die Strom-Flags: hält jeden Einbruch mit Beginn und Dauer fest (siehe power.py)
        self.power = PowerWatch(Path(data_dir) / "power_events.jsonl" if data_dir else None,
                                lambda: self._try("throttled"), clock=clock)
        # nach einem Einbruch EINMAL die Sendezeiten des Hauptprogramms holen (sparsam, siehe sends.py)
        self.sends = SendCorrelator(app_base_url, app_control_token, self.power.add_note, clock=clock) if app_base_url else None
        self.power.on_event = self.sends.request if self.sends else None
        self.snap: dict | None = None
        self._net_down_since: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- Thread ------------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="sysmon", daemon=True)
            self._thread.start()
        self.power.start()

    def stop(self) -> None:
        self._stop.set()
        self.power.stop()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except BaseException:
                log.exception("Systemprüfung: unerwarteter Fehler")
            self._stop.wait(self.interval)

    # ---- Messen --------------------------------------------------------------
    def _try(self, name: str, *args):
        try:
            return self.probes[name](*args)
        except Exception:
            return None

    def refresh(self) -> None:
        now = self._clock()
        snap: dict = {"checked": now, "boot_ts": self.boot_ts}
        snap["disk"] = self._try("disk", self.data_dir or self.app_dir or ".")
        if self.app_db:
            try:
                snap["app_db_mb"] = round(Path(self.app_db).stat().st_size / 2**20, 2)
            except Exception:
                snap["app_db_mb"] = None
        snap["power"] = self._try("throttled")
        snap["power_events"] = self.power.summary(now)
        snap["temp_c"] = self._try("temp")
        snap["time_synced"] = self._try("time")
        net_up = bool(self._try("internet"))
        snap["tailscale"] = self._try("tailscale")
        if self.netlog.last_up() is not net_up:                # nur Wechsel aufzeichnen (auch der erste Wert)
            self.netlog.add(now, net_up, mono=self._mono(), boot=self._boot)
        if net_up:
            self._net_down_since = None
        elif self._net_down_since is None:
            self._net_down_since = now
        snap["internet"] = {"up": net_up, "down_since": self._net_down_since, "outages": self.netlog.outages()}
        snap["problems"] = self._problems(snap, now)
        self.snap = snap
        p = snap.get("power") or {}
        d = snap.get("disk") or {}
        self.metricslog.add({"ts": now, "temp_c": snap.get("temp_c"), "undervolt_now": p.get("undervolt_now"),
                             "throttled_now": p.get("throttled_now"), "free_mb": d.get("free_mb")})

    def history(self, max_points: int = 200) -> list[dict]:
        """Alle Messwerte seit diesem Start des Rechners, auf höchstens `max_points` verdichtet
        (die x-Achse im Diagramm zeigt so immer die gesamte Betriebszeit, nicht nur ein Zeitfenster)."""
        return self.metricslog.since_boot(max_points)

    def _problems(self, s: dict, now: float) -> list[str]:
        out: list[str] = []
        d = s.get("disk")
        if d and ((d["used_pct"] or 0) >= DISK_WARN_PCT or d["free_mb"] < DISK_WARN_FREE_MB):
            out.append(f"Speicher fast voll: {d['free_mb']} MB frei ({d['used_pct']} % belegt). "
                       f"Bei voller Karte fällt zuerst die Datenbank aus.")
        p = s.get("power")
        if p:
            detail = power_detail(s.get("power_events"))
            if p["undervolt_now"]:
                out.append("Unterspannung: Netzteil oder Kabel liefert zu wenig Strom (Pi-Meldung)." + detail)
            elif p["undervolt_ever"]:
                when = ""
                if not detail:                     # keine genauen Daten: grobe Angabe aus dem 30-s-Takt (kann kurze Einbrüche verpassen)
                    last = next((e["ts"] for e in reversed(self.metricslog.events) if e.get("undervolt_now")), None)
                    when = f" (zuletzt aufgezeichnet {time.strftime('%H:%M:%S', time.localtime(last))})" if last else " (vor Beginn der Aufzeichnung)"
                out.append(f"Seit dem Start gab es Unterspannung{when}: Netzteil/Kabel prüfen (Neustart löscht die Meldung)." + detail)
            if p["throttled_now"] or p["capped_now"]:
                out.append("Der Pi ist gedrosselt (Hitze oder Unterspannung).")
        t = s.get("temp_c")
        if t is not None and t >= TEMP_WARN_C:
            out.append(f"Prozessor sehr heiß: {t} °C.")
        if s.get("time_synced") is False:
            out.append("Die Uhrzeit ist nicht mit einem Zeitserver abgeglichen: Zeitstempel und Verläufe können falsch sein.")
        if self.expect_internet:
            if self._net_down_since is not None and now - self._net_down_since >= NET_GRACE_S:
                out.append(f"Kein Internet seit {int(now - self._net_down_since)} s (Push und Tailscale gehen nicht).")
            ts = s.get("tailscale")
            if ts and not ts["ok"]:
                out.append(f"Tailscale ist nicht verbunden (Status: {ts['state']}).")
        return out

    def view(self) -> dict | None:
        return self.snap
