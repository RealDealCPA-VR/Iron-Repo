# iron connect: a direct cloud connection in one command

```bash
iron connect google-drive        # or onedrive, dropbox, box, pcloud, s3, b2, webdav
iron setup --to gdrive:          # then, inside a project folder
```

`iron connect` turns "I have a Google account" into a working, tested Iron
location without installing a desktop app. It uses [rclone](https://rclone.org)
underneath, like every direct-API location (see [PROVIDERS](PROVIDERS.md)).

## What it does

1. **Finds rclone.** It uses, in this order: `IRON_RCLONE` if you set it, the
   copy Iron installed in `<IRON_HOME>/bin` (`~/.iron/bin`), then `rclone` on
   your PATH. If you already have rclone, nothing is downloaded. If you have
   both your own rclone and Iron's copy and prefer yours, set
   `IRON_RCLONE=rclone` or delete `~/.iron/bin/rclone`.
2. **Offers to install rclone if it is missing.** It always asks first and
   shows the URL and size. Without a terminal to ask in, the answer is no
   unless you pass `--yes`. See [Download and verification](#download-and-verification).
3. **Creates the remote** with `rclone config create <name> <type>`.
   - Google Drive, OneDrive, Dropbox, Box and pCloud use OAuth. rclone opens
     your browser, you sign in to the provider, and rclone stores the token in
     its own config on this computer. Iron never sees the token.
   - S3, B2 and WebDAV ask for keys or a URL (secrets are read without echo)
     and pass them to rclone, which keeps them in its config. S3 and B2 also
     need `--bucket`.
4. **Checks the storage** by running the provider compliance suite (the same
   one as `iron doctor --deep`) in `<name>:Iron-Repos/.connect-check`. It
   writes a few test files, reads them back, lists, deletes, and removes them
   again. Results are reported in plain words.
5. **Prints the next step**, `iron setup --to <name>:`, and inside a
   repository offers to run it (default no).

The remote names are `gdrive`, `onedrive`, `dropbox`, `box`, `pcloud`, `s3`,
`b2` and `webdav`; choose another with `--name`. Running `iron connect` again
for a remote that exists just re-runs the check.

## Options

| | |
|---|---|
| `--name NAME` | rclone remote name |
| `--bucket B` | bucket for S3 and B2 (`iron setup --to s3:B`) |
| `--set KEY=VALUE` | answer a question without a prompt, e.g. `--set access_key_id=...` (visible in shell history; prefer the prompt) |
| `--headless` | no browser on this computer; see below |
| `--yes`, `-y` | allow the rclone download, or a `--remove`, without asking |
| `--list` | configured rclone remotes and their types |
| `--remove NAME` | forget a remote, after asking. Files in the cloud are not touched |

## Headless computers (servers, SSH sessions)

Over SSH, or on Linux without a display, `iron connect` (or `--headless`)
asks rclone to sign in elsewhere. rclone prints a command like

```bash
rclone authorize "drive"
```

Run it on any computer with a browser and rclone, sign in there, and paste the
token it prints back into the waiting prompt. The token goes straight to
rclone.

## Download and verification

When rclone is missing and you agree:

1. Iron downloads `https://downloads.rclone.org/rclone-current-<os>-<arch>.zip`
   (for example `windows-amd64`, `osx-arm64`, `linux-amd64`).
2. It reads the current version from `https://downloads.rclone.org/version.txt`
   and fetches `https://downloads.rclone.org/v<version>/SHA256SUMS`.
3. The SHA-256 of the zip must match the line for
   `rclone-v<version>-<os>-<arch>.zip`. If it does not match, or the file is not
   listed, nothing is installed and the download is deleted.
4. Only the `rclone` executable is copied out of the zip, to
   `<IRON_HOME>/bin/rclone` (`rclone.exe` on Windows). Nothing else in the
   archive is unpacked, so archive paths cannot write elsewhere.

Other safeguards: only `https` is accepted (plain `http` only for
`127.0.0.1`/`localhost`, which the tests use), redirects that change the
scheme are refused, and the download size is capped.

What this does not protect against: the checksum file comes from the same
server as the zip. It catches corrupted or swapped downloads and stale
mirrors, but not a compromised downloads.rclone.org. rclone also signs
SHA256SUMS with PGP; Iron does not check that signature (the Python standard
library cannot). If that matters to you, install rclone yourself and verify the
signature as described at https://rclone.org/release_signing/.

`IRON_RCLONE_DOWNLOAD_BASE` replaces `https://downloads.rclone.org` for all
three URLs, for a mirror you trust.

## Credentials

- OAuth tokens never pass through Iron.
- Keys and passwords for S3, B2 and WebDAV pass through Iron once, on the
  command line of `rclone config create`. They are briefly visible to other
  processes of your user on this computer while it runs. rclone obscures
  WebDAV passwords in its config.
- Everything is stored in rclone's config (`rclone config file` shows where).
  `iron connect --remove NAME` deletes that entry.
