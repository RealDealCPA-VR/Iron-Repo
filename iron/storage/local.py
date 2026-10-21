"""A folder on this computer: a plain directory, a NAS or network share, or a
folder kept in sync by the Google Drive, OneDrive, Dropbox, Box or iCloud app."""

import os
import uuid
from pathlib import Path

from .base import AlreadyExists, NotFound, StorageError, StorageProvider, TransientError

# Errors worth retrying: files briefly locked by sync clients or antivirus, flaky network shares.
_TRANSIENT = (PermissionError, BlockingIOError, InterruptedError, TimeoutError, ConnectionError)


def _error(path, e):
    cls = TransientError if isinstance(e, _TRANSIENT) else StorageError
    return cls("%s: %s" % (path, e))


# Files and folders that sync clients Iron cannot otherwise detect leave behind.
_SYNC_MARKERS = (".stfolder", ".nextcloudsync.log", ".owncloudsync.log", ".megaignore", ".pcloud",
                 ".SynologyWorkingDirectory", ".tresorit", ".dropbox")
_SYNC_GLOBS = (".sync_*.db", "._sync_*.db")  # Nextcloud / ownCloud journals


def synced_by_unknown_client(path):
    """True if this folder or any parent carries a sync client's marker."""
    try:
        p = Path(path).resolve()
    except OSError:
        return True
    for d in [p] + list(p.parents):
        try:
            if any((d / m).exists() for m in _SYNC_MARKERS) or any(next(d.glob(g), None) for g in _SYNC_GLOBS):
                return True
        except OSError:
            continue
    return False


class LocalProvider(StorageProvider):
    name = "local"

    def __init__(self, root, consistent_list=None):
        self.root = Path(os.path.expanduser(str(root)))
        if consistent_list is None:
            from ..locations import sync_folder_kind

            consistent_list = sync_folder_kind(self.root) is None
        self.consistent_list = consistent_list

    def describe(self):
        return str(self.root)

    def _path(self, path):
        parts = [p for p in path.split("/") if p]
        if any(p in (".", "..") for p in parts):
            raise StorageError("invalid storage path: %r" % path)
        return self.root.joinpath(*parts)

    def read(self, path):
        try:
            return self._path(path).read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise NotFound(path) from None
        except OSError as e:
            # Sync clients briefly lock files they are uploading; network shares drop.
            raise _error(path, e) from None

    def write(self, path, data):
        target = self._path(path)
        tmp = target.parent / (".iron-tmp-" + uuid.uuid4().hex)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except OSError as e:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise _error(path, e) from None

    def create_exclusive(self, path, data):
        """Write a temp file, then hard-link it into place: the link either creates the
        complete file atomically or fails because the name is taken."""
        target = self._path(path)
        tmp = target.parent / (".iron-tmp-" + uuid.uuid4().hex)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.link(tmp, target)
        except FileExistsError:
            raise AlreadyExists(path) from None
        except OSError as e:
            raise _error(path, e) from None
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def supports_exclusive(self):
        if not self.consistent_list or synced_by_unknown_client(self.root):
            return False  # a sync client copies files between computers non-atomically
        a = self.root / (".iron-probe-%s" % uuid.uuid4().hex)
        b = self.root / (".iron-probe-%s" % uuid.uuid4().hex)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            a.write_bytes(b"iron")
            os.link(a, b)
            try:
                os.link(a, b)
                return False
            except FileExistsError:
                return b.read_bytes() == b"iron"
        except OSError:
            return False  # e.g. FAT/exFAT USB disks have no hard links
        finally:
            for p in (a, b):
                try:
                    p.unlink()
                except OSError:
                    pass

    def delete(self, path):
        try:
            self._path(path).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            raise _error(path, e) from None

    def list(self, prefix):
        d = self._path(prefix) if prefix else self.root
        try:
            with os.scandir(d) as it:
                return sorted(e.name for e in it if not e.name.startswith(".") and e.is_file())
        except (FileNotFoundError, NotADirectoryError):
            return []
        except OSError as e:
            raise _error(prefix, e) from None

    def remove_dir(self, prefix):
        try:
            os.rmdir(self._path(prefix))
        except OSError:
            pass

    def health_check(self):
        probe_dir = self.root if self.root.exists() else self.root.parent
        if not probe_dir.exists():
            return False, "%s does not exist" % probe_dir
        probe = probe_dir / (".iron-probe-" + uuid.uuid4().hex)
        try:
            probe.write_bytes(b"iron")
            ok = probe.read_bytes() == b"iron"
            probe.unlink()
        except OSError as e:
            return False, "cannot write to %s: %s" % (probe_dir, e)
        return ok, "read/write ok" + ("" if self.consistent_list else " (sync folder)")
