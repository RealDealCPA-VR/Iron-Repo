import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
sys.path.insert(0, str(ROOT))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An isolated world: its own Iron home, git config and PATH with Iron's launchers."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Tester\n\temail = tester@example.com\n"
                         "[init]\n\tdefaultBranch = main\n[core]\n\tautocrlf = false\n", encoding="utf-8")
    monkeypatch.setenv("IRON_HOME", str(tmp_path / "iron-home"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("PATH", str(BIN) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("IRON_LOCK_TIMEOUT", "30")
    monkeypatch.setenv("FAKE_RCLONE_ROOT", str(tmp_path / "fake-cloud"))
    monkeypatch.setenv("IRON_RCLONE", json.dumps([sys.executable, str(ROOT / "tests" / "fake_rclone.py")]))
    for var in ("IRON_KEY", "IRON_ALLOW_FORCE", "IRON_ALLOW_DELETE", "IRON_AGENT", "IRON_SESSION", "GIT_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(args, cwd, input=None, check=True, env=None):
    proc = subprocess.run(args, cwd=str(cwd), input=input, capture_output=True, text=True, env=env,
                          encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL if input is None else None)
    if check and proc.returncode != 0:
        raise AssertionError("%s failed (%d)\nstdout: %s\nstderr: %s" % (args, proc.returncode, proc.stdout, proc.stderr))
    return proc


def git(cwd, *args, check=True, env=None):
    return run(["git"] + list(args), cwd, check=check, env=env)


def iron(cwd, *args, check=True, input=None, env=None):
    return run([sys.executable, str(BIN / "iron_launch.py"), "cli"] + list(args), cwd, input=input, check=check, env=env)


def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit(repo, filename, text, message=None):
    write(Path(repo) / filename, text)
    git(repo, "add", "--all")
    git(repo, "commit", "-q", "-m", message or "edit %s" % filename)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def make_repo(path, files=None):
    git(path.parent, "init", "-q", "-b", "main", str(path))
    for name, text in (files or {"README.md": "hello\n"}).items():
        commit(path, name, text, "add %s" % name)
    return path


def loc(path):
    return Path(path).as_posix()
