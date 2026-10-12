# Sicherheit

**Bitte Sicherheitslücken nicht als öffentliches Issue melden.** Nutze stattdessen auf GitHub unter dem Reiter *Security* den Punkt
**„Report a vulnerability“** (privates Melden). So kann das Problem behoben werden, bevor es öffentlich bekannt wird.

Bitte nenne dabei:
- was betroffen ist (Programmteil, Version aus `watchdog/__init__.py`),
- wie man das Problem nachstellt,
- was ein Angreifer damit erreichen könnte.

## Wichtig zu wissen

- Dieses Projekt ist **KI-generiert** (Claude von Anthropic) und vom Veröffentlichenden **nicht überprüft**. Es gibt keine Garantie auf Antwortzeit oder Behebung,
  siehe auch den Haftungsausschluss in der [README](README.md) und die [LICENSE](LICENSE).
- Der Watchdog darf das Hauptprogramm starten, stoppen und aktualisieren (er führt `git pull` und `pip install` aus). Er ist **nicht für das offene Internet gedacht**:
  Standard ist `127.0.0.1`, davor Tailscale oder ein SSH-Tunnel. Bei `host = "0.0.0.0"` immer ein `token` setzen (im lokalen Netz im Klartext!).
- Wer auf den Branch `main` des Hauptprogramm-Repos pushen kann, kann Code auf deinen Geräten ausführen lassen: GitHub-Konto mit Zwei-Faktor-Anmeldung schützen.
- `config.toml` und `data/` sind geheim und gehören nie ins Git.

Unterstützt wird nur der jeweils neueste Stand von `main`.
