"""Finding storage the user already has, and turning friendly names into locations.

``resolve_location`` accepts what a person would naturally type:

    onedrive://Iron-Repos/app.iron     google-drive://Work/app.iron
    dropbox://Iron-Repos/app.iron      box://Iron-Repos/app.iron
    gdrive:Iron-Repos/app.iron         (an rclone remote name)
    D:/Backups/app.iron                (any folder: NAS, USB disk, network share)

A provider alias prefers a configured rclone remote of that type (direct API,
consistent listings) and falls back to the provider's desktop sync folder.
"""

import json
import os
import re
import string
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .errors import IronError

# alias -> rclone backend type
ALIASES = {
    "google-drive": "drive", "gdrive": "drive", "googledrive": "drive", "drive": "drive",
    "onedrive": "onedrive", "dropbox": "dropbox", "box": "box", "icloud": "icloud",
    "s3": "s3", "webdav": "webdav", "sftp": "sftp", "b2": "b2",
}
SYNC_APPS = {"drive", "onedrive", "dropbox", "box", "icloud"}
LABELS = {"drive": "Google Drive", "onedrive": "OneDrive", "dropbox": "Dropbox", "box": "Box",
          "icloud": "iCloud Drive", "s3": "S3", "webdav": "WebDAV", "sftp": "SFTP", "b2": "Backblaze B2"}


@dataclass
class Destination:
    kind: str       # rclone backend type, or "local"
    label: str      # what a person calls it
    location: str   # base location; repositories go in <location>/Iron-Repos/<name>.iron
    via: str        # "sync folder" or "rclone"


