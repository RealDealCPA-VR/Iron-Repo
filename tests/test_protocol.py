"""The push log: replay rules and the confirm/withdraw protocol, including races
with the lock deliberately bypassed."""

import contextlib
import json
import random
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from iron import recover
from iron.errors import PushRace, PushRejected
from iron.remote import RemoteRepo


def rec(seq, parent, updates, **extra):
    return dict({"seq": seq, "parent": parent, "updates": updates}, **extra)


def upd(ref, old, new):
    return {"ref": ref, "old": old, "new": new}


M = "refs/heads/main"


def test_replay_applies_confirmed_chain_only():
    records = {
        "0000000001-aaaaaaaaaaaaaaaa.json": rec(1, None, [upd(M, None, "a1")], head=M),
        "0000000002-bbbbbbbbbbbbbbbb.json": rec(2, "0000000001-aaaaaaaaaaaaaaaa.json", [upd(M, "a1", "b2")]),
        "0000000003-cccccccccccccccc.json": rec(3, "0000000002-bbbbbbbbbbbbbbbb.json", [upd(M, "b2", "c3")]),
    }
    confirmed = set(list(records)[:2])
    state = recover.replay(records, confirmed)
    assert state["refs"] == {M: "b2"}
    assert state["seq"] == 2 and state["head"] == M
    assert recover.replay(records, set(records))["refs"] == {M: "c3"}


def test_replay_rejects_stale_old_values_and_orphans():
    a = "0000000001-aaaaaaaaaaaaaaaa.json"
    records = {
        a: rec(1, None, [upd(M, None, "a1")]),
        "0000000002-0000000000000000.json": rec(2, a, [upd(M, "WRONG", "x")]),  # CAS mismatch
        "0000000002-1111111111111111.json": rec(2, a, [upd(M, "a1", "good")]),
        "0000000003-2222222222222222.json": rec(3, "missing.json", [upd(M, "good", "orphan")]),
    }
    state = recover.replay(records, set(records))
    assert state["refs"] == {M: "good"}
    assert state["accepted"] == [a, "0000000002-1111111111111111.json"]


def test_replay_tie_breaks_by_name_and_tracks_deletes_and_snapshots():
    a = "0000000001-aaaaaaaaaaaaaaaa.json"
    records = {
        a: rec(1, None, [upd(M, None, "a1"), upd("refs/heads/x", None, "x1")]),
        "0000000002-ffffffffffffffff.json": rec(2, a, [upd(M, "a1", "late")]),
        "0000000002-0000000000000001.json": rec(2, a, [upd("refs/heads/x", "x1", None)], kind="snapshot",
                                               bundle="b" * 64),
    }
    state = recover.replay(records, set(records))
    assert state["refs"] == {M: "a1"}
    assert state["snapshot"] == "0000000002-0000000000000001.json"
    assert recover.bundle_plan(records, state) == ["b" * 64]


@pytest.fixture
def remote(env):
    """A remote using the confirm protocol (as rclone and sync-folder remotes do)."""
    path = env / "remote.iron"
    r = RemoteRepo(path.as_posix())
    r.initialize(claims=False)
    return path


@pytest.fixture
def claimed(env):
    """A remote on storage with atomic exclusive create (folders, NAS, USB disks)."""
    path = env / "claimed.iron"
    r = RemoteRepo(path.as_posix())
    assert r.initialize()[0]["claims"] is True
    return path


def no_lock(repo):
    repo.lock = contextlib.nullcontext
    return repo


def cas(ref, old, new):
    def build(state):
        if state.refs.get(ref) != old:
            raise PushRejected(ref)
        return {"updates": [upd(ref, old, new)]}
    return build


def test_append_is_compare_and_swap(remote):
    r = RemoteRepo(remote.as_posix())
    r.open()
    r.append(cas(M, None, "a1"))
    with pytest.raises(PushRejected):
        r.append(cas(M, None, "b1"))
    name, state = r.append(cas(M, "a1", "a2"))
    assert state.refs == {M: "a2"} and state.tip == name
    # a fresh reader with no cache sees the same thing
    fresh = RemoteRepo(remote.as_posix())
    fresh.open()
    assert fresh.load_state().refs == {M: "a2"}


