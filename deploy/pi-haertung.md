# Raspberry Pi für den Dauerbetrieb härten

Ungetestet auf echter Hardware; die Befehle sind Standard unter Raspberry Pi OS (Bookworm). Vor dem Ändern ein Abbild der Karte ziehen.

## 1. Hardware-Watchdog (der Pi startet sich bei einem Systemhänger selbst neu)
```bash
sudo mkdir -p /etc/systemd/system.conf.d
printf '[Manager]\nRuntimeWatchdogSec=15\nRebootWatchdogSec=2min\n' | sudo tee /etc/systemd/system.conf.d/watchdog.conf
sudo systemctl daemon-reexec
```
Prüfen: `sudo dmesg | grep -i watchdog` (sollte `bcm2835-wdt` zeigen).

## 2. Weniger Schreibzugriffe auf die SD-Karte
- Systemprotokolle nur im RAM: `/etc/systemd/journald.conf` → `Storage=volatile`, `RuntimeMaxUse=30M`, dann `sudo systemctl restart systemd-journald`.
  (Protokolle gehen bei einem Neustart verloren; das Programm führt sein eigenes Protokoll in der Datenbank.)
- Auslagerungsdatei aus: `sudo dphys-swapfile swapoff && sudo systemctl disable dphys-swapfile`
  (nur beim Pi 3 mit 1 GB sinnvoll; beim Zero 2 W mit 512 MB zuerst `free -m` beobachten).
- Karte mit hoher Haltbarkeit nehmen (z. B. „High Endurance“/„Max Endurance“).
- `retention_days` im Hauptprogramm klein halten (Standard 90).

## 3. Uhrzeit
Der Pi hat keine Echtzeituhr. Ohne Netz beim Start stimmt die Uhr nicht, und Verläufe bekommen falsche Zeiten.
Der Watchdog zeigt eine Warnung, wenn die Uhr nicht abgeglichen ist (`timedatectl`). Optional eine Echtzeituhr (RTC-Modul) nachrüsten.

## 4. Datenbanksicherung auf einen USB-Stick (optional)
Stick einstecken, einen festen Einhängepunkt einrichten (`/etc/fstab` mit `nofail`, damit der Pi auch ohne Stick bootet) und in der
`config.toml` des Watchdogs `backup_extra_dir = "/media/pi/STICK/meshcore"` setzen. Fehlt der Stick, ist das nur ein Hinweis.

## 5. Stromversorgung
Netzteil mit mindestens 2,5 A und ein kurzes, dickes Kabel. Der Watchdog meldet Unterspannung (`vcgencmd get_throttled`).
Am Pi Zero 2 W: Node und Netzwerk-Adapter über einen Hub mit eigener Stromversorgung anschließen.
