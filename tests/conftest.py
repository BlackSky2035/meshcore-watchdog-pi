"""Testumgebung mit echten Git-Repositories und einem winzigen Fake-Programm.

remote.git (bare)  <-- seed (Entwickler-Klon, hier wird "gepusht")
                   <-- app  (Klon, den der Watchdog verwaltet)
"""
import os
import socket
import subprocess
import sys
import textwrap

import pytest

from watchdog.config import Config
from watchdog.managers import ProcessManager
from watchdog.updater import Updater

FAKE_APP = textwrap.dedent('''
    import http.server, os, sys, threading, time
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    def flag(n): return os.path.exists(os.path.join(here, n))
    if flag("BROKEN"):
        print("kaputt: Import-Fehler", flush=True); sys.exit(3)
    version = open(os.path.join(here, "VERSION")).read().strip()
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            b = ('{"version": "%s"}' % version).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def log_message(self, *a): pass
    if flag("CRASH"):
        threading.Timer(2.0, lambda: os._exit(4)).start()
    http.server.ThreadingHTTPServer(("127.0.0.1", int(os.environ["FAKE_PORT"])), H).serve_forever()
''')


def git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.port = free_port()
        monkeypatch.setenv("FAKE_PORT", str(self.port))
        self.remote = tmp_path / "remote.git"
        self.seed = tmp_path / "seed"
        self.app = tmp_path / "app"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.remote)], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(self.remote), str(self.seed)], check=True, capture_output=True)
        (self.seed / "app").mkdir()
        (self.seed / "app" / "main.py").write_text(FAKE_APP)
        (self.seed / "app" / "__init__.py").write_text("")
        (self.seed / "requirements.txt").write_text("# leer\n")
        self.push("v1", VERSION="1\n")
        subprocess.run(["git", "clone", str(self.remote), str(self.app)], check=True, capture_output=True)
        git(self.app, "checkout", "-q", "main")
        self.cfg = Config(
            repo_dir=str(self.app), manager="process",
            start_command=f"{sys.executable} -m app.main",
            health_url=f"http://127.0.0.1:{self.port}/api/version",
            health_timeout_s=10, stable_s=1.0, stop_timeout_s=5,
            install_command=f"{sys.executable} -c \"open('INSTALLED','a').write('x')\"",
            data_dir=str(tmp_path / "wd-data"), port=0)
        self.mgr = ProcessManager(self.cfg.start_command, self.cfg.repo_dir, self.cfg.data_dir)
        self.up = Updater(self.cfg, self.mgr)

    def push(self, msg, **files):
        for name, content in files.items():
            (self.seed / name).write_text(content)
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-q", "-m", msg)
        git(self.seed, "push", "-q", "origin", "main")
        return git(self.seed, "rev-parse", "HEAD")

    def version(self):
        import urllib.request, json
        with urllib.request.urlopen(self.cfg.health_url, timeout=2) as r:
            return json.load(r)["version"]

    def head(self):
        return git(self.app, "rev-parse", "HEAD")


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    yield e
    try:
        e.mgr.stop(3)
    except Exception:
        pass
