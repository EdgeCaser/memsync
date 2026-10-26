# Codex rollout harvest: scope

Status: implemented on `feat/codex-harvest`. Written 2026-10-26.

memsync harvests Claude Code transcripts only. Codex is wired in on the other side: it can be a model backend, and memsync writes its block into `~/.codex/AGENTS.md`. This scope adds Codex as a transcript source. Skulld has nothing to port. Its Codex support is a push-based capture tool over MCP and never reads rollout files.

## What's on disk

Measured on the PC against `~/.codex/sessions` on 2026-10-26, looking only at keys and types, not content.

- 53 files at `sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl`, plus older zstd-compressed files in `archived_sessions/*.jsonl.zst`.
- Every line is `{timestamp, type, payload, ordinal}`. Line 1 is always `session_meta`.
- 40 of the 53 are Codex's `guardian` auto-review subagent (`session_meta.payload.source = {"subagent": {"other": "guardian"}}`). Another 3 are spawned subagents (`{"subagent": {"thread_spawn": ...}}`), and 2 have `originator = "codex_exec"`. That leaves 8 interactive sessions (`source = "vscode"`, from Codex Desktop) that hold your conversations.
- Conversation turns are `type = "response_item"`, `payload.type = "message"`, with `role` set to `user`, `assistant` or `developer`. Content is a list of parts: `input_text` and `input_image` for user turns, `output_text` for assistant turns.
- Assistant messages carry `phase`: `final_answer` (584) or `commentary` (212, progress notes mid-turn).
- Codex injects a lot of context as user parts. In the interactive sessions, 117 user parts were plain typed text and 64 were whole `<tag>...</tag>` blocks (`<environment_context>`, `<external_codex_apps_open_page>`, `<send_user_message_question_reply>`, and others). Another 25 started with a Markdown header: `# AGENTS.md`, `# Files`, `## Referenced`, `# ROLE`. Every tag block was closed, so a whole-part match is safe.
- Everything else is noise for memory purposes: `reasoning` (encrypted), function and custom tool calls and their outputs, `agent_message` (inter-agent traffic), `event_msg`, `turn_context`, `token_usage_record`, `world_state`, and `compacted`.

The guardian count is why this scope exists. Without a filter, three quarters of the harvest would be review prompts that restate the parent session, and the merge would see each real conversation twice.

## Discovery

New function in `memsync/harvest.py`:

```python
def list_codex_sessions(sessions_root: Path) -> list[Path]:
    """Interactive Codex rollouts under sessions_root, newest first."""
```

- `rglob("rollout-*.jsonl")` under each root. Default root is `$CODEX_HOME/sessions`, falling back to `~/.codex/sessions`, which matches how `config.py` already resolves `codex_agents_target`.
- Read line 1 only and keep the file when `session_meta.payload.source` is a string and `originator` is not `codex_exec`. Anything with a dict `source` is a subagent. A missing or unparseable first line means skip with a debug log, not an error.
- Skip `archived_sessions/`. Python's standard library only gets zstd in 3.14 (`compression.zstd`) and CI runs 3.11 and 3.12. Archived sessions are old enough that their facts should already be in memory or not matter. Adding them later means an optional `zstandard` extra.

memsync's own Codex backend runs `codex exec --ephemeral`, which should never write a rollout, so the `codex_exec` filter is a second line of defense. The 2 `codex_exec` rollouts on disk came from somewhere else, and I haven't tracked down where.

## Parser

New function, same contract as `read_session_transcript`: returns `(transcript, message_count)`, turns joined with `_TURN_SEPARATOR`, `[USER]` / `[ASSISTANT]` labels, so chunking and the batched merge need no changes.

Keep:
- `response_item` / `message` with role `user`: `input_text` parts that survive the filters below.
- `response_item` / `message` with role `assistant`: `output_text` parts from both phases. The Claude parser keeps every assistant text block, and commentary often holds the "I found X" lines that become facts.

Drop:
- role `developer` (system and permission scaffolding).
- user parts that are a single `<tag>...</tag>` block.
- user parts starting with `# AGENTS.md` (our own memory coming back in), `# Files` or `## Referenced` (attached file bodies).
- `input_image` parts.
- every non-`message` line type.

