"""The one-line installers and `iron update`, run against local archives.

Nothing here downloads from the internet, runs a package manager, or touches the real PATH:
HOME, LOCALAPPDATA and IRON_INSTALL_DIR point into a temp folder, IRON_INSTALL_URL points at
archives built from this working copy with `git archive`, and IRON_INSTALL_NO_PATH=1 is set.
"""

import functools
import http.server
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest

from conftest import ROOT, git, run
from iron import __version__, update

WINDOWS = os.name == "nt"


def find_sh():
    """A POSIX sh; on Windows, the one that ships with Git for Windows (not WSL's bash)."""
    if not WINDOWS:
        return shutil.which("sh")
    found = shutil.which("sh")
    if found:
        return found
    git_exe = shutil.which("git")
    for up in (Path(git_exe).resolve().parents if git_exe else []):
        if (up / "usr" / "bin" / "sh.exe").is_file():
            return str(up / "usr" / "bin" / "sh.exe")
    return None


SH = find_sh()
POWERSHELL = shutil.which("powershell") if WINDOWS else None
needs_sh = pytest.mark.skipif(not SH, reason="needs a POSIX sh")
needs_powershell = pytest.mark.skipif(not POWERSHELL, reason="install.ps1 is tested with Windows PowerShell")
PREFIX = "Iron-Repo-main/"


# -- archives built from this working copy --------------------------------------

@pytest.fixture(scope="module")
def source_tar(tmp_path_factory):
    """`git archive` of the working copy as it is now, new files included, without touching the real index."""
    tmp = tmp_path_factory.mktemp("src")
    env = dict(os.environ, GIT_INDEX_FILE=str(tmp / "index"))
    run(["git", "add", "-A"], ROOT, env=env)
    tree = run(["git", "write-tree"], ROOT, env=env).stdout.strip()
    proc = subprocess.run(["git", "archive", "--format=tar", "--prefix=" + PREFIX, tree], cwd=str(ROOT),
                          capture_output=True, check=True)
    return proc.stdout


def repack(tar_bytes, dest, version=None, broken=False):
    """Write the archive as .zip or .tar.gz (by dest's name), optionally with another version or broken."""
    entries = []
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as src:
        for m in src.getmembers():
            if not (m.isfile() or m.isdir()):
                continue
            data = src.extractfile(m).read() if m.isfile() else None
            if m.name == PREFIX + "iron/__init__.py":
                if version:
                    data = re.sub(rb'__version__ = "[^"]*"', b'__version__ = "' + version.encode() + b'"', data)
                if broken:
                    data += b"\nraise SystemExit('this build is broken')\n"
            entries.append((m, data))
    dest = Path(dest)
    if dest.name.endswith(".zip"):
        with zipfile.ZipFile(str(dest), "w", zipfile.ZIP_DEFLATED) as z:
            for m, data in entries:
                if data is not None:
                    info = zipfile.ZipInfo(m.name)
                    info.external_attr = (m.mode & 0o777 | 0o100000) << 16
                    z.writestr(info, data)
    else:
        with tarfile.open(str(dest), "w:gz") as out:
            for m, data in entries:
                if data is None:
                    out.addfile(m)
                else:
                    m.size = len(data)
                    out.addfile(m, io.BytesIO(data))
    return dest


@pytest.fixture
def world(env):
    """Environment for running an installer: temp HOME/LOCALAPPDATA, no PATH edits, no real repo launchers."""
    home = env / "home"
    home.mkdir()
    e = dict(os.environ)
    e["PATH"] = os.pathsep.join(p for p in e["PATH"].split(os.pathsep)
                                if Path(p or ".").resolve() != (ROOT / "bin").resolve())
    if SH and WINDOWS:  # as in Git Bash: the POSIX tools sit next to sh
        e["PATH"] = str(Path(SH).parent) + os.pathsep + e["PATH"]
    e.update(HOME=str(home), LOCALAPPDATA=str(env / "localappdata"), IRON_INSTALL_NO_PATH="1",
             OneDrive="", OneDriveConsumer="", OneDriveCommercial="")
    for var in ("IRON_INSTALL_DIR", "IRON_INSTALL_URL", "IRON_INSTALL_YES", "IRON_INSTALL_DRY_RUN",
                "IRON_INSTALL_PRETEND_MISSING"):
        e.pop(var, None)
    (env / "localappdata").mkdir()
    return e


