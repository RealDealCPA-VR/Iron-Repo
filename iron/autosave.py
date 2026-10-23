"""Automatic saving and syncing, for people who should not have to remember git.

``iron autosave`` saves a version whenever something changed and syncs it,
every few minutes, in the foreground or (``--install``) in the background at
login. ``iron autosync on`` adds a git ``post-commit`` hook that syncs in the
background after every commit.

Both are careful: they never force anything, never commit files that look
like secrets, never run while git is in the middle of a merge, rebase or
cherry-pick, and stop with a clear message when changes conflict.
"""

import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import checkpoint, gitutil, keys
from .cli import BAD, OK, WARN, contains_private_key, iron_remotes, looks_secret, say
from .errors import IronError

DOT_OFF = "o"
PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)
SAVED, CLEAN, SKIPPED, SYNCED, OFFLINE, CONFLICT = "saved", "clean", "skipped", "synced", "offline", "conflict"
UNMERGED = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
BUSY = [("MERGE_HEAD", "a merge is in progress"), ("rebase-merge", "a rebase is in progress"),
        ("rebase-apply", "a rebase is in progress"), ("CHERRY_PICK_HEAD", "a cherry-pick is in progress"),
        ("REVERT_HEAD", "a revert is in progress"), ("index.lock", "another git command is running")]
HEARTBEAT = 20          # seconds between "still alive" updates while waiting
STALE = 90              # a heartbeat older than this means the process is gone
LOG_BYTES, LOG_BACKUPS = 1048576, 3
SKIP_ENV = "IRON_AUTOSYNC_SKIP"   # set while autosave commits, so the hook does not sync twice
HOOK_BEGIN, HOOK_END = "# >>> iron autosync >>>", "# <<< iron autosync <<<"
CHAINED = "post-commit.iron-chained"
RUN_IRON = "import sys;sys.path.insert(0,sys.argv[1]);from iron.cli import main;sys.exit(main(sys.argv[2:]))"
RUN_HOOK = "import sys;sys.path.insert(0,sys.argv[1]);from iron.autosave import hook_main;hook_main()"


# -- small helpers ------------------------------------------------------------

