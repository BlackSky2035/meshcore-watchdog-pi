"""Minimaler HTTP-Server (nur Standardbibliothek) mit Status-Seite und kleiner API.

Eigener Port, unabhängig vom Hauptprogramm: antwortet auch dann, wenn dieses kaputt
oder gestoppt ist.

Schutz der ändernden Aktionen:
- Header `X-Watchdog: 1` ist Pflicht. Browser senden ihn bei fremden Webseiten nicht
  ohne Vorabprüfung (CORS), die wir nie bestätigen: blockiert Fremdaufrufe aus dem Browser.
- Optional `token`: dann muss zusätzlich `X-Token` stimmen. Empfohlen, sobald die Seite
  über das lokale Netz oder Tailscale erreichbar ist.
- Host-Prüfung (DNS-Rebinding): ALLE Aufrufe (auch Lesen per GET und die Startseite) nur mit bekanntem `Host`
  (IP-Adresse, localhost, *.ts.net, Rechnername[.local], `allowed_hosts`), sonst 403. Einzige Ausnahme `/api/ping`
  (Selbstprüfung/Monitoring, liefert keine Daten).

Schutz vor langsamen Verbindungen (Slowloris):
- Gesamtfrist `REQUEST_DEADLINE_S` je Anfrage für das Lesen von Kopf UND Body (ab Annahme der Verbindung), nicht nur je
  Lesevorgang; wer tröpfelt, fliegt nach der Frist raus.
- Höchstens `MAX_PER_IP` gleichzeitige Verbindungen je Client-Adresse; `LOCAL_RESERVED` Plätze sind für Verbindungen vom
  Rechner selbst (Loopback bzw. eigene Adresse) reserviert, damit die eigene Selbstprüfung (Herzschlag) immer durchkommt.
"""
from __future__ import annotations

import hmac
import io
import ipaddress
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from .config import Config
from .health import StatusCache

log = logging.getLogger("watchdog.http")
STATIC = Path(__file__).parent / "static"
ACTIONS = {"check", "update", "restart", "start", "stop", "rollback"}
MAINT_ACTIONS = {"maintenance", "maintenance-end"}      # Wartungsmodus (eigener Schalter, siehe maintenance.py)
MAX_BODY = 4096
SOCKET_TIMEOUT_S = 15.0     # hängende Verbindungen blockieren keinen Thread dauerhaft (je Lesevorgang)
REQUEST_DEADLINE_S = 15.0   # Gesamtfrist für das Lesen EINER Anfrage (Kopf + Body), gegen tröpfelnde Verbindungen
MAX_CONNECTIONS = 48       # darüber: 503 statt unbegrenzt Threads
MAX_PER_IP = 8             # gleichzeitige Verbindungen je Client-Adresse (nicht für den Rechner selbst)
LOCAL_RESERVED = 4         # so viele der MAX_CONNECTIONS Plätze bekommen nur Verbindungen vom Rechner selbst
STARTED = time.time()
LOG_SLOT = threading.BoundedSemaphore(1)   # höchstens EIN Protokollabruf (journalctl) gleichzeitig


def _host_name(header: str | None) -> str:
    """Host-Header ohne Port, klein geschrieben ("[::1]:8100" → "::1")."""
    h = (header or "").strip().lower()
    if h.startswith("["):
        return h[1:].split("]", 1)[0]
    if h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h.rstrip(".")


def host_allowed(header: str | None, extra=()) -> bool:
    """Darf ein ändernder Aufruf mit diesem Host-Header kommen? IP-Adressen sind kein Weg für DNS-Rebinding (dort steht
    immer der Name des Angreifers im Host), ebenso wenig ein fehlender Header (kein Browser)."""
    host = _host_name(header)
    if not host:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host == "localhost" or host.endswith(".ts.net"):
        return True
    try:
        me = socket.gethostname().lower()
    except Exception:
        me = ""
    if me and host in (me, me + ".local", me.split(".")[0], me.split(".")[0] + ".local"):
        return True
    for entry in extra or ():
        e = str(entry).strip().lower().rstrip(".")
        if e and (host == e or (e.startswith(".") and host.endswith(e))):
            return True
    return False

HOST_FORBIDDEN = ("Unbekannter Host-Name (Schutz vor DNS-Rebinding). "
                  "Eigene Namen in allowed_hosts eintragen.")


class _DeadlineReader(io.RawIOBase):
    """Liest vom Socket, aber jeder einzelne recv bekommt höchstens die RESTZEIT bis zur Frist der Anfrage als Zeitlimit.
    So begrenzt die Frist die ganze Anfrage, auch wenn ein Client alle paar Sekunden ein Byte nachschiebt."""

    def __init__(self, sock, handler):
        super().__init__()
        self._sock, self._h = sock, handler

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        left = self._h.read_deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("Frist für die Anfrage abgelaufen")
        self._sock.settimeout(min(left, SOCKET_TIMEOUT_S))
        return self._sock.recv_into(b)


