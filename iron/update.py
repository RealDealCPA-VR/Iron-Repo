"""``iron update``: replace this copy of Iron with the latest one, checked before it goes live.

A git checkout is updated with ``git pull --ff-only``. A copy unpacked from an archive (what the
one-line installers make when they download) is replaced by a fresh archive: it is unpacked next
to the old copy, must answer ``iron --version``, and only then swapped in; the old copy is kept
until the new one runs from its final place.
"""

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from . import __version__
from .errors import IronError

OK = "✓"
ARCHIVE_URL = "https://github.com/RealDealCPA-VR/Iron-Repo/archive/refs/heads/main.tar.gz"
SOURCE_FILE = ".iron-source"  # written by the installers: the archive URL this copy came from
ONE_LINERS = {
    "nt": "irm https://raw.githubusercontent.com/RealDealCPA-VR/Iron-Repo/main/install.ps1 | iex",
    "posix": "curl -fsSL https://raw.githubusercontent.com/RealDealCPA-VR/Iron-Repo/main/install.sh | sh",
}
PACKAGES = {  # tool -> package manager -> the command that installs it
    "git": {"winget": "winget install --id Git.Git -e", "brew": "brew install git",
            "apt-get": "sudo apt-get install -y git", "dnf": "sudo dnf install -y git",
            "pacman": "sudo pacman -S --needed git", "zypper": "sudo zypper install -y git"},
    "python": {"winget": "winget install --id Python.Python.3.12 -e", "brew": "brew install python",
               "apt-get": "sudo apt-get install -y python3", "dnf": "sudo dnf install -y python3",
               "pacman": "sudo pacman -S --needed python", "zypper": "sudo zypper install -y python3"},
    "rclone": {"winget": "winget install --id Rclone.Rclone -e", "brew": "brew install rclone",
               "apt-get": "sudo apt-get install -y rclone", "dnf": "sudo dnf install -y rclone",
               "pacman": "sudo pacman -S --needed rclone", "zypper": "sudo zypper install -y rclone"},
}


def say(msg=""):
    print(msg, flush=True)


# -- hints for `iron doctor` ----------------------------------------------------

def install_hint(tool):
    """The one-line command that installs ``tool`` with this computer's package manager, or None."""
    if os.name == "nt":
        managers = ["winget"]
    elif sys.platform == "darwin":
        managers = ["brew"]
    else:
        managers = ["apt-get", "dnf", "pacman", "zypper", "brew"]
    for manager in managers:
        if shutil.which(manager):
            return PACKAGES[tool][manager]
    return None


def reinstall_hint():
    """The one-line installer for this OS."""
    return ONE_LINERS["nt" if os.name == "nt" else "posix"]


# -- update ---------------------------------------------------------------------

def install_root():
    """The folder this Iron runs from (it holds iron/ and bin/)."""
    root = Path(__file__).resolve().parent.parent
    if not (root / "bin" / "iron_launch.py").is_file():
        raise IronError("this Iron was installed as a Python package; update it with the tool that installed it "
                        "(e.g. `pipx upgrade iron-repo`), or reinstall with:  %s" % reinstall_hint())
    return root


