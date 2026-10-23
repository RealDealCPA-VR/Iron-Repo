"""The ``iron`` command.

Iron is a superset of git: the commands below are Iron's own, and anything
else (``iron log``, ``iron diff``, ``iron branch``, ``iron commit`` ...) is
passed straight to git with iron:: remotes enabled.
"""

import argparse
import binascii
import getpass
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path

from . import __version__, checkpoint, gitutil, keys, locations, maintenance
from .crypto import crypto_available
from .errors import IronError, KeyMissing
from .remote import RemoteRepo
from .remote_helper import cache_dir_for
from .storage import rclone_available
from .storage.compliance import run_compliance
from .update import cmd_update, install_hint, reinstall_hint

OK, WARN, BAD, DOT = "\u2713", "!", "\u2717", "\u25cf"

USAGE = """iron {version} - your code, your history, your storage.

Get started
  iron setup                 put this folder under version control and back it up to storage you own
  iron clone <location>      get a repository onto this computer
  iron connect <provider>    sign in to google-drive, onedrive, dropbox, box, pcloud, s3, b2, webdav directly
  iron doctor                check that everything is installed and reachable
  iron update                update Iron itself to the latest version

Everyday
  iron                       overview of this repository
  iron sync                  pull others' changes, then publish yours
  iron changes               what changed since the last commit
  iron commit -am "msg"      save a version (any git command works: log, diff, branch, ...)
  iron history               recent versions, with which agent made them
  iron ui [--port N] [--no-browser]   the same in your browser: changes, history, restore, storage
  iron autosave [--every 5m] save and sync automatically (--install: in the background at login)
  iron autosync on|off       publish every commit in the background

Safety net
  iron checkpoint [-m msg]   snapshot everything right now, without committing
  iron checkpoints           list checkpoints
  iron restore <id|last|commit>   put files back exactly as they were (saves a checkpoint first)
  iron agent [--test CMD] -- <command>   run an AI agent with automatic checkpoint, test and rollback

Storage and sovereignty
  iron remote add <name> <location> [--encrypt]   e.g. onedrive://Iron-Repos/app.iron, D:/Backup/app.iron
  iron where                 where each remote lives, and whether it is reachable
  iron verify [--deep]       prove the remote alone can rebuild every commit
  iron migrate <to>          move the repository to other storage, verified
  iron export [file]         one-file backup that plain git can restore
  iron compact               speed up future clones (nothing is deleted)
  iron key show|import       the recovery key of an encrypted remote
  iron unlock                clear a lock left by a crashed device

Locations: any folder path, onedrive://..., google-drive://..., dropbox://..., box://...,
an rclone remote (gdrive:path), s3://..., webdav://...  -  see docs/PROVIDERS.md
"""


# -- helpers ------------------------------------------------------------------

def say(msg=""):
    print(msg, flush=True)


def passthrough(args, cwd=None, env=None):
    return subprocess.call(["git"] + list(args), cwd=cwd, env=env or gitutil.helper_env())


def interactive():
    """True only when a person can answer prompts (Windows reports NUL as a tty)."""
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return False
        if os.name == "nt":
            import ctypes
            import msvcrt

            mode = ctypes.c_ulong()
            handle = msvcrt.get_osfhandle(sys.stdin.fileno())
            return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
        return True
    except (OSError, ValueError, AttributeError):
        return False


def ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        raise IronError("cancelled") from None


def confirm(question, default=False, assume_yes=False):
    if assume_yes:
        return True
    if not interactive():
        return default
    answer = ask("%s [%s] " % (question, "Y/n" if default else "y/N")).strip().lower()
    return default if not answer else answer in ("y", "yes")


def git_remotes(cwd=None):
    proc = gitutil.run_git(["config", "--get-regexp", r"^remote\..*\.url$"], cwd=cwd, check=False)
    out = {}
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        key, _, url = line.partition(" ")
        out[key[len("remote."):-len(".url")]] = url
    return out


def iron_remotes(cwd=None):
    out = {}
    for name, url in git_remotes(cwd).items():
        if url.startswith("iron::"):
            out[name] = url[len("iron::"):]
        elif url.startswith("iron://"):
            out[name] = url[len("iron://"):]
    return out


def default_remote(cwd=None):
    remotes = iron_remotes(cwd)
    branch = gitutil.current_branch(cwd)
    if branch:
        upstream = gitutil.run_git(["config", "branch.%s.remote" % branch], cwd=cwd, check=False).stdout.decode().strip()
        if upstream in remotes:
            return upstream
    if "origin" in remotes:
        return "origin"
    if remotes:
        return sorted(remotes)[0]
    raise IronError("this repository has no Iron remote yet. Run `iron setup` or `iron remote add <name> <location>`.")


