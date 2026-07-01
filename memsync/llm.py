from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from memsync.config import Config, normalize_backend_name

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised when all configured LLM backends fail."""


def call_llm(system: str, user: str, prefill: str, config: Config) -> dict:
    """
    Call the LLM with automatic fallback.

    Default chain: Codex → Claude Code → Gemini → Ollama.
    Set llm_backend = "anthropic" in config to use the legacy Anthropic path.

    Returns a dict with keys:
        text (str)          — full model response (includes prefill for Anthropic backend)
        input_tokens (int)
        output_tokens (int)
        truncated (bool)    — True if the response hit the token limit
        backend (str)       — which backend actually answered
    """
    backends = resolve_backends(config)
    errors: list[str] = []

    for name, _fn in backends:
        try:
            return call_llm_with_backend(name, system, user, prefill, config)
        except Exception as e:  # noqa: BLE001
            logger.warning("LLM backend '%s' failed: %s", name, e)
            errors.append(f"{name}: {e}")

    raise LLMError("All LLM backends failed:\n" + "\n".join(errors))


# ---------------------------------------------------------------------------
# Backend resolution
# ---------------------------------------------------------------------------

_BACKEND_FNS: dict[str, object] = {}

_adc_creds = None


def resolve_backends(config: Config) -> list[tuple[str, object]]:
    """Return ordered list of (name, callable) backends from config.llm_backends."""
    chain: list[tuple[str, object]] = []
    for raw_name in config.llm_backends:
        name = normalize_backend_name(raw_name)
        if name not in _BACKEND_FNS:
            logger.warning("Unknown backend '%s' in backends list — skipping", name)
            continue
        chain.append((name, _BACKEND_FNS[name]))
    if not chain:
        raise LLMError(
            f"No valid backends configured. Valid: {', '.join(_BACKEND_FNS)}"
        )
    return chain


def call_llm_with_backend(
    backend: str,
    system: str,
    user: str,
    prefill: str,
    config: Config,
) -> dict:
    """Call one specific LLM backend without consulting the fallback chain."""
    name = normalize_backend_name(backend)
    fn = _BACKEND_FNS.get(name)
    if fn is None:
        raise LLMError(
            f"Unknown LLM backend '{name}'. Valid: {', '.join(_BACKEND_FNS)}"
        )

    result = fn(system, user, prefill, config)
    result["backend"] = name
    return result


# ---------------------------------------------------------------------------
# Per-backend helpers
# ---------------------------------------------------------------------------

def _inject_prefill(system: str, prefill: str) -> str:
    """
    Embed a prefill hint into the system prompt for non-Anthropic backends.
    These backends don't support true assistant-turn seeding, so we instruct
    the model explicitly to start its output with the given text.
    """
    if not prefill:
        return system
    return (
        system
        + f"\n\nCRITICAL: Begin your response with exactly this text"
        f" (no preamble, no code fences, no explanation before it):\n{prefill}"
    )


def _windows_cli_fallbacks(name: str) -> list[Path]:
    """Return common Windows install locations for CLIs we invoke."""
    home = Path.home()
    appdata = os.environ.get("APPDATA")
    localappdata = os.environ.get("LOCALAPPDATA")

    candidates: list[Path] = []
    if name in {"codex", "gemini"} and appdata:
        candidates.append(Path(appdata) / "npm" / f"{name}.cmd")
    elif name == "claude":
        candidates.append(home / ".local" / "bin" / "claude.exe")
    elif name == "ollama" and localappdata:
        candidates.append(Path(localappdata) / "Programs" / "Ollama" / "ollama.exe")
    return candidates


def _resolve_cli_path(name: str) -> str | None:
    """Resolve a CLI from PATH, with Windows fallbacks for non-interactive jobs."""
    import shutil

    resolved = shutil.which(name)
    if resolved is not None:
        return resolved

    if sys.platform != "win32":
        return None

    for candidate in _windows_cli_fallbacks(name):
        if candidate.exists():
            return str(candidate)
    return None


def _build_cli_command(cli_path: str, *args: str) -> list[str]:
    """Build a subprocess argv that can invoke direct binaries and .cmd shims."""
    if sys.platform == "win32" and cli_path.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", cli_path, *args]
    return [cli_path, *args]


def no_window_kwargs() -> dict:
    """subprocess kwargs that suppress the console-window flash on Windows.

    The daemon runs detached (no console of its own), so every child CLI call
    would otherwise allocate a fresh console window and steal focus — a flash
    per backend call, per session, on every scheduled cycle. CREATE_NO_WINDOW
    runs the child without a window. Returns {} on non-Windows platforms, where
    there is no console to create.
    """
    if sys.platform == "win32":
        # getattr keeps this safe under tests that patch sys.platform to "win32"
        # on a POSIX host, where the constant does not exist (0 == no flags).
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _call_gemini(system: str, user: str, prefill: str, config: Config) -> dict:
    """
    Call the Gemini API using the native generateContent endpoint.

    Auth priority:
      1. gemini_api_key in config  →  passed as x-goog-api-key header
      2. Application Default Credentials  →  OAuth Bearer token
    """
    try:
        import httpx
    except ImportError as e:
        raise ImportError("httpx package required: pip install httpx") from e

    system_prompt = _inject_prefill(system, prefill)
    model = config.gemini_model
    url_base = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": config.max_tokens},
    }

    if config.gemini_api_key:
        url = url_base
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": config.gemini_api_key,
        }
    else:
        # ADC path — Bearer token, no ?key= param
        creds = _get_adc_creds()
        url = url_base
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {creds.token}",
        }

    response = httpx.post(url, headers=headers, json=body, timeout=60)
    response.raise_for_status()
    data = response.json()

    candidate = data["candidates"][0]
    text = candidate["content"]["parts"][0]["text"]
    truncated = candidate.get("finishReason") == "MAX_TOKENS"
    usage = data.get("usageMetadata", {})

    return {
        "text": text,
        "input_tokens": usage.get("promptTokenCount", 0),
        "output_tokens": usage.get("candidatesTokenCount", 0),
        "truncated": truncated,
    }


def _call_gemini_cli(system: str, user: str, prefill: str, config: Config) -> dict:
    """
    Call Gemini via the installed `gemini` CLI tool (@google/gemini-cli).

    Uses the CLI's own Google account OAuth — no API key required.
    Prompt is passed via stdin to avoid Windows command line length limits.
    """
    full_prompt = _inject_prefill(system, prefill) + "\n\n" + user

    # -p with a short string triggers headless mode; full content comes from stdin.
    headless_flag = ["-p", "Process the task from stdin and return only the requested output."]

    cli_path = _resolve_cli_path("gemini")
    if cli_path is None:
        raise RuntimeError(
            "gemini CLI not found. Install with: npm install -g @google/gemini-cli"
        )

    cmd = _build_cli_command(
        cli_path,
        "-m",
        config.gemini_model,
        "--yolo",
        *headless_flag,
    )

    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            input=full_prompt.encode("utf-8"),  # bytes bypasses Windows cp1252 encoding issues
            capture_output=True,
            timeout=600,
            **no_window_kwargs(),
        )
    except FileNotFoundError as e:
        raise RuntimeError("gemini CLI not found on PATH") from e

    if result.returncode != 0:
        stderr_text = result.stderr.decode("utf-8", errors="replace").strip()
        if "ERR_STREAM_PREMATURE_CLOSE" in stderr_text or "Premature close" in stderr_text:
            raise RuntimeError(
                "gemini CLI quota/rate-limit (ERR_STREAM_PREMATURE_CLOSE) — "
                "daily token quota likely exhausted; will retry next run"
            )
        raise RuntimeError(
            f"gemini CLI failed (exit {result.returncode}): {stderr_text}"
        )

    return {
        "text": result.stdout.decode("utf-8", errors="replace").strip(),
        "input_tokens": 0,
        "output_tokens": 0,
        "truncated": False,
    }


def _ollama_health_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def _check_ollama_reachable(config: Config, timeout: float = 3.0) -> None:
    """Ensure Ollama is reachable AND the target model is warm.

    A running-but-cold Ollama (model not yet in RAM) is the common cause of
    the very first chat call timing out — the daemon hit this on 5/5 (see
    commit 2d8a1d0 for the auto-start case). Warm-up is cheap when the model
    is already loaded, so it's safe to do on every call.
    """
    import urllib.request

    health_url = _ollama_health_url(config.ollama_base_url)
    try:
        urllib.request.urlopen(health_url, timeout=timeout)  # noqa: S310
    except Exception:  # noqa: BLE001, S110
        _start_ollama_service(config)
        return  # _start_ollama_service already warms up

    _ensure_model_loaded(config)


def _ensure_model_loaded(config: Config) -> None:
    """Warm up the model if it isn't already resident in RAM.

    Cheap path: query /api/ps to see if the model is loaded. If yes, return
    immediately. If no (or the probe fails), fall through to a 1-token warm-up.
    """
    import json as _json
    import urllib.request

    parsed = urlparse(config.ollama_base_url)
    ps_url = f"{parsed.scheme}://{parsed.netloc}/api/ps"
    try:
        with urllib.request.urlopen(ps_url, timeout=3) as resp:  # noqa: S310
            body = _json.loads(resp.read().decode("utf-8", errors="replace"))
        loaded = {m.get("name") or m.get("model") for m in body.get("models", [])}
        if config.ollama_model in loaded:
            return
    except Exception:  # noqa: BLE001, S110
        pass  # fall through to warm-up; better safe than sorry

    logger.info("Ollama reachable but model %s not warm — warming up", config.ollama_model)
    _warmup_ollama_model(config)


def _start_ollama_service(config: Config) -> None:
    """Start `ollama serve` as a detached background process, wait for it, then warm up."""
    import time
    import urllib.request

    ollama_path = _resolve_cli_path("ollama")
    if ollama_path is None:
        raise RuntimeError(
            "Ollama is not reachable and 'ollama' binary not found — "
            "install from https://ollama.com"
        )

    logger.info("Ollama not running — starting 'ollama serve' in the background")

    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True

    try:
        subprocess.Popen([ollama_path, "serve"], **kwargs)  # noqa: S603
    except Exception as e:
        raise RuntimeError(f"Failed to start Ollama: {e}") from e

    health_url = _ollama_health_url(config.ollama_base_url)
    for _ in range(10):
        time.sleep(2)
        try:
            urllib.request.urlopen(health_url, timeout=2)  # noqa: S310
            logger.info("Ollama started — warming up model %s", config.ollama_model)
            _warmup_ollama_model(config)
            return
        except Exception:  # noqa: BLE001, S110
            pass

    raise RuntimeError(
        f"Ollama was started but did not become reachable at {health_url} within 20s"
    )


# Warm-up timeout is intentionally long: first load pulls the model into RAM,
# which can take several minutes on a Raspberry Pi.
_OLLAMA_WARMUP_TIMEOUT = 300


def _warmup_ollama_model(config: Config) -> None:
    """Send a minimal 1-token prompt to force the model to load before harvest calls arrive."""
    try:
        import openai
    except ImportError:
        return  # openai not installed — skip warmup, real call will handle the import error

    client = openai.OpenAI(
        api_key="ollama",
        base_url=config.ollama_base_url,
        timeout=_OLLAMA_WARMUP_TIMEOUT,
        max_retries=0,
    )
    try:
        client.chat.completions.create(
            model=config.ollama_model,
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
            extra_body={"options": {"num_ctx": config.ollama_num_ctx}},
        )
        logger.info("Ollama model %s is ready", config.ollama_model)
    except Exception as e:
        logger.warning("Ollama warm-up failed (model may still load on first use): %s", e)


_CANDIDATE_FACTS_MARKER = "\n\nCANDIDATE FACTS:\n"


def _truncate_user_for_ollama(user: str, system: str, num_ctx: int) -> str | None:
    """
    Attempt to truncate user to fit within num_ctx tokens alongside system.

    For merge-call prompts (containing CANDIDATE FACTS section), truncates the
    memory section and preserves the candidates. Returns None if even candidates
    alone won't fit.
    """
    # 200-token safety margin; *4 converts token budget to char budget
    max_user_chars = (num_ctx - len(system) // 4 - 200) * 4
    if max_user_chars < 200 * 4:
        return None

    if len(user) <= max_user_chars:
        return user

    idx = user.find(_CANDIDATE_FACTS_MARKER)
    if idx == -1:
        # No semantic structure — truncate from end
        return user[:max_user_chars] + "\n[PROMPT TRUNCATED TO FIT CONTEXT WINDOW]"

    candidates_section = user[idx:]
    if len(candidates_section) >= max_user_chars:
        return None  # candidates alone won't fit

    memory_budget = max_user_chars - len(candidates_section) - 60
    return (
        user[:memory_budget]
        + "\n[MEMORY TRUNCATED TO FIT OLLAMA CONTEXT WINDOW]\n"
        + candidates_section
    )


def _call_ollama(system: str, user: str, prefill: str, config: Config) -> dict:
    _check_ollama_reachable(config)

    estimated_tokens = (len(system) + len(user)) // 4
    if estimated_tokens > config.ollama_num_ctx:
        truncated_user = _truncate_user_for_ollama(user, system, config.ollama_num_ctx)
        if truncated_user is None:
            raise RuntimeError(
                f"prompt too large for Ollama: ~{estimated_tokens} estimated tokens "
                f"exceeds ollama_num_ctx={config.ollama_num_ctx} — cannot truncate safely"
            )
        logger.warning(
            "Ollama prompt (~%d tokens) exceeds context window (%d); truncating memory section",
            estimated_tokens,
            config.ollama_num_ctx,
        )
        user = truncated_user

    try:
        import openai
    except ImportError as e:
        raise ImportError("openai package required: pip install openai") from e

    client = openai.OpenAI(
        api_key="ollama",  # required by the openai client, not validated by Ollama
        base_url=config.ollama_base_url,
        timeout=config.ollama_timeout,
        max_retries=0,
    )

    response = client.chat.completions.create(
        model=config.ollama_model,
        max_tokens=config.max_tokens,
        messages=[
            {"role": "system", "content": _inject_prefill(system, prefill)},
            {"role": "user", "content": user},
        ],
        extra_body={"options": {"num_ctx": config.ollama_num_ctx}},
    )

    choice = response.choices[0]
    return {
        "text": choice.message.content or "",
        "input_tokens": response.usage.prompt_tokens if response.usage else 0,
        "output_tokens": response.usage.completion_tokens if response.usage else 0,
        "truncated": choice.finish_reason == "length",
    }


def _call_anthropic(system: str, user: str, prefill: str, config: Config) -> dict:
    try:
        import anthropic
    except ImportError as e:
        raise ImportError(
            "anthropic package required for legacy backend: pip install anthropic"
        ) from e

    client = anthropic.Anthropic(api_key=config.api_key or None)

    messages: list[dict] = [{"role": "user", "content": user}]
    if prefill:
        messages.append({"role": "assistant", "content": prefill})

    response = client.messages.create(
        model=config.model,
        max_tokens=config.max_tokens,
        system=system,
        messages=messages,
    )

    # For Anthropic, the response is only the continuation — prepend the prefill
    # so callers always receive the complete output.
    text = (prefill + response.content[0].text) if prefill else response.content[0].text

    return {
        "text": text,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "truncated": response.stop_reason == "max_tokens",
    }


def _get_adc_creds():
    global _adc_creds
    try:
        import google.auth
        import google.auth.transport.requests
    except ImportError as e:
        raise ImportError("google-auth required for ADC: pip install google-auth") from e

    if _adc_creds is None:
        _adc_creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/generative-language"]
        )

    if not _adc_creds.valid:
        _adc_creds.refresh(google.auth.transport.requests.Request())

    return _adc_creds


def _call_claude_code(system: str, user: str, prefill: str, config: Config) -> dict:
    """
    Call Claude via the locally installed `claude --print` CLI.

    Uses the Max/Pro subscription — no API key or per-token billing.
    Tools are disabled so this is a pure text-completion call.
    The model is pinned via config (default haiku/low) so merges never
    inherit an expensive default model from the user's CLI settings.
    """
    cli_path = _resolve_cli_path("claude")
    if cli_path is None:
        raise RuntimeError(
            "claude CLI not found on PATH — install from https://claude.ai/code"
        )

    full_prompt = _inject_prefill(system, prefill) + "\n\n" + user

    args = [
        "--print",
        "--no-session-persistence",
        "--tools",
        "",
    ]
    if config.claude_code_model:
        args += ["--model", config.claude_code_model]
    if config.claude_code_effort:
        args += ["--effort", config.claude_code_effort]

    cmd = _build_cli_command(cli_path, *args)

    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            input=full_prompt.encode("utf-8"),
            capture_output=True,
            timeout=config.claude_code_timeout,
            cwd=tempfile.gettempdir(),  # neutral dir — avoids loading any project CLAUDE.md
            **no_window_kwargs(),
        )
    except FileNotFoundError as e:
        raise RuntimeError("claude CLI not found on PATH") from e

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"claude CLI failed (exit {result.returncode}): {stderr}")

    return {
        "text": result.stdout.decode("utf-8", errors="replace").strip(),
        "input_tokens": 0,
        "output_tokens": 0,
        "truncated": False,
    }


def _call_codex(system: str, user: str, prefill: str, config: Config) -> dict:  # noqa: ARG001
    """
    Call OpenAI via the `codex` CLI (OAuth — no API key needed with a ChatGPT account).

    Install with: npm install -g @openai/codex
    """
    cli_path = _resolve_cli_path("codex")
    if cli_path is None:
        raise RuntimeError(
            "codex CLI not found — install with: npm install -g @openai/codex"
        )

    full_prompt = _inject_prefill(system, prefill) + "\n\n" + user

    # Use stdin rather than argv so large harvest prompts do not hit Windows
    # command-line length limits. --skip-git-repo-check keeps scheduled runs
    # working when they start outside a repository.
    # --ignore-user-config: prevents codex from using its own stored memories,
    # which can corrupt responses when they contain context from unrelated projects.
    # Auth still works — codex docs confirm CODEX_HOME is still used for auth.
    # --ephemeral: no session persistence.
    # low reasoning effort: sufficient for memory-update tasks, much faster than medium.
    cmd = _build_cli_command(
        cli_path,
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--color",
        "never",
        "-c",
        'model_reasoning_effort="low"',
        "-",
    )

    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            input=full_prompt.encode("utf-8"),
            capture_output=True,
            timeout=config.codex_timeout,
            **no_window_kwargs(),
        )
    except FileNotFoundError as e:
        raise RuntimeError("codex CLI not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"codex CLI timed out after {config.codex_timeout} seconds") from e

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"codex CLI failed (exit {result.returncode}): {stderr}")

    return {
        "text": result.stdout.decode("utf-8", errors="replace").strip(),
        "input_tokens": 0,
        "output_tokens": 0,
        "truncated": False,
    }


_BACKEND_FNS["claude_code"] = _call_claude_code
_BACKEND_FNS["codex"] = _call_codex
_BACKEND_FNS["gemini"] = _call_gemini
_BACKEND_FNS["gemini_cli"] = _call_gemini_cli
_BACKEND_FNS["ollama"] = _call_ollama
_BACKEND_FNS["anthropic"] = _call_anthropic