def test_late_writer_defers_to_confirmed_record(remote):
    """B loads state, then A pushes completely, then B writes: B must withdraw."""
    a, b = no_lock(RemoteRepo(remote.as_posix())), no_lock(RemoteRepo(remote.as_posix()))
    a.open(), b.open()

    def build_b(state):
        a.append(cas(M, None, "from-a"))  # A completes while B holds a stale view
        return {"updates": [upd(M, None, "from-b")]}

    with pytest.raises(PushRace):
        b.append(build_b)
    state = RemoteRepo(remote.as_posix())
    state.open()
    s = state.load_state()
    assert s.refs == {M: "from-a"}
    assert len(s.withdrawn) == 1 and len(s.pending) == 1


def test_simultaneous_writers_never_both_confirm(remote):
    """A and B both write before either checks: neither may confirm."""
    a, b = no_lock(RemoteRepo(remote.as_posix())), no_lock(RemoteRepo(remote.as_posix()))
    a.open(), b.open()
    b_written, a_checked = threading.Event(), threading.Event()
    real_write = b.store.write

    def b_write(path, data):
        real_write(path, data)
        if path.endswith(".json"):
            b_written.set()
            a_checked.wait(10)

    b.store.write = b_write
    outcome = {}

    def run_b():
        try:
            outcome["b"] = b.append(cas(M, None, "from-b"))[0]
        except PushRace:
            outcome["b"] = None

    t = threading.Thread(target=run_b)
    a_real_load = a.load_state
    calls = {"n": 0}

    def a_load():
        calls["n"] += 1
        st = a_real_load()
        if calls["n"] == 2:  # A's post-write check
            a_checked.set()
        return st

    def build_a(state):
        t.start()
        b_written.wait(10)
        return {"updates": [upd(M, None, "from-a")]}

    a.load_state = a_load
    try:
        outcome["a"] = a.append(build_a)[0]
    except PushRace:
        outcome["a"] = None
    t.join(10)
    assert outcome["a"] is None, "A saw B's record and must not confirm"
    final = RemoteRepo(remote.as_posix())
    final.open()
    s = final.load_state()
    winners = [n for n in (outcome["a"], outcome.get("b")) if n]
    assert set(winners) <= set(s.accepted)
    assert len(winners) <= 1


def test_stress_without_lock_every_acknowledged_push_survives(remote):
    """Many writers hammer one ref with the lock disabled. Every append that
    returned success must be in the final accepted chain; nothing else may be."""
    successes, errors = [], []
    lock = threading.Lock()

    def worker():
        r = no_lock(RemoteRepo(remote.as_posix()))
        r.open()
        for _ in range(12):
            for _attempt in range(50):
                try:
                    def build(state):
                        return {"updates": [upd(M, state.refs.get(M), uuid.uuid4().hex)]}
                    name, _ = r.append(build)
                    with lock:
                        successes.append(name)
                    break
                except (PushRace, PushRejected):
                    time.sleep(random.uniform(0, 0.03))
                    continue
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
                    return

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not errors, errors
    final = RemoteRepo(remote.as_posix())
    final.open()
    s = final.load_state()
    assert s.rejected == []
    assert set(successes) == set(s.accepted)
    assert len(successes) == 48


def test_stale_unconfirmed_record_does_not_block_forever(remote):
    """A writer that crashed between writing and confirming blocks its position
    only while it could still confirm."""
    r = RemoteRepo(remote.as_posix())
    r.open()
    old_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    crashed = {"format": 1, "seq": 1, "parent": None, "updates": [upd(M, None, "crash")], "time": old_time}
    stored = json.dumps(crashed).encode()
    r.store.write("log/0000000001-%s.json" % recover.record_digest(stored), stored)
    name, state = r.append(cas(M, None, "ok"))
    assert state.refs == {M: "ok"}

    recent = dict(crashed, time=datetime.now(timezone.utc).isoformat(timespec="seconds"), nonce="x")
    stored = json.dumps(dict(recent, seq=2, parent=name, updates=[upd(M, "ok", "zzz")])).encode()
    r.store.write("log/0000000002-%s.json" % recover.record_digest(stored), stored)
    with pytest.raises(PushRace):
        r.append(cas(M, "ok", "next"))


