"""The Iron remote: a Git repository stored as write-once files on any storage.

Layout of a remote container (see docs/SPEC.md)::

    iron.json                      marker: format, repo id, object format, encryption
    README.txt, iron-recover.py    human instructions + standalone recovery tool
    bundles/<sha256>.bundle        immutable git bundles (name = hash of stored bytes)
    log/<seq>-<digest>.json        immutable push records forming a linked chain
    log/<seq>.claim                claims mode: atomically created, names the record that owns <seq>
    log/<seq>-<digest>.ok          confirmation: the writer saw no rival, the record counts
    log/<seq>-<digest>.abandon     another writer asks to retire this stalled, unconfirmed record
    log/<seq>-<digest>.kept        ...and found it confirmed after all, so it stays
    log/<seq>-<digest>.withdrawn   the record is dead and never counts
    locks/<ms>-<token>.lock        short advisory leases that expire on their own

Nothing is ever overwritten, so sync clients cannot create conflicting copies
and a crashed upload can never corrupt existing history.
"""

import json
import os
import random
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, keys, recover
from .crypto import AesGcmCodec, PlainCodec
from .errors import IntegrityError, IronError, KeyMissing, NoRepository, PushRace, RemoteBusy
from .storage import AlreadyExists, NotFound, RetryingProvider, StorageError, open_location

MARKER = recover.MARKER
LOCK_TTL = 60.0
#: Only an unconfirmed record older than this is worth trying to retire.
#: This is a liveness heuristic: whether a push counts never depends on a clock.
STALL = 2 * LOCK_TTL

