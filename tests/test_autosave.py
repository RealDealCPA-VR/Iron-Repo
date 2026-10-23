"""iron autosave and iron autosync: automatic saving and syncing, without ever forcing anything."""

import os
import time
from pathlib import Path

import pytest

from conftest import commit, git, iron, loc, make_repo, write


@pytest.fixture
def home(env, monkeypatch):
    """Point every per-user location (Startup folder, LaunchAgents, systemd) at the temp dir."""
    for var in ("APPDATA", "HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(env / "user"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(env / "user" / ".config"))
    for var in ("IRON_AUTOSAVE_DRY_RUN", "IRON_AUTOSAVE_PLATFORM", "IRON_AUTOSYNC_SKIP"):
        monkeypatch.delenv(var, raising=False)
    return env


def published(repo, remote_dir):
    """The commit the remote holds for main, read straight from the remote."""
    out = git(repo, "ls-remote", "iron::" + loc(remote_dir), "refs/heads/main").stdout.split()
    return out[0] if out else None


def head(repo):
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def synced_repo(env, name="p"):
    repo = make_repo(env / name)
    iron(repo, "remote", "add", "origin", loc(env / "remote.iron"))
    iron(repo, "sync")
    return repo


def test_once_saves_a_version_and_syncs_it(home):
    repo = synced_repo(home)
    write(repo / "notes.md", "draft\n")
    write(repo / "src" / "app.py", "print(1)\n")
    out = iron(repo, "autosave", "--once")
    assert git(repo, "log", "-1", "--format=%s").stdout.strip() == "Autosave: 2 file(s) changed"
    assert "Iron-Autosave: true" in git(repo, "log", "-1", "--format=%b").stdout
    assert published(repo, home / "remote.iron") == head(repo)
    assert "Saved 2 changed file(s)" in out.stdout
    log = Path(os.environ["IRON_HOME"]) / "logs"
    assert any(p.name.startswith("autosave-p-") and p.suffix == ".log" for p in log.iterdir())

    # nothing changed: no new version, but it still syncs quietly
    before = head(repo)
    out = iron(repo, "autosave", "--once")
    assert head(repo) == before and "Nothing changed" in out.stdout
    assert "Nothing changed" not in iron(repo, "autosave", "--once", "--quiet").stdout


def test_once_skips_while_a_merge_or_another_git_command_is_running(home):
    repo = synced_repo(home)
    git(repo, "switch", "-q", "-c", "other")
    commit(repo, "README.md", "theirs\n")
    git(repo, "switch", "-q", "main")
    commit(repo, "README.md", "ours\n")
    assert git(repo, "merge", "other", check=False).returncode != 0  # conflict: merge in progress
    before = head(repo)
    out = iron(repo, "autosave", "--once", check=False)  # conflicted files stop autosave, loudly
    assert out.returncode == 1 and "Files conflict between your computers: README.md" in out.stdout
    assert head(repo) == before
    git(repo, "merge", "--abort")
    git(repo, "switch", "-q", "-c", "clean-side")
    commit(repo, "side.txt", "s\n")
    git(repo, "switch", "-q", "main")
    git(repo, "merge", "--no-commit", "--no-ff", "clean-side")  # a merge in progress, nothing conflicted
    out = iron(repo, "autosave", "--once")
    assert "a merge is in progress" in out.stdout and head(repo) == before
    git(repo, "merge", "--abort")

    write(repo / "new.txt", "x\n")
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    out = iron(repo, "autosave", "--once")
    assert "another git command is running" in out.stdout and head(repo) == before
    lock.unlink()
    iron(repo, "autosave", "--once")
    assert head(repo) != before


def test_secrets_are_never_saved(home):
    repo = synced_repo(home)
    write(repo / ".env", "TOKEN=abc\n")
    write(repo / "deploy" / "id_rsa", "x\n")
    write(repo / "config" / "server.txt", "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")
    write(repo / ".env.example", "TOKEN=\n")
    write(repo / "app.py", "print(1)\n")
    out = iron(repo, "autosave", "--once")
    files = set(git(repo, "ls-files").stdout.split())
    assert {"app.py", ".env.example"} <= files
    assert not files & {".env", "deploy/id_rsa", "config/server.txt"}
    assert "look like secrets" in out.stdout and ".env" in out.stdout

    # a secret someone staged by hand stops the save entirely
    git(repo, "add", "-f", ".env")
    write(repo / "app.py", "print(2)\n")
    before = head(repo)
    out = iron(repo, "autosave", "--once")
    assert head(repo) == before and "Not saving" in out.stdout


def test_refuses_without_an_iron_remote(home):
    repo = make_repo(home / "p")
    write(repo / "a.txt", "a\n")
    out = iron(repo, "autosave", "--once", check=False)
    assert out.returncode == 1 and "iron setup" in out.stderr
    assert git(repo, "status", "--porcelain").stdout.strip()  # nothing was committed
    assert iron(repo, "autosync", "on", check=False).returncode == 1


def test_offline_saves_locally_and_retries_later(home):
    repo = synced_repo(home)
    os.rename(str(home / "remote.iron"), str(home / "unplugged.iron"))  # the USB disk is gone
    write(repo / "a.txt", "a\n")
    out = iron(repo, "autosave", "--once")
    assert out.returncode == 0 and "Could not sync right now" in out.stdout
    saved = head(repo)
    assert git(repo, "log", "-1", "--format=%s").stdout.startswith("Autosave:")
    os.rename(str(home / "unplugged.iron"), str(home / "remote.iron"))
    iron(repo, "autosave", "--once")  # clean now, but the next tick publishes what was waiting
    assert published(repo, home / "remote.iron") == saved


def test_stops_on_conflicts_with_a_clear_message(home):
    a = synced_repo(home, "a")
    iron(home, "clone", loc(home / "remote.iron"), str(home / "b"))
    b = home / "b"
    commit(a, "README.md", "from a\n")
    iron(a, "sync")
    write(b / "README.md", "from b\n")
    out = iron(b, "autosave", "--once", check=False)
    assert out.returncode == 1 and "Autosave stopped" in out.stdout and "iron commit" in out.stdout
    assert git(b, "log", "-1", "--format=%s", "HEAD").stdout.startswith("Autosave:")  # b's work is saved
    assert published(a, home / "remote.iron") == head(a)  # nothing was forced over a's version
    out = iron(b, "autosave", "--once", check=False)  # and it keeps saying so
    assert out.returncode == 1 and "Files conflict between your computers: README.md" in out.stdout


def wait_for(condition, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.5)
    return False


def test_autosync_hook_publishes_every_commit(home):
    repo = synced_repo(home)
    iron(repo, "autosync", "on")
    hook = repo / ".git" / "hooks" / "post-commit"
    assert "iron autosync" in hook.read_text(encoding="utf-8")
    assert "on" in iron(repo, "autosync").stdout
    started = time.time()
    sha = commit(repo, "a.txt", "a\n")
    assert time.time() - started < 15  # the commit does not wait for the sync
    assert wait_for(lambda: published(repo, home / "remote.iron") == sha), "the hook did not sync"
    logs = Path(os.environ["IRON_HOME"]) / "logs"
    assert any(p.name.startswith("autosync-") for p in logs.iterdir())

    iron(repo, "autosync", "off")
    assert not hook.exists()
    sha = commit(repo, "b.txt", "b\n")
    time.sleep(2)
    assert published(repo, home / "remote.iron") != sha


def test_autosync_chains_existing_hooks_and_removes_only_its_part(home):
    repo = synced_repo(home)
    hook = repo / ".git" / "hooks" / "post-commit"
    original = "#!/bin/sh\necho ran >> \"$(git rev-parse --git-dir)/user-hook-ran\"\nexit 0\n"
    write(hook, original)
    os.chmod(str(hook), 0o755)
    iron(repo, "autosync", "on")
    iron(repo, "autosync", "on")  # twice is harmless
    text = hook.read_text(encoding="utf-8")
    assert text.count("# >>> iron autosync >>>") == 1 and "user-hook-ran" in text
    assert text.index("iron autosync") < text.index("exit 0")  # runs before the user's `exit`
    sha = commit(repo, "a.txt", "a\n")
    assert (repo / ".git" / "user-hook-ran").is_file()
    assert wait_for(lambda: published(repo, home / "remote.iron") == sha), "the hook did not sync"
    iron(repo, "autosync", "off")
    assert hook.read_text(encoding="utf-8") == original

    # a hook in another language is kept aside and still runs
    py_hook = "#!/usr/bin/env python3\nprint('hi')\n"
    write(hook, py_hook)
    iron(repo, "autosync", "on")
    assert (hook.parent / "post-commit.iron-chained").read_text(encoding="utf-8") == py_hook
    assert "post-commit.iron-chained" in hook.read_text(encoding="utf-8")
    iron(repo, "autosync", "off")
    assert hook.read_text(encoding="utf-8") == py_hook and not (hook.parent / "post-commit.iron-chained").exists()


def test_autosync_respects_core_hooks_path(home):
    repo = synced_repo(home)
    git(repo, "config", "core.hooksPath", ".githooks")
    iron(repo, "autosync", "on")
    assert (repo / ".githooks" / "post-commit").is_file()
    assert not (repo / ".git" / "hooks" / "post-commit").exists()
    iron(repo, "autosync", "off")
    assert not (repo / ".githooks" / "post-commit").exists()


def test_autosave_commits_do_not_trigger_a_second_sync(home):
    repo = synced_repo(home)
    iron(repo, "autosync", "on")
    write(repo / "a.txt", "a\n")
    iron(repo, "autosave", "--once")
    assert published(repo, home / "remote.iron") == head(repo)
    time.sleep(1)
    logs = Path(os.environ["IRON_HOME"]) / "logs"
    assert not any(p.name.startswith("autosync-") for p in logs.iterdir())


@pytest.mark.parametrize("platform,expect", [
    ("windows", ["Startup", "iron-autosave-p-", ".cmd", "start \"\""]),
    ("macos", ["LaunchAgents", "com.ironrepo.autosave.p-", "<key>RunAtLoad</key>", "launchctl bootstrap"]),
    ("linux-systemd", ["systemd", "iron-autosave-p-", "WantedBy=default.target", "systemctl --user enable --now"]),
    ("linux-cron", ["crontab", "@reboot", "# iron-autosave p-"]),
])
def test_install_dry_run_only_prints(home, platform, expect):
    repo = synced_repo(home)
    env = dict(os.environ, IRON_AUTOSAVE_DRY_RUN="1", IRON_AUTOSAVE_PLATFORM=platform)
    out = iron(repo, "autosave", "--install", "--every", "10m", env=env).stdout
    for text in expect:
        assert text in out, (text, out)
    assert "run_background(" in out and "600.0" in out and "[dry run] log:" in out
    # nothing was written or registered
    assert not (home / "user").exists()
    assert not (Path(os.environ["IRON_HOME"]) / "autosave").exists()
    out = iron(repo, "autosave", "--uninstall", env=env).stdout
    assert "[dry run]" in out
    out = iron(repo, "autosave", "--status", env=dict(env, IRON_AUTOSAVE_PLATFORM="windows")).stdout
    assert "Starts at login: no" in out and "Running: no" in out


def test_install_refuses_without_an_iron_remote(home):
    repo = make_repo(home / "p")
    env = dict(os.environ, IRON_AUTOSAVE_DRY_RUN="1")
    out = iron(repo, "autosave", "--install", env=env, check=False)
    assert out.returncode == 1 and "iron remote add" in out.stderr


def test_intervals_and_log_rotation(home):
    from iron import autosave

    assert autosave.parse_interval("5m") == 300 and autosave.parse_interval("30s") == 30
    assert autosave.parse_interval("1h") == 3600 and autosave.parse_interval("90") == 90
    for bad in ("soon", "1s", "-5m"):
        with pytest.raises(autosave.IronError):
            autosave.parse_interval(bad)
    path = home / "logs" / "autosave-x.log"
    log = autosave.Log(path, console=False, max_bytes=200)
    for i in range(50):
        log.info("line %d with some padding to fill the file" % i)
    log.close()
    names = sorted(p.name for p in path.parent.iterdir())
    assert names == ["autosave-x.log", "autosave-x.log.1", "autosave-x.log.2", "autosave-x.log.3"]
    assert all(p.stat().st_size <= 400 for p in path.parent.iterdir())


def test_status_reports_a_running_autosave(home):
    from iron import autosave

    repo = synced_repo(home)
    os.chdir(str(repo))
    pid = os.getpid()  # a live process that is not the one we start below
    autosave.write_state(repo, pid=pid, heartbeat=time.time(), every=300, last_result="saved", last_tick="now")
    out = iron(repo, "autosave", "--status", env=dict(os.environ, IRON_AUTOSAVE_PLATFORM="windows")).stdout
    assert "Running: yes (process %d" % pid in out
    out = iron(repo, "autosave", "--once", check=False)
    assert out.returncode == 1 and "already running" in out.stderr
    autosave.write_state(repo, heartbeat=time.time() - 3600)
    assert "Running: no" in iron(repo, "autosave", "--status",
                                 env=dict(os.environ, IRON_AUTOSAVE_PLATFORM="windows")).stdout
