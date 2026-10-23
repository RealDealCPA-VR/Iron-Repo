"""iron connect: rclone download and verification, remote creation, the compliance check.

Nothing here reaches the internet: rclone "downloads" come from a local http.server on
127.0.0.1, and "rclone" itself is tests/fake_rclone.py with its config in a temp file.
"""

import configparser
import hashlib
import http.server
import io
import os
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from conftest import ROOT, iron, make_repo
from iron import connect
from iron.errors import IronError
from iron.storage.rclone import installed_rclone, rclone_command

FAKE = ROOT / "tests" / "fake_rclone.py"
VERSION = "1.99.0"


@pytest.fixture
def cenv(env, monkeypatch):
    """The isolated world from conftest, plus an rclone config file of its own."""
    monkeypatch.setenv("RCLONE_CONFIG", str(env / "rclone-config" / "rclone.conf"))
    for var in ("SSH_CONNECTION", "SSH_TTY", "IRON_RCLONE_DOWNLOAD_BASE"):
        monkeypatch.delenv(var, raising=False)
    if sys.platform.startswith("linux"):
        monkeypatch.setenv("DISPLAY", ":0")
    return env


def folders(env):
    """Every folder in the fake cloud, relative and with forward slashes (one per remote not counted)."""
    cloud = env / "fake-cloud"
    out = []
    for path in sorted(cloud.rglob("*")) if cloud.is_dir() else []:
        rel = path.relative_to(cloud).as_posix()
        if path.is_dir() and "/" in rel:
            out.append(rel)
    return out


def remotes(env):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(str(env / "rclone-config" / "rclone.conf"), encoding="utf-8")
    return {name: dict(cfg[name]) for name in cfg.sections()}


# -- a local stand-in for downloads.rclone.org --------------------------------

def fake_binary():
    """The 'rclone' inside the fake zip: fake_rclone.py, runnable directly on macOS/Linux."""
    return ("#!%s\n" % sys.executable).encode() + FAKE.read_bytes()


def build_zip(system, arch):
    exe = "rclone.exe" if os.name == "nt" else "rclone"
    folder = "rclone-v%s-%s-%s/" % (VERSION, system, arch)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(folder + exe, fake_binary())
        z.writestr(folder + "README.txt", "docs\n")
        z.writestr(folder + "rclone.1", "man page\n")
        z.writestr("../escape.txt", "must never be written\n")
    return buf.getvalue()


@pytest.fixture
def downloads(cenv, monkeypatch, tmp_path):
    """Serve version.txt, the zip and SHA256SUMS from 127.0.0.1; returns a control dict."""
    try:
        system, arch = connect.platform_tag()
    except IronError:
        pytest.skip("no rclone build name for this platform")
    data = build_zip(system, arch)
    state = {"requests": [], "data": data, "sha": hashlib.sha256(data).hexdigest(), "listed": True,
             "zip": "/v%s/rclone-v%s-%s-%s.zip" % (VERSION, VERSION, system, arch)}

    class Handler(http.server.BaseHTTPRequestHandler):
        def _body(self):
            if self.path == "/version.txt":
                return b"rclone v%s\n" % VERSION.encode()
            if self.path == state["zip"]:
                return state["data"]
            if self.path == "/rclone-current-%s-%s.zip" % (system, arch):
                return b"a newer release than version.txt said: must never be used"
            if self.path == "/v%s/SHA256SUMS" % VERSION:
                lines = ["-----BEGIN PGP SIGNED MESSAGE-----", "Hash: SHA1", "",
                         "%s  rclone-v%s-linux-mips.zip" % ("0" * 64, VERSION)]
                if state["listed"]:
                    lines.append("%s  rclone-v%s-%s-%s.zip" % (state["sha"], VERSION, system, arch))
                lines += ["-----BEGIN PGP SIGNATURE-----", "xyz", "-----END PGP SIGNATURE-----"]
                return ("\n".join(lines) + "\n").encode()
            return None

        def _answer(self, with_body):
            state["requests"].append((self.command, self.path))
            body = self._body()
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if with_body:
                self.wfile.write(body)

        def do_GET(self):
            self._answer(True)

        def do_HEAD(self):
            self._answer(False)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("IRON_RCLONE_DOWNLOAD_BASE", "http://127.0.0.1:%d" % server.server_address[1])
    monkeypatch.delenv("IRON_RCLONE", raising=False)
    yield state
    server.shutdown()
    server.server_close()


# -- download, verification and the rclone search order ------------------------

