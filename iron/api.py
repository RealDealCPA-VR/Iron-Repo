"""Python API for coding agents (PRD section 15).

    from iron.api import Repository

    repo = Repository("path/to/project", agent="Claude", session="task-42")
    cp = repo.checkpoint("before refactor")
    ...  # edit files
    if tests_failed:
        repo.restore(cp["id"])
    else:
        repo.commit("Refactor storage layer")
        repo.push()

Every method returns plain dicts/lists, ready to serialize as JSON.
"""

import os
import subprocess
import sys
from pathlib import Path

from . import checkpoint, gitutil, locations, maintenance
from .cli import commit_all, default_remote, iron_remotes, open_remote
from .errors import IronError, KeyMissing
from .remote import RemoteRepo

SESSION_FORMAT = ("%H%x00%ar%x00%aI%x00%s%x00%(trailers:key=Iron-Agent,valueonly,separator=%x2C)%x00"
                  "%(trailers:key=Iron-Session,valueonly,separator=%x2C)%x00%(trailers:key=Iron-Checkpoint,valueonly)%x1e")


def change_kind(code):
    """A porcelain status code in words."""
    if "U" in code or code in ("AA", "DD"):
        return "conflict"
    if "?" in code or "A" in code:
        return "new"
    if "D" in code:
        return "deleted"
    if "R" in code:
        return "renamed"
    return "changed"


