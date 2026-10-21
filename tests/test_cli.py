"""The iron command: setup, checkpoints, restore, agents, encryption and the sovereignty tools."""

import json
import os
import shutil
import sys

import pytest

from conftest import commit, git, iron, loc, make_repo, run, write
from iron.crypto import crypto_available

needs_crypto = pytest.mark.skipif(not crypto_available(), reason="cryptography not installed")


def test_setup_from_a_plain_folder(env):
    project = env / "My Project"
    write(project / "notes.md", "# notes\n")
    write(project / "src" / "app.py", "print('hi')\n")
    out = iron(project, "setup", "--to", loc(env / "Cloud"), "--yes")
    assert "Done" in out.stdout
    container = env / "Cloud" / "Iron-Repos" / "My Project.iron"
    assert (container / "iron.json").is_file() and (container / "iron-recover.py").is_file()
    assert git(project, "log", "--format=%s").stdout.strip() == "First version"
    assert "✓ Synced with origin" in iron(project).stdout

    clone = env / "elsewhere"
    iron(env, "clone", loc(container), str(clone))
    assert (clone / "src" / "app.py").read_text() == "print('hi')\n"
    # running setup again is harmless and just syncs
    assert "Already set up" in iron(project, "setup", "--yes").stdout


def test_setup_without_destination_explains_itself(env):
    project = make_repo(env / "p")
    out = iron(project, "setup", check=False)
    assert out.returncode != 0 and "--to" in out.stderr


def test_sync_combines_work_from_two_computers(env):
    a = make_repo(env / "a")
    iron(a, "remote", "add", "origin", loc(env / "r.iron"))
    iron(a, "sync")
    iron(env, "clone", loc(env / "r.iron"), str(env / "b"))
    b = env / "b"
    commit(a, "a.txt", "a\n")
    commit(b, "b.txt", "b\n")
    iron(a, "sync")
    out = iron(b, "sync")
    assert "Combining" in out.stdout and "Published" in out.stdout
    iron(a, "sync")
    assert (a / "b.txt").exists() and (b / "a.txt").exists()
    assert "Already in sync" in iron(a, "sync").stdout


def test_checkpoint_captures_untracked_files_without_touching_anything(env):
    repo = make_repo(env / "p")
    write(repo / "README.md", "edited\n")
    write(repo / "new.txt", "brand new\n")
    write(repo / ".gitignore", "secret.key\n")
    write(repo / "secret.key", "ignored\n")
    git(repo, "add", "README.md")
    status_before = git(repo, "status", "--porcelain").stdout
    cp = json.loads(iron(repo, "checkpoint", "-m", "before agent", "--json").stdout)
    assert git(repo, "status", "--porcelain").stdout == status_before
    files = git(repo, "ls-tree", "-r", "--name-only", cp["sha"]).stdout.split()
    assert {"README.md", "new.txt", ".gitignore"} <= set(files) and "secret.key" not in files
    listed = json.loads(iron(repo, "checkpoints", "--json").stdout)
    assert listed[0]["id"] == cp["id"]


def test_restore_undoes_destruction_and_keeps_a_safety_copy(env):
    repo = make_repo(env / "p", {"keep.txt": "keep\n", "src/a.py": "a\n"})
    write(repo / "draft.txt", "untracked but precious\n")
    cp = json.loads(iron(repo, "checkpoint", "--json").stdout)
    # an agent goes wild
    shutil.rmtree(repo / "src")
    (repo / "draft.txt").unlink()
    write(repo / "keep.txt", "corrupted\n")
    write(repo / "junk" / "garbage.bin", "junk\n")
    out = json.loads(iron(repo, "restore", cp["id"], "--json").stdout)
    assert (repo / "src" / "a.py").read_text() == "a\n"
    assert (repo / "draft.txt").read_text() == "untracked but precious\n"
    assert (repo / "keep.txt").read_text() == "keep\n"
    assert not (repo / "junk").exists()
    assert git(repo, "status", "--porcelain").stdout.strip() == "?? draft.txt"
    # the wrecked state is itself recoverable
    iron(repo, "restore", out["safety_checkpoint"])
    assert (repo / "junk" / "garbage.bin").exists() and not (repo / "src").exists()
    iron(repo, "restore", "last")
    # `iron restore <file>` style arguments still reach git's own restore
    write(repo / "keep.txt", "changed\n")
    iron(repo, "restore", "--source", "HEAD", "keep.txt")
    assert (repo / "keep.txt").read_text() == "keep\n"


