#!/usr/bin/env python3
"""Iron Repository standalone recovery tool.

A copy of this file is written into every Iron remote as ``iron-recover.py``.
It needs only Python 3.8+ and git, nothing else, so you can rebuild your
repository even if the Iron project disappears.

    1. Copy the remote folder to this computer (sync client, the provider's
       web "download folder" button, or ``rclone copy``).
    2. python iron-recover.py <remote-folder> <new-repo-folder> [--key KEY]

Encrypted remotes also need the ``cryptography`` package and your recovery key.

This module is also the single source of truth for how Iron replays the push
log, so the recovery tool and the remote helper can never disagree.
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

FORMAT_VERSION = 2  # 2 = confirm protocol with retire handshake, optional atomic claims
MARKER = "iron.json"
RECORD_RE = re.compile(r"^(\d{10})-([0-9a-f]{16})\.json$")
CLAIM_RE = re.compile(r"^(\d{10})\.claim$")
CONFIRM_RE = re.compile(r"^(\d{10})-([0-9a-f]{16})\.ok$")
WITHDRAWN_RE = re.compile(r"^(\d{10})-([0-9a-f]{16})\.withdrawn$")
BUNDLE_RE = re.compile(r"^([0-9a-f]{64})\.bundle$")
MAGIC = b"IRONENC1"
AAD = b"iron-v1"


def record_digest(stored):
    """The 16-hex-digit name component of a log record: sha256 of its stored bytes."""
    return hashlib.sha256(stored).hexdigest()[:16]


def bundle_digest(stored):
    return hashlib.sha256(stored).hexdigest()


def decode_key(text):
    text = text.strip()
    if text.startswith("iron1-"):
        text = text[len("iron1-"):]
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if len(raw) != 32:
        raise ValueError("an Iron key is 32 bytes")
    return raw


def decrypt(key, stored):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not stored.startswith(MAGIC):
        raise ValueError("not an Iron-encrypted object")
    nonce = stored[len(MAGIC):len(MAGIC) + 12]
    return AESGCM(key).decrypt(nonce, stored[len(MAGIC) + 12:], AAD)


def counted_records(records, confirmed, withdrawn=(), unresolved=()):
    """Records that count: confirmed (``.ok``), not withdrawn, and not halfway
    through being retired (``.abandon`` without ``.kept``), plus every ancestor
    of such a record (a writer only ever builds on a counted tip, so a lost
    ``.ok`` file cannot roll history back)."""
    withdrawn = set(withdrawn) | set(unresolved)
    counted = {n for n in confirmed if n in records and n not in withdrawn}
    for name in list(counted):
        parent = records[name].get("parent")
        while parent and parent in records and parent not in counted and parent not in withdrawn:
            counted.add(parent)
            parent = records[parent].get("parent")
    return counted


def replay(records, confirmed, withdrawn=(), claims=None, unresolved=()):
    """Compute the remote's refs from its push log.

    ``records`` maps record file name -> parsed record; ``confirmed`` and
    ``withdrawn`` are the record names with a ``.ok`` / ``.withdrawn`` file.
    Only counted records (see ``counted_records``) apply. A record is accepted
    when it extends the accepted chain (its ``parent`` is the current tip and
    its ``seq`` is tip + 1) and every ``old`` value it claims matches the
    current refs. The writer protocol guarantees at most one counted record
    per position on consistently listed storage; if an eventually consistent
    folder ever shows two, the lowest file name wins, which every reader
    computes identically.

    Remotes on storage with atomic exclusive create (``"claims": true`` in
    iron.json) instead commit each position by creating ``<seq>.claim``, which
    exactly one writer can do. ``claims`` maps seq -> record name, and only
    claimed records count.
    """
    if claims is not None:
        counted = counted_records(records, {n for n in claims.values() if n in records})
    else:
        counted = counted_records(records, confirmed, withdrawn, unresolved)
    children = {}
    for name, rec in records.items():
        if name in counted:
            children.setdefault(rec.get("parent"), []).append(name)

    tip, seq, refs, head, snapshot = None, 0, {}, None, None
    accepted = []
    while True:
        chosen = None
        for name in sorted(children.get(tip, [])):
            rec = records[name]
            if rec.get("seq") != seq + 1:
                continue
            if all(refs.get(u["ref"]) == u.get("old") for u in rec.get("updates", [])):
                chosen = name
                break
        if chosen is None:
            break
        rec = records[chosen]
        for u in rec.get("updates", []):
            if u.get("new") is None:
                refs.pop(u["ref"], None)
            else:
                refs[u["ref"]] = u["new"]
        if rec.get("head"):
            head = rec["head"]
        if rec.get("kind") == "snapshot":
            snapshot = chosen
        accepted.append(chosen)
        tip, seq = chosen, seq + 1
    return {"tip": tip, "seq": seq, "refs": refs, "head": head,
            "accepted": accepted, "snapshot": snapshot}


def bundle_plan(records, state):
    """Bundles needed to rebuild everything from nothing, in apply order:
    the latest snapshot (if any) followed by every later bundle."""
    accepted = state["accepted"]
    start = accepted.index(state["snapshot"]) if state["snapshot"] else 0
    return [records[n]["bundle"] for n in accepted[start:] if records[n].get("bundle")]


def _git(args, cwd):
    proc = subprocess.run(["git"] + args, cwd=cwd, capture_output=True)
    if proc.returncode != 0:
        raise SystemExit("git %s failed: %s" % (" ".join(args), proc.stderr.decode(errors="replace")))
    return proc.stdout.decode().strip()


def recover(remote_dir, target_dir, key=None, log=print):
    with open(os.path.join(remote_dir, MARKER), "rb") as f:
        marker = json.load(f)
    if marker.get("version", 1) > FORMAT_VERSION:
        raise SystemExit("this repository uses format v%s, newer than this tool understands (v%d). Use the "
                         "iron-recover.py from a newer Iron." % (marker.get("version"), FORMAT_VERSION))
    if marker.get("encryption") and key is None:
        raise SystemExit("this repository is encrypted: pass --key <recovery key> (or set IRON_KEY)")
    if marker.get("encryption"):
        import hmac
        check = hmac.new(key, b"iron-key-check-v1", hashlib.sha256).hexdigest()[:32]
        if check != marker["encryption"].get("key_check"):
            raise SystemExit("that key does not belong to this repository")
    if os.path.exists(target_dir) and os.listdir(target_dir):
        raise SystemExit("%s already exists and is not empty; recover into a new folder" % target_dir)
    decode = (lambda b: decrypt(key, b)) if marker.get("encryption") else (lambda b: b)

    log_dir = os.path.join(remote_dir, "log")
    names = sorted(os.listdir(log_dir)) if os.path.isdir(log_dir) else []
    confirmed = {n[:-len(".ok")] + ".json" for n in names if CONFIRM_RE.match(n)}
    withdrawn = {n[:-len(".withdrawn")] + ".json" for n in names if WITHDRAWN_RE.match(n)}
    abandon = {n[:-len(".abandon")] + ".json" for n in names if n.endswith(".abandon")}
    kept = {n[:-len(".kept")] + ".json" for n in names if n.endswith(".kept")}
    records = {}
    for name in names:
        m = RECORD_RE.match(name)
        if not m:
            continue
        with open(os.path.join(log_dir, name), "rb") as f:
            stored = f.read()
        if record_digest(stored) != m.group(2):
            log("skipping corrupted record %s" % name)
            continue
        rec = json.loads(decode(stored))
        if rec.get("seq") != int(m.group(1)):
            log("skipping record %s: its contents do not match its name" % name)
            continue
        records[name] = rec
    claims = None
    if marker.get("claims"):
        claims = {}
        for name in names:
            m = CLAIM_RE.match(name)
            if m:
                with open(os.path.join(log_dir, name), "rb") as f:
                    claims[int(m.group(1))] = json.loads(f.read()).get("record")
    state = replay(records, confirmed, withdrawn, claims, unresolved=abandon - kept - withdrawn)

    _git(["init", "--quiet", "--object-format=" + marker.get("object_format", "sha1"), target_dir], cwd=None)
    for h in bundle_plan(records, state):
        with open(os.path.join(remote_dir, "bundles", h + ".bundle"), "rb") as f:
            stored = f.read()
        if bundle_digest(stored) != h:
            raise SystemExit("bundle %s is corrupted (hash mismatch)" % h)
        fd, tmp = tempfile.mkstemp(suffix=".bundle")
        with os.fdopen(fd, "wb") as f:
            f.write(decode(stored))
        try:
            _git(["bundle", "unbundle", tmp], cwd=target_dir)
        finally:
            os.unlink(tmp)
    for ref, sha in state["refs"].items():
        _git(["update-ref", ref, sha], cwd=target_dir)
    head = state["head"] if state["head"] in state["refs"] else next(
        (r for r in sorted(state["refs"]) if r.startswith("refs/heads/")), None)
    if head:
        _git(["symbolic-ref", "HEAD", head], cwd=target_dir)
        _git(["reset", "--quiet", "--hard"], cwd=target_dir)
    log("recovered %d ref(s) from %d push record(s) into %s" % (len(state["refs"]), len(state["accepted"]), target_dir))
    return state


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    key_text = os.environ.get("IRON_KEY")
    if "--key" in argv:
        i = argv.index("--key")
        key_text = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    if len(argv) != 2:
        print(__doc__)
        return 2
    try:
        key = decode_key(key_text) if key_text else None
    except ValueError:
        raise SystemExit("that does not look like an Iron key (it starts with iron1-)") from None
    recover(argv[0], argv[1], key=key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