def open_remote(target=None, cwd=None, key=None):
    """A RemoteRepo for a remote name in this repository, or for a location."""
    if gitutil.in_worktree(cwd) or gitutil.git_ok("rev-parse", "--git-dir", cwd=cwd):
        remotes = iron_remotes(cwd)
        name = target or default_remote(cwd)
        if name in remotes:
            git_dir = gitutil.git("rev-parse", "--absolute-git-dir", cwd=cwd)
            return RemoteRepo(remotes[name], cache_dir=cache_dir_for(git_dir, remotes[name]), key=key), name
    if not target:
        raise IronError("say which remote or location, e.g. `iron verify D:/Backups/app.iron`")
    return RemoteRepo(locations.resolve_location(target), key=key), None


def print_new_key(repo_id, key):
    say()
    say("  This repository is encrypted. Its recovery key is:")
    say()
    say("      " + keys.encode_key(key))
    say()
    say("  Put it in your password manager now. Without it nobody - not even you -")
    say("  can read the backup. (This computer keeps a copy in %s)" % keys.key_file(repo_id))
    say()


def refuse_inside_worktree(loc, cwd=None):
    if loc.startswith(("rclone:", "sim:")):
        return
    try:
        root = gitutil.toplevel(cwd).resolve()
        target = Path(loc[len("file://"):] if loc.startswith("file://") else loc).resolve()
    except (IronError, OSError):
        return
    if target == root or root in target.parents:
        raise IronError("%s is inside this project, so the backup would end up inside itself. "
                        "Choose a folder outside %s." % (loc, root))


def add_remote(name, location, encrypt=False, cwd=None, no_claims=False):
    loc = locations.resolve_location(location)
    refuse_inside_worktree(loc, cwd)
    if name in git_remotes(cwd):
        raise IronError("a remote called %r already exists (pick another name, or `git remote remove %s`)" % (name, name))
    repo = RemoteRepo(loc)
    marker, new_key = repo.initialize(object_format=gitutil.object_format(cwd), encrypt=encrypt,
                                      claims=False if no_claims else None)
    gitutil.git("remote", "add", name, "iron::" + loc, cwd=cwd)
    say("%s %s -> %s" % (OK, name, locations.describe_location(loc)))
    if new_key:
        print_new_key(marker["repo_id"], new_key)
    elif encrypt and not marker.get("encryption"):
        say("%s that location already holds an unencrypted repository; it stays unencrypted" % WARN)
    return loc


def ensure_repo(directory=None, sha256=False):
    target = Path(directory or ".").resolve()
    target.mkdir(parents=True, exist_ok=True)
    if gitutil.in_worktree(target) and gitutil.toplevel(target).resolve() == target:
        return target, False
    args = ["init", "--quiet", "-b", "main"] + (["--object-format=sha256"] if sha256 else []) + [str(target)]
    gitutil.git(*args)
    return target, True


STARTER_GITIGNORE = """# Added by `iron setup`. Files listed here are never saved or uploaded.
.env
.env.*
*.pem
*.key
id_rsa*
id_ed25519*
node_modules/
.venv/
venv/
__pycache__/
*.pyc
dist/
build/
*.log
.DS_Store
Thumbs.db
"""


def looks_secret(path):
    name = path.lower().rsplit("/", 1)[-1]
    if name.endswith((".example", ".sample", ".template", ".dist")):
        return False
    return (name.startswith((".env", "id_rsa", "id_ed25519", "id_ecdsa")) or ".env." in name or name.endswith((".pem", ".key", ".pfx", ".p12"))
            or name in ("credentials.json", "secrets.json", "service-account.json", ".netrc", ".pypirc"))


def contains_private_key(path):
    try:
        if path.stat().st_size > 65536:
            return False
        return b"PRIVATE KEY-----" in path.read_bytes()
    except OSError:
        return False


def first_version_preview(root):
    """Files the first commit would include, and the ones that look like secrets."""
    out = gitutil.run_git(["ls-files", "-z", "--others", "--cached", "--exclude-standard"], cwd=root).stdout
    files = [f for f in out.decode("utf-8", errors="replace").split("\0") if f]
    risky = [f for f in files if looks_secret(f) or contains_private_key(Path(root) / f)]
    size = 0
    for f in files:
        try:
            size += (Path(root) / f).stat().st_size
        except OSError:
            pass
    return files, risky, size


def has_changes(cwd=None):
    return bool(gitutil.git("status", "--porcelain", cwd=cwd).strip())


def commit_all(message, cwd=None, agent=None, session=None):
    gitutil.git("add", "--all", cwd=cwd)
    if not gitutil.run_git(["diff", "--cached", "--quiet"], cwd=cwd, check=False).returncode:
        return None
    lines = checkpoint.trailers(agent, session)
    text = message + ("\n\n" + "\n".join(lines) if lines else "") + "\n"
    gitutil.git("commit", "--quiet", "-F", "-", cwd=cwd, input=text, env=checkpoint.identity_env(cwd))
    return gitutil.head_sha(cwd)


