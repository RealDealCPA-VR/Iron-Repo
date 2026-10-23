"""``iron connect <provider>``: from nothing to a working direct-API remote in one command.

1. If rclone is missing, offer to download the official build from
   downloads.rclone.org, check it against the published SHA256SUMS and keep
   only the rclone binary in ``<IRON_HOME>/bin`` (always asks first).
2. ``rclone config create <name> <type>``. For OAuth providers rclone opens the
   browser and keeps the token in its own config on this computer; Iron never
   sees it.
3. Run the provider compliance suite against ``<name>:Iron-Repos/.connect-check``
   and say in plain words whether the storage behaves.

A user's own rclone on PATH (or IRON_RCLONE) is used as is; nothing is downloaded.
"""

import argparse
import getpass
import hashlib
import ipaddress
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from urllib.parse import urlparse

from . import __version__, gitutil, locations
from .cli import BAD, OK, ask, cmd_setup, confirm, interactive, say
from .errors import IronError
from .storage import open_location
from .storage.compliance import run_compliance
from .storage.rclone import installed_rclone, rclone_available, rclone_command

DOWNLOAD_BASE = "https://downloads.rclone.org"
MAX_ZIP_BYTES = 200 * 1024 * 1024
MAX_BINARY_BYTES = 400 * 1024 * 1024
CHECK_FOLDER = "Iron-Repos/.connect-check"


@dataclass
class Question:
    key: str            # rclone option name
    prompt: str
    secret: bool = False
    default: str = ""
    required: bool = True


@dataclass
class Provider:
    kind: str           # rclone backend type
    label: str
    name: str           # default remote name
    oauth: bool = False
    bucket: bool = False   # the location needs a bucket first (S3, B2)
    questions: list = field(default_factory=list)


PROVIDERS = {
    "google-drive": Provider("drive", "Google Drive", "gdrive", oauth=True),
    "onedrive": Provider("onedrive", "OneDrive", "onedrive", oauth=True),
    "dropbox": Provider("dropbox", "Dropbox", "dropbox", oauth=True),
    "box": Provider("box", "Box", "box", oauth=True),
    "pcloud": Provider("pcloud", "pCloud", "pcloud", oauth=True),
    "s3": Provider("s3", "S3-compatible storage", "s3", bucket=True, questions=[
        Question("provider", "Provider (AWS, Cloudflare, Wasabi, Minio, Other)", default="AWS"),
        Question("access_key_id", "Access key ID"),
        Question("secret_access_key", "Secret access key", secret=True),
        Question("region", "Region (blank for the default)", required=False),
        Question("endpoint", "Endpoint URL (blank for AWS)", required=False),
    ]),
    "b2": Provider("b2", "Backblaze B2", "b2", bucket=True, questions=[
        Question("account", "Application key ID"),
        Question("key", "Application key", secret=True),
    ]),
    "webdav": Provider("webdav", "WebDAV", "webdav", questions=[
        Question("url", "Server URL (e.g. https://cloud.example.com/remote.php/dav/files/me)"),
        Question("vendor", "Vendor (nextcloud, owncloud, sharepoint, other)", default="other"),
        Question("user", "User name"),
        Question("pass", "Password (rclone stores it obscured)", secret=True),
    ]),
}
PROVIDER_ALIASES = {"gdrive": "google-drive", "googledrive": "google-drive", "drive": "google-drive",
                    "backblaze": "b2", "nextcloud": "webdav", "owncloud": "webdav"}

PLAIN = {
    "binary round-trip": "files come back byte for byte",
    "overwrite": "a file can be replaced",
    "listing": "folder listings are correct",
    "exists": "it can tell whether a file exists",
    "missing file": "a missing file is reported as missing",
    "3 MB object": "a 3 MB file round-trips",
    "delete": "files can be deleted",
    "health check": "the storage is reachable",
}

NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
VERSION_RE = re.compile(r"^rclone v(\d+\.\d+\.\d+)$")
SUM_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(\S+)$")


def add_arguments(p):
    p.add_argument("provider", nargs="?", help=", ".join(PROVIDERS))
    p.add_argument("--name", help="name for the rclone remote (default: gdrive, onedrive, dropbox ...)")
    p.add_argument("--bucket", help="bucket to use (S3 and B2)")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="answer a provider question without a prompt (visible in shell history)")
    p.add_argument("--headless", action="store_true",
                   help="this computer has no browser: sign in elsewhere with `rclone authorize`")
    p.add_argument("--list", action="store_true", help="show the configured rclone remotes")
    p.add_argument("--remove", metavar="NAME", help="forget an rclone remote (the data in the cloud is kept)")
    p.add_argument("--yes", "-y", action="store_true", help="answer yes (download rclone, remove a remote)")
    return p