def test_restore_in_a_repository_without_commits(env):
    git(env, "init", "-q", "-b", "main", "fresh")
    repo = env / "fresh"
    write(repo / "a.txt", "1\n")
    cp = json.loads(iron(repo, "checkpoint", "--json").stdout)
    write(repo / "a.txt", "2\n")
    iron(repo, "restore", cp["id"])
    assert (repo / "a.txt").read_text() == "1\n"


AGENT_OK = "import pathlib; pathlib.Path('feature.py').write_text('x = 1\\n')"
AGENT_BAD = "import pathlib, os; os.remove('README.md'); pathlib.Path('broken.py').write_text('oops')"


def test_agent_success_is_committed_with_attribution(env):
    repo = make_repo(env / "p")
    out = iron(repo, "agent", "--name", "Claude", "--session", "s42", "--test", "%s -c \"import feature\"" % sys.executable,
               "--", sys.executable, "-c", AGENT_OK)
    assert "committed" in out.stdout
    body = git(repo, "log", "-1", "--format=%B").stdout
    assert "Iron-Agent: Claude" in body and "Iron-Session: s42" in body
    history = json.loads(iron(repo, "history", "--json").stdout)
    assert history[0]["agent"] == "Claude"


def test_agent_failure_rolls_back(env):
    repo = make_repo(env / "p")
    out = iron(repo, "agent", "--name", "Codex", "--test", "%s -c \"raise SystemExit(1)\"" % sys.executable,
               "--", sys.executable, "-c", AGENT_BAD, check=False)
    assert out.returncode != 0 and "rolled back" in out.stdout
    assert (repo / "README.md").exists() and not (repo / "broken.py").exists()
    assert git(repo, "status", "--porcelain").stdout == ""
    assert len(json.loads(iron(repo, "checkpoints", "--json").stdout)) == 2  # before task + the attempt


def test_merge_takes_a_checkpoint_first(env):
    repo = make_repo(env / "p")
    git(repo, "switch", "-q", "-c", "side")
    commit(repo, "side.txt", "s\n")
    git(repo, "switch", "-q", "main")
    out = iron(repo, "merge", "--no-edit", "side")
    assert "checkpoint" in out.stdout


def test_passthrough_to_git(env):
    repo = make_repo(env / "p")
    assert "add README.md" in iron(repo, "log", "--oneline").stdout
    assert iron(repo, "status", "--short").returncode == 0


def test_verify_detects_corruption_and_missing_bundles(env):
    repo = make_repo(env / "p")
    iron(repo, "remote", "add", "origin", loc(env / "r.iron"))
    iron(repo, "sync")
    commit(repo, "more.txt", "more\n")
    iron(repo, "sync")
    deep = iron(repo, "verify", "--deep")
    assert "full restore test passed: 2 commit(s)" in deep.stdout
    bundle = sorted((env / "r.iron" / "bundles").iterdir())[0]
    bundle.write_bytes(bundle.read_bytes()[:-5] + b"XXXXX")
    out = iron(repo, "verify", "--deep", check=False)
    assert out.returncode == 1 and "restore test failed" in out.stdout
    bundle.unlink()
    out = iron(repo, "verify", check=False)
    assert out.returncode == 1 and "is missing" in out.stdout


def test_recovery_without_iron(env):
    """The remote carries its own recovery tool: Python + git are enough."""
    repo = make_repo(env / "p", {"a.txt": "1\n"})
    iron(repo, "remote", "add", "origin", loc(env / "r.iron"))
    iron(repo, "sync")
    git(repo, "tag", "v1")
    git(repo, "push", "origin", "v1")
    standalone = env / "only-the-folder"
    shutil.copytree(env / "r.iron", standalone)
    clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    run([sys.executable, "-I", str(standalone / "iron-recover.py"), str(standalone), str(env / "rebuilt")],
        env, env=clean_env)
    assert (env / "rebuilt" / "a.txt").read_text() == "1\n"
    assert git(env / "rebuilt", "tag").stdout.split() == ["v1"]
    assert git(env / "rebuilt", "rev-parse", "HEAD").stdout == git(repo, "rev-parse", "HEAD").stdout


