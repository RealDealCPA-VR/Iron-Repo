"""Coordination between Iron processes: the repository lock, the agent session marker, and what
background saving and syncing do while an agent works or files conflict."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import BIN, commit, git, iron, loc, make_repo, write


@pytest.fixture
def home(env, monkeypatch):
    for var in ("APPDATA", "HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(env / "user"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(env / "user" / ".config"))
    for var in ("IRON_AUTOSAVE_DRY_RUN", "IRON_AUTOSAVE_PLATFORM", "IRON_AUTOSYNC_SKIP", "IRON_REPO_LOCK"):
        monkeypatch.delenv(var, raising=False)
    return env


def head(repo):
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def published(repo, remote_dir):
    out = git(repo, "ls-remote", "iron::" + loc(remote_dir), "refs/heads/main").stdout.split()
    return out[0] if out else None


def synced_repo(env, name="p"):
    repo = make_repo(env / name)
    iron(repo, "remote", "add", "origin", loc(env / "remote.iron"))
    iron(repo, "sync")
    return repo


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def write_marker(repo, pid, started=None, agent="Claude"):
    path = repo / ".git" / "iron" / "agent-session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "agent": agent, "session": "s1",
                                "started": time.time() if started is None else started}), encoding="utf-8")
    return path


def iron_popen(cwd, *args, env=None):
    return subprocess.Popen([sys.executable, str(BIN / "iron_launch.py"), "cli"] + list(args), cwd=str(cwd),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", env=env)


# -- secrets stay out of every commit path --------------------------------------

def test_dashboard_and_api_commits_leave_secrets_out(home):
    from iron.api import Repository
    from iron.errors import IronError
    from iron.ui import Dashboard

    repo_path = synced_repo(home)
    write(repo_path / "credentials.json", "{\"token\": \"x\"}\n")
    write(repo_path / "notes" / "deploy.txt", "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")
    write(repo_path / "app.py", "print(1)\n")
    repo = Repository(repo_path)
    assert sorted(repo.status()["kept_out"]) == ["credentials.json", "notes/deploy.txt"]

    result = Dashboard(repo_path).commit("Save from the dashboard")  # what "Save version" calls
    files = set(git(repo_path, "ls-files").stdout.split())
    assert "app.py" in files and not files & {"credentials.json", "notes/deploy.txt"}
    assert result["sha"] == head(repo_path)

    # the overview does not count them as pending changes, but says why they stay
    out = iron(repo_path).stdout
    assert "0 uncommitted change(s)" in out
    assert "2 file(s) kept out because they look like secrets (add them to .gitignore)" in out

    write(repo_path / "app.py", "print(2)\n")
    details = repo.commit_details("Second")
    assert details["sha"] == head(repo_path)
    assert sorted(details["left_out"]) == ["credentials.json", "notes/deploy.txt"]
    write(repo_path / "app.py", "print(3)\n")
    assert repo.commit("Third") == head(repo_path)  # still returns the sha

    # a secret staged by hand stops the commit: unstaging it is the fix
    git(repo_path, "add", "-f", "credentials.json")
    write(repo_path / "app.py", "print(4)\n")
    before = head(repo_path)
    with pytest.raises(IronError, match="credentials.json"):
        repo.commit("Fourth")
    assert head(repo_path) == before
    git(repo_path, "restore", "--staged", "credentials.json")
    assert repo.commit("Fourth") != before
    assert "credentials.json" not in git(repo_path, "ls-files").stdout


def test_agent_commits_leave_secrets_out(home):
    repo = make_repo(home / "p")
    task = ("import pathlib; pathlib.Path('feature.py').write_text('x = 1\\n'); "
            "pathlib.Path('credentials.json').write_text('{}')")
    out = iron(repo, "agent", "--name", "Claude", "--", sys.executable, "-c", task)
    files = git(repo, "ls-files").stdout.split()
    assert "feature.py" in files and "credentials.json" not in files
    assert "credentials.json" in out.stdout and "look like secrets" in out.stdout


# -- the agent session marker ----------------------------------------------------

def test_autosave_waits_while_an_agent_works_and_resumes_when_the_marker_is_stale(home):
    repo = synced_repo(home)
    write(repo / "half-done.py", "x =\n")
    marker = write_marker(repo, os.getpid())  # this test process stands in for `iron agent`
    before = head(repo)
    out = iron(repo, "autosave", "--once")
    assert "an agent is working; waiting" in out.stdout and head(repo) == before
    assert "Claude" in iron(repo, "autosave", "--status").stdout

    from iron.api import Repository
    result = Repository(repo).sync()  # the dashboard's "Sync now"
    assert not result["ok"] and result["waiting"] and "waiting" in result["output"]

    write_marker(repo, dead_pid())  # the agent crashed: its marker no longer counts
    out = iron(repo, "autosave", "--once")
    assert head(repo) != before and published(repo, home / "remote.iron") == head(repo)

    write(repo / "more.py", "y = 1\n")
    write_marker(repo, os.getpid(), started=time.time() - 3 * 86400)  # far too old to be real
    before = head(repo)
    iron(repo, "autosave", "--once")
    assert head(repo) != before
    marker.unlink()


def test_agent_rollback_is_not_undone_by_autosave(home):
    repo = synced_repo(home)
    start = head(repo)
    inner = home / "inner-autosave.txt"
    task = ("import pathlib, subprocess, sys; pathlib.Path('half_done.py').write_text('x =\\n'); "
            "p = subprocess.run([sys.executable, %r, 'cli', 'autosave', '--once'], capture_output=True, text=True); "
            "pathlib.Path(%r).write_text(p.stdout + p.stderr); sys.exit(1)"
            % (str(BIN / "iron_launch.py"), str(inner)))
    out = iron(repo, "agent", "--name", "Codex", "--", sys.executable, "-c", task, check=False)
    assert out.returncode != 0 and "rolled back" in out.stdout
    assert "an agent is working; waiting" in inner.read_text(encoding="utf-8")
    assert published(repo, home / "remote.iron") == start  # the half-done work was never published
    assert not (repo / ".git" / "iron" / "agent-session.json").exists()

    iron(repo, "autosave", "--once")  # the next tick has nothing to pull back in
    assert not (repo / "half_done.py").exists() and head(repo) == start


def test_autosync_hook_waits_while_an_agent_works(home):
    repo = synced_repo(home)
    iron(repo, "autosync", "on")
    write_marker(repo, os.getpid())
    sha = commit(repo, "a.txt", "a\n")
    log = Path(os.environ["IRON_HOME"]) / "logs"
    deadline = time.time() + 60
    while time.time() < deadline:
        texts = [p.read_text(encoding="utf-8") for p in log.glob("autosync-*.log")] if log.exists() else []
        if any("an agent is working" in t for t in texts):
            break
        time.sleep(0.5)
    else:
        pytest.fail("the hook did not report the agent")
    assert published(repo, home / "remote.iron") != sha


# -- the repository lock -----------------------------------------------------------

def test_repository_lock_serializes_sync_and_autosave(home):
    from iron import locks

    repo = synced_repo(home)
    write(repo / "a.txt", "a\n")
    with locks.repo_lock(repo, "a test"):
        outsider = {k: v for k, v in os.environ.items() if k != "IRON_REPO_LOCK"}  # not our child
        # autosave does not wait: it skips this tick and names the holder
        out = iron(repo, "autosave", "--once", env=outsider)
        assert "another Iron operation is running (pid %d)" % os.getpid() in out.stdout
        assert git(repo, "status", "--porcelain").stdout.strip() == "?? a.txt"
        # a sync waits, with a clear message, until the lock is free
        sync = iron_popen(repo, "sync", env=outsider)
        time.sleep(3)
        assert sync.poll() is None
        # and gives up after the timeout
        out = iron(repo, "sync", check=False, env=dict(outsider, IRON_REPO_LOCK_TIMEOUT="1"))
        assert out.returncode == 1 and "another Iron operation is running (pid %d" % os.getpid() in out.stderr
    text, _ = sync.communicate(timeout=120)
    assert sync.returncode == 0 and "Waiting for another Iron operation" in text
    assert not (repo / ".git" / "iron" / "repo.lock").exists()

    # a lock left by a process that died is taken over at once
    (repo / ".git" / "iron" / "repo.lock").write_text(json.dumps({"pid": dead_pid(), "time": time.time()}))
    out = iron(repo, "autosave", "--once")
    assert "Saved 1 changed file(s)" in out.stdout


def test_lock_is_reentrant_and_lent_to_child_processes(home):
    from iron import locks

    repo = make_repo(home / "p")
    with locks.repo_lock(repo, "outer"):
        with locks.repo_lock(repo, "inner", timeout=0):
            assert locks.holds_lock(repo)
        assert (repo / ".git" / "iron" / "repo.lock").exists()
        # a child started now (like autosave's `iron sync`) works under our lock
        write(repo / "x.txt", "x\n")
        out = iron(repo, "checkpoint", "--json", env=dict(os.environ, IRON_REPO_LOCK_TIMEOUT="1"))
        assert json.loads(out.stdout)["id"]
        # one without our token waits for us
        env = {k: v for k, v in os.environ.items() if k != "IRON_REPO_LOCK"}
        out = iron(repo, "checkpoint", check=False, env=dict(env, IRON_REPO_LOCK_TIMEOUT="1"))
        assert out.returncode == 1 and "another Iron operation" in out.stderr
    assert not (repo / ".git" / "iron" / "repo.lock").exists() and "IRON_REPO_LOCK" not in os.environ


def test_concurrent_syncs_and_autosaves_do_not_trip_over_each_other(home):
    a = synced_repo(home, "a")
    iron(home, "clone", loc(home / "remote.iron"), str(home / "b"))
    b = home / "b"
    for n in range(3):
        commit(b, "b%d.txt" % n, "from b\n")
        iron(b, "sync")
        commit(a, "c%d.txt" % n, "committed in a\n")
        write(a / ("a%d.txt" % n), "unsaved in a\n")
        procs = [iron_popen(a, "sync"), iron_popen(a, "autosave", "--once"), iron_popen(a, "sync")]
        for p in procs:
            text, _ = p.communicate(timeout=300)
            assert p.returncode == 0, text
            for bad in ("cannot lock", "fatal", "Traceback", "Could not sync", "Autosave stopped"):
                assert bad not in text, text
    iron(a, "autosave", "--once")
    assert published(a, home / "remote.iron") == head(a)
    assert git(a, "status", "--porcelain").stdout.strip() == ""
    assert all((a / ("%s%d.txt" % (k, n))).exists() for k in "abc" for n in range(3))


# -- sync failures and conflicts -----------------------------------------------------

def test_sync_failures_are_classified(home):
    from iron import autosave

    repo = synced_repo(home)
    classify = autosave.classify_sync
    assert classify(repo, 0, "Published main") == autosave.SYNCED
    assert classify(repo, 1, " ! [rejected]  main -> main (fetch first)") == autosave.RACE
    assert classify(repo, 1, "error main non-fast-forward") == autosave.RACE
    assert classify(repo, 1, "another push reached the remote at the same moment") == autosave.RACE
    assert classify(repo, 1, "iron: you have uncommitted changes. Save them first") == autosave.RETRY
    assert classify(repo, 1, "Could not fast-forward: commit or checkpoint your local edits first.") == autosave.RETRY
    assert classify(repo, 1, "iron: another Iron operation is running (pid 7)") == autosave.RETRY
    assert classify(repo, 1, "fatal: iron: cannot reach D:/Backups") == autosave.OFFLINE

    # a merge that is still running (no conflicted files) is not a conflict
    git(repo, "switch", "-q", "-c", "side")
    commit(repo, "side.txt", "s\n")
    git(repo, "switch", "-q", "main")
    commit(repo, "main.txt", "m\n")
    git(repo, "merge", "--no-commit", "--no-ff", "side")
    assert (repo / ".git" / "MERGE_HEAD").exists() and not autosave.in_conflict(repo)
    assert classify(repo, 1, "something failed") == autosave.OFFLINE
    out = iron(repo, "autosave", "--once")
    assert out.returncode == 0 and "a merge is in progress" in out.stdout


def test_a_race_is_retried_and_reported_as_such(home, monkeypatch):
    from iron import autosave

    repo = synced_repo(home)
    calls = []

    def fake(root, timeout):
        calls.append(1)
        return (1, " ! [rejected] main -> main (fetch first)") if len(calls) < 2 else (0, "Published main to origin")
    monkeypatch.setattr(autosave, "_run_sync", fake)
    log = autosave.Log(home / "logs" / "x.log", console=False)
    try:
        assert autosave.sync_once(repo, log) == autosave.SYNCED and len(calls) == 2
        calls.clear()
        monkeypatch.setattr(autosave, "_run_sync", lambda root, timeout: (1, "error main fetch first"))
        assert autosave.sync_once(repo, log) == autosave.RETRY
    finally:
        log.close()
    text = (home / "logs" / "x.log").read_text(encoding="utf-8")
    assert "Someone else published at the same moment; retrying" in text and "offline" not in text


def test_conflicts_are_loud_everywhere(home):
    from iron.api import Repository

    a = synced_repo(home, "a")
    iron(home, "clone", loc(home / "remote.iron"), str(home / "b"))
    b = home / "b"
    commit(a, "README.md", "from a\n")
    iron(a, "sync")
    write(b / "README.md", "from b\n")
    assert iron(b, "autosave", "--once", check=False).returncode == 1
    for _ in range(2):  # every later tick says so again, loudly
        out = iron(b, "autosave", "--once", "--quiet", check=False)
        assert out.returncode == 1
        assert "Files conflict between your computers: README.md" in out.stdout
    banner = "Files conflict between your computers: README.md"
    assert banner in iron(b).stdout and "iron sync" in iron(b).stdout
    assert banner in iron(b, "autosave", "--status").stdout
    assert Repository(b).status()["conflicts"] == ["README.md"]


# -- smaller fixes ---------------------------------------------------------------------

def test_sync_merges_get_a_human_title(home):
    a = synced_repo(home, "a")
    iron(home, "clone", loc(home / "remote.iron"), str(home / "b"))
    b = home / "b"
    commit(a, "a.txt", "a\n")
    iron(a, "sync")
    commit(b, "b.txt", "b\n")
    iron(b, "sync")
    assert git(b, "log", "-1", "--format=%s").stdout.strip() == "Combine changes from origin"


def test_automatic_checkpoints_are_pruned_and_dates_agree(home):
    from iron import checkpoint

    repo = make_repo(home / "p")
    write(repo / "mine.txt", "0\n")
    manual = checkpoint.create("before big change", cwd=repo)
    assert manual.date == checkpoint.list_checkpoints(repo)[0].date  # one date format
    safety, _ = checkpoint.restore(manual.id, cwd=repo)
    agent = checkpoint.create("before Claude task", agent="Claude", cwd=repo)
    for i in range(25):
        write(repo / "n.txt", "%d\n" % i)
        checkpoint.create("before syncing with origin" if i % 2 else "before merge side", cwd=repo, auto=True)
    cps = checkpoint.list_checkpoints(repo)
    auto = [c for c in cps if c.message.startswith(checkpoint.AUTO_PREFIXES)]
    assert len(auto) == 20
    assert {manual.id, safety.id, agent.id} <= {c.id for c in cps}
    assert len({len(c.date) for c in cps}) == 1


def test_setup_without_a_destination_fails_before_git_init(home):
    project = home / "plain"
    write(project / "notes.md", "hi\n")
    out = iron(project, "setup", check=False)
    assert out.returncode == 1 and "--to" in out.stderr
    assert not (project / ".git").exists()


def test_a_dead_autosave_does_not_block_a_new_one(home):
    from iron import autosave

    repo = synced_repo(home)
    autosave.write_state(repo, pid=dead_pid(), heartbeat=time.time(), every=300)
    env = dict(os.environ, IRON_AUTOSAVE_PLATFORM="windows")
    assert "Running: no" in iron(repo, "autosave", "--status", env=env).stdout
    assert iron(repo, "autosave", "--once").returncode == 0


def test_runner_adds_only_the_folders_iron_needs_to_path(home):
    from iron import autosave

    repo = synced_repo(home)
    script = autosave.runner_script(repo, 300)
    assert os.environ["PATH"] not in script
    needed = autosave.runner_path_dirs()
    assert str(Path(sys.executable).resolve().parent) in needed
    assert len(needed) <= 5
    ns = {}
    code = script.split("from iron.autosave")[0].replace("sys.path.insert(", "print(")
    saved = os.environ["PATH"]
    try:
        os.environ["PATH"] = "C:/later/added" if os.name == "nt" else "/later/added"
        exec(code, ns)  # noqa: S102 - the runner's own PATH lines
        parts = os.environ["PATH"].split(os.pathsep)
        assert parts[:len(needed)] == needed and parts[-1].endswith("later/added")
    finally:
        os.environ["PATH"] = saved
