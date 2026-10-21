"""Verification, migration and compaction: the sovereignty features (PRD sections 18 and 21)."""

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

from . import gitutil, recover
from .errors import IntegrityError, IronError, PushRejected
from .remote import RemoteRepo, now_iso
from .remote_helper import cache_dir_for
from .storage import AlreadyExists, NotFound
from .sync import apply_bundles, load_json, make_bundle, save_json


def rmtree(path):
    """shutil.rmtree that also removes git's read-only pack files on Windows."""
    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=onerror)
    else:
        shutil.rmtree(path, onerror=onerror)


class TempDir:
    def __enter__(self):
        self.path = Path(tempfile.mkdtemp(prefix="iron-"))
        return self.path

    def __exit__(self, *exc):
        try:
            rmtree(self.path)
        except OSError:
            pass


def verify(repo, deep=False, log=lambda m: None):
    """Check a remote end to end. With ``deep`` it rebuilds the whole repository
    from the remote alone, in a scratch folder, and runs ``git fsck`` on it."""
    marker = repo.open()
    state = repo.load_state()
    problems, warnings, info = [], [], []
    for name in state.corrupt:
        problems.append("log record %s is unreadable or fails its checksum" % name)
    listed = {n[:-len(".bundle")] for n in repo.store.list("bundles") if recover.BUNDLE_RE.match(n)}
    needed = [state.records[n]["bundle"] for n in state.accepted if state.records[n].get("bundle")]
    for digest in needed:
        if digest not in listed:
            problems.append("bundle %s, needed by the history, is missing" % digest[:12])
    for name in state.rejected:
        problems.append("push %s is confirmed but not part of the history: two devices pushed at the same "
                        "moment through a sync folder, or log files were lost. Its commits are still stored "
                        "in the remote; the device that made it should `iron sync` again" % name)
    referenced = {r.get("bundle") for r in state.records.values() if r.get("bundle")}
    orphans = listed - referenced
    if orphans:
        info.append("%d bundle(s) left by interrupted pushes (harmless)" % len(orphans))
    if state.pending:
        info.append("%d push attempt(s) never confirmed (a detected race or an interrupted push); "
                    "they were never applied" % len(state.pending))
    newest = max(state.records, default=None)
    if newest and newest not in state.accepted and newest not in state.withdrawn and state.records[newest].get("seq") == state.seq + 1:
        info.append("the newest push record %s is unconfirmed: a push in progress, an interrupted push, "
                    "or a lost .ok file" % newest)
    if state.unsettled():
        warnings.append("%d record(s) are halfway through being retired; `iron unlock` finishes that"
                        % len(state.unsettled()))
    if repo.live_locks():
        info.append("a push is in progress (or a lock is about to expire)")
    info.append("%d ref(s), %d push record(s), encryption %s, object format %s" % (
        len(state.refs), len(state.accepted), "on" if marker.get("encryption") else "off",
        marker.get("object_format", "sha1")))

    commits = None
    if deep and not problems:
        log("rebuilding the repository from %s alone..." % repo.describe())
        with TempDir() as tmp:
            bare = tmp / "repo.git"
            gitutil.git("init", "--quiet", "--bare", "--object-format=" + marker.get("object_format", "sha1"), str(bare))
            try:
                apply_bundles(repo, state, tmp / "work", set(), incremental=False, cwd=str(bare))
                for ref, sha in state.refs.items():
                    gitutil.git("update-ref", ref, sha, cwd=str(bare))
                fsck = gitutil.run_git(["fsck", "--full", "--no-dangling"], cwd=str(bare), check=False)
                if fsck.returncode != 0:
                    problems.append("git fsck failed on the rebuilt repository: %s"
                                    % fsck.stderr.decode("utf-8", errors="replace").strip()[-500:])
                elif state.refs:
                    commits = int(gitutil.git("rev-list", "--all", "--count", cwd=str(bare)))
                    info.append("full restore test passed: %d commit(s) rebuilt from the remote alone" % commits)
            except (IronError, NotFound) as e:
                problems.append("full restore test failed: %s" % e)
    return {"ok": not problems, "problems": problems, "warnings": warnings, "info": info,
            "refs": state.refs, "pushes": len(state.accepted), "commits": commits}


