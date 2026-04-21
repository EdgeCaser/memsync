"""
Weekly email digest for the memsync daemon.

Collects the past 7 days of session logs, sends them to the Claude API
for summarization, and delivers the result via email.

Only runs when config.daemon.digest_enabled is True and email is configured.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from memsync.config import Config

DIGEST_SYSTEM_PROMPT = (
    "You are summarizing a week of AI assistant session notes for the user. "
    "Write a brief, plain-text weekly summary: what they worked on, "
    "any notable decisions or completions, and anything that seems worth "
    "following up on. 150-250 words. No headers. Direct and useful."
)


def generate_and_send(config: Config) -> None:
    """Generate a weekly digest and send via configured email."""
    from memsync.daemon.notify import _send_email
    from memsync.providers import get_provider

    provider = get_provider(config.provider)
    sync_root = config.sync_root or provider.detect()
    if not sync_root:
        return

    memory_root = provider.get_memory_root(sync_root)
    digest_text = generate_digest(memory_root, config)

    if digest_text:
        _send_email(
            config,
            subject=f"memsync weekly digest — week of {date.today().strftime('%b %d')}",
            body=digest_text,
        )


def generate_digest(memory_root: Path, config: Config) -> str:
    """
    Collect the past 7 days of session logs and summarize via the Claude API.
    Returns an empty string if there are no session logs this week.
    """
    today = date.today()
    week_ago = today - timedelta(days=7)

    session_logs: list[str] = []
    for i in range(7):
        day = week_ago + timedelta(days=i + 1)
        log_path = memory_root / "sessions" / f"{day.strftime('%Y-%m-%d')}.md"
        if log_path.exists():
            day_label = day.strftime("%A %b %d")
            session_logs.append(f"## {day_label}\n{log_path.read_text(encoding='utf-8')}")

    if not session_logs:
        return ""

    all_notes = "\n\n".join(session_logs)

    import anthropic  # optional daemon extra — lazy import keeps base install clean
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=config.model,
        max_tokens=1000,
        system=DIGEST_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": all_notes}],
    )

    return response.content[0].text.strip()
