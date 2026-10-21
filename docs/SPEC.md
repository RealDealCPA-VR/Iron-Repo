# Iron remote format, version 2

This document is the public specification of how Iron stores a repository
(PRD Phase 9: *document repository specification publicly*). Anyone can
implement a reader from this page. A complete reader,
[`iron/recover.py`](../iron/recover.py), is about 200 lines and is copied into
every remote.

## Design principles

1. **Git is the engine.** Commits, trees, blobs, branches, tags, diffs and
   merges are standard Git. Iron re-implements none of it. Local repositories
   are ordinary Git repositories (SHA-1 or SHA-256 object format).
2. **Write once, never modify.** Every file in a remote is created exactly
   once under a name that no other write will ever use. Iron never overwrites,
   renames or deletes anything except its own short-lived lock files. This is
   what makes desktop sync clients, eventually consistent object stores and
   flaky networks safe.
3. **Storage is dumb.** A provider only needs to read, write, list and delete
   whole files ([`iron/storage/base.py`](../iron/storage/base.py)). Locking,
   compare-and-swap and integrity are all built above that line.
4. **Leaving is free.** Remotes carry their own recovery tool and README. The
   data inside is standard Git bundles.

## Architecture

```
  user / agent:  iron CLI · iron.api.Repository · plain git · any Git GUI or IDE
                          │
                       git (engine: objects, commits, branches, merge, diff)
                          │  gitremote-helpers(7) protocol
                  git-remote-iron  ── iron/remote_helper.py
                          │
        Iron remote: push log + bundles + locks      ── iron/remote.py, iron/sync.py
                          │
        StorageProvider: read · write · list · delete · health_check
           │                     │                         │
     LocalProvider         RcloneProvider             FlakyProvider
  (folder, NAS, USB,   (Google Drive, OneDrive,     (fault injection
   sync folders)        Dropbox, Box, S3, B2,         for testing)
                        WebDAV, SFTP, ...)
```

The remote layer never imports a vendor SDK, and git never knows where the
bytes live. This matches PRD section 3.

## Container layout

A remote is one folder (the *container*), conventionally `<name>.iron`:

```
iron.json                       marker (written last when the container is created)
README.txt                      what this folder is and how to recover it
iron-recover.py                 standalone recovery tool (Python 3.8+ and git only)
bundles/<sha256>.bundle         git bundles
log/<seq>-<digest>.json         push records
log/<seq>.claim                 claims mode only: names the record that owns position <seq>
log/<seq>-<digest>.ok           confirmations
log/<seq>-<digest>.abandon      requests to retire a stalled, unconfirmed record
log/<seq>-<digest>.withdrawn    dead records (never count)
locks/<ms>-<token>.lock         advisory leases
MOVED.json                      written by `iron migrate`: {"location": <new location>}
```

Readers ignore any name that does not match these patterns, including names
starting with `.` and the "conflicted copy" files that sync clients create.

### `iron.json`

```json
{
  "format": "iron-remote",
  "version": 2,                     // readers refuse versions newer than they know
  "repo_id": "9f2c…",               // random, 32 hex digits; keys are stored per repo_id
  "created": "2026-10-21T17:00:00+00:00",
  "created_by": "iron 0.1.0",
  "object_format": "sha1",          // or "sha256"; must match the local repository
  "claims": true,                   // how pushes commit: atomic claims, or the confirm protocol
  "encryption": null                // or {"algorithm": "aes-256-gcm", "key_check": "<32 hex>"}
}
```

`key_check` is `HMAC-SHA256(key, "iron-key-check-v1")[:32 hex]`. It tells a
right key from a wrong one without revealing the key.

### Stored bytes and encryption

Every bundle and log file is stored as `encode(plaintext)`:

- unencrypted: `encode(x) = x`;
- encrypted: `encode(x) = "IRONENC1" || nonce(12) || AES-256-GCM(key, nonce, x, aad="iron-v1")`.

Names are derived from the **stored** bytes, never from the plaintext. So with
encryption on, the provider cannot even confirm whether you hold a known file.
What the provider can still see is file sizes, the number of pushes, timing,
and the device names in lock files.

### Bundles

