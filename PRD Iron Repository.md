PRD: Iron Repository
Turn Any Cloud Storage Provider Into a Private, Git-Like Repository
Product concept: A provider-agnostic version-control layer that turns an ordinary folder in Google Drive, OneDrive, Dropbox, Box, or similar object/file storage into a user's own private GitHub-like repository.

The cloud provider is storage, not the source of truth for product behavior. Iron Repository owns the repository model, history, commits, branches, metadata, conflict handling, encryption options, and interfaces. Users can switch storage providers without changing how the repository works.

The initial goal should not be to reimplement GitHub. It should be to provide the parts of Git/GitHub that make private source ownership useful: immutable history, commits, branches, rollback, synchronization, diffs, and collaboration.
1. Product Vision
A user should be able to point Iron Repository at:

Google Drive/My Drive/Iron-Repos/MyProject

and have that folder behave conceptually like:

github.com/valentino/MyProject

except the actual repository data lives inside storage the user controls.

The experience should be:
Your code. Your history. Your storage. Your repository.

A user could later move the repository from Google Drive to a NAS, S3-compatible storage, OneDrive, or a local server without rebuilding the repository.
2. Primary Use Cases
Private development
A developer wants Git-like source control without putting the repository on GitHub, GitLab, or another centralized code host.
AI coding
Claude, Codex, local models, or coding agents can safely modify projects while every meaningful change is versioned.
Business sovereignty
A company can maintain internal software repositories using storage infrastructure it already controls.
Agent checkpoints
Agents can automatically create checkpoints:
Agent begins task
      ↓
Create checkpoint
      ↓
Modify files
      ↓
Run validation
      ↓
Commit
      ↓
Store repository objects in user's cloud

Disaster recovery
If an agent destroys a project:
iron restore <commit>

returns the project to the previous state.
3. Core Architecture
The system should have six major layers.
┌─────────────────────────────────────────────┐
│                 USER / AGENT                │
│                                             │
│ CLI │ Desktop UI │ API │ Jarvis │ IDE       │
└─────────────────────┬───────────────────────┘
                      │
              Repository API
                      │
┌─────────────────────▼───────────────────────┐
│             REPOSITORY ENGINE               │
│                                             │
│ Commits │ Branches │ Diffs │ Merge │ Status │
│ Checkout │ Restore │ History │ Tags         │
└─────────────────────┬───────────────────────┘
                      │
┌─────────────────────▼───────────────────────┐
│              OBJECT ENGINE                  │
│                                             │
│ Blob │ Tree │ Commit │ Ref │ Pack │ Index   │
│ Hashing │ Compression │ Deduplication       │
└─────────────────────┬───────────────────────┘
                      │
┌─────────────────────▼───────────────────────┐
│           STORAGE ABSTRACTION               │
│                                             │
│ read() │ write() │ list() │ delete()        │
│ atomic_write() │ lock() │ metadata()        │
└───────────┬─────────┬─────────┬─────────────┘
            │         │         │
       Google      OneDrive   Dropbox
        Drive         │          │
                      │          │
                    Box       Local/NAS

The storage abstraction layer is critical.

Nothing above it should know whether the repository lives on Dropbox, Drive, OneDrive, Box, S3, WebDAV, or a local filesystem.
4. Repository Format
Do not rely on the cloud provider's native version-history system.

Iron Repository maintains its own repository state.

Example:
MyProject/
├── src/
├── tests/
├── README.md
│
└── .iron/
    ├── HEAD
    ├── config
    ├── index
    │
    ├── refs/
    │   ├── heads/
    │   │   ├── main
    │   │   └── feature-x
    │   └── tags/
    │
    ├── objects/
    │   ├── 0a/
    │   ├── 19/
    │   ├── 72/
    │   └── ff/
    │
    ├── packs/
    ├── locks/
    └── metadata/

However, the architecture should support separating the working tree from repository storage.

For example:
Local working directory
C:\Projects\MyProject

could use:
Google Drive
/Iron-Repos/MyProject.iron

as its remote repository.

That becomes important for performance.
5. Content-Addressed Object Store
Files should be stored using cryptographic content hashes.

For example:
SHA-256(file contents)

→

7f83b1657ff1fc53...

The resulting object becomes immutable.
objects/
└── 7f/
    └── 83b1657ff1fc53...

If ten commits contain the exact same file, only one blob needs to exist.

This gives the system:
- deduplication
- corruption detection
- immutable history
- efficient snapshots
- provider independence
SHA-256 would be a sensible default for a new system rather than inheriting Git's historical SHA-1 design.
6. Object Types
The MVP requires four primary objects.
Blob
Raw file contents.
{
  "type": "blob",
  "hash": "...",
  "size": 4821
}

Tree
Directory structure.
{
  "type": "tree",
  "entries": [
    {
      "name": "main.py",
      "type": "blob",
      "hash": "..."
    }
  ]
}

Commit
Immutable repository state.
{
  "type": "commit",
  "tree": "...",
  "parent": "...",
  "author": "Valentino",
  "timestamp": "...",
  "message": "Add agent routing"
}

