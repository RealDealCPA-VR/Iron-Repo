"""Direct cloud APIs through rclone (https://rclone.org): Google Drive, OneDrive,
Dropbox, Box, S3-compatible storage, WebDAV, SFTP, Backblaze B2 and ~70 more.

rclone is a single open-source binary that keeps its credentials on this
computer. Iron never talks to a vendor SDK itself.
"""

import json
import os
import shutil
import subprocess
import tempfile

from .base import NotFound, StorageError, StorageProvider, TransientError


def rclone_command():
    """The rclone executable as an argv prefix. IRON_RCLONE overrides it (a path, or a JSON list)."""
    raw = os.environ.get("IRON_RCLONE", "").strip()
    if raw.startswith("["):
        return json.loads(raw)
    return [raw or "rclone"]


def rclone_available():
    cmd = rclone_command()
    return bool(shutil.which(cmd[0]) or os.path.isfile(cmd[0]))


def run_rclone(args, data=None):
    try:
        proc = subprocess.run(rclone_command() + args, input=data, capture_output=True)
    except FileNotFoundError:
        raise StorageError("rclone is not installed. Get it from https://rclone.org/install/ "
                           "or use a synced folder instead.") from None
    if proc.returncode == 0:
        return proc.stdout
    err = proc.stderr.decode("utf-8", errors="replace").strip()
    # rclone exit codes: 3 directory not found, 4 file not found, 5 temporary error,
    # 6 less serious error, 8 transfer limit exceeded.
    if proc.returncode in (3, 4) or "object not found" in err.lower() or "directory not found" in err.lower():
        raise NotFound(err)
    if proc.returncode in (5, 6, 8):
        raise TransientError(err)
    raise StorageError("rclone %s failed (exit %d): %s" % (args[0], proc.returncode, err[-800:]))


class RcloneProvider(StorageProvider):
    name = "rclone"

    def __init__(self, target):
        self.target = target if target.endswith(":") else target.rstrip("/")

    def describe(self):
        return "rclone " + self.target

    def _remote(self, path):
        path = path.strip("/")
        if not path:
            return self.target
        if self.target.endswith(":"):
            return self.target + path
        return self.target + "/" + path

    def read(self, path):
        return run_rclone(["cat", self._remote(path)])

    def write(self, path, data):
        run_rclone(["rcat", self._remote(path)], data=data)

    def delete(self, path):
        try:
            run_rclone(["deletefile", self._remote(path)])
        except NotFound:
            pass

    def list(self, prefix):
        try:
            out = run_rclone(["lsf", "--files-only", "--max-depth", "1", self._remote(prefix)])
        except NotFound:
            return []
        return sorted(n for n in out.decode("utf-8").splitlines() if n and not n.startswith("."))

    def remove_dir(self, prefix):
        try:
            run_rclone(["rmdir", self._remote(prefix)])
        except StorageError:
            pass

    def download_many(self, paths, dest_dir):
        if not paths:
            return
        fd, list_file = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(paths) + "\n")
        try:
            run_rclone(["copy", self.target, dest_dir, "--files-from", list_file, "--no-traverse"])
        finally:
            os.unlink(list_file)

    def health_check(self):
        try:
            run_rclone(["lsf", "--max-depth", "1", self._remote("")])
            return True, "reachable"
        except NotFound:
            return True, "reachable (folder not created yet)"
        except StorageError as e:
            return False, str(e)
