from __future__ import annotations

import os
import platform
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LLM_BACKENDS = ["claude_code", "gemini_cli", "ollama"]
BACKEND_ALIASES = {
    "claude": "claude_code",
}
DEFAULT_BACKEND_HARVEST_CHUNK_TOKENS = {
    "codex": 8000,
    "claude_code": 8000,
    "gemini": 6000,
    "gemini_cli": 6000,
    "ollama": 2500,
    "anthropic": 8000,
}
BACKEND_HARVEST_CHUNK_FIELDS = {
    "codex": "harvest_chunk_tokens_codex",
    "claude_code": "harvest_chunk_tokens_claude_code",
    "gemini": "harvest_chunk_tokens_gemini",
    "gemini_cli": "harvest_chunk_tokens_gemini_cli",
    "ollama": "harvest_chunk_tokens_ollama",
    "anthropic": "harvest_chunk_tokens_anthropic",
}


def normalize_backend_name(name: str) -> str:
    """Map user-facing backend aliases onto internal backend keys."""
    return BACKEND_ALIASES.get(name, name)


def _coerce_schedule(value: str | list[str]) -> list[str]:
    """Accept a single cron string or a list; always return a non-empty list."""
    if isinstance(value, str):
        return [value]
    return list(value) if value else ["55 23 * * *"]


def normalize_backends(names: list[str]) -> list[str]:
    """Normalize backend aliases, drop duplicates, and skip disabled entries."""
    normalized: list[str] = []
    seen: set[str] = set()
    for name in names:
        if not name or name == "none":
            continue
        canonical = normalize_backend_name(name)
        if canonical in seen:
            continue
        normalized.append(canonical)
        seen.add(canonical)
    return normalized


def harvest_chunk_tokens_for_backend(config: Config, name: str) -> int:
    """
    Return the configured harvest chunk size for one backend.

    Backend-specific overrides take precedence. A zero or negative override falls
    back to the shared harvest_chunk_tokens value.
    """
    canonical = normalize_backend_name(name)
    field_name = BACKEND_HARVEST_CHUNK_FIELDS.get(canonical)
    specific = getattr(config, field_name, 0) if field_name else 0
    return specific if specific > 0 else config.harvest_chunk_tokens


def instruction_targets(config: Config) -> list[tuple[str, Path]]:
    """Return configured instruction targets for supported agents."""
    targets = [
        ("CLAUDE.md", config.claude_md_target),
        ("AGENTS.md", config.codex_agents_target),
    ]
    deduped: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for label, path in targets:
        if path is None:
            continue
        expanded = path.expanduser()
        key = str(expanded).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append((label, expanded))
    return deduped


@dataclass
class DaemonConfig:
    """
    Configuration for the optional daemon module.
    Only present in config.toml if the user has run 'memsync daemon install'.
    All features default to reasonable values; none are on by default except
    scheduled refresh and backup mirror (which requires a path to be set).
    """
    enabled: bool = True

    # Scheduled refresh — reads today's session log and calls the Claude API
    refresh_enabled: bool = True
    refresh_schedule: list[str] = None  # type: ignore[assignment]  # set in __post_init__

    def __post_init__(self) -> None:
        if self.refresh_schedule is None:
            self.refresh_schedule = ["55 23 * * *"]

    # Backup mirror — local rsync copy of .claude-memory/ (empty = disabled)
    backup_mirror_path: str = ""
    backup_mirror_schedule: str = "0 * * * *"  # hourly

    # Web UI — browser-based view/edit of GLOBAL_MEMORY.md
    web_ui_enabled: bool = True
    web_ui_port: int = 5000
    web_ui_host: str = "0.0.0.0"  # noqa: S104  # 0.0.0.0 = LAN; 127.0.0.1 = localhost only

    # Mobile capture endpoint — REST POST for iPhone Shortcuts etc.
    capture_enabled: bool = True
    capture_port: int = 5001
    capture_token: str = ""                     # empty = no auth (local network only)

    # Drift detection — alerts when CLAUDE.md is stale
    drift_check_enabled: bool = True
    drift_check_interval_hours: int = 6
    drift_notify: str = "log"                   # "log", "email", or "file"

    # Nightly harvest — sweeps ~/.claude/projects/ and extracts memories from session transcripts
    harvest_enabled: bool = True
    harvest_schedule: str = "0 2 * * *"            # 2am daily
    harvest_projects_dir: str = ""                  # empty = ~/.claude/projects (default)
    harvest_allow_ollama: bool = False              # unattended local LLM work can run hot
    harvest_max_runtime_seconds: int = 1800          # stop starting new sessions after 30 min
    harvest_max_sessions_per_run: int = 25           # prevent one backlog from monopolizing host
    harvest_lock_stale_seconds: int = 3600           # store lock: older = abandoned, steal

    # Weekly digest email
    digest_enabled: bool = False
    digest_schedule: str = "0 9 * * 1"         # Monday 9am
    digest_email_to: str = ""
    digest_email_from: str = ""
    digest_smtp_host: str = ""
    digest_smtp_port: int = 587
    digest_smtp_user: str = ""
    digest_smtp_password: str = ""              # prefer MEMSYNC_SMTP_PASSWORD env var


