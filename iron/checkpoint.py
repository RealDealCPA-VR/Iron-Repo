"""Checkpoints and restore (PRD sections 2, 15 and 16).

A checkpoint snapshots the whole working tree, including new files that git
does not track yet (but not ignored ones), without touching the branch, the
staging area or any file. It is an ordinary git commit kept under
``refs/iron/checkpoints/<id>``, so it survives anything an agent does to the
working tree and can be pushed like any other ref.
"""

import getpass
import os
import secrets
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import gitutil, locks
from .errors import IronError

NAMESPACE = "refs/iron/checkpoints/"
# Checkpoints Iron takes on its own before syncing or merging. Only these are pruned;
# manual, agent and restore-safety checkpoints are kept for ever.
AUTO_PREFIXES = ("before syncing with ", "before merge ")
KEEP_AUTO = 20


@dataclass
class Checkpoint:
    id: str
    sha: str
    date: str
    message: str

    def as_dict(self):
        return {"id": self.id, "sha": self.sha, "date": self.date, "message": self.message}


def identity_env(cwd=None):
    """Git refuses to commit without a name; fall back to the computer's user name."""
    env = dict(os.environ)
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name on some containers
        user = "iron"
    if not gitutil.git_ok("config", "user.name", cwd=cwd) and "GIT_AUTHOR_NAME" not in env:
        env.update(GIT_AUTHOR_NAME=user, GIT_COMMITTER_NAME=user)
    if not gitutil.git_ok("config", "user.email", cwd=cwd) and "GIT_AUTHOR_EMAIL" not in env:
        env.update(GIT_AUTHOR_EMAIL="%s@localhost" % user, GIT_COMMITTER_EMAIL="%s@localhost" % user)
    return env


def _temp_index(root):
    git_dir = Path(gitutil.git("rev-parse", "--absolute-git-dir", cwd=root))
    tmp_dir = git_dir / "iron" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return tmp_dir / ("index-%s" % uuid.uuid4().hex[:8]), git_dir


def snapshot_tree(root):
    """Write a tree of every non-ignored file in the working tree, leaving the real index alone."""
    tmp, git_dir = _temp_index(root)
    real = Path(gitutil.git("rev-parse", "--git-path", "index", cwd=root))
    real = real if real.is_absolute() else Path(root) / real
    try:
        if real.is_file():
            shutil.copyfile(real, tmp)
        env = dict(os.environ, GIT_INDEX_FILE=str(tmp))
        gitutil.git("add", "--all", "--", ".", cwd=root, env=env)
        return gitutil.git("write-tree", cwd=root, env=env)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def trailers(agent=None, session=None):
    agent = agent or os.environ.get("IRON_AGENT")
    session = session or os.environ.get("IRON_SESSION")
    lines = []
    if agent:
        lines.append("Iron-Agent: %s" % agent)
    if session:
        lines.append("Iron-Session: %s" % session)
    return lines


def create(message="", agent=None, session=None, cwd=None, auto=False):
    """Snapshot the working tree. ``auto`` marks Iron's own checkpoints, of which the newest 20 are kept."""
    root = gitutil.toplevel(cwd)
    with locks.repo_lock(root, "a checkpoint"):
        cp = _create(root, message, agent, session)
        if auto:
            prune_auto(root)
        return cp