# -- rclone download ----------------------------------------------------------

def download_base():
    """downloads.rclone.org, or IRON_RCLONE_DOWNLOAD_BASE (https, or http on this computer only)."""
    base = (os.environ.get("IRON_RCLONE_DOWNLOAD_BASE") or DOWNLOAD_BASE).strip().rstrip("/")
    u = urlparse(base)
    if u.scheme == "https" and u.hostname:
        return base
    if u.scheme == "http" and u.hostname:
        try:
            if u.hostname == "localhost" or ipaddress.ip_address(u.hostname).is_loopback:
                return base
        except ValueError:
            pass
    raise IronError("refusing to download rclone from %r: only https is allowed" % base)


def platform_tag():
    """rclone's name for this OS and CPU, e.g. ('windows', 'amd64')."""
    systems = {"windows": "windows", "darwin": "osx", "linux": "linux", "freebsd": "freebsd",
               "openbsd": "openbsd", "netbsd": "netbsd"}
    arches = {"amd64": "amd64", "x86_64": "amd64", "x64": "amd64", "arm64": "arm64", "aarch64": "arm64",
              "i386": "386", "i686": "386", "x86": "386", "armv7l": "arm-v7", "armv6l": "arm-v6"}
    system, machine = platform.system().lower(), platform.machine().lower()
    if system not in systems or machine not in arches:
        raise IronError("there is no automatic rclone download for %s/%s. Install it from "
                        "https://rclone.org/install/ and run `iron connect` again." % (system, machine))
    return systems[system], arches[machine]


def _open(url):
    req = urllib.request.Request(url, headers={"User-Agent": "iron/%s" % __version__})
    try:
        r = urllib.request.urlopen(req, timeout=60)
    except (urllib.error.URLError, OSError) as e:
        raise IronError("could not download %s: %s" % (url, getattr(e, "reason", e))) from None
    if urlparse(r.geturl()).scheme != urlparse(url).scheme:
        r.close()
        raise IronError("refusing a redirect from %s to %s" % (url, r.geturl()))
    return r


def _fetch_text(url, limit=1024 * 1024):
    with _open(url) as r:
        return r.read(limit).decode("utf-8", errors="replace")


def _remote_size(url):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "iron/%s" % __version__})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return int(r.headers.get("Content-Length") or 0) or None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def latest_version(base):
    text = _fetch_text(base + "/version.txt").strip()
    m = VERSION_RE.match(text)
    if not m:
        raise IronError("unexpected answer from %s/version.txt: %r" % (base, text[:80]))
    return m.group(1)


def expected_sha256(sums_text, filename):
    for line in sums_text.splitlines():
        m = SUM_RE.match(line.strip())
        if m and m.group(2) == filename:
            return m.group(1).lower()
    raise IronError("%s is not listed in the published SHA256SUMS, so it cannot be verified" % filename)


def _download(url, dest):
    digest, size = hashlib.sha256(), 0
    with _open(url) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_ZIP_BYTES:
                raise IronError("the download is larger than expected; stopped")
            digest.update(chunk)
            f.write(chunk)
    return digest.hexdigest()


def _extract_binary(zip_path, dest):
    """Copy only the rclone executable out of the zip, to ``dest``. Nothing else is unpacked."""
    exe = os.path.basename(dest)
    with zipfile.ZipFile(zip_path) as z:
        members = [m for m in z.infolist()
                   if not m.is_dir() and m.filename.replace("\\", "/").rsplit("/", 1)[-1] == exe
                   and m.filename.replace("\\", "/").count("/") <= 1]
        if len(members) != 1:
            raise IronError("the rclone download does not contain exactly one %s" % exe)
        if members[0].file_size > MAX_BINARY_BYTES:
            raise IronError("the rclone binary in the download is implausibly large; stopped")
        tmp = dest + ".partial"
        with z.open(members[0]) as src, open(tmp, "wb") as out:
            shutil.copyfileobj(src, out)
    if os.name != "nt":
        os.chmod(tmp, 0o755)
    os.replace(tmp, dest)