`message_count` is the number of turns emitted, the same as the Claude parser, so the CLI's "re-harvest when the session has grown" check works on Codex rollouts without changes. Codex appends to the same rollout file when a thread resumes, so that check matters here.

## Index keying

No change is needed, which corrects what I said earlier in chat. Claude session stems are bare UUIDs, and Codex stems are `rollout-<timestamp>-<uuid>`, so they can't collide in `harvested.json`. The existing `seen_stems` dedupe across roots works as is. No migration, and harvested.json stays readable by older memsync versions.

## Wiring

There are four places that read transcripts: `cli.py` lines 700, 733 and 758 inside `_harvest_all_locked`, and `scheduler.py` line 378 in the nightly job. Rather than branch at each one, `read_session_transcript` dispatches on `path.name.startswith("rollout-")`. That makes one change point, and all four callers pick it up.

Discovery is a different story. `_harvest_all_locked` and the nightly job each have their own copy of the roots-walking loop, and the copies have already drifted: the CLI re-harvests sessions that grew, the scheduler does not. The clean move is one `discover_sessions(config, harvested)` in `harvest.py` that walks both Claude and Codex roots and that both callers use. Pulling the drift fix into this change would change scheduler behavior, so I'd keep that to a separate commit inside the same PR.

`_harvest_all_locked` also returns early with "No Claude Code projects directory found" when the Claude roots are missing. That check has to look at the Codex roots too, or a Codex-only user gets nothing.

`_cmd_harvest_session` (`cli.py` line 997, harvest the latest session for the cwd) stays Claude-only. Codex doesn't key folders by cwd, and mapping cwd to rollouts needs a full scan of `session_meta.cwd`, which isn't worth it for v1.

## Config

In `[daemon]`, alongside `harvest_projects_dir`:

```toml
harvest_codex = false                       # opt in
harvest_codex_sessions_dir = "~/.codex/sessions"   # str or list, like harvest_projects_dir
```

Also: `DaemonConfig.codex_sessions_roots()`, mirroring `projects_roots()` (dedupe, preserve order), and a line in `memsync status` that names the Codex root and how many rollouts were eligible versus filtered.

My recommendation is to ship it off by default for one release. The first harvest on your PC would pull in 8 sessions, and that's small enough to read the merged result before it runs nightly.

## Tests

All fixtures synthetic and built in code, no copied rollouts, since the repo is public. A `_write_rollout(path, meta, items)` helper in `tests/test_harvest.py` that writes a `session_meta` line plus response items for a made-up "Widget Factory" thread.

Discovery:
- finds rollouts in nested `YYYY/MM/DD` folders, newest first
- skips guardian, thread_spawn and `codex_exec` rollouts
- skips files with a missing, empty or malformed first line, without raising
- ignores `archived_sessions/` and `.zst` files
- honors `CODEX_HOME`

Parser:
- keeps plain user text and assistant `output_text` from both phases
- drops developer messages, reasoning, tool calls and outputs, `agent_message`, `compacted`, `event_msg`
- drops whole-tag user parts and `# AGENTS.md` / `# Files` / `## Referenced` parts, but keeps a user part that only mentions a tag mid-sentence
- a user message whose parts are all injected produces no turn
- `message_count` grows when lines are appended (resume case)
- malformed lines are skipped, like the Claude parser
- `read_session_transcript` routes `rollout-*.jsonl` to the Codex parser and everything else to the Claude parser

Integration:
- `memsync harvest --all` with only a Codex root present harvests it (no early return)
- with `harvest_codex = false` the Codex root is never read
- mixed Claude and Codex roots produce one batch, and `harvested.json` records both kinds of stem
- nightly harvest picks up Codex rollouts through the shared discovery function
- config round-trip for both new keys, string and list forms

Per the skulld lesson, write the guardian filter test so it fails when the filter is removed, then put the filter back.

## Size

About 120 lines in `harvest.py`, 40 in config, around 30 net in `cli.py` and `scheduler.py` once discovery is shared, and 25 to 30 tests. One PR.

## Decisions (2026-10-26)

1. `<send_user_message_question_reply>` is unwrapped: the outer tags go, the text inside is kept as a user turn.
2. `# ROLE` is injected (Ian doesn't set it). Dropped with the other header parts.
3. `harvest_codex` ships off by default for other users. On Ian's PC it gets turned on and a harvest runs by hand straight after setup, rather than waiting for the nightly job.
