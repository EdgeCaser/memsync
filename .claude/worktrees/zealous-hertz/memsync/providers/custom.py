from __future__ import annotations

from pathlib import Path

from memsync.providers import BaseProvider, register


@register
class CustomProvider(BaseProvider):
    """
    Fallback for any sync service not explicitly supported.
    User sets the path manually via: memsync config set sync_root /path/to/folder
    """
    name = "custom"
    display_name = "Custom Path"

    def __init__(self, path: Path | None = None):
        self._path = path

    def detect(self) -> Path | None:
        # Custom provider only works if path is explicitly configured
        if self._path and self._path.exists():
            return self._path
        return None

    def is_available(self) -> bool:
        return self.detect() is not None
