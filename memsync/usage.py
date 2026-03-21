from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
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
) -> None:
    """Append one usage record to usage.jsonl (synced, append-only)."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "machine": socket.gethostname(),
        "command": command,
        "model": model,
        "session": session_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(_cost(model, input_tokens, output_tokens), 6),
        "changed": changed,
    }
    path = usage_log_path(memory_root)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


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


def format_summary(entries: list[dict]) -> str:
    if not entries:
        return "No usage recorded yet."

    now = datetime.now(timezone.utc)
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
