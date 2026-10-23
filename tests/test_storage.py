"""Every provider passes the same compliance suite (PRD Phase 5)."""

import os
from pathlib import Path

import pytest

from iron import locations
from iron.errors import IronError
from iron.storage import FlakyProvider, LocalProvider, RcloneProvider, RetryingProvider, open_location
from iron.storage.compliance import run_compliance


def providers(env):
    yield "local", LocalProvider(env / "local")
    yield "rclone", RcloneProvider("fake:bucket/dir")
    yield "flaky+retry", RetryingProvider(
        FlakyProvider(LocalProvider(env / "flaky"), fail=0.2, partial=0.2, duplicate=0.3, seed=11),
        attempts=15, base_delay=0, sleep=lambda s: None)


@pytest.mark.parametrize("which", ["local", "rclone", "flaky+retry"])
def test_compliance(env, which):
    (env / "local").mkdir()
    provider = dict(providers(env))[which]
    results = run_compliance(provider)
    failed = [(n, d) for n, ok, d in results if not ok]
    assert not failed, failed
    assert len(results) == 8


def test_compliance_reports_a_broken_provider(env):
    class Lossy(LocalProvider):
        def write(self, path, data):
            super().write(path, data[:-1])

    results = run_compliance(Lossy(env))
    assert not dict((n, ok) for n, ok, _ in results)["binary round-trip"]


def test_open_location_forms(env):
    assert isinstance(open_location("rclone:gdrive:Iron-Repos/x.iron"), RcloneProvider)
    assert open_location("rclone:gdrive:Iron-Repos/x.iron/").target == "gdrive:Iron-Repos/x.iron"
    assert open_location("rclone:gdrive:").target == "gdrive:"
    assert isinstance(open_location("sim:%s?fail=0.5&seed=2" % env.as_posix()), FlakyProvider)
    p = open_location(env.as_uri())
    assert isinstance(p, LocalProvider) and Path(p.root) == env
    assert isinstance(open_location("iron://" + env.as_posix()), LocalProvider)


def test_resolve_location(env, monkeypatch):
    monkeypatch.setattr(locations, "rclone_remotes", lambda: [("mydrive", "drive")])
    monkeypatch.setattr(locations, "sync_folders", lambda: (
        locations.Destination("onedrive", "OneDrive", (env / "OneDrive").as_posix(), "sync folder"),))
    assert locations.resolve_location("google-drive://Iron-Repos/app.iron") == "rclone:mydrive:Iron-Repos/app.iron"
    assert locations.resolve_location("mydrive:work/app.iron") == "rclone:mydrive:work/app.iron"
    assert locations.resolve_location("onedrive://Iron-Repos/app.iron") == (env / "OneDrive" / "Iron-Repos" / "app.iron").as_posix()
    absolute = "C:/x/y.iron" if os.name == "nt" else "/x/y.iron"  # "C:/x" is a relative path on POSIX
    assert locations.resolve_location(absolute) == absolute
    with pytest.raises(IronError, match="No Dropbox"):
        locations.resolve_location("dropbox://Iron-Repos/app.iron")
    assert locations.container_for("onedrive", "app") == (env / "OneDrive" / "Iron-Repos" / "app.iron").as_posix()
    assert locations.container_for("mydrive:", "app") == "rclone:mydrive:Iron-Repos/app.iron"
    assert locations.container_for((env / "nas").as_posix(), "app") == (env / "nas" / "Iron-Repos" / "app.iron").as_posix()
    assert locations.container_for((env / "exact.iron").as_posix(), "app") == (env / "exact.iron").as_posix()


def test_sync_folder_remotes_are_flagged_eventually_consistent(env, monkeypatch):
    monkeypatch.setattr(locations, "sync_folders", lambda: (
        locations.Destination("dropbox", "Dropbox", env.as_posix(), "sync folder"),))
    assert LocalProvider(env / "Iron-Repos" / "x.iron").consistent_list is False
    assert locations.sync_folder_kind(env / "Iron-Repos") == "dropbox"
    assert "Dropbox sync folder" in locations.describe_location((env / "a").as_posix())


def test_claims_mode_is_not_used_inside_folders_synced_by_other_clients(env):
    (env / "Sync").mkdir()
    (env / "Sync" / ".stfolder").mkdir()  # Syncthing
    assert LocalProvider(env / "Sync" / "Iron-Repos" / "x.iron").supports_exclusive() is False
    assert LocalProvider(env / "plain" / "x.iron").supports_exclusive() is True
