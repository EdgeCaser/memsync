from __future__ import annotations

import logging
import subprocess
import sys

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

_BACKEND_FNS: dict[str, object] = {
    "gemini": None,       # filled below after function definitions
    "gemini_cli": None,
    "ollama": None,
    "anthropic": None,
}


def _resolve_backends(config: Config) -> list[tuple[str, object]]:
    """
    Return ordered list of (name, callable) backends to attempt.

    Primary backend is config.llm_backend.
    If that fails, config.fallback_backend is tried next.
    Set fallback_backend = "none" to disable fallback and hard-error on primary failure.
    """
    _BACKEND_FNS["gemini"] = _call_gemini
    _BACKEND_FNS["gemini_cli"] = _call_gemini_cli
    _BACKEND_FNS["ollama"] = _call_ollama
    _BACKEND_FNS["anthropic"] = _call_anthropic

    primary = config.llm_backend
    fallback = config.fallback_backend

    if primary not in _BACKEND_FNS:
        raise LLMError(f"Unknown llm_backend '{primary}'. Valid: {', '.join(_BACKEND_FNS)}")

    chain: list[tuple[str, object]] = [(primary, _BACKEND_FNS[primary])]

    if fallback and fallback != "none" and fallback != primary:
        if fallback not in _BACKEND_FNS:
            raise LLMError(f"Unknown fallback_backend '{fallback}'. Valid: {', '.join(_BACKEND_FNS)}")
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
        try:
            import google.auth
            import google.auth.transport.requests
        except ImportError as e:
            raise ImportError("google-auth required for ADC: pip install google-auth") from e

        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/generative-language"]
        )
        creds.refresh(google.auth.transport.requests.Request())
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
            timeout=300,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "gemini CLI not found. Install with: npm install -g @google/gemini-cli"
        ) from e

    if result.returncode != 0:
        raise RuntimeError(
            f"gemini CLI failed (exit {result.returncode}): "
            f"{result.stderr.decode('utf-8', errors='replace').strip()}"
        )

    return {
        "text": result.stdout.decode("utf-8", errors="replace").strip(),
        "input_tokens": 0,
        "output_tokens": 0,
        "truncated": False,
    }


def _call_ollama(system: str, user: str, prefill: str, config: Config) -> dict:
    try:
        import openai
    except ImportError as e:
        raise ImportError("openai package required: pip install openai") from e

    client = openai.OpenAI(
        api_key="ollama",  # required by the openai client, not validated by Ollama
        base_url=config.ollama_base_url,
    )

    response = client.chat.completions.create(
        model=config.ollama_model,
        max_tokens=config.max_tokens,
        messages=[
            {"role": "system", "content": _inject_prefill(system, prefill)},
            {"role": "user", "content": user},
        ],
        extra_body={"options": {"num_ctx": 32768}},  # expand context window beyond 8K default
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
