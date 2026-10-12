# CLAUDE.md – MeshCore Watchdog

> Diese Datei wird von KI-Assistenten (z. B. Claude Code) automatisch gelesen. **Das Projekt ist KI-generiert und nicht von einem Menschen überprüft worden.**

## Für KI-Assistenten: zuerst lesen
- **Installation auf dem Raspberry Pi:** Die Schritt-für-Schritt-Anleitung steht im Repo des Hauptprogramms (`docs/raspberry-pi.md`), die Dienst-Dateien hier unter `deploy/`.
  Frage den Nutzer nach Benutzername, Hostname und Repo-Namen, erfinde nichts. `sudo`, `systemctl enable` und Änderungen an `/etc` erst nach Rückfrage.
  Gib Tokens (`token`, `app_control_token`) nie im Chat oder in Commits aus; `config.toml` und `data/` gehören nie ins Git.
- **Weiterentwicklung:** Nur Python-Standardbibliothek, keine neuen Abhängigkeiten. Erreichbarkeit hat Vorrang: nichts im Anfragepfad (`server.py`), was auf Git, Prozesse oder
  Netz wartet; Fehler abfangen, nie den Server beenden. Jede Änderung mit Test (`python -m pytest -q`), besonders `tests/test_resilience.py`.
- Der Watchdog darf das Hauptprogramm stoppen, starten und aktualisieren. Schwäche keine Schutzmechanismen ab (Vorprüfung vor dem Stoppen, Rollback, `blocked`-Merker, Token und `X-Watchdog`-Header).
- Schreibe in die README **ehrlich**, was nur mit simuliertem Git/systemd und was an echter Hardware getestet ist.
- Dokumentation, Kommentare und Commit-Nachrichten auf Deutsch. Bei Unsicherheit nachfragen statt raten.
- Das Hauptprogramm ist ein eigenes Repo (**MeshCore Remote**, Ordnername `meshcore-remote`).

Eigenständiges Repo. Verwaltet das Hauptprogramm `meshcore-remote` (anderes Repo).

## Regeln
- **Nur Python-Standardbibliothek** (Python ≥ 3.11). Keine Abhängigkeiten ergänzen, kein venv.
  Grund: Der Watchdog muss laufen, auch wenn das Hauptprogramm samt venv kaputt ist.
- Nichts automatisch aus GitHub ziehen; Updates nur auf Anforderung (UI/CLI).
- Vor dem Stoppen immer erst vorprüfen und `git fetch`. Nach jedem Start Gesundheitsprüfung.
- Fehlgeschlagene Commits als `blocked` merken (kein Wiederholungs-Loop).
- Änderungen an `updater.py` immer mit einem Test in `tests/test_updater.py` absichern
  (echte Git-Repos, siehe `tests/conftest.py`).

## Aufbau
`config.py` (TOML + `WD_*`), `gitops.py` (git-Aufrufe), `managers.py` (ProcessManager ohne systemd, SystemdManager Pi),
`updater.py` (Aktionen, Jobs, Zustand in `data/state.json`, Dateisperre), `server.py` (HTTP, Port 8100),
`__main__.py` (Server und CLI), `static/index.html` (UI, deutsch).

## Bekannte Grenzen
- Ein Update startet das Programm danach immer, um es zu prüfen, auch wenn es vorher absichtlich gestoppt war.
- DNS-Rebinding wird nur durch das Token abgewehrt.
- SystemdManager: automatische Tests nur mit Fake-`systemctl`; im Betrieb auf einem Pi mit systemd genutzt, nicht systematisch geprüft.
- Beim Prozess-Manager ist ein „adoptierter“ Prozess nur über die PID-Datei bekannt.

