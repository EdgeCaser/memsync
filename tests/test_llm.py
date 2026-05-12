from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

from memsync.config import Config
from memsync.llm import _call_codex, _call_gemini, _call_ollama, _warmup_ollama_model


class TestCodexBackend:
    def test_codex_exec_reads_prompt_from_stdin(self):
        config = Config()
        captured: dict = {}

        class Result:
            returncode = 0
            stdout = b"ok"
            stderr = b""

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input")
            return Result()

        with patch("memsync.llm.sys.platform", "win32"):
            with patch("memsync.llm._resolve_cli_path", return_value="C:/Users/test/AppData/Roaming/npm/codex.cmd"):
                with patch("memsync.llm.subprocess.run", side_effect=fake_run):
                    result = _call_codex("SYSTEM", "USER", "", config)

        assert captured["cmd"][0:3] == ["cmd.exe", "/c", "C:/Users/test/AppData/Roaming/npm/codex.cmd"]
        assert "exec" in captured["cmd"]
        assert captured["cmd"][-1] == "-"
        assert captured["input"] == b"SYSTEM\n\nUSER"
        assert result["text"] == "ok"


class TestOllamaBackend:
    def test_ollama_chat_disables_openai_retries(self):
        captured: dict = {}

        class ChatCompletions:
            @staticmethod
            def create(**kwargs):
                captured["chat_kwargs"] = kwargs
                choice = SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")
                usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
                return SimpleNamespace(choices=[choice], usage=usage)

        class OpenAI:
            def __init__(self, **kwargs):
                captured["client_kwargs"] = kwargs
                self.chat = SimpleNamespace(completions=ChatCompletions())

        fake_openai = SimpleNamespace(OpenAI=OpenAI)

        with patch.dict(sys.modules, {"openai": fake_openai}):
            with patch("memsync.llm._check_ollama_reachable"):
                result = _call_ollama("SYSTEM", "USER", "", Config())

        assert captured["client_kwargs"]["max_retries"] == 0


class TestGeminiBackend:
    def test_gemini_api_key_is_sent_in_header_not_url(self):
        captured: dict = {}

        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {
                    "candidates": [
                        {
                            "content": {"parts": [{"text": "ok"}]},
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 1,
                        "candidatesTokenCount": 1,
                    },
                }

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs["headers"]
            return Response()

        fake_httpx = SimpleNamespace(post=fake_post)
        config = Config(gemini_api_key="secret-key")

        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            result = _call_gemini("SYSTEM", "USER", "", config)

        assert "secret-key" not in captured["url"]
        assert captured["headers"]["x-goog-api-key"] == "secret-key"
        assert result["text"] == "ok"
        assert result["text"] == "ok"

    def test_ollama_warmup_disables_openai_retries(self):
        captured: dict = {}

        class ChatCompletions:
            @staticmethod
            def create(**kwargs):
                captured["chat_kwargs"] = kwargs
                return SimpleNamespace()

        class OpenAI:
            def __init__(self, **kwargs):
                captured["client_kwargs"] = kwargs
                self.chat = SimpleNamespace(completions=ChatCompletions())

        fake_openai = SimpleNamespace(OpenAI=OpenAI)

        with patch.dict(sys.modules, {"openai": fake_openai}):
            _warmup_ollama_model(Config())

        assert captured["client_kwargs"]["max_retries"] == 0
