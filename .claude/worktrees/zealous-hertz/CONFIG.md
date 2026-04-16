# CONFIG.md

## Config file location

```python
import platform
from pathlib import Path

def get_config_path() -> Path:
    if platform.system() == "Windows":
        import os
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(appdata) / "memsync" / "config.toml"
    else:
        # Mac and Linux — XDG standard
        import os
        xdg_config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        return Path(xdg_config) / "memsync" / "config.toml"
```

---

## Config schema (TOML)

```toml
[core]
provider = "onedrive"                     # which provider is active on this machine
model = "claude-sonnet-4-20250514"        # Anthropic model for refresh
max_memory_lines = 400                    # soft cap passed to the refresh prompt

[paths]
# Optional overrides — set by memsync if auto-detect finds a non-default location
# sync_root = "/Users/ian/Library/CloudStorage/OneDrive-Personal"

# Where to write the CLAUDE.md file that Claude Code reads at session start.
# Change this if Claude Code ever moves its config location, or if you use
# a non-standard Claude Code install.
claude_md_target = "~/.claude/CLAUDE.md"

[backups]
keep_days = 30

[providers.onedrive]
# provider-specific config (currently unused, reserved for future)

[providers.icloud]
# same

[providers.gdrive]
# same
```

---

## Config dataclass

```python
# memsync/config.py

from __future__ import annotations
import tomllib
import platform
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    # [core]
    provider: str = "onedrive"
    model: str = "claude-sonnet-4-20250514"
    max_memory_lines: int = 400

    # [paths]
    sync_root: Path | None = None           # None = use provider auto-detect
    claude_md_target: Path = Path("~/.claude/CLAUDE.md")

    # [backups]
    keep_days: int = 30

    @classmethod
    def load(cls) -> "Config":
        path = get_config_path()
        if not path.exists():
            return cls()  # all defaults
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        return cls._from_dict(raw)

    @classmethod
    def _from_dict(cls, raw: dict) -> "Config":
        core = raw.get("core", {})
        paths = raw.get("paths", {})
        backups = raw.get("backups", {})

        sync_root = paths.get("sync_root")
        claude_md_target = paths.get("claude_md_target", "~/.claude/CLAUDE.md")
        return cls(
            provider=core.get("provider", "onedrive"),
            model=core.get("model", "claude-sonnet-4-20250514"),
            max_memory_lines=core.get("max_memory_lines", 400),
            sync_root=Path(sync_root) if sync_root else None,
            claude_md_target=Path(claude_md_target).expanduser(),
            keep_days=backups.get("keep_days", 30),
        )

    def save(self) -> None:
        path = get_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._to_toml(), encoding="utf-8")

    def _to_toml(self) -> str:
        """
        tomllib is read-only (stdlib). We write TOML manually.
        Schema is simple enough that this is fine.
        If it grows, add tomli_w as a dependency.
        """
        lines = [
            "[core]",
            f'provider = "{self.provider}"',
            f'model = "{self.model}"',
            f"max_memory_lines = {self.max_memory_lines}",
            "",
            "[paths]",
            f'claude_md_target = "{self.claude_md_target.as_posix()}"',
        ]
        if self.sync_root:
            # TOML strings need forward slashes or escaped backslashes
            lines.append(f'sync_root = "{self.sync_root.as_posix()}"')
        lines += [
            "",
            "[backups]",
            f"keep_days = {self.keep_days}",
            "",
        ]
        return "\n".join(lines)


def get_config_path() -> Path:
    if platform.system() == "Windows":
        import os
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(appdata) / "memsync" / "config.toml"
    else:
        import os
        xdg_config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        return Path(xdg_config) / "memsync" / "config.toml"
```

---

## Model handling

The model string is the only config value that will need regular user attention
as Anthropic releases new models. Design for this explicitly:

**`memsync config set model <string>`** — already in the plan, primary update path.

**`memsync refresh --model <string>`** — one-off override without touching config.
Useful when a user wants to test a new model before committing, or use a cheaper
model for a quick session without changing their default.

```python
# In cmd_refresh — merge --model into config before passing to sync
if args.model:
    config = dataclasses.replace(config, model=args.model)
result = refresh_memory_content(notes, current_memory, config)
```

**Friendly error on bad model string.** The Anthropic API returns a specific error
when a model ID is not found. Catch it and print a useful message:

```python
except anthropic.BadRequestError as e:
    if "model" in str(e).lower():
        print(
            f"Error: model '{config.model}' may be unavailable or misspelled.\n"
            f"Update with: memsync config set model <model-id>\n"
            f"Current models: https://docs.anthropic.com/en/docs/about-claude/models",
            file=sys.stderr,
        )
        return 5
    raise
```

**`memsync models` command** — v2, not v1. Would call the Anthropic API to list
available models and flag if the configured one is deprecated. Don't build it yet —
note it in CHANGELOG as a planned feature.

**Valid model strings as of writing (2026-03):**
- `claude-sonnet-4-20250514` — default, best balance of quality and cost
- `claude-opus-4-20250514` — highest quality, higher cost
- `claude-haiku-4-5-20251001` — fastest, lowest cost, fine for simple memory updates

Users on a budget can set Haiku as their default. The memory refresh prompt is
not complex enough to need Opus for most use cases.

---

## `memsync config` commands

```
memsync config show
    → prints current config.toml contents

memsync config set provider icloud
    → updates config.provider, saves

memsync config set model claude-opus-4-20250514
    → updates config.model, saves

memsync config set sync_root /path/to/custom/folder
    → updates config.sync_root, saves
    → also sets provider to "custom" automatically

memsync config set keep_days 60
    → updates config.keep_days, saves
```

Valid keys for `memsync config set`:
- `provider` — must be a registered provider name
- `model` — any string (validated on first API call with friendly error)
- `sync_root` — path, must exist
- `claude_md_target` — path to write CLAUDE.md (default: `~/.claude/CLAUDE.md`)
- `max_memory_lines` — integer
- `keep_days` — integer

---

## Notes

- Config is machine-specific. It lives in `~/.config/` or `%APPDATA%`, NOT in the
  sync folder. Two machines can use different providers pointing to the same cloud
  storage location — that's fine and expected.

- The model default (`claude-sonnet-4-20250514`) will rot as Anthropic releases new
  models. The intent is for users to update it via `memsync config set model ...`
  when they want to upgrade. Do not auto-update the model. Do not pin to a specific
  version in code — always read from config.

- `claude_md_target` defaults to `~/.claude/CLAUDE.md` but is configurable so users
  aren't broken if Claude Code ever changes its config location, or if they have a
  non-standard setup. Always expand `~` via `.expanduser()` before use.

- `tomllib` (stdlib, Python 3.11+) is read-only. Writing is done manually via
  `_to_toml()`. If the config schema grows significantly, add `tomli_w` as a
  dependency. For now, keep the dep count at 1 (anthropic only).
