"""Start: `python -m watchdog` (Server) oder `python -m watchdog <aktion>` (einmalig, ohne Server).

Aktionen: status, check, update, restart, start, stop, rollback
Die Kommandozeile ist der Notweg über SSH, falls die Weboberfläche nicht erreichbar ist.

Der Server-Modus ist auf Dauerbetrieb ausgelegt: Er startet auch mit kaputter Konfiguration (Notbetrieb mit
Fehlermeldung), fängt Fehler einzelner Anfragen ab, baut den HTTP-Server nach einem Absturz neu auf und meldet
systemd regelmäßig „lebt“, solange er wirklich antwortet.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import signal
import sys
import threading
import time
from pathlib import Path

from .config import Config, load_config_safe
from .health import FallbackUpdater, Heartbeat, sd_notify
from .server import create_server

ACTIONS = ["status", "check", "update", "restart", "start", "stop", "rollback"]
log = logging.getLogger("watchdog")


def setup_logging(data_dir: str, verbose: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(Path(data_dir) / "watchdog.log",
                                                             maxBytes=1_000_000, backupCount=3))
    except Exception as e:                       # Logdatei nicht möglich: nur auf die Konsole
        print(f"Warnung: Logdatei nicht nutzbar ({e})", file=sys.stderr)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def install_excepthooks() -> None:
    """Ungefangene Fehler (auch in Threads) ins Log schreiben statt still zu verschwinden."""
    def hook(exc_type, exc, tb):
        log.error("Ungefangener Fehler", exc_info=(exc_type, exc, tb))

    def thread_hook(args):
        log.error("Ungefangener Fehler im Thread %s", getattr(args.thread, "name", "?"),
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    sys.excepthook = hook
    threading.excepthook = thread_hook


def build_updater(cfg: Config, config_error: str | None):
    """Baut den echten Updater; schlägt etwas fehl, kommt der Notbetrieb mit Erklärung zurück. Wirft nie."""
    if config_error:
        return FallbackUpdater(config_error)
    if not cfg.repo_dir:
        return FallbackUpdater("repo_dir ist nicht gesetzt (config.toml oder WD_REPO_DIR).")
    try:
        from .managers import make_manager
        from .updater import Updater
        return Updater(cfg, make_manager(cfg))
    except Exception as e:
        log.exception("Updater konnte nicht aufgebaut werden")
        return FallbackUpdater(f"{type(e).__name__}: {e}")


def serve(cfg: Config, updater, stop: threading.Event, make_server=create_server,
          retry_min: float = 1.0, retry_max: float = 30.0) -> None:
    """Hält den HTTP-Server am Leben: Schlägt das Binden des Ports fehl (belegt) oder bricht der Server
    ab, wird nach kurzer Wartezeit (wachsend bis `retry_max`) neu gestartet. Kehrt nur bei `stop` zurück."""
    delay = retry_min
    while not stop.is_set():
        server = None
        try:
            server = make_server(cfg, updater)
            serve.current = server
            log.info("Watchdog lauscht auf http://%s:%s", cfg.host, server.server_address[1])
            delay = retry_min
            started = time.monotonic()
            server.serve_forever()
            if time.monotonic() - started > 60:
                delay = retry_min
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            log.error("HTTP-Server gestoppt (%s: %s) – neuer Versuch in %.0f s", type(e).__name__, e, delay)
        finally:
            serve.current = None
            if server is not None:
                try:
                    server.server_close()
                except Exception:
                    pass
        if stop.wait(delay):
            break
        delay = min(delay * 2, retry_max)


serve.current = None


def run_demo(verbose: bool) -> int:
    from .demo import DemoUpdater
    cfg = Config()                               # 127.0.0.1:8100, kein Token
    setup_logging(cfg.data_dir, verbose)
    updater = DemoUpdater(cfg)
    stop = threading.Event()
    print(f"DEMO-Watchdog (simuliert, ändert nichts) auf http://{cfg.host}:{cfg.port}/  –  Beenden mit Strg+C")
    try:
        serve(cfg, updater, stop)
    except KeyboardInterrupt:
        stop.set()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="watchdog")
    ap.add_argument("action", nargs="?", choices=ACTIONS, help="einmalige Aktion statt Server")
    ap.add_argument("--config", help="Pfad zur config.toml")
    ap.add_argument("--force", action="store_true", help="bei update: auch als fehlerhaft markierten Stand versuchen")
    ap.add_argument("--demo", action="store_true", help="simulierter Watchdog zum Ausprobieren (nichts wird verändert)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    install_excepthooks()
    if args.demo:
        return run_demo(args.verbose)

    cfg, config_error = load_config_safe(args.config)
    setup_logging(cfg.data_dir, args.verbose)
    updater = build_updater(cfg, config_error)

    if args.action:                              # Kommandozeile: hier ist ein Fehler ein Fehler
        if isinstance(updater, FallbackUpdater):
            print(f"Fehler: {updater.unavailable}", file=sys.stderr)
            return 2
        if args.action == "status":
            print(json.dumps(updater.status(), indent=2, default=str))
            return 0
        kwargs = {"force": args.force} if args.action == "update" else {}
        ok, message = updater.run(args.action, **kwargs)
        for line in updater.job["log"]:
            print(line["text"])
        print(("OK: " if ok else "FEHLER: ") + message)
        return 0 if ok else 1

    stop = threading.Event()

    def shutdown(*_):
        stop.set()
        srv = serve.current
        if srv is not None:
            threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    if isinstance(updater, FallbackUpdater):
        log.error("NOTBETRIEB: %s", updater.unavailable)
    else:
        log.info("Watchdog %s (Manager: %s, Projekt: %s)", __import__("watchdog").__version__,
                 updater.mgr.name, cfg.repo_dir)
    if cfg.host not in ("127.0.0.1", "localhost") and not cfg.token:
        log.warning("Der Watchdog ist im Netz erreichbar, aber es ist kein token gesetzt!")
    elif not cfg.token:                          # 127.0.0.1 hinter `tailscale serve`: im ganzen Tailnet erreichbar
        log.info("Hinweis: kein token gesetzt. Hinter tailscale serve kann jeder im Tailnet Aktionen auslösen.")

    # Autostart-Prüfung im Hintergrund: ein langsamer Dienst-Manager verzögert den Start der Oberfläche nicht
    threading.Thread(target=_safe_startup, args=(updater,), name="startup", daemon=True).start()
    beat = Heartbeat(lambda: serve.current.server_address[1] if serve.current else cfg.port, cfg.host)
    beat.start()
    sd_notify("READY=1")
    try:
        serve(cfg, updater, stop)
    except KeyboardInterrupt:
        pass
    finally:
        beat.stop()
        sd_notify("STOPPING=1")
    return 0


def _safe_startup(updater) -> None:
    try:
        updater.startup()
    except BaseException:
        log.exception("Start-Prüfung fehlgeschlagen")


if __name__ == "__main__":
    sys.exit(main())