def _existing(paths):
    seen, out = set(), []
    for p in paths:
        if not p:
            continue
        p = Path(p)
        try:
            if p.is_dir():
                key = str(p.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    out.append(p)
        except OSError:
            continue
    return out


def _dropbox_paths():
    paths = []
    for base in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA"), str(Path.home() / ".dropbox")):
        if not base:
            continue
        info = Path(base) / "Dropbox" / "info.json" if not base.endswith(".dropbox") else Path(base) / "info.json"
        try:
            data = json.loads(info.read_text(encoding="utf-8"))
            paths += [v.get("path") for v in data.values() if isinstance(v, dict)]
        except (OSError, ValueError):
            pass
    return paths + [str(Path.home() / "Dropbox")]


@lru_cache(maxsize=1)
def sync_folders():
    """Desktop sync folders present on this computer, as Destinations."""
    home = Path.home()
    candidates = {"drive": [], "onedrive": [], "dropbox": _dropbox_paths(), "box": [], "icloud": []}
    if os.name == "nt":
        for letter in string.ascii_uppercase[2:]:
            candidates["drive"].append("%s:\\My Drive" % letter)
        candidates["drive"] += [home / "Google Drive", home / "My Drive"]
        candidates["onedrive"] += [os.environ.get(v) for v in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")]
        candidates["box"].append(home / "Box")
        candidates["icloud"].append(home / "iCloudDrive")
    else:
        cloud = home / "Library" / "CloudStorage"
        if cloud.is_dir():
            for entry in sorted(cloud.iterdir()):
                n = entry.name
                if n.startswith("GoogleDrive-"):
                    candidates["drive"].append(entry / "My Drive")
                elif n.startswith("OneDrive"):
                    candidates["onedrive"].append(entry)
                elif n.startswith("Dropbox"):
                    candidates["dropbox"].append(entry)
                elif n.startswith("Box"):
                    candidates["box"].append(entry)
        candidates["drive"] += [home / "Google Drive", home / "GoogleDrive"]
        candidates["onedrive"].append(home / "OneDrive")
        candidates["box"].append(home / "Box")
        candidates["icloud"].append(home / "Library" / "Mobile Documents" / "com~apple~CloudDocs")
    found = []
    for kind, paths in candidates.items():
        for p in _existing(paths):
            found.append(Destination(kind, LABELS[kind], p.as_posix(), "sync folder"))
    return tuple(found)


def sync_folder_kind(path):
    """The sync provider whose folder contains ``path``, or None."""
    try:
        target = str(Path(path).resolve()).lower()
    except OSError:
        return None
    for dest in sync_folders():
        root = str(Path(dest.location).resolve()).lower()
        if target == root or target.startswith(root.rstrip("\\/") + os.sep) or target.startswith(root + "/"):
            return dest.kind
    return None


def rclone_remotes():
    """[(name, backend type)] for every configured rclone remote."""
    from .storage.rclone import rclone_available, rclone_command

    if not rclone_available():
        return []
    try:
        proc = subprocess.run(rclone_command() + ["listremotes", "--long"], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    out = []
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        name, _, kind = line.partition(":")
        if name.strip():
            out.append((name.strip(), kind.strip()))
    return out


def detect_destinations():
    dests = [Destination(kind, "%s (%s)" % (name, LABELS.get(kind, kind)), name + ":", "rclone")
             for name, kind in rclone_remotes()]
    return dests + list(sync_folders())


def _join(base, rel):
    rel = rel.strip("/")
    if not rel:
        return base
    if base.startswith("rclone:"):
        return base + rel if base.endswith(":") else base.rstrip("/") + "/" + rel
    return (Path(base) / rel).as_posix()


def resolve_location(location):
    """Turn what the user typed into a concrete location for ``iron::<location>``."""
    loc = location.strip()
    m = re.match(r"^([A-Za-z0-9-]+)://(.*)$", loc)
    if m and m.group(1).lower() in ALIASES:
        kind, rel = ALIASES[m.group(1).lower()], m.group(2)
        remotes = [name for name, t in rclone_remotes() if t == kind]
        if remotes:
            return _join("rclone:%s:" % remotes[0], rel)
        folders = [d for d in sync_folders() if d.kind == kind]
        if folders:
            return _join(folders[0].location, rel)
        label = LABELS.get(kind, kind)
        if kind not in SYNC_APPS:
            raise IronError("%s is reached through rclone: run `rclone config create mystore %s`, then use "
                            "mystore:<path> as the location." % (label, kind))
        raise IronError(
            "No %s found on this computer. Either install the %s desktop app and sign in, "
            "or connect it through rclone (`rclone config create mydrive %s`), then try again."
            % (label, label, kind))
    if loc.startswith(("rclone:", "sim:", "file://", "iron://")):
        return loc
    m = re.match(r"^([A-Za-z0-9_.\- ]{2,}):(.*)$", loc)
    if m:
        if m.group(1) in {name for name, _ in rclone_remotes()}:
            return "rclone:" + loc
        raise IronError("%r looks like an rclone remote, but no rclone remote called %r is configured. "
                        "Check `rclone listremotes`, or give a folder path instead." % (loc, m.group(1)))
    return Path(os.path.abspath(os.path.expanduser(loc))).as_posix()


def container_for(to, name):
    """Where repository ``name`` lives when the user says "put it in <to>"."""
    t = to.strip()
    leaf = "Iron-Repos/%s.iron" % name
    if t.rstrip("/").endswith(".iron"):
        return resolve_location(t.rstrip("/"))
    if t.lower() in ALIASES:
        return resolve_location("%s://%s" % (t.lower(), leaf))
    if "://" in t and t.split("://")[0].lower() in ALIASES:
        return resolve_location(t.rstrip("/") + "/" + leaf)
    return _join(resolve_location(t), leaf)


def describe_location(location):
    """A short human label, e.g. 'OneDrive (sync folder) C:/Users/me/OneDrive/Iron-Repos/app.iron'."""
    if location.startswith("rclone:"):
        return "rclone " + location[len("rclone:"):]
    if location.startswith(("sim:", "file://", "iron://")):
        return location
    kind = sync_folder_kind(location)
    if kind:
        return "%s sync folder %s" % (LABELS[kind], location)
    return "folder " + location
