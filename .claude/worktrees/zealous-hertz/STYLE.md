# STYLE.md

## Non-negotiables

- **Python 3.11+ only.** Use `tomllib` (stdlib), `match` statements where they clarify,
  `Path` everywhere (never `os.path`), `from __future__ import annotations` at the top
  of every module.
- **No dependencies beyond `anthropic`.** Everything else is stdlib.
  Exception: `pytest`, `pytest-mock`, `ruff` in dev dependencies only.
- **Type hints everywhere.** Return types on all functions. No bare `dict` or `list` —
  use `dict[str, Path]`, `list[Path]`, etc.
- **`Path` for all filesystem operations.** Never concatenate strings to build paths.

---

## Module boundaries

Each module has one job. Don't let them bleed:

- `sync.py` calls the API and returns text. It does not write files.
- `cli.py` handles I/O (print, argparse). It does not contain business logic.
- `providers/` detect paths. They do not create directories or write files.
- `config.py` loads and saves config. It does not call the API.

If you find yourself importing `cli` from `sync` or `sync` from `providers`,
stop and reconsider the design.

---

## Function design

Keep functions small and single-purpose. If a function is doing two things,
split it. The test for this: can you describe what it does in one sentence
without using "and"?

Prefer explicit parameters over reading from global state:

```python
# Good
def refresh_memory_content(notes: str, current_memory: str, config: Config) -> dict:
    ...

# Bad — reads global config internally, hard to test
def refresh_memory_content(notes: str) -> dict:
    config = Config.load()  # hidden dependency
    ...
```

---

## Error handling

- Use specific exceptions, not bare `except Exception`.
- Errors that the user can fix → print to stderr with a fix suggestion, return exit code 1.
- Errors that are bugs → let them propagate with a full traceback.
- Never swallow exceptions silently.

```python
# Good
try:
    path = provider.detect()
except PermissionError as e:
    print(f"Error: can't access sync folder: {e}", file=sys.stderr)
    print("Check folder permissions or run: memsync config set sync_root /path", file=sys.stderr)
    return 4

# Bad
try:
    path = provider.detect()
except Exception:
    path = None
```

---

## CLI output

- Success output → stdout
- Errors → stderr
- Keep success output minimal. Users will run this in terminal sessions —
  wall-of-text output is noise.
- Use `✓` and `✗` for status indicators in `memsync status` and `memsync providers`.
- Emoji in output: only the two above, nowhere else.

---

## Naming conventions

| Thing | Convention | Example |
|---|---|---|
| Modules | snake_case | `claude_md.py` |
| Classes | PascalCase | `OneDriveProvider` |
| Functions | snake_case | `refresh_memory_content` |
| Constants | UPPER_SNAKE | `SYSTEM_PROMPT` |
| CLI commands | hyphen-case | `memsync dry-run` |
| Config keys | snake_case | `keep_days` |
| Provider names | lowercase, no hyphens | `"onedrive"`, `"icloud"`, `"gdrive"` |

---

## What "done" looks like for a module

A module is done when:
1. All functions have type hints
2. All functions have docstrings (one line is fine for obvious things)
3. Tests exist and pass on Mac, Windows, Linux (CI green)
4. No hardcoded paths, model strings, or magic numbers

---

## Commit messages

```
feat: add iCloud provider detection
fix: restore hard constraints dropped by compaction
refactor: extract backup logic into backups.py
test: add provider detection tests with mocked filesystem
docs: update adding-a-provider guide
```

First word: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`.
Present tense. No period at the end.

---

## What to avoid

- Don't use `print()` for debugging — use proper logging or remove before commit
- Don't use `os.path` — use `pathlib.Path`
- Don't use `open()` without `encoding="utf-8"`
- Don't write to `~/.claude/CLAUDE.md` without backing up first
- Don't call the Anthropic API in any path except `sync.py`
- Don't import from `cli.py` in any other module
