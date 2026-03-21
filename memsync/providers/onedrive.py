from __future__ import annotations

import os
import platform
from pathlib import Path

from memsync.providers import BaseProvider, register


@register
class OneDriveProvider(BaseProvider):
    name = "onedrive"
    display_name = "OneDrive"

    def detect(self) -> Path | None:
        try:
            return self._find()
        except Exception:
            return None

    def is_available(self) -> bool:
        return self.detect() is not None

    def _find(self) -> Path | None:
        system = platform.system()

        if system == "Windows":
            # Windows sets these env vars when OneDrive is running
            for var in ("OneDrive", "ONEDRIVE", "OneDriveConsumer", "OneDriveCommercial"):
                val = os.environ.get(var)
                if val:
                    p = Path(val)
                    if p.exists():
                        return p
            # Fallback: common default paths
            username = os.environ.get("USERNAME", "")
            for candidate in [
                Path.home() / "OneDrive",
                Path(f"C:/Users/{username}/OneDrive"),
            ]:
                if candidate.exists():
                    return candidate

        elif system == "Darwin":
            # Mac: OneDrive doesn't set env vars, check filesystem
            # Personal OneDrive
            personal = Path.home() / "OneDrive"
            if personal.exists():
                return personal

            # OneDrive via CloudStorage (newer Mac client)
            cloud_storage = Path.home() / "Library" / "CloudStorage"
            if cloud_storage.exists():
                # Personal first, then business
                for d in sorted(cloud_storage.iterdir()):
                    if d.name == "OneDrive-Personal":
                        return d
                for d in sorted(cloud_storage.iterdir()):
                    if d.name.startswith("OneDrive") and d.is_dir():
                        return d

        else:
            # Linux: OneDrive via rclone or manual mount
            for candidate in [
                Path.home() / "OneDrive",
                Path.home() / "onedrive",
            ]:
                if candidate.exists():
                    return candidate

        return None