def migrate(src, dst_location, log=lambda m: None):
    """Copy a remote to new storage, verifying every file on both sides (PRD section 18).

    Files are copied as stored, so encrypted remotes migrate without decrypting.
    The source is never modified. Safe to re-run after an interruption.
    """
    marker = src.open()
    dst = RemoteRepo(dst_location, key=getattr(src.codec, "key", None))
    if src.moved_to():
        raise IronError("this repository already moved to %s; migrate from there" % src.moved_to())
    existing = dst.open(required=False)
    if existing and existing.get("repo_id") != marker["repo_id"]:
        raise IronError("%s already holds a different Iron repository" % dst.describe())
    src_state = src.load_state()

    def copy(path, check=None):
        data = src.store.read(path)
        if check and not check(data):
            raise IntegrityError("%s is corrupted on the source; run `iron verify` there first" % path)
        try:
            if dst.store.read(path) == data:
                return 0
        except NotFound:
            pass
        dst.store.write(path, data)
        if dst.store.read(path) != data:
            raise IntegrityError("%s did not arrive intact at %s" % (path, dst.describe()))
        return len(data)

    total = 0
    bundles = [n for n in src.store.list("bundles") if recover.BUNDLE_RE.match(n)]
    log("copying %d bundle(s)..." % len(bundles))
    for name in bundles:
        digest = name[:-len(".bundle")]
        total += copy("bundles/" + name, lambda d, h=digest: recover.bundle_digest(d) == h)
    entries = [n for n in src.store.list("log")
               if recover.RECORD_RE.match(n) or recover.CONFIRM_RE.match(n) or recover.WITHDRAWN_RE.match(n)
               or recover.CLAIM_RE.match(n) or n.endswith((".abandon", ".kept"))]
    log("copying %d log entries..." % len(entries))
    for name in sorted(entries, key=lambda n: (not n.endswith(".json"), n)):
        m = recover.RECORD_RE.match(name)
        total += copy("log/" + name, (lambda d, h=m.group(2): recover.record_digest(d) == h) if m else None)
    try:
        total += copy("README.txt")
    except NotFound:
        pass
    # Always ship this version's recovery tool: the source's copy may predate the
    # destination's format (claims mode) and would silently rebuild stale history.
    dst.store.write("iron-recover.py", Path(recover.__file__).read_bytes())
    # The destination may or may not offer atomic claims: re-express the accepted
    # history in the destination's own commit style before writing its marker.
    dst_claims = dst.store.supports_exclusive()
    for name in src_state.accepted:
        seq = src_state.records[name]["seq"]
        if dst_claims:
            try:
                dst.store.create_exclusive("log/%010d.claim" % seq, json.dumps({"record": name}).encode())
            except AlreadyExists:
                pass
        else:
            dst.store.write("log/" + name[:-len(".json")] + ".ok", json.dumps({"record": name}).encode())
    dst.store.write(recover.MARKER, json.dumps(dict(marker, claims=dst_claims, version=recover.FORMAT_VERSION),
                                               indent=2).encode())
    dst.open()
    dst_state = dst.load_state()
    if dst_state.refs != src_state.refs or dst_state.tip != src_state.tip:
        raise IntegrityError("the copy at %s does not replay to the same history" % dst.describe())
    # Other computers still point at the old location: pushes there are now refused
    # with directions to the new one, so the two copies cannot drift apart.
    src.store.write("MOVED.json", json.dumps({"location": dst_location, "time": now_iso()}).encode())
    return {"bytes": total, "bundles": len(bundles), "records": len(entries), "refs": dst_state.refs}


def compact(repo, git_dir, cwd=None, log=lambda m: None):
    """Store one snapshot bundle of the whole history so new clones download a
    single file instead of every push. Nothing is deleted."""
    state = repo.load_state()
    if not state.refs:
        raise IronError("the remote is empty; nothing to compact")
    if state.snapshot and state.snapshot == state.tip:
        return None
    cache = cache_dir_for(git_dir, repo.location)
    local = load_json(cache / "state.json", {})
    applied = set(local.get("applied", []))
    tmp = Path(git_dir) / "iron" / "tmp"
    if not apply_bundles(repo, state, tmp, applied, incremental=True, cwd=cwd, log=log):
        applied = set()
        apply_bundles(repo, state, tmp, applied, incremental=False, cwd=cwd, log=log)
    missing = [sha for sha in state.refs.values() if not gitutil.object_exists(sha, cwd=cwd)]
    if missing:
        raise IronError("cannot compact: object %s is not available locally" % missing[0])
    head = state.head if state.head in state.refs else next(
        (r for r in sorted(state.refs) if r.startswith("refs/heads/")), None)
    path = make_bundle(state.refs, [], tmp, cwd=cwd, head=head)
    size = path.stat().st_size
    log("uploading a %d KB snapshot of %d ref(s)..." % (size // 1024, len(state.refs)))
    try:
        digest = repo.upload_bundle(path)
    finally:
        path.unlink()

    def build(fresh):
        if fresh.tip != state.tip:
            raise PushRejected("the remote changed while compacting; run it again")
        return {"kind": "snapshot", "updates": [], "bundle": digest}

    name, _ = repo.append(build)
    applied.add(digest)
    local["applied"] = sorted(applied)
    save_json(cache / "state.json", local)
    return {"record": name, "bytes": size}