# -- commands -----------------------------------------------------------------

def cmd_overview(_args):
    if not gitutil.in_worktree():
        say(USAGE.format(version=__version__))
        say("You are not inside a repository. Start with:  iron setup")
        return 0
    root = gitutil.toplevel()
    say("IRON REPOSITORY")
    say()
    say("%s   (%s)" % (root.name, root))
    say("\u2500" * 40)
    branch = gitutil.current_branch(root) or "(detached)"
    say("%s" % branch)
    remotes = iron_remotes(root)
    for name, loc in sorted(remotes.items()):
        tracking = "refs/remotes/%s/%s" % (name, branch)
        if not gitutil.git_ok("rev-parse", "--verify", "--quiet", tracking, cwd=root):
            say("%s %s: not published yet - run `iron sync`  (%s)" % (WARN, name, locations.describe_location(loc)))
            continue
        ahead, behind = gitutil.git("rev-list", "--left-right", "--count", "HEAD...%s" % tracking, cwd=root).split()
        state = OK + " Synced with" if ahead == behind == "0" else WARN + " %s to publish, %s to pull from" % (ahead, behind)
        say("%s %s  (%s, as of last sync)" % (state, name, locations.describe_location(loc)))
    if not remotes:
        say("%s No backup storage yet - run `iron setup`" % WARN)
    say()
    if gitutil.head_sha(root):
        say("Recent changes")
        say()
        for item in history(root, 5):
            agent = "  [%s]" % item["agent"] if item["agent"] else ""
            say("%s %s%s" % (DOT, item["subject"], agent))
            say("  %s" % item["when"])
            say()
    pending = len([l for l in gitutil.git("status", "--porcelain", cwd=root).splitlines() if l.strip()])
    cps = checkpoint.list_checkpoints(root)
    say("%d uncommitted change(s) - %d checkpoint(s)" % (pending, len(cps)))
    say()
    say("[ iron changes ] [ iron history ] [ iron sync ] [ iron where ]")
    return 0


def history(root, n):
    fmt = "%h%x00%an%x00%ar%x00%s%x00%(trailers:key=Iron-Agent,valueonly,separator=%x2C)%x1e"
    out = gitutil.git("log", "-n", str(n), "--format=" + fmt, cwd=root)
    items = []
    for chunk in out.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        sha, author, when, subject, agent = (chunk.split("\x00") + [""] * 5)[:5]
        items.append({"sha": sha, "author": author, "when": when, "subject": subject, "agent": agent.strip()})
    return items


def cmd_history(a):
    root = gitutil.toplevel()
    if not gitutil.head_sha(root):
        say("No versions saved yet.")
        return 0
    items = history(root, a.n)
    if a.json:
        say(json.dumps(items, indent=2))
        return 0
    for item in items:
        who = item["agent"] or item["author"]
        say("%s  %-12s %s  (%s)" % (item["sha"], who[:12], item["subject"], item["when"]))
    return 0


def cmd_changes(_a):
    root = gitutil.toplevel()
    if not has_changes(root):
        say("%s No changes since the last commit." % OK)
        return 0
    passthrough(["status", "--short"], cwd=root)
    if gitutil.head_sha(root):
        say()
        passthrough(["--no-pager", "diff", "HEAD", "--stat"], cwd=root)
    return 0


def cmd_init(a):
    target, created = ensure_repo(a.directory, sha256=a.sha256)
    say("%s %s repository in %s" % (OK, "Created" if created else "Using existing", target))
    if a.remote:
        add_remote(a.remote_name, a.remote, encrypt=a.encrypt, cwd=target, no_claims=a.no_claims)
    return 0


def cmd_remote(argv):
    p = argparse.ArgumentParser(prog="iron remote add")
    p.add_argument("name")
    p.add_argument("location")
    p.add_argument("--encrypt", action="store_true", help="encrypt everything before upload")
    p.add_argument("--no-claims", action="store_true",
                   help="never use atomic claims (for folders synced by a client Iron does not recognise)")
    a = p.parse_args(argv)
    gitutil.toplevel()
    add_remote(a.name, a.location, encrypt=a.encrypt, no_claims=a.no_claims)
    return 0