def version_of(install_dir, env):
    out = run([sys.executable, str(Path(install_dir) / "bin" / "iron_launch.py"), "cli", "--version"],
              Path(install_dir).parent, env=env).stdout
    return out.split()[1]


def launcher_version(install_dir, env):
    """`iron --version` through the platform launcher the installer put in place."""
    if WINDOWS:
        cmd = ["cmd", "/c", str(Path(install_dir) / "bin" / "iron.cmd"), "--version"]
    else:
        cmd = [SH, str(Path(install_dir) / "bin" / "iron"), "--version"]
    return run(cmd, Path(install_dir).parent, env=env).stdout.strip()


def leftovers(install_dir):
    return sorted(p.name for p in Path(install_dir).parent.iterdir() if p.name != Path(install_dir).name)


@pytest.fixture
def http_root(tmp_path):
    """A local http.server serving tmp_path/www; yields (folder, base URL)."""
    www = tmp_path / "www"
    www.mkdir()

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(www)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield www, "http://127.0.0.1:%d/" % server.server_address[1]
    server.shutdown()
    server.server_close()


# -- install.sh -----------------------------------------------------------------

def run_sh_piped(args, env, cwd):
    """Like `curl ... | sh -s -- ARGS`: the script arrives on stdin, with no checkout next to it."""
    script = (ROOT / "install.sh").read_bytes()
    proc = subprocess.run([SH, "-s", "--"] + list(args), input=script, cwd=str(cwd), env=env, capture_output=True)
    return proc.returncode, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")


@needs_sh
def test_sh_one_liner_installs_from_a_tarball(env, world, source_tar):
    archive = repack(source_tar, env / "iron.tar.gz")
    world["IRON_INSTALL_URL"] = archive.as_uri()
    del world["IRON_INSTALL_NO_PATH"]  # HOME is a temp folder, so ~/.local/bin is too
    code, out = run_sh_piped([], world, env)
    assert code == 0, out
    install_dir = Path(world["HOME"]) / ".local" / "share" / "iron"
    assert "Installed Iron into" in out
    assert (install_dir / ".iron-source").read_text().strip() == archive.as_uri()
    for tool in ("iron", "git-remote-iron"):
        assert (Path(world["HOME"]) / ".local" / "bin" / tool).is_file()
    assert launcher_version(install_dir, world) == "iron %s" % __version__
    shim = run([SH, str(Path(world["HOME"]) / ".local" / "bin" / "iron"), "--version"], env, env=world)
    assert shim.stdout.strip() == "iron %s" % __version__

    # running the one-liner again updates in place, verified
    world["IRON_INSTALL_URL"] = repack(source_tar, env / "iron-2.tar.gz", version="9.9.1").as_uri()
    code, out = run_sh_piped([], world, env)
    assert code == 0, out
    assert "%s -> 9.9.1" % __version__ in out
    assert version_of(install_dir, world) == "9.9.1"
    assert leftovers(install_dir) == []


@needs_sh
def test_sh_clones_with_git_and_update_pulls(env, world, source_tar):
    upstream = env / "upstream"
    untar(source_tar, env / "unpacked")
    shutil.move(str(env / "unpacked" / PREFIX.rstrip("/")), str(upstream))
    git(env, "init", "-q", "-b", "main", str(upstream))
    git(upstream, "add", "-A")
    git(upstream, "commit", "-q", "-m", "release")
    install_dir = env / "apps" / "iron"
    world.update(IRON_INSTALL_URL=upstream.as_uri(), IRON_INSTALL_DIR=install_dir.as_posix())
    code, out = run_sh_piped([], world, env)
    assert code == 0, out
    assert "Cloning" in out and (install_dir / ".git").exists()
    assert not (Path(world["HOME"]) / ".local" / "bin" / "iron").exists()  # IRON_INSTALL_NO_PATH

    init = upstream / "iron" / "__init__.py"
    init.write_text(re.sub(r'__version__ = "[^"]*"', '__version__ = "9.9.2"', init.read_text()))
    git(upstream, "commit", "-q", "-am", "bump")
    out = run([sys.executable, str(install_dir / "bin" / "iron_launch.py"), "cli", "update"], env, env=world).stdout
    assert "%s -> 9.9.2" % __version__ in out
    assert version_of(install_dir, world) == "9.9.2"


