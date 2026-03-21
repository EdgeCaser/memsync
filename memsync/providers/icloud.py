from __future__ import annotations

import os
import platform
from pathlib import Path

from memsync.providers import BaseProvider, register


@register
class ICloudProvider(BaseProvider):
    name = "icloud"
    display_name = "iCloud Drive"

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
            # Primary path on Mac
            icloud = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
            if icloud.exists():
                return icloud

        elif system == "Windows":
            # iCloud for Windows installs here
            username = os.environ.get("USERNAME", "")
            for candidate in [
                Path.home() / "iCloudDrive",
                Path(f"C:/Users/{username}/iCloudDrive"),
            ]:
                if candidate.exists():
                    return candidate

        # Linux: iCloud has no official client — not supported
        return None

    def get_memory_root(self, sync_root: Path) -> Path:
        # iCloud hides dot-folders on Mac — use a visible name instead
        return sync_root / "claude-memory"