def installed_version(root):
    """Run the Iron in ``root`` and return its version; IronError if it does not work."""
    try:
        proc = subprocess.run([sys.executable, str(root / "bin" / "iron_launch.py"), "cli", "--version"],
                              cwd=str(root.parent), capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise IronError("the new Iron in %s does not run: %s" % (root, e)) from None
    words = proc.stdout.split()
    if proc.returncode != 0 or len(words) < 2 or words[0] != "iron":
        detail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no output"]
        raise IronError("the Iron in %s does not run (`iron --version`: %s)" % (root, detail[0]))
    return words[1]


def is_archive(url):
    return url.lower().split("?")[0].endswith((".zip", ".tar.gz", ".tgz"))


def archive_url(root):
    """IRON_INSTALL_URL if it names an archive, else where this copy came from, else GitHub."""
    url = os.environ.get("IRON_INSTALL_URL", "").strip()
    if is_archive(url):
        return url
    try:
        saved = (root / SOURCE_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        saved = ""
    return saved if is_archive(saved) else ARCHIVE_URL


def download(url, dest):
    try:
        with urllib.request.urlopen(url, timeout=120) as resp, open(str(dest), "wb") as out:
            shutil.copyfileobj(resp, out)
    except (OSError, ValueError) as e:
        raise IronError("could not download %s: %s" % (url, e)) from None


def _inside(dest, name):
    target = (dest / name).resolve()
    if target != dest and dest not in target.parents:
        raise IronError("the archive tries to write outside its folder (%s); not using it" % name)


def extract(archive, dest):
    """Unpack a .zip or .tar.gz into ``dest``, refusing links and paths that escape it."""
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(str(archive)):
        with zipfile.ZipFile(str(archive)) as z:
            for name in z.namelist():
                _inside(dest, name)
            z.extractall(str(dest))
        return
    try:
        with tarfile.open(str(archive)) as t:
            members = [m for m in t.getmembers() if m.isfile() or m.isdir()]
            for m in members:
                _inside(dest, m.name)
            kw = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            t.extractall(str(dest), members=members, **kw)
    except tarfile.TarError as e:
        raise IronError("the download is not a .zip or .tar.gz archive (%s)" % e) from None


def find_iron(folder):
    """The folder inside an unpacked archive that holds Iron (GitHub adds one top-level folder)."""
    for d in [folder] + sorted(p for p in folder.iterdir() if p.is_dir()):
        if (d / "iron" / "cli.py").is_file() and (d / "bin" / "iron_launch.py").is_file():
            return d
    raise IronError("the archive does not contain Iron")


def _rename(src, dst, tries=10):
    """os.rename, retried briefly: on Windows a virus scanner may hold new files for a moment."""
    for i in range(tries):
        try:
            os.rename(str(src), str(dst))
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.5)


def swap(root, new_root):
    """Put ``new_root`` where ``root`` is. The old copy comes back if anything goes wrong."""
    backup = root.with_name("%s.old-%d" % (root.name, os.getpid()))
    try:
        _rename(root, backup)
    except OSError as e:
        raise IronError("could not move %s aside (%s). Close programs and terminals using that folder, "
                        "then run `iron update` again." % (root, e)) from None
    try:
        _rename(new_root, root)
        installed_version(root)
    except (OSError, IronError):
        if root.exists():
            shutil.rmtree(str(root), ignore_errors=True)
        _rename(backup, root)
        raise IronError("the new Iron did not work in %s; your previous Iron is back in place" % root) from None
    _rmtree(backup)
    if backup.exists():
        say("! could not remove the previous copy in %s; delete it when convenient" % backup)


def _force_remove(func, path, _exc):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _rmtree(path):
    """Remove a folder, including read-only files; leftovers are not an error."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(str(path), onexc=_force_remove)
    else:
        shutil.rmtree(str(path), onerror=_force_remove)


def update_checkout(root):
    say("Updating %s with git pull --ff-only" % root)
    proc = subprocess.run(["git", "-C", str(root), "pull", "--ff-only"], stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise IronError("`git pull --ff-only` failed in %s (local changes, or a branch that diverged?). "
                        "Sort it out there, or reinstall with:  %s" % (root, reinstall_hint()))
    return installed_version(root)


def update_archive(root):
    url = archive_url(root)
    work = Path(tempfile.mkdtemp(prefix=".%s-update-" % root.name, dir=str(root.parent)))
    try:
        say("Downloading %s" % url)
        download(url, work / "download")
        extract(work / "download", work / "x")
        new_root = find_iron(work / "x")
        for launcher in ("iron", "git-remote-iron"):
            p = new_root / "bin" / launcher
            if p.is_file():
                p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        version = installed_version(new_root)
        (new_root / SOURCE_FILE).write_text(url + "\n", encoding="utf-8")
        swap(root, new_root)
        return version
    finally:
        _rmtree(work)


def fingerprint(root):
    """What is installed, beyond the version number: the commit, or a hash of the code."""
    root = Path(root)
    if (root / ".git").exists():
        proc = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout.strip()
    digest = hashlib.sha256()
    for path in sorted((root / "iron").rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode() + b"|" + path.read_bytes())
    return digest.hexdigest()


def cmd_update(_a):
    root = install_root()
    old, before = __version__, fingerprint(root)
    new = update_checkout(root) if (root / ".git").exists() else update_archive(root)
    if fingerprint(root) == before:
        say("%s Iron %s is already up to date" % (OK, old))
    elif new == old:
        say("%s Updated Iron %s to the latest build" % (OK, old))
    else:
        say("%s Updated Iron %s -> %s" % (OK, old, new))
    return 0
