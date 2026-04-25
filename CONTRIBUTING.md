# Contributing to memsync

Thanks for your interest. memsync is designed to be easy to extend — adding a new provider requires touching exactly one new file and one line in `__init__.py`.

---

## Setup

```bash
git clone https://github.com/EdgeCaser/memsync
cd memsync
pip install -e ".[dev]"
```

Run tests:

```bash
pytest tests/ -v
```

The default install (`.[dev]`) runs the core test suite. Daemon tests require additional extras (`apscheduler`, `flask`). To run the full suite:

```bash
pip install -e ".[daemon,dev]"
pytest tests/ -v
```

---

## Adding a provider

The most common contribution is adding support for a new cloud storage provider (Dropbox, Box, Synology Drive, etc.). See [docs/adding-a-provider.md](docs/adding-a-provider.md) for a complete guide with a worked example.

---

## Code style

- Python 3.11+. Use `Path` everywhere, `from __future__ import annotations` at the top of every module.
- Type hints on all functions.
- No dependencies beyond `anthropic` (stdlib only, except dev deps).
- See [STYLE.md](STYLE.md) for the full style guide.

---

## Module boundaries

Each module has one job:

- `sync.py` — calls the API, returns text. Does not write files.
- `cli.py` — handles I/O. Does not contain business logic.
- `providers/` — detect paths. Do not create directories or write files.
- `config.py` — loads and saves config. Does not call the API.

---

## Tests

- All tests use `tmp_path` for filesystem isolation — never touch `~/.config`, `~/.claude`, or any cloud folder.
- All tests mock the Anthropic API — never make real API calls.
- Tests run on macOS, Windows, and Linux via CI. If your change is platform-specific, add a `pytest.mark.skipif` guard.

---

## Pull requests

- Open an issue first for anything beyond a small bug fix.
- PRs require CI green on all 6 matrix combinations (3 OS × 2 Python versions).
- Squash merge — keep the commit history clean.
- Commit style: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:` prefix, present tense, no period.

---

## Hard rules

- Never hardcode the model string. Always read from `config.model`.
- Never hardcode paths. Always go through the provider or config system.
- Hard constraints in GLOBAL_MEMORY.md are append-only. This is enforced in Python in `sync.py` — do not remove that check.
- Backups before every write. No exceptions.
- Read [PITFALLS.md](PITFALLS.md) before touching `sync.py` or any provider.
