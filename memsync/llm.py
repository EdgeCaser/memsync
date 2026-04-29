from __future__ import annotations

import logging
import subprocess
import sys
from urllib.parse import urlparse

from memsync.config import Config

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised when all configured LLM backends fail."""


def call_llm(system: str, user: str, prefill: str, config: Config) -> dict:
    """
    Call the LLM with automatic fallback.

    Default chain: Gemini (primary) → Ollama (fallback).
    Set llm_backend = "anthropic" in config to use the legacy Anthropic path.

    Returns a dict with keys:
        text (str)          — full model response (includes prefill for Anthropic backend)
        input_tokens (int)
        output_tokens (int)
        truncated (bool)    — True if the response hit the token limit
        backend (str)       — which backend actually answered
    """
    backends = _resolve_backends(config)
    errors: list[str] = []

    for name, fn in backends:
        try:
            result = fn(system, user, prefill, config)
            result["backend"] = name
            return result
        except Exception as e:  # noqa: BLE001
            logger.warning("LLM backend '%s' failed: %s", name, e)
            errors.append(f"{name}: {e}")

    raise LLMError("All LLM backends failed:\n" + "\n".join(errors))


# ---------------------------------------------------------------------------
# Backend resolution
# ---------------------------------------------------------------------------

_BACKEND_FNS: dict[str, object] = {}

_adc_creds = None


def _resolve_backends(config: Config) -> list[tuple[str, object]]:
    """
    Return ordered list of (name, callable) backends to attempt.

    Primary backend is config.llm_backend.
    If that fails, config.fallback_backend is tried next.
    Set fallback_backend = "none" to disable fallback and hard-error on primary failure.
    """
    primary = config.llm_backend
    fallback = config.fallback_backend

    if primary not in _BACKEND_FNS:
        raise LLMError(f"Unknown llm_backend '{primary}'. Valid: {', '.join(_BACKEND_FNS)}")

    chain: list[tuple[str, object]] = [(primary, _BACKEND_FNS[primary])]

    if fallback and fallback != "none" and fallback != primary:
        if fallback not in _BACKEND_FNS:
            raise LLMError(
                f"Unknown fallback_backend '{fallback}'. Valid: {', '.join(_BACKEND_FNS)}"
            )
        chain.append((fallback, _BACKEND_FNS[fallback]))

    return chain


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


def _call_gemini(system: str, user: str, prefill: str, config: Config) -> dict:
    """
    Call the Gemini API using the native generateContent endpoint.

    Auth priority:
      1. gemini_api_key in config  →  passed as ?key= query param (no Bearer conflict)
      2. Application Default Credentials  →  OAuth Bearer token, no ?key=
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
        url = f"{url_base}?key={config.gemini_api_key}"
        headers = {"Content-Type": "application/json"}
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

    if sys.platform == "win32":
        # On Windows, npm CLI scripts are .cmd files — cmd.exe is required to execute them.
        cmd = ["cmd.exe", "/c", "gemini", "-m", config.gemini_model, "--yolo"] + headless_flag
    else:
        cmd = ["gemini", "-m", config.gemini_model, "--yolo"] + headless_flag

    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            input=full_prompt.encode("utf-8"),  # bytes bypasses Windows cp1252 encoding issues
            capture_output=True,
            timeout=600,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "gemini CLI not found. Install with: npm install -g @google/gemini-cli"
        ) from e

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
    """Ensure Ollama is reachable, starting and warming up the model if not."""
    import urllib.request

    health_url = _ollama_health_url(config.ollama_base_url)
    try:
        urllib.request.urlopen(health_url, timeout=timeout)  # noqa: S310
        return  # already running
    except Exception:  # noqa: BLE001, S110
        pass

    _start_ollama_service(config)


def _start_ollama_service(config: Config) -> None:
    """Start `ollama serve` as a detached background process, wait for it, then warm up."""
    import shutil
    import time
    import urllib.request

    if shutil.which("ollama") is None:
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
        subprocess.Popen(["ollama", "serve"], **kwargs)  # noqa: S603
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


_BACKEND_FNS["gemini"] = _call_gemini
_BACKEND_FNS["gemini_cli"] = _call_gemini_cli
_BACKEND_FNS["ollama"] = _call_ollama
_BACKEND_FNS["anthropic"] = _call_anthropic
