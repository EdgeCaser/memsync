# Progressive-disclosure memory — design

Status: phase 1 in progress
Date: 2026-07-26

## Problem

`GLOBAL_MEMORY.md` is loaded in full into every Claude Code session via
`~/.claude/CLAUDE.md`. On 2026-07-26 it reached 46,792 chars / 244 lines against
Claude Code's 40,000-char warning threshold and its documented 200-line
adherence target.

Two distinct failures produced that:

1. **Growth with no ceiling.** `max_hot_lines` is interpolated into the merge
   prompt as a request ("keep under 100 lines") and enforced nowhere in code.
   The file ran 2.4x over its own stated target with nothing objecting.
2. **The hot layer is the only layer with a read path.** The tiered design
   already splits hot from cold, but cold is defined as "never in context,"
   which in practice means unreachable. Everything that might ever be needed
   therefore has to live in hot.

The constraint-ratcheting bug fixed in `451ae39` was the proximate cause of the
bloat, but it is not the structural problem. Even with zero duplicates, a single
always-resident file grows until it hits the ceiling.

## What changed externally

Two things landed since the tiered design was written.

**Letta shipped Context Repositories (Feb 2026)** — git-backed markdown memory
where a `system/` directory is pinned to the prompt, every other file carries
frontmatter describing its contents, and the agent fetches bodies on demand.
Their frontmatter convention is explicitly modeled on Anthropic's `SKILL.md`.

**Claude Code shipped the same shape natively.** Auto memory
(`~/.claude/projects/<project>/memory/`) is a `MEMORY.md` index whose first 200
lines / 25KB load every session, plus topic files that are *not* loaded at
startup and are read on demand. Claude Code measures the index after each write
and errors when it is over limit.

Verified against the docs, and load-bearing for this design:

- `@path` imports in CLAUDE.md are **eager**. "Splitting into `@path` imports
  helps organization but doesn't reduce context, since imported files load at
  launch." Imports are not a progressive-disclosure mechanism.
- Skills **are**. "A skill's body loads only when it's used, so long reference
  material costs almost nothing until you need it."
- Auto memory is **machine-local** and **per-repository**. "Files are not shared
  across machines or cloud environments."

That last point is the whole opportunity. Anthropic built the progressive
disclosure; they deliberately did not build cross-machine sync or a global
identity scope. Those two things are exactly what memsync already is.

## Design

Keep one canonical store. Derive the on-disk layout from it.

```
memsync-shared/.claude-memory/
  GLOBAL_MEMORY.md      canonical hot layer — harvest still merges into this
  MEMORY_ARCHIVE.md     canonical cold layer — unchanged
  core/
    CLAUDE_CORE.md      generated: constraints + index. This is what syncs to ~/.claude/CLAUDE.md
    topics/
      seattle-love-letter-tracking.md
      memsync-active.md
      ...               generated: one file per ### section
```

**Projection, not migration.** `GLOBAL_MEMORY.md` remains the single source of
truth. `core/` is a derived artifact, regenerated on every write, never edited
by hand. This was the main design call and it was taken deliberately:

- Harvest, merge, dedupe, and the hard-constraints guard keep working untouched.
  None of them need to become N-file-aware.
- No multi-file merge conflicts, and no risk of a fact being routed to the wrong
  topic file and becoming unfindable.
- The cost is that per-file git history is not meaningful and Claude cannot
  reorganize its own hierarchy. Both are phase-3+ concerns.

Native multi-file (Letta's and auto memory's model, where the agent owns the
tree) remains the eventual target. Projection is how we get the context win
without destabilizing the merge path first. The reverse order would change the
storage format and the merge logic simultaneously.

### What stays in the core

- **The whole `## Hard constraints` section.** These are safety rules; a
  constraint that has to be fetched is a constraint that will be missed. This is
  also the largest section (29,509 chars pre-dedupe), which is why the
  subsumption pass is a prerequisite rather than an optimization.
- **A generated `## Memory index`** — one line per topic file: title, a
  description derived from the section, and the absolute path.

Everything else — the 18 `###` per-project sections — projects out to
`core/topics/`.

### What makes the index useful in phase 1

The index carries absolute paths, so Claude can read a topic file with ordinary
file tools the moment it needs one. That is a working read path on day one, just
not an automatic one. Phase 2 makes it automatic via a skill.

### Budget enforcement

`core_max_chars` (default 30,000) and `max_hot_lines` become real checks against
the generated core, not prompt text. Over budget is a loud failure that names
the largest sections, not a silent write.

## Phases

1. **Projection + budget enforcement.** `memsync/projection.py`, a `memsync
   project` command, `projection_enabled` config flag defaulting off. Plus
   `memsync dedup --subsumed`, the deterministic pass needed to fit constraints
   into the core.
2. **Read path.** Generate `~/.claude/skills/memory/SKILL.md` from the index so
   topic bodies load on demand rather than on request.
3. **Git store.** A `GitProvider` beside the existing four. Retires the
   Syncthing conflict files, the `backups/` directory, and the journal's diff
   storage in favour of real history.
4. **Defrag.** Promote the subsumption pass from a manual command to a scheduled
   maintenance step.

## Interaction with auto memory

`autoMemoryDirectory` in `settings.json` accepts any absolute or `~/`-relative
path. Pointing it inside the memsync sync root makes Claude Code's own auto
memory cross-machine with no code at all. It is worth doing, but it inherits the
Syncthing conflict problem, so it should follow phase 3 rather than lead.

Auto memory stays per-repository and machine-written; memsync's core stays
global and harvest-written. They are complementary, not competing, and should
not be merged into one store.

## Known hazard fixed alongside

`claude_md.sync` copies onto the target path on Windows. When the target is an
existing symlink pointing somewhere other than the new source — exactly what
happens the first time projection is enabled on a machine whose `CLAUDE.md`
symlinks to `GLOBAL_MEMORY.md` — the copy follows the link and overwrites the
canonical store with the projected core. The symlink is now cleared before the
copy.