`bundles/<H>.bundle`, where `H = sha256(stored bytes)` as 64 hex digits.
Decoded, each file is a standard
[git bundle](https://git-scm.com/docs/git-bundle). A push bundle contains the
objects reachable from the pushed tips minus those reachable from every ref
the remote already had. Its prerequisites are therefore always provided by
earlier bundles in the accepted chain. A *snapshot* bundle (`iron compact`) has
no prerequisites and contains every ref. Readers must check `H` before using a
bundle.

### Push records

`log/<seq>-<digest>.json`, where `seq` is 10 decimal digits and
`digest = sha256(stored bytes)[:16 hex]`:

```json
{
  "format": 1,
  "seq": 7,                                   // position in the chain, 1-based
  "parent": "0000000006-4be1c3a0d29f8e71.json", // previous record, or null for seq 1
  "kind": "push",                              // or "snapshot"
  "updates": [{"ref": "refs/heads/main", "old": "<sha or null>", "new": "<sha or null>"}],
  "bundle": "<H or null>",                     // null when the remote already had every object
  "head": "refs/heads/main",                   // optional: the default branch
  "by": {"device": "laptop", "device_id": "…", "agent": "Claude", "session": "…"},
  "time": "2026-10-21T17:00:00+00:00",
  "nonce": "<random>"
}
```

`new: null` deletes a ref. Several updates in one record apply atomically.

### Confirmations, abandon requests and withdrawals

`log/<seq>-<digest>.ok`, `.abandon` and `.withdrawn` refer to the record with
the same stem. Their content (`{"record": …, "time": …}`) is informational.

## Replaying the log (reader rules)

1. Collect every record whose digest matches its name and that decodes to JSON
   whose `seq` matches its name. Ignore anything else.
2. A record **counts** if it has a `.ok` and no `.withdrawn`, or if it is an
   ancestor (through `parent`) of a record that counts and is not withdrawn.
   The second rule means a lost `.ok` file cannot roll history back.
3. Start with `tip = null`, `seq = 0` and `refs = {}`. Repeatedly pick, among
   counted records with `parent == tip` and `seq == seq + 1` whose every
   `old` equals the current `refs[ref]`, the one with the lowest file name.
   Apply its updates, set `tip` to it, and continue. Stop when none qualifies.
4. `head` is the last `head` value seen. The latest accepted `snapshot` record
   lets a fresh reader skip every earlier bundle.

`replay()` and `bundle_plan()` in [`iron/recover.py`](../iron/recover.py) are
the normative implementation.

## Two ways to commit a push

When a remote is created, Iron probes the storage. If it can atomically
create a file that must not already exist, the remote uses **claims mode**.
Plain folders, NAS and network shares, and USB disks with a real file system
can, via hard links. Everything else uses the **confirm protocol**: rclone
backends, sync folders and FAT-formatted disks. `iron migrate` converts
between the two as needed.

### Claims mode: provably safe

A push writes its record, then atomically creates `log/<seq>.claim` naming it.
Exactly one writer can create that file, so exactly one record owns each
position, regardless of stalls, crashes, clock skew or a missing lock. The
loser is told to fetch and retry. Readers count only claimed records (plus
their ancestors, so a lost claim file cannot truncate history).

## Writing with the confirm protocol

A push runs [`RemoteRepo.append`](../iron/remote.py) and
[`Helper.push`](../iron/remote_helper.py):

1. Check each ref locally: fast-forward, no force unless allowed, no deletion
   unless confirmed, tags immutable.
2. Upload the bundle. It is immutable and uniquely named, so no lock is needed
   for this, and if the push later fails the bundle is merely unused.
3. Acquire the lease lock (below).
4. Replay the log. If any `old` no longer matches, reject with *fetch first*
   (compare-and-swap).
5. Write the record at `seq = tip.seq + 1`, `parent = tip`.
6. **List and replay again.** Look for *rivals*: other records with the same
   `seq` and `parent` that are not withdrawn. If a rival is confirmed, or is
   unconfirmed but recent, write `.withdrawn` for our own record and fail the
   push with *another push reached the remote at the same moment*. An
   unconfirmed rival that has been stalled for more than 120 s (a crashed or
   frozen writer) is *retired* first, see below.
7. Write `.ok`, then **list again**. Report success only if no `.abandon` for
   our record is visible and the record is in the accepted chain. Otherwise
   report failure. The push may still have landed, in which case a retry is
   simply up to date.

**Retiring a stalled record.** Write `R.abandon`, list again, then record the
verdict: `R.withdrawn` if no `R.ok` is visible, or `R.kept` if one is, in which
case `R` stays alive and our own push fails instead. Until a verdict exists,
`R` is *unresolved*. Nobody builds on an unresolved record, and no push is
acknowledged while one is in its chain. A verdict left unwritten by a crashed
retirer is written by the next writer after 120 s, or by `iron unlock`.

**Why an acknowledged push is never lost.** No clock decides whether a push
counts; the 120 s threshold only decides when retiring is worth trying.
Every decision follows a write by a fresh listing:

- *Two writers at one position.* Each lists after writing its record, so
  whichever lists second sees the other's record and withdraws.
- *A stalled owner and a retiring writer.* The owner lists after writing
  `.ok`, and the retiring writer lists after writing `.abandon`. Whichever
  lists second sees the other's file:
  - if the owner reported success, the retiring writer saw `.ok` and does not
    withdraw the record;
  - if the retiring writer withdrew the record, the owner saw `.abandon` and
    did not report success.
- *A third writer* never acknowledges a push that sits on top of an
  unresolved record. So a record cannot be retired from under an
  acknowledged push: either the third writer saw the `.abandon` (and did not
  acknowledge), or the `.abandon` came later, in which case the retirer's
  listing sees `.ok` and keeps the record.
- *A crash between steps 5 and 7* leaves an unconfirmed record. It never
  counts, and it is retired once it has stalled.

**The one remaining assumption (confirm protocol only).** Retiring and
settling use a 120 s threshold, measured by one writer's clock against
another writer's timestamps. Acknowledged pushes are safe unless a retirer's
verdict is delayed by more than 120 s *as one device's clock reads
another's*. That can come from a real freeze inside the narrow window between
the retirer's listing and its verdict (milliseconds on a disk, seconds over
rclone), or from clock skew. Clock skew of more than 60 s between devices also
removes the lease lock's protection. Keep device clocks synchronized (every
modern OS does this by default). `iron verify` reports the result if it ever
happens: a confirmed record outside the history. Remotes in claims mode do
not have this assumption.