def test_download_verifies_and_installs_only_the_binary(downloads, cenv):
    path = connect.install_rclone(assume_yes=True)
    assert path == installed_rclone()
    assert Path(path).parent == cenv / "iron-home" / "bin"
    assert Path(path).read_bytes() == fake_binary()
    assert os.listdir(str(Path(path).parent)) == [os.path.basename(path)]   # no zip, docs or temp files
    assert not (cenv / "escape.txt").exists() and not (cenv / "iron-home" / "escape.txt").exists()
    # version.txt first, then checksum and zip from that one release folder; no HEAD, no rclone-current
    assert downloads["requests"] == [("GET", "/version.txt"), ("GET", "/v%s/SHA256SUMS" % VERSION),
                                     ("GET", downloads["zip"])]
    if os.name != "nt":
        assert os.access(path, os.X_OK)
    assert rclone_command() == [path]   # Iron now uses it without being told


def test_checksum_mismatch_is_refused(downloads, cenv):
    downloads["sha"] = "f" * 64
    with pytest.raises(IronError, match="does not match its published checksum"):
        connect.install_rclone(assume_yes=True)
    bin_dir = cenv / "iron-home" / "bin"
    assert not Path(installed_rclone()).exists()
    assert not bin_dir.exists() or os.listdir(str(bin_dir)) == []   # the bad zip was deleted too


def test_unlisted_download_is_refused(downloads, cenv):
    downloads["listed"] = False
    with pytest.raises(IronError, match="not listed in the published SHA256SUMS"):
        connect.install_rclone(assume_yes=True)
    assert not Path(installed_rclone()).exists()
    assert ("GET", downloads["zip"]) not in downloads["requests"]


def test_no_download_without_consent(downloads, cenv, monkeypatch):
    with pytest.raises(IronError, match="not downloading without your go-ahead"):
        connect.install_rclone(assume_yes=False)
    assert downloads["requests"] == []          # not even a HEAD request
    out = iron(cenv, "connect", "dropbox", check=False)
    assert out.returncode == 1 and "--yes" in out.stderr
    assert "/v<version>/rclone-v<version>-" in out.stdout and "/version.txt" in out.stdout   # the URL, shown first
    assert downloads["requests"] == [] and not Path(installed_rclone()).exists()

    # asked in a terminal: nothing is requested before the answer, and nothing after a no
    seen = []
    monkeypatch.setattr(connect, "interactive", lambda: True)
    monkeypatch.setattr(connect, "confirm", lambda *a, **k: seen.append(list(downloads["requests"])) or False)
    with pytest.raises(IronError, match="rclone is needed"):
        connect.install_rclone(assume_yes=False)
    assert seen == [[]] and downloads["requests"] == []


def test_only_https_or_this_computer(monkeypatch):
    monkeypatch.setenv("IRON_RCLONE_DOWNLOAD_BASE", "http://downloads.example.com")
    with pytest.raises(IronError, match="only https"):
        connect.download_base()
    monkeypatch.setenv("IRON_RCLONE_DOWNLOAD_BASE", "https://mirror.example.com/")
    assert connect.download_base() == "https://mirror.example.com"
    monkeypatch.delenv("IRON_RCLONE_DOWNLOAD_BASE")
    assert connect.download_base() == "https://downloads.rclone.org"


def test_rclone_search_order(cenv, monkeypatch):
    monkeypatch.delenv("IRON_RCLONE", raising=False)
    assert rclone_command() == ["rclone"]                  # nothing installed: rclone on PATH
    own = Path(installed_rclone())
    own.parent.mkdir(parents=True)
    own.write_bytes(b"x")
    assert rclone_command() == [str(own)]                  # Iron's own copy beats PATH
    monkeypatch.setenv("IRON_RCLONE", "/opt/my/rclone")
    assert rclone_command() == ["/opt/my/rclone"]          # an explicit choice beats both


@pytest.mark.skipif(os.name == "nt", reason="the fake rclone in the zip is a script, runnable only via #!")
def test_connect_from_nothing(downloads, cenv):
    out = iron(cenv, "connect", "google-drive", "--yes")
    assert "Checksum verified" in out.stdout and "All 8 checks passed" in out.stdout
    assert remotes(cenv)["gdrive"]["type"] == "drive"


# -- connecting ---------------------------------------------------------------

def test_connect_google_drive(cenv):
    out = iron(cenv, "connect", "google-drive")
    assert "browser" in out.stdout and "Iron never sees it" in out.stdout
    assert "All 8 checks passed" in out.stdout
    assert "iron setup --to gdrive:" in out.stdout
    assert remotes(cenv)["gdrive"] == {"type": "drive"}
    cloud = cenv / "fake-cloud" / "gdrive" / "Iron-Repos"
    assert folders(cenv) == []                              # the check cleaned up after itself, folders too

    assert "gdrive:" in iron(cenv, "connect", "--list").stdout
    again = iron(cenv, "connect", "google-drive")
    assert "already set up" in again.stdout and "All 8 checks passed" in again.stdout

    repo = make_repo(cenv / "app")
    iron(repo, "setup", "--to", "gdrive:", "--yes")
    assert (cloud / "app.iron" / "iron.json").is_file()
    # with repositories in Iron-Repos, a new check removes only its own folders
    assert "All 8 checks passed" in iron(cenv, "connect", "google-drive").stdout
    assert (cloud / "app.iron" / "iron.json").is_file()
    assert not [f for f in folders(cenv) if ".connect-check" in f or "iron-compliance-" in f]