@needs_sh
def test_sh_from_a_checkout_runs_in_place(env, world):
    proc = subprocess.run([SH, (ROOT / "install.sh").as_posix()], cwd=str(env), env=world, capture_output=True)
    out = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode == 0, out + proc.stderr.decode("utf-8", "replace")
    assert "Not adding launchers" in out and "Next:" in out
    assert not (Path(world["HOME"]) / ".local" / "share" / "iron").exists()


@needs_sh
def test_sh_asks_before_installing_missing_tools(env, world):
    world["IRON_INSTALL_PRETEND_MISSING"] = "git"
    code, out = run_sh_piped([], world, env)
    assert code != 0
    assert "Iron needs Git, which is not installed." in out
    assert "Not installing" in out or "git-scm.com" in out
    assert "[dry run]" not in out  # nobody said yes

    code, out = run_sh_piped(["--yes"], world, env)
    assert code != 0
    assert "[dry run] would run:" in out or "git-scm.com" in out

    world["IRON_INSTALL_PRETEND_MISSING"] = "python"
    code, out = run_sh_piped(["--yes"], world, env)
    assert code != 0 and "Iron needs Python 3.8 or newer" in out


# -- install.ps1 ----------------------------------------------------------------

def run_ps1_file(script_dir, args, env):
    proc = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                           str(Path(script_dir) / "install.ps1")] + list(args),
                          cwd=str(script_dir), env=env, capture_output=True, stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")


@needs_powershell
def test_ps1_one_liner_installs_into_localappdata(env, world, source_tar, http_root):
    www, base = http_root
    repack(source_tar, www / "main.zip")
    world["IRON_INSTALL_URL"] = base + "main.zip"
    script = str(ROOT / "install.ps1").replace("'", "''")
    proc = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                           "Get-Content -Raw -LiteralPath '%s' | Invoke-Expression" % script],
                          cwd=str(env), env=world, capture_output=True, stdin=subprocess.DEVNULL)
    out = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
    install_dir = Path(world["LOCALAPPDATA"]) / "Iron"
    assert "Installed Iron into" in out, out
    assert "Not changing PATH" in out
    for launcher in ("iron.cmd", "iron", "git-remote-iron", "iron_launch.py"):
        assert (install_dir / "bin" / launcher).is_file()
    assert launcher_version(install_dir, world) == "iron %s" % __version__
    assert leftovers(install_dir) == []


@needs_powershell
def test_ps1_rerun_updates_a_downloaded_install(env, world, source_tar):
    lone = env / "downloaded"
    lone.mkdir()
    shutil.copy(str(ROOT / "install.ps1"), str(lone / "install.ps1"))  # no checkout next to it
    install_dir = env / "apps" / "Iron"
    world.update(IRON_INSTALL_DIR=str(install_dir), IRON_INSTALL_URL=repack(source_tar, env / "a.zip").as_uri())
    code, out = run_ps1_file(lone, [], world)
    assert code == 0, out
    assert launcher_version(install_dir, world) == "iron %s" % __version__

    world["IRON_INSTALL_URL"] = repack(source_tar, env / "b.zip", version="9.9.3").as_uri()
    code, out = run_ps1_file(lone, [], world)
    assert code == 0, out
    assert "%s -> 9.9.3" % __version__ in out
    assert launcher_version(install_dir, world) == "iron 9.9.3"
    assert leftovers(install_dir) == []


@needs_powershell
def test_ps1_asks_before_installing_missing_tools(env, world):
    world["IRON_INSTALL_PRETEND_MISSING"] = "git"
    code, out = run_ps1_file(ROOT, [], world)
    assert code == 1
    assert "Iron needs Git, which is not installed." in out
    assert "winget install --id Git.Git -e" in out or "git-scm.com" in out
    assert "[dry run]" not in out

    code, out = run_ps1_file(ROOT, ["-Yes"], world)
    assert code == 1
    assert "[dry run] would run: winget install --id Git.Git -e" in out or "git-scm.com" in out

    world["IRON_INSTALL_PRETEND_MISSING"] = "python"
    code, out = run_ps1_file(ROOT, ["-Yes"], world)
    assert code == 1
    assert "winget install --id Python.Python.3.12 -e" in out or "python.org" in out


@needs_powershell
def test_ps1_from_a_checkout_runs_in_place(env, world):
    code, out = run_ps1_file(ROOT, [], world)
    assert code == 0, out
    assert "Not changing PATH" in out and str(ROOT / "bin") in out
    assert not (Path(world["LOCALAPPDATA"]) / "Iron").exists()


