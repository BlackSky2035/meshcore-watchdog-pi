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
