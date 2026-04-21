from __future__ import annotations

import os
import platform
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


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
    refresh_schedule: str = "55 23 * * *"      # 11:55pm daily

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
    llm_backend: str = "gemini"              # "gemini" | "ollama" | "anthropic"
    fallback_backend: str = "ollama"         # tried when llm_backend fails; "none" to hard-error
    gemini_api_key: str = ""                 # AI Studio key; leave empty to use ADC instead
    gemini_model: str = "gemini-2.5-flash"    # any model available on your Gemini account
    ollama_base_url: str = "http://localhost:11434/v1"  # Ollama OpenAI-compatible endpoint
    ollama_model: str = "llama3.2:3b"        # ~2GB RAM; good balance of quality and Pi headroom
    ollama_timeout: int = 120                # seconds; caps fallback burn time on weak hardware
    ollama_num_ctx: int = 8192               # context window; 32K OOMs the 1b on an 8GB Pi

    # [paths]
    sync_root: Path | None = None           # None = use provider auto-detect
    claude_md_target: Path = None           # set in __post_init__

    # [backups]
    keep_days: int = 30

    # [daemon] — only populated when daemon is installed
    daemon: DaemonConfig = field(default_factory=DaemonConfig)

    def __post_init__(self) -> None:
        if self.claude_md_target is None:
            self.claude_md_target = Path("~/.claude/CLAUDE.md").expanduser()

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

        # Daemon section — only present if user has run 'memsync daemon install'
        daemon_raw = raw.get("daemon", {})
        daemon = DaemonConfig(
            enabled=daemon_raw.get("enabled", True),
            refresh_enabled=daemon_raw.get("refresh_enabled", True),
            refresh_schedule=daemon_raw.get("refresh_schedule", "55 23 * * *"),
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

        instance = cls(
            provider=core.get("provider", "onedrive"),
            model=core.get("model", "claude-sonnet-4-20250514"),
            max_memory_lines=core.get("max_memory_lines", 400),
            max_tokens=core.get("max_tokens", 16384),
            api_key=core.get("api_key", ""),
            llm_backend=llm_raw.get("backend", "gemini"),
            fallback_backend=llm_raw.get("fallback_backend", "ollama"),
            gemini_api_key=llm_raw.get("gemini_api_key", ""),
            gemini_model=llm_raw.get("gemini_model", "gemini-2.5-flash"),
            ollama_base_url=llm_raw.get("ollama_base_url", "http://localhost:11434/v1"),
            ollama_model=llm_raw.get("ollama_model", "llama3.2:3b"),
            ollama_timeout=llm_raw.get("ollama_timeout", 120),
            ollama_num_ctx=llm_raw.get("ollama_num_ctx", 8192),
            sync_root=Path(sync_root) if sync_root else None,
            claude_md_target=(
                Path(claude_md_target_str).expanduser() if claude_md_target_str else None
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
        lines = [
            "[core]",
            f'provider = "{self.provider}"',
            f'model = "{self.model}"',
            f"max_memory_lines = {self.max_memory_lines}",
            f"max_tokens = {self.max_tokens}",
        ]
        if self.api_key:
            lines.append(f'api_key = "{self.api_key}"')
        lines += [
            "",
            "[paths]",
            f'claude_md_target = "{self.claude_md_target.as_posix()}"',
        ]
        if self.sync_root:
            # TOML strings need forward slashes
            lines.append(f'sync_root = "{self.sync_root.as_posix()}"')
        lines += [
            "",
            "[backups]",
            f"keep_days = {self.keep_days}",
            "",
            "[llm]",
            f'backend = "{self.llm_backend}"',
            f'fallback_backend = "{self.fallback_backend}"',
            f'gemini_model = "{self.gemini_model}"',
            f'ollama_base_url = "{self.ollama_base_url}"',
            f'ollama_model = "{self.ollama_model}"',
            f"ollama_timeout = {self.ollama_timeout}",
            f"ollama_num_ctx = {self.ollama_num_ctx}",
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
                f'refresh_schedule = "{d.refresh_schedule}"',
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