def test_lock_serializes_and_expires(remote):
    a, b = RemoteRepo(remote.as_posix()), RemoteRepo(remote.as_posix())
    a.open(), b.open()
    with a.lock():
        with pytest.raises(Exception) as exc:
            with b.lock(timeout=0.5):
                pass
        assert "busy" in str(exc.value)
    # a crashed holder's lock (older than the TTL) is ignored
    stale = "%013d-deadbeef0000.lock" % 1000
    a.store.write("locks/" + stale, b"{}")
    with b.lock(timeout=1):
        pass
    assert b.unlock() == 1


def test_stalled_writer_cannot_confirm_after_being_retired(remote, monkeypatch):
    """Reviewer's scenario: A passes its rival check, then stalls (laptop sleep or
    clock skew) long enough that B retires A's record and pushes. A must not
    report success, and B's acknowledged push must survive."""
    import iron.remote as remote_mod

    base = RemoteRepo(remote.as_posix())
    base.open()
    base.append(cas(M, None, "base"))
    a, b = RemoteRepo(remote.as_posix()), RemoteRepo(remote.as_posix())
    a.open(), b.open()
    acked = {}
    real_marker = a._marker_file

    def a_marker(name, ext):
        if ext == ".ok":
            monkeypatch.setattr(remote_mod, "STALL", -1)  # everything now looks stalled to B
            with a_lock_bypassed(b):
                acked["b"] = b.append(cas(M, "base", "from-b"))[0]
        return real_marker(name, ext)

    a._marker_file = a_marker
    with pytest.raises(PushRace):
        a.append(cas(M, "base", "from-a"))
    final = RemoteRepo(remote.as_posix())
    final.open()
    s = final.load_state()
    assert s.refs == {M: "from-b"} and acked["b"] in s.accepted


@contextlib.contextmanager
def a_lock_bypassed(repo):
    saved = repo.lock
    repo.lock = contextlib.nullcontext
    try:
        yield
    finally:
        repo.lock = saved


def test_lost_confirmation_files_do_not_roll_history_back(remote):
    r = RemoteRepo(remote.as_posix())
    r.open()
    for i in range(5):
        r.append(cas(M, None if i == 0 else "v%d" % (i - 1), "v%d" % i))
    for ok in sorted((remote / "log").glob("*.ok"))[1:4]:
        ok.unlink()  # lose the middle confirmations (not the newest)
    fresh = RemoteRepo(remote.as_posix())
    fresh.open()
    assert fresh.load_state().refs == {M: "v4"}


def test_third_writer_never_builds_on_a_record_being_retired(remote, monkeypatch):
    """Second review's scenario: O stalls after its check; B starts retiring O's
    record; O's .ok lands; C tries to push on top of O; then B decides. No push
    that was acknowledged may be lost."""
    import iron.remote as remote_mod

    seed = no_lock(RemoteRepo(remote.as_posix()))
    seed.open()
    seed.append(cas(M, None, "base"))
    o, b, c = (no_lock(RemoteRepo(remote.as_posix())) for _ in range(3))
    for r in (o, b, c):
        r.open()
    acked, o_name = {}, {}
    o_real, b_real = o._marker_file, b._marker_file

    def b_marker(name, ext):
        if ext == ".withdrawn" and name == o_name.get("n"):
            monkeypatch.setattr(remote_mod, "STALL", 120.0)  # only O froze; B's retire is fresh
            o_real(name, ".ok")  # O's confirmation lands mid-retire
            try:
                acked["c"] = c.append(lambda st: {"updates": [upd(M, st.refs.get(M), "from-c")]})[0]
            except (PushRace, PushRejected):
                pass
        return b_real(name, ext)

    def o_marker(name, ext):
        if ext == ".ok" and "n" not in o_name:
            o_name["n"] = name
            monkeypatch.setattr(remote_mod, "STALL", -1)  # O now looks frozen to B
            try:
                acked["b"] = b.append(cas(M, "base", "from-b"))[0]
            except (PushRace, PushRejected):
                pass
            monkeypatch.setattr(remote_mod, "STALL", 120.0)
        return o_real(name, ext)

    b._marker_file, o._marker_file = b_marker, o_marker
    try:
        acked["o"] = o.append(cas(M, "base", "from-o"))[0]
    except PushRace:
        pass
    final = RemoteRepo(remote.as_posix())
    final.open()
    s = final.load_state()
    assert acked and all(n in s.accepted for n in acked.values()), (acked, s.accepted)
    assert "c" not in acked  # C must refuse while O's record was unresolved