def install_rclone(assume_yes=False):
    """Download, verify and install rclone into <IRON_HOME>/bin. Returns the binary's path."""
    base = download_base()
    system, arch = platform_tag()
    url = "%s/rclone-current-%s-%s.zip" % (base, system, arch)
    dest = installed_rclone()
    say("rclone is not installed. Iron can download the official build:")
    say("  %s" % url)
    if not assume_yes and not interactive():
        raise IronError("not downloading without your go-ahead. Run `iron connect --yes ...` to allow it, "
                        "or install rclone yourself from https://rclone.org/install/")
    size = _remote_size(url)
    say("  size: %s, checked against the SHA256SUMS published by rclone" %
        ("%.1f MB" % (size / 1048576.0) if size else "unknown"))
    say("  installs only the rclone program, into %s" % os.path.dirname(dest))
    if not confirm("Download rclone now?", default=False, assume_yes=assume_yes):
        raise IronError("rclone is needed for a direct connection. Install it from https://rclone.org/install/ "
                        "or use a desktop sync folder instead (`iron setup --to onedrive`).")
    version = latest_version(base)
    sums = _fetch_text("%s/v%s/SHA256SUMS" % (base, version))
    expected = expected_sha256(sums, "rclone-v%s-%s-%s.zip" % (version, system, arch))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, zip_path = tempfile.mkstemp(prefix=".rclone-download-", suffix=".zip", dir=os.path.dirname(dest))
    os.close(fd)
    try:
        say("Downloading rclone v%s..." % version)
        actual = _download(url, zip_path)
        if actual != expected:
            raise IronError("the rclone download does not match its published checksum (expected %s, got %s). "
                            "Nothing was installed." % (expected, actual))
        say("%s Checksum verified (SHA-256 %s)" % (OK, actual))
        _extract_binary(zip_path, dest)
    finally:
        for leftover in (zip_path, dest + ".partial"):
            try:
                os.remove(leftover)
            except OSError:
                pass
    say("%s Installed rclone v%s at %s" % (OK, version, dest))
    return dest


# -- rclone remotes -----------------------------------------------------------

def run_rclone_interactive(args):
    """Run rclone with this terminal attached, so its browser sign-in and prompts reach the user."""
    try:
        return subprocess.call(rclone_command() + args)
    except OSError as e:
        raise IronError("could not run rclone: %s" % e) from None


def headless():
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return True
    return sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _answers(provider, a):
    given = {}
    for item in a.set:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise IronError("--set expects KEY=VALUE, got %r" % item)
        given[key.strip()] = value
    prompting = interactive()
    missing = []
    for q in provider.questions:
        if q.key in given:
            continue
        if prompting:
            label = "%s%s: " % (q.prompt, " [%s]" % q.default if q.default else "")
            value = getpass.getpass(label) if q.secret else ask(label).strip()
            given[q.key] = value or q.default
        elif q.default:
            given[q.key] = q.default
        if q.required and not given.get(q.key):
            missing.append(q.key)
    if missing:
        raise IronError("%s needs: %s. Run `iron connect` in a terminal to be asked, or pass --set KEY=VALUE."
                        % (provider.label, ", ".join(missing)))
    return [k + "=" + v for k, v in given.items() if v != ""]


def _bucket(provider, a):
    if not provider.bucket:
        return ""
    bucket = (a.bucket or "").strip().strip("/")
    if not bucket and interactive():
        bucket = ask("Bucket for your repositories (it must already exist or be creatable): ").strip().strip("/")
    if not bucket or "/" in bucket or bucket.startswith("-"):
        raise IronError("%s needs a bucket: pass --bucket my-bucket" % provider.label)
    return bucket


def create_remote(provider, name, a):
    args = ["config", "create", name, provider.kind] + _answers(provider, a)
    if provider.oauth:
        if a.headless or headless():
            say("This computer looks headless (no browser). rclone will print a command like")
            say("  rclone authorize \"%s\"" % provider.kind)
            say("Run it on a computer that has a browser and rclone, sign in there, and paste the")
            say("token it prints back here. The token goes straight into rclone's config.")
            args.append("config_is_local=false")
        else:
            say("Opening your browser to sign in to %s. rclone keeps the sign-in token in its own" % provider.label)
            say("config on this computer; Iron never sees it.")
    rc = run_rclone_interactive(args)
    if rc != 0:
        raise IronError("rclone could not set up %r (exit %d). Nothing was checked. If a half-made remote "
                        "was left behind, remove it with `iron connect --remove %s`." % (name, rc, name))
    say("%s rclone remote %r is set up (%s)" % (OK, name, provider.label))