Ref
Human-readable pointer.
main → a91f283...

7. Local Working Model
Users should not edit directly against cloud files whenever avoidable.

Instead:
Cloud Repository
       ↓
      clone
       ↓
Local Working Directory
       ↓
      edit
       ↓
      commit
       ↓
      push
       ↓
Cloud Repository

This prevents cloud synchronization latency from becoming application latency.

It also avoids depending on Drive/Dropbox/OneDrive desktop sync behavior.
8. Provider Adapter Interface
Every provider implements the same interface.
StorageProvider

authenticate()
read_object()
write_object()
object_exists()
delete_object()
list_objects()
read_ref()
write_ref()
acquire_lock()
release_lock()
get_metadata()
health_check()

Example implementations:
GoogleDriveProvider
OneDriveProvider
DropboxProvider
BoxProvider
LocalProvider
S3Provider
WebDAVProvider

The repository engine must never call Google/Dropbox/etc. directly.
9. Synchronization Model
The synchronization system should behave similarly to distributed Git.

Local repository:
Working Tree
     │
     ▼
Local Object Store
     │
     ▼
Remote Object Store

push determines which local objects do not exist remotely.

Only missing objects are uploaded.

pull determines which remote objects are missing locally.

Only missing objects are downloaded.
10. Concurrency
This is one of the hardest pieces.

Two users might modify:
main

simultaneously.

The system must prevent silent overwrites.

Ref updates should use compare-and-swap semantics conceptually:
Expected remote HEAD:
abc123

Actual remote HEAD:
abc123

Update allowed.

But:
Expected:
abc123

Actual:
def456

REJECT PUSH

The client must pull/merge/rebase before retrying.

Never silently overwrite a branch pointer.
11. Locking
Some storage providers do not offer reliable distributed locking primitives.

Therefore locks should be implemented by Iron Repository.

Example:
.iron/locks/main.lock

Containing:
{
  "owner": "device-1291",
  "created": "...",
  "expires": "...",
  "expected_ref": "abc123"
}

Locks must expire so crashed clients cannot permanently lock repositories.

However, locking alone must not be relied upon for correctness. Ref validation is still required.
12. Encryption
Optional client-side encryption should be part of the architecture from the beginning, even if it ships after MVP.

Flow:
File
 ↓
Hash
 ↓
Compress
 ↓
Encrypt
 ↓
Cloud Provider

The provider sees encrypted objects rather than source code.

Keys remain local.

Potential modes:
None
Repository key
Organization key
Hardware-backed key

This would turn ordinary consumer cloud storage into a considerably more interesting private repository backend.
13. CLI
Initial interface:
iron init

iron status

iron add .

iron commit -m "message"

iron log

iron diff

iron branch

iron checkout

iron merge

iron remote add

iron push

iron pull

iron clone

iron restore

Example:
iron remote add origin google-drive://Iron-Repos/Jarvis

iron push origin main

14. Git Compatibility
Do not attempt full Git compatibility in V1.

However, interoperability should be planned.

Useful future commands:
iron import-git

iron export-git

This means a user can take:
GitHub repository
      ↓
Iron Repository

or:
Iron Repository
      ↓
Git repository

This prevents Iron Repository itself from becoming another lock-in mechanism.

That should be a core design principle.
15. Agent API
This becomes particularly useful for coding agents.

Expose:
repository.status()

repository.checkpoint()

repository.diff()

repository.commit()

repository.restore()

repository.branch()

repository.push()

An agent workflow could therefore become:
Task received
     ↓
Create checkpoint
     ↓
Agent edits project
     ↓
Run tests
     ↓
Inspect diff
     ↓
Commit
     ↓
Push

If tests fail catastrophically:
restore(checkpoint)

This would fit extremely naturally into IJ.
16. Safety Model
Agents should not automatically destroy repository history.

Required protections:
No force push by default
No history deletion by default
No remote branch deletion without confirmation
Automatic checkpoint before agent work
Automatic backup before merge
Immutable commit objects
Hash validation on every downloaded object

17. Desktop UI
The UI should feel much simpler than GitHub.
IRON REPOSITORY

Jarvis
────────────────────────────

main ▼

✓ Synced with OneDrive

Recent Changes

● Add Codex native protocol
  4 minutes ago

● Build session backend
  31 minutes ago

● Add Iron Proxy accounts
  1 hour ago


[ Changes ] [ History ] [ Branches ] [ Storage ]

The product should hide Git terminology where terminology isn't useful.

A non-developer should understand:

Current version

Changes

History

Restore

Storage
18. Provider Migration
This should eventually be a first-class feature.
iron migrate

FROM:
Google Drive

TO:
Local NAS

Process:
Enumerate repository objects
        ↓
Verify hashes
        ↓
Copy objects
        ↓
Verify destination
        ↓
Copy refs
        ↓
Switch remote

The repository should not care where it lives.