def parse_interval(text):
    """'30s', '5m', '1h' or a number of seconds."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*$", str(text).lower())
    if not m:
        raise IronError("say how often like 30s, 5m or 1h (got %r)" % text)
    seconds = float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]
    if seconds < 5:
        raise IronError("autosave runs at most every 5 seconds")
    return seconds


def describe_interval(seconds):
    if seconds % 3600 == 0:
        return "%d h" % (seconds // 3600)
    if seconds % 60 == 0:
        return "%d min" % (seconds // 60)
    return "%g s" % seconds


def repo_id(root):
    """A readable, unique name for a repository: its folder name plus a hash of its path."""
    root = Path(root).resolve()
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", root.name).strip("-.") or "repo"
    return "%s-%s" % (name[:40], hashlib.sha1(os.path.normcase(str(root)).encode("utf-8")).hexdigest()[:8])


def state_dir():
    return keys.iron_home() / "autosave"


def log_path(root, kind="autosave"):
    return keys.iron_home() / "logs" / ("%s-%s.log" % (kind, repo_id(root)))


def git_paths(root, names):
    out = gitutil.git("rev-parse", *[x for n in names for x in ("--git-path", n)], cwd=root).splitlines()
    return [p if Path(p).is_absolute() else Path(root) / p for p in map(Path, out)]


def require_iron_remote(root):
    if not iron_remotes(root):
        raise IronError("this repository has no Iron remote yet, so there is nowhere to sync to.\n"
                        "  Set one up first:   iron setup --to onedrive      (or google-drive, dropbox, box)\n"
                        "  or any folder:      iron remote add origin D:/Backups/%s.iron" % Path(root).name)


def hide_child_consoles():
    """Background runs have no console on Windows; keep every git child from opening one."""
    if os.name != "nt" or getattr(subprocess.Popen, "_iron_hidden", False):
        return
    original = subprocess.Popen.__init__

    def init(self, *args, **kwargs):
        if not kwargs.get("creationflags"):
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = init
    subprocess.Popen._iron_hidden = True


class Log:
    """A rotating log file in IRON_HOME/logs, mirrored to the console when someone is watching."""

    def __init__(self, path, console=True, quiet=False, max_bytes=LOG_BYTES):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
        self.console, self.quiet = console, quiet
        self.logger = logging.getLogger("iron.autosave.%s" % hashlib.sha1(str(path).encode()).hexdigest()[:12])
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO)
        self.handler = logging.handlers.RotatingFileHandler(str(path), maxBytes=max_bytes, backupCount=LOG_BACKUPS,
                                                            encoding="utf-8", delay=True)
        self.handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        self.logger.addHandler(self.handler)
        self.last_secrets = None

    def info(self, msg, loud=False):
        self.logger.info(msg)
        if self.console and (loud or not self.quiet):
            say(msg)

    def close(self):
        self.logger.removeHandler(self.handler)
        self.handler.close()


# -- one tick -----------------------------------------------------------------

def busy_reason(root):
    """Why git must not be touched right now, or None."""
    for path, (name, reason) in zip(git_paths(root, [n for n, _ in BUSY]), BUSY):
        if path.exists():
            if name == "index.lock":
                return "%s (if none is, delete %s)" % (reason, path)
            return reason
    return None


def status_entries(root):
    """[(XY, path)] from `git status --porcelain`, one entry per file."""
    out = gitutil.run_git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=root).stdout
    parts = out.decode("utf-8", errors="replace").split("\0")
    entries, i = [], 0
    while i < len(parts):
        item = parts[i]
        i += 1
        if len(item) < 4:
            continue
        xy, path = item[:2], item[3:]
        if "R" in xy or "C" in xy:
            i += 1  # the original name of a rename or copy follows
        entries.append((xy, path))
    return entries


def in_conflict(root):
    return git_paths(root, ["MERGE_HEAD"])[0].exists() or any(xy in UNMERGED for xy, _ in status_entries(root))


def secret_files(root, paths):
    out = []
    for p in paths:
        full = Path(root) / p
        if full.is_file() and (looks_secret(p) or contains_private_key(full)):
            out.append(p)
    return out


def _names(output):
    return [p for p in output.decode("utf-8", errors="replace").split("\0") if p]


def save(root, entries, log):
    """Commit every change except files that look like secrets. Returns (sha or None, result)."""
    staged = _names(gitutil.run_git(["diff", "--cached", "--name-only", "-z", "--diff-filter=d"], cwd=root).stdout)
    risky_staged = secret_files(root, staged)
    if risky_staged:
        log.info("%s Not saving: %s %s staged and look%s like a secret. Unstage with `git restore --staged %s`."
                 % (WARN, ", ".join(risky_staged[:5]), "is" if len(risky_staged) == 1 else "are",
                    "s" if len(risky_staged) == 1 else "", risky_staged[0]), loud=True)
        return None, SKIPPED
    risky = secret_files(root, [p for _, p in entries])
    if risky != log.last_secrets and risky:
        log.info("%s Left out %d file(s) that look like secrets: %s. Add them to .gitignore to keep them out for good."
                 % (WARN, len(risky), ", ".join(risky[:6]) + (" ..." if len(risky) > 6 else "")), loud=True)
    log.last_secrets = risky
    spec = "\0".join(["."] + [":(exclude,literal)%s" % p for p in risky]) + "\0"
    gitutil.run_git(["add", "--all", "--pathspec-from-file=-", "--pathspec-file-nul"], cwd=root, input=spec)
    changed = _names(gitutil.run_git(["diff", "--cached", "--name-only", "-z"], cwd=root).stdout)
    if not changed:
        return None, CLEAN
    lines = ["Iron-Autosave: true"] + checkpoint.trailers()
    text = "Autosave: %d file(s) changed\n\n%s\n" % (len(changed), "\n".join(lines))
    env = dict(checkpoint.identity_env(root), **{SKIP_ENV: "1"})
    proc = gitutil.run_git(["commit", "--quiet", "-F", "-"], cwd=root, input=text, env=env, check=False)
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).decode("utf-8", errors="replace").strip().splitlines()
        log.info("%s Could not save a version: %s. Will try again next time." % (BAD, detail[-1] if detail else "git commit failed"),
                 loud=True)
        return None, SKIPPED
    sha = gitutil.head_sha(root)
    log.info("%s Saved %d changed file(s) as %s" % (OK, len(changed), sha[:10]))
    return sha, SAVED


CONFLICT_HELP = ("%s Autosave stopped: your changes and the ones from the other computer touch the same lines.\n"
                 "  Open the files marked as conflicted, fix them, then run\n"
                 "    iron commit -am \"Combine changes\"   and   iron sync\n"
                 "  and start autosave again. To back out instead: git merge --abort" % BAD)


def sync_once(root, log, timeout=None):
    """Run `iron sync` in a child process, so its output lands in the log. Returns SYNCED, OFFLINE or CONFLICT."""
    timeout = timeout or float(os.environ.get("IRON_AUTOSAVE_SYNC_TIMEOUT", "1800"))
    env = dict(gitutil.helper_env(), **{SKIP_ENV: "1"})
    try:
        proc = subprocess.run(gitutil.iron_command() + ["sync"], cwd=str(root), env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.info("%s Sync took longer than %d s and was stopped. Will try again next time." % (WARN, timeout), loud=True)
        return OFFLINE
    text = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    for line in lines:
        log.logger.info("  sync: %s", line)
    if proc.returncode == 0:
        published = [l for l in lines if "Published" in l or "Pulled" in l]
        for line in published:
            log.info(line)
        return SYNCED
    if in_conflict(root):
        log.info(CONFLICT_HELP, loud=True)
        return CONFLICT
    log.info("%s Could not sync right now (offline, or the storage is not reachable). Will try again next time.%s"
             % (WARN, ("\n  " + lines[-1]) if lines else ""), loud=True)
    return OFFLINE


def tick(root, log, sync=True):
    """Save a version if anything changed, then sync. Returns the most important outcome."""
    reason = busy_reason(root)
    if reason:
        log.info("Skipped: %s. Will try again next time." % reason)
        return SKIPPED
    if not gitutil.current_branch(root):
        log.info("Skipped: you are not on a branch (detached HEAD). Switch to one, e.g. `iron switch main`.")
        return SKIPPED
    entries = status_entries(root)
    if any(xy in UNMERGED for xy, _ in entries):
        log.info(CONFLICT_HELP, loud=True)
        return CONFLICT
    result = CLEAN
    if entries:
        _, result = save(root, entries, log)
    if result == CLEAN:
        log.info("Nothing changed.")
    if sync:
        synced = sync_once(root, log)
        if synced in (CONFLICT, OFFLINE):
            return synced
    return result


# -- the loop -----------------------------------------------------------------

def _state_file(root):
    return state_dir() / ("%s.json" % repo_id(root))


def _stop_file(root):
    return state_dir() / ("%s.stop" % repo_id(root))


def read_state(root):
    try:
        return json.loads(_state_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(root, **fields):
    state = dict(read_state(root), **fields)
    path = _state_file(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp%d" % os.getpid())
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        pass
    return state


def running(root):
    """The state of a live autosave process for this repository, or None."""
    state = read_state(root)
    if state.get("pid") and time.time() - state.get("heartbeat", 0) < STALE:
        return state
    return None


def run_loop(root, every, log, once=False, background=False):
    root = Path(root)
    require_iron_remote(root)
    other = running(root)
    if other and other.get("pid") != os.getpid():
        raise IronError("autosave is already running for this repository (process %s). "
                        "Stop it first, or see `iron autosave --status`." % other["pid"])
    try:
        _stop_file(root).unlink()
    except OSError:
        pass
    if not once:
        log.info("Autosave is on for %s: saving and syncing every %s.%s"
                 % (root, describe_interval(every), "" if background else " Press Ctrl+C to stop."), loud=True)
    try:
        while True:
            write_state(root, pid=os.getpid(), path=str(root), every=every, heartbeat=time.time(),
                        background=background)
            result = tick(root, log)
            write_state(root, heartbeat=time.time(), last_tick=datetime.now().isoformat(timespec="seconds"),
                        last_result=result)
            if result == CONFLICT:
                return 1
            if once:
                return 0
            deadline = time.monotonic() + every
            while time.monotonic() < deadline:
                if _stop_file(root).exists():
                    log.info("Autosave stopped (it was uninstalled).", loud=True)
                    return 0
                time.sleep(max(0.05, min(HEARTBEAT, deadline - time.monotonic())))
                write_state(root, heartbeat=time.time())
    except KeyboardInterrupt:
        log.info("Autosave stopped.", loud=True)
        return 0
    finally:
        state = read_state(root)
        if state.get("pid") == os.getpid():
            write_state(root, pid=None, heartbeat=0)
        for p in (_stop_file(root),):
            try:
                p.unlink()
            except OSError:
                pass


def run_background(root, every):
    """Entry point of the runner script registered at login."""
    hide_child_consoles()
    log = Log(log_path(root), console=False)
    try:
        return run_loop(root, every, log, background=True)
    except IronError as e:
        log.info("%s %s" % (BAD, e))
        return 1
    except Exception:  # noqa: BLE001 - nobody sees a traceback in the background; keep it in the log
        import traceback
        log.info("%s autosave crashed:\n%s" % (BAD, traceback.format_exc()))
        return 1
    finally:
        log.close()


# -- registering at login -----------------------------------------------------

def target_platform():
    forced = os.environ.get("IRON_AUTOSAVE_PLATFORM", "").strip().lower()
    if forced:
        if forced not in ("windows", "macos", "linux", "linux-systemd", "linux-cron"):
            raise IronError("IRON_AUTOSAVE_PLATFORM must be windows, macos, linux, linux-systemd or linux-cron")
        return forced
    if os.name == "nt":
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def dry_run():
    return gitutil.env_flag("IRON_AUTOSAVE_DRY_RUN")


def background_python(plat):
    exe = Path(sys.executable)
    if plat == "windows":
        w = exe.with_name("pythonw.exe")
        if w.is_file():
            return str(w)
    return str(exe)


def systemd_available():
    if not shutil.which("systemctl"):
        return False
    try:
        return subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def runner_script(root, every):
    home = str(keys.iron_home())
    return ("# Iron autosave for %s\n"
            "# Written by `iron autosave --install`; `iron autosave --uninstall` removes it.\n"
            "import os\nimport sys\n\n"
            "sys.path.insert(0, %r)\n"
            "os.environ[\"PATH\"] = %r\n"
            "os.environ.setdefault(\"IRON_HOME\", %r)\n"
            "from iron.autosave import run_background\n\n"
            "sys.exit(run_background(%r, %r))\n"
            % (root, PACKAGE_PARENT, os.environ.get("PATH", ""), home, str(root), every))


def _label(root):
    return "com.ironrepo.autosave.%s" % repo_id(root)


def _startup_dir():
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _registration(root, plat):
    """(kind, path) of where autosave is registered on this platform. kind is file or cron."""
    rid = repo_id(root)
    if plat == "windows":
        return "file", _startup_dir() / ("iron-autosave-%s.cmd" % rid)
    if plat == "macos":
        return "file", Path.home() / "Library" / "LaunchAgents" / ("%s.plist" % _label(root))
    unit = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / (
        "iron-autosave-%s.service" % rid)
    if plat == "linux-systemd" or (plat == "linux" and (unit.exists() or systemd_available())):
        return "systemd", unit
    return "cron", None


def _xml(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _read_crontab():
    if not shutil.which("crontab"):
        return ""
    proc = subprocess.run(["crontab", "-l"], capture_output=True)
    return proc.stdout.decode("utf-8", errors="replace") if proc.returncode == 0 else ""


def _cron_marker(root):
    return "# iron-autosave %s" % repo_id(root)


def plan_install(root, every):
    """What --install writes and runs: (files [(path, text)], commands [argv], description, start_now)."""
    plat = target_platform()
    py = background_python(plat)
    runner = state_dir() / ("%s-runner.py" % repo_id(root))
    files = [(runner, runner_script(root, every))]
    kind, path = _registration(root, plat)
    if plat == "windows":
        text = ("@echo off\r\n"
                "rem Iron autosave for %s\r\n"
                "rem Added by `iron autosave --install`; `iron autosave --uninstall` removes it.\r\n"
                "start \"\" \"%s\" \"%s\"\r\n" % (root, py.replace("%", "%%"), str(runner).replace("%", "%%")))
        try:
            text.encode("ascii")
        except UnicodeEncodeError:
            text = "@echo off\r\nchcp 65001 >nul\r\n" + text[len("@echo off\r\n"):]
        files.append((path, text))
        return files, [], "your Startup folder (%s)" % path, True
    if plat == "macos":
        args = "".join("    <string>%s</string>\n" % _xml(a) for a in (py, str(runner)))
        files.append((path, '<?xml version="1.0" encoding="UTF-8"?>\n'
                            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                            '<plist version="1.0">\n<dict>\n'
                            '  <key>Label</key>\n  <string>%s</string>\n'
                            '  <key>ProgramArguments</key>\n  <array>\n%s  </array>\n'
                            '  <key>WorkingDirectory</key>\n  <string>%s</string>\n'
                            '  <key>RunAtLoad</key>\n  <true/>\n'
                            '  <key>ProcessType</key>\n  <string>Background</string>\n'
                            '</dict>\n</plist>\n' % (_label(root), args, _xml(str(root)))))
        uid = str(os.getuid()) if hasattr(os, "getuid") else "$UID"
        return files, [["launchctl", "bootstrap", "gui/%s" % uid, str(path)]], "a launchd agent (%s)" % path, False
    if kind == "systemd":
        def q(s):
            return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        files.append((path, "[Unit]\nDescription=Iron autosave for %s\n\n"
                            "[Service]\nType=simple\nWorkingDirectory=%s\nExecStart=%s %s\nRestart=no\n\n"
                            "[Install]\nWantedBy=default.target\n" % (root, q(str(root)), q(py), q(str(runner)))))
        unit = path.name
        return files, [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", unit]], \
            "a systemd user service (%s)" % path, False

    def cq(s):
        return "'%s'" % s.replace("'", "'\\''").replace("%", "\\%")
    marker = _cron_marker(root)
    kept = [l for l in _read_crontab().splitlines() if marker not in l]
    table = "\n".join(kept + ["@reboot %s %s %s" % (cq(py), cq(str(runner)), marker)]) + "\n"
    return files, [["crontab", "-", table]], "your crontab (@reboot; no systemd user session found)", True


def plan_uninstall(root):
    plat = target_platform()
    runner = state_dir() / ("%s-runner.py" % repo_id(root))
    kind, path = _registration(root, plat)
    commands = []
    if plat == "macos":
        uid = str(os.getuid()) if hasattr(os, "getuid") else "$UID"
        commands = [["launchctl", "bootout", "gui/%s/%s" % (uid, _label(root))]]
    elif kind == "systemd":
        commands = [["systemctl", "--user", "disable", "--now", path.name], ["systemctl", "--user", "daemon-reload"]]
    elif kind == "cron":
        marker = _cron_marker(root)
        current = _read_crontab()
        if marker in current:
            kept = [l for l in current.splitlines() if marker not in l]
            commands = [["crontab", "-", "\n".join(kept) + "\n" if kept else ""]]
    files = [p for p in (path, runner) if p is not None]
    return files, commands


def installed(root):
    kind, path = _registration(root, target_platform())
    if kind == "cron":
        return _cron_marker(root) in _read_crontab(), "crontab"
    return path.exists(), str(path)


def _run(argv):
    if argv[0] == "crontab" and argv[1:2] == ["-"]:
        return subprocess.run(["crontab", "-"], input=argv[2].encode("utf-8"), capture_output=True)
    return subprocess.run(argv, capture_output=True)


def install(root, every):
    if gitutil.is_frozen():
        raise IronError("background autosave and autosync are not available in the standalone executable yet. "
                        "Run `iron autosave` in a terminal, or install Iron with the one-line installer.")
    root = Path(root)
    require_iron_remote(root)
    files, commands, where, start_now = plan_install(root, every)
    plat = target_platform()
    if dry_run():
        say("[dry run] iron autosave --install on %s would register %s" % (plat, where))
        for path, text in files:
            say("[dry run] would write %s:" % path)
            for line in text.splitlines():
                say("    " + line)
        for argv in commands:
            shown = argv[:2] if argv[0] == "crontab" else argv
            say("[dry run] would run: %s" % " ".join(shown))
            if argv[0] == "crontab":
                for line in argv[2].splitlines():
                    say("    " + line)
        if start_now:
            say("[dry run] would start it now: %s %s" % (background_python(plat), files[0][0]))
        say("[dry run] log: %s" % log_path(root))
        return 0
    for path, text in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path), "w", encoding="utf-8", newline="") as f:
            f.write(text)
    for argv in commands:
        proc = _run(argv)
        if proc.returncode and argv[:2] == ["launchctl", "bootstrap"]:
            proc = _run(["launchctl", "load", "-w", argv[2]])  # older macOS
        if proc.returncode:
            raise IronError("could not register autosave (%s): %s"
                            % (" ".join(argv[:3]), proc.stderr.decode("utf-8", errors="replace").strip()))
    if start_now and not running(root):
        kw = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
        subprocess.Popen([background_python(plat), str(files[0][0])], cwd=str(root), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **kw)
    say("%s Autosave will run in the background every %s, starting now and at every login." % (OK, describe_interval(every)))
    say("  Registered in %s" % where)
    say("  Log: %s" % log_path(root))
    say("  Check on it with `iron autosave --status`; turn it off with `iron autosave --uninstall`.")
    return 0


def uninstall(root):
    root = Path(root)
    files, commands = plan_uninstall(root)
    if dry_run():
        for argv in commands:
            say("[dry run] would run: %s" % " ".join(argv[:3]))
        for path in files:
            say("[dry run] would remove %s" % path)
        if running(root):
            say("[dry run] would ask the running autosave to stop")
        return 0
    for argv in commands:
        proc = _run(argv)
        if proc.returncode and argv[:2] == ["launchctl", "bootout"]:
            _run(["launchctl", "unload", str(files[0])])
    removed = 0
    for path in files:
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            pass
    if running(root):
        state_dir().mkdir(parents=True, exist_ok=True)
        _stop_file(root).write_text("stop\n", encoding="utf-8")
        say("%s Asked the running autosave to stop (it finishes within %d s)." % (OK, HEARTBEAT))
    say("%s Autosave no longer starts at login for %s." % (OK, root) if removed or commands
        else "Autosave was not installed for %s." % root)
    return 0


def show_status(root):
    root = Path(root)
    say("Autosave for %s" % root)
    is_installed, where = installed(root)
    say("%s Starts at login: %s" % (OK if is_installed else DOT_OFF, "yes (%s)" % where if is_installed else "no"))
    state = running(root)
    if state:
        when = int(time.time() - state.get("heartbeat", 0))
        say("%s Running: yes (process %s, every %s, checked in %d s ago; last result: %s at %s)"
            % (OK, state["pid"], describe_interval(state.get("every", 300)), when,
               state.get("last_result", "-"), state.get("last_tick", "-")))
    else:
        last = read_state(root)
        say("%s Running: no%s" % (DOT_OFF, "  (last result: %s at %s)" % (last["last_result"], last.get("last_tick"))
                                  if last.get("last_result") else ""))
    say("%s Sync after every commit: %s" % (OK if hook_installed(root) else DOT_OFF,
                                            "on" if hook_installed(root) else "off (turn on with `iron autosync on`)"))
    say("  Log: %s" % log_path(root))
    return 0


# -- iron autosave ------------------------------------------------------------

def cmd_autosave(a):
    root = gitutil.toplevel()
    if a.status:
        return show_status(root)
    if a.uninstall:
        return uninstall(root)
    every = parse_interval(a.every)
    if a.install:
        return install(root, every)
    log = Log(log_path(root), console=True, quiet=a.quiet)
    try:
        return run_loop(root, every, log, once=a.once)
    finally:
        log.close()


# -- iron autosync: the post-commit hook ----------------------------------------

def hook_path(root):
    return git_paths(root, ["hooks"])[0] / "post-commit"


def hook_block():
    py = background_python("windows" if os.name == "nt" else "posix").replace("\\", "/")

    def q(s):
        return "'%s'" % s.replace("'", "'\\''")
    return "\n".join([
        HOOK_BEGIN,
        "# Added by `iron autosync on`: publish each commit in the background. `iron autosync off` removes",
        "# only this block. It never blocks or fails the commit.",
        'if [ -z "$%s" ]; then' % SKIP_ENV,
        "  ( unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX",
        "    %s -c %s %s </dev/null >/dev/null 2>&1 & ) >/dev/null 2>&1 || true"
        % (q(py), q(RUN_HOOK), q(PACKAGE_PARENT.replace("\\", "/"))),
        "fi",
        HOOK_END,
        ""])


def _strip_block(text):
    out, skipping = [], False
    for line in text.splitlines(True):
        if line.rstrip("\r\n") == HOOK_BEGIN:
            skipping = True
            continue
        if skipping:
            if line.rstrip("\r\n") == HOOK_END:
                skipping = False
            continue
        out.append(line)
    return "".join(out)


def _write_hook(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8", newline="") as f:
        f.write(text)
    try:
        os.chmod(str(path), 0o755)
    except OSError:
        pass


def _read(path):
    with open(str(path), "r", encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read()


def hook_installed(root):
    path = hook_path(root)
    try:
        return HOOK_BEGIN in _read(path)
    except OSError:
        return False


def autosync_on(root):
    if gitutil.is_frozen():
        raise IronError("background autosave and autosync are not available in the standalone executable yet. "
                        "Run `iron autosave` in a terminal, or install Iron with the one-line installer.")
    path = hook_path(root)
    block = hook_block()
    if not path.exists():
        _write_hook(path, "#!/bin/sh\n" + block)
        return "added"
    text = _read(path)
    if HOOK_BEGIN in text:
        text = _strip_block(text)  # refresh: Python may have moved
    first, nl, rest = text.partition("\n")
    if first.startswith("#!") and not re.search(r"\b(sh|bash|dash|zsh|ksh|ash)\b", first):
        chained = path.with_name(CHAINED)
        if chained.exists():
            raise IronError("%s already exists; remove it or run `iron autosync off` first" % chained)
        os.replace(str(path), str(chained))
        _write_hook(path, "#!/bin/sh\n" + block +
                    "# The post-commit hook that was here before Iron's (`iron autosync off` puts it back):\n"
                    "exec \"$(dirname \"$0\")/%s\" \"$@\"\n" % CHAINED)
        return "chained"
    if first.startswith("#!"):
        _write_hook(path, first + nl + block + rest)
    else:
        _write_hook(path, block + text)
    return "chained"


def autosync_off(root):
    path = hook_path(root)
    chained = path.with_name(CHAINED)
    if not path.exists():
        return False
    text = _read(path)
    if HOOK_BEGIN not in text:
        return False
    if chained.exists() and CHAINED in text:
        os.replace(str(chained), str(path))
        return True
    rest = _strip_block(text)
    if rest.strip() in ("", "#!/bin/sh"):
        path.unlink()
    else:
        _write_hook(path, rest)
    return True


def cmd_autosync(a):
    root = gitutil.toplevel()
    if a.action == "on":
        require_iron_remote(root)
        how = autosync_on(root)
        say("%s Every commit is now published in the background%s." % (
            OK, " (your existing post-commit hook still runs)" if how == "chained" else ""))
        say("  Hook: %s" % hook_path(root))
        say("  Log:  %s" % log_path(root, "autosync"))
        return 0
    if a.action == "off":
        if autosync_off(root):
            say("%s Commits are no longer synced automatically." % OK)
        else:
            say("Autosync was not on for this repository.")
        return 0
    on = hook_installed(root)
    say("%s Sync after every commit: %s" % (OK if on else DOT_OFF, "on" if on else "off"))
    return 0


def hook_main():
    """Runs in the background from the post-commit hook. Never raises."""
    try:
        if os.environ.get(SKIP_ENV):
            return
        hide_child_consoles()
        root = gitutil.toplevel()
        if not iron_remotes(root):
            return
        log = Log(log_path(root, "autosync"), console=False)
        try:
            for _ in range(20):  # git may still be finishing the commit
                reason = busy_reason(root)
                if not reason:
                    break
                if "index.lock" not in reason:
                    log.info("Not syncing: %s." % reason)
                    return
                time.sleep(0.5)
            else:
                log.info("Not syncing: another git command kept running.")
                return
            lock = git_paths(root, ["iron/autosync.lock"])[0]
            pending = lock.with_name("autosync.pending")
            lock.parent.mkdir(parents=True, exist_ok=True)
            try:
                if lock.exists() and time.time() - lock.stat().st_mtime > 1800:
                    lock.unlink()  # left by a crashed sync
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
            except FileExistsError:
                pending.write_text("1", encoding="utf-8")  # the running sync picks this commit up too
                return
            try:
                while True:
                    try:
                        pending.unlink()
                    except OSError:
                        pass
                    log.info("Syncing after commit %s" % (gitutil.head_sha(root) or "")[:10])
                    if sync_once(root, log) == CONFLICT or not pending.exists():
                        break
            finally:
                lock.unlink()
        finally:
            log.close()
    except Exception:  # noqa: BLE001 - a hook must never get in the way
        pass


# -- registration with the iron command -----------------------------------------

def register(parser):
    p = parser("autosave", cmd_autosave, description="Save a version and sync automatically, every few minutes.")
    p.add_argument("--every", default="5m", help="how often: 30s, 5m, 1h (default 5m)")
    p.add_argument("--quiet", action="store_true", help="only print when something needs your attention")
    p.add_argument("--once", action="store_true", help="save and sync once, then exit")
    p.add_argument("--install", action="store_true", help="run in the background at every login")
    p.add_argument("--uninstall", action="store_true", help="stop running at login")
    p.add_argument("--status", action="store_true", help="is autosave installed and running?")
    p = parser("autosync", cmd_autosync, description="Publish every commit in the background.")
    p.add_argument("action", nargs="?", choices=["on", "off", "status"], default="status")
