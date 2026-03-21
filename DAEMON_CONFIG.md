# DAEMON_CONFIG.md

## What this file is

Additions to `memsync/config.py` needed to support the daemon module.
Do not apply these until core memsync is complete and tested.
These changes are additive — nothing in the existing Config dataclass changes.

---

## DaemonConfig dataclass

Add this class to `memsync/config.py` alongside the existing `Config`:

```python
@dataclass
class DaemonConfig:
    """
    Configuration for the optional daemon module.
    Only present in config.toml if the user has run 'memsync daemon install'.
    """
    enabled: bool = True

    # Scheduled refresh
    refresh_enabled: bool = True
    refresh_schedule: str = "55 23 * * *"      # 11:55pm daily

    # Backup mirror
    backup_mirror_path: str = ""               # empty = disabled
    backup_mirror_schedule: str = "0 * * * *"  # hourly

    # Web UI
    web_ui_enabled: bool = True
    web_ui_port: int = 5000
    web_ui_host: str = "0.0.0.0"

    # Capture endpoint
    capture_enabled: bool = True
    capture_port: int = 5001
    capture_token: str = ""                    # empty = no auth

    # Drift detection
    drift_check_enabled: bool = True
    drift_check_interval_hours: int = 6
    drift_notify: str = "log"                  # "log", "email", or "file"

    # Weekly digest
    digest_enabled: bool = False
    digest_schedule: str = "0 9 * * 1"        # Monday 9am
    digest_email_to: str = ""
    digest_email_from: str = ""
    digest_smtp_host: str = ""
    digest_smtp_port: int = 587
    digest_smtp_user: str = ""
    digest_smtp_password: str = ""
```

---

## Config dataclass update

Add `daemon` field to the existing `Config` dataclass:

```python
@dataclass
class Config:
    # ... existing fields unchanged ...

    # Optional daemon config — only populated if [daemon] section exists in config.toml
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
```

---

## _from_dict update

Add daemon section parsing to `Config._from_dict()`:

```python
@classmethod
def _from_dict(cls, raw: dict) -> "Config":
    # ... existing parsing unchanged ...

    daemon_raw = raw.get("daemon", {})
    daemon = DaemonConfig(
        enabled=daemon_raw.get("enabled", True),
        refresh_enabled=daemon_raw.get("refresh_enabled", True),
        refresh_schedule=daemon_raw.get("refresh_schedule", "55 23 * * *"),
        backup_mirror_path=daemon_raw.get("backup_mirror_path", ""),
        backup_mirror_schedule=daemon_raw.get("backup_mirror_schedule", "0 * * * *"),
        web_ui_enabled=daemon_raw.get("web_ui_enabled", True),
        web_ui_port=daemon_raw.get("web_ui_port", 5000),
        web_ui_host=daemon_raw.get("web_ui_host", "0.0.0.0"),
        capture_enabled=daemon_raw.get("capture_enabled", True),
        capture_port=daemon_raw.get("capture_port", 5001),
        capture_token=daemon_raw.get("capture_token", ""),
        drift_check_enabled=daemon_raw.get("drift_check_enabled", True),
        drift_check_interval_hours=daemon_raw.get("drift_check_interval_hours", 6),
        drift_notify=daemon_raw.get("drift_notify", "log"),
        digest_enabled=daemon_raw.get("digest_enabled", False),
        digest_schedule=daemon_raw.get("digest_schedule", "0 9 * * 1"),
        digest_email_to=daemon_raw.get("digest_email_to", ""),
        digest_email_from=daemon_raw.get("digest_email_from", ""),
        digest_smtp_host=daemon_raw.get("digest_smtp_host", ""),
        digest_smtp_port=daemon_raw.get("digest_smtp_port", 587),
        digest_smtp_user=daemon_raw.get("digest_smtp_user", ""),
        digest_smtp_password=daemon_raw.get("digest_smtp_password", ""),
    )

    return cls(
        # ... existing fields unchanged ...
        daemon=daemon,
    )
```

---

## _to_toml update

Add daemon section to `Config._to_toml()`.
Only write the `[daemon]` section if `daemon.enabled` is True
(i.e. user has run `memsync daemon install`):

```python
def _to_toml(self) -> str:
    # ... existing lines unchanged ...

    if self.daemon.enabled:
        lines += [
            "",
            "[daemon]",
            f"enabled = {str(self.daemon.enabled).lower()}",
            f'refresh_schedule = "{self.daemon.refresh_schedule}"',
            f"refresh_enabled = {str(self.daemon.refresh_enabled).lower()}",
            f'backup_mirror_path = "{self.daemon.backup_mirror_path}"',
            f'backup_mirror_schedule = "{self.daemon.backup_mirror_schedule}"',
            f"web_ui_enabled = {str(self.daemon.web_ui_enabled).lower()}",
            f"web_ui_port = {self.daemon.web_ui_port}",
            f'web_ui_host = "{self.daemon.web_ui_host}"',
            f"capture_enabled = {str(self.daemon.capture_enabled).lower()}",
            f"capture_port = {self.daemon.capture_port}",
            f'capture_token = "{self.daemon.capture_token}"',
            f"drift_check_enabled = {str(self.daemon.drift_check_enabled).lower()}",
            f"drift_check_interval_hours = {self.daemon.drift_check_interval_hours}",
            f'drift_notify = "{self.daemon.drift_notify}"',
            f"digest_enabled = {str(self.daemon.digest_enabled).lower()}",
            f'digest_schedule = "{self.daemon.digest_schedule}"',
            f'digest_email_to = "{self.daemon.digest_email_to}"',
            f'digest_email_from = "{self.daemon.digest_email_from}"',
            f'digest_smtp_host = "{self.daemon.digest_smtp_host}"',
            f"digest_smtp_port = {self.daemon.digest_smtp_port}",
            f'digest_smtp_user = "{self.daemon.digest_smtp_user}"',
            f'digest_smtp_password = "{self.daemon.digest_smtp_password}"',
            "",
        ]

    return "\n".join(lines)
```

---

## Important: SMTP password handling

Storing SMTP passwords in a plaintext config file is not ideal.
For v1 it's acceptable with a clear warning, but note in the README:

> For better security, leave `digest_smtp_password` empty and use an
> app-specific password stored in your system keyring instead.
> Set it at runtime with: `MEMSYNC_SMTP_PASSWORD=... memsync daemon start`

Add `MEMSYNC_SMTP_PASSWORD` env var support as a fallback in `notify.py`:

```python
import os
password = config.daemon.digest_smtp_password or os.environ.get("MEMSYNC_SMTP_PASSWORD", "")
```