def test_migrate_to_new_storage_and_keep_working(env):
    repo = make_repo(env / "p")
    iron(repo, "remote", "add", "origin", loc(env / "drive" / "p.iron"))
    iron(repo, "sync")
    out = iron(repo, "migrate", loc(env / "nas"))
    assert "Copied and verified" in out.stdout
    assert git(repo, "remote", "get-url", "origin").stdout.strip() == "iron::" + loc(env / "nas" / "Iron-Repos" / "p.iron")
    commit(repo, "after.txt", "after migration\n")
    iron(repo, "sync")
    iron(env, "clone", loc(env / "nas" / "Iron-Repos" / "p.iron"), str(env / "c"))
    assert (env / "c" / "after.txt").exists()
    assert (env / "drive" / "p.iron" / "iron.json").exists()  # source untouched
    # migrating again into the same place is a no-op resume
    git(repo, "remote", "set-url", "origin", "iron::" + loc(env / "drive" / "p.iron"))
    out = iron(repo, "migrate", loc(env / "nas"), "--keep-url", check=False)
    assert out.returncode != 0  # source is behind the destination now: histories differ


def test_compact_then_fresh_clone_downloads_one_bundle(env):
    repo = make_repo(env / "p")
    iron(repo, "remote", "add", "origin", loc(env / "r.iron"))
    for i in range(4):
        commit(repo, "f%d.txt" % i, "%d\n" % i)
        iron(repo, "sync")
    iron(repo, "compact")
    out = git(env, "clone", "iron::" + loc(env / "r.iron"), "fresh")
    assert "downloading 1 bundle(s)" in out.stderr
    assert git(env / "fresh", "rev-parse", "HEAD").stdout == git(repo, "rev-parse", "HEAD").stdout
    commit(repo, "after.txt", "x\n")
    iron(repo, "sync")
    git(env / "fresh", "pull", "--no-rebase")
    assert (env / "fresh" / "after.txt").exists()
    assert "Already compact" not in iron(repo, "compact").stdout
    assert "Already compact" in iron(repo, "compact").stdout


def test_export_is_restorable_by_plain_git(env):
    repo = make_repo(env / "p")
    iron(repo, "checkpoint")
    iron(repo, "export", str(env / "backup.bundle"))
    git(env, "clone", "-q", str(env / "backup.bundle"), "restored")
    assert (env / "restored" / "README.md").exists()
    refs = git(env, "bundle", "list-heads", str(env / "backup.bundle")).stdout
    assert "refs/iron/checkpoints/" in refs


@needs_crypto
def test_encrypted_remote(env):
    repo = make_repo(env / "p", {"secret.txt": "the plans\n"})
    out = iron(repo, "remote", "add", "origin", loc(env / "r.iron"), "--encrypt")
    key = [l.strip() for l in out.stdout.splitlines() if l.strip().startswith("iron1-")][0]
    iron(repo, "sync")
    for f in (env / "r.iron" / "bundles").iterdir():
        assert b"the plans" not in f.read_bytes() and b"PACK" not in f.read_bytes()
    for f in (env / "r.iron" / "log").glob("*.json"):
        assert b"refs/heads" not in f.read_bytes()
    assert iron(repo, "key", "show").stdout.strip() == key
    # another computer has no key: clear failure, then success with the key
    other = dict(os.environ, IRON_HOME=str(env / "other-home"))
    out = iron(env, "clone", loc(env / "r.iron"), str(env / "c1"), check=False, env=other)
    assert out.returncode != 0 and "encrypted" in out.stderr
    iron(env, "clone", loc(env / "r.iron"), str(env / "c2"), "--key", key, env=other)
    assert (env / "c2" / "secret.txt").read_text() == "the plans\n"
    wrong = "iron1-" + "A" * 43
    out = iron(env, "key", "import", wrong, loc(env / "r.iron"), check=False, env=dict(other, IRON_HOME=str(env / "h3")))
    assert out.returncode != 0 and "does not belong" in out.stderr
    # the standalone recovery tool works with the key too
    run([sys.executable, str(env / "r.iron" / "iron-recover.py"), str(env / "r.iron"), str(env / "rebuilt"),
         "--key", key], env)
    assert (env / "rebuilt" / "secret.txt").read_text() == "the plans\n"
    assert iron(repo, "verify", "--deep").returncode == 0


