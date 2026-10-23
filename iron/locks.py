"""Coordination between Iron processes working on the same repository.

``repo_lock`` serializes everything that changes the repository (sync,
commit, checkpoint, restore, the autosave tick and the autosync hook), so two
of them never interleave their git commands. It is a file under
``.git/iron/repo.lock`` created with exclusive create, holding the owner's pid
and start time. A lock whose process is gone is taken over at once.

The lock is re-entrant within one process (``iron sync`` takes a checkpoint
while holding it) and is inherited by Iron child processes started while it is
held (autosave runs ``iron sync`` as a child): the holder puts its token in
``IRON_REPO_LOCK`` and a child that finds the same token in the lock file
knows its parent holds the lock for it.

The agent session marker, ``.git/iron/agent-session.json``, says that ``iron
agent`` is running here. Background saving and syncing wait while it is live,
so an agent's half-done work is never published and later pulled back in over
a rollback.
"""

import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import gitutil
from .errors import IronError

LOCK_ENV = "IRON_REPO_LOCK"
LOCK_MAX_AGE = 6 * 3600       # a lock this old is stale even if its pid was reused
AGENT_MAX_AGE = 24 * 3600     # an agent marker this old is stale
DEFAULT_WAIT = 300            # seconds a command waits for another Iron operation

_held = {}                    # lock path -> {"thread", "depth", "token", "inherited", "saved_env"}
_guard = threading.Lock()


class RepoBusy(IronError):
    def __init__(self, holder):
        self.holder = holder or {}
        what = self.holder.get("what")
        super().__init__("another Iron operation is running (pid %s%s). Try again when it finishes."
                         % (self.holder.get("pid", "?"), ": " + what if what else ""))


def pid_alive(pid):
    """True if a process with this id is running (unknown counts as alive)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return kernel32.GetLastError() == 5  # access denied: it exists
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def iron_dir(root):
    """<git dir>/iron for the repository at ``root``."""
    return Path(gitutil.git("rev-parse", "--absolute-git-dir", cwd=str(root))) / "iron"


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def lock_holder(root):
    """The lock file's content ({pid, time, what, token}), or None when nobody holds it."""
    return _read_json(iron_dir(root) / "repo.lock")


def _stale(path, info):
    if info is None:
        # Being written right now, or garbage: give a writer a moment before taking it over.
        try:
            return time.time() - path.stat().st_mtime > 10
        except OSError:
            return False
    if not pid_alive(info.get("pid")):
        return True
    return time.time() - float(info.get("time") or 0) > LOCK_MAX_AGE


def _try_create(path, info):
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(info, f)
    return True


@contextmanager
def repo_lock(root, what="", timeout=None, on_wait=None):
    """Hold the repository lock. Waits up to ``timeout`` seconds (IRON_REPO_LOCK_TIMEOUT,
    default 300; 0 means do not wait), then raises RepoBusy."""
    path = iron_dir(root) / "repo.lock"
    key = os.path.normcase(str(path))
    me = threading.get_ident()
    if timeout is None:
        try:
            timeout = float(os.environ.get("IRON_REPO_LOCK_TIMEOUT", DEFAULT_WAIT))
        except ValueError:
            timeout = DEFAULT_WAIT
    deadline = time.monotonic() + timeout
    told = False
    while True:
        with _guard:
            entry = _held.get(key)
            if entry is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                token = secrets.token_hex(8)
                info = {"pid": os.getpid(), "time": time.time(), "what": what, "token": token}
                inherited_token = os.environ.get(LOCK_ENV)
                current = _read_json(path) if path.exists() else None
                if current and inherited_token and current.get("token") == inherited_token:
                    entry = {"thread": me, "depth": 1, "token": inherited_token, "inherited": True}
                elif _try_create(path, info):
                    entry = {"thread": me, "depth": 1, "token": token, "inherited": False}
                if entry is not None:
                    entry["saved_env"] = os.environ.get(LOCK_ENV)
                    os.environ[LOCK_ENV] = entry["token"]
                    _held[key] = entry
                    break
            elif entry["thread"] == me:
                entry["depth"] += 1
                break
        holder = _read_json(path) if path.exists() else None
        if path.exists() and key not in _held and _stale(path, holder):
            again = _read_json(path)
            if (again or {}).get("token") == (holder or {}).get("token"):  # not just taken over by another
                try:
                    path.unlink()  # its owner is gone
                except OSError:
                    pass
            continue
        if time.monotonic() >= deadline:
            raise RepoBusy(holder)
        if on_wait and not told:
            on_wait(holder or {})
            told = True
        time.sleep(0.1)
    try:
        yield
    finally:
        with _guard:
            entry = _held[key]
            entry["depth"] -= 1
            if entry["depth"] == 0:
                del _held[key]
                if entry["saved_env"] is None:
                    os.environ.pop(LOCK_ENV, None)
                else:
                    os.environ[LOCK_ENV] = entry["saved_env"]
                if not entry["inherited"]:
                    try:
                        path.unlink()
                    except OSError:
                        pass


def holds_lock(root):
    """True if this thread holds the repository lock (itself, or lent by its parent process)."""
    key = os.path.normcase(str(iron_dir(root) / "repo.lock"))
    with _guard:
        entry = _held.get(key)
        return bool(entry and entry["thread"] == threading.get_ident())


# -- the agent session marker ---------------------------------------------------

def agent_marker_path(root):
    return iron_dir(root) / "agent-session.json"


def agent_session(root):
    """The live agent session working in this repository ({pid, agent, session, started}), or None.

    A marker whose process has exited, or that is older than a day, is stale and ignored."""
    try:
        path = agent_marker_path(root)
    except IronError:
        return None
    info = _read_json(path)
    if not info:
        return None
    if not pid_alive(info.get("pid")) or time.time() - float(info.get("started") or 0) > AGENT_MAX_AGE:
        return None
    return info


def describe_agent(info):
    return "%s (session %s, process %s)" % (info.get("agent") or "an agent", info.get("session") or "-",
                                            info.get("pid"))


@contextmanager
def agent_marker(root, agent, session):
    """Mark this repository as being worked on by ``agent`` until the block ends."""
    path = agent_marker_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    info = {"pid": os.getpid(), "agent": agent, "session": session, "started": time.time()}
    tmp = path.with_name("agent-session.json.tmp%d" % os.getpid())
    tmp.write_text(json.dumps(info), encoding="utf-8")
    os.replace(str(tmp), str(path))
    try:
        yield info
    finally:
        current = _read_json(path)
        if current is None or current.get("pid") == os.getpid():
            try:
                path.unlink()
            except OSError:
                pass
