<div align="center">

# ⛓️ Iron Repository

### Your code. Your history. Your storage. Your repository.

**Turn the OneDrive, Google Drive, Dropbox, Box, NAS or USB stick you already own
into a private Git remote. No server. No account. No middleman. No lock-in.**

![license](https://img.shields.io/badge/license-MIT-black)
![python](https://img.shields.io/badge/python-3.8%2B-black)
![dependencies](https://img.shields.io/badge/runtime%20deps-zero-black)
![engine](https://img.shields.io/badge/engine-real%20git-black)
![tests](https://github.com/RealDealCPA-VR/Iron-Repo/actions/workflows/ci.yml/badge.svg)
![platforms](https://img.shields.io/badge/CI-Windows%20%7C%20macOS%20%7C%20Linux-black)

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

## Install: one line

**Windows** (PowerShell):
```powershell
irm https://raw.githubusercontent.com/RealDealCPA-VR/Iron-Repo/main/install.ps1 | iex
```
**macOS / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/RealDealCPA-VR/Iron-Repo/main/install.sh | sh
```

That's it. The installer:
- checks for Git and Python 3.8+, and **offers** to install whatever is missing (winget, brew, apt, dnf, pacman or zypper), never without asking;
- puts Iron in a per-user folder (`%LOCALAPPDATA%\Iron` or `~/.local/share/iron`) and on your PATH;
- runs `iron doctor`.

Run it again, or `iron update`, to upgrade in place (it rolls back if the new version fails). Options, uninstalling and installing from a clone: [docs/INSTALL.md](docs/INSTALL.md).

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

## 🖥️ Prefer clicking? `iron ui`

```bash
iron ui
```

This opens a private dashboard in your browser, served from your own computer
and reachable only through a private link that works until you stop it. It works offline and needs
nothing installed. It has:
- **Changes**, with a line-by-line diff and a **Save version** button;
- **History**, with one-click **Restore files to this version**;
- **Checkpoints**;
- **Storage**, with a health check and a full restore test;
- **Agents**, showing who did what.

Every restore asks first and can be undone. Light and dark mode, and it works
on a phone-sized window too. [docs/UI.md](docs/UI.md)

## 💤 Forget about saving: `iron autosave`

```bash
iron autosave --install     # every 5 minutes: save what changed, then sync. Starts at login.
iron autosync on            # or: publish every commit in the background
```

No admin rights needed. It uses a per-user Startup entry on Windows, launchd on
macOS, and a systemd user unit or cron on Linux. It skips during merges, leaves
secret-looking files out, keeps working offline and catches up later, and
stops with instructions on a conflict. `iron autosave --status` shows what it
is doing. [docs/AUTOSAVE.md](docs/AUTOSAVE.md)

## ☁️ No desktop app? `iron connect`

```bash
iron connect google-drive   # or onedrive, dropbox, box, pcloud, s3, b2, webdav
iron setup --to gdrive:
```

If rclone is missing, Iron offers to fetch the official build. It shows the URL
and size, and verifies the published SHA-256 checksum. rclone then opens your
browser to sign in; the token stays in rclone's config on your computer and
Iron never sees it. Iron then runs the storage compliance suite on the new
connection before you trust it with anything. [docs/CONNECT.md](docs/CONNECT.md)

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
That's it: about 6,000 lines of standard-library Python, with no dependencies.

```
 you / agents / IDEs ──► git ──► git-remote-iron ──► push log + bundles ──► StorageProvider
                                                                         ├─ folder / NAS / sync folder
                                                                         ├─ rclone (Drive, S3, Box, …)
                                                                         └─ fault simulator (tests)
```

## Honest status

| | |
|---|---|
| ✅ Verified | CI on Windows, macOS and Linux (Python 3.8/3.9 and 3.13); standalone executables built and smoke-tested on all three, including a Linux machine with no Python at all; folders, sync folders, a simulated rclone, and fault-injected storage; real parallel pushes from 3 processes; the installers; the dashboard's security checks; autosave |
| 🟡 Implemented, not yet run live | live Google Drive, OneDrive, Dropbox, Box, S3 and WebDAV accounts through rclone; the live `irm`/`curl` one-liners; real login registration for autosave |
| ⬜ Not yet | key rotation, organization and hardware keys |

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
| [INSTALL.md](docs/INSTALL.md) | one-line install, prerequisites, updating, uninstalling |
| [UI.md](docs/UI.md) | the browser dashboard and its security model |
| [AUTOSAVE.md](docs/AUTOSAVE.md) | automatic saving and syncing |
| [CONNECT.md](docs/CONNECT.md) | one-command cloud connections through rclone |
| [PLATFORMS.md](docs/PLATFORMS.md) | CI coverage and the standalone executables |
| [PRD](PRD%20Iron%20Repository.md) | the original product requirements |

## Hacking on Iron

```bash
python -m pip install pytest cryptography
python -m pytest            # 120+ tests, 10-20 minutes
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
  ui.py              iron ui: the local browser dashboard
  autosave.py        iron autosave / autosync
  connect.py         iron connect: guided rclone setup
  update.py          iron update
packaging/        PyInstaller builds of standalone executables
.github/workflows/  CI on three OSes, and the standalone builds
tests/            protocol, git end to end, CLI, storage, installers, ui, autosave, connect
```

<div align="center">

**MIT licensed. Your code is yours. Keep it that way.**

</div>
