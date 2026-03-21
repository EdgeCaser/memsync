from __future__ import annotations

import os
import platform
from pathlib import Path

from memsync.providers import BaseProvider, register


@register
class GoogleDriveProvider(BaseProvider):
    name = "gdrive"
    display_name = "Google Drive"

    def detect(self) -> Path | None:
        try:
            return self._find()
        except Exception:
            return None

    def is_available(self) -> bool:
        return self.detect() is not None

    def _find(self) -> Path | None:
        system = platform.system()

        if system == "Darwin":
            # Google Drive for Desktop (current client)
            cloud_storage = Path.home() / "Library" / "CloudStorage"
            if cloud_storage.exists():
                for d in cloud_storage.iterdir():
                    if d.name.startswith("GoogleDrive") and d.is_dir():
                        # My Drive is inside the account folder
                        my_drive = d / "My Drive"
                        if my_drive.exists():
                            return my_drive
                        return d

            # Legacy Backup and Sync path
            legacy = Path.home() / "Google Drive"
            if legacy.exists():
                return legacy

        elif system == "Windows":
            # Google Drive for Desktop on Windows
            gdrive_env = os.environ.get("GDRIVE_ROOT")
            if gdrive_env:
                p = Path(gdrive_env)
                if p.exists():
                    return p

            username = os.environ.get("USERNAME", "")
            for candidate in [
                Path.home() / "Google Drive",
                Path(f"C:/Users/{username}/Google Drive"),
                # Google Drive for Desktop default
                Path("G:/My Drive"),
                Path("G:/"),
            ]:
                if candidate.exists():
                    return candidate

        elif system == "Linux":
            # Google Drive via google-drive-ocamlfuse or rclone
            for candidate in [
                Path.home() / "GoogleDrive",
                Path.home() / "google-drive",
                Path.home() / "gdrive",
            ]:
                if candidate.exists():
                    return candidate

        return None
