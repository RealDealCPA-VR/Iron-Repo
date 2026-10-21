# Requirements and traceability

This document maps each requirement in the [PRD](../PRD%20Iron%20Repository.md)
to how Iron meets it and which test proves it.

**Key decision (by the product owner):** use open-source Git as the engine
instead of building a new version-control system. The PRD's own principles
support this: no lock-in (§14), Git compatibility, and a core that knows
nothing about storage vendors (§19). With Git as the engine, the PRD's object
engine and repository engine (§3 to §7) come from Git, and Iron builds the
storage abstraction, sync, safety, encryption, agent and sovereignty layers on
top of it.

**Status key:**
- **Done:** implemented and covered by an automated test.
- **Done (unverified live):** implemented, and tested against simulated or
  local storage, but not yet against a live account at that provider.
- **Partial:** part of the requirement is implemented; the note says what is
  missing.
- **Not done:** not implemented in this release.

Tests live in `tests/`: `P` = test_protocol.py, `G` = test_git_remote.py,
`C` = test_cli.py, `S` = test_storage.py.

## Product vision and use cases (PRD §1, §2)

| Requirement | Status | How | Proof |
|---|---|---|---|
| Point at a cloud folder; it behaves like a private GitHub repository | Done | `iron setup --to onedrive`, `iron remote add`, `git clone iron::…` | C `test_setup_from_a_plain_folder`, G `test_push_and_clone_roundtrip` |
| Repository data lives only in storage the user controls | Done | no server; remotes are plain folders or rclone remotes | entire suite runs offline |
| Move between providers without rebuilding | Done | `iron migrate` | C `test_migrate_to_new_storage_and_keep_working` |
| Private development without a code host | Done | | G, C |
| Agents modify projects safely; checkpoints | Done | `iron agent`, `iron checkpoint`, `iron.api` | C `test_agent_*`, `test_python_agent_api` |
| Disaster recovery: `iron restore <commit>` | Done | `iron restore <id\|last\|commit>` | C `test_restore_undoes_destruction_and_keeps_a_safety_copy` |
| Business sovereignty on existing infrastructure | Done | NAS, network share, S3 or WebDAV via folder or rclone | S `test_compliance[*]` |

## Architecture (PRD §3, §8, §19)

| Requirement | Status | How |
|---|---|---|
| Six layers: interfaces, repository engine, object engine, storage abstraction, providers | Done | interfaces = `iron` CLI, `iron.api`, plain git and IDEs; repository and object engine = Git; storage abstraction = `iron/storage/base.py`; providers = `iron/storage/*` |
| Nothing above the storage layer knows the vendor | Done | `iron/remote.py` uses only `StorageProvider`; vendor access goes only through rclone |
| Provider interface (authenticate, read/write/exists/delete/list objects, read/write ref, lock, metadata, health check) | Done | `read`, `write`, `exists`, `delete`, `list`, `health_check` on providers. Refs are log records (`RemoteRepo.append` / `load_state`), locks are `RemoteRepo.lock`, metadata is `iron.json`. Authentication is delegated to the sync app or rclone, so Iron never handles credentials. |
| Separate working tree from repository storage | Done | the local clone is the working tree; `<name>.iron` is a bare remote |

## Repository format and objects (PRD §4, §5, §6)

| Requirement | Status | How | Proof |
|---|---|---|---|
| Own repository state, not the provider's version history | Done | push log plus bundles ([SPEC](SPEC.md)) | P |
| Content-addressed, immutable objects; deduplication; corruption detection | Done | Git objects are content-addressed. Each push uploads only new objects. Bundles are named by SHA-256 and verified on every download. | C `test_verify_detects_corruption_and_missing_bundles`, G `test_new_branch_pointing_at_existing_commit_uploads_nothing` |
| SHA-256 by default for a new system | Partial | Storage integrity always uses SHA-256. Git objects default to SHA-1 because GitHub and most tools reject SHA-256 repositories, which would conflict with §14. `iron init --sha256` gives full SHA-256. | G `test_sha256_repository` |
| Blob, tree, commit, ref objects | Done | Git's own objects; refs in the log | |

