"""Thin wrappers around the git command line. Git is Iron's engine; Iron never
re-implements commits, trees, diffs or merges."""

import os
import subprocess
import sys
import sysconfig
from pathlib import Path

from .errors import IronError

MIN_GIT = (2, 31)


class GitError(IronError):
    def __init__(self, args, returncode, stderr):
        self.returncode = returncode
        self.stderr = stderr
        super().__init__("git %s failed: %s" % (" ".join(args[:3]), stderr.strip() or "exit %d" % returncode))


def run_git(args, cwd=None, input=None, env=None, check=True):
    if isinstance(input, str):
        input = input.encode("utf-8")
    proc = subprocess.run(["git"] + list(args), cwd=cwd, input=input, env=env, capture_output=True)
    if check and proc.returncode != 0:
        raise GitError(list(args), proc.returncode, proc.stderr.decode("utf-8", errors="replace"))
    return proc


def git(*args, cwd=None, input=None, env=None):
    out = run_git(args, cwd=cwd, input=input, env=env).stdout.decode("utf-8", errors="replace")
    return out[:-1] if out.endswith("\n") else out


def git_ok(*args, cwd=None, env=None):
    return run_git(args, cwd=cwd, env=env, check=False).returncode == 0


def git_version():
    out = git("--version")  # "git version 2.50.0.windows.2"
    parts = out.split()[2].split(".")
    return tuple(int(p) for p in parts[:2] if p.isdigit())


def object_exists(sha, cwd=None):
    return git_ok("cat-file", "-e", sha, cwd=cwd)


def is_ancestor(old, new, cwd=None):
    return git_ok("merge-base", "--is-ancestor", old, new, cwd=cwd)


def object_format(cwd=None):
    try:
        return git("rev-parse", "--show-object-format", cwd=cwd) or "sha1"
    except GitError:
        return "sha1"


def config_bool(name, cwd=None, default=False):
    proc = run_git(["config", "--type=bool", "--get", name], cwd=cwd, check=False)
    if proc.returncode != 0:
        return default
    return proc.stdout.decode().strip() == "true"


def env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def toplevel(cwd=None):
    try:
        return Path(git("rev-parse", "--show-toplevel", cwd=cwd))
    except GitError:
        raise IronError("not inside a repository (run `iron setup` or `iron init` first)") from None


def in_worktree(cwd=None):
    return git_ok("rev-parse", "--is-inside-work-tree", cwd=cwd)


def head_sha(cwd=None):
    proc = run_git(["rev-parse", "--verify", "--quiet", "HEAD"], cwd=cwd, check=False)
    return proc.stdout.decode().strip() or None


def current_branch(cwd=None):
    proc = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=cwd, check=False)
    return proc.stdout.decode().strip() or None


def helper_dirs():
    """Directories that hold a git-remote-iron launcher, so git can find the helper."""
    names = ("git-remote-iron", "git-remote-iron.exe")
    candidates = [Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else None,  # standalone build
                  Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else None,
                  Path(__file__).resolve().parent.parent / "bin",
                  Path(sysconfig.get_path("scripts"))]
    try:
        candidates.append(Path(sysconfig.get_path("scripts", "%s_user" % os.name)))
    except KeyError:
        pass
    out = []
    for d in candidates:
        if d and any((d / n).is_file() for n in names) and d not in out:
            out.append(d)
    return out


def is_frozen():
    """True inside a standalone executable (PyInstaller), where sys.executable is Iron itself."""
    return bool(getattr(sys, "frozen", False))


def iron_command():
    """argv prefix that runs the iron CLI in a child process, from a checkout or a standalone build."""
    if is_frozen():
        return [sys.executable]
    package_parent = str(Path(__file__).resolve().parent.parent)
    run = "import sys;sys.path.insert(0,sys.argv[1]);from iron.cli import main;sys.exit(main(sys.argv[2:]))"
    return [sys.executable, "-c", run, package_parent]


def helper_on_path():
    """The git-remote-iron that git itself would find on PATH, or None."""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        for n in ("git-remote-iron", "git-remote-iron.exe"):
            p = Path(d.strip('"')) / n if d else None
            if p and p.is_file():
                return p
    return None


def helper_env():
    """The environment for running git so that iron:: remotes always work."""
    env = dict(os.environ)
    if helper_on_path() is None:
        env["PATH"] = os.pathsep.join([str(d) for d in helper_dirs()] + [env.get("PATH", "")])
    return env