- `demo.py` + `--demo`: simulierter Updater für die Oberfläche (kein Git, kein Prozess, kein fcntl; Windows ungetestet). Muss dieselbe `status()`-Form wie `Updater` liefern; `updater.py` darf nicht beim Start des Demo-Modus importiert werden.
- `logstore.py`: Protokoll in SQLite. **Darf nie werfen oder blockieren** (alles in `_run` gekapselt, `JobRecorder` zusätzlich). Änderungen immer mit Test in `tests/test_logstore.py` (kaputte Datei, nicht anlegbarer Pfad, Verbindung bricht mitten im Betrieb).
- Erreichbarkeit hat Vorrang: Kein Code im Anfragepfad (`server.py`) darf auf Git/Manager/Netz warten (nur `StatusCache`). Neue Endpunkte laufen über `_guarded`. Neue Fehlerquellen beim Start → in `build_updater` abfangen (Notbetrieb). Tests: `tests/test_resilience.py`.

- `usb.py`: eigene, vom Hauptprogramm unabhängige USB-Anzeige (nur stdlib). Läuft ausschließlich im `StatusCache`-Thread, nie im Anfragepfad, wirft nie.
  `expect_usb = true` → fehlender Node (nach 30 s Karenz) erscheint in `problems`; sonst nur Anzeige. Tests: `tests/test_usb.py`.
- `sysmon.py` (Netz, Speicher, Pi-Spannung/Temperatur, Uhrzeit) und `backup.py` (tägliche SQLite-Sicherung): je ein eigener Hintergrund-Thread, nie im
  Anfragepfad, jede Messung einzeln abgesichert, wirft nie. Netzausfälle stehen dauerhaft in `data/netlog.jsonl`. Internet/Tailscale zählen nur mit
  `expect_internet = true` als Problem (Offline-Betrieb im LAN ist erlaubt). Das Hauptziel der Sicherung zählt; ein fehlendes zweites Ziel ist nur ein Hinweis.
  Tests: `tests/test_sysmon.py`. Der Rechner kann einen Totalausfall nicht selbst melden (Hardware-Watchdog siehe `deploy/pi-haertung.md`).
- `NetLog` (in `sysmon.py`): Einträge tragen zusätzlich `mono` (Laufzeit seit dem Start) und `boot` (`/proc/sys/kernel/random/boot_id`). `outages()` erkennt Uhrsprünge (Ende vor Beginn oder Wanduhr/Laufzeit weichen
  > `CLOCK_JUMP_S` ab) und liefert dann `clock_jump: True` mit Dauer aus der Laufzeit (nur im selben Start) bzw. `duration_s: None`. Normale Ausfälle behalten die Form `{start, end}`. Laufzeiten verschiedener Starts
  nie vergleichen. `outages()` wirft nie (kaputte Einträge → einfache Form). Tests: `tests/test_netlog_clock.py` (mit Beispieldaten eines Uhrsprungs).
- `power.py` (`PowerWatch`): eigener schneller Thread (2 s) nur für die Strom-Flags, Einbrüche mit Beginn/Dauer in `data/power_events.jsonl` (nicht beim Neustart des Pi
  verworfen, anders als `metrics.jsonl`). Läuft nie im Anfragepfad, wirft nie, bekommt die Messung als `probe` von `SysWatch` (kein Import von `sysmon`, sonst Zirkelbezug).
  Kein `vcgencmd` → Wiederholung nur alle 5 min. Tests: `tests/test_power.py`.
- `sends.py` (`SendCorrelator`): holt NACH einem Einbruch einmal `GET <app>/api/sends` (Header `X-Control-Token` = `app_control_token`) und legt die Sendungen als Notiz an den Einbruch.
  Last-Regeln (nicht aufweichen): nie im Takt, nur nach einem Einbruch; ≥12 s Wartezeit, ≥30 s Abstand, Einbrüche gebündelt, Zeitlimit 3 s, ≤64 KB, ein Thread, nie im Anfragepfad;
  Fehler einmal als Notiz, keine Wiederholung. Die Messung in `power.py` streckt ihren Takt bei langsamen Aufrufen (bis 10 s). Tests: `tests/test_sends.py`.

