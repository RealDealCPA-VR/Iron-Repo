# Recovery, migration and leaving

The sovereignty test from the PRD: *if the Iron project disappears tomorrow,
can the user still recover and migrate everything?* Yes, in four independent
ways.

## 1. New computer, Iron installed

```bash
iron clone onedrive://Iron-Repos/app.iron       # or any location
```

You get every file, commit, branch and tag, plus checkpoints that were pushed
with `iron checkpoint --push`. Encrypted repository? Iron asks for the
recovery key (or pass `--key iron1-…`).

## 2. Iron is gone: only Python and git

Every remote contains its own recovery tool:

```bash
# get the folder onto this computer (sync app, the provider's web "Download", or rclone copy)
python app.iron/iron-recover.py app.iron restored-app
python app.iron/iron-recover.py app.iron restored-app --key iron1-...   # encrypted
# the target must be a new or empty folder; the tool never overwrites existing work
```

`iron-recover.py` is a single file that uses only the standard library
(`cryptography` is needed only for encrypted remotes). It verifies every hash
and rebuilds an ordinary Git repository with all refs.

## 3. Iron is gone and so is Python: plain git

Unencrypted bundles are standard git bundles:

```bash
git bundle list-heads app.iron/bundles/<hash>.bundle
```

Bundles carry real ref names (`refs/heads/main`, tags). After `iron compact`,
the newest bundle is a snapshot that contains everything:

```bash
git clone app.iron/bundles/<snapshot-hash>.bundle restored      # snapshots include HEAD
```

Without a snapshot, apply the bundles in log order. Each `log/*.json` record
names its bundle, and [SPEC.md](SPEC.md) explains how to read the log.

## 4. An independent export

```bash
iron export app-2026-10-21.bundle     # every branch, tag and checkpoint, in one file
git clone app-2026-10-21.bundle restored
```

Keep one on a USB disk from time to time.

## Proving it works, before you need it

```bash
iron verify --deep
```

This downloads everything from the remote alone into a scratch folder,
rebuilds the repository, sets every ref and runs `git fsck --full`. It reports
missing or corrupted bundles, unreadable records, races that were detected,
and leftover locks.

## Moving to other storage

```bash
iron migrate D:/Backups     # or: iron migrate //nas/share, iron migrate gdrive:, iron migrate onedrive
```

This follows PRD section 18:

1. enumerate every bundle and log entry;
2. verify each hash at the source;
3. copy it;
4. read it back from the destination and verify it;
5. copy the marker, README and recovery tool last;
6. check that the destination replays to the same history;
7. switch the remote URL (`--keep-url` copies without switching);
8. leave `MOVED.json` in the old copy. Other computers that still push there
   are refused and shown the new location, so the two copies never drift apart.

The source is never changed apart from that one `MOVED.json` note. Encrypted
repositories migrate without being decrypted. If it gets interrupted, run it again: files already present are
skipped.

## Leaving Iron entirely

Your local repository is a normal Git repository. To move to any other host:

```bash
git remote add elsewhere https://example.com/me/app.git
git push elsewhere --all && git push elsewhere --tags
```

## Losing the encryption key

The recovery key is printed once, when the encrypted remote is created. Each
computer keeps a copy under `~/.iron/keys/`. To print it again on any of those
computers:

```bash
iron key show
```

If every copy is lost, the encrypted remote cannot be read by anyone. That is
the point of encryption. Your local clones are unaffected, so push them to a
new remote.