METRICS_MAX_POINTS = 1000     # /api/metrics: höchstens so viele (verdichtete) Messpunkte je Abfrage; die Oberfläche fragt 200


def token_matches(given: str | None, expected: str) -> bool:
    """Token vergleichen, auch mit Umlauten/Emojis. Header kommen bei http.server als Latin-1-Text an: die Oberfläche schickt
    das Token als UTF-8-Bytes (wie curl), dann ergibt `encode("latin-1")` genau diese Bytes. Ein Browser, der „ä“ selbst als
    ein Latin-1-Byte schickt, passt über den zweiten Vergleich. Früher: `compare_digest` mit Nicht-ASCII-Text → TypeError → 500."""
    g = given or ""
    exp = (expected or "").encode("utf-8")
    try:
        raw = g.encode("latin-1")
    except UnicodeEncodeError:
        raw = g.encode("utf-8")
    return hmac.compare_digest(raw, exp) | hmac.compare_digest(g.encode("utf-8"), exp)


def is_local_peer(peer_ip: str, own_ip: str | None = None) -> bool:
    """Kommt die Verbindung vom Rechner selbst (Loopback oder eigene Adresse)? Wirft nie."""
    try:
        ip = ipaddress.ip_address(str(peer_ip).split("%", 1)[0])
        mapped = getattr(ip, "ipv4_mapped", None)
        if ip.is_loopback or (mapped is not None and mapped.is_loopback):
            return True
    except ValueError:
        return False
    return bool(own_ip) and str(peer_ip) == str(own_ip)


FALLBACK_PAGE = ("<!doctype html><meta charset=utf-8><title>MeshCore Watchdog</title>"
                 "<h1>MeshCore Watchdog</h1><p>Die Oberfläche (static/index.html) fehlt, der Watchdog läuft aber.</p>"
                 "<p>Status: <a href='/api/status'>/api/status</a></p>")