def _create(root, message, agent, session):
    tree = snapshot_tree(root)
    head = gitutil.head_sha(root)
    latest = (list_checkpoints(root) or [None])[0]
    if latest and gitutil.git("rev-parse", latest.sha + "^{tree}", cwd=root) == tree:
        parent = gitutil.run_git(["rev-parse", "--verify", "--quiet", latest.sha + "^"], cwd=root, check=False)
        if (parent.stdout.decode().strip() or None) == head:
            return latest  # nothing changed since the last checkpoint
    now = datetime.now()
    cid = now.strftime("%Y%m%d-%H%M%S-") + "%03d" % (now.microsecond // 1000) + secrets.token_hex(2)
    body = "\n".join(["Iron-Checkpoint: %s" % cid] + trailers(agent, session))
    text = "%s\n\n%s\n" % (message or "checkpoint", body)
    args = ["commit-tree", tree, "-F", "-"] + (["-p", head] if head else [])
    sha = gitutil.git(*args, cwd=root, input=text, env=identity_env(root))
    gitutil.git("update-ref", "-m", "iron checkpoint", NAMESPACE + cid, sha, cwd=root)
    date = gitutil.git("log", "-1", "--format=%cI", sha, cwd=root)  # the same format list_checkpoints shows
    return Checkpoint(cid, sha, date, message or "checkpoint")


def prune_auto(root, keep=KEEP_AUTO):
    """Delete all but the newest ``keep`` automatic checkpoints. Returns how many were deleted."""
    auto = [c for c in list_checkpoints(root) if c.message.startswith(AUTO_PREFIXES)]
    old = auto[keep:]
    if old:
        script = "".join("delete %s%s\n" % (NAMESPACE, c.id) for c in old)
        gitutil.git("update-ref", "--stdin", cwd=root, input=script)
    return len(old)


def list_checkpoints(cwd=None):
    out = gitutil.git("for-each-ref", NAMESPACE, "--format=%(refname)%00%(objectname)%00%(committerdate:iso-strict)%00%(subject)",
                      cwd=cwd)
    items = []
    for line in out.splitlines():
        ref, sha, date, subject = line.split("\0")
        items.append(Checkpoint(ref[len(NAMESPACE):], sha, date, subject))
    return sorted(items, key=lambda c: c.id, reverse=True)


def resolve(target, cwd=None):
    """A checkpoint id (or unique prefix), 'last', or any commit name."""
    cps = list_checkpoints(cwd)
    if target in ("last", "latest"):
        if not cps:
            raise IronError("there are no checkpoints yet (create one with `iron checkpoint`)")
        return cps[0].sha
    matches = [c for c in cps if c.id == target] or [c for c in cps if c.id.startswith(target)]
    if len(matches) == 1:
        return matches[0].sha
    proc = gitutil.run_git(["rev-parse", "--verify", "--quiet", target + "^{commit}"], cwd=cwd, check=False)
    sha = proc.stdout.decode().strip()
    if proc.returncode != 0 or not sha:
        raise IronError("no checkpoint or commit called %r (see `iron checkpoints` or `iron history`)" % target)
    return sha


def _files(tree_ish, root):
    out = gitutil.run_git(["ls-tree", "-r", "-z", "--name-only", tree_ish], cwd=root).stdout.decode("utf-8")
    return {p for p in out.split("\0") if p}


def restore(target, cwd=None, agent=None, session=None):
    """Make the working tree match ``target`` exactly, after saving a safety checkpoint.

    Files that exist now but not in the target are removed (they stay in the
    safety checkpoint). Ignored files are never touched. The branch and HEAD do
    not move; afterwards ``git status`` shows the restored content as changes.
    """
    root = gitutil.toplevel(cwd)
    with locks.repo_lock(root, "a restore"):
        return _restore(root, target, agent, session)


def _restore(root, target, agent, session):
    sha = resolve(target, root)
    safety = create("before restore to %s" % target, agent=agent, session=session, cwd=root)
    wanted = _files(sha, root)
    for rel in sorted(_files(safety.sha, root) - wanted, reverse=True):
        path = Path(root, rel)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        parent = path.parent
        while parent != Path(root) and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    # Write through the real index, then point it back at HEAD: entries that did not
    # change keep valid stat data, so `git status` shows only real differences.
    gitutil.git("read-tree", "-u", "--reset", sha, cwd=root)
    if gitutil.head_sha(root):
        gitutil.git("reset", "--quiet", cwd=root)
    else:
        gitutil.git("read-tree", "--empty", cwd=root)
    return safety, sha
