# Adding a new provider

memsync supports any cloud storage service through a simple plugin interface. Adding a provider requires:

1. One new file in `memsync/providers/`
2. One line in `memsync/providers/__init__.py`
3. Tests in `tests/test_providers.py`
4. A row in the README providers table

That's the complete list. No other files need to change.

---

## The provider contract

Every provider implements two required methods and inherits one optional override:

```python
class BaseProvider(ABC):

    name: str           # short id: "dropbox", "box", etc.
    display_name: str   # human-readable: "Dropbox", "Box", etc.

    @abstractmethod
    def detect(self) -> Path | None:
        """
        Return the sync root path if found, None otherwise.
        Must never raise — wrap _find() in try/except.
        """

    @abstractmethod
    def is_available(self) -> bool:
        """Quick check — is this provider's folder accessible?"""

    def get_memory_root(self, sync_root: Path) -> Path:
        """
        Where inside the sync root to store memsync data.
        Default: sync_root / ".claude-memory"
        Override only if the provider hides dot-folders (e.g. iCloud).
        """
        return sync_root / ".claude-memory"
```

---

## Worked example: Dropbox

### Step 1 — Create `memsync/providers/dropbox.py`

```python
from __future__ import annotations

import os
import platform
from pathlib import Path

from memsync.providers import BaseProvider, register


@register
class DropboxProvider(BaseProvider):
    name = "dropbox"
    display_name = "Dropbox"

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
            # Dropbox sets ~/.dropbox/info.json with the sync path
            info = Path.home() / ".dropbox" / "info.json"
            if info.exists():
                import json
                data = json.loads(info.read_text(encoding="utf-8"))
                path_str = data.get("personal", {}).get("path")
                if path_str:
                    p = Path(path_str)
                    if p.exists():
                        return p
            # Fallback: common default
            default = Path.home() / "Dropbox"
            if default.exists():
                return default

        elif system == "Windows":
            # Check Dropbox info.json on Windows
            appdata = os.environ.get("APPDATA", "")
            info = Path(appdata) / "Dropbox" / "info.json"
            if info.exists():
                import json
                data = json.loads(info.read_text(encoding="utf-8"))
                path_str = data.get("personal", {}).get("path")
                if path_str:
                    p = Path(path_str)
                    if p.exists():
                        return p
            default = Path.home() / "Dropbox"
            if default.exists():
                return default

        elif system == "Linux":
            # Dropbox info.json also exists on Linux
            info = Path.home() / ".dropbox" / "info.json"
            if info.exists():
                import json
                data = json.loads(info.read_text(encoding="utf-8"))
                path_str = data.get("personal", {}).get("path")
                if path_str:
                    p = Path(path_str)
                    if p.exists():
                        return p
            default = Path.home() / "Dropbox"
            if default.exists():
                return default

        return None
```

### Step 2 — Register it in `memsync/providers/__init__.py`

Add one line at the bottom of the file, after the existing provider imports:

```python
from memsync.providers import onedrive, icloud, gdrive, custom, dropbox  # noqa: E402, F401
```

The `@register` decorator handles the rest. The import order determines priority during `memsync init` auto-detection.

### Step 3 — Add tests in `tests/test_providers.py`

```python
from memsync.providers.dropbox import DropboxProvider


class TestDropboxProvider:
    def test_detects_default_path(self, tmp_path, monkeypatch):
        dropbox_dir = tmp_path / "Dropbox"
        dropbox_dir.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(platform, "system", lambda: "Darwin")

        provider = DropboxProvider()
        result = provider.detect()
        assert result == dropbox_dir

    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        provider = DropboxProvider()
        assert provider.detect() is None

    def test_never_raises(self, monkeypatch):
        monkeypatch.setattr(DropboxProvider, "_find", lambda self: (_ for _ in ()).throw(Exception("boom")))
        provider = DropboxProvider()
        assert provider.detect() is None
```

### Step 4 — Update the README

Add a row to the providers table in `README.md`:

```markdown
| Dropbox | ✓ | ✓ | ✓ |
```

---

## Things to get right

**`detect()` must never raise.** Wrap all detection logic in `_find()` and call it from `detect()` inside `try/except Exception`. A provider that throws crashes `memsync providers` for everyone.

**Check `exists()` before returning.** Always verify the path actually exists before returning it. A path that exists in the config but not on disk is wrong.

**`info.json` vs env vars vs default paths.** Prefer provider-documented paths (like Dropbox's `info.json`) over guessing default paths. The guesses are a fallback.

**Don't override `get_memory_root()` unless necessary.** The default (`.claude-memory`) is correct for most providers. Only override it if the provider has a technical reason not to sync dot-folders (like iCloud on Mac).

**Detection priority.** Providers are detected in import order. If you want your provider to be checked before Google Drive but after iCloud, put it in that order in the import line.

---

## Testing your provider without a real account

Use `tmp_path` and `monkeypatch` to simulate the filesystem. See the existing provider tests in `tests/test_providers.py` for the pattern. Never create real files in `~`, `~/.config`, or any cloud folder during tests.
