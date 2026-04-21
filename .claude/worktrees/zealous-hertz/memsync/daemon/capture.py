"""
REST endpoint for mobile note capture.

Accepts POST /note with a JSON body {"text": "..."} and appends the note
to today's session log. Designed for iPhone Shortcuts, curl, or any HTTP client.

iPhone Shortcut setup:
  Action: "Get Contents of URL"
  URL:    http://pi.local:5001/note
  Method: POST
  Headers: X-Memsync-Token: <your token>   (if capture_token is configured)
  Body (JSON): {"text": "Shortcut Input"}

Token auth is optional. When capture_token is empty, all requests are accepted
(safe for local-network-only use; do not expose port to internet).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request

from memsync.config import Config


def create_capture_app(config: Config) -> Flask:
    """Create and configure the capture endpoint Flask application."""
    app = Flask(__name__)

    def get_session_log() -> Path:
        from memsync.providers import get_provider

        provider = get_provider(config.provider)
        sync_root = config.sync_root or provider.detect()
        memory_root = provider.get_memory_root(sync_root)
        today = datetime.now().strftime("%Y-%m-%d")
        return memory_root / "sessions" / f"{today}.md"

    def check_token() -> bool:
        """Return True if the request is authorized."""
        token = config.daemon.capture_token
        if not token:
            return True  # no auth configured — accept all (local network only)
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
    """Start the capture endpoint server. Blocks until interrupted."""
    app = create_capture_app(config)
    app.run(
        host="0.0.0.0",  # noqa: S104  # always local-network accessible
        port=config.daemon.capture_port,
        debug=False,
    )
