"""
Notification abstraction for the memsync daemon.

Sends alerts via the channel configured in config.daemon.drift_notify:
  "log"   — write to the daemon logger (default, always works)
  "email" — send via SMTP
  "file"  — write a flag file to ~/.config/memsync/alerts/

Never raises — notification failure must not crash the daemon.
"""
from __future__ import annotations

import logging
import os

from memsync.config import Config

logger = logging.getLogger("memsync.daemon")


def notify(config: Config, subject: str, body: str) -> None:
    """
    Send a notification via the configured channel.
    Silently logs any delivery error rather than propagating it.
    """
    try:
        match config.daemon.drift_notify:
            case "email":
                _send_email(config, subject, body)
            case "file":
                _write_flag_file(subject, body)
            case _:
                logger.warning("%s: %s", subject, body)
    except Exception as e:
        logger.error("Notification failed (%s): %s", config.daemon.drift_notify, e)


def _send_email(config: Config, subject: str, body: str) -> None:
    """Send an alert via SMTP."""
    import smtplib
    from email.message import EmailMessage

    # Prefer env var over plaintext config — see DAEMON_PITFALLS.md #9
    password = os.environ.get("MEMSYNC_SMTP_PASSWORD") or config.daemon.digest_smtp_password

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.daemon.digest_email_from
    msg["To"] = config.daemon.digest_email_to
    msg.set_content(body)

    with smtplib.SMTP(config.daemon.digest_smtp_host, config.daemon.digest_smtp_port) as smtp:
        smtp.starttls()
        smtp.login(config.daemon.digest_smtp_user, password)
        smtp.send_message(msg)


def _write_flag_file(subject: str, body: str) -> None:
    """Write an alert to ~/.config/memsync/alerts/ as a timestamped text file."""
    from datetime import datetime
    from pathlib import Path

    flag_dir = Path.home() / ".config" / "memsync" / "alerts"
    flag_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    flag_file = flag_dir / f"{ts}_alert.txt"
    flag_file.write_text(f"{subject}\n\n{body}\n", encoding="utf-8")
    logger.info("Alert written to %s", flag_file)