@dataclass
class Config:
    # [core]
    provider: str = "onedrive"
    model: str = "claude-sonnet-4-20250514"   # used only when llm_backend = "anthropic"
    max_memory_lines: int = 400
    max_tokens: int = 16384     # API response ceiling — must exceed tokenized memory file size
    api_key: str = ""           # Anthropic API key (legacy); stored in config.toml, not env

    # [llm] — backend selection and per-backend settings
    llm_backends: list = field(default_factory=lambda: list(DEFAULT_LLM_BACKENDS))
    llm_backend: str = DEFAULT_LLM_BACKENDS[0]   # legacy; ignored when llm_backends is set
    fallback_backend: str = DEFAULT_LLM_BACKENDS[1]  # legacy; ignored when llm_backends is set
    claude_code_model: str = "haiku"         # alias for `claude --model`; "" = inherit CLI default
    claude_code_effort: str = "low"          # passed to `claude --effort`; "" = inherit CLI default
    claude_code_timeout: int = 420           # seconds; headroom over a full merge, caps hangs
    gemini_api_key: str = ""                 # AI Studio key; leave empty to use ADC instead
    gemini_model: str = "gemini-2.5-flash"    # any model available on your Gemini account
    ollama_base_url: str = "http://localhost:11434/v1"  # Ollama OpenAI-compatible endpoint
    ollama_model: str = "llama3.2:3b"        # ~2GB RAM; good balance of quality and Pi headroom
    codex_timeout: int = 600                 # seconds; codex can be slow on large chunks
    ollama_timeout: int = 300                # seconds; covers cold-load of mid-sized models
    ollama_num_ctx: int = 8192               # context window; 32K OOMs the 1b on an 8GB Pi
    harvest_chunk_tokens: int = 6000         # split transcripts into chunks this size; 0 = one-shot
    harvest_chunk_tokens_codex: int = DEFAULT_BACKEND_HARVEST_CHUNK_TOKENS["codex"]
    harvest_chunk_tokens_claude_code: int = DEFAULT_BACKEND_HARVEST_CHUNK_TOKENS["claude_code"]
    harvest_chunk_tokens_gemini: int = DEFAULT_BACKEND_HARVEST_CHUNK_TOKENS["gemini"]
    harvest_chunk_tokens_gemini_cli: int = DEFAULT_BACKEND_HARVEST_CHUNK_TOKENS["gemini_cli"]
    harvest_chunk_tokens_ollama: int = DEFAULT_BACKEND_HARVEST_CHUNK_TOKENS["ollama"]
    harvest_chunk_tokens_anthropic: int = DEFAULT_BACKEND_HARVEST_CHUNK_TOKENS["anthropic"]
    chunk_inter_call_sleep: int = 5          # seconds between chunk extract calls; avoids RPM 429s

    # [archive] — tiered memory
    max_hot_lines: int = 100                 # hard cap on GLOBAL_MEMORY.md (hot layer)
    archive_in_harvest: bool = True          # whether harvest/refresh consults MEMORY_ARCHIVE.md
    archive_max_lines_in_prompt: int = 300   # truncation limit for archive in LLM call
    # merge appends new cold entries instead of regenerating the whole archive
    # (keeps merges fast and makes it impossible to clobber the archive)
    harvest_append_only_cold: bool = True

    # [projection] — progressive disclosure (docs/progressive-disclosure-memory-design.md)
    # When enabled, instruction targets sync from the generated core rather than
    # from GLOBAL_MEMORY.md, so per-project detail stops being resident context.
    projection_enabled: bool = False
    core_max_chars: int = 30000              # enforced budget on the generated core

    # [paths]
    sync_root: Path | None = None           # None = use provider auto-detect
    claude_md_target: Path = None           # set in __post_init__
    codex_agents_target: Path = None        # set in __post_init__
    project_cwd: Path | None = None         # Optional: working directory for Claude

    # [backups]
    keep_days: int = 30

    # [daemon] — only populated when daemon is installed
    daemon: DaemonConfig = field(default_factory=DaemonConfig)

    def __post_init__(self) -> None:
        if self.claude_md_target is None:
            self.claude_md_target = Path("~/.claude/CLAUDE.md").expanduser()
        if self.codex_agents_target is None:
            self.codex_agents_target = Path("~/AGENTS.md").expanduser()

    @classmethod
    def load(cls) -> Config:
        """Load config from disk, returning defaults if the file doesn't exist."""
        path = get_config_path()
        if not path.exists():
            return cls()
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        return cls._from_dict(raw)

    @classmethod
    def _from_dict(cls, raw: dict) -> Config:
        core = raw.get("core", {})
        paths = raw.get("paths", {})
        backups = raw.get("backups", {})

        sync_root = paths.get("sync_root")
        claude_md_target_str = paths.get("claude_md_target")
        codex_agents_target_str = paths.get("codex_agents_target")
        project_cwd_str = paths.get("project_cwd") # Add this line

        # Daemon section — only present if user has run 'memsync daemon install'
        daemon_raw = raw.get("daemon", {})
        daemon = DaemonConfig(
            enabled=daemon_raw.get("enabled", True),
            refresh_enabled=daemon_raw.get("refresh_enabled", True),
            refresh_schedule=_coerce_schedule(daemon_raw.get("refresh_schedule", ["55 23 * * *"])),
            backup_mirror_path=daemon_raw.get("backup_mirror_path", ""),
            backup_mirror_schedule=daemon_raw.get("backup_mirror_schedule", "0 * * * *"),
            web_ui_enabled=daemon_raw.get("web_ui_enabled", True),
            web_ui_port=daemon_raw.get("web_ui_port", 5000),
            web_ui_host=daemon_raw.get("web_ui_host", "0.0.0.0"),  # noqa: S104
            capture_enabled=daemon_raw.get("capture_enabled", True),
            capture_port=daemon_raw.get("capture_port", 5001),
            capture_token=daemon_raw.get("capture_token", ""),
            drift_check_enabled=daemon_raw.get("drift_check_enabled", True),
            drift_check_interval_hours=daemon_raw.get("drift_check_interval_hours", 6),
            drift_notify=daemon_raw.get("drift_notify", "log"),
            harvest_enabled=daemon_raw.get("harvest_enabled", True),
            harvest_schedule=daemon_raw.get("harvest_schedule", "0 2 * * *"),
            harvest_projects_dir=daemon_raw.get("harvest_projects_dir", ""),
            harvest_allow_ollama=daemon_raw.get("harvest_allow_ollama", False),
            harvest_max_runtime_seconds=daemon_raw.get("harvest_max_runtime_seconds", 1800),
            harvest_max_sessions_per_run=daemon_raw.get("harvest_max_sessions_per_run", 25),
            harvest_lock_stale_seconds=daemon_raw.get("harvest_lock_stale_seconds", 3600),
            digest_enabled=daemon_raw.get("digest_enabled", False),
            digest_schedule=daemon_raw.get("digest_schedule", "0 9 * * 1"),
            digest_email_to=daemon_raw.get("digest_email_to", ""),
            digest_email_from=daemon_raw.get("digest_email_from", ""),
            digest_smtp_host=daemon_raw.get("digest_smtp_host", ""),
            digest_smtp_port=daemon_raw.get("digest_smtp_port", 587),
            digest_smtp_user=daemon_raw.get("digest_smtp_user", ""),
            digest_smtp_password=daemon_raw.get("digest_smtp_password", ""),
        )

        llm_raw = raw.get("llm", {})

        # Resolve backend chain — new list form takes precedence over legacy keys
        if "backends" in llm_raw:
            llm_backends = normalize_backends(llm_raw["backends"])
        elif "backend" in llm_raw or "fallback_backend" in llm_raw:
            primary = llm_raw.get("backend", DEFAULT_LLM_BACKENDS[0])
            fallback = llm_raw.get("fallback_backend", DEFAULT_LLM_BACKENDS[1])
            llm_backends = normalize_backends([primary, fallback])
        else:
            llm_backends = list(DEFAULT_LLM_BACKENDS)

        instance = cls(
            provider=core.get("provider", "onedrive"),
            model=core.get("model", "claude-sonnet-4-20250514"),
            max_memory_lines=core.get("max_memory_lines", 400),
            max_tokens=core.get("max_tokens", 16384),
            api_key=core.get("api_key", ""),
            llm_backends=llm_backends,
            llm_backend=llm_backends[0] if llm_backends else DEFAULT_LLM_BACKENDS[0],
            fallback_backend=llm_backends[1] if len(llm_backends) > 1 else "none",
            claude_code_model=llm_raw.get("claude_code_model", "haiku"),
            claude_code_effort=llm_raw.get("claude_code_effort", "low"),
            claude_code_timeout=llm_raw.get("claude_code_timeout", 420),
            gemini_api_key=llm_raw.get("gemini_api_key", ""),
            gemini_model=llm_raw.get("gemini_model", "gemini-2.5-flash"),
            ollama_base_url=llm_raw.get("ollama_base_url", "http://localhost:11434/v1"),
            ollama_model=llm_raw.get("ollama_model", "llama3.2:3b"),
            codex_timeout=llm_raw.get("codex_timeout", 600),
            ollama_timeout=llm_raw.get("ollama_timeout", 120),
            ollama_num_ctx=llm_raw.get("ollama_num_ctx", 8192),
            harvest_chunk_tokens=llm_raw.get("harvest_chunk_tokens", 6000),
            harvest_chunk_tokens_codex=llm_raw.get(
                "harvest_chunk_tokens_codex",
                DEFAULT_BACKEND_HARVEST_CHUNK_TOKENS["codex"],
            ),
            harvest_chunk_tokens_claude_code=llm_raw.get(
                "harvest_chunk_tokens_claude_code",
                DEFAULT_BACKEND_HARVEST_CHUNK_TOKENS["claude_code"],
            ),
            harvest_chunk_tokens_gemini=llm_raw.get(
                "harvest_chunk_tokens_gemini",
                DEFAULT_BACKEND_HARVEST_CHUNK_TOKENS["gemini"],
            ),
            harvest_chunk_tokens_gemini_cli=llm_raw.get(
                "harvest_chunk_tokens_gemini_cli",
                DEFAULT_BACKEND_HARVEST_CHUNK_TOKENS["gemini_cli"],
            ),
            harvest_chunk_tokens_ollama=llm_raw.get(
                "harvest_chunk_tokens_ollama",
                DEFAULT_BACKEND_HARVEST_CHUNK_TOKENS["ollama"],
            ),
            harvest_chunk_tokens_anthropic=llm_raw.get(
                "harvest_chunk_tokens_anthropic",
                DEFAULT_BACKEND_HARVEST_CHUNK_TOKENS["anthropic"],
            ),
            chunk_inter_call_sleep=llm_raw.get("chunk_inter_call_sleep", 5),
            max_hot_lines=core.get("max_hot_lines", 100),
            archive_in_harvest=core.get("archive_in_harvest", True),
            archive_max_lines_in_prompt=core.get("archive_max_lines_in_prompt", 300),
            harvest_append_only_cold=core.get("harvest_append_only_cold", True),
            projection_enabled=core.get("projection_enabled", False),
            core_max_chars=core.get("core_max_chars", 30000),
            sync_root=Path(sync_root) if sync_root else None,
            claude_md_target=(
                Path(claude_md_target_str).expanduser() if claude_md_target_str else None
            ),
            codex_agents_target=(
                Path(codex_agents_target_str).expanduser()
                if codex_agents_target_str else None
            ),
            project_cwd=( # Add this block
                Path(project_cwd_str).expanduser() if project_cwd_str else None
            ),
            keep_days=backups.get("keep_days", 30),
            daemon=daemon,
        )
        return instance

    def save(self) -> None:
        """Write config to disk, creating parent directories if needed."""
        path = get_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._to_toml(), encoding="utf-8")

    def _to_toml(self) -> str:
        """
        Serialize config to TOML manually.
        tomllib is read-only (stdlib). Schema is simple enough that manual
        serialization avoids needing a tomli_w dependency.
        """
        llm_backends = normalize_backends(self.llm_backends) or list(DEFAULT_LLM_BACKENDS)
        lines = [
            "[core]",
            f'provider = "{self.provider}"',
            f'model = "{self.model}"',
            f"max_memory_lines = {self.max_memory_lines}",
            f"max_tokens = {self.max_tokens}",
            f"max_hot_lines = {self.max_hot_lines}",
            f"archive_in_harvest = {str(self.archive_in_harvest).lower()}",
            f"archive_max_lines_in_prompt = {self.archive_max_lines_in_prompt}",
            f"harvest_append_only_cold = {str(self.harvest_append_only_cold).lower()}",
            f"projection_enabled = {str(self.projection_enabled).lower()}",
            f"core_max_chars = {self.core_max_chars}",
        ]
        if self.api_key:
            lines.append(f'api_key = "{self.api_key}"')
        lines += [
            "",
            "[paths]",
            f'claude_md_target = "{self.claude_md_target.as_posix()}"',
            f'codex_agents_target = "{self.codex_agents_target.as_posix()}"',
        ]
        if self.sync_root:
            # TOML strings need forward slashes
            lines.append(f'sync_root = "{self.sync_root.as_posix()}"')
        if self.project_cwd: # Add this block
            lines.append(f'project_cwd = "{self.project_cwd.as_posix()}"')
        lines += [
            "",
            "[backups]",
            f"keep_days = {self.keep_days}",
            "",
            "[llm]",
            "backends = [" + ", ".join(f'"{b}"' for b in llm_backends) + "]",
            f'claude_code_model = "{self.claude_code_model}"',
            f'claude_code_effort = "{self.claude_code_effort}"',
            f"claude_code_timeout = {self.claude_code_timeout}",
            f'gemini_model = "{self.gemini_model}"',
            f'ollama_base_url = "{self.ollama_base_url}"',
            f'ollama_model = "{self.ollama_model}"',
            f"codex_timeout = {self.codex_timeout}",
            f"ollama_timeout = {self.ollama_timeout}",
            f"ollama_num_ctx = {self.ollama_num_ctx}",
            f"harvest_chunk_tokens = {self.harvest_chunk_tokens}",
            f"harvest_chunk_tokens_codex = {self.harvest_chunk_tokens_codex}",
            f"harvest_chunk_tokens_claude_code = {self.harvest_chunk_tokens_claude_code}",
            f"harvest_chunk_tokens_gemini = {self.harvest_chunk_tokens_gemini}",
            f"harvest_chunk_tokens_gemini_cli = {self.harvest_chunk_tokens_gemini_cli}",
            f"harvest_chunk_tokens_ollama = {self.harvest_chunk_tokens_ollama}",
            f"harvest_chunk_tokens_anthropic = {self.harvest_chunk_tokens_anthropic}",
            f"chunk_inter_call_sleep = {self.chunk_inter_call_sleep}",
        ]
        if self.gemini_api_key:
            lines.append(f'gemini_api_key = "{self.gemini_api_key}"')
        lines.append("")

        # Only write [daemon] section if daemon is enabled (i.e. user ran daemon install)
        if self.daemon.enabled:
            d = self.daemon
            lines += [
                "[daemon]",
                f"enabled = {str(d.enabled).lower()}",
                "refresh_schedule = [" + ", ".join(f'"{s}"' for s in d.refresh_schedule) + "]",
                f"refresh_enabled = {str(d.refresh_enabled).lower()}",
                f'backup_mirror_path = "{d.backup_mirror_path}"',
                f'backup_mirror_schedule = "{d.backup_mirror_schedule}"',
                f"web_ui_enabled = {str(d.web_ui_enabled).lower()}",
                f"web_ui_port = {d.web_ui_port}",
                f'web_ui_host = "{d.web_ui_host}"',
                f"capture_enabled = {str(d.capture_enabled).lower()}",
                f"capture_port = {d.capture_port}",
                f'capture_token = "{d.capture_token}"',
                f"drift_check_enabled = {str(d.drift_check_enabled).lower()}",
                f"drift_check_interval_hours = {d.drift_check_interval_hours}",
                f'drift_notify = "{d.drift_notify}"',
                f"harvest_enabled = {str(d.harvest_enabled).lower()}",
                f'harvest_schedule = "{d.harvest_schedule}"',
                f'harvest_projects_dir = "{d.harvest_projects_dir}"',
                f"harvest_allow_ollama = {str(d.harvest_allow_ollama).lower()}",
                f"harvest_max_runtime_seconds = {d.harvest_max_runtime_seconds}",
                f"harvest_max_sessions_per_run = {d.harvest_max_sessions_per_run}",
                f"harvest_lock_stale_seconds = {d.harvest_lock_stale_seconds}",
                f"digest_enabled = {str(d.digest_enabled).lower()}",
                f'digest_schedule = "{d.digest_schedule}"',
                f'digest_email_to = "{d.digest_email_to}"',
                f'digest_email_from = "{d.digest_email_from}"',
                f'digest_smtp_host = "{d.digest_smtp_host}"',
                f"digest_smtp_port = {d.digest_smtp_port}",
                f'digest_smtp_user = "{d.digest_smtp_user}"',
                f'digest_smtp_password = "{d.digest_smtp_password}"',
                "",
            ]

        return "\n".join(lines)


def get_config_path() -> Path:
    """Return the platform-appropriate config file path."""
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(appdata) / "memsync" / "config.toml"
    else:
        xdg_config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        return Path(xdg_config) / "memsync" / "config.toml"
