from __future__ import annotations

import json
import socket
from datetime import UTC, datetime
from pathlib import Path

# Pricing per million tokens: (prefix, input_$/MTok, output_$/MTok)
# Matched by longest prefix — update as Anthropic releases new models.
_PRICING: list[tuple[str, float, float]] = [
    ("claude-opus-4", 15.0, 75.0),
    ("claude-sonnet-4", 3.0, 15.0),
    ("claude-haiku-4", 0.80, 4.0),
    ("claude-opus-3-7", 15.0, 75.0),
    ("claude-sonnet-3-7", 3.0, 15.0),
    ("claude-opus-3-5", 15.0, 75.0),
    ("claude-sonnet-3-5", 3.0, 15.0),
    ("claude-haiku-3-5", 0.80, 4.0),
]

_FALLBACK_INPUT = 3.0    # $/MTok — assume sonnet-tier if model is unknown
_FALLBACK_OUTPUT = 15.0


def _price_for_model(model: str) -> tuple[float, float]:
    for prefix, inp, out in _PRICING:
        if model.startswith(prefix):
            return inp, out
    return _FALLBACK_INPUT, _FALLBACK_OUTPUT


def _cost(model: str, input_tokens: int, output_tokens: int) -> float:
    inp_rate, out_rate = _price_for_model(model)
    return (input_tokens * inp_rate + output_tokens * out_rate) / 1_000_000


def usage_log_path(memory_root: Path) -> Path:
    return memory_root / "usage.jsonl"


def append_usage(
    memory_root: Path,
    command: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    session_id: str = "",
    changed: bool = False,
    backend: str = "",
    duration_ms: int = 0,
) -> None:
    """
    Append one usage record to usage.jsonl (synced, append-only).

    `backend` and `duration_ms` are the telemetry half: which backend actually
    answered, and how long it took. Without them the log can say what a harvest
    cost but not why a run took thirty minutes for twenty-four sessions, which
    is a question that stayed unanswerable through a whole debugging session.
    Both are optional so older readers and older records still parse.
    """
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "machine": socket.gethostname(),
        "command": command,
        "model": model,
        "session": session_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(_cost(model, input_tokens, output_tokens), 6),
        "changed": changed,
    }
    if backend:
        entry["backend"] = backend
    if duration_ms:
        entry["duration_ms"] = duration_ms
    path = usage_log_path(memory_root)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def append_run(memory_root: Path, command: str, **fields: object) -> None:
    """
    Record that a whole run finished, alongside the per-call records.

    Per-call records cannot answer "did last night's harvest finish, and what
    did it achieve" — a run that fails every session writes no per-call records
    at all, which is exactly the shape of the two nights that went unnoticed.
    This writes one line per run regardless of outcome.
    """
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "machine": socket.gethostname(),
        "command": f"{command}_run",
        **fields,
    }
    path = usage_log_path(memory_root)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # telemetry must never take down the work it is measuring


def load_usage(memory_root: Path) -> list[dict]:
    path = usage_log_path(memory_root)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # skip malformed lines (concurrent-write edge case)
    return entries


