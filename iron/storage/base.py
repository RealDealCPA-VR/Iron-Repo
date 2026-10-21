"""The storage abstraction (PRD section 8).

Nothing above this layer knows whether bytes live in a OneDrive folder, on a NAS,
or behind the Google Drive API. Iron only ever needs five operations on
'/'-separated paths relative to the repository container: read, write, list,
delete and a health check. Every file Iron stores is written once under a
unique name, so providers never need atomic overwrite, locking or
compare-and-swap; those are built on top in ``iron.remote``.
"""

import os
import random
import time

from ..errors import IronError


class StorageError(IronError):
    """A storage operation failed."""


class NotFound(StorageError):
    """The requested object does not exist."""


class TransientError(StorageError):
    """A retryable failure: timeout, rate limit, busy file, lost connection."""


class AlreadyExists(StorageError):
    """create_exclusive found the name taken."""


class StorageProvider:
    name = "abstract"
    #: True when a listing reflects every completed write immediately. Folders
    #: kept in sync by a desktop client are only eventually consistent.
    consistent_list = True

    def describe(self):
        return self.name

    def read(self, path):
        raise NotImplementedError

    def write(self, path, data):
        raise NotImplementedError

    def delete(self, path):
        """Remove a file. Deleting a missing file is not an error."""
        raise NotImplementedError

    def list(self, prefix):
        """Names of the files directly inside ``prefix`` (no directories, no hidden files)."""
        raise NotImplementedError

    def exists(self, path):
        parent, _, leaf = path.rpartition("/")
        return leaf in self.list(parent)

    def create_exclusive(self, path, data):
        """Atomically create a complete file, or raise AlreadyExists. Optional."""
        raise NotImplementedError

    def supports_exclusive(self):
        """True when create_exclusive is atomic on this storage (probed, not assumed)."""
        return False

    def remove_dir(self, prefix):
        """Remove an empty directory if the backend has directories. Best effort."""

    def download_many(self, paths, dest_dir):
        """Copy several files to ``dest_dir`` keeping their relative paths. Backends override to batch."""
        for path in paths:
            target = os.path.join(dest_dir, *path.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(self.read(path))

    def health_check(self):
        """Return (ok, detail)."""
        return True, "ok"


class RetryingProvider(StorageProvider):
    """Retries transient failures with jittered exponential backoff."""

    def __init__(self, inner, attempts=None, base_delay=0.2, max_delay=5.0, sleep=time.sleep):
        self.inner = inner
        self.attempts = attempts or int(os.environ.get("IRON_RETRY_ATTEMPTS", "6"))
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.sleep = sleep

    @property
    def name(self):
        return self.inner.name

    @property
    def consistent_list(self):
        return self.inner.consistent_list

    def describe(self):
        return self.inner.describe()

    def _call(self, fn, *args):
        for attempt in range(self.attempts):
            try:
                return fn(*args)
            except TransientError:
                if attempt == self.attempts - 1:
                    raise
                delay = min(self.max_delay, self.base_delay * 2 ** attempt)
                self.sleep(delay * (0.5 + random.random() / 2))

    def read(self, path):
        return self._call(self.inner.read, path)

    def write(self, path, data):
        return self._call(self.inner.write, path, data)

    def delete(self, path):
        return self._call(self.inner.delete, path)

    def list(self, prefix):
        return self._call(self.inner.list, prefix)

    def exists(self, path):
        return self._call(self.inner.exists, path)

    def create_exclusive(self, path, data):
        return self._call(self.inner.create_exclusive, path, data)

    def supports_exclusive(self):
        return self.inner.supports_exclusive()

    def remove_dir(self, prefix):
        return self._call(self.inner.remove_dir, prefix)

    def download_many(self, paths, dest_dir):
        return self._call(self.inner.download_many, paths, dest_dir)

    def health_check(self):
        return self._call(self.inner.health_check)