That's the sovereignty feature.
19. Required Architecture
I'd build it roughly like this:
iron-repository/
│
├── core/
│   ├── objects/
│   ├── commits/
│   ├── trees/
│   ├── refs/
│   ├── branches/
│   ├── diff/
│   ├── merge/
│   └── index/
│
├── storage/
│   ├── interface/
│   ├── local/
│   ├── google_drive/
│   ├── onedrive/
│   ├── dropbox/
│   └── box/
│
├── sync/
│   ├── push/
│   ├── pull/
│   ├── locking/
│   ├── conflicts/
│   └── integrity/
│
├── crypto/
│   ├── hashing/
│   ├── encryption/
│   └── keys/
│
├── cli/
│
├── api/
│
├── desktop/
│
├── agents/
│
├── tests/
│
└── docs/

I would make the core repository engine completely unaware of UI and storage vendor.

That architectural rule should be enforced aggressively.
20. Implementation TODO
Phase 0 — Specification & invariants
- [ ] Define repository specification
- [ ] Define object serialization format
- [ ] Define SHA-256 hashing rules
- [ ] Define commit format
- [ ] Define tree format
- [ ] Define ref format
- [ ] Define repository configuration
- [ ] Define corruption detection behavior
- [ ] Define concurrency guarantees
- [ ] Write repository invariants as automated tests
Phase 1 — Local repository engine
- [ ] Implement iron init
- [ ] Implement object store
- [ ] Implement blob creation
- [ ] Implement tree creation
- [ ] Implement index
- [ ] Implement commits
- [ ] Implement HEAD
- [ ] Implement refs
- [ ] Implement status
- [ ] Implement log
- [ ] Implement diff
- [ ] Implement checkout
- [ ] Implement restore
- [ ] Add integrity verification
Milestone: fully functional local version-control system.
Phase 2 — Storage abstraction
- [ ] Define StorageProvider
- [ ] Implement provider capability discovery
- [ ] Implement object read/write
- [ ] Implement object enumeration
- [ ] Implement ref read/write
- [ ] Implement retry system
- [ ] Implement rate-limit handling
- [ ] Implement provider health checks
- [ ] Implement simulated unreliable provider for testing
That last one matters. Deliberately simulate:
latency
timeouts
partial writes
duplicate requests
network loss
stale reads

Phase 3 — First remote
Start with one provider, not all four.
- [ ] Implement authentication
- [ ] Create repository container
- [ ] Upload objects
- [ ] Download objects
- [ ] Push
- [ ] Pull
- [ ] Clone
- [ ] Remote refs
- [ ] Conflict detection
- [ ] Concurrent push rejection
- [ ] Interrupted-transfer recovery
Milestone:
Computer A
   ↓
Cloud Drive
   ↓
Computer B

can safely collaborate on the same repository.
Phase 4 — Branching
- [ ] Create branch
- [ ] Delete branch
- [ ] Switch branch
- [ ] Merge
- [ ] Detect conflicts
- [ ] Conflict resolution
- [ ] Fast-forward detection
- [ ] Protect main branch
- [ ] Tags
Phase 5 — Additional providers
- [ ] Google Drive
- [ ] OneDrive
- [ ] Dropbox
- [ ] Box
- [ ] Local filesystem
- [ ] NAS/network share
- [ ] S3-compatible storage
- [ ] WebDAV
Each must pass the same provider compliance test suite.
Phase 6 — Agent integration
- [ ] Agent checkpoint API
- [ ] Agent commit API
- [ ] Agent diff API
- [ ] Agent rollback API
- [ ] Automatic pre-task checkpoint
- [ ] Automatic post-task commit
- [ ] Test validation hooks
- [ ] Agent attribution
- [ ] Session IDs in commits
- [ ] IJ integration
Example history:
Claude
Add storage abstraction
12:31 PM

Codex
Fix OneDrive retry behavior
12:46 PM

GLM
Add provider tests
1:03 PM

Phase 7 — Encryption
- [ ] Repository encryption specification
- [ ] Local key generation
- [ ] Object encryption
- [ ] Key rotation
- [ ] Recovery keys
- [ ] Multi-device key provisioning
- [ ] Encrypted metadata mode
- [ ] Security audit
Phase 8 — Desktop application
- [ ] Repository dashboard
- [ ] History viewer
- [ ] Diff viewer
- [ ] Branch manager
- [ ] Restore UI
- [ ] Storage configuration
- [ ] Sync status
- [ ] Conflict resolution
- [ ] Provider migration
- [ ] Agent activity history
Phase 9 — Sovereignty / portability
- [ ] Git import
- [ ] Git export
- [ ] Provider-to-provider migration
- [ ] Offline repository export
- [ ] Repository verification utility
- [ ] Disaster recovery
- [ ] Zero-provider local mode
- [ ] Document repository specification publicly
21. Definition of Done
The project is successful when I can take a new computer and run:
iron clone <my-private-repository>

authenticate to my own storage provider, and recover:
- every file
- every commit
- every branch
- every tag
- complete history
without Iron Repository operating any central server.

And the ultimate sovereignty test should be:
If the Iron Repository project disappears tomorrow, can the user still recover and migrate everything?

The answer must be yes.