def last_successful_harvest(memory_root: Path) -> tuple[str, datetime] | None:
    """
    When a harvest last actually wrote something, and from which machine.

    A harvest that fails writes no usage record, so this timestamp going stale
    is itself the failure signal — the one number that distinguishes "nothing
    to harvest" from "the harvest has been dead for two nights". Returns None
    when nothing has ever harvested.

    Reported across all machines rather than for this one: where a single
    always-on host does the harvesting, asking only about the local machine
    would report stale on every laptop while the setup is working correctly.
    """
    newest: tuple[str, datetime] | None = None
    for entry in load_usage(memory_root):
        if entry.get("command") != "harvest":
            continue
        raw = entry.get("ts")
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            continue
        if newest is None or ts > newest[1]:
            newest = (entry.get("machine", "unknown"), ts)
    return newest


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if not ordered:
        return 0.0
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def format_telemetry(memory_root: Path, limit: int = 10) -> str:
    """
    Recent runs and per-backend timings.

    Medians, not means: one 400-second outlier from a backend timing out drags a
    mean somewhere no individual call ever was, and the question this answers is
    "what does a typical call cost me", not "what was the total".
    """
    entries = load_usage(memory_root)
    runs = [e for e in entries if str(e.get("command", "")).endswith("_run")]
    calls = [e for e in entries if e.get("duration_ms")]

    lines: list[str] = []
    lines.append(f"Recent runs (last {limit}):")
    if not runs:
        lines.append("  none recorded yet — runs are logged from this version on")
    for entry in runs[-limit:][::-1]:
        ts = str(entry.get("ts", ""))[:16].replace("T", " ")
        secs = float(entry.get("duration_ms", 0)) / 1000
        lines.append(
            f"  {ts}  {entry.get('machine', '?'):<16} "
            f"{entry.get('command', '?'):<14} "
            f"{entry.get('sessions', 0):>3} seen  "
            f"{entry.get('updated', 0):>3} updated  "
            f"{entry.get('errors', 0):>3} errors  "
            f"{secs:>6.0f}s"
        )

    lines.append("")
    lines.append("Per-backend call latency:")
    if not calls:
        lines.append("  no timed calls recorded yet")
    by_backend: dict[str, list[float]] = {}
    for entry in calls:
        by_backend.setdefault(str(entry.get("backend", "unknown")), []).append(
            float(entry["duration_ms"])
        )
    for backend, durations in sorted(by_backend.items(), key=lambda kv: -len(kv[1])):
        lines.append(
            f"  {backend:<14} {len(durations):>5} calls   "
            f"median {_median(durations) / 1000:>6.1f}s   "
            f"slowest {max(durations) / 1000:>6.1f}s"
        )
    return "\n".join(lines)


def format_summary(entries: list[dict]) -> str:
    if not entries:
        return "No usage recorded yet."

    now = datetime.now(UTC)
    this_month = now.strftime("%Y-%m")
    month_entries = [e for e in entries if e.get("ts", "").startswith(this_month)]

    def _totals(es: list[dict]) -> tuple[int, int, int, float]:
        calls = len(es)
        inp = sum(e.get("input_tokens", 0) for e in es)
        out = sum(e.get("output_tokens", 0) for e in es)
        cost = sum(e.get("cost_usd", 0.0) for e in es)
        return calls, inp, out, cost

    all_calls, all_inp, all_out, all_cost = _totals(entries)
    mo_calls, mo_inp, mo_out, mo_cost = _totals(month_entries)

    machines: dict[str, dict] = {}
    for e in entries:
        m = e.get("machine", "unknown")
        if m not in machines:
            machines[m] = {"calls": 0, "cost": 0.0}
        machines[m]["calls"] += 1
        machines[m]["cost"] += e.get("cost_usd", 0.0)

    lines: list[str] = []

    lines.append("All time:")
    lines.append(f"  Calls:           {all_calls:,}")
    lines.append(f"  Input tokens:    {all_inp:,}")
    lines.append(f"  Output tokens:   {all_out:,}")
    lines.append(f"  Estimated cost:  ${all_cost:.4f}")

    lines.append(f"\nThis month ({this_month}):")
    if month_entries:
        lines.append(f"  Calls:           {mo_calls:,}")
        lines.append(f"  Input tokens:    {mo_inp:,}")
        lines.append(f"  Output tokens:   {mo_out:,}")
        lines.append(f"  Estimated cost:  ${mo_cost:.4f}")
    else:
        lines.append("  No activity this month.")

    lines.append("\nBy machine:")
    for machine, data in sorted(machines.items()):
        lines.append(
            f"  {machine:<22} {data['calls']:>4} call(s)  ${data['cost']:.4f}"
        )

    lines.append("\nRecent (last 10):")
    for e in entries[-10:]:
        ts = e.get("ts", "")[:16].replace("T", " ")
        cmd = e.get("command", "?")
        inp = e.get("input_tokens", 0)
        out = e.get("output_tokens", 0)
        cost = e.get("cost_usd", 0.0)
        machine = e.get("machine", "?")
        changed = " changed" if e.get("changed") else ""
        lines.append(
            f"  {ts}  {cmd:<8}  {inp:>6} in / {out:>5} out  ${cost:.4f}  [{machine}]{changed}"
        )

    return "\n".join(lines)