def cmd_setup(a):
    root, created = ensure_repo()
    if created:
        say("%s Started version control in %s" % (OK, root))
    name = a.name or root.name
    remotes = iron_remotes(root)
    if a.remote_name in remotes:
        say("%s Already set up: %s -> %s" % (OK, a.remote_name, locations.describe_location(remotes[a.remote_name])))
        return cmd_sync(argparse.Namespace(remote=a.remote_name))
    if a.remote_name in git_remotes(root):
        raise IronError("this repository already has a remote called %r that is not Iron storage. "
                        "Add Iron alongside it with: iron setup --remote-name backup" % a.remote_name)

    to = a.to
    prompting = interactive()
    if not to:
        dests = locations.detect_destinations()
        if not prompting:
            found = ", ".join(d.label for d in dests) or "none detected"
            raise IronError("tell Iron where to keep the repository, e.g. `iron setup --to onedrive` or "
                            "`iron setup --to D:/Backups`. Storage found on this computer: %s" % found)
        say()
        say("Where should %r live?" % name)
        for i, d in enumerate(dests, 1):
            say("  %d. %-28s %s  (%s)" % (i, d.label, d.location, d.via))
        say("  %d. Another folder (USB disk, NAS, network share ...)" % (len(dests) + 1))
        while True:
            choice = ask("Choose 1-%d: " % (len(dests) + 1)).strip()
            if choice.isdigit() and 1 <= int(choice) <= len(dests):
                to = dests[int(choice) - 1].location
                break
            if choice == str(len(dests) + 1):
                to = ask("Folder path: ").strip().strip('"')
                break
    container = locations.container_for(to, name)
    encrypt = a.encrypt or (prompting and not a.yes and confirm(
        "Encrypt everything before it leaves this computer? (you will get a recovery key)", default=False))
    if encrypt and not crypto_available():
        raise IronError("encryption needs one extra package: python -m pip install cryptography")

    if not gitutil.head_sha(root) and has_changes(root):
        if not (root / ".gitignore").exists():
            (root / ".gitignore").write_text(STARTER_GITIGNORE, encoding="utf-8")
            say("%s Added a starter .gitignore (secrets, dependencies and build output stay out)" % OK)
        files, risky, size = first_version_preview(root)
        say("The first version will contain %d file(s), %.1f MB%s."
            % (len(files), size / 1048576.0, ", encrypted" if encrypt else ", NOT encrypted"))
        if risky:
            say("%s These look like secrets: %s" % (WARN, ", ".join(risky[:6]) + (" ..." if len(risky) > 6 else "")))
            say("  Add them to .gitignore to keep them out of the history.")
            if not a.allow_secrets and not confirm("Save them anyway?", default=False):
                raise IronError("stopped so you can review those files. Edit .gitignore and run `iron setup` "
                                "again, or pass --allow-secrets.")
        if confirm("Save your current files as the first version?", default=True, assume_yes=a.yes):
            commit_all("First version", cwd=root)
            say("%s Saved the first version" % OK)
    add_remote(a.remote_name, container, encrypt=encrypt, cwd=root, no_claims=a.no_claims)
    if gitutil.head_sha(root):
        say("Publishing...")
        rc = passthrough(["push", "--quiet", "-u", a.remote_name, "--all"], cwd=root)
        if rc:
            return rc
        if gitutil.git("tag", "--list", cwd=root):
            passthrough(["push", "--quiet", a.remote_name, "--tags"], cwd=root)
    say()
    say("%s Done. Your repository lives in %s" % (OK, locations.describe_location(container)))
    say()
    say("  On any other computer:   iron clone \"%s\"" % container)
    say("  Save your work:          iron commit -am \"what changed\"   then   iron sync")
    say("  Prove it is recoverable: iron verify --deep")
    return 0


def cmd_clone(a):
    loc = locations.resolve_location(a.location)
    key = keys.decode_key(a.key) if a.key else None
    try:
        marker = RemoteRepo(loc, key=key).open()
    except KeyMissing:
        if key is not None or not interactive():
            raise
        key = keys.decode_key(getpass.getpass("This repository is encrypted. Recovery key: "))
        marker = RemoteRepo(loc, key=key).open()
    if key is not None:
        keys.save_key(marker["repo_id"], key)
    directory = a.directory or loc.replace(":", "/").rstrip("/").rsplit("/", 1)[-1]
    if directory.endswith(".iron"):
        directory = directory[:-len(".iron")]
    rc = passthrough(["clone", "iron::" + loc, directory])
    if rc == 0:
        say("%s Cloned into %s. Work there, then `iron sync` to publish." % (OK, directory))
    return rc


