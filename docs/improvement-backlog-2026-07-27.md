# Improvement backlog — 2026-07-27

Six items, ordered by value for effort. Each records *why* it is on the list,
because every one of them came out of a real failure rather than a design
review, and the evidence is the part that is hard to reconstruct later.

Context for all of them: a session on 2026-07-27 found that memsync's failures
are quiet, and that several code paths reported success while doing nothing.
That is the theme these items address.

---

## 1. Audit for the bug class, not the bugs

**Why.** Three separate "reports success while doing nothing" defects turned up
in one session, in code nobody suspected:

- `semantic_dedupe_memory` sent the whole memory file in one call. On a 43 KB
  file the model returned `NONE`; the same prompt over that file's 17 KB
  constraints section returned 13 duplicates.
- The same pass then failed to match any of them, because the model returns
  bullets re-wrapped in markdown (`- *   rule` for a stored `*   rule`), and the
  constraints section uses `*   ` markers. 15 proposed, 0 matched. Reported as
  "No semantic duplicates found."
- Store autosync pulled before committing. Since it runs immediately after a
  memory write, the tree is always dirty and `git pull --rebase` always refused.
  It had never pulled once. Logged as a skip nobody read.

Three in one day, none suspected beforehand, is a pattern rather than luck.

**Approach.** Sweep for the shape rather than the instances:

- `|| true` and bare `except: pass` — swallowed failures
- functions that report a count they never verified against reality
- any "found N" / "removed N" message on a path where N can be zero because a
  match silently failed, rather than because there was genuinely nothing to do
- success messages emitted before the thing they describe is confirmed

**Effort.** ~1 hour. **Done when** each swallowed failure either surfaces or
carries a comment saying why silence is correct there.

---

## 2. A consolidation primitive

**Why.** This is the root cause of the hard-constraint ratchet, and it is a
design gap rather than a bug. memsync can *delete* a bullet that another bullet
fully covers. It cannot *merge* several bullets into one carrying their union.

Dedupe is subtraction. Restatements that each accrete a few unique words are
therefore permanent: the coverage gate correctly refuses to delete any of them,
because no survivor is a word-superset. One rule had reached six copies and 30%
of the always-resident context before a human rewrote them by hand.

**Approach.** A command that proposes a union-rewrite of a duplicate set and
verifies it mechanically before anything is written. The verification done by
hand on 2026-07-27 is the specification:

1. Collect the set (model-proposed or user-named).
2. Draft one replacement carrying every clause.
3. Word-level diff the originals against the draft. Anything dropped that is not
   a stopword or a superseded date is a defect in the draft — this caught two
   real omissions in the first attempt.
4. Show the text and the diff. **Require approval before writing.**

**Do not automate step 4 away.** A bad merge silently loses a hard constraint,
which is exactly the failure the append-only guard exists to prevent.

**Effort.** Half a day. **Done when** a set of restatements can be collapsed
without hand-editing the memory file, and the tool refuses to write a draft that
drops a clause.

---

## 3. A constraint budget guard

**Why.** Nothing notices the always-resident core growing. By the time anyone
measured, hard constraints were **97% of it** — 17,275 of 17,775 chars. The
growth is structural: constraints are append-only by design, and that is the
right safety property, so the size can only be managed, never solved.

**Approach.** Warn in `status` or `project` when the constraint block passes a
configurable threshold. The point is to make the creep visible at 20% rather
than 97%.

**Effort.** ~10 lines. **Done when** exceeding the threshold prints a warning
naming the current size and the threshold.

---

## 4. Drop ollama from the waterfall where it cannot help

**Why.** Measured with `doctor --probe`: on one machine ollama needed **129
seconds** to answer "Reply with exactly: OK" (a 14B model); on another, 27
seconds (a 1B model). It sits last in a waterfall it is only reached through
when everything else has failed, and it has never once salvaged a run.

Keeping a fallback that cannot realistically complete a real chunk is a false
sense of depth — it makes the waterfall look four deep when it is effectively
three.

**Effort.** One config line per machine. **Done when** the backends list
reflects what can actually answer in time.

---

## 5. Make `--dry-run` actually dry

**Why.** Two paths write during a dry run: `refresh --dry-run` appends to the
usage log (which then gets swept into a commit), and `harvest --dry-run` writes
to the harvested index. Both are small, and both mean a preview cannot be
trusted as a preview.

**Approach.** Thread the dry-run flag into the write paths rather than relying
on callers to skip them.

**Effort.** ~1 hour. **Done when** a dry run leaves the memory root
byte-identical.

---

## 6. A "what got remembered" digest

**Why.** There is currently no way to see what memsync decided to store without
diffing the file yourself. A single harvest merged eleven sessions into memory
with no visibility into what went in. Bad extractions are cheapest to fix while
they are one line, and most expensive after they have been restated six times —
see item 2 for what that costs.

**Approach.** Summarise bullets added since the last digest and report them on a
schedule, through whatever notification path is already configured. Reuse the
run records in the usage log to bound the window.

**Effort.** Half a day. **Done when** a scheduled message lists what entered
memory, and a wrong entry is catchable the week it appears.

---

## Considered and rejected

**A second off-machine copy of the store history.** Raised, then withdrawn on
inspection: there are already three copies of the full history across two
physical locations, plus hourly encrypted backups. Any one clone can rebuild
everything. The "single point of failure" framing was wrong.

**Automation to keep the third-place backend authenticated.** Its credentials
expire from disuse precisely because it is rarely reached. Either accept the
occasional manual re-auth or remove it — building machinery to keep an unused
fallback warm costs more than the fallback is worth.

---

## Deliberately not scheduled yet: evals

Strategically the largest gap. The entire architecture rests on the assumption
that the model reads the projected memory and acts on it, and nothing tests
that. The on-demand skill went a full day unverified after being built, and was
then confirmed by invoking it once — an anecdote, not a test.

Held back on purpose: behavioural evals for "did it load the right topic" are
hard to make non-flaky, and per-run telemetry only started being recorded on
2026-07-27. Better to let real failure modes show up in that data first, so the
evals target what actually breaks rather than what seems likely to.