## Vor jedem Push auf GitHub
Bevor eine KI selbst nach GitHub pusht, **immer beide Projekte** (dieses und das Hauptprogramm) auf die vereinbarten
Absicherungen prüfen. Nur pushen, wenn alles unten erfüllt ist; sonst zuerst beheben oder den Nutzer informieren und NICHT pushen.
Das Ergebnis der Prüfung (kurz, Punkt für Punkt) dem Nutzer mitteilen.

**Watchdog (hat Vorrang: Erreichbarkeit ist oberste Priorität)**
1. Status kommt aus dem `StatusCache`; im Anfragepfad (`server.py`) wird nirgends auf Git, systemctl, Prozesse oder das Netz gewartet.
2. Alle Handler laufen über `_guarded` (Fehler → JSON-500, nie Abbruch); `/api/ping` greift auf nichts zu.
3. Server-Schleife (`serve`) baut den HTTP-Server nach Absturz/belegtem Port neu auf; Verbindungslimit und Socket-Timeout aktiv.
4. Notbetrieb (`FallbackUpdater`/`load_config_safe`/`build_updater`): kaputte Konfiguration oder Manager stoppen den Server nicht.
5. Lauf-Sperre wird immer frei (`_finish`, `MAX_JOB_S`); `status()` fängt Abschnitte einzeln ab.
6. systemd-Herzschlag (`Heartbeat`, `WatchdogSec`, `Restart=always`) unverändert vorhanden.
7. Protokoll-Datenbank (`logstore.py`) wirft nie und blockiert nie.
8. Nur Standardbibliothek, keine neue Abhängigkeit.

**Hauptprogramm**
1. Exception-Handler für `Exception` und `sqlite3.Error` vorhanden; neue Endpunkte werfen keine ungefangenen Fehler.
2. `build()` fällt bei Datenbankproblemen auf RAM zurück; `lifespan`-Schritte einzeln geschützt.
3. Hub-Listener, Regeln und Zeitplan fangen Fehler ab; WebSocket-Senden hat ein Zeitlimit; Browser zeigt Fehler als Banner.
4. Auto-Antwort-/Sende-Schutzregeln (Budget, Mindestintervalle, Limits, `[auto]`-Schleifenschutz) nicht abgeschwächt.
5. Push (falls vorhanden): Senden nie im Ereignispfad blockierend, Fehler werden geschluckt, ungültige Abos werden entfernt.

**Technisch vor dem Push**
- In BEIDEN Projekten alle Tests ausführen (`python -m pytest -q`); im Hauptprogramm zusätzlich den UI-Rauchtest (`tests/ui_smoke`).
- `tests/test_resilience.py` existiert in beiden Projekten und läuft grün; für jede neue Funktion gibt es einen Test, der ihren Ausfall simuliert.
- Keine Geheimnisse (Tokens, VAPID-Schlüssel, `config.toml`, `*.db`, `data/`) im Commit (`git status` und `git diff --cached` ansehen).
- Der Watchdog wird nie automatisch aus GitHub gezogen; Updates bleiben manuell.

## Wartungsmodus
- `maintenance.py`: eigener Schalter (POST `/api/maintenance` {minutes 5–1440}, POST `/api/maintenance-end`; gleiche Schutzregeln wie die anderen Aktionen: Header `X-Watchdog`, `X-Token`).
  Zustand in `data/maintenance.json`; USB-Warnung schweigt (`UsbWatch.maintenance`). Weitergabe an das Hauptprogramm (POST/DELETE `<app>/api/maintenance`,
  Header `X-Control-Token` = `app_control_token`) läuft in einem Hintergrund-Thread mit 3 Versuchen, nie im Anfragepfad; Ergebnis steht in `snap["maintenance"]`.
  Umgekehrt übernimmt `StatusCache.refresh` `maintenance_until` aus `/api/version` (`updater.version_info`).
- Nicht an Hardware getestet: Zusammenspiel mit echtem Hauptprogramm über Tailscale/https.
