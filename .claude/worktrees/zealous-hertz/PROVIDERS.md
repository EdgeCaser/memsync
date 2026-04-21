# PROVIDERS.md

## The plugin contract

Every provider implements this ABC. Nothing else in the codebase needs to change
when a new provider is added.

```python
# memsync/providers/__init__.py

from abc import ABC, abstractmethod
from pathlib import Path


class BaseProvider(ABC):
    """
    A sync provider knows how to find the cloud storage root on the current machine.
    That's its only job. Memory structure lives above it.
    """

    name: str           # short id used in config: "onedrive", "icloud", "gdrive", "custom"
    display_name: str   # human-readable: "OneDrive", "iCloud Drive", "Google Drive", "Custom Path"

    @abstractmethod
    def detect(self) -> Path | None:
        """
        Try to find this provider's sync root on the current machine.
        Returns the path if found and accessible, None otherwise.
        Never raises — detection failure is not an error.
        """

    @abstractmethod
    def is_available(self) -> bool:
        """
        Quick check: is this provider installed and its sync folder accessible?
        Should be fast — no API calls, just filesystem checks.
        """

    def get_memory_root(self, sync_root: Path) -> Path:
        """
        Where inside the sync root to store memsync data.
        Default is <sync_root>/.claude-memory
        Providers can override if needed (e.g. iCloud has invisible dot-folders).
        """
        return sync_root / ".claude-memory"

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"


# Provider registry — add new providers here
_REGISTRY: dict[str, type[BaseProvider]] = {}


def register(cls: type[BaseProvider]) -> type[BaseProvider]:
    """Decorator to register a provider."""
    _REGISTRY[cls.name] = cls
    return cls


def get_provider(name: str) -> BaseProvider:
    """Get a provider instance by name. Raises KeyError if not found."""
    if name not in _REGISTRY:
        available = ", ".join(_REGISTRY.keys())
        raise KeyError(f"Unknown provider {name!r}. Available: {available}")
    return _REGISTRY[name]()


def all_providers() -> list[BaseProvider]:
    """Return one instance of each registered provider."""
    return [cls() for cls in _REGISTRY.values()]


def auto_detect() -> list[BaseProvider]:
    """
    Return all providers that detect successfully on this machine,
    in priority order: OneDrive, iCloud, Google Drive, Custom.
    """
    return [p for p in all_providers() if p.detect() is not None]
```

---

## OneDrive provider

```python
# memsync/providers/onedrive.py

import os
import platform
from pathlib import Path
from . import BaseProvider, register


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
            candidates = [
                Path.home() / "OneDrive",
                Path.home() / "onedrive",
            ]
            for c in candidates:
                if c.exists():
                    return c

        return None
```

---

## iCloud Drive provider

```python
# memsync/providers/icloud.py

import platform
from pathlib import Path
from . import BaseProvider, register


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
            import os
            username = os.environ.get("USERNAME", "")
            candidates = [
                Path.home() / "iCloudDrive",
                Path(f"C:/Users/{username}/iCloudDrive"),
            ]
            for c in candidates:
                if c.exists():
                    return c

        # Linux: iCloud has no official client — not supported
        return None

    def get_memory_root(self, sync_root: Path) -> Path:
        # iCloud hides dot-folders on Mac — use a visible name instead
        return sync_root / "claude-memory"
```

**Note on iCloud dot-folders:** iCloud Drive on Mac does not sync folders whose
names begin with `.` to other devices. Use `claude-memory` not `.claude-memory`
for the iCloud provider. The `get_memory_root` override handles this automatically.

---

## Google Drive provider

```python
# memsync/providers/gdrive.py

import platform
from pathlib import Path
from . import BaseProvider, register


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
            import os
            # Google Drive for Desktop on Windows
            # Sets GDRIVE_ROOT or uses default path
            gdrive_env = os.environ.get("GDRIVE_ROOT")
            if gdrive_env:
                p = Path(gdrive_env)
                if p.exists():
                    return p

            username = os.environ.get("USERNAME", "")
            candidates = [
                Path.home() / "Google Drive",
                Path(f"C:/Users/{username}/Google Drive"),
                # Google Drive for Desktop default
                Path("G:/My Drive"),
                Path("G:/"),
            ]
            for c in candidates:
                if c.exists():
                    return c

        elif system == "Linux":
            # Google Drive via google-drive-ocamlfuse or rclone
            candidates = [
                Path.home() / "GoogleDrive",
                Path.home() / "google-drive",
                Path.home() / "gdrive",
            ]
            for c in candidates:
                if c.exists():
                    return c

        return None
```

**Note on Google Drive path instability:** Google Drive for Desktop changed its
mount path between versions. The `~/Library/CloudStorage/GoogleDrive-*` path is
current (2024+). The `~/Google Drive` path is legacy Backup and Sync. Both are
checked. If a user reports detection failure, first ask which Google Drive client
version they have. See `PITFALLS.md`.

---

## Custom provider (manual path)

```python
# memsync/providers/custom.py

from pathlib import Path
from . import BaseProvider, register


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
```

---

## Adding a new provider

To add Dropbox, Box, Synology, etc.:

1. Create `memsync/providers/dropbox.py`
2. Implement `BaseProvider` (detect + is_available)
3. Add `@register` decorator
4. Import it in `memsync/providers/__init__.py` (the import triggers registration)
5. Add tests in `tests/test_providers.py` using the mocked filesystem pattern
6. Update the providers table in README.md

That's the complete list. No other files need to change.

See `docs/adding-a-provider.md` for the full contributor guide.

---

## Provider detection priority

During `memsync init`, providers are tried in this order:
1. OneDrive
2. iCloud
3. Google Drive
4. Custom (only if path already configured)

If multiple are detected, the user is prompted to choose. The choice is saved to config.