class Repository:
    def __init__(self, path=".", agent=None, session=None):
        self.path = gitutil.toplevel(path)
        self.agent = agent
        self.session = session

    def _git(self, *args):
        return gitutil.git(*args, cwd=self.path)

    def status(self):
        branch = gitutil.current_branch(self.path)
        changes = []
        for line in self._git("status", "--porcelain").splitlines():
            if line.strip():
                changes.append({"status": line[:2].strip(), "path": line[3:]})
        return {"branch": branch, "head": gitutil.head_sha(self.path), "changes": changes,
                "remotes": iron_remotes(self.path), "clean": not changes}

    def checkpoint(self, message=""):
        return checkpoint.create(message, agent=self.agent, session=self.session, cwd=self.path).as_dict()

    def checkpoints(self):
        return [c.as_dict() for c in checkpoint.list_checkpoints(self.path)]

    def changes(self):
        """Every changed file one by one (new folders expanded), with its kind in words."""
        out = gitutil.run_git(["status", "--porcelain", "-z", "--untracked-files=all"],
                              cwd=self.path).stdout.decode("utf-8", errors="replace")
        parts, items = out.split("\0"), []
        while parts:
            entry = parts.pop(0)
            if len(entry) < 4:
                continue
            item = {"path": entry[3:], "status": entry[:2].strip(), "kind": change_kind(entry[:2])}
            if entry[0] in "RC" and parts:
                item["from"] = parts.pop(0)
            items.append(item)
        return items

    def diff(self, against="HEAD", paths=()):
        """Unified diff of every change (including new files) relative to ``against``, or of ``paths`` only."""
        tree = checkpoint.snapshot_tree(self.path)
        base = against if gitutil.head_sha(self.path) else gitutil.git("hash-object", "-t", "tree", "--stdin",
                                                                       cwd=self.path, input="")
        return self._git("diff", "--no-color", "--no-ext-diff", base, tree, "--", *paths)

    def commit(self, message):
        """Commit every change with agent attribution. Returns the new sha, or None if nothing changed."""
        return commit_all(message, cwd=self.path, agent=self.agent, session=self.session)

    def restore(self, target):
        safety, sha = checkpoint.restore(target, cwd=self.path, agent=self.agent, session=self.session)
        return {"restored": sha, "safety_checkpoint": safety.id}

    def branch(self, name=None, switch=True):
        if name is None:
            return self._git("branch", "--format=%(refname:short)").splitlines()
        self._git("switch", "-c", name) if switch else self._git("branch", name)
        return name

    def history(self, n=20):
        from .cli import history
        return history(self.path, n) if gitutil.head_sha(self.path) else []

    def sync_state(self):
        """For each Iron remote, how this branch compares with it as of the last sync (no network)."""
        branch, head = gitutil.current_branch(self.path), gitutil.head_sha(self.path)
        items = []
        for name, loc in sorted(iron_remotes(self.path).items()):
            item = {"remote": name, "location": locations.describe_location(loc), "ahead": 0, "behind": 0,
                    "state": "unpublished"}
            tracking = "refs/remotes/%s/%s" % (name, branch)
            if branch and head and gitutil.git_ok("rev-parse", "--verify", "--quiet", tracking, cwd=self.path):
                ahead, behind = (int(x) for x in self._git("rev-list", "--left-right", "--count",
                                                           "HEAD...%s" % tracking).split())
                state = "diverged" if ahead and behind else "ahead" if ahead else "behind" if behind else "synced"
                item.update(ahead=ahead, behind=behind, state=state)
            items.append(item)
        return items

    def storage(self):
        """Where each Iron remote lives, whether it answers, and whether it is encrypted."""
        items = []
        for name, loc in sorted(iron_remotes(self.path).items()):
            item = {"remote": name, "location": locations.describe_location(loc), "healthy": False, "detail": "",
                    "exists": None, "encrypted": False, "key_on_this_computer": None}
            try:
                repo = RemoteRepo(loc)
                item["healthy"], item["detail"] = repo.store.health_check()
                try:
                    marker = repo.open(required=False)
                    item["exists"] = marker is not None
                    item["encrypted"] = bool(marker and marker.get("encryption"))
                    item["key_on_this_computer"] = True if item["encrypted"] else None
                except KeyMissing:
                    item.update(exists=True, encrypted=True, key_on_this_computer=False)
            except IronError as e:
                item.update(healthy=False, detail=str(e))
            items.append(item)
        return items

    def agent_sessions(self, n=500):
        """Agent work recorded in commit and checkpoint trailers, newest session first."""
        if not self._git("for-each-ref", "--count=1", "--format=%(refname)"):
            return []
        sessions = {}
        for chunk in self._git("log", "--all", "-n", str(n), "--format=" + SESSION_FORMAT).split("\x1e"):
            fields = (chunk.strip("\n").split("\x00") + [""] * 7)[:7]
            _sha, when, date, subject, agent, session, cp = (f.strip() for f in fields)
            if not agent:
                continue
            item = sessions.setdefault((agent, session), {
                "agent": agent, "session": session, "versions": 0, "checkpoints": 0,
                "last": when, "last_date": date, "latest": subject})
            item["checkpoints" if cp else "versions"] += 1
            item.update(first=when, first_date=date)
        return sorted(sessions.values(), key=lambda s: s["last_date"], reverse=True)

    def sync(self, remote=None):
        """Run ``iron sync`` in a child process (it reports as it goes); returns its outcome and output."""
        here = str(Path(__file__).resolve().parent.parent)
        env = dict(gitutil.helper_env(), PYTHONIOENCODING="utf-8", GIT_TERMINAL_PROMPT="0",
                   PYTHONPATH=os.pathsep.join(p for p in (here, os.environ.get("PYTHONPATH")) if p))
        proc = subprocess.run([sys.executable, "-m", "iron", "sync"] + ([remote] if remote else []), cwd=self.path,
                              env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL)
        return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr).strip()}

    def push(self, remote=None, branch=None):
        remote = remote or default_remote(self.path)
        branch = branch or gitutil.current_branch(self.path)
        proc = subprocess.run(["git", "push", "--porcelain", remote, branch], cwd=self.path,
                              env=gitutil.helper_env(), capture_output=True, text=True)
        return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr).strip()}

    def verify(self, remote=None, deep=False):
        repo, _ = open_remote(remote, cwd=self.path)
        return maintenance.verify(repo, deep=deep)
