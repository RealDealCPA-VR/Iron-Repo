"""A deliberately unreliable provider for testing (PRD Phase 2).

Wraps another provider and injects latency, timeouts, partial writes,
duplicate requests, lost acknowledgements and stale listings. Use it from any
git command with a location like::

    sim:C:/tmp/remote?fail=0.2&partial=0.1&duplicate=0.1&stale=0.1&seed=7
"""

import random
import time

from .base import StorageProvider, TransientError


class FlakyProvider(StorageProvider):
    name = "sim"

    def __init__(self, inner, fail=0.0, latency=0.0, partial=0.0, duplicate=0.0, stale=0.0, seed=None):
        self.inner = inner
        self.fail = float(fail)
        self.latency = float(latency)
        self.partial = float(partial)
        self.duplicate = float(duplicate)
        self.stale = float(stale)
        self.rng = random.Random(None if seed is None else int(seed))
        self._recent = []

    @property
    def consistent_list(self):
        return self.stale == 0 and self.inner.consistent_list

    def describe(self):
        return "sim(%s)" % self.inner.describe()

    def _chance(self, p):
        return p > 0 and self.rng.random() < p

    def _before(self):
        if self.latency:
            time.sleep(self.rng.random() * self.latency)
        if self._chance(self.fail):
            raise TransientError("simulated timeout")

    def read(self, path):
        self._before()
        return self.inner.read(path)

    def write(self, path, data):
        self._before()
        if self._chance(self.partial):
            self.inner.write(path, data[: len(data) // 2])
            raise TransientError("simulated partial write")
        self.inner.write(path, data)
        if self._chance(self.duplicate):
            self.inner.write(path, data)
        self._recent = (self._recent + [path])[-4:]
        if self._chance(self.fail):
            raise TransientError("simulated lost acknowledgement")

    def create_exclusive(self, path, data):
        self._before()
        self.inner.create_exclusive(path, data)
        if self._chance(self.fail):
            raise TransientError("simulated lost acknowledgement")

    def supports_exclusive(self):
        return self.stale == 0 and self.inner.supports_exclusive()

    def delete(self, path):
        self._before()
        self.inner.delete(path)

    def list(self, prefix):
        self._before()
        names = self.inner.list(prefix)
        if self._chance(self.stale):
            hidden = {p.rpartition("/")[2] for p in self._recent if p.rpartition("/")[0] == prefix.strip("/")}
            names = [n for n in names if n not in hidden]
        return names

    def remove_dir(self, prefix):
        self.inner.remove_dir(prefix)

    def health_check(self):
        return self.inner.health_check()
