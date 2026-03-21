from __future__ import annotations

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


# Import providers to trigger registration — order determines priority
from memsync.providers import custom, gdrive, icloud, onedrive  # noqa: E402, F401
