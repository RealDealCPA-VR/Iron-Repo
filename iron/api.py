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

import subprocess

from . import checkpoint, gitutil, maintenance
from .cli import commit_all, default_remote, iron_remotes, open_remote


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

    def diff(self, against="HEAD"):
        """Unified diff of every change (including new files) relative to ``against``."""
        tree = checkpoint.snapshot_tree(self.path)
        base = against if gitutil.head_sha(self.path) else gitutil.git("hash-object", "-t", "tree", "--stdin",
                                                                       cwd=self.path, input="")
        return self._git("diff", base, tree)

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
        return history(self.path, n)

    def push(self, remote=None, branch=None):
        remote = remote or default_remote(self.path)
        branch = branch or gitutil.current_branch(self.path)
        proc = subprocess.run(["git", "push", "--porcelain", remote, branch], cwd=self.path,
                              env=gitutil.helper_env(), capture_output=True, text=True)
        return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr).strip()}

    def verify(self, remote=None, deep=False):
        repo, _ = open_remote(remote, cwd=self.path)
        return maintenance.verify(repo, deep=deep)