def cmd_sync(a):
    root = gitutil.toplevel()
    remote = a.remote or default_remote(root)
    branch = gitutil.current_branch(root)
    if not branch:
        raise IronError("you are not on a branch (detached HEAD); switch to one first, e.g. `iron switch main`")
    if not gitutil.head_sha(root):
        say("Nothing to publish yet: save a first version with `iron commit -am \"First version\"`.")
        return 0
    rc = passthrough(["fetch", "--quiet", remote], cwd=root)
    if rc:
        return rc
    tracking = "refs/remotes/%s/%s" % (remote, branch)
    if gitutil.git_ok("rev-parse", "--verify", "--quiet", tracking, cwd=root):
        ahead, behind = (int(x) for x in gitutil.git("rev-list", "--left-right", "--count",
                                                      "HEAD...%s" % tracking, cwd=root).split())
        if behind and not ahead:
            rc = passthrough(["merge", "--quiet", "--ff-only", tracking], cwd=root)
            if rc:
                say("%s Could not fast-forward: commit or checkpoint your local edits first." % BAD)
                return rc
            say("%s Pulled %d new version(s)" % (OK, behind))
        elif behind and ahead:
            if gitutil.run_git(["diff", "--quiet", "HEAD"], cwd=root, check=False).returncode:
                raise IronError("you have uncommitted changes. Save them first with `iron commit -am \"...\"`, "
                                "then run `iron sync` again.")
            cp = checkpoint.create("before syncing with %s" % remote, cwd=root)
            say("Combining %d of your version(s) with %d from %s (checkpoint %s saved)..." % (ahead, behind, remote, cp.id))
            rc = passthrough(["merge", "--no-edit", tracking], cwd=root, env=dict(gitutil.helper_env(),
                                                                               **checkpoint.identity_env(root)))
            if rc:
                say()
                say("%s Some files were changed on both sides. Edit the files marked above," % BAD)
                say("  then `iron commit -am \"Combine changes\"` and `iron sync` again.")
                say("  To back out instead: git merge --abort")
                return rc
        if not ahead and not behind:
            say("%s Already in sync with %s" % (OK, remote))
            return 0
        if not ahead:
            return 0
    rc = passthrough(["push", "--quiet", "-u", remote, branch], cwd=root)
    if rc == 0:
        say("%s Published %s to %s" % (OK, branch, remote))
    return rc


def cmd_push(argv):
    env = gitutil.helper_env()
    args = [x for x in argv if x != "--yes"]
    if any(x in ("--delete", "-d") for x in args) or any(x.startswith(":") for x in args):
        if not confirm("Delete branch(es) on the remote? (their history stays in the remote's log)",
                       default=False, assume_yes="--yes" in argv):
            say("Nothing deleted. (Add --yes to confirm without a prompt.)")
            return 1
        env["IRON_ALLOW_DELETE"] = "1"
    if any(x in ("-f", "--force") or x.startswith("--force-with-lease") or x.startswith("+") for x in args):
        say("%s Iron refuses force-push unless `git config iron.allowForcePush true` is set; history is append-only." % WARN)
    return passthrough(["push"] + args, env=env)


def cmd_merge(argv):
    if gitutil.in_worktree() and gitutil.head_sha() and not any(x in ("--abort", "--continue", "--quit") for x in argv):
        cp = checkpoint.create("before merge %s" % " ".join(argv))
        say("checkpoint %s saved (undo with `iron restore %s`)" % (cp.id, cp.id))
    return passthrough(["merge"] + argv)


def cmd_checkpoint(a):
    cp = checkpoint.create(a.message, agent=a.agent, session=a.session)
    if a.push:
        remote = default_remote()
        passthrough(["push", "--quiet", remote, "%s%s" % (checkpoint.NAMESPACE, cp.id)])
    if a.json:
        say(json.dumps(cp.as_dict()))
    else:
        say("%s checkpoint %s saved. Restore with: iron restore %s" % (OK, cp.id, cp.id))
    return 0


def cmd_checkpoints(a):
    cps = checkpoint.list_checkpoints()
    if a.json:
        say(json.dumps([c.as_dict() for c in cps], indent=2))
        return 0
    if not cps:
        say("No checkpoints yet. Make one with `iron checkpoint -m \"before big change\"`.")
    for c in cps[: a.n]:
        say("%s  %s  %s" % (c.id, c.date, c.message))
    return 0


def cmd_restore(argv):
    positional = [x for x in argv if not x.startswith("-")]
    if (len(positional) != 1 or any(x not in ("--json", "--yes") for x in argv if x.startswith("-"))
            or (Path(positional[0]).exists() and positional[0] not in ("last", "latest"))):
        return passthrough(["restore"] + argv)  # git's own file-level `restore <file>`
    safety, sha = checkpoint.restore(positional[0])
    if "--json" in argv:
        say(json.dumps({"restored": sha, "safety_checkpoint": safety.id}))
    else:
        say("%s Files restored to %s." % (OK, positional[0]))
        say("  What was there before is saved as checkpoint %s (iron restore %s to undo)." % (safety.id, safety.id))
    return 0


