"""Minimaler HTTP-Server (nur Standardbibliothek) mit Status-Seite und kleiner API.

Eigener Port, unabhängig vom Hauptprogramm: antwortet auch dann, wenn dieses kaputt
oder gestoppt ist.

Schutz der ändernden Aktionen:
- Header `X-Watchdog: 1` ist Pflicht. Browser senden ihn bei fremden Webseiten nicht
  ohne Vorabprüfung (CORS), die wir nie bestätigen: blockiert Fremdaufrufe aus dem Browser.
- Optional `token`: dann muss zusätzlich `X-Token` stimmen. Empfohlen, sobald die Seite
  über das lokale Netz oder Tailscale erreichbar ist.
"""
from __future__ import annotations

import hmac
import json
import logging
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
SOCKET_TIMEOUT_S = 15.0     # hängende Verbindungen blockieren keinen Thread dauerhaft
MAX_CONNECTIONS = 48       # darüber: 503 statt unbegrenzt Threads
STARTED = time.time()

FALLBACK_PAGE = ("<!doctype html><meta charset=utf-8><title>MeshCore Watchdog</title>"
                 "<h1>MeshCore Watchdog</h1><p>Die Oberfläche (static/index.html) fehlt, der Watchdog läuft aber.</p>"
                 "<p>Status: <a href='/api/status'>/api/status</a></p>")


def make_handler(cfg: Config, updater, cache=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Watchdog"
        timeout = SOCKET_TIMEOUT_S

        def log_message(self, fmt, *args):          # nicht jede Anfrage ins Log schreiben
            log.debug("%s %s", self.address_string(), fmt % args)

        # ---- Antworten ---------------------------------------------------
        def _json(self, code: int, obj) -> None:
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
                try:
                    lines = updater.log_tail(n, since=since, until=until)
                except Exception as e:
                    lines = [f"(Programm-Protokoll nicht lesbar: {e})"]
                return self._json(200, {"lines": lines})
            if path == "/api/metrics":
                try:
                    n = int(dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("limit", 200))
                except ValueError:
                    n = 200
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
            if self.headers.get("X-Watchdog") != "1":
                return self._json(403, {"error": "Header X-Watchdog: 1 fehlt"})
            if cfg.token and not hmac.compare_digest(self.headers.get("X-Token", ""), cfg.token):
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
    """HTTP-Server, der Fehler einzelner Verbindungen schluckt und Überlast begrenzt."""

    daemon_threads = True
    request_queue_size = 32
    allow_reuse_address = True

    def __init__(self, *a, max_connections: int = MAX_CONNECTIONS, **kw):
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(*a, **kw)

    def server_close(self):
        cache = getattr(self, "status_cache", None)
        if cache:
            cache.stop()
        super().server_close()

    def handle_error(self, request, client_address):
        log.debug("Verbindungsfehler von %s", client_address, exc_info=True)    # nie auf stderr/abstürzen

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:                                        # überlastet: kurz und ehrlich ablehnen
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except Exception:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def create_server(cfg: Config, updater, max_connections: int = MAX_CONNECTIONS) -> GuardedServer:
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
    server = GuardedServer((cfg.host, cfg.port), make_handler(cfg, updater, cache), max_connections=max_connections)
    server.status_cache = cache
    cache.start()
    return server