def make_handler(cfg: Config, updater, cache=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Watchdog"
        timeout = SOCKET_TIMEOUT_S

        def log_message(self, fmt, *args):          # nicht jede Anfrage ins Log schreiben
            log.debug("%s %s", self.address_string(), fmt % args)

        # ---- Gesamtfrist je Anfrage (Slowloris) ------------------------------
        def setup(self):
            self.read_deadline = time.monotonic() + REQUEST_DEADLINE_S     # ab Annahme der Verbindung
            super().setup()
            old = self.rfile
            self.rfile = io.BufferedReader(_DeadlineReader(self.connection, self))
            try:
                old.close()
            except Exception:
                pass

        def handle_one_request(self):
            try:
                super().handle_one_request()
            finally:                                   # (falls je Keep-Alive: neue Anfrage, neue Frist)
                self.read_deadline = time.monotonic() + REQUEST_DEADLINE_S

        def _host_ok(self) -> bool:
            return host_allowed(self.headers.get("Host"), getattr(cfg, "allowed_hosts", ()))

        # ---- Antworten ---------------------------------------------------
        def _writing(self) -> None:
            """Vor dem Antworten: Das Lesen hat das Zeitlimit evtl. auf die (kurze) Restfrist gesetzt; Schreiben bekommt wieder
            das normale Zeitlimit je Vorgang."""
            try:
                self.connection.settimeout(SOCKET_TIMEOUT_S)
            except Exception:
                pass

        def _json(self, code: int, obj) -> None:
            self._writing()
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path, ctype: str, fallback: str | None = None) -> None:
            try:
                body = path.read_bytes()
            except OSError:
                if fallback is None:
                    return self._json(404, {"error": "nicht gefunden"})
                body = fallback.encode()
            self._writing()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _guarded(self, fn) -> None:
            """Jede Anfrage läuft geschützt: ein Fehler darin wird zur Fehlermeldung, nie zum Absturz."""
            try:
                fn()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                self.close_connection = True           # Browser weg: nichts zu tun
            except Exception as e:
                log.exception("Fehler bei %s %s", self.command, self.path)
                try:
                    self._json(500, {"error": f"Interner Fehler: {type(e).__name__}: {e}"})
                except Exception:
                    self.close_connection = True

        # ---- GET -----------------------------------------------------------
        def do_GET(self):
            self._guarded(self._get)

        def _get(self):
            path, _, query = self.path.partition("?")
            if path == "/api/ping":                    # kostet nichts und hängt an nichts: für Selbstprüfung/Monitoring
                return self._json(200, {"ok": True, "uptime_s": round(time.time() - STARTED)})
            if not self._host_ok():                    # DNS-Rebinding: auch Lesen nur mit bekanntem Host-Namen
                return self._json(403, {"error": HOST_FORBIDDEN})
            if path in ("/", "/index.html"):
                return self._file(STATIC / "index.html", "text/html; charset=utf-8", FALLBACK_PAGE)
            if path == "/api/status":
                return self._json(200, cache.get() if cache else updater.status())
            if path == "/api/log":
                q = parse_qs(query)
                try:
                    n = int(q.get("lines", ["100"])[0])
                except ValueError:
                    n = 100
                since, until = q.get("since", [""])[0], q.get("until", [""])[0]
                # journalctl läuft hier im Anfragepfad (kurzes Zeitlimit im Manager): nie mehrere gleichzeitig, sonst belegen
                # wiederholte Abrufe die Verbindungen des Servers
                if not LOG_SLOT.acquire(blocking=False):
                    return self._json(429, {"error": "Protokoll wird gerade geladen – bitte kurz warten"})
                try:
                    lines = updater.log_tail(n, since=since, until=until)
                except Exception as e:
                    lines = [f"(Programm-Protokoll nicht lesbar: {e})"]
                finally:
                    LOG_SLOT.release()
                return self._json(200, {"lines": lines})
            if path == "/api/metrics":
                try:
                    n = int(dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("limit", 200))
                except ValueError:
                    n = 200
                n = min(METRICS_MAX_POINTS, n) if n > 0 else 200     # 0/negativ hieß früher „alles“ (bis 40 000 Werte)
                sys_watch = getattr(cache, "sys", None) if cache else None
                try:
                    samples = sys_watch.history(n) if sys_watch else []
                    boot_ts = sys_watch.boot_ts if sys_watch else None
                except Exception as e:
                    samples, boot_ts = [], None
                    log.warning("Messwert-Verlauf nicht lesbar: %s", e)
                return self._json(200, {"samples": samples, "boot_ts": boot_ts})
            if path == "/api/runs":
                try:
                    n = int(dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("limit", 50))
                except ValueError:
                    n = 50
                store = getattr(getattr(updater, "rec", None), "store", None)
                try:
                    jobs = store.list_jobs(n) if store else []
                except Exception as e:
                    jobs = []
                    log.warning("Lauf-Liste nicht lesbar: %s", e)
                return self._json(200, {"jobs": jobs})
            if path.startswith("/api/runs/"):
                try:
                    job_id = int(path[len("/api/runs/"):])
                except ValueError:
                    return self._json(400, {"error": "ungültige Lauf-ID"})
                store = getattr(getattr(updater, "rec", None), "store", None)
                try:
                    lines = store.job_lines(job_id) if store else []
                except Exception as e:
                    lines = []
                    log.warning("Lauf-Zeilen nicht lesbar: %s", e)
                return self._json(200, {"id": job_id, "lines": lines})
            self._json(404, {"error": "nicht gefunden"})

        # ---- POST ----------------------------------------------------------
        def do_POST(self):
            self._guarded(self._post)

        def _post(self):
            path = self.path.partition("?")[0]
            if not path.startswith("/api/") or (path[5:] not in ACTIONS and path[5:] not in MAINT_ACTIONS):
                return self._json(404, {"error": "nicht gefunden"})
            if not self._host_ok():
                return self._json(403, {"error": HOST_FORBIDDEN})
            if self.headers.get("X-Watchdog") != "1":
                return self._json(403, {"error": "Header X-Watchdog: 1 fehlt"})
            if cfg.token and not token_matches(self.headers.get("X-Token"), cfg.token):
                return self._json(401, {"error": "Token fehlt oder ist falsch"})

            body = {}
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._json(400, {"error": "Content-Length ist keine Zahl"})
            if length < 0:
                return self._json(400, {"error": "Content-Length ungültig"})
            if length:
                if length > MAX_BODY:
                    return self._json(413, {"error": "Anfrage zu groß"})
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._json(400, {"error": "kein gültiges JSON"})
                if not isinstance(body, dict):
                    return self._json(400, {"error": "JSON-Objekt erwartet"})

            if path[5:] in MAINT_ACTIONS:
                maint = getattr(cache, "maint", None) if cache else None
                if maint is None:
                    return self._json(503, {"error": "Wartungsmodus nicht verfügbar"})
                if path[5:] == "maintenance":
                    minutes = body.get("minutes", 120)
                    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 5 <= minutes <= 1440:
                        return self._json(400, {"error": "minutes muss eine ganze Zahl von 5 bis 1440 sein"})
                    view = maint.start(minutes)
                else:
                    view = maint.end()
                cache.poke()
                return self._json(200, view)

            reason = getattr(updater, "unavailable", None)
            if reason:                                  # Notbetrieb: Oberfläche geht, Aktionen nicht
                return self._json(503, {"error": f"Aktion nicht möglich: {reason}"})
            action = path[5:]
            kwargs = {"force": bool(body.get("force"))} if action == "update" else {}
            if updater.start(action, **kwargs):
                if cache:
                    cache.poke()
                return self._json(202, {"started": action})
            self._json(409, {"error": "Es läuft bereits eine Aktion"})

    return Handler


