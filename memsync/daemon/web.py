"""
Flask web UI for memsync daemon.

Provides a browser-based view/edit interface for GLOBAL_MEMORY.md,
accessible on the local network at http://<host>:<port>/ (default :5000).

Intended for use on a home network only. Do not expose to the public internet.
See DAEMON.md for Flask-in-production guidance.
"""
from __future__ import annotations

import datetime
from pathlib import Path

from flask import Flask, redirect, render_template_string, request

from memsync.backups import backup
from memsync.claude_md import sync as sync_claude_md
from memsync.config import Config

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
    """Create and configure the Flask web UI application."""
    app = Flask(__name__)
    app.config["MEMSYNC_CONFIG"] = config

    def get_memory_path() -> Path:
        from memsync.providers import get_provider

        provider = get_provider(config.provider)
        sync_root = config.sync_root or provider.detect()
        return provider.get_memory_root(sync_root) / "GLOBAL_MEMORY.md"

    @app.get("/")
    def index() -> str:
        path = get_memory_path()
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        last_mod = (
            datetime.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            if path.exists()
            else "never"
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
    """Start the web UI server. Blocks until interrupted."""
    app = create_app(config)
    app.run(
        host=config.daemon.web_ui_host,
        port=config.daemon.web_ui_port,
        debug=False,
    )