`tests/test_protocol.py` covers all of this with the lock disabled: 4
contending writers, a writer frozen between its check and its `.ok`, a third
writer building during a retire (both found by independent review against
earlier designs), lost `.ok` files, and crashed writers.

### Lease lock

`locks/<ms since epoch, 13 digits>-<random>.lock`. To acquire it, write your
own lock, list the folder, and hold the lock if yours is the oldest live one.
Otherwise delete yours and retry with jitter (30 s by default,
`IRON_LOCK_TIMEOUT`). A lock older than 60 s (by the writer's clock) is
treated as dead. In claims mode the lock exists for efficiency only, so a wrong
clock can cause a retry but never a lost push. Under the confirm protocol, see
the clock assumption above. `iron unlock` removes
lock files by hand.

## Guarantees and limits

| Storage | Guarantee |
|---|---|
| Folder, NAS, network share, USB disk with a real file system that **no sync client touches** (**claims mode**; Iron detects the OneDrive, Google Drive, Dropbox, Box and iCloud apps plus the markers of Syncthing, Nextcloud/ownCloud, MEGA, pCloud, Synology Drive and Tresorit, and falls back to the confirm protocol for them. For any other sync tool, create the remote with `--no-claims`.) | A push reported as successful is never lost or overwritten, under any timing, crash or clock. Conflicting pushes are rejected and tell the user to pull. |
| rclone backends whose listings show a completed write immediately (S3 is documented as strongly consistent; most file APIs behave this way, but some, Google Drive's search index among them, can lag briefly) | The same, under the one clock assumption stated above. |
| Folders synced between computers by a desktop client (OneDrive, Google Drive, Dropbox, Box, iCloud apps) | Each computer only sees the other's files after the sync client delivers them. Two pushes from two computers within that delay can both confirm. When the files meet, every reader picks the same winner (lowest name), and the losing computer is told on its next fetch or push. Its commits are still on that computer and in the remote. **Nothing is ever overwritten or deleted**, but for heavy simultaneous collaboration prefer an rclone remote (direct API). |

**What encryption does and does not protect.** Bundles and records are
sealed with AES-256-GCM: the provider cannot read them, and it cannot alter
them without detection. The small marker files (`.ok`, `.abandon`,
`.withdrawn`, locks) are not authenticated, and anyone who can write to the
storage can delete files. A malicious provider could therefore hide recent
pushes (roll history back) or switch which of two racing pushes counts. It
cannot forge content. Your local clones are unaffected, `iron verify` reports
records that are confirmed but not part of the history, and a second remote
(`iron remote add backup …`) or an `iron export` protects against loss.

**Remotes are never created implicitly.** Pushing to a location without
`iron.json` fails, because the location could be an unmounted disk, a typo,
or a sync folder that has not finished downloading. Create remotes with
`iron setup` or `iron remote add`, or push once with `IRON_CREATE=1`. After
`iron migrate`, the old container holds `MOVED.json`, and pushes to it are
refused with the new location.

Each push and fetch holds the bundle it moves in memory. This is comfortable
up to a few hundred MB per push. Large binary assets belong in Git LFS or
plain storage.

## Local state

Iron keeps per-remote caches in `.git/iron/remotes/<hash>/`: decoded log
records and the bundles already applied. Keys and the device identity live in
`~/.iron/` (override with `IRON_HOME`). Deleting the caches is always safe.
Checkpoints are commits under `refs/iron/checkpoints/<id>`.

## Configuration

| Setting | Effect |
|---|---|
| `git config iron.allowForcePush true` or `IRON_ALLOW_FORCE=1` | allow non-fast-forward pushes (old commits stay in the remote's history) |
| `git config iron.allowDelete true` or `IRON_ALLOW_DELETE=1` | allow deleting remote refs (`iron push --delete` asks instead) |
| `IRON_KEY` | encryption key, overriding `~/.iron/keys` |
| `IRON_RCLONE` | the rclone executable (a path, or a JSON argv list) |
| `IRON_LOCK_TIMEOUT`, `IRON_RETRY_ATTEMPTS` | how long to wait for the lock (seconds), and how many times to retry transient errors |
| `IRON_AGENT`, `IRON_SESSION` | attribution recorded in commits, checkpoints and push records |
| `IRON_CREATE=1` | let a plain `git push` create a missing remote container |
