"""Dünne Hülle um die git-Kommandozeile. Verändert nie etwas ohne ausdrücklichen Aufruf."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path


class GitError(Exception):
    pass


def _run(repo: str, *args: str, timeout: float = 60) -> str:
    # nie nach Passwort fragen; GIT_OPTIONAL_LOCKS=0: `git status` (Statusabfrage alle 3 s) legt kein index.lock an –
    # sonst scheiterte ein gleichzeitiges Update/Zurücksetzen zufällig an „Unable to create index.lock“
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        p = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]}: Zeitüberschreitung nach {timeout:.0f} s")
    except FileNotFoundError:
        raise GitError("git ist nicht installiert")
    if p.returncode != 0:
        raise GitError(f"git {args[0]}: {(p.stderr or p.stdout).strip() or 'Fehler'}")
    return p.stdout.strip()


def head(repo: str) -> str:
    return _run(repo, "rev-parse", "HEAD")


def current_branch(repo: str) -> str:
    return _run(repo, "rev-parse", "--abbrev-ref", "HEAD")


def is_dirty(repo: str) -> bool:
    """Geänderte, nachverfolgte Dateien. Nicht nachverfolgte Dateien (config.toml, data/) zählen nicht."""
    return bool(_run(repo, "status", "--porcelain", "--untracked-files=no"))


def fetch(repo: str, remote: str, timeout: float) -> None:
    _run(repo, "fetch", "--quiet", remote, timeout=timeout)


def rev(repo: str, ref: str) -> str:
    return _run(repo, "rev-parse", ref)


def behind_count(repo: str, remote: str, branch: str) -> int:
    return int(_run(repo, "rev-list", "--count", f"HEAD..{remote}/{branch}"))


def is_ancestor(repo: str, older: str, newer: str) -> bool:
    try:
        _run(repo, "merge-base", "--is-ancestor", older, newer)
        return True
    except GitError:
        return False


def merge_ff(repo: str, remote: str, branch: str) -> None:
    _run(repo, "merge", "--ff-only", "--quiet", f"{remote}/{branch}")


def reset_hard(repo: str, commit: str) -> None:
    _run(repo, "reset", "--hard", "--quiet", commit)


STALE_LOCK_S = 120          # jeder git-Aufruf des Watchdogs ist nach höchstens 90 s beendet; älter = Überbleibsel (Stromausfall, SIGKILL)


def index_lock(repo: str) -> Path:
    return Path(repo) / ".git" / "index.lock"


def clear_stale_index_lock(repo: str, older_than_s: float = STALE_LOCK_S, now=time.time) -> bool:
    """Entfernt ein verwaistes .git/index.lock. Ein frisches (anderer git-Prozess läuft gerade) bleibt unangetastet.
    True = jetzt ist keine Sperre (mehr) da. Wirft nie."""
    lock = index_lock(repo)
    try:
        age = now() - lock.stat().st_mtime
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if age < older_than_s:
        return False
    try:
        lock.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def untracked_collisions(repo: str, old: str, new: str) -> list[str]:
    """Nicht nachverfolgte (und nicht ignorierte) Dateien, die ein Fast-Forward von `old` nach `new` überschreiben müsste.
    Git bricht den Merge dann ab. Ignorierte Dateien überschreibt git ohne Rückfrage, sie zählen nicht."""
    added = _run(repo, "diff", "--no-renames", "--name-only", "--diff-filter=A", old, new).splitlines()
    if not added:
        return []
    untracked = set(_run(repo, "ls-files", "--others", "--exclude-standard").splitlines())
    out = []
    for path in added:
        parts = path.split("/")
        parents = ["/".join(parts[:i]) for i in range(1, len(parts))]   # Datei, wo das Update einen Ordner braucht
        if path in untracked or any(p in untracked for p in parents):
            out.append(path)
    return sorted(out)


def changed_files(repo: str, old: str, new: str) -> set[str]:
    out = _run(repo, "diff", "--name-only", old, new)
    return set(out.splitlines())


def subjects(repo: str, rev_range: str, limit: int = 5) -> list[str]:
    out = _run(repo, "log", f"--max-count={limit}", "--format=%h %s", rev_range)
    return out.splitlines()


def short(commit: str | None) -> str:
    return commit[:7] if commit else "?"
