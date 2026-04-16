# What to put in GLOBAL_MEMORY.md

This is the file Claude Code reads at the start of every session. It's your **identity layer** — not a project wiki, not a knowledge base. Keep it tight and personal.

---

## The starter template

When you run `memsync init`, you get this:

```markdown
<!-- memsync v0.2 -->
# Global Memory

> Loaded by Claude Code at session start on all machines and projects.
> Edit directly or run: memsync refresh --notes "..."

## Identity & context
- (Fill this in — who you are, your roles, active projects)

## Current priorities
- (What you're working on right now)

## Standing preferences
- (How you like to work — communication style, output format, etc.)

## Hard constraints
- (Rules that must never be lost or softened through compaction)
```

Fill it in. The section names are your structure — keep them.

---

## Identity & context

Who you are and what you're working on. Claude reads this cold at every session.

```markdown
## Identity & context
- Ian, product leader at a B2B SaaS company
- Side projects: memsync (Python CLI), personal finance tracker (Go)
- Background: 10 years PM, comfortable with code but not a full-time engineer
- Working across: Mac (home), Windows (work)
```

---

## Current priorities

What's active right now. This section gets updated most often by `memsync refresh`.

```markdown
## Current priorities
- memsync v0.2: finish tests and CI, publish to PyPI
- Q2 planning deck due April 15
- Hiring: two PM openings, first round interviews next week
```

Completed items get demoted to a brief "Recent completions" section automatically during refresh. They don't stay forever — that's what session logs are for.

---

## Standing preferences

How you like to work. These persist across all projects and sessions.

```markdown
## Standing preferences
- Prefer concise output — skip the preamble, just give me the thing
- Code: Python 3.11+, pathlib everywhere, no magic, no cleverness
- Writing: active voice, short sentences, no bullet-point summaries unless asked
- Don't suggest tests unless I ask — I know when I need them
- When in doubt, ask one clarifying question rather than guessing
```

---

## Hard constraints

Rules that must never be removed or softened, no matter how much the memory compacts. Claude checks this section in Python code — it's enforced, not just prompted.

```markdown
## Hard constraints
- Never hardcode credentials or API keys in any code I write
- Always ask before deleting files or making destructive changes
- Never rewrite from scratch — refactor what exists
- Don't add emoji to output unless I explicitly ask
```

Good candidates: safety rules, things that bit you in the past, preferences so strong that "sometimes" isn't acceptable.

---

## What NOT to put here

- **Project-specific docs** — those go in each project's `CLAUDE.md`
- **Reference material** — API docs, schemas, architecture diagrams
- **Cold storage** — old project summaries, historical context
- **Everything** — this file has a soft cap of ~400 lines. If it's getting long, you have too much in it.

The memory file should read like a dense briefing note, not a wiki. If Claude can derive something from the project files, it doesn't need to be here.

---

## Keeping it current

After any session where something important shifted — a decision made, a priority changed, a preference discovered — run:

```bash
memsync refresh --notes "Decided to use JWT auth, not sessions. Slower but simpler for our use case."
```

The Claude API reads your notes and the current memory file, and updates it accordingly. The old version is backed up automatically.

You can also edit `GLOBAL_MEMORY.md` directly at any time. Just run `memsync refresh` afterward (even with minimal notes) to sync the copy to `~/.claude/CLAUDE.md`.
