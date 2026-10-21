<div align="center">

# ⛓️ Iron Repository

### Your code. Your history. Your storage. Your repository.

**Turn the OneDrive, Google Drive, Dropbox, Box, NAS or USB stick you already own
into a private Git remote. No server. No account. No middleman. No lock-in.**

![license](https://img.shields.io/badge/license-MIT-black)
![python](https://img.shields.io/badge/python-3.8%2B-black)
![dependencies](https://img.shields.io/badge/runtime%20deps-zero-black)
![engine](https://img.shields.io/badge/engine-real%20git-black)
![tests](https://img.shields.io/badge/tests-67%20passing-black)

</div>

---

```bash
cd my-project
iron setup                 # finds your OneDrive / Google Drive / Dropbox, asks one question
```
```
✓ Started version control in C:\Projects\my-project
✓ Added a starter .gitignore (secrets, dependencies and build output stay out)
The first version will contain 214 file(s), 3.1 MB, NOT encrypted.
✓ Saved the first version
✓ origin -> OneDrive sync folder C:/Users/you/OneDrive/Iron-Repos/my-project.iron

✓ Done. Your repository lives in OneDrive sync folder C:/Users/you/OneDrive/Iron-Repos/my-project.iron
  On any other computer:   iron clone "C:/Users/you/OneDrive/Iron-Repos/my-project.iron"
```

That's a private, versioned, multi-computer Git repository, stored in **your**
cloud, in one command. Everything after that is just Git:
`git push`, `git pull`, VS Code, GitHub Desktop, your CI, all of it.

---

## Why this exists

GitHub is great until you remember that your company's source code, your
agent experiments and your side projects all live on someone else's servers,
behind someone else's terms, pricing and outages.

You already pay for cloud storage. **Iron makes it a Git host.**

- 🧱 **Real Git underneath.** Iron does not reinvent version control. Commits,
  branches, merges and diffs are 100% standard Git. Iron is a *transport*
  (`git-remote-iron`) plus a friendly CLI.
- 🔒 **Optional end-to-end encryption.** AES-256-GCM before anything leaves
  your machine. The provider sees noise, and even file names are hashes of
  ciphertext.
- 🛟 **Survives Iron disappearing.** Every remote carries its own
  `iron-recover.py`, and snapshots are plain git bundles. If this project
  vanished tomorrow, `git clone` still gets your code back.
- 🤖 **Built for AI agents.** Checkpoint everything, let the agent loose, run
  your tests, and get automatic rollback on failure, with every commit
  attributed to the agent that made it.
- 🚚 **Move anywhere, anytime.** `iron migrate` copies a repository between
  providers, verifying every byte on both sides.

## Install (30 seconds, nothing downloaded)

You need [Git](https://git-scm.com/downloads) and
[Python 3.8+](https://www.python.org/downloads/). That's the whole list.

```bash
git clone https://github.com/RealDealCPA-VR/Iron-Repo.git
cd Iron-Repo
```

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File .\install.ps1          # add -WithEncryption for crypto
```
```bash
# macOS / Linux
sh install.sh                                                    # add --with-encryption for crypto
```

The installer puts `bin/` on your PATH and runs `iron doctor`. Iron runs
straight from the folder you cloned. Prefer packages? `pipx install .` works too.

## The 60-second tour

```bash
iron setup --to onedrive          # or google-drive, dropbox, box, icloud, D:/Backups, gdrive: ...
iron commit -am "add login page"  # any git command works through iron
iron sync                         # pull everyone's work, then publish yours
iron                              # dashboard: branch, sync state, recent changes
```

```
IRON REPOSITORY

my-project   (C:\Projects\my-project)
────────────────────────────────────────
main
✓ Synced with origin  (OneDrive sync folder …/Iron-Repos/my-project.iron, as of last sync)

Recent changes

● Add Codex native protocol  [Codex]
  4 minutes ago

● Build session backend  [Claude]
  31 minutes ago
```

**On your other laptop:**

```bash
iron clone onedrive://Iron-Repos/my-project.iron
```

## 🤖 Agent mode

```bash
iron agent --name Claude --test "pytest -q" -- claude -p "fix the flaky upload test"
```

| Step | What happens |
|---|---|
| 1 | **Checkpoint**: snapshots *everything*, including untracked files like `.env` |
| 2 | **Run**: the agent works; `IRON_AGENT` and `IRON_SESSION` are set, and it never sees your encryption key |
| 3 | **Validate**: your test command runs |
| 4a | ✅ Pass: committed with `Iron-Agent: Claude` attribution |
| 4b | ❌ Fail: files **and branch** are rolled back, any agent commits are undone, and the attempt is kept as a checkpoint for inspection |

Manual safety net: `iron checkpoint`, `iron restore last`. A restore always
checkpoints first, so even an undo can be undone. There is also a Python API
(`iron.api.Repository`) for agents that want to drive it directly. See
[docs/AGENTS.md](docs/AGENTS.md).

## 🛡️ Hard to break, by design

**Every file Iron stores is written exactly once and never modified.** That
single rule is why a sync client like OneDrive or Dropbox can't corrupt your
repository: there is never a conflict for it to "resolve".

On top of that:

| Threat | Defense |
|---|---|
| Two laptops push at the same instant | On folders, NAS and USB disks, every push commits with an **atomic exclusive create**: exactly one push owns each slot, whatever the timing, crashes or clocks. The loser is told to pull. Cloud APIs without atomic primitives use a confirm handshake that is safe as long as device clocks roughly agree. |
| Laptop sleeps mid-push | Claims mode doesn't care. On the confirm protocol, a stalled push is retired with a two-sided handshake. |
| Flaky Wi-Fi, half-written uploads | Every object is SHA-256-named and verified on download, and retries are idempotent. Tested against a fault injector that drops, truncates and duplicates writes. |
| `git push --force`, branch deletion | Refused by default. Even an allowed force-push deletes nothing from storage. |
| Typo'd path, unmounted NAS | Iron never creates a repository implicitly, so you get an error instead of a silent fork. |
| Accidentally committing secrets | `iron setup` writes a starter `.gitignore`, then scans names and contents for keys before the first commit. |
| A nosy cloud provider | `--encrypt`: AES-256-GCM over every bundle and record. |

Not hand-waving: an independent reviewer attacked the concurrency protocol
twice and found two real holes, both now fixed and pinned by regression tests.
The format, the protocol and its one remaining stated assumption are
documented in [docs/SPEC.md](docs/SPEC.md).

## 🛟 The sovereignty test

> *If the Iron project disappears tomorrow, can you still recover everything?*

```bash
iron verify --deep          # rebuild every commit from the remote alone, then git fsck it
iron export backup.bundle   # one file; `git clone backup.bundle` restores it, no Iron needed
python my-project.iron/iron-recover.py my-project.iron rebuilt   # ships inside every remote
iron migrate D:/Backups     # copy, verify both sides, switch over, leave a forwarding note
```

## Where can it live?

| You type | It goes to |
|---|---|
| `onedrive` · `google-drive` · `dropbox` · `box` · `icloud` | that app's sync folder (auto-detected), or a matching rclone remote |
| `D:/Backups` · `//nas/share` · `/mnt/usb` | any folder: NAS, USB disk, network share |
| `gdrive:` · `s3:bucket` · any [rclone](https://rclone.org) remote | the provider's API directly: about 70 backends, no desktop app needed |

Details per provider: [docs/PROVIDERS.md](docs/PROVIDERS.md). Run
`iron doctor --deep` to put your own storage through the compliance suite.

## How it works (one paragraph)

`git push` hands your commits to `git-remote-iron`, which packs only the new
objects into a standard git bundle and uploads it under its SHA-256 name.
It then appends a tiny record (*"main moved from a1b2 to c3d4"*) to an
append-only log, using a write → list → confirm protocol that makes
concurrent pushes safe on storage that has no locking or atomic operations at
all. `git fetch` replays the log and downloads only the bundles it hasn't seen.
That's it: about 3,500 lines of standard-library Python, with no dependencies.

```
 you / agents / IDEs ──► git ──► git-remote-iron ──► push log + bundles ──► StorageProvider
                                                                         ├─ folder / NAS / sync folder
                                                                         ├─ rclone (Drive, S3, Box, …)
                                                                         └─ fault simulator (tests)
```

## Honest status

| | |
|---|---|
| ✅ Verified | Windows 11 (PowerShell, Git Bash, pip venv); folders, sync folders, a simulated rclone, and fault-injected storage; 67 automated tests, including real parallel pushes from 3 processes |
| 🟡 Implemented, not yet run live | macOS and Linux launchers; live Google Drive, OneDrive, Dropbox, Box, S3 and WebDAV accounts through rclone |
| ⬜ Not yet | desktop GUI (any Git GUI works meanwhile), key rotation, organization and hardware keys |

Every PRD requirement, its status and the test that proves it:
[docs/REQUIREMENTS.md](docs/REQUIREMENTS.md).

## Docs

| | |
|---|---|
| [SPEC.md](docs/SPEC.md) | on-storage format, push protocol, guarantees and limits |
| [REQUIREMENTS.md](docs/REQUIREMENTS.md) | PRD traceability |
| [PROVIDERS.md](docs/PROVIDERS.md) | storage setup, per provider |
| [RECOVERY.md](docs/RECOVERY.md) | recovery, migration, leaving |
| [AGENTS.md](docs/AGENTS.md) | agent mode, Python API, Claude Code hooks |
| [PRD](PRD%20Iron%20Repository.md) | the original product requirements |

## Hacking on Iron

```bash
python -m pip install pytest cryptography
python -m pytest            # 67 tests, a few minutes
```

```
bin/              zero-install launchers (iron, iron.cmd, git-remote-iron)
iron/
  remote_helper.py   the git transport
  remote.py          remote format and compare-and-swap log
  recover.py         replay rules + standalone recovery (copied into every remote)
  sync.py            bundle creation and application
  storage/           provider interface, folder, rclone, fault simulator, compliance suite
  checkpoint.py      checkpoints and restore
  maintenance.py     verify, migrate, compact
  cli.py, api.py     the iron command and the agent API
tests/            protocol, git end to end, CLI, storage compliance
```

<div align="center">

**MIT licensed. Your code is yours. Keep it that way.**

</div>