def test_doctor_and_where(env):
    repo = make_repo(env / "p")
    iron(repo, "remote", "add", "origin", loc(env / "r.iron"))
    out = iron(repo, "doctor", "--deep")
    assert out.returncode == 0, out.stdout
    assert "binary round-trip" in out.stdout and "Everything needed is in place" in out.stdout
    assert "reachable" in iron(repo, "where").stdout or "read/write ok" in iron(repo, "where").stdout


def test_unlock_clears_stale_locks(env):
    repo = make_repo(env / "p")
    iron(repo, "remote", "add", "origin", loc(env / "r.iron"))
    write(env / "r.iron" / "locks" / "0000000001000-abcdef123456.lock", "{}")
    assert "removed 1 lock" in iron(repo, "unlock", "--yes").stdout


def test_python_agent_api(env):
    from iron.api import Repository

    repo_path = make_repo(env / "p")
    iron(repo_path, "remote", "add", "origin", loc(env / "r.iron"))
    repo = Repository(repo_path, agent="GLM", session="t1")
    cp = repo.checkpoint("before")
    write(repo_path / "new.py", "print(1)\n")
    assert "new.py" in repo.diff()
    assert repo.status()["changes"] == [{"status": "??", "path": "new.py"}]
    sha = repo.commit("Add provider tests")
    assert sha and repo.history(1)[0]["agent"] == "GLM"
    assert repo.push()["ok"]
    assert repo.verify(deep=True)["ok"]
    repo.restore(cp["id"])
    assert not (repo_path / "new.py").exists()


def test_identical_checkpoints_are_not_duplicated(env):
    repo = make_repo(env / "p")
    first = json.loads(iron(repo, "checkpoint", "-m", "one", "--json").stdout)
    again = json.loads(iron(repo, "checkpoint", "-m", "two", "--json").stdout)
    assert again["id"] == first["id"]
    write(repo / "x.txt", "change\n")
    third = json.loads(iron(repo, "checkpoint", "--json").stdout)
    assert third["id"] != first["id"]


def test_agent_rollback_also_undoes_agent_commits(env):
    repo = make_repo(env / "p")
    start = git(repo, "rev-parse", "HEAD").stdout
    bad = "import subprocess, pathlib; pathlib.Path('README.md').write_text('evil'); " \
          "subprocess.run(['git', 'commit', '-qam', 'evil'], check=True)"
    out = iron(repo, "agent", "--test", "%s -c \"raise SystemExit(1)\"" % sys.executable,
               "--", sys.executable, "-c", bad, check=False)
    assert "rolled back" in out.stdout
    assert git(repo, "rev-parse", "HEAD").stdout == start
    assert (repo / "README.md").read_text() == "hello\n" and git(repo, "status", "--porcelain").stdout == ""


def test_recovery_tool_refuses_to_overwrite_a_folder(env):
    repo = make_repo(env / "p")
    iron(repo, "remote", "add", "origin", loc(env / "r.iron"))
    iron(repo, "sync")
    write(repo / "README.md", "uncommitted work\n")
    out = run([sys.executable, str(env / "r.iron" / "iron-recover.py"), str(env / "r.iron"), str(repo)], env, check=False)
    assert out.returncode != 0 and "not empty" in out.stderr
    assert (repo / "README.md").read_text() == "uncommitted work\n"


def test_snapshot_bundle_restores_with_plain_git(env):
    repo = make_repo(env / "p")
    iron(repo, "remote", "add", "origin", loc(env / "r.iron"))
    iron(repo, "sync")
    iron(repo, "compact")
    bundles = sorted((env / "r.iron" / "bundles").iterdir(), key=lambda p: p.stat().st_mtime)
    git(env, "clone", "-q", str(bundles[-1]), "plain")
    assert (env / "plain" / "README.md").read_text() == "hello\n"


