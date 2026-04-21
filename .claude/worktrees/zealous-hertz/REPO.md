# REPO.md

## Target repository structure

```
memsync/
├── memsync/
│   ├── __init__.py              # version string only
│   ├── cli.py                   # entry point, argument parsing, command routing
│   ├── config.py                # Config dataclass, load/save, path resolution
│   ├── sync.py                  # Claude API call, compaction, hard constraint enforcement
│   ├── claude_md.py             # CLAUDE.md symlink/copy management
│   ├── backups.py               # backup, prune, list operations
│   └── providers/
│       ├── __init__.py          # BaseProvider ABC, registry, auto_detect()
│       ├── onedrive.py
│       ├── icloud.py
│       ├── gdrive.py
│       └── custom.py
├── tests/
│   ├── conftest.py              # shared fixtures (tmp_path wrappers, mock config)
│   ├── test_config.py           # Config load/save, platform path resolution
│   ├── test_providers.py        # each provider's detect() with mocked filesystem
│   ├── test_sync.py             # refresh logic with mocked API
│   ├── test_backups.py          # backup, prune, list
│   ├── test_claude_md.py        # symlink + copy behavior
│   └── test_cli.py              # CLI integration (subprocess or direct function calls)
├── docs/
│   ├── adding-a-provider.md     # contributor guide for new sync providers
│   └── global-memory-guide.md  # what to put in GLOBAL_MEMORY.md (user guide)
├── .github/
│   ├── workflows/
│   │   ├── ci.yml               # test matrix
│   │   └── release.yml          # PyPI publish on tag
│   └── ISSUE_TEMPLATE/
│       ├── bug_report.md
│       └── provider_request.md
├── pyproject.toml
├── README.md
└── CONTRIBUTING.md
```

---

## pyproject.toml (target)

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "memsync"
version = "0.2.0"
description = "Cross-platform global memory manager for Claude Code"
readme = "README.md"
license = { text = "MIT" }
requires-python = ">=3.11"
keywords = ["claude", "claude-code", "ai", "memory", "cli"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Environment :: Console",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Operating System :: MacOS",
    "Operating System :: Microsoft :: Windows",
    "Operating System :: POSIX :: Linux",
]
dependencies = [
    "anthropic>=0.40.0",
]

[project.urls]
Homepage = "https://github.com/EdgeCaser/memsync"
Issues = "https://github.com/EdgeCaser/memsync/issues"

[project.scripts]
memsync = "memsync.cli:main"

[tool.setuptools.packages.find]
where = ["."]
include = ["memsync*"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py311"
```

---

## CI workflow

```yaml
# .github/workflows/ci.yml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    name: Test (${{ matrix.os }}, Python ${{ matrix.python-version }})
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
        python-version: ["3.11", "3.12"]

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}

      - name: Install dependencies
        run: |
          pip install -e ".[dev]"

      - name: Run tests
        run: pytest tests/ -v
```

---

## Release workflow

```yaml
# .github/workflows/release.yml
name: Release

on:
  push:
    tags:
      - "v*"

jobs:
  publish:
    runs-on: ubuntu-latest
    permissions:
      id-token: write  # for trusted publishing

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Build
        run: |
          pip install build
          python -m build

      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
```

Use PyPI Trusted Publishing (OIDC) — no API keys stored in GitHub secrets.
Set up at: https://pypi.org/manage/account/publishing/

---

## Dev dependencies

Add to pyproject.toml:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-mock>=3.12",
    "ruff>=0.4",
]
```

Install with: `pip install -e ".[dev]"`

---

## Test patterns

### Mocking the filesystem

```python
# tests/conftest.py
import pytest
from pathlib import Path
from memsync.config import Config


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Config pointing entirely to tmp_path — no real filesystem touched."""
    config = Config(
        provider="custom",
        sync_root=tmp_path / "sync",
    )
    (tmp_path / "sync" / ".claude-memory" / "backups").mkdir(parents=True)
    (tmp_path / "sync" / ".claude-memory" / "sessions").mkdir(parents=True)
    monkeypatch.setattr("memsync.config.get_config_path",
                        lambda: tmp_path / "config.toml")
    return config, tmp_path
```

### Mocking the Anthropic API

```python
# tests/test_sync.py
from unittest.mock import MagicMock, patch
from memsync.sync import refresh_memory_content


def test_refresh_returns_updated_content(tmp_config):
    config, tmp_path = tmp_config
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="# Updated memory\n\n## Identity\n- Test user")]

    with patch("anthropic.Anthropic") as mock_client:
        mock_client.return_value.messages.create.return_value = mock_response
        result = refresh_memory_content(
            notes="Test session notes",
            current_memory="# Global Memory\n\n## Identity\n- Test user",
            config=config,
        )

    assert result["changed"] is False  # content same after strip
```

### Mocking provider detection

```python
# tests/test_providers.py
from memsync.providers.onedrive import OneDriveProvider


def test_onedrive_detects_personal_path(tmp_path, monkeypatch):
    onedrive_dir = tmp_path / "OneDrive"
    onedrive_dir.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    provider = OneDriveProvider()
    result = provider.detect()
    assert result == onedrive_dir
```

---

## Build order (recommended)

Work in this sequence to keep the code in a runnable state at each step:

1. `memsync/providers/` — BaseProvider, registry, all 3 providers
2. `memsync/config.py` — Config dataclass, load/save
3. `memsync/backups.py` — extract from prototype sync.py
4. `memsync/claude_md.py` — extract sync_to_claude_md from prototype
5. `memsync/sync.py` — refactor to accept Config, fix hardcoded model
6. `memsync/cli.py` — refactor to wire config + providers through all commands,
                       add new commands: `providers`, `config show/set`
7. `tests/` — write tests for each module as you go
8. CI workflows
9. README, CONTRIBUTING, docs/adding-a-provider.md

---

## GitHub repo conventions

- **main** branch is always releasable
- PRs required for all changes (even from owner)
- Squash merge to keep history clean
- Version: semantic versioning. v0.x.y while in alpha.
- Changelog: keep a simple CHANGELOG.md, updated per release

## Issue templates

### bug_report.md
Ask for: OS, Python version, provider, `memsync status` output, error message.

### provider_request.md
Ask for: provider name, OS, default install path, whether they're willing to
implement it (link to adding-a-provider.md).
