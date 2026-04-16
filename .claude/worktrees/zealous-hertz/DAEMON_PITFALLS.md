# DAEMON_PITFALLS.md

Daemon-specific pitfalls on top of the core ones in `PITFALLS.md`.
Read both before building the daemon module.

---

## 1. Core module boundary is sacred

The daemon imports from core. Core never imports from daemon.

If you find yourself adding a daemon import to `sync.py`, `config.py`,
`backups.py`, or any other core module — stop. Restructure so the daemon
calls core, not the other way around. Violating this boundary means
`pip install memsync` (core only) pulls in daemon dependencies.

---

## 2. The nightly refresh job must handle missing session logs gracefully

If the user didn't run any sessions that day, `sessions/<date>.md` won't exist.
`job_nightly_refresh` returns early if the file doesn't exist or is empty.
This is already in the spec — do not remove this guard. An empty notes
payload to the API wastes tokens and risks producing hallucinated changes.

---

## 3. systemd unit file and API key exposure

The generated systemd unit file includes `Environment=ANTHROPIC_API_KEY=...`
as a placeholder. Unit files in `/etc/systemd/system/` are world-readable by default.

Two mitigations to document clearly:
- Use `systemctl edit memsync` to create a drop-in override file (mode 600)
- Use `EnvironmentFile=/etc/memsync/secrets` pointing to a mode 600 file

Do not suggest storing the real key in the main unit file.
Print a prominent warning after `memsync daemon install` on Linux.

---

## 4. Flask dev server is fine for local network, not for internet exposure

`app.run()` is the Flask development server. It's single-threaded and has no
auth. This is acceptable for a Pi on a home LAN. It is not acceptable for any
internet-facing deployment.

If a user asks about exposing the web UI to the internet:
- Tell them this is out of scope for v1
- Point them toward nginx + basic auth as a general approach
- Do not add this to the tool itself

---

## 5. Port conflicts on common dev machines

5000 is used by AirPlay Receiver on Mac (macOS 12+) and many dev servers.
5001 is also commonly used. Document both ports as configurable.

On Mac, if `web_ui_host = "0.0.0.0"` and port 5000 is taken by AirPlay,
the web UI will silently fail to start or throw a bind error. Print a clear
error message pointing to `memsync config set web_ui_port <other-port>`.

---

## 6. APScheduler timezone handling

APScheduler uses local system time by default. On a Pi, make sure the system
timezone is set correctly (`timedatectl set-timezone America/New_York` or
wherever the user is). The nightly refresh at 11:55pm will fire at 11:55pm
in the Pi's system timezone, which may not match the user's timezone if the
Pi was set up with UTC (the default on many Pi images).

Document this in the Pi setup guide. Add a note to `memsync daemon install`
output: "Make sure your Pi's timezone is set correctly: `timedatectl`"

---

## 7. OneDrive sync lag on the Pi

If the Pi has OneDrive mounted (via rclone or similar), there may be sync lag
between when `GLOBAL_MEMORY.md` is updated on a Mac/Windows machine and when
the Pi sees the change. The nightly refresh reads the file at job time —
if OneDrive hasn't synced yet, it reads a stale version.

This is an inherent limitation of filesystem-based sync. Document it.
Workaround: schedule the nightly refresh a few minutes after midnight rather
than 11:55pm, giving OneDrive time to sync the day's changes before the
Pi reads them.

---

## 8. The backup mirror is not a substitute for OneDrive

The `job_backup_mirror` rsync copies files from the OneDrive-synced
`.claude-memory/` to a local path. It's a redundant local backup in case
OneDrive has an outage or the user accidentally deletes something in OneDrive.

It is not a real-time mirror. It runs on a schedule (default: hourly).
Document this limitation clearly — it's not a safety net for changes made
in the last hour.

---

## 9. Digest email and SMTP credentials

SMTP credentials in a config file are a security concern. The v1 approach
(plaintext in config.toml) is acceptable with a warning, but:

- Never log or print SMTP credentials
- Support `MEMSYNC_SMTP_PASSWORD` env var as an alternative (see DAEMON_CONFIG.md)
- Document that Gmail requires an App Password, not the account password
- Document that many ISPs block outbound port 587 — common user frustration

---

## 10. Test isolation for daemon jobs

Daemon jobs touch the filesystem and call the API. Tests must mock both.
Never let a test job run `job_nightly_refresh` against a real filesystem
or make a real API call. Use `tmp_path` and `unittest.mock.patch` throughout.

For Flask tests, use the Flask test client — never bind to a real port in tests.

```python
# Good
def test_capture_endpoint(tmp_config):
    from memsync.daemon.capture import create_capture_app
    config, tmp_path = tmp_config
    app = create_capture_app(config)
    client = app.test_client()
    response = client.post("/note", json={"text": "test note"})
    assert response.status_code == 200

# Bad — binds to real port, can conflict with other tests
def test_capture_endpoint():
    run_capture(config)  # never do this in tests
```