def cmd_agent(a):
    if not a.command:
        raise IronError("give the agent command after --, e.g. iron agent --test \"pytest -q\" -- claude -p \"fix the bug\"")
    root = gitutil.toplevel()
    agent = a.name or os.environ.get("IRON_AGENT") or Path(a.command[0]).stem
    session = a.session or os.environ.get("IRON_SESSION") or uuid.uuid4().hex[:8]
    cp = checkpoint.create("before %s task" % agent, agent=agent, session=session, cwd=root)
    start_branch, start_head = gitutil.current_branch(root), gitutil.head_sha(root)
    say("iron: checkpoint %s saved before %s (session %s)" % (cp.id, agent, session))
    env = {k: v for k, v in os.environ.items() if k != "IRON_KEY"}  # agents never see the key
    env.update(IRON_AGENT=agent, IRON_SESSION=session, IRON_CHECKPOINT=cp.id)
    command = [shutil.which(a.command[0]) or a.command[0]] + a.command[1:]
    code = subprocess.call(command, cwd=root, env=env)
    failed = code != 0
    if a.test and not failed:
        say("iron: validating with: %s" % a.test)
        failed = subprocess.call(a.test, cwd=root, env=env, shell=True) != 0
    if failed:
        if a.keep:
            say("%s the task failed; changes kept (undo with `iron restore %s`)" % (BAD, cp.id))
        else:
            safety, _ = checkpoint.restore(cp.id, cwd=root, agent=agent, session=session)
            if start_branch:
                gitutil.git("symbolic-ref", "HEAD", "refs/heads/" + start_branch, cwd=root)
            if start_head:
                gitutil.git("reset", "--quiet", start_head, cwd=root)  # undo any commits the agent made
            say("%s the task failed, so the project was rolled back to checkpoint %s." % (BAD, cp.id))
            say("  The agent's attempt is kept as checkpoint %s if you want to look at it." % safety.id)
        return code or 1
    if a.no_commit or not has_changes(root):
        say("%s task finished%s" % (OK, "" if has_changes(root) else " (no changes)"))
        return 0
    sha = commit_all(a.message or "%s: task %s" % (agent, session), cwd=root, agent=agent, session=session)
    say("%s committed %s as %s" % (OK, sha[:10], agent))
    if a.push:
        return cmd_sync(argparse.Namespace(remote=None))
    return 0


def cmd_where(_a):
    remotes = iron_remotes()
    if not remotes:
        say("No Iron remotes. Add one with `iron setup` or `iron remote add <name> <location>`.")
        return 0
    for name, loc in sorted(remotes.items()):
        ok, detail = RemoteRepo(loc).store.health_check()
        say("%s %-10s %s  (%s)" % (OK if ok else BAD, name, locations.describe_location(loc), detail))
    return 0


def print_report(report):
    for p in report["problems"]:
        say("%s %s" % (BAD, p))
    for w in report["warnings"]:
        say("%s %s" % (WARN, w))
    for i in report["info"]:
        say("  %s" % i)


def cmd_verify(a):
    repo, _ = open_remote(a.target)
    report = maintenance.verify(repo, deep=a.deep, log=say)
    if a.json:
        say(json.dumps(report, indent=2))
    else:
        say("Verifying %s" % repo.describe())
        print_report(report)
        say("%s %s" % ((OK, "Remote is healthy.") if report["ok"] else (BAD, "Problems found.")))
        if report["ok"] and not a.deep:
            say("  Run `iron verify --deep` to rebuild everything from the remote alone.")
    return 0 if report["ok"] else 1