class GuardedServer(ThreadingHTTPServer):
    """HTTP-Server, der Fehler einzelner Verbindungen schluckt und Überlast begrenzt.

    Plätze: insgesamt `max_connections`; davon sind `local_reserved` nur für Verbindungen vom Rechner selbst (Herzschlag);
    je fremder Client-Adresse höchstens `max_per_ip`. Hinweis: Steht ein Proxy auf demselben Rechner davor (z. B. `tailscale
    serve`), zählen dessen Verbindungen als lokal; dann begrenzt nur die Gesamtzahl und die Frist je Anfrage."""

    daemon_threads = True
    request_queue_size = 32
    allow_reuse_address = True

    def __init__(self, *a, max_connections: int = MAX_CONNECTIONS, max_per_ip: int | None = None,
                 local_reserved: int | None = None, **kw):
        self._max = max(1, int(max_connections))
        self._per_ip_max = max(1, int(MAX_PER_IP if max_per_ip is None else max_per_ip))
        reserved = LOCAL_RESERVED if local_reserved is None else local_reserved
        self._reserved = max(0, min(int(reserved), self._max - 1))
        self._lock = threading.Lock()
        self._total = 0
        self._per_ip: dict[str, int] = {}
        super().__init__(*a, **kw)

    def server_close(self):
        cache = getattr(self, "status_cache", None)
        if cache:
            cache.stop()
        super().server_close()

    def handle_error(self, request, client_address):
        log.debug("Verbindungsfehler von %s", client_address, exc_info=True)    # nie auf stderr/abstürzen

    @staticmethod
    def _peer(request, client_address) -> tuple[str, bool]:
        ip = str(client_address[0]) if client_address else ""
        try:
            own = request.getsockname()[0]
        except Exception:
            own = None
        return ip, is_local_peer(ip, own)

    def _take_slot(self, ip: str, local: bool) -> bool:
        with self._lock:
            if local:
                if self._total >= self._max:
                    return False
            else:
                if self._total >= self._max - self._reserved or self._per_ip.get(ip, 0) >= self._per_ip_max:
                    return False
            self._total += 1
            self._per_ip[ip] = self._per_ip.get(ip, 0) + 1
            return True

    def _free_slot(self, ip: str) -> None:
        with self._lock:
            self._total = max(0, self._total - 1)
            n = self._per_ip.get(ip, 0) - 1
            if n > 0:
                self._per_ip[ip] = n
            else:
                self._per_ip.pop(ip, None)

    def process_request(self, request, client_address):
        ip, local = self._peer(request, client_address)
        if not self._take_slot(ip, local):
            try:                                        # überlastet: kurz und ehrlich ablehnen
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except Exception:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._free_slot(ip)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._free_slot(str(client_address[0]) if client_address else "")


def create_server(cfg: Config, updater, max_connections: int = MAX_CONNECTIONS, **limits) -> GuardedServer:
    sys_watch = backup = None
    try:                                    # Zusatzprüfungen dürfen den Start nie verhindern
        from pathlib import Path
        from .backup import Backup
        from .sysmon import SysWatch
        app_db = cfg.app_db_path or (str(Path(cfg.repo_dir) / "data" / "meshcore.db") if cfg.repo_dir else "")
        from .maintenance import app_base
        sys_watch = SysWatch(cfg.data_dir, cfg.repo_dir, app_db, bool(cfg.expect_internet),
                             app_base_url=app_base(getattr(cfg, "app_url", ""), cfg.health_url),
                             app_control_token=getattr(cfg, "app_control_token", ""))
        backup = Backup(app_db, cfg.backup_dir or str(Path(cfg.data_dir) / "backups"), cfg.backup_extra_dir, cfg.backup_keep)
    except Exception:
        logging.getLogger("watchdog").exception("System-/Sicherungsprüfung nicht verfügbar")
    maint = None
    try:
        from .maintenance import Maintenance, app_base
        maint = Maintenance(cfg.data_dir, app_base(getattr(cfg, "app_url", ""), cfg.health_url), getattr(cfg, "app_control_token", ""))
    except Exception:
        logging.getLogger("watchdog").exception("Wartungsmodus nicht verfügbar")
    cache = StatusCache(updater, usb_expect=bool(getattr(cfg, "expect_usb", False)), sys_watch=sys_watch, backup=backup, maintenance=maint)
    server = GuardedServer((cfg.host, cfg.port), make_handler(cfg, updater, cache), max_connections=max_connections, **limits)
    server.status_cache = cache
    cache.start()
    return server
