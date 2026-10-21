"""Moving git objects between a local repository and an Iron remote (PRD section 9).

Push uploads one bundle holding only the objects the remote does not have yet
(everything reachable from the new tips, minus everything reachable from the
remote's current refs). Fetch applies only the bundles this repository has not
applied before, so neither direction ever lists or copies the whole remote.
"""

import json
import os
import shutil
import uuid
from pathlib import Path

from . import gitutil
from .errors import IntegrityError


def make_bundle(tips, excludes, workdir, cwd=None, head=None):
    """Bundle the objects reachable from ``tips`` ({ref: sha}) but not from ``excludes``.

    The bundle is built in a throwaway repository that borrows this one's
    objects (git alternates), so it lists the real ref names: a snapshot can be
    restored with nothing but ``git clone <file>.bundle``. Returns the bundle
    path, or None when the remote already has every object.
    """
    token = uuid.uuid4().hex[:10]
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    objects = Path(gitutil.git("rev-parse", "--git-path", "objects", cwd=cwd))
    if not objects.is_absolute():
        objects = Path(cwd or os.getcwd()) / objects
    scratch = work / ("stage-" + token)
    path = work / ("push-%s.bundle" % token)
    fmt = gitutil.object_format(cwd)
    # The remote helper runs with GIT_DIR and friends pointing at the real repository.
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_COMMON_DIR")}
    try:
        gitutil.git("init", "--quiet", "--bare", "--object-format=" + fmt, str(scratch), env=env)
        (scratch / "objects" / "info" / "alternates").write_bytes((objects.resolve().as_posix() + "\n").encode())
        for ref, sha in tips.items():
            gitutil.git("update-ref", ref, sha, cwd=str(scratch), env=env)
        names = list(tips)
        if head in tips:
            gitutil.git("symbolic-ref", "HEAD", head, cwd=str(scratch), env=env)
            names.append("HEAD")
        revs = "".join(r + "\n" for r in names) + "".join("^%s\n" % x for x in excludes)
        proc = gitutil.run_git(["bundle", "create", "--quiet", str(path), "--stdin"], cwd=str(scratch),
                               input=revs, env=env, check=False)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace")
            if "empty bundle" in err:
                return None
            raise gitutil.GitError(["bundle", "create"], proc.returncode, err)
        return path
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def pending_bundles(state, applied):
    """Bundle digests to apply, in order, given the set already applied locally."""
    order = {n: i for i, n in enumerate(state.accepted)}
    chain = [(n, state.records[n]["bundle"]) for n in state.accepted if state.records[n].get("bundle")]
    if not state.snapshot:
        return [b for _, b in chain if b not in applied]
    si = order[state.snapshot]
    snap = state.records[state.snapshot].get("bundle")
    before = [b for n, b in chain if order[n] < si]
    after = [b for n, b in chain if order[n] > si and b not in applied]
    if snap in applied or (before and all(b in applied for b in before)):
        return after
    return [snap] + after


def apply_bundles(repo, state, workdir, applied, incremental=True, cwd=None, log=None):
    """Unbundle what is missing into the repository at ``cwd`` (or $GIT_DIR).

    Adds each applied digest to ``applied``. In incremental mode returns False
    when a bundle's prerequisites are missing locally (for example after
    ``git gc`` pruned unreachable objects); the caller then retries with
    incremental=False, which rebuilds from the latest snapshot.
    """
    pending = pending_bundles(state, applied if incremental else set())
    if pending and log:
        log("downloading %d bundle(s) from %s" % (len(pending), repo.describe()))
    Path(workdir).mkdir(parents=True, exist_ok=True)
    for digest in pending:
        path = Path(workdir) / ("%s.bundle" % digest)
        repo.download_bundle(digest, path)
        try:
            proc = gitutil.run_git(["bundle", "unbundle", str(path)], cwd=cwd, check=False)
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        if proc.returncode != 0:
            if incremental:
                return False
            raise IntegrityError("could not apply bundle %s: %s"
                                 % (digest[:12], proc.stderr.decode("utf-8", errors="replace").strip()))
        applied.add(digest)
    return True


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)
