# DAEMON.md

## What this module is

The daemon is an optional, always-on companion to memsync core. It runs on a
persistent machine (Raspberry Pi, home server, always-on desktop) and automates
the operations that core requires you to trigger manually.

It is a separate optional install. Core memsync never imports from this module.

```bash
pip install memsync[daemon]     # installs core + daemon extras
```

Read `CLAUDE.md` and `ARCHITECTURE.md` before this file — this module extends
that system, it does not replace any of it.

---

## What it does

| Feature | What it automates |
|---|---|
| Scheduled refresh | Runs `memsync refresh` nightly from session logs — no manual trigger needed |
| Backup mirror | rsync of `.claude-memory/` to a local path hourly — independent of OneDrive |
| Web UI | Browser-based view/edit of `GLOBAL_MEMORY.md` on the local network |
| Capture endpoint | REST endpoint for mobile notes (iPhone Shortcuts, etc.) |
| Drift detection | Alerts when `CLAUDE.md` on any machine is stale vs `GLOBAL_MEMORY.md` |
| Weekly digest | Email summary of the week's session logs and memory changes |

All features are individually toggleable in config. None are on by default except
scheduled refresh and backup mirror.

---

## Module structure

```
memsync/daemon/
├── __init__.py          # version, public API
├── scheduler.py         # APScheduler wrapper, job definitions
├── web.py               # Flask web UI (view + edit GLOBAL_MEMORY.md)
├── capture.py           # REST endpoint for mobile note capture
├── watchdog.py          # drift detection between CLAUDE.md and GLOBAL_MEMORY.md
├── digest.py            # weekly email digest
├── service.py           # systemd (Pi/Linux) and launchd (Mac) service install
└── notify.py            # notification abstraction (email, file flag, log)
```

---

## New CLI commands

```
memsync daemon start            start the daemon in the foreground (for testing)
memsync daemon start --detach   start as background process
memsync daemon stop             stop background process
memsync daemon status           show running status, last job times, next scheduled runs
memsync daemon install          register as system service (auto-starts on boot)
memsync daemon uninstall        remove system service registration
memsync daemon schedule         show all scheduled jobs and last/next run times
memsync daemon web              open web UI in browser (shortcut)
```

---

## Config additions

The daemon adds a `[daemon]` section to `config.toml`. Written by `memsync daemon install`,
not present in a core-only install.

```toml
[daemon]
enabled = true

# Scheduled refresh
# Reads today's sessions/<date>.md and runs memsync refresh automatically.
# Cron syntax. Default: 11:55pm daily.
refresh_schedule = "55 23 * * *"
refresh_enabled = true

# Backup mirror
# Independent local copy of .claude-memory/ — not subject to OneDrive sync.
# Empty string = disabled.
backup_mirror_path = ""
backup_mirror_schedule = "0 * * * *"    # hourly

# Web UI
web_ui_enabled = true
web_ui_port = 5000
web_ui_host = "0.0.0.0"                 # 0.0.0.0 = accessible on local network
                                         # 127.0.0.1 = localhost only

# Mobile capture endpoint
capture_enabled = true
capture_port = 5001
capture_token = ""                       # optional shared secret for the endpoint

# Drift detection
drift_check_enabled = true
drift_check_interval_hours = 6
drift_notify = "log"                     # "log", "email", or "file"

# Weekly digest
digest_enabled = false
digest_schedule = "0 9 * * 1"           # Monday 9am
digest_email_to = ""
digest_email_from = ""
digest_smtp_host = ""
digest_smtp_port = 587
digest_smtp_user = ""
digest_smtp_password = ""               # consider using keyring instead
```

---

## scheduler.py

Uses APScheduler in blocking mode for foreground, background thread mode for detached.

