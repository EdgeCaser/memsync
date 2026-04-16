"""
Tests for memsync.daemon.capture

Uses Flask's built-in test client. Verifies auth, request validation,
and session log writing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from memsync.config import Config, DaemonConfig
from memsync.daemon.capture import create_capture_app


@pytest.fixture
def capture_config(tmp_path: Path) -> tuple[Config, Path]:
    sync_root = tmp_path / "sync"
    memory_root = sync_root / ".claude-memory"
    (memory_root / "sessions").mkdir(parents=True)

    config = Config(
        provider="custom",
        sync_root=sync_root,
        daemon=DaemonConfig(
            capture_enabled=True,
            capture_port=5001,
            capture_token="",  # no token by default
        ),
    )
    return config, memory_root


class TestHealth:
    def test_health_returns_ok(self, capture_config: tuple) -> None:
        config, _ = capture_config
        app = create_capture_app(config)
        with app.test_client() as client:
            resp = client.get("/health")
        assert resp.status_code == 200
        assert json.loads(resp.data)["ok"] is True


class TestAddNote:
    def test_accepts_valid_note(self, capture_config: tuple) -> None:
        config, _ = capture_config
        app = create_capture_app(config)
        with app.test_client() as client:
            resp = client.post(
                "/note",
                data=json.dumps({"text": "Test note from iPhone"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["ok"] is True
        assert "timestamp" in body

    def test_rejects_empty_text(self, capture_config: tuple) -> None:
        config, _ = capture_config
        app = create_capture_app(config)
        with app.test_client() as client:
            resp = client.post(
                "/note",
                data=json.dumps({"text": "   "}),
                content_type="application/json",
            )
        assert resp.status_code == 400
        assert b"empty" in resp.data

    def test_rejects_missing_text_field(self, capture_config: tuple) -> None:
        config, _ = capture_config
        app = create_capture_app(config)
        with app.test_client() as client:
            resp = client.post(
                "/note",
                data=json.dumps({"msg": "wrong key"}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_rejects_non_json_body(self, capture_config: tuple) -> None:
        config, _ = capture_config
        app = create_capture_app(config)
        with app.test_client() as client:
            resp = client.post("/note", data="not json", content_type="text/plain")
        assert resp.status_code == 400

    def test_writes_to_session_log(self, capture_config: tuple) -> None:
        config, memory_root = capture_config
        app = create_capture_app(config)
        with app.test_client() as client:
            client.post(
                "/note",
                data=json.dumps({"text": "Important note captured"}),
                content_type="application/json",
            )
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        log_path = memory_root / "sessions" / f"{today}.md"
        assert log_path.exists()
        content = log_path.read_text(encoding="utf-8")
        assert "Important note captured" in content
        assert "(captured)" in content

    def test_appends_to_existing_session_log(self, capture_config: tuple) -> None:
        config, memory_root = capture_config
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        log_path = memory_root / "sessions" / f"{today}.md"
        log_path.write_text("existing content\n", encoding="utf-8")

        app = create_capture_app(config)
        with app.test_client() as client:
            client.post(
                "/note",
                data=json.dumps({"text": "appended note"}),
                content_type="application/json",
            )
        content = log_path.read_text(encoding="utf-8")
        assert "existing content" in content
        assert "appended note" in content


class TestTokenAuth:
    def test_accepts_without_token_when_none_configured(self, capture_config: tuple) -> None:
        config, _ = capture_config  # capture_token = ""
        app = create_capture_app(config)
        with app.test_client() as client:
            resp = client.post(
                "/note",
                data=json.dumps({"text": "unauthenticated"}),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_requires_token_when_configured(self, capture_config: tuple) -> None:
        import dataclasses

        config, _ = capture_config
        config = dataclasses.replace(
            config,
            daemon=dataclasses.replace(config.daemon, capture_token="secret123"),
        )
        app = create_capture_app(config)
        with app.test_client() as client:
            resp = client.post(
                "/note",
                data=json.dumps({"text": "no token"}),
                content_type="application/json",
            )
        assert resp.status_code == 401

    def test_accepts_valid_token(self, capture_config: tuple) -> None:
        import dataclasses

        config, _ = capture_config
        config = dataclasses.replace(
            config,
            daemon=dataclasses.replace(config.daemon, capture_token="secret123"),
        )
        app = create_capture_app(config)
        with app.test_client() as client:
            resp = client.post(
                "/note",
                data=json.dumps({"text": "authenticated"}),
                content_type="application/json",
                headers={"X-Memsync-Token": "secret123"},
            )
        assert resp.status_code == 200

    def test_rejects_wrong_token(self, capture_config: tuple) -> None:
        import dataclasses

        config, _ = capture_config
        config = dataclasses.replace(
            config,
            daemon=dataclasses.replace(config.daemon, capture_token="secret123"),
        )
        app = create_capture_app(config)
        with app.test_client() as client:
            resp = client.post(
                "/note",
                data=json.dumps({"text": "wrong token"}),
                content_type="application/json",
                headers={"X-Memsync-Token": "wrongtoken"},
            )
        assert resp.status_code == 401
