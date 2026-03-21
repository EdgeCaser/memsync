from __future__ import annotations

import platform
from pathlib import Path

import pytest

from memsync.providers import all_providers, auto_detect, get_provider
from memsync.providers.custom import CustomProvider
from memsync.providers.gdrive import GoogleDriveProvider
from memsync.providers.icloud import ICloudProvider
from memsync.providers.onedrive import OneDriveProvider


def _raise_boom(self):
    raise Exception("boom")


@pytest.mark.smoke
class TestRegistry:
    def test_all_four_providers_registered(self):
        names = {p.name for p in all_providers()}
        assert names == {"onedrive", "icloud", "gdrive", "custom"}

    def test_get_provider_by_name(self):
        p = get_provider("onedrive")
        assert isinstance(p, OneDriveProvider)

    def test_get_provider_raises_for_unknown(self):
        with pytest.raises(KeyError, match="dropbox"):
            get_provider("dropbox")


class TestOneDriveProvider:
    def test_detects_home_onedrive(self, tmp_path, monkeypatch):
        onedrive_dir = tmp_path / "OneDrive"
        onedrive_dir.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(platform, "system", lambda: "Darwin")

        provider = OneDriveProvider()
        result = provider.detect()
        assert result == onedrive_dir

    def test_detects_cloudstore_personal(self, tmp_path, monkeypatch):
        cloud = tmp_path / "Library" / "CloudStorage"
        personal = cloud / "OneDrive-Personal"
        personal.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(platform, "system", lambda: "Darwin")

        provider = OneDriveProvider()
        result = provider.detect()
        assert result == personal

    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(platform, "system", lambda: "Darwin")

        provider = OneDriveProvider()
        result = provider.detect()
        assert result is None

    def test_never_raises(self, monkeypatch):
        # detect() must never raise — patch _find to throw internally
        monkeypatch.setattr(OneDriveProvider, "_find", _raise_boom)
        provider = OneDriveProvider()
        result = provider.detect()
        assert result is None

    def test_memory_root_uses_dot_prefix(self, tmp_path):
        provider = OneDriveProvider()
        root = provider.get_memory_root(tmp_path)
        assert root.name == ".claude-memory"

    def test_is_available_true_when_detected(self, tmp_path, monkeypatch):
        onedrive_dir = tmp_path / "OneDrive"
        onedrive_dir.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        provider = OneDriveProvider()
        assert provider.is_available() is True


class TestICloudProvider:
    def test_memory_root_has_no_dot(self, tmp_path):
        """iCloud hides dot-folders — memory root must not start with '.'"""
        provider = ICloudProvider()
        root = provider.get_memory_root(tmp_path)
        assert not root.name.startswith(".")
        assert root.name == "claude-memory"

    def test_detects_mac_icloud(self, tmp_path, monkeypatch):
        icloud_path = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
        icloud_path.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(platform, "system", lambda: "Darwin")

        provider = ICloudProvider()
        result = provider.detect()
        assert result == icloud_path

    def test_returns_none_on_linux(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        provider = ICloudProvider()
        result = provider.detect()
        assert result is None

    def test_never_raises(self, monkeypatch):
        monkeypatch.setattr(ICloudProvider, "_find", _raise_boom)
        provider = ICloudProvider()
        result = provider.detect()
        assert result is None


class TestGoogleDriveProvider:
    def test_detects_legacy_path(self, tmp_path, monkeypatch):
        gdrive = tmp_path / "Google Drive"
        gdrive.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(platform, "system", lambda: "Darwin")

        provider = GoogleDriveProvider()
        result = provider.detect()
        assert result == gdrive

    def test_detects_cloudstore_my_drive(self, tmp_path, monkeypatch):
        cloud = tmp_path / "Library" / "CloudStorage"
        gdrive_dir = cloud / "GoogleDrive-test@gmail.com"
        my_drive = gdrive_dir / "My Drive"
        my_drive.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(platform, "system", lambda: "Darwin")

        provider = GoogleDriveProvider()
        result = provider.detect()
        assert result == my_drive

    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        provider = GoogleDriveProvider()
        result = provider.detect()
        assert result is None

    def test_never_raises(self, monkeypatch):
        monkeypatch.setattr(GoogleDriveProvider, "_find", _raise_boom)
        provider = GoogleDriveProvider()
        result = provider.detect()
        assert result is None


class TestCustomProvider:
    def test_detects_when_path_set_and_exists(self, tmp_path):
        provider = CustomProvider(path=tmp_path)
        assert provider.detect() == tmp_path

    def test_returns_none_when_path_not_set(self):
        provider = CustomProvider()
        assert provider.detect() is None

    def test_returns_none_when_path_missing(self, tmp_path):
        provider = CustomProvider(path=tmp_path / "nonexistent")
        assert provider.detect() is None


class TestAutoDetect:
    def test_returns_only_detected_providers(self, tmp_path, monkeypatch):
        # Only OneDrive folder exists
        onedrive = tmp_path / "OneDrive"
        onedrive.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(platform, "system", lambda: "Darwin")

        detected = auto_detect()
        names = [p.name for p in detected]
        assert "onedrive" in names
        assert "gdrive" not in names