```python
# memsync/daemon/scheduler.py
from __future__ import annotations

from pathlib import Path
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from memsync.config import Config
from memsync.sync import refresh_memory_content
from memsync.backups import backup


def build_scheduler(config: Config, blocking: bool = False):
    """
    Build and configure the scheduler from config.
    blocking=True for foreground (testing), False for daemon mode.
    """
    scheduler = BlockingScheduler() if blocking else BackgroundScheduler()

    if config.daemon.refresh_enabled:
        scheduler.add_job(
            func=job_nightly_refresh,
            trigger=CronTrigger.from_crontab(config.daemon.refresh_schedule),
            args=[config],
            id="nightly_refresh",
            name="Nightly memory refresh",
            misfire_grace_time=3600,    # run even if missed by up to 1 hour
        )

    if config.daemon.backup_mirror_path:
        scheduler.add_job(
            func=job_backup_mirror,
            trigger=CronTrigger.from_crontab(config.daemon.backup_mirror_schedule),
            args=[config],
            id="backup_mirror",
            name="Backup mirror sync",
            misfire_grace_time=3600,
        )

    if config.daemon.drift_check_enabled:
        scheduler.add_job(
            func=job_drift_check,
            trigger="interval",
            hours=config.daemon.drift_check_interval_hours,
            args=[config],
            id="drift_check",
            name="CLAUDE.md drift check",
        )

    if config.daemon.digest_enabled:
        scheduler.add_job(
            func=job_weekly_digest,
            trigger=CronTrigger.from_crontab(config.daemon.digest_schedule),
            args=[config],
            id="weekly_digest",
            name="Weekly digest email",
        )

    return scheduler


def job_nightly_refresh(config: Config) -> None:
    """
    Read today's session log and run a refresh if there are notes.
    Silently skips if no session log exists for today.
    """
    from datetime import date
    from memsync.providers import get_provider

    provider = get_provider(config.provider)
    sync_root = config.sync_root or provider.detect()
    if not sync_root:
        return

    memory_root = provider.get_memory_root(sync_root)
    today = date.today().strftime("%Y-%m-%d")
    session_log = memory_root / "sessions" / f"{today}.md"

    if not session_log.exists():
        return

    notes = session_log.read_text(encoding="utf-8").strip()
    if not notes:
        return

    memory_path = memory_root / "GLOBAL_MEMORY.md"
    current_memory = memory_path.read_text(encoding="utf-8")

    result = refresh_memory_content(notes, current_memory, config)

    if result["changed"]:
        backup(memory_path, memory_root / "backups")
        memory_path.write_text(result["updated_content"], encoding="utf-8")
        from memsync.claude_md import sync as sync_claude_md
        sync_claude_md(memory_path, config.claude_md_target)


def job_backup_mirror(config: Config) -> None:
    """rsync .claude-memory/ to the local mirror path."""
    import shutil
    from memsync.providers import get_provider

    provider = get_provider(config.provider)
    sync_root = config.sync_root or provider.detect()
    if not sync_root:
        return

    memory_root = provider.get_memory_root(sync_root)
    mirror = Path(config.daemon.backup_mirror_path).expanduser()
    mirror.mkdir(parents=True, exist_ok=True)

    # Copy all files, preserve timestamps
    for src in memory_root.rglob("*"):
        if src.is_file():
            rel = src.relative_to(memory_root)
            dst = mirror / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def job_drift_check(config: Config) -> None:
    """Check if CLAUDE.md is stale relative to GLOBAL_MEMORY.md."""
    from memsync.claude_md import is_synced
    from memsync.providers import get_provider
    from memsync.daemon.notify import notify

    provider = get_provider(config.provider)
    sync_root = config.sync_root or provider.detect()
    if not sync_root:
        return

    memory_root = provider.get_memory_root(sync_root)
    memory_path = memory_root / "GLOBAL_MEMORY.md"

    if not is_synced(memory_path, config.claude_md_target):
        notify(
            config,
            subject="memsync: CLAUDE.md is out of sync",
            body=(
                f"CLAUDE.md at {config.claude_md_target} does not match "
                f"GLOBAL_MEMORY.md at {memory_path}.\n"
                f"Run: memsync refresh to resync."
            ),
        )


def job_weekly_digest(config: Config) -> None:
    """Generate and email a weekly digest of session logs."""
    from memsync.daemon.digest import generate_and_send
    generate_and_send(config)
```

---

## web.py

