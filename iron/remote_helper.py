"""git-remote-iron: the git remote helper that makes ``iron::<location>`` URLs work.

Git runs this program for any remote whose URL starts with ``iron::`` and
talks to it over stdin/stdout (see gitremote-helpers(7)). Every ordinary git
command (clone, fetch, pull, push, ls-remote) then works against cloud storage.
"""

import hashlib
import os
import sys
from pathlib import Path

from . import gitutil, keys
from .errors import IronError, PushRace, PushRejected, RemoteBusy
from .remote import RemoteRepo, now_iso
from .sync import apply_bundles, load_json, make_bundle, save_json


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def cache_dir_for(git_dir, url):
    return Path(git_dir) / "iron" / "remotes" / hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


class Helper:
    def __init__(self, remote_name, url, git_dir):
        if url.startswith("iron://"):
            url = url[len("iron://"):]
        self.remote_name = remote_name
        self.url = url
        self.git_dir = Path(git_dir).resolve()
        self.cache = cache_dir_for(self.git_dir, url)
        self.tmp = self.git_dir / "iron" / "tmp"
        self.repo = RemoteRepo(url, cache_dir=self.cache)
        self.verbosity = 1
        self.dry_run = False
        self.want_object_format = False
        self.state = None

    def log(self, msg, level=1):
        if self.verbosity >= level:
            sys.stderr.write("iron: %s\n" % msg)
            sys.stderr.flush()

    # -- protocol loop ------------------------------------------------------

    def run(self, stdin, stdout):
        def write(text):
            stdout.write(text.encode("utf-8"))
            stdout.flush()

        def batch(first):
            lines = [first]
            while True:
                line = stdin.readline().decode("utf-8").rstrip("\r\n")
                if not line:
                    return lines
                lines.append(line.partition(" ")[2])

        while True:
            raw = stdin.readline()
            if not raw:
                return
            line = raw.decode("utf-8").rstrip("\r\n")
            if not line:
                return
            cmd, _, rest = line.partition(" ")
            if cmd == "capabilities":
                write("option\nfetch\npush\nobject-format\n\n")
            elif cmd == "option":
                name, _, value = rest.partition(" ")
                write(self.option(name, value) + "\n")
            elif cmd == "list":
                write(self.list(for_push=(rest == "for-push")))
            elif cmd == "fetch":
                self.fetch([tuple(item.split(" ", 1)) for item in batch(rest)])
                write("\n")
            elif cmd == "push":
                write(self.push(batch(rest)))
            else:
                raise IronError("unsupported remote-helper command %r" % cmd)

    def option(self, name, value):
        if name == "verbosity":
            self.verbosity = int(value)
        elif name == "dry-run":
            self.dry_run = value == "true"
        elif name == "object-format":
            self.want_object_format = True
        elif name not in ("progress", "cloning", "followtags"):
            return "unsupported"
        return "ok"

    # -- list ---------------------------------------------------------------

    def list(self, for_push):
        marker = self.repo.open(required=not for_push)
        lines = []
        if self.want_object_format:
            lines.append(":object-format %s" % (marker["object_format"] if marker else gitutil.object_format()))
        if marker:
            moved = self.repo.moved_to()
            if moved:
                self.log('WARNING: this repository moved to %s; run `git remote set-url %s "iron::%s"`'
                         % (moved, self.remote_name, moved), 0)
            self.state = self.repo.load_state()
            self.warn_lost_races(self.state)
            lines += ["%s %s" % (sha, ref) for ref, sha in sorted(self.state.refs.items())]
            if self.state.head in self.state.refs:
                lines.append("@%s HEAD" % self.state.head)
        return "".join(line + "\n" for line in lines) + "\n"

    def warn_lost_races(self, state):
        """Tell the user if a push that once succeeded was later out-raced (sync-folder remotes)."""
        path = self.cache / "state.json"
        local = load_json(path, {})
        accepted, changed = set(state.accepted), False
        for push in local.get("pushes", []):
            name = push["record"]
            if push.get("warned") or name not in state.records:
                continue
            if name not in accepted:
                refs = ", ".join(push.get("refs", []))
                self.log("WARNING: your push of %s at %s lost a race with another device that pushed at the "
                         "same time. Your commits are safe on this computer (and stored in the remote). "
                         "Run `iron sync` (or git pull, then git push) to publish them again." % (refs, push["time"]), 0)
                push["warned"] = changed = True
        if changed:
            save_json(path, local)

    # -- fetch --------------------------------------------------------------

    def fetch(self, wants):
        state = self.state or self.repo.load_state()
        missing = lambda: [sha for sha, _ in wants if not gitutil.object_exists(sha)]  # noqa: E731
        if not missing():
            return
        path = self.cache / "state.json"
        local = load_json(path, {})
        applied = set(local.get("applied", []))
        if not apply_bundles(self.repo, state, self.tmp, applied, incremental=True, log=self.log) or missing():
            self.log("rebuilding from the remote's full history")
            applied = set()
            apply_bundles(self.repo, state, self.tmp, applied, incremental=False, log=self.log)
        local["applied"] = sorted(applied)
        save_json(path, local)
        still = missing()
        if still:
            raise IronError("the remote lists %s but none of its bundles contain it; run `iron verify`" % still[0])

    # -- push ---------------------------------------------------------------

    def push(self, specs):
        parsed = []
        for spec in specs:
            force = spec.startswith("+")
            src, _, dst = spec[1:].partition(":") if force else spec.partition(":")
            parsed.append((force, src, dst))
        results = {}
        # Resolve every source once, before talking to the remote or uploading anything, and use
        # those shas from here on: a branch that moves during a slow upload must not make us report
        # "ok" for a commit we never sent. Git gives a push-capable helper only the ref names
        # (`push <src>:<dst>`), never the sha it expects to be pushed, so this is as early as the
        # value can be pinned; Iron's repository lock keeps the branch still during `iron sync`.
        resolved = {}
        for _, src, _ in parsed:
            if src and src not in resolved:
                proc = gitutil.run_git(["rev-parse", "--verify", "--quiet", src], check=False)
                resolved[src] = proc.stdout.decode().strip() if proc.returncode == 0 else ""

        marker = self.repo.open(required=False)
        if marker is None and gitutil.env_flag("IRON_CREATE") and not self.dry_run:
            marker, _ = self.repo.initialize(object_format=gitutil.object_format())
            self.log("created a new Iron repository at %s" % self.repo.describe())
        if marker is None:
            # Never create one implicitly: the location may be an unmounted disk, a typo, a
            # migrated repository's old home, or a sync folder that has not finished downloading.
            msg = ("no Iron repository at %s. Create it with `iron remote add` or `iron setup` "
                   "(or push with IRON_CREATE=1)" % self.repo.describe())
            return "".join("error %s %s\n" % (dst, msg) for _, _, dst in parsed) + "\n"
        moved = self.repo.moved_to()
        if moved:
            msg = 'this repository moved to %s. Run: git remote set-url %s "iron::%s"' % (moved, self.remote_name, moved)
            return "".join("error %s %s\n" % (dst, msg) for _, _, dst in parsed) + "\n"
        if marker and marker.get("object_format", "sha1") != gitutil.object_format():
            msg = "the remote stores %s objects but this repository uses %s" % (
                marker.get("object_format"), gitutil.object_format())
            return "".join("error %s %s\n" % (dst, msg) for _, _, dst in parsed) + "\n"

        state = self.repo.load_state() if marker else None
        refs = state.refs if state else {}
        allow_force = gitutil.env_flag("IRON_ALLOW_FORCE") or gitutil.config_bool("iron.allowForcePush")
        allow_delete = gitutil.env_flag("IRON_ALLOW_DELETE") or gitutil.config_bool("iron.allowDelete")
        updates = []
        for force, src, dst in parsed:
            old = refs.get(dst)
            if not src:
                if not allow_delete:
                    results[dst] = ("deleting remote branches is turned off; confirm with "
                                    "`iron push --delete` or set IRON_ALLOW_DELETE=1")
                elif old is None:
                    results[dst] = "no such ref on the remote"
                else:
                    updates.append({"ref": dst, "old": old, "new": None})
                continue
            new = resolved.get(src)
            if not new:
                results[dst] = "src refspec %s does not match any object" % src
                continue
            if old == new:
                results[dst] = None
                continue
            if old is not None:
                if dst.startswith("refs/tags/") and not (force and allow_force):
                    results[dst] = "already exists"
                    continue
                if not gitutil.object_exists(old):
                    results[dst] = "fetch first"
                    continue
                if not gitutil.is_ancestor(old, new):
                    if not force:
                        results[dst] = "non-fast-forward"
                        continue
                    if not allow_force:
                        results[dst] = ("force-push is turned off: Iron keeps history append-only "
                                        "(allow with IRON_ALLOW_FORCE=1 or git config iron.allowForcePush true)")
                        continue
            updates.append({"ref": dst, "old": old, "new": new})

        if updates:
            error = self.publish(updates, state)
            for u in updates:
                results[u["ref"]] = error

        lines = []
        for _, _, dst in parsed:
            err = results.get(dst)
            lines.append("ok %s\n" % dst if err is None else "error %s %s\n" % (dst, err))
        return "".join(lines) + "\n"

    def publish(self, updates, state):
        """Upload one bundle and append one record covering every update (all or nothing).
        Returns None on success, otherwise the error message for all of them."""
        if self.dry_run:
            return None
        refs = state.refs if state else {}
        tips = {u["ref"]: u["new"] for u in updates if u["new"]}
        bundle = None
        if tips:
            excludes = sorted({sha for sha in refs.values() if gitutil.object_exists(sha)})
            path = make_bundle(tips, excludes, self.tmp)
            if path is not None:
                self.log("uploading %s to %s" % (_human(path.stat().st_size), self.repo.describe()))
                try:
                    bundle = self.repo.upload_bundle(path)
                finally:
                    path.unlink()

        branches = [r for r in tips if r.startswith("refs/heads/")]
        head = next((b for b in ("refs/heads/main", "refs/heads/master") if b in branches),
                    branches[0] if branches else None)

        def build(fresh):
            for u in updates:
                if fresh.refs.get(u["ref"]) != u["old"]:
                    raise PushRejected(u["ref"])
            body = {"updates": updates, "bundle": bundle}
            if head and fresh.head not in fresh.refs:
                body["head"] = head
            return body

        error = None
        try:
            name, _ = self.repo.append(build)
            if name:
                path = self.cache / "state.json"
                local = load_json(path, {})
                pushes = local.get("pushes", [])[-49:]
                pushes.append({"record": name, "refs": [u["ref"] for u in updates], "time": now_iso()})
                local["pushes"] = pushes
                applied = set(local.get("applied", []))
                if bundle:
                    applied.add(bundle)  # these objects came from here
                local["applied"] = sorted(applied)
                save_json(path, local)
        except PushRejected:
            error = "fetch first"
        except (PushRace, RemoteBusy) as e:
            error = str(e)
        return error


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if len(argv) < 3:
        sys.stderr.write("git-remote-iron is run by git. Use it like:  git clone iron::<location>\n")
        return 2
    # `git ls-remote` outside a repository runs us without GIT_DIR; listing still works.
    git_dir = os.environ.get("GIT_DIR") or str(keys.iron_home() / "no-repository")
    try:
        Helper(argv[1], argv[2], git_dir).run(sys.stdin.buffer, sys.stdout.buffer)
        return 0
    except IronError as e:
        sys.stderr.write("fatal: iron: %s\n" % e)
        return 128
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
