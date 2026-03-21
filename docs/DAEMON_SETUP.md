# DAEMON_SETUP.md

## Raspberry Pi setup guide

This is the end-user guide for setting up the daemon on a Raspberry Pi.
It belongs in `docs/raspberry-pi.md` in the final repo.

---

## What you need

- Raspberry Pi 3B+ or newer (3B+ is fine, Pi 4 is better)
- Raspberry Pi OS Lite (no desktop needed)
- Your cloud sync folder accessible on the Pi (see options below)
- Python 3.11+ (comes with Raspberry Pi OS Bookworm and later)

---

## Step 1: Get Python 3.11+

```bash
python3 --version
# If below 3.11:
sudo apt update && sudo apt install -y python3.11 python3.11-pip
```

---

## Step 2: Install memsync with daemon extras

```bash
pip3 install memsync[daemon] --break-system-packages
```

---

## Step 3: Mount your cloud sync folder on the Pi

The Pi needs to see your `GLOBAL_MEMORY.md` file. Three options:

### Option A: rclone (OneDrive, Google Drive, iCloud via workaround)

```bash
# Install rclone
curl https://rclone.org/install.sh | sudo bash

# Configure (follow interactive prompts)
rclone config
# Choose your provider (OneDrive = "Microsoft OneDrive", Google Drive = "drive")

# Mount (run at boot via cron or systemd)
rclone mount onedrive: ~/OneDrive --daemon --vfs-cache-mode full
```

Add to `/etc/rc.local` before `exit 0` to mount on boot:
```bash
sudo -u pi rclone mount onedrive: /home/pi/OneDrive --daemon --vfs-cache-mode full
```

### Option B: NFS mount from another machine on your LAN

If your Mac or Windows machine is always on, share the OneDrive folder over
NFS and mount it on the Pi. Simpler than rclone for home LAN setups.

### Option C: Manual sync via rsync + cron (simplest, less real-time)

```bash
# Add to Pi's crontab — syncs from Mac every 15 minutes
*/15 * * * * rsync -az your-mac.local:/Users/ian/OneDrive/.claude-memory/ ~/claude-memory/
```

Then point memsync at the local copy:
```bash
memsync config set sync_root ~/claude-memory
memsync config set provider custom
```

---

## Step 4: Configure memsync on the Pi

```bash
# Initialize (uses OneDrive via rclone mount, or custom path from Option C)
memsync init

# Set your API key
export ANTHROPIC_API_KEY="sk-ant-..."
# Add to ~/.bashrc to persist across reboots:
echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.bashrc

# Check everything looks right
memsync status
```

---

## Step 5: Install and start the daemon

```bash
# Install as a systemd service (starts on boot)
sudo memsync daemon install

# The installer will print a warning about the API key in the unit file.
# Add it properly via override:
sudo systemctl edit memsync
```

In the editor that opens, add:
```ini
[Service]
Environment=ANTHROPIC_API_KEY=sk-ant-...
```

Save and close, then:
```bash
sudo systemctl restart memsync
sudo systemctl status memsync    # should show "active (running)"
```

---

## Step 6: Set your timezone

```bash
# Check current timezone
timedatectl

# Set correct timezone (important for nightly refresh timing)
sudo timedatectl set-timezone America/Los_Angeles
# or America/New_York, Europe/London, etc.
```

---

## Step 7: Verify the nightly refresh

The easiest way to test without waiting until 11:55pm:

```bash
# Trigger a manual refresh to confirm everything works
memsync refresh --notes "Pi daemon setup and tested successfully"

# Check it ran and updated the memory file
memsync show | head -20
```

---

## Step 8: Set up the web UI (optional)

The web UI starts automatically with the daemon. Access it from any browser
on your home network:

```
http://raspberrypi.local:5000
```

If `raspberrypi.local` doesn't resolve, use the Pi's IP address instead:
```bash
hostname -I    # shows Pi's IP
```

---

## Step 9: Set up mobile capture (optional)

On iPhone, create a Shortcut:
1. Add action: "Get Contents of URL"
2. URL: `http://raspberrypi.local:5001/note`
3. Method: POST
4. Request body: JSON → `{"text": "Shortcut Input"}`
5. Add a "Text" input action before it so you can type the note

Add to your home screen. One tap → type note → it goes into tonight's session log.

If you want basic auth, set a token first:
```bash
memsync config set capture_token mytoken123
```

Then add header to the Shortcut: `X-Memsync-Token: mytoken123`

---

## Checking daemon health

```bash
# See what's running and when jobs last fired
memsync daemon status

# See daemon logs
sudo journalctl -u memsync -f

# Check the schedule
memsync daemon schedule
```

---

## Troubleshooting

**Daemon won't start:**
```bash
sudo journalctl -u memsync --no-pager | tail -30
```
Most common cause: ANTHROPIC_API_KEY not set in the systemd override.

**Web UI not accessible from other devices:**
Check that `web_ui_host` is `0.0.0.0` not `127.0.0.1`:
```bash
memsync config show | grep web_ui_host
memsync config set web_ui_host 0.0.0.0
sudo systemctl restart memsync
```

**Port 5000 already in use:**
```bash
memsync config set web_ui_port 5050
sudo systemctl restart memsync
```

**OneDrive not syncing on Pi:**
```bash
rclone ls onedrive:.claude-memory/    # test rclone can see the files
```
If this fails, reconfigure rclone: `rclone config reconnect onedrive:`
