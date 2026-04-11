# PITFALLS.md

Everything here either went wrong during prototyping, was identified as a risk,
or is a subtle behavior that will cause hard-to-debug issues if you miss it.
Read this before touching sync.py, any provider, or the CLAUDE.md sync logic.

---

## 1. Hard constraints must be enforced in code, not just in the prompt

**The problem:** The system prompt tells the model to never remove hard constraints.
But the model compresses by semantic salience — a constraint that didn't appear in
this week's session notes is easy to quietly drop.

**The fix:** After getting the updated content back from the API, diff the
`## Hard constraints` section between old and new. Any item present in old
but missing in new gets re-appended. This is done in Python, not by the model.

```python
def enforce_hard_constraints(old: str, new: str) -> str:
    """
    Re-append any hard constraint lines that the model dropped.
    Works on the raw markdown text — finds the constraints section and diffs it.
    """
    old_constraints = extract_constraints_section(old)
    new_constraints = extract_constraints_section(new)

    dropped = set(old_constraints) - set(new_constraints)
    if not dropped:
        return new

    # Re-append dropped constraints to the section
    # Find the end of the constraints section in `new` and insert there
    return reinsert_constraints(new, sorted(dropped))
```

This is not implemented in the prototype yet. It must be in the refactor.

---

## 2. The model string will rot

`claude-sonnet-4-20250514` will become outdated. Do not hardcode it anywhere.
It lives in config. The prototype has it hardcoded in `sync.py` — that's the
first thing to fix in the refactor.

The risk is not just stale output quality — old model strings may eventually
return API errors, silently breaking refresh for users who never check.

---

## 3. iCloud hides dot-folders

iCloud Drive on Mac does not sync folders whose names begin with `.` to other
devices. If the memory root is `.claude-memory`, it will exist on the Mac that
created it but be invisible to iCloud sync and won't appear on other Macs or
on Windows.

**Fix:** The `ICloudProvider` overrides `get_memory_root()` to return
`claude-memory` (no leading dot). This is already in PROVIDERS.md.
Do not change it. Do not use `.claude-memory` with iCloud.

---

## 4. Windows symlinks require admin rights

On Windows, creating symlinks requires either admin rights or Developer Mode
enabled. Most users won't have either. Don't attempt a symlink on Windows —
always copy. The copy approach means `CLAUDE.md` can drift from `GLOBAL_MEMORY.md`
if the user edits the memory file directly without running `memsync refresh`.

Document this clearly in the Windows section of the README. The copy gets
updated on every `memsync refresh`, so it's fine in practice.

---

## 5. OneDrive path instability across client versions

OneDrive has had three different default paths on Mac across major client versions:

- `~/OneDrive` — old consumer client
- `~/Library/CloudStorage/OneDrive-Personal` — newer client
- `~/Library/CloudStorage/OneDrive - CompanyName` — business/work accounts

The prototype's `get_onedrive_root()` checks all three. Keep all three checks.
Business account names vary (it's the company name in the Microsoft tenant).
The `startswith("OneDrive")` check in the CloudStorage loop catches most cases.

If a user reports detection failure on Mac with a business OneDrive account,
the fix is: `memsync config set sync_root /path/to/their/onedrive`

---

## 6. Google Drive path instability across client versions

Google Drive is worse than OneDrive for this. There have been at least four
different default paths:

- `~/Google Drive` — legacy Backup and Sync (before 2021)
- `~/Library/CloudStorage/GoogleDrive-email@domain.com/My Drive` — current (Drive for Desktop)
- `G:/My Drive` — Windows, Drive for Desktop with G: drive mapping
- Custom drive letter — Windows users can change the drive letter

The provider checks all known paths. The `GoogleDrive-` prefix in CloudStorage
is the most reliable current indicator on Mac. On Windows, check for `G:/My Drive`
as well as `~/Google Drive`.

If detection fails: `memsync config set sync_root /path/to/their/gdrive`

---

## 7. Concurrent writes from two machines

If the user runs `memsync refresh` on Mac and Windows at nearly the same time,
both will read the same `GLOBAL_MEMORY.md`, update independently, and the last
write wins — the other change is lost.

This is an edge case (refresh is a deliberate manual action), not a background
sync, so the risk is low. Document it in the README. Do not add locking — it's
not worth the complexity for v1.

If a user hits this: the backup from the losing write is in `backups/`. They
can manually merge.

---

## 8. The system prompt is load-bearing — don't casually edit it

The system prompt in `sync.py` was iterated over multiple sessions. Specific
phrases matter:

- **"identity layer — not project docs, not cold storage"** — prevents the model
  from treating this like a knowledge base and trying to be exhaustive.
- **"Preserve the user's exact voice, formatting, and section structure"** — without
  this, the model reformats the memory into its own preferred style after a few
  refreshes, eroding the user's structure.
- **"If nothing meaningful changed, return the file UNCHANGED"** — without this,
  the model always makes small edits just to show it did something, creating
  spurious diffs and unnecessary backups.
- **"RETURN: Only the updated GLOBAL_MEMORY.md content. No explanation, no preamble."**
  — without this, the model occasionally prepends "Here is the updated memory file:"
  which then gets written into the file.

If you edit the prompt, test with `--dry-run` across several different notes
inputs before committing. Prompt changes are the highest-risk edits in this codebase.

---

## 9. Empty notes should not trigger a refresh

If `--notes ""` or a notes file that's all whitespace is passed, refuse with
a clear error. Don't send an empty notes payload to the API — it will either
change nothing (wasted tokens) or hallucinate something to change.

This is handled in the prototype's `cmd_refresh`. Keep it in the refactor.

---

## 10. The `max_tokens` ceiling

Every harvest/refresh call must re-emit the entire memory file. If `max_tokens`
is lower than the tokenized file length, the API returns `stop_reason=max_tokens`
and memsync skips the session as "truncated". A dense memory file can blow past
4096 tokens easily — a 400-line file with a long hard-constraints section is
~6-10k tokens.

Mitigation: `max_tokens` is a config field (`[core] max_tokens`, default 16384).
Raise it if harvests start reporting truncation. Truncation detection uses
`stop_reason == "max_tokens"` — more reliable than content heuristics.

Historical note: original ceiling was 4096, which silently skipped all sessions
once the memory file grew past that size.

---

## 11. Sessions log is append-only by design

`sessions/<date>.md` files are never pruned. They're the raw audit trail.
The `prune` command only touches `backups/`. This is intentional — session logs
are cheap (text only) and losing them removes the only way to recover if the
compaction drops something important.

If a user asks for a way to prune sessions, direct them to delete manually.
Don't add a `--sessions` flag to `prune`.

---

## 12. Test isolation — never touch real filesystem or real API in tests

All tests must mock:
- The filesystem (use `tmp_path` from pytest)
- The Anthropic API (use `unittest.mock.patch`)

Never create files in `~/.config`, `~/.claude`, or any cloud sync folder during tests.
Never make real API calls in tests.

See REPO.md for the test structure and mock patterns.
