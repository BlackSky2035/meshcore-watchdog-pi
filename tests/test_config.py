from watchdog.config import load_config


def test_defaults_without_file(tmp_path, monkeypatch):
    monkeypatch.delenv("WD_PORT", raising=False)
    c = load_config(tmp_path / "nix.toml")
    assert c.port == 8100 and c.host == "127.0.0.1" and c.branch == "main"


def test_toml_sections_and_env(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text('[program]\nrepo_dir = "/x"\nstable_s = 2\ndeps_files = ["a.txt", "b.txt"]\n[server]\nport = 9000\n')
    monkeypatch.setenv("WD_TOKEN", "abc")
    c = load_config(p)
    assert (c.repo_dir, c.port, c.token, c.stable_s) == ("/x", 9000, "abc", 2.0)
    assert c.deps_files == ("a.txt", "b.txt")


def test_example_config_loads():
    from pathlib import Path
    c = load_config(Path(__file__).parent.parent / "config.example.toml")
    assert c.repo_dir == "/home/pi/meshcore-remote" and c.port == 8100


def test_allowed_hosts_from_file_and_env(tmp_path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text('[server]\nallowed_hosts = ["watchdog.heim.lan"]\n')
    monkeypatch.delenv("WD_ALLOWED_HOSTS", raising=False)
    assert load_config(p).allowed_hosts == ("watchdog.heim.lan",)
    monkeypatch.setenv("WD_ALLOWED_HOSTS", "a.lan, .heim.lan")
    assert load_config(p).allowed_hosts == ("a.lan", ".heim.lan")


def test_systemd_unit_has_a_stop_timeout_shorter_than_the_watchdog_wait():
    """Sonst wartet systemd bis zu 90 s, der Watchdog bricht vorher ab und das Update endet ohne Wiederherstellung."""
    from pathlib import Path
    from watchdog.config import Config
    unit = (Path(__file__).parent.parent / "deploy" / "meshcore-remote.service").read_text()
    line = [l for l in unit.splitlines() if l.startswith("TimeoutStopSec=")]
    assert line and int(line[0].split("=")[1]) < 60