def test_connect_name_conflict_and_bad_names(cenv):
    iron(cenv, "connect", "onedrive", "--name", "work")
    out = iron(cenv, "connect", "dropbox", "--name", "work", check=False)
    assert out.returncode == 1 and "already exists for OneDrive" in out.stderr
    out = iron(cenv, "connect", "dropbox", "--name", "-rf", check=False)
    assert out.returncode != 0
    out = iron(cenv, "connect", "dropbox", "--name", "a b", check=False)
    assert out.returncode == 1 and "not a usable remote name" in out.stderr


def test_connect_s3_needs_keys_and_bucket(cenv):
    out = iron(cenv, "connect", "s3", "--bucket", "my-bucket", check=False)
    assert out.returncode == 1 and "access_key_id, secret_access_key" in out.stderr
    assert "s3" not in remotes(cenv)
    out = iron(cenv, "connect", "s3", "--set", "access_key_id=AK", "--set", "secret_access_key=SK", check=False)
    assert out.returncode == 1 and "--bucket" in out.stderr
    out = iron(cenv, "connect", "s3", "--bucket", "my-bucket", "--set", "access_key_id=AK",
               "--set", "secret_access_key=SK", "--set", "endpoint=https://minio.example.test")
    assert "All 8 checks passed" in out.stdout and "iron setup --to s3:my-bucket" in out.stdout
    assert folders(cenv) == ["s3/my-bucket"]                # the bucket stays; the check's folders do not
    assert remotes(cenv)["s3"] == {"type": "s3", "provider": "AWS", "access_key_id": "AK",
                                   "secret_access_key": "SK", "endpoint": "https://minio.example.test"}


def test_connect_headless_uses_rclone_authorize(cenv, monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 22 10.0.0.2 22")
    out = iron(cenv, "connect", "box")
    assert 'rclone authorize "box"' in out.stdout
    assert remotes(cenv)["box"]["config_is_local"] == "false"


def test_failed_sign_in_is_reported(cenv, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_FAIL_CREATE", "1")
    out = iron(cenv, "connect", "pcloud", check=False)
    assert out.returncode == 1 and "could not set up 'pcloud'" in out.stderr and "--remove pcloud" in out.stderr


def test_cleanup_leaves_no_folders_even_when_checks_fail(cenv, monkeypatch):
    """Through the real RcloneProvider and the fake rclone: every rmdir really runs."""
    iron(cenv, "connect", "dropbox")
    assert folders(cenv) == []
    import iron.storage.compliance as compliance
    real = compliance.run_compliance

    def crashing(provider):
        real(provider)
        raise RuntimeError("storage went away")         # the suite crashes after its own cleanup
    monkeypatch.setattr(connect, "run_compliance", crashing)
    with pytest.raises(RuntimeError):
        connect.check_remote("dropbox:" + connect.CHECK_FOLDER)
    assert folders(cenv) == []


def test_failing_storage_is_reported_in_plain_words(cenv, monkeypatch):
    def broken(provider):
        return [("binary round-trip", True, ""), ("listing", False, "AssertionError: listing is wrong")]
    monkeypatch.setattr(connect, "run_compliance", broken)
    monkeypatch.setattr(connect, "open_location", lambda loc: type("P", (), {"remove_dir": lambda s, p: None})())
    assert not connect.check_remote("x:Iron-Repos/.connect-check")


def test_list_and_remove(cenv):
    iron(cenv, "connect", "dropbox")
    assert "dropbox:" in iron(cenv, "connect", "--list").stdout
    out = iron(cenv, "connect", "--remove", "dropbox", check=False)
    assert out.returncode == 1 and "Nothing removed" in out.stdout         # never without a yes
    assert "dropbox" in remotes(cenv)
    out = iron(cenv, "connect", "--remove", "dropbox:", "--yes")
    assert "Files already in the cloud stay there" in out.stdout
    assert "dropbox" not in remotes(cenv)
    out = iron(cenv, "connect", "--remove", "dropbox", "--yes", check=False)
    assert out.returncode == 1 and "no rclone remote called" in out.stderr


def test_list_without_rclone(cenv, monkeypatch):
    monkeypatch.setenv("IRON_RCLONE", str(cenv / "no-such-rclone"))
    assert "rclone is not installed" in iron(cenv, "connect", "--list").stdout