def test_migrated_remote_refuses_stale_pushes(env):
    a = make_repo(env / "a")
    iron(a, "remote", "add", "origin", loc(env / "old.iron"))
    iron(a, "sync")
    iron(env, "clone", loc(env / "old.iron"), str(env / "b"))
    iron(a, "migrate", loc(env / "new"))
    commit(env / "b", "late.txt", "pushed to the old copy\n")
    out = git(env / "b", "push", check=False)
    assert out.returncode != 0 and "moved to" in out.stderr


def test_locations_inside_the_project_or_unknown_rclone_names_are_refused(env):
    repo = make_repo(env / "p")
    out = iron(repo, "remote", "add", "origin", loc(repo / "backup.iron"), check=False)
    assert out.returncode != 0 and "inside this project" in out.stderr
    out = iron(repo, "remote", "add", "x", "nosuchremote:Iron/app.iron", check=False)
    assert out.returncode != 0 and "rclone" in out.stderr and "Traceback" not in out.stderr
    out = iron(env, "clone", loc(env / "nowhere"), "--key", "garbage", check=False)
    assert out.returncode != 0 and "Traceback" not in out.stderr


def test_setup_keeps_secrets_out(env):
    project = env / "proj"
    write(project / "app.py", "print(1)\n")
    write(project / "server.pem", "-----BEGIN KEY-----\n")
    write(project / "credentials.json", "{}\n")
    write(project / ".env", "TOKEN=abc\n")
    out = iron(project, "setup", "--to", loc(env / "cloud"), "--yes", check=False)
    # .env and *.pem are kept out by the starter .gitignore; credentials.json needs a decision
    assert out.returncode != 0 and "credentials.json" in out.stdout and "server.pem" not in out.stdout
    assert ".env" in (project / ".gitignore").read_text()
    (project / "credentials.json").unlink()
    iron(project, "setup", "--to", loc(env / "cloud"), "--yes")
    files = git(project, "ls-files").stdout.split()
    assert "app.py" in files and ".env" not in files and "server.pem" not in files and "server.pem" not in files


def test_migrate_between_claims_and_confirm_storage(env):
    """Folder (atomic claims) -> rclone (confirm protocol) -> folder, pushing at every stop."""
    repo = make_repo(env / "p")
    iron(repo, "remote", "add", "origin", loc(env / "disk" / "p.iron"))
    assert json.loads((env / "disk" / "p.iron" / "iron.json").read_text())["claims"] is True
    iron(repo, "sync")
    iron(repo, "migrate", "rclone:fake:cloud")
    marker = json.loads((env / "fake-cloud" / "cloud" / "Iron-Repos" / "p.iron" / "iron.json").read_text())
    assert marker["claims"] is False
    commit(repo, "on-rclone.txt", "1\n")
    iron(repo, "sync")
    iron(repo, "migrate", loc(env / "disk2"))
    commit(repo, "back-on-disk.txt", "2\n")
    iron(repo, "sync")
    assert iron(repo, "verify", "--deep").returncode == 0
    iron(env, "clone", loc(env / "disk2" / "Iron-Repos" / "p.iron"), str(env / "c"))
    assert (env / "c" / "on-rclone.txt").exists() and (env / "c" / "back-on-disk.txt").exists()


def test_no_claims_flag_and_recovery_tool_version_guard(env):
    repo = make_repo(env / "p")
    iron(repo, "remote", "add", "origin", loc(env / "r.iron"), "--no-claims")
    marker = json.loads((env / "r.iron" / "iron.json").read_text())
    assert marker["claims"] is False and marker["version"] == 2
    iron(repo, "sync")
    future = env / "future.iron"
    shutil.copytree(env / "r.iron", future)
    (future / "iron.json").write_text(json.dumps(dict(marker, version=99)))
    out = run([sys.executable, str(future / "iron-recover.py"), str(future), str(env / "x")], env, check=False)
    assert out.returncode != 0 and "newer" in out.stderr
    # migrate always ships the current recovery tool
    (env / "r.iron" / "iron-recover.py").write_text("# stale tool\n")
    iron(repo, "migrate", loc(env / "dest"))
    shipped = (env / "dest" / "Iron-Repos" / "r.iron" / "iron-recover.py").read_text()
    assert "CLAIM_RE" in shipped