def cmd_migrate(a):
    root = gitutil.toplevel()
    remote = a.remote or default_remote(root)
    src, _ = open_remote(remote, cwd=root)
    name = src.location.replace(":", "/").rstrip("/").rsplit("/", 1)[-1]
    name = name[:-len(".iron")] if name.endswith(".iron") else name
    dst_loc = locations.container_for(a.to, name)
    refuse_inside_worktree(dst_loc, root)
    say("Migrating %s -> %s" % (src.describe(), locations.describe_location(dst_loc)))
    result = maintenance.migrate(src, dst_loc, log=lambda m: say("  " + m))
    say("%s Copied and verified %d bundle(s), %d log entries (%d KB)."
        % (OK, result["bundles"], result["records"], result["bytes"] // 1024))
    if not a.keep_url:
        gitutil.git("remote", "set-url", remote, "iron::" + dst_loc, cwd=root)
        say("%s %s now points to the new location. The old copy was left untouched;" % (OK, remote))
        say("  delete it yourself once you are happy.")
    return 0


def cmd_compact(a):
    root = gitutil.toplevel()
    repo, _ = open_remote(a.remote, cwd=root)
    result = maintenance.compact(repo, gitutil.git("rev-parse", "--absolute-git-dir", cwd=root), cwd=str(root), log=say)
    say("%s Already compact." % OK if result is None else "%s Snapshot stored; new clones download one file." % OK)
    return 0


def cmd_export(a):
    root = gitutil.toplevel()
    path = Path(a.file or "%s-%s.bundle" % (root.name, date.today().isoformat())).resolve()
    gitutil.git("bundle", "create", "--quiet", str(path), "--all", cwd=root)
    gitutil.git("bundle", "verify", "--quiet", str(path), cwd=root)
    say("%s Wrote %s (%d KB): every branch, tag and checkpoint." % (OK, path, path.stat().st_size // 1024))
    say("  Restore it anywhere, without Iron:  git clone \"%s\" restored" % path)
    return 0


def cmd_key(a):
    if a.action == "show":
        repo, _ = open_remote(a.target)
        marker = repo.open()
        if not marker.get("encryption"):
            say("This repository is not encrypted.")
            return 0
        say(keys.encode_key(keys.load_key(marker["repo_id"])))
        return 0
    if a.action == "path":
        say(str(keys.iron_home() / "keys"))
        return 0
    if not a.key:
        raise IronError("usage: iron key import <recovery key> [remote or location]")
    key = keys.decode_key(a.key)
    repo, _ = open_remote(a.target, key=key)
    marker = repo.open()
    path = keys.save_key(marker["repo_id"], key)
    say("%s Key saved to %s" % (OK, path))
    return 0


def cmd_unlock(a):
    repo, _ = open_remote(a.remote)
    live = repo.live_locks()
    if live and not confirm("%s is pushing right now. Remove its lock anyway?" % repo.lock_holder(live[0]),
                            default=False, assume_yes=a.yes):
        return 1
    say("%s removed %d lock file(s)" % (OK, repo.unlock()))
    return 0


def cmd_doctor(a):
    good = True

    def line(ok, text, optional=False):
        nonlocal good
        good = good and (ok or optional)
        say("%s %s" % (OK if ok else ("o" if optional else BAD), text))

    line(sys.version_info >= (3, 8), "Python %s" % sys.version.split()[0])
    try:
        v = gitutil.git_version()
        line(v >= gitutil.MIN_GIT, "git %s (needs %d.%d or newer)" % (".".join(map(str, v)), *gitutil.MIN_GIT)
             + ("" if v >= gitutil.MIN_GIT else ". Update it: %s" % (install_hint("git") or "https://git-scm.com/downloads")))
    except (IronError, OSError):
        line(False, "git is not installed. Install it: %s" % (install_hint("git") or "https://git-scm.com/downloads"))
    helper = gitutil.helper_on_path()
    if helper:
        line(True, "git-remote-iron on PATH: %s" % helper)
    elif gitutil.helper_dirs():
        line(False, "git-remote-iron is in %s but not on PATH. `iron` works anyway; add that folder to "
             "PATH so plain `git` can use iron:: remotes too" % gitutil.helper_dirs()[0], optional=True)
    else:
        line(False, "git-remote-iron not found. Reinstall Iron:  %s" % reinstall_hint())
    line(crypto_available(), "encryption support" + ("" if crypto_available() else
         " (optional: python -m pip install cryptography)"), optional=True)
    line(rclone_available(), "rclone" + ("" if rclone_available() else
         " (optional, for direct cloud APIs: %s)" % (install_hint("rclone") or "https://rclone.org/install/")), optional=True)
    dests = locations.detect_destinations()
    say("Storage found on this computer: %s" % (", ".join("%s (%s)" % (d.label, d.location) for d in dests) or "none"))
    if gitutil.in_worktree():
        for name, loc in sorted(iron_remotes().items()):
            repo = RemoteRepo(loc)
            ok, detail = repo.store.health_check()
            line(ok, "remote %s: %s - %s" % (name, locations.describe_location(loc), detail))
            if ok and a.deep:
                for check, passed, info in run_compliance(repo.provider):
                    line(passed, "    %s%s" % (check, (": " + info) if info else ""))
    say()
    say("%s Everything needed is in place." % OK if good else "%s Fix the items marked %s above." % (BAD, BAD))
    return 0 if good else 1


def cmd_ui(a):
    from .ui import serve
    return serve(port=a.port, open_browser=not a.no_browser)


# -- dispatch -----------------------------------------------------------------

def build_parsers():
    parsers = {}

    def parser(name, func, **kw):
        p = argparse.ArgumentParser(prog="iron " + name, **kw)
        p.set_defaults(func=func)
        parsers[name] = p
        return p

    p = parser("setup", cmd_setup, description="Version this folder and back it up to storage you own.")
    p.add_argument("--to", help="onedrive, google-drive, dropbox, box, an rclone remote, or a folder")
    p.add_argument("--name", help="repository name (default: this folder's name)")
    p.add_argument("--encrypt", action="store_true")
    p.add_argument("--remote-name", default="origin")
    p.add_argument("--yes", "-y", action="store_true", help="accept defaults without asking")
    p.add_argument("--allow-secrets", action="store_true", help="save files that look like secrets anyway")
    p.add_argument("--no-claims", action="store_true", help="the folder is synced by a client Iron may not detect")

    from .connect import add_arguments, cmd_connect
    add_arguments(parser("connect", cmd_connect, description="Connect cloud storage directly through rclone."))

    p = parser("init", cmd_init)
    p.add_argument("directory", nargs="?")
    p.add_argument("--remote", help="also add an Iron remote at this location")
    p.add_argument("--remote-name", default="origin")
    p.add_argument("--encrypt", action="store_true")
    p.add_argument("--sha256", action="store_true", help="use SHA-256 object ids (not accepted by GitHub)")
    p.add_argument("--no-claims", action="store_true")

    p = parser("clone", cmd_clone)
    p.add_argument("location")
    p.add_argument("directory", nargs="?")
    p.add_argument("--key", help="recovery key for an encrypted repository")

    p = parser("sync", cmd_sync)
    p.add_argument("remote", nargs="?")

    p = parser("checkpoint", cmd_checkpoint)
    p.add_argument("-m", "--message", default="")
    p.add_argument("--agent")
    p.add_argument("--session")
    p.add_argument("--push", action="store_true", help="also store the checkpoint in the remote")
    p.add_argument("--json", action="store_true")

    p = parser("checkpoints", cmd_checkpoints)
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--json", action="store_true")

    p = parser("history", cmd_history)
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--json", action="store_true")

    parser("changes", cmd_changes)
    parser("where", cmd_where)

    p = parser("agent", cmd_agent, description="Run an agent with checkpoint, validation and rollback.")
    p.add_argument("--name", help="agent name recorded in history (default: the command name)")
    p.add_argument("--session")
    p.add_argument("--test", help="validation command; failure rolls the project back")
    p.add_argument("--message", "-m")
    p.add_argument("--keep", action="store_true", help="keep changes even if the task fails")
    p.add_argument("--no-commit", action="store_true")
    p.add_argument("--push", action="store_true", help="sync after committing")
    p.add_argument("command", nargs=argparse.REMAINDER)

    p = parser("verify", cmd_verify)
    p.add_argument("target", nargs="?", help="remote name or location")
    p.add_argument("--deep", action="store_true", help="rebuild everything from the remote alone")
    p.add_argument("--json", action="store_true")

    p = parser("migrate", cmd_migrate)
    p.add_argument("to", help="destination: onedrive, a folder, an rclone remote ...")
    p.add_argument("--remote")
    p.add_argument("--keep-url", action="store_true", help="copy only; keep using the old location")

    p = parser("compact", cmd_compact)
    p.add_argument("remote", nargs="?")

    p = parser("export", cmd_export)
    p.add_argument("file", nargs="?")

    p = parser("key", cmd_key)
    p.add_argument("action", choices=["show", "import", "path"])
    p.add_argument("key", nargs="?")
    p.add_argument("target", nargs="?")

    p = parser("unlock", cmd_unlock)
    p.add_argument("remote", nargs="?")
    p.add_argument("--yes", "-y", action="store_true")

    parser("update", cmd_update, description="Update Iron itself: git pull, or a verified fresh download.")

    p = parser("ui", cmd_ui, description="A dashboard in your browser, on this computer only.")
    p.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free one)")
    p.add_argument("--no-browser", action="store_true", help="print the link instead of opening a browser")

    p = parser("doctor", cmd_doctor)
    p.add_argument("--deep", action="store_true", help="run the provider compliance suite on each remote")

    from . import autosave  # imported here: it uses this module's helpers
    autosave.register(parser)
    return parsers


def _fix_streams():
    for stream in (sys.stdout, sys.stderr):
        try:
            if (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(errors="replace") if stream.isatty() else stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    _fix_streams()
    try:
        if not argv or argv[0] == "overview":
            return cmd_overview(None)
        cmd, rest = argv[0], argv[1:]
        if cmd in ("-h", "--help", "help"):
            say(USAGE.format(version=__version__))
            return 0
        if cmd in ("--version", "version"):
            say("iron %s" % __version__)
            return 0
        if cmd == "remote" and rest[:1] == ["add"]:
            return cmd_remote(rest[1:])
        if cmd == "push":
            return cmd_push(rest)
        if cmd == "merge":
            return cmd_merge(rest)
        if cmd == "restore":
            return cmd_restore(rest)
        if cmd == "key" and rest[:1] == ["import"] and len(rest) >= 2:
            rest = ["import", rest[1]] + rest[2:]
        parsers = build_parsers()
        if cmd in parsers:
            if cmd == "agent" and "--" in rest:
                i = rest.index("--")
                a = parsers[cmd].parse_args(rest[:i])
                a.command = rest[i + 1:]
            else:
                a = parsers[cmd].parse_args(rest)
            return a.func(a)
        return passthrough(argv)
    except KeyMissing as e:
        print("iron: %s" % e, file=sys.stderr)
        return 3
    except (ValueError, binascii.Error) as e:
        if "key" not in str(e).lower() and not isinstance(e, binascii.Error):
            raise
        print("iron: that does not look like an Iron recovery key (it starts with iron1-)", file=sys.stderr)
        return 1
    except IronError as e:
        print("iron: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
