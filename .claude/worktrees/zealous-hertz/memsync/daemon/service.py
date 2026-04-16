"""
System service installation for the memsync daemon.

Supports:
  Linux  — systemd unit file at /etc/systemd/system/memsync.service
  Mac    — launchd plist at ~/Library/LaunchAgents/com.memsync.daemon.plist
  Windows — not supported (use Task Scheduler with 'memsync daemon start --detach')

IMPORTANT: systemd install requires root (sudo memsync daemon install).
The unit file contains a placeholder for ANTHROPIC_API_KEY. After install,
use 'systemctl edit memsync' to add the key in an override file rather than
editing the unit file directly — override files survive package updates.
"""
from __future__ import annotations

import platform
import subprocess
from pathlib import Path

SYSTEMD_UNIT = """\
[Unit]
Description=memsync daemon
After=network.target

[Service]
Type=simple
ExecStart={memsync_bin} daemon start
Restart=on-failure
RestartSec=10
Environment=ANTHROPIC_API_KEY=<set ANTHROPIC_API_KEY here>

[Install]
WantedBy=multi-user.target
"""

LAUNCHD_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.memsync.daemon</string>
  <key>ProgramArguments</key>
  <array>
    <string>{memsync_bin}</string>
    <string>daemon</string>
    <string>start</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{log_dir}/memsync-daemon.log</string>
  <key>StandardErrorPath</key>
  <string>{log_dir}/memsync-daemon.err</string>
</dict>
</plist>
"""


def install_service() -> None:
    """Install the memsync daemon as a system service."""
    system = platform.system()
    memsync_bin = _find_memsync_bin()

    if system == "Linux":
        _install_systemd(memsync_bin)
    elif system == "Darwin":
        _install_launchd(memsync_bin)
    else:
        raise NotImplementedError(
            "Service install is not supported on Windows.\n"
            "Use Task Scheduler to run 'memsync daemon start --detach' on boot."
        )


def uninstall_service() -> None:
    """Remove the memsync daemon system service registration."""
    system = platform.system()
    if system == "Linux":
        _uninstall_systemd()
    elif system == "Darwin":
        _uninstall_launchd()
    else:
        raise NotImplementedError("Service uninstall not supported on Windows.")


def _install_systemd(memsync_bin: str) -> None:
    unit_path = Path("/etc/systemd/system/memsync.service")
    unit_content = SYSTEMD_UNIT.format(memsync_bin=memsync_bin)
    unit_path.write_text(unit_content, encoding="utf-8")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "memsync"], check=True)
    subprocess.run(["systemctl", "start", "memsync"], check=True)
    print(f"Service installed: {unit_path}")
    print("Edit ANTHROPIC_API_KEY via: sudo systemctl edit memsync")
    print("Then restart with: sudo systemctl restart memsync")


def _install_launchd(memsync_bin: str) -> None:
    log_dir = Path.home() / "Library" / "Logs" / "memsync"
    log_dir.mkdir(parents=True, exist_ok=True)
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / "com.memsync.daemon.plist"
    plist_content = LAUNCHD_PLIST.format(memsync_bin=memsync_bin, log_dir=log_dir)
    plist_path.write_text(plist_content, encoding="utf-8")
    subprocess.run(["launchctl", "load", str(plist_path)], check=True)
    print(f"Service installed: {plist_path}")
    print(f"Logs: {log_dir}/memsync-daemon.log")


def _uninstall_systemd() -> None:
    subprocess.run(["systemctl", "stop", "memsync"], check=False)
    subprocess.run(["systemctl", "disable", "memsync"], check=False)
    unit_path = Path("/etc/systemd/system/memsync.service")
    if unit_path.exists():
        unit_path.unlink()
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print("Service removed.")


def _uninstall_launchd() -> None:
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.memsync.daemon.plist"
    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], check=False)
        plist_path.unlink()
    print("Service removed.")


def _find_memsync_bin() -> str:
    import shutil

    bin_path = shutil.which("memsync")
    if not bin_path:
        raise FileNotFoundError(
            "memsync not found in PATH. Install with: pip install memsync[daemon]"
        )
    return bin_path
