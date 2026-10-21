"""Storage providers. ``open_location`` turns a location string into a provider:

    C:/Users/me/OneDrive/Iron-Repos/app.iron   a folder (synced, NAS, or plain)
    file:///mnt/nas/Iron-Repos/app.iron        the same, as a URL
    rclone:gdrive:Iron-Repos/app.iron          any rclone remote (direct cloud API)
    sim:/tmp/remote?fail=0.2&seed=1            the unreliable simulator, for testing
"""

from urllib.parse import parse_qsl, unquote, urlparse
from urllib.request import url2pathname

from .base import AlreadyExists, NotFound, RetryingProvider, StorageError, StorageProvider, TransientError
from .flaky import FlakyProvider
from .local import LocalProvider
from .rclone import RcloneProvider, rclone_available

__all__ = ["AlreadyExists", "open_location", "StorageProvider", "RetryingProvider", "StorageError", "NotFound",
           "TransientError", "LocalProvider", "RcloneProvider", "FlakyProvider", "rclone_available"]


def open_location(location):
    loc = location.strip()
    if loc.startswith("iron://"):
        loc = loc[len("iron://"):]
    if loc.startswith("rclone:"):
        return RcloneProvider(loc[len("rclone:"):])
    if loc.startswith("sim:"):
        path, _, query = loc[len("sim:"):].partition("?")
        return FlakyProvider(open_location(path), **dict(parse_qsl(query)))
    if loc.startswith("file://"):
        return LocalProvider(url2pathname(unquote(urlparse(loc).path)))
    return LocalProvider(loc)