README = """This folder is an Iron repository: a private Git repository stored as plain files.

Repository id: {repo_id}
Created:       {created}
Encryption:    {encryption}

Do not edit or rename files in here by hand. Every file is written once and
never changed, which is what makes it safe to keep in Google Drive, OneDrive,
Dropbox, Box, a NAS or a USB disk.

To use it, install Iron (https://github.com/ - or copy any existing Iron folder)
and run:   iron clone <this folder>

If Iron no longer exists, you can still rebuild everything with only Python
and git:   python iron-recover.py <this folder> <new folder> [--key KEY]

Unencrypted bundles in bundles/ are standard git bundles; `git bundle
list-heads <file>` shows what each one contains.
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class State:
    tip: str = None
    seq: int = 0
    refs: dict = field(default_factory=dict)
    head: str = None
    accepted: list = field(default_factory=list)
    snapshot: str = None
    records: dict = field(default_factory=dict)
    confirmed: set = field(default_factory=set)
    withdrawn: set = field(default_factory=set)
    corrupt: list = field(default_factory=list)
    markers: dict = field(default_factory=dict)

    @property
    def pending(self):
        """Records never confirmed: withdrawn after a race, or abandoned by a crash. Never applied."""
        return sorted(n for n in self.records if n not in self.confirmed)

    @property
    def rejected(self):
        """Confirmed records that did not make the chain: a race an eventually consistent folder hid."""
        acc = set(self.accepted)
        return sorted(n for n in self.confirmed if n not in acc and n not in self.withdrawn)

    def has(self, name, ext):
        return name in self.markers.get(ext, set())

    def unresolved(self, name):
        """A retire attempt on this record has not reached a verdict yet."""
        return self.has(name, ".abandon") and not self.has(name, ".kept") and name not in self.withdrawn

    def unsettled(self):
        """Confirmed records whose fate is still being decided. They do not count
        until settled, and nobody pushes while one exists."""
        return sorted(n for n in self.confirmed if self.unresolved(n))


class RemoteRepo:
    def __init__(self, location, cache_dir=None, key=None):
        self.location = location
        self.provider = open_location(location)
        self.store = RetryingProvider(self.provider)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._key = key
        self.marker = None
        self.codec = PlainCodec()
        self._records = {}
        self._claims = {}

    @property
    def claims_mode(self):
        return bool(self.marker and self.marker.get("claims"))

    def describe(self):
        return self.provider.describe()

    # -- marker -------------------------------------------------------------

    def open(self, required=True):
        try:
            raw = self.store.read(MARKER)
        except NotFound:
            if required:
                raise NoRepository("no Iron repository at %s" % self.describe()) from None
            return None
        try:
            marker = json.loads(raw)
        except ValueError:
            raise NoRepository("%s has a damaged %s" % (self.describe(), MARKER)) from None
        if marker.get("format") != "iron-remote":
            raise NoRepository("%s is not an Iron repository" % self.describe())
        if marker.get("version", 0) > recover.FORMAT_VERSION:
            raise IronError("this repository was created by a newer Iron (format v%s); please upgrade"
                            % marker.get("version"))
        enc = marker.get("encryption")
        if enc:
            key = self._key or keys.load_key(marker["repo_id"])
            if key is None:
                raise KeyMissing("this repository is encrypted and this computer has no key for it. "
                                 "Clone with `iron clone %s --key <recovery key>`, or run `iron key import <recovery key> %s`"
                                 % (self.location, self.location),
                                 repo_id=marker["repo_id"])
            if keys.key_check(key) != enc.get("key_check"):
                raise KeyMissing("that key does not belong to this repository", repo_id=marker["repo_id"])
            self.codec = AesGcmCodec(key)
        self.marker = marker
        return marker

    def initialize(self, object_format="sha1", encrypt=False, key=None, claims=None):
        """Create the container if it does not exist. Returns (marker, new_key or None)."""
        existing = self.open(required=False)
        if existing:
            if existing.get("object_format", "sha1") != object_format:
                raise IronError("the remote stores %s objects but this repository uses %s"
                                % (existing.get("object_format"), object_format))
            if existing.get("version", 1) == recover.FORMAT_VERSION:
                try:  # keep the bundled recovery tool current
                    if self.store.read("iron-recover.py") != Path(recover.__file__).read_bytes():
                        self.store.write("iron-recover.py", Path(recover.__file__).read_bytes())
                except StorageError:
                    pass
            return existing, None
        marker = {"format": "iron-remote", "version": recover.FORMAT_VERSION, "repo_id": uuid.uuid4().hex,
                  "created": now_iso(), "created_by": "iron " + __version__,
                  "object_format": object_format, "encryption": None}
        new_key = None
        if encrypt:
            new_key = key or keys.generate_key()
            AesGcmCodec(new_key)  # fail early if cryptography is missing
            keys.save_key(marker["repo_id"], new_key)
            marker["encryption"] = {"algorithm": "aes-256-gcm", "key_check": keys.key_check(new_key)}
        self.store.write("README.txt", README.format(
            repo_id=marker["repo_id"], created=marker["created"],
            encryption="AES-256-GCM (keep your recovery key safe)" if encrypt else "none").encode())
        self.store.write("iron-recover.py", Path(recover.__file__).read_bytes())
        marker["claims"] = self.store.supports_exclusive() if claims is None else bool(claims)
        self.store.write(MARKER, json.dumps(marker, indent=2).encode())  # last: marks the container complete
        self.open()
        return self.marker, new_key

    def moved_to(self):
        """The new location if this repository was migrated away (``MOVED.json``)."""
        try:
            return json.loads(self.store.read("MOVED.json")).get("location")
        except (NotFound, ValueError):
            return None

    # -- log ------------------------------------------------------------------

    def _cache_path(self, name):
        return self.cache_dir / "log" / name if self.cache_dir else None

    def _ingest(self, name, stored, corrupt):
        m = recover.RECORD_RE.match(name)
        if recover.record_digest(stored) != m.group(2):
            corrupt.append(name)
            return False
        try:
            rec = json.loads(self.codec.decode(stored))
            if rec.get("seq") != int(m.group(1)) or not isinstance(rec.get("updates", []), list):
                raise ValueError("record does not match its name")
        except (ValueError, IntegrityError, KeyError, TypeError):
            corrupt.append(name)
            return False
        self._records[name] = rec
        cache = self._cache_path(name)
        if cache and not cache.exists():
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_bytes(stored)
            except OSError:
                pass
        return True

    def _fetch_records(self, names, corrupt):
        need = []
        for name in names:
            cache = self._cache_path(name)
            if cache and cache.is_file():
                bad = []
                if self._ingest(name, cache.read_bytes(), bad):
                    continue
                cache.unlink()
            need.append(name)
        if len(need) > 4:
            with tempfile.TemporaryDirectory(prefix="iron-log-") as tmp:
                try:
                    self.store.download_many(["log/" + n for n in need], tmp)
                except StorageError:
                    pass
                for name in list(need):
                    p = Path(tmp, "log", name)
                    if p.is_file():
                        self._ingest(name, p.read_bytes(), corrupt)
                        need.remove(name)
        for name in need:
            try:
                self._ingest(name, self.store.read("log/" + name), corrupt)
            except NotFound:
                continue

    def load_state(self):
        if self.marker is None:
            self.open()  # the marker decides how the log is read (claims or confirmations)
        names = self.store.list("log")
        record_names = {n for n in names if recover.RECORD_RE.match(n)}
        confirmed = {n[:-len(".ok")] + ".json" for n in names if recover.CONFIRM_RE.match(n)}
        withdrawn = {n[:-len(".withdrawn")] + ".json" for n in names if recover.WITHDRAWN_RE.match(n)}
        abandon = {n[:-len(".abandon")] + ".json" for n in names if n.endswith(".abandon")}
        kept = {n[:-len(".kept")] + ".json" for n in names if n.endswith(".kept")}
        corrupt = []
        claims = None
        if self.claims_mode:
            for n in names:
                m = recover.CLAIM_RE.match(n)
                if m and int(m.group(1)) not in self._claims:
                    try:
                        self._claims[int(m.group(1))] = json.loads(self.store.read("log/" + n)).get("record")
                    except (NotFound, ValueError):
                        continue
            claims = dict(self._claims)
        missing = sorted(n for n in record_names if n not in self._records)
        if missing:
            self._fetch_records(missing, corrupt)
        records = {n: self._records[n] for n in record_names if n in self._records}
        r = recover.replay(records, confirmed, withdrawn, claims, unresolved=abandon - kept - withdrawn)
        if claims is not None:
            confirmed = set(claims.values())  # in claims mode, "confirmed" means claimed
        return State(tip=r["tip"], seq=r["seq"], refs=r["refs"], head=r["head"], accepted=r["accepted"],
                     snapshot=r["snapshot"], records=records, confirmed=confirmed & set(records),
                     withdrawn=withdrawn & set(records), corrupt=corrupt,
                     markers={".ok": confirmed, ".withdrawn": withdrawn, ".abandon": abandon, ".kept": kept})

    # -- locking --------------------------------------------------------------

    def live_locks(self):
        now_ms = time.time() * 1000
        out = []
        for name in self.store.list("locks"):
            stem, _, ext = name.partition("-")
            if not name.endswith(".lock") or not stem.isdigit():
                continue
            created = int(stem)
            if now_ms - LOCK_TTL * 1000 < created < now_ms + LOCK_TTL * 10000:
                out.append(name)
        return sorted(out)

    def lock_holder(self, name):
        try:
            info = json.loads(self.store.read("locks/" + name))
            return "%s (%s, since %s)" % (info.get("device", "?"), info.get("user", "?"), info.get("created", "?"))
        except (StorageError, ValueError):
            return name

    @contextmanager
    def lock(self, timeout=None):
        """An advisory lease that serializes pushes. Correctness never depends on it:
        the log protocol in ``append`` detects any race the lock fails to prevent."""
        timeout = float(os.environ.get("IRON_LOCK_TIMEOUT", "30")) if timeout is None else timeout
        deadline = time.monotonic() + timeout
        who = keys.identity()
        while True:
            token = "%013d-%s.lock" % (int(time.time() * 1000), uuid.uuid4().hex[:12])
            body = {"device": who["device"], "device_id": who["device_id"], "user": os.environ.get("USERNAME")
                    or os.environ.get("USER", "?"), "pid": os.getpid(), "created": now_iso(), "ttl": LOCK_TTL}
            self.store.write("locks/" + token, json.dumps(body).encode())
            live = self.live_locks()
            if not live or live[0] == token:
                break
            self.store.delete("locks/" + token)
            if time.monotonic() > deadline:
                raise RemoteBusy("the remote is busy: %s is pushing. A crashed device's lock expires within "
                                 "%d seconds, or clear it with `iron unlock`." % (self.lock_holder(live[0]), LOCK_TTL))
            time.sleep(random.uniform(0.1, 0.5))
        try:
            yield
        finally:
            try:
                self.store.delete("locks/" + token)
            except StorageError:
                pass

    def unlock(self):
        """Remove every lock and finish any retire attempt left half-done by a crashed device."""
        names = [n for n in self.store.list("locks") if n.endswith(".lock")]
        for n in names:
            self.store.delete("locks/" + n)
        state = self.load_state()
        for name in state.unsettled():
            self._settle(name, state)
        return len(names)

    # -- writing --------------------------------------------------------------

    def _marker_file(self, name, ext):
        body = json.dumps({"record": name, "time": now_iso()}).encode()
        self.store.write("log/" + name[:-len(".json")] + ext, body)

    @staticmethod
    def _stalled(record):
        try:
            written = datetime.fromisoformat(record.get("time", "")).timestamp()
        except (TypeError, ValueError):
            return True
        return time.time() - written > STALL

    def _retire(self, name):
        """Try to retire another writer's unconfirmed record. True if it is now dead.

        Write ``.abandon``, list again, and write ``.withdrawn`` only if no
        ``.ok`` is visible. The owner lists after writing ``.ok`` and reports
        success only if no ``.abandon`` is visible. Whichever of the two lists
        second sees the other's file, so an acknowledged push is never retired
        and the owner of a retired record never reports success."""
        self._marker_file(name, ".abandon")
        return self._settle(name, self.load_state())

    def _settle(self, name, state):
        """Record the verdict on an abandoned record: kept if it is confirmed and its
        position is still free, dead otherwise. Never revive a record whose position
        another counted record already holds (for example after a lost .withdrawn)."""
        rec = state.records.get(name, {})
        # `accepted` excludes unresolved records, so this sees whoever holds the position meanwhile
        taken = any(n != name and state.records[n].get("seq") == rec.get("seq") for n in state.accepted)
        if state.has(name, ".ok") and not taken:
            self._marker_file(name, ".kept")
            return False
        self._marker_file(name, ".withdrawn")
        return True

    def _settle_stale(self, state):
        """Finish retire attempts whose retirer vanished, so the remote cannot jam."""
        for name in state.unsettled():
            try:
                note = json.loads(self.store.read("log/" + name[:-len(".json")] + ".abandon"))
            except (NotFound, ValueError):
                note = {}
            if self._stalled(note):  # measured from when the retire began, not from the record
                self._settle(name, state)

    def _clear_position(self, state, mine, record):
        """True when no other live record claims our position."""
        for name, other in sorted(state.records.items()):
            if name == mine or name in state.withdrawn:
                continue
            if other.get("seq") != record["seq"] or other.get("parent") != record["parent"]:
                continue
            if name in state.confirmed or not self._stalled(other) or not self._retire(name):
                return False
        return True

    def append(self, build):
        """Append one record to the log with compare-and-swap semantics.

        ``build(state)`` returns the record body (updates, bundle, head, kind) or
        raises PushRejected if the refs it expected have moved. The protocol:

        1. write the record under a unique name (it does not count yet);
        2. list the log again. If another live record claims the same position,
           withdraw ours and raise PushRace (an unconfirmed rival stalled for
           minutes is first retired with ``_retire``);
        3. write ``.ok``, list again, and report success only if nobody asked to
           retire our record meanwhile, it is in the accepted chain, and no
           record in that chain is in the middle of being retired.

        No clock decides whether a push counts. Two writers cannot both succeed
        at one position: each lists after its own write, so whichever lists
        second sees the other. The lease lock around this makes races rare;
        the protocol makes them harmless.
        """
        if self.claims_mode:
            return self._append_claimed(build)
        with self.lock():
            state = self.load_state()
            if state.unsettled():
                self._settle_stale(state)
                state = self.load_state()
                if state.unsettled():
                    raise PushRace("the remote is settling an interrupted push; try again in a moment")
            body = build(state)
            record = {"format": recover.FORMAT_VERSION, "seq": state.seq + 1, "parent": state.tip,
                      "kind": body.get("kind", "push"), "updates": body.get("updates", []),
                      "bundle": body.get("bundle"), "head": body.get("head"), "by": keys.identity(),
                      "time": now_iso(), "nonce": uuid.uuid4().hex}
            stored = self.codec.encode(json.dumps(record, sort_keys=True).encode())
            name = "%010d-%s.json" % (record["seq"], recover.record_digest(stored))
            self.store.write("log/" + name, stored)
            self._ingest(name, stored, [])
            if not self._clear_position(self.load_state(), name, record):
                self._marker_file(name, ".withdrawn")
                raise PushRace("another push reached the remote at the same moment (or one was just "
                               "interrupted). Nothing was overwritten; fetch and push again.")
            self._marker_file(name, ".ok")
            after = self.load_state()
            if after.has(name, ".abandon") or name not in after.accepted or after.unsettled():
                raise PushRace("the remote changed while pushing; fetch and push again")
            return name, after

    def _append_claimed(self, build):
        """Claims mode: the position is committed by atomically creating <seq>.claim.
        Exactly one writer can create it, whatever the timing, so there is nothing to
        confirm, retire or settle."""
        if not self.store.supports_exclusive():
            raise IronError("this repository commits pushes with atomic claims, which %s cannot create "
                            "(an rclone remote, a sync folder, or a disk without hard links). Move it to "
                            "storage that fits with `iron migrate <location>`." % self.describe())
        with self.lock():
            state = self.load_state()
            body = build(state)
            record = {"format": recover.FORMAT_VERSION, "seq": state.seq + 1, "parent": state.tip,
                      "kind": body.get("kind", "push"), "updates": body.get("updates", []),
                      "bundle": body.get("bundle"), "head": body.get("head"), "by": keys.identity(),
                      "time": now_iso(), "nonce": uuid.uuid4().hex}
            stored = self.codec.encode(json.dumps(record, sort_keys=True).encode())
            name = "%010d-%s.json" % (record["seq"], recover.record_digest(stored))
            self.store.write("log/" + name, stored)
            self._ingest(name, stored, [])
            claim = "log/%010d.claim" % record["seq"]
            try:
                self.store.create_exclusive(claim, json.dumps({"record": name}).encode())
            except AlreadyExists:
                try:  # a retried create whose first attempt succeeded is ours
                    owner = json.loads(self.store.read(claim)).get("record")
                except (NotFound, ValueError):
                    owner = None
                if owner != name:
                    self._marker_file(name, ".withdrawn")
                    raise PushRace("another push reached the remote at the same moment. Nothing was "
                                   "overwritten; fetch and push again.") from None
            after = self.load_state()
            if name not in after.accepted:
                raise PushRace("the remote changed while pushing; fetch and push again")
            return name, after

    def upload_bundle(self, path):
        stored = self.codec.encode(Path(path).read_bytes())
        digest = recover.bundle_digest(stored)
        self.store.write("bundles/%s.bundle" % digest, stored)
        return digest

    def download_bundle(self, digest, dest):
        stored = self.store.read("bundles/%s.bundle" % digest)
        if recover.bundle_digest(stored) != digest:
            raise IntegrityError("bundle %s on the remote is corrupted (hash mismatch)" % digest[:12])
        Path(dest).write_bytes(self.codec.decode(stored))