# -- iron update ----------------------------------------------------------------

def untar(tar_bytes, dest):
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as t:
        t.extractall(str(dest), **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))


def unpack_install(source_tar, dest):
    """What a download install looks like: the archive's folder at dest."""
    untar(source_tar, dest.parent / "unpack")
    shutil.move(str(dest.parent / "unpack" / PREFIX.rstrip("/")), str(dest))
    (dest.parent / "unpack").rmdir()
    return dest


@pytest.mark.parametrize("fmt", ["tar.gz", "zip"])
def test_update_swaps_in_a_newer_archive(env, world, source_tar, fmt):
    install_dir = unpack_install(source_tar, env / "apps" / "iron")
    world["IRON_INSTALL_URL"] = repack(source_tar, env / ("new." + fmt), version="9.9.4").as_uri()
    out = run([sys.executable, str(install_dir / "bin" / "iron_launch.py"), "cli", "update"], env, env=world).stdout
    assert "Updated Iron %s -> 9.9.4" % __version__ in out
    assert version_of(install_dir, world) == "9.9.4"
    assert launcher_version(install_dir, world) == "iron 9.9.4"
    assert (install_dir / ".iron-source").read_text().strip() == world["IRON_INSTALL_URL"]
    assert leftovers(install_dir) == []

    # with no IRON_INSTALL_URL, the next update uses the URL the copy came from
    del world["IRON_INSTALL_URL"]
    out = run([sys.executable, str(install_dir / "bin" / "iron_launch.py"), "cli", "update"], env, env=world).stdout
    assert "9.9.4 -> 9.9.4 (already the latest version)" in out


def test_update_keeps_the_old_copy_when_the_new_one_is_broken(env, world, source_tar):
    install_dir = unpack_install(source_tar, env / "apps" / "iron")
    world["IRON_INSTALL_URL"] = repack(source_tar, env / "bad.tar.gz", version="9.9.5", broken=True).as_uri()
    proc = run([sys.executable, str(install_dir / "bin" / "iron_launch.py"), "cli", "update"], env, env=world,
               check=False)
    assert proc.returncode == 1 and "does not run" in proc.stderr
    assert version_of(install_dir, world) == __version__
    assert leftovers(install_dir) == []


def test_update_refuses_a_bad_download(env, world, source_tar):
    install_dir = unpack_install(source_tar, env / "apps" / "iron")
    (env / "junk.tar.gz").write_bytes(b"not an archive")
    world["IRON_INSTALL_URL"] = (env / "junk.tar.gz").as_uri()
    proc = run([sys.executable, str(install_dir / "bin" / "iron_launch.py"), "cli", "update"], env, env=world,
               check=False)
    assert proc.returncode == 1 and "archive" in proc.stderr
    world["IRON_INSTALL_URL"] = (env / "missing.tar.gz").as_uri()
    proc = run([sys.executable, str(install_dir / "bin" / "iron_launch.py"), "cli", "update"], env, env=world,
               check=False)
    assert proc.returncode == 1 and "could not download" in proc.stderr
    assert version_of(install_dir, world) == __version__
    assert leftovers(install_dir) == []


def test_extract_refuses_paths_outside_the_folder(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(str(evil), "w") as z:
        z.writestr("../escaped.txt", "x")
    with pytest.raises(update.IronError):
        update.extract(evil, tmp_path / "out")
    assert not (tmp_path / "escaped.txt").exists()


# -- iron doctor hints ----------------------------------------------------------

def test_install_hints_match_the_package_manager(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda name: "/usr/bin/" + name if name == "apt-get" else None)
    monkeypatch.setattr(update.os, "name", "posix")
    monkeypatch.setattr(update.sys, "platform", "linux")
    assert update.install_hint("git") == "sudo apt-get install -y git"
    assert update.install_hint("rclone") == "sudo apt-get install -y rclone"
    assert update.reinstall_hint().startswith("curl -fsSL")
    monkeypatch.setattr(update.shutil, "which", lambda name: None)
    assert update.install_hint("git") is None


@pytest.mark.skipif(not WINDOWS, reason="winget is Windows-only")
def test_install_hint_on_windows_uses_winget(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda name: "C:/winget.exe" if name == "winget" else None)
    assert update.install_hint("git") == "winget install --id Git.Git -e"
    assert update.install_hint("python") == "winget install --id Python.Python.3.12 -e"
    assert update.reinstall_hint().startswith("irm ")
