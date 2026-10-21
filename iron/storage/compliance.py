"""Provider compliance suite (PRD Phase 5: every provider passes the same tests).

Run it against your own storage with ``iron doctor --deep``. It works inside a
throwaway ``iron-compliance-*`` folder and removes it afterwards.
"""

import os
import uuid

from .base import NotFound


def run_compliance(provider):
    """Return a list of (check name, passed, detail)."""
    base = "iron-compliance-" + uuid.uuid4().hex[:8]
    results = []

    def check(name, fn):
        try:
            detail = fn()
            results.append((name, True, detail or ""))
        except AssertionError as e:
            results.append((name, False, str(e) or "assertion failed"))
        except Exception as e:  # noqa: BLE001 - report every failure, keep going
            results.append((name, False, "%s: %s" % (type(e).__name__, e)))

    binary = bytes(range(256)) * 4 + b"\r\n\x00end"
    big = os.urandom(3 * 1024 * 1024)

    def roundtrip():
        provider.write(base + "/a.bin", binary)
        assert provider.read(base + "/a.bin") == binary, "binary content changed in transit"

    def overwrite():
        provider.write(base + "/a.bin", b"second")
        assert provider.read(base + "/a.bin") == b"second", "rewrite did not replace content"

    def listing():
        provider.write(base + "/sub/nested.json", b"{}")
        names = provider.list(base)
        assert "a.bin" in names, "written file missing from listing: %r" % names
        assert "nested.json" not in names and "sub" not in names, "listing is not limited to files in one folder"
        assert provider.list(base + "/does-not-exist") == [], "listing a missing folder should be empty"

    def existence():
        assert provider.exists(base + "/a.bin"), "exists() is false for a written file"
        assert not provider.exists(base + "/nope.bin"), "exists() is true for a missing file"

    def missing_read():
        try:
            provider.read(base + "/nope.bin")
        except NotFound:
            return
        raise AssertionError("reading a missing file must raise NotFound")

    def large():
        provider.write(base + "/big.bin", big)
        assert provider.read(base + "/big.bin") == big, "3 MB object did not round-trip"

    def deletion():
        provider.delete(base + "/a.bin")
        assert not provider.exists(base + "/a.bin"), "file still listed after delete"
        provider.delete(base + "/a.bin")  # deleting twice is fine

    def health():
        ok, detail = provider.health_check()
        assert ok, detail
        return detail

    for name, fn in [("binary round-trip", roundtrip), ("overwrite", overwrite), ("listing", listing),
                     ("exists", existence), ("missing file", missing_read), ("3 MB object", large),
                     ("delete", deletion), ("health check", health)]:
        check(name, fn)

    for leaf in ("a.bin", "big.bin", "sub/nested.json"):
        try:
            provider.delete(base + "/" + leaf)
        except Exception:  # noqa: BLE001 - cleanup is best effort
            pass
    provider.remove_dir(base + "/sub")
    provider.remove_dir(base)
    return results
