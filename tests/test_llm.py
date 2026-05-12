from __future__ import annotations

from unittest.mock import patch

from memsync.config import Config
from memsync.llm import _call_codex


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

        with patch("memsync.llm._resolve_cli_path", return_value="C:/Users/test/AppData/Roaming/npm/codex.cmd"):
            with patch("memsync.llm.subprocess.run", side_effect=fake_run):
                result = _call_codex("SYSTEM", "USER", "", config)

        assert captured["cmd"][0:3] == ["cmd.exe", "/c", "C:/Users/test/AppData/Roaming/npm/codex.cmd"]
        assert "exec" in captured["cmd"]
        assert captured["cmd"][-1] == "-"
        assert captured["input"] == b"SYSTEM\n\nUSER"
        assert result["text"] == "ok"