def test_unlock_finishes_an_abandoned_retire(remote):
    r = RemoteRepo(remote.as_posix())
    r.open()
    name, _ = r.append(cas(M, None, "a"))
    r._marker_file(name, ".abandon")  # a retirer crashed right after starting
    with pytest.raises(PushRace):
        no_lock(RemoteRepo(remote.as_posix())).append(cas(M, "a", "b"))
    r.unlock()
    assert r.load_state().has(name, ".kept")
    r.append(cas(M, "a", "b"))


def test_claims_mode_stress_without_lock(claimed):
    """With atomic claims, no interleaving can lose an acknowledged push."""
    test_stress_without_lock_every_acknowledged_push_survives(claimed)


def test_claims_mode_ignores_stalls_and_clocks(claimed, monkeypatch):
    """A writer frozen for any length of time can never take a position someone else claimed."""
    import iron.remote as remote_mod

    base = RemoteRepo(claimed.as_posix())
    base.open()
    base.append(cas(M, None, "base"))
    a, b = no_lock(RemoteRepo(claimed.as_posix())), no_lock(RemoteRepo(claimed.as_posix()))
    a.open(), b.open()
    acked = {}
    real_create = a.store.create_exclusive

    def a_create(path, data):
        monkeypatch.setattr(remote_mod, "STALL", -1)  # A is "frozen" right before claiming
        acked["b"] = b.append(cas(M, "base", "from-b"))[0]
        return real_create(path, data)

    a.store.create_exclusive = a_create
    with pytest.raises(PushRace):
        a.append(cas(M, "base", "from-a"))
    s = RemoteRepo(claimed.as_posix())
    s.open()
    assert s.load_state().refs == {M: "from-b"}


def test_claims_mode_survives_a_lost_claim_file(claimed):
    r = RemoteRepo(claimed.as_posix())
    r.open()
    for i in range(4):
        r.append(cas(M, None if i == 0 else "v%d" % (i - 1), "v%d" % i))
    (claimed / "log" / "0000000002.claim").unlink()
    fresh = RemoteRepo(claimed.as_posix())
    fresh.open()
    assert fresh.load_state().refs == {M: "v3"}


def test_a_lost_withdrawn_file_cannot_revive_a_retired_record(remote, monkeypatch):
    """Third review: B retires stalled A and pushes; A's .ok lands late; A's .withdrawn
    is then lost and someone runs `iron unlock`. B's acknowledged push must survive."""
    import iron.remote as remote_mod

    base = RemoteRepo(remote.as_posix())
    base.open()
    base.append(cas(M, None, "base"))
    a, b = RemoteRepo(remote.as_posix()), RemoteRepo(remote.as_posix())
    a.open(), b.open()
    acked, a_name = {}, {}
    real_marker = a._marker_file

    def a_marker(name, ext):
        if ext == ".ok":
            a_name["n"] = name
            monkeypatch.setattr(remote_mod, "STALL", -1)
            with a_lock_bypassed(b):
                acked["b"] = b.append(cas(M, "base", "from-b"))[0]
            monkeypatch.setattr(remote_mod, "STALL", 120.0)
        return real_marker(name, ext)

    a._marker_file = a_marker
    with pytest.raises(PushRace):
        a.append(cas(M, "base", "from-a"))  # its .ok still lands, late
    (remote / "log" / (a_name["n"][:-len(".json")] + ".withdrawn")).unlink()
    fixer = RemoteRepo(remote.as_posix())
    fixer.open()
    fixer.unlock()
    final = RemoteRepo(remote.as_posix())
    final.open()
    s = final.load_state()
    assert s.refs == {M: "from-b"} and acked["b"] in s.accepted