## Working model and sync (PRD §7, §9)

| Requirement | Status | How | Proof |
|---|---|---|---|
| Clone → edit locally → commit → push; no editing against cloud files | Done | standard Git workflow; the cloud holds only the bare remote | G |
| Independent of desktop sync behaviour | Done | write-once unique names, so sync clients never see a conflict; direct API via rclone for no sync app at all | G `test_rclone_backend` |
| Push uploads only missing objects; pull downloads only missing ones | Done | bundles are built against the remote's tips; a local cache of applied bundles avoids repeat downloads | G `test_new_branch_pointing_at_existing_commit_uploads_nothing`, C `test_compact_then_fresh_clone_downloads_one_bundle` |

## Concurrency and locking (PRD §10, §11)

| Requirement | Status | How | Proof |
|---|---|---|---|
| Prevent silent overwrites; compare-and-swap ref updates; reject and require pull | Done | CAS on `old` values; write, check, then confirm | P `test_append_is_compare_and_swap`, G `test_two_computers_collaborate_and_conflicting_push_is_rejected` |
| Correct under real concurrency | Done | 3 processes pushing to one branch in parallel; 4 threads with the lock disabled; a writer frozen between its check and its `.ok` | G `test_parallel_pushers_never_lose_an_acknowledged_push`, P `test_stress_without_lock_every_acknowledged_push_survives`, `test_simultaneous_writers_never_both_confirm`, `test_late_writer_defers_to_confirmed_record`, `test_stalled_writer_cannot_confirm_after_being_retired` |
| Safe under any timing on folder, NAS and USB remotes | Done | claims mode: each position is committed by an atomic exclusive create ([SPEC](SPEC.md#claims-mode-provably-safe)) | P `test_claims_mode_stress_without_lock`, `test_claims_mode_ignores_stalls_and_clocks`, `test_claims_mode_survives_a_lost_claim_file` |
| Safe on rclone remotes without atomic primitives | Partial | confirm protocol with retire handshake. Safe against single stalls and crashes; relies on device clocks agreeing within about 60 to 120 s ([SPEC](SPEC.md#writing-with-the-confirm-protocol)) | P `test_stalled_writer_cannot_confirm_after_being_retired`, `test_third_writer_never_builds_on_a_record_being_retired` |
| Lost confirmation files cannot roll history back | Done | ancestors of confirmed records count | P `test_lost_confirmation_files_do_not_roll_history_back` |
| Locks implemented by Iron; they expire; correctness does not rely on them | Done | `locks/<ms>-<token>.lock`, 60 s lease; a dead holder's lock is ignored; `iron unlock` | P `test_lock_serializes_and_expires` (busy lock, plus a dead lock that is ignored), `test_stale_unconfirmed_record_does_not_block_forever` |
| Never create a repository by accident | Done | pushing to a location with no `iron.json` fails (unmounted disk, typo, half-synced folder) | G `test_push_never_creates_a_repository_implicitly` |
| Eventually consistent folders | Partial | A race between two computers on a desktop sync folder is detected after the fact, and the losing computer is warned. It cannot be prevented without an atomic primitive, which sync folders do not offer. See [SPEC](SPEC.md#guarantees-and-limits). | |

## Encryption (PRD §12, Phase 7)

| Requirement | Status | How | Proof |
|---|---|---|---|
| Optional client-side encryption, designed in from the start | Done | `--encrypt`, AES-256-GCM over every bundle and record | C `test_encrypted_remote` |
| Provider sees only ciphertext; keys stay local | Done | names hash the ciphertext; keys live in `~/.iron/keys`; agents never see `IRON_KEY` | C `test_encrypted_remote` (no plaintext, no `PACK` header, no ref names) |
| Tamper evidence | Partial | Bundles and records are authenticated (AES-GCM). Marker files are not, so a malicious provider can hide recent pushes but cannot forge content ([SPEC](SPEC.md#guarantees-and-limits)). | |
| Repository key mode | Done | one key per repository | |
| Organization key, hardware-backed key | Not done | the format allows other algorithms; not implemented | |
| Recovery keys; multi-device provisioning | Done | printed `iron1-…` key; `iron key import`, `iron clone --key` | C `test_encrypted_remote` |
| Encrypted metadata mode | Done | log records are encrypted too (ref names and authors hidden) | C `test_encrypted_remote` |
| Key rotation | Not done | workaround: `iron remote add new <loc> --encrypt`, then push everything there | |
| Security audit | Not done | needs an external reviewer | |

## CLI (PRD §13)

| Command | Status | Notes |
|---|---|---|
| `iron init`, `status`, `add`, `commit`, `log`, `diff`, `branch`, `checkout` | Done | `init` is Iron's; the others pass through to Git (C `test_passthrough_to_git`) |
| `iron merge` | Done | saves a checkpoint first (C `test_merge_takes_a_checkpoint_first`) |
| `iron remote add origin google-drive://Iron-Repos/Jarvis` | Done | exact PRD syntax supported (S `test_resolve_location`) |
| `iron push origin main`, `pull`, `clone`, `restore` | Done | G, C |
| Additions | Done | `setup`, `sync`, `checkpoint(s)`, `history`, `changes`, `agent`, `verify`, `migrate`, `compact`, `export`, `key`, `unlock`, `doctor`, `where` |

## Git compatibility (PRD §14, Phase 9)

| Requirement | Status | How |
|---|---|---|
| `iron import-git` | Done | it is already Git: `iron remote add origin <loc>` in any existing repository, then `iron sync` |
| `iron export-git` | Done | `git push` to any Git host, or `iron export file.bundle` (C `test_export_is_restorable_by_plain_git`) |
| No lock-in | Done | standard Git locally; standard bundles remotely; [SPEC](SPEC.md) published |

## Agent API and safety (PRD §15, §16, Phase 6)

| Requirement | Status | Proof |
|---|---|---|
| `status`, `checkpoint`, `diff`, `commit`, `restore`, `branch`, `push` | Done | `iron.api.Repository`; C `test_python_agent_api` |
| Checkpoint → edit → test → diff → commit → push; restore on failure | Done | `iron agent --test`; on failure, files and the branch go back (agent commits undone); C `test_agent_success_is_committed_with_attribution`, `test_agent_failure_rolls_back`, `test_agent_rollback_also_undoes_agent_commits` |
| Agent attribution, session IDs in commits | Done | `Iron-Agent:` / `Iron-Session:` trailers and push records; shown in `iron history` |
| IJ integration | Not done | not specified in the PRD; `iron.api` and `IRON_AGENT` / `IRON_SESSION` are the integration points |
| No force push, no history deletion, no remote branch deletion without confirmation | Done | G `test_force_push_and_deletion_are_refused_by_default` |
| Automatic checkpoint before agent work; automatic backup before merge | Done | C `test_agent_*`, `test_merge_takes_a_checkpoint_first`; `iron sync` checkpoints before combining |
| Immutable commits; hash validation on every download | Done | C `test_verify_detects_corruption_and_missing_bundles` |
| Checkpoints include untracked files | Done | C `test_checkpoint_captures_untracked_files_without_touching_anything` |

## Desktop UI (PRD §17, Phase 8)

| Requirement | Status | Notes |
|---|---|---|
| Simple dashboard: branch, sync state, recent changes, actions | Partial | `iron` with no arguments prints the PRD's layout in the terminal (C `test_setup_from_a_plain_folder`). Git terminology is hidden in Iron's own commands (`changes`, `history`, `sync`, `restore`). |
| Desktop application: history, diff and branch views; restore UI; storage config; conflict resolution; migration; agent activity | Not done | Any Git GUI (VS Code, GitHub Desktop, Fork) already works against Iron repositories. A native app can be built on `iron.api`. |

## Provider migration (PRD §18)

| Requirement | Status | Proof |
|---|---|---|
| Enumerate → verify → copy → verify destination → copy refs → switch remote | Done | C `test_migrate_to_new_storage_and_keep_working` |
| Other computers cannot keep writing to the old copy | Done | `MOVED.json` in the old container; pushes there are refused with the new location (C `test_migrated_remote_refuses_stale_pushes`) |

## Implementation phases

| Phase | Status | Notes |
|---|---|---|
| 0 Specification and invariants | Done | [SPEC.md](SPEC.md); invariants as tests (P) |
| 1 Local engine | Done | Git |
| 2 Storage abstraction: interface, capability discovery, retries, rate limits, health, simulator | Done | `consistent_list` capability, `health_check`, `RetryingProvider` with jittered backoff (rate limits are also paced by rclone), `sim:` fault injector (S, G `test_unreliable_storage`) |
| 3 First remote: push, pull, clone, conflicts, concurrent rejection, interrupted transfers | Done | G; interrupted uploads are harmless because they are write-once and hash-checked |
| 4 Branching: create, delete, switch, merge, conflicts, fast-forward, protected main, tags | Done | Git; deletion and force are protected for every branch; tags are immutable (G `test_tags_and_branches`) |
| 5 Providers: Google Drive, OneDrive, Dropbox, Box | Done (unverified live) | Sync-folder detection and the rclone backend are both implemented. The rclone code path is tested through a stand-in rclone binary. Real accounts still need `iron doctor --deep`. |
| 5 Providers: local filesystem, NAS / network share | Done | S, G |
| 5 Providers: S3-compatible, WebDAV | Done (unverified live) | via rclone |
| 5 Same compliance suite for every provider | Done | `iron/storage/compliance.py`, `iron doctor --deep` |
| 6 Agent integration | Done, except IJ | see above |
| 7 Encryption | Partial | see above |
| 8 Desktop app | Not done | terminal dashboard only |
| 9 Sovereignty: Git import and export, migration, offline export, verification, disaster recovery, zero-provider mode, public spec | Done | C `test_recovery_without_iron`, `test_export_is_restorable_by_plain_git`, `test_verify_*`; zero-provider mode is plain Git |

## Definition of done (PRD §21)

> On a new computer, `iron clone <my-private-repository>` recovers every file,
> commit, branch, tag and the complete history, with no central server.

Done. C `test_setup_from_a_plain_folder` and G `test_tags_and_branches` clone
onto a fresh location. `iron verify --deep` rebuilds everything from the
remote alone.

> If the Iron project disappears tomorrow, can the user still recover and
> migrate everything?

Yes. C `test_recovery_without_iron` copies only the remote folder and rebuilds
the repository with `python -I iron-recover.py`. The tool refuses to write
into a non-empty folder (C `test_recovery_tool_refuses_to_overwrite_a_folder`),
and snapshot bundles restore with plain `git clone`
(C `test_snapshot_bundle_restores_with_plain_git`). Python's isolated mode
guarantees the Iron package is not imported. Unencrypted bundles are also
readable by plain `git`. See [RECOVERY.md](RECOVERY.md).

## Non-functional

| Requirement | How |
|---|---|
| Extremely easy setup | Two prerequisites (Git and Python). The installer downloads nothing. `iron setup` detects storage, asks one question, adds a starter `.gitignore` and stops if files look like secrets (C `test_setup_keeps_secrets_out`). `iron doctor` explains anything missing. |
| Portable | Pure Python standard library; zero-install launchers for cmd/PowerShell (`iron.cmd`) and POSIX shells (`iron`, `git-remote-iron`); `.gitattributes` keeps launchers LF; also installable with pip or pipx. **Verified on Windows 11** (PowerShell, Git Bash, pip venv). The macOS and Linux code paths are standard POSIX but have not yet been run on those systems. |
| Sovereign | No server and no account; works offline with folder remotes; credentials stay with the sync app or rclone; Iron never sees them |
