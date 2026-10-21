"""End to end through real git: clone, fetch, pull and push against iron:: remotes."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path


from conftest import commit, git, iron, loc, make_repo, run


def setup_pair(env, location=None):
    location = location or loc(env / "remote.iron")
    a = make_repo(env / "a")
    git(a, "remote", "add", "origin", "iron::" + location)
    git(a, "push", "-u", "origin", "main", env=dict(os.environ, IRON_CREATE="1"))
    git(env, "clone", "iron::" + location, "b")
    return a, env / "b", location


def test_push_and_clone_roundtrip(env):
    a, b, _ = setup_pair(env)
    assert (b / "README.md").read_text() == "hello\n"
    assert git(b, "rev-parse", "HEAD").stdout == git(a, "rev-parse", "HEAD").stdout
    assert git(b, "symbolic-ref", "HEAD").stdout.strip() == "refs/heads/main"
    out = git(a, "ls-remote", "origin").stdout
    assert "refs/heads/main" in out and "HEAD" in out


def test_two_computers_collaborate_and_conflicting_push_is_rejected(env):
    a, b, _ = setup_pair(env)
    commit(a, "a.txt", "from a\n")
    git(a, "push")
    commit(b, "b.txt", "from b\n")
    rejected = git(b, "push", check=False)
    assert rejected.returncode != 0
    assert "fetch first" in rejected.stderr or "rejected" in rejected.stderr
    git(b, "pull", "--no-rebase", "--no-edit")
    git(b, "push")
    git(a, "pull", "--no-rebase")
    assert (a / "b.txt").exists() and (b / "a.txt").exists()
    assert git(a, "rev-parse", "HEAD").stdout == git(b, "rev-parse", "HEAD").stdout


def test_force_push_and_deletion_are_refused_by_default(env, monkeypatch):
    a, b, location = setup_pair(env)
    first = git(a, "rev-parse", "HEAD").stdout.strip()
    commit(a, "x.txt", "x\n")
    git(a, "push")
    git(a, "reset", "--hard", "HEAD~1")
    commit(a, "y.txt", "rewritten history\n")
    out = git(a, "push", "--force", check=False)
    assert out.returncode != 0 and "force-push is turned off" in out.stderr
    monkeypatch.setenv("IRON_ALLOW_FORCE", "1")
    git(a, "push", "--force")
    monkeypatch.delenv("IRON_ALLOW_FORCE")

    git(a, "push", "origin", "main:refs/heads/feature")
    out = git(a, "push", "origin", "--delete", "feature", check=False)
    assert out.returncode != 0 and "deleting remote branches is turned off" in out.stderr
    assert iron(a, "push", "origin", "--delete", "feature", "--yes").returncode == 0
    assert "feature" not in git(a, "ls-remote", "origin").stdout
    # even after a force push the overwritten commit is still in the remote's history
    restored = env / "restored"
    run([sys.executable, str(Path(location) / "iron-recover.py"), location, str(restored)], env)
    assert git(restored, "cat-file", "-t", first).stdout.strip() == "commit"


def test_tags_and_branches(env):
    a, b, _ = setup_pair(env)
    git(a, "tag", "-a", "v1", "-m", "release 1")
    git(a, "switch", "-q", "-c", "topic")
    commit(a, "topic.txt", "t\n")
    git(a, "push", "origin", "topic", "--tags")
    git(b, "fetch", "--tags")
    assert git(b, "tag").stdout.split() == ["v1"]
    assert "origin/topic" in git(b, "branch", "-r").stdout
    git(a, "tag", "-f", "-a", "v1", "-m", "moved", "HEAD")
    assert git(a, "push", "origin", "v1", check=False).returncode != 0


def test_new_branch_pointing_at_existing_commit_uploads_nothing(env):
    a, _, location = setup_pair(env)
    before = sorted(os.listdir(Path(location) / "bundles"))
    git(a, "push", "origin", "main:refs/heads/same")
    assert sorted(os.listdir(Path(location) / "bundles")) == before
    assert "refs/heads/same" in git(a, "ls-remote", "origin").stdout


def test_sha256_repository(env):
    git(env, "init", "-q", "-b", "main", "--object-format=sha256", "s")
    s = env / "s"
    commit(s, "f.txt", "sha256!\n")
    iron(s, "remote", "add", "origin", loc(env / "r256.iron"))
    git(s, "push", "-u", "origin", "main")
    git(env, "clone", "iron::" + loc(env / "r256.iron"), "s2")
    assert git(env / "s2", "rev-parse", "--show-object-format").stdout.strip() == "sha256"
    assert len(git(env / "s2", "rev-parse", "HEAD").stdout.strip()) == 64
    sha1 = make_repo(env / "sha1")
    git(sha1, "remote", "add", "origin", "iron::" + loc(env / "r256.iron"))
    out = git(sha1, "push", "origin", "main", check=False)
    assert out.returncode != 0 and "sha256" in out.stderr


def test_clone_of_missing_location_fails_clearly(env):
    out = git(env, "clone", "iron::" + loc(env / "nothing-here"), "x", check=False)
    assert out.returncode != 0 and "no Iron repository" in out.stderr


def test_unreliable_storage(env, monkeypatch):
    """Timeouts, partial writes, duplicate requests and lost acknowledgements."""
    monkeypatch.setenv("IRON_RETRY_ATTEMPTS", "12")
    location = "sim:%s?fail=0.15&partial=0.15&duplicate=0.3&seed=3" % loc(env / "flaky.iron")
    a, b, _ = setup_pair(env, location)
    for i in range(4):
        commit(a, "f%d.txt" % i, "content %d\n" % i)
        git(a, "push")
    git(b, "pull", "--no-rebase")
    assert sorted(p.name for p in b.glob("f*.txt")) == ["f0.txt", "f1.txt", "f2.txt", "f3.txt"]
    assert iron(a, "verify", "--deep").returncode == 0


def test_rclone_backend(env):
    location = "rclone:fake:Iron-Repos/app.iron"
    a, b, _ = setup_pair(env, location)
    commit(a, "cloud.txt", "via rclone\n")
    git(a, "push")
    git(b, "pull", "--no-rebase")
    assert (b / "cloud.txt").read_text() == "via rclone\n"
    assert (env / "fake-cloud" / "Iron-Repos" / "app.iron" / "iron.json").exists()


def test_fetch_recovers_after_local_objects_are_pruned(env):
    a, b, _ = setup_pair(env)
    for i in range(3):
        commit(a, "n%d.txt" % i, "%d\n" % i)
        git(a, "push")
    git(b, "fetch")
    # simulate a repository that lost objects: a fresh repo sharing b's Iron cache
    c = env / "c"
    git(env, "init", "-q", "-b", "main", str(c))
    git(c, "remote", "add", "origin", git(b, "remote", "get-url", "origin").stdout.strip())
    import shutil
    shutil.copytree(b / ".git" / "iron", c / ".git" / "iron")
    git(c, "fetch", "origin")
    assert git(c, "rev-parse", "origin/main").stdout == git(a, "rev-parse", "HEAD").stdout


WORKER = textwrap.dedent('''
    import subprocess, sys, uuid
    repo, rounds = sys.argv[1], int(sys.argv[2])
    def git(*a, check=True):
        p = subprocess.run(["git"] + list(a), cwd=repo, capture_output=True, text=True)
        if check and p.returncode:
            raise SystemExit("git %s: %s" % (a, p.stderr))
        return p
    for i in range(rounds):
        name = uuid.uuid4().hex[:8] + ".txt"
        open(repo + "/" + name, "w").write(name)
        git("add", name); git("commit", "-q", "-m", name)
        for attempt in range(40):
            if git("push", "-q", "origin", "main", check=False).returncode == 0:
                print(git("rev-parse", "HEAD").stdout.strip(), flush=True)
                break
            git("pull", "-q", "--no-rebase", "--no-edit", "origin", "main", check=False)
        else:
            raise SystemExit("gave up pushing")
''')


def test_parallel_pushers_never_lose_an_acknowledged_push(env):
    """Three computers push to one branch at the same time, over and over."""
    a, _, location = setup_pair(env)
    clones = []
    for i in range(3):
        git(env, "clone", "-q", "iron::" + location, "w%d" % i)
        clones.append(env / ("w%d" % i))
    script = env / "worker.py"
    script.write_text(WORKER, encoding="utf-8")
    procs = [subprocess.Popen([sys.executable, str(script), str(c), "4"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for c in clones]
    acked = []
    for p in procs:
        out, err = p.communicate(timeout=600)
        assert p.returncode == 0, err
        acked += out.split()
    assert len(acked) == 12
    git(a, "pull", "-q", "--no-rebase")
    for sha in acked:
        assert git(a, "merge-base", "--is-ancestor", sha, "HEAD", check=False).returncode == 0, sha
    assert iron(a, "verify", "--deep").returncode == 0


def test_ls_remote_outside_a_repository(env):
    _, _, location = setup_pair(env)
    outside = env / "outside"
    outside.mkdir()
    out = git(outside, "ls-remote", "iron::" + location)
    assert "refs/heads/main" in out.stdout


def test_push_never_creates_a_repository_implicitly(env):
    """An unmounted disk, a typo or a migrated remote must not silently start a new history."""
    a = make_repo(env / "a")
    git(a, "remote", "add", "origin", "iron::" + loc(env / "typo.iron"))
    out = git(a, "push", "origin", "main", check=False)
    assert out.returncode != 0 and "no Iron repository" in out.stderr
    assert not (env / "typo.iron").exists()


def test_bundles_carry_real_ref_names(env):
    a, _, location = setup_pair(env)
    bundle = next((Path(location) / "bundles").iterdir())
    heads = git(env, "bundle", "list-heads", str(bundle)).stdout
    assert heads.strip().endswith("refs/heads/main")
