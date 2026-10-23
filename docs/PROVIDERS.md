# Where your repository can live

Iron reaches storage in two ways. Both use the same format, so you can switch
later with `iron migrate`.

| | Sync folder | Direct API (rclone) |
|---|---|---|
| Setup | nothing, if the desktop app is installed | `iron connect google-drive` (fetches rclone if needed, opens your browser to sign in) |
| Works offline | yes; the app uploads later | no |
| Simultaneous pushes from several computers | detected late (see [SPEC](SPEC.md#guarantees-and-limits)) | fully serialized |
| Good for | one person, several computers | teams, servers, CI, headless machines |

Check what Iron can see on this computer:

```bash
iron doctor
```

## Sync folders: Google Drive, OneDrive, Dropbox, Box, iCloud Drive

Install the provider's desktop app and sign in. Then:

```bash
iron setup --to onedrive        # or google-drive, dropbox, box, icloud
```

Iron finds the sync folder by itself: `OneDrive` via `%OneDrive%`, Google Drive
via `X:\My Drive` or `~/Library/CloudStorage/GoogleDrive-*`, Dropbox via its
`info.json`, and so on. The repository goes into
`<sync folder>/Iron-Repos/<name>.iron`. You can also give a path directly:

```bash
iron remote add origin "G:/My Drive/Iron-Repos/app.iron"
```

**Google Drive for desktop:** "Stream files" mode works. "Mirror files" is
faster for large repositories.

**OneDrive "Files On-Demand":** works. Files download when Iron reads them.

## Direct API through rclone (no desktop app needed)

The easy way is `iron connect <provider>` ([CONNECT.md](CONNECT.md)). It fetches a verified rclone if you have none, signs you in, and tests the connection. Doing it by hand also works:

[rclone](https://rclone.org) is a free, open-source, single-binary client for
about 70 storage systems. Its credentials stay in your local rclone config. If
you want no third-party dependency at all, rclone lets you use your own OAuth
client ID.

```bash
rclone config create gdrive drive          # opens a browser to sign in to Google
iron setup --to gdrive:                    # repository goes to gdrive:Iron-Repos/<name>.iron
```

Examples:

| Provider | rclone backend | Iron location |
|---|---|---|
| Google Drive | `drive` | `gdrive:Iron-Repos/app.iron` or `google-drive://Iron-Repos/app.iron` |
| OneDrive / SharePoint | `onedrive` | `od:Iron-Repos/app.iron` |
| Dropbox | `dropbox` | `dbx:Iron-Repos/app.iron` |
| Box | `box` | `box:Iron-Repos/app.iron` |
| S3, MinIO, Backblaze B2, Wasabi, R2 | `s3` / `b2` | `s3:my-bucket/app.iron` |
| WebDAV (Nextcloud, ownCloud, NAS) | `webdav` | `dav:Iron-Repos/app.iron` |
| SFTP server | `sftp` | `server:repos/app.iron` |

A friendly alias such as `google-drive://…` picks the first rclone remote of
that type if one exists, and otherwise falls back to the sync folder.

## Plain folders: NAS, USB disk, network share, local disk

```bash
iron remote add usb E:/Iron-Repos/app.iron
iron remote add nas //nas/backups/app.iron
```

A repository can have any number of remotes. A good setup is one cloud remote
plus one offline disk.

## Checking a provider

```bash
iron doctor --deep
```

This runs the provider compliance suite against each remote: binary
round-trip, overwrite, listing semantics, missing files, a 3 MB object and
deletion. It uses a throwaway folder and removes it afterwards. Every built-in
provider passes the same suite in `tests/test_storage.py`.

## Writing a new provider

Subclass `iron.storage.base.StorageProvider` and implement `read`, `write`,
`delete`, `list` and `health_check`. Raise `NotFound` for missing files and
`TransientError` for anything worth retrying. Register a location prefix in
`iron/storage/__init__.py:open_location`. Then run `run_compliance()` against
it. Atomic writes, locking and compare-and-swap are not needed; Iron builds
those itself.

## Testing with unreliable storage

The `sim:` provider injects faults into any folder:

```bash
git clone "iron::sim:D:/tmp/r.iron?fail=0.2&partial=0.1&duplicate=0.2&stale=0.1&seed=1" test
```

It can inject timeouts and lost acknowledgements (`fail`), truncated writes
(`partial`), repeated requests (`duplicate`), listings that omit recent writes
(`stale`) and random delays (`latency`, in seconds).