def check_remote(target):
    """Run the compliance suite; return True when every check passed."""
    say("Checking that %s behaves like reliable storage (writes a few test files, then removes them)..." % target)
    provider = open_location("rclone:" + target)
    results = run_compliance(provider)
    provider.remove_dir("")
    failed = [r for r in results if not r[1]]
    for check, passed, detail in results:
        words = PLAIN.get(check, check)
        say("  %s %s%s" % (OK if passed else BAD, words, "" if passed or not detail else "  (%s)" % detail))
    if failed:
        say("%s %d of %d checks failed. Iron will not trust this storage until they pass." % (BAD, len(failed), len(results)))
    else:
        say("%s All %d checks passed." % (OK, len(results)))
    return not failed


# -- the command --------------------------------------------------------------

def list_remotes():
    if not rclone_available():
        say("rclone is not installed, so there are no direct connections yet. Try: iron connect google-drive")
        return 0
    remotes = locations.rclone_remotes()
    if not remotes:
        say("No rclone remotes configured yet. Try: iron connect google-drive")
        return 0
    say("rclone remotes (%s):" % " ".join(rclone_command()))
    for name, kind in remotes:
        say(("  %-20s %-10s %s" % (name + ":", kind, locations.LABELS.get(kind, ""))).rstrip())
    return 0


def remove_remote(name, assume_yes):
    if not rclone_available():
        raise IronError("rclone is not installed, so there is nothing to remove")
    if name.endswith(":"):
        name = name[:-1]
    if name not in {n for n, _ in locations.rclone_remotes()}:
        raise IronError("no rclone remote called %r. See `iron connect --list`." % name)
    say("This forgets rclone's sign-in for %r on this computer. Files already in the cloud stay there." % name)
    if not confirm("Remove rclone remote %r?" % name, default=False, assume_yes=assume_yes):
        say("Nothing removed.")
        return 1
    rc = run_rclone_interactive(["config", "delete", name])
    if rc != 0:
        raise IronError("rclone could not remove %r (exit %d)" % (name, rc))
    say("%s Removed rclone remote %r" % (OK, name))
    return 0


def cmd_connect(a):
    if a.list:
        return list_remotes()
    if a.remove:
        return remove_remote(a.remove, a.yes)
    if not a.provider:
        raise IronError("which storage? iron connect <%s>  (or --list, --remove NAME)" % "|".join(PROVIDERS))
    key = PROVIDER_ALIASES.get(a.provider.lower(), a.provider.lower())
    if key not in PROVIDERS:
        raise IronError("unknown provider %r. Choose one of: %s" % (a.provider, ", ".join(PROVIDERS)))
    provider = PROVIDERS[key]
    name = a.name or provider.name
    if not NAME_RE.match(name):
        raise IronError("%r is not a usable remote name: use letters, digits, '-', '_' or '.'" % name)

    if not rclone_available():
        install_rclone(assume_yes=a.yes)

    existing = dict(locations.rclone_remotes())
    if name in existing and existing[name] != provider.kind:
        raise IronError("an rclone remote called %r already exists for %s. Pick another name with --name."
                        % (name, locations.LABELS.get(existing[name], existing[name])))
    bucket = _bucket(provider, a)
    if name in existing:
        say("%s rclone remote %r (%s) is already set up; checking it again." % (OK, name, provider.label))
    else:
        create_remote(provider, name, a)

    base = "%s:%s" % (name, bucket)
    target = base + ("/" if bucket else "") + CHECK_FOLDER
    if not check_remote(target):
        say()
        say("Look at the failures above; `iron connect --remove %s` starts over." % name)
        return 1
    setup_to = base if bucket else name + ":"
    say()
    say("%s %s is connected. Next step, inside a project folder:" % (OK, provider.label))
    say("  iron setup --to %s" % setup_to)
    if gitutil.in_worktree() and interactive() and confirm("Run it now for this repository?", default=False):
        return cmd_setup(argparse.Namespace(to=setup_to, name=None, encrypt=False, remote_name="origin",
                                            yes=False, allow_secrets=False, no_claims=False))
    return 0