Simple Flask app. Read-only view by default, edit mode behind a confirmation.
Accessible on the local network at `http://pi.local:5000` (or whatever the
Pi's hostname is).

```python
# memsync/daemon/web.py
from __future__ import annotations

from pathlib import Path
from flask import Flask, render_template_string, request, redirect, url_for

from memsync.config import Config
from memsync.backups import backup
from memsync.claude_md import sync as sync_claude_md

# Inline template — no separate template files needed for this simple UI
TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
  <title>memsync — Global Memory</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: monospace; max-width: 860px; margin: 2rem auto; padding: 0 1rem; }
    textarea { width: 100%; height: 70vh; font-family: monospace; font-size: 0.9rem; }
    .actions { margin-top: 1rem; display: flex; gap: 0.5rem; }
    .meta { color: #888; font-size: 0.8rem; margin-bottom: 1rem; }
    .saved { color: green; }
    .error { color: red; }
  </style>
</head>
<body>
  <h2>Global Memory</h2>
  <div class="meta">
    {{ memory_path }}<br>
    Last modified: {{ last_modified }}
    {% if message %}<span class="{{ message_class }}"> — {{ message }}</span>{% endif %}
  </div>
  <form method="POST" action="/save">
    <textarea name="content">{{ content }}</textarea>
    <div class="actions">
      <button type="submit">Save</button>
      <a href="/">Cancel</a>
    </div>
  </form>
</body>
</html>
"""


def create_app(config: Config) -> Flask:
    app = Flask(__name__)
    app.config["MEMSYNC_CONFIG"] = config

    def get_memory_path() -> Path:
        from memsync.providers import get_provider
        provider = get_provider(config.provider)
        sync_root = config.sync_root or provider.detect()
        return provider.get_memory_root(sync_root) / "GLOBAL_MEMORY.md"

    @app.get("/")
    def index():
        path = get_memory_path()
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        import datetime
        last_mod = (
            datetime.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            if path.exists() else "never"
        )
        return render_template_string(
            TEMPLATE,
            content=content,
            memory_path=path,
            last_modified=last_mod,
            message=request.args.get("message", ""),
            message_class=request.args.get("cls", "saved"),
        )

    @app.post("/save")
    def save():
        path = get_memory_path()
        new_content = request.form["content"]
        try:
            if path.exists():
                backup(path, path.parent / "backups")
            path.write_text(new_content, encoding="utf-8")
            sync_claude_md(path, config.claude_md_target)
            return redirect("/?message=Saved+successfully&cls=saved")
        except Exception as e:
            return redirect(f"/?message=Error:+{e}&cls=error")

    return app


def run_web(config: Config) -> None:
    app = create_app(config)
    app.run(
        host=config.daemon.web_ui_host,
        port=config.daemon.web_ui_port,
        debug=False,
    )
```

---

## capture.py

Minimal REST endpoint. Accepts a POST with a note string, appends to today's
session log. Designed for iPhone Shortcuts or any HTTP client.

```python
# memsync/daemon/capture.py
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify

from memsync.config import Config


def create_capture_app(config: Config) -> Flask:
    app = Flask(__name__)

    def get_session_log() -> Path:
        from memsync.providers import get_provider
        provider = get_provider(config.provider)
        sync_root = config.sync_root or provider.detect()
        memory_root = provider.get_memory_root(sync_root)
        today = datetime.now().strftime("%Y-%m-%d")
        return memory_root / "sessions" / f"{today}.md"

    def check_token() -> bool:
        token = config.daemon.capture_token
        if not token:
            return True     # no auth configured — accept all (local network only)
        return request.headers.get("X-Memsync-Token") == token

    @app.post("/note")
    def add_note():
        if not check_token():
            return jsonify({"error": "unauthorized"}), 401

        body = request.get_json(silent=True)
        if not body or "text" not in body:
            return jsonify({"error": "missing 'text' field"}), 400

        text = body["text"].strip()
        if not text:
            return jsonify({"error": "empty note"}), 400

        log_path = get_session_log()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%H:%M:%S")

        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n---\n### {timestamp} (captured)\n{text}\n")

        return jsonify({"ok": True, "timestamp": timestamp})

    @app.get("/health")
    def health():
        return jsonify({"ok": True})

    return app


def run_capture(config: Config) -> None:
    app = create_capture_app(config)
    app.run(
        host="0.0.0.0",         # always local-network accessible
        port=config.daemon.capture_port,
        debug=False,
    )
```

**iPhone Shortcut setup:** Create a "Get Contents of URL" action with:
- URL: `http://pi.local:5001/note`
- Method: POST
- Headers: `X-Memsync-Token: <your token>` (if configured)
- Body JSON: `{"text": "Shortcut Input"}`

---

## service.py

Installs memsync daemon as a system service so it starts on boot.

```python
# memsync/daemon/service.py
from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from memsync.config import get_config_path


SYSTEMD_UNIT = """\
[Unit]
Description=memsync daemon
After=network.target

[Service]
Type=simple
ExecStart={memsync_bin} daemon start
Restart=on-failure
RestartSec=10
Environment=ANTHROPIC_API_KEY={api_key_placeholder}

[Install]
WantedBy=multi-user.target
"""

LAUNCHD_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.memsync.daemon</string>
  <key>ProgramArguments</key>
  <array>
    <string>{memsync_bin}</string>
    <string>daemon</string>
    <string>start</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{log_dir}/memsync-daemon.log</string>
  <key>StandardErrorPath</key>
  <string>{log_dir}/memsync-daemon.err</string>
</dict>
</plist>
"""


def install_service() -> None:
    system = platform.system()
    memsync_bin = _find_memsync_bin()

    if system == "Linux":
        _install_systemd(memsync_bin)
    elif system == "Darwin":
        _install_launchd(memsync_bin)
    else:
        raise NotImplementedError(
            "Service install not supported on Windows. "
            "Run 'memsync daemon start --detach' from Task Scheduler instead."
        )


def uninstall_service() -> None:
    system = platform.system()
    if system == "Linux":
        _uninstall_systemd()
    elif system == "Darwin":
        _uninstall_launchd()


def _install_systemd(memsync_bin: str) -> None:
    unit_path = Path("/etc/systemd/system/memsync.service")
    unit_content = SYSTEMD_UNIT.format(
        memsync_bin=memsync_bin,
        api_key_placeholder="<set ANTHROPIC_API_KEY here>",
    )
    unit_path.write_text(unit_content)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "memsync"], check=True)
    subprocess.run(["systemctl", "start", "memsync"], check=True)
    print(f"Service installed: {unit_path}")
    print("Set ANTHROPIC_API_KEY in the unit file, then: systemctl restart memsync")


def _install_launchd(memsync_bin: str) -> None:
    log_dir = Path.home() / "Library" / "Logs" / "memsync"
    log_dir.mkdir(parents=True, exist_ok=True)
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.memsync.daemon.plist"
    plist_content = LAUNCHD_PLIST.format(memsync_bin=memsync_bin, log_dir=log_dir)
    plist_path.write_text(plist_content)
    subprocess.run(["launchctl", "load", str(plist_path)], check=True)
    print(f"Service installed: {plist_path}")


def _uninstall_systemd() -> None:
    subprocess.run(["systemctl", "stop", "memsync"], check=False)
    subprocess.run(["systemctl", "disable", "memsync"], check=False)
    unit_path = Path("/etc/systemd/system/memsync.service")
    if unit_path.exists():
        unit_path.unlink()
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print("Service removed.")


def _uninstall_launchd() -> None:
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.memsync.daemon.plist"
    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], check=False)
        plist_path.unlink()
    print("Service removed.")


def _find_memsync_bin() -> str:
    import shutil
    bin_path = shutil.which("memsync")
    if not bin_path:
        raise FileNotFoundError(
            "memsync not found in PATH. Install with: pip install memsync[daemon]"
        )
    return bin_path
```

---

## notify.py

Abstraction so watchdog and digest can send alerts without caring about the channel.

```python
# memsync/daemon/notify.py
from __future__ import annotations

import logging
from memsync.config import Config

logger = logging.getLogger("memsync.daemon")


def notify(config: Config, subject: str, body: str) -> None:
    """
    Send a notification via the configured channel.
    Channels: "log" (default), "email", "file"
    Never raises — notification failure should not crash the daemon.
    """
    try:
        match config.daemon.drift_notify:
            case "email":
                _send_email(config, subject, body)
            case "file":
                _write_flag_file(config, subject, body)
            case _:
                logger.warning("%s: %s", subject, body)
    except Exception as e:
        logger.error("Notification failed: %s", e)


def _send_email(config: Config, subject: str, body: str) -> None:
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.daemon.digest_email_from
    msg["To"] = config.daemon.digest_email_to
    msg.set_content(body)

    with smtplib.SMTP(config.daemon.digest_smtp_host, config.daemon.digest_smtp_port) as smtp:
        smtp.starttls()
        smtp.login(config.daemon.digest_smtp_user, config.daemon.digest_smtp_password)
        smtp.send_message(msg)


def _write_flag_file(config: Config, subject: str, body: str) -> None:
    from pathlib import Path
    from datetime import datetime

    flag_dir = Path.home() / ".config" / "memsync" / "alerts"
    flag_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    flag_file = flag_dir / f"{ts}_alert.txt"
    flag_file.write_text(f"{subject}\n\n{body}\n", encoding="utf-8")
```

---

## digest.py

Weekly email summarizing what changed in the memory file and what was logged.

```python
# memsync/daemon/digest.py
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import anthropic

from memsync.config import Config


def generate_and_send(config: Config) -> None:
    """Generate a weekly digest and send via configured email."""
    from memsync.providers import get_provider
    from memsync.daemon.notify import _send_email

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
    Collect this week's session logs and generate a plain-text summary
    via the Claude API.
    """
    today = date.today()
    week_ago = today - timedelta(days=7)

    session_logs = []
    for i in range(7):
        day = week_ago + timedelta(days=i + 1)
        log_path = memory_root / "sessions" / f"{day.strftime('%Y-%m-%d')}.md"
        if log_path.exists():
            session_logs.append(f"## {day.strftime('%A %b %d')}\n{log_path.read_text(encoding='utf-8')}")

    if not session_logs:
        return ""

    all_notes = "\n\n".join(session_logs)

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=config.model,
        max_tokens=1000,
        system=(
            "You are summarizing a week of AI assistant session notes for the user. "
            "Write a brief, plain-text weekly summary: what they worked on, "
            "any notable decisions or completions, and anything that seems worth "
            "following up on. 150-250 words. No headers. Direct and useful."
        ),
        messages=[{"role": "user", "content": all_notes}],
    )

    return response.content[0].text.strip()
```

---

## Pitfalls specific to the daemon

### API key in systemd unit file
The systemd unit template includes a placeholder for `ANTHROPIC_API_KEY`.
Storing secrets in unit files is not ideal — they're world-readable by default.
Document that users should use `systemctl edit memsync` to add the key in an
override file, or use a secrets manager. Do not store keys in the repo.

### Flask in production
The Flask dev server (`app.run()`) is fine for local network use on a Pi.
Do not suggest or document using it as a public-facing server. If a user
asks about exposing it to the internet, redirect them to proper WSGI + auth.

### Port conflicts
5000 and 5001 are common dev ports. Document that they're configurable and
how to change them if there's a conflict.

### systemd on Pi requires sudo
`systemctl enable` and the unit file write require root. The install function
will fail without it. Print a clear error and suggest `sudo memsync daemon install`.

### APScheduler job persistence
APScheduler by default runs jobs in memory — if the daemon restarts, job
history is lost. That's fine for memsync (jobs are time-based, not state-based).
Do not add a job store database — it's unnecessary complexity.

### Nightly refresh with empty session log
If the user didn't run any sessions that day, `sessions/<date>.md` won't exist.
`job_nightly_refresh` handles this with an early return. Make sure this stays
in place — an empty notes payload to the API wastes tokens and may produce
hallucinated changes.

---

## Build order for daemon module

Do this after core memsync is complete and tested.

1. `DaemonConfig` dataclass additions to `config.py`
2. `scheduler.py` + `notify.py` — the backbone
3. `web.py` — Flask UI
4. `capture.py` — REST endpoint
5. `service.py` — system service install
6. `digest.py` — weekly email (depends on notify)
7. Tests for scheduler jobs (mock filesystem + mock API)
8. Tests for web UI (Flask test client)
9. Tests for capture endpoint (Flask test client)
10. Update `pyproject.toml` with `[daemon]` optional dependencies
11. Update `REPO.md` directory structure
12. Update README with daemon section

---

## pyproject.toml additions

```toml
[project.optional-dependencies]
daemon = [
    "apscheduler>=3.10",
    "flask>=3.0",
]
dev = [
    "pytest>=8.0",
    "pytest-mock>=3.12",
    "ruff>=0.4",
]
```
