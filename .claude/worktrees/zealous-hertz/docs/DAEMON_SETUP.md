# Setting up the memsync daemon

The daemon runs two jobs automatically overnight so you don't have to think about it:

- **2:00am — harvest**: reads your Claude Code sessions from today and extracts anything worth remembering into your memory file.
- **11:55pm — refresh**: merges any notes you captured via the mobile endpoint during the day.

Pick your platform below. If you're setting up a Raspberry Pi to run this 24/7, jump straight to the [Raspberry Pi](#raspberry-pi) section.

---

## Mac

### Step 1: Install the daemon extras

Open Terminal and run:

```bash
pip3 install 'memsync[daemon]'
```

The quotes around `memsync[daemon]` are required on Mac — without them zsh (the default Mac shell) misreads the brackets.

If you get "command not found: pip3", try:
```bash
python3 -m pip install 'memsync[daemon]'
```

### Step 2: Verify the install worked

```bash
memsync daemon status
```

You should see something like:
```
Daemon is not running.

Web UI:   enabled  (port 5000)
Capture:  enabled  (port 5001)
Refresh:  enabled  (schedule: 55 23 * * *)
```

If you see "The daemon module is not installed", your Mac has multiple Python installations and pip installed into the wrong one. Find which Python runs memsync:

```bash
head -1 $(which memsync)
```

This prints something like `/opt/homebrew/opt/python@3.11/bin/python3.11`. Use that Python to install:

```bash
/opt/homebrew/opt/python@3.11/bin/python3.11 -m pip install 'memsync[daemon]'
```

Then run `memsync daemon status` again.

### Step 3: Start the daemon

```bash
memsync daemon start --detach
```

This starts the daemon as a background process. It will run until you restart your Mac.

Confirm it started:
```bash
memsync daemon status
```

You should now see "Daemon is running (PID xxxxx)."

### Step 4: Make it start automatically at login (optional)

If you want the daemon to start automatically every time you log in without doing anything:

```bash
memsync daemon install
```

This registers the daemon with macOS's launchd service manager. It will start at login and restart itself if it crashes.

**After installing, you must add your API key.** The launchd service doesn't inherit your terminal's environment variables, so you need to add it to the service configuration manually.

Open the service config file:
```bash
open -e ~/Library/LaunchAgents/com.memsync.daemon.plist
```

Find this section in the file:
```xml
  <key>RunAtLoad</key>
  <true/>
```

Add the following lines **directly above** that section:
```xml
  <key>EnvironmentVariables</key>
  <dict>
    <key>ANTHROPIC_API_KEY</key>
    <string>sk-ant-YOUR-KEY-HERE</string>
  </dict>
```

Replace `sk-ant-YOUR-KEY-HERE` with your actual API key. Save the file.

Then reload the service to pick up the change:
```bash
launchctl unload ~/Library/LaunchAgents/com.memsync.daemon.plist
launchctl load ~/Library/LaunchAgents/com.memsync.daemon.plist
```

Check it's running:
```bash
memsync daemon status
```

### Step 5: Verify the schedule

```bash
memsync daemon schedule
```

You should see the harvest and refresh jobs listed.

### Stopping the daemon

```bash
memsync daemon stop        # if started with --detach
memsync daemon uninstall   # if installed with daemon install
```

---

## Windows

### Step 1: Install the daemon extras

Open PowerShell and run:

```powershell
pip install memsync[daemon]
```

PowerShell handles the brackets fine — no quotes needed.

If you get "command not found", try:
```powershell
python -m pip install memsync[daemon]
```

### Step 2: Set your API key as a system environment variable

The scheduled task runs in the background without access to your PowerShell profile, so the API key needs to be set at the system level.

1. Press `Win + R`, type `sysdm.cpl`, press Enter
2. Click the **Advanced** tab
3. Click **Environment Variables**
4. Under **User variables** (top half), click **New**
5. Variable name: `ANTHROPIC_API_KEY`
6. Variable value: your key starting with `sk-ant-...`
7. Click OK on all dialogs

Close and reopen PowerShell for the change to take effect. Verify it worked:
```powershell
echo $env:ANTHROPIC_API_KEY
```

You should see your key printed back.

### Step 3: Test that the daemon starts

```powershell
memsync daemon start --detach
memsync daemon status
```

You should see "Daemon is running (PID xxxxx)." Stop it for now — you'll run it via Task Scheduler instead:

```powershell
memsync daemon stop
```

### Step 4: Set up Task Scheduler to run it automatically

Task Scheduler is Windows's built-in tool for running programs on a schedule or at login.

1. Press `Win + R`, type `taskschd.msc`, press Enter. Task Scheduler opens.

2. In the right panel, click **Create Basic Task...**

3. **Name:** `memsync daemon`
   **Description:** `Start memsync daemon at login`
   Click Next.

4. **Trigger:** Select **When I log on**. Click Next.

5. **Action:** Select **Start a program**. Click Next.

6. **Program/script:** Type `memsync`
   **Add arguments:** `daemon start`
   Leave "Start in" blank.
   Click Next.

7. Check **Open the Properties dialog** at the bottom. Click Finish.

8. In the Properties dialog that opens:
   - Click the **General** tab
   - Check **Run with highest privileges**
   - Click OK

The daemon will now start automatically every time you log into Windows.

### Step 5: Verify it's working

Log out and back in (or restart). Then open PowerShell:

```powershell
memsync daemon status
```

You should see "Daemon is running."

### Stopping the daemon

```powershell
memsync daemon stop
```

To remove the scheduled task: open Task Scheduler, find "memsync daemon" in the list, right-click → Delete.

---

## Linux

### Step 1: Install the daemon extras

```bash
pip3 install 'memsync[daemon]'
```

### Step 2: Install as a systemd service

```bash
sudo memsync daemon install
```

This creates a systemd unit file and enables it to start on boot.

### Step 3: Add your API key

The service won't have your API key yet. Add it via a systemd override (this survives package updates):

```bash
sudo systemctl edit memsync
```

An editor opens. Add these lines exactly:

```ini
[Service]
Environment=ANTHROPIC_API_KEY=sk-ant-YOUR-KEY-HERE
```

Replace `sk-ant-YOUR-KEY-HERE` with your actual key. Save and close the editor.

Restart the service to pick up the change:

```bash
sudo systemctl restart memsync
sudo systemctl status memsync
```

The status output should show `active (running)`.

### Step 4: Set your timezone

The scheduled jobs run at 2am and 11:55pm local time. Make sure your system timezone is correct:

```bash
timedatectl                                    # check current timezone
sudo timedatectl set-timezone America/New_York # set if wrong
```

Common timezone strings: `America/Los_Angeles`, `America/Chicago`, `America/New_York`, `Europe/London`, `Europe/Berlin`.

### Step 5: Verify

```bash
memsync daemon schedule    # shows scheduled jobs
memsync daemon status      # shows running status
```

### Stopping / uninstalling

```bash
sudo memsync daemon uninstall    # removes systemd service
```

---

## Raspberry Pi

The Pi is the ideal machine to run the memsync daemon — it's always on, uses minimal power (about $1/month in electricity), and handles the nightly jobs even when your laptop is closed.

**What the Pi can and can't do:**

The Pi runs the memory refresh job (11:55pm) perfectly — it reads session notes from your cloud sync folder. The harvest job (2am) won't find anything on the Pi because Claude Code session files live on your Mac/Windows machine locally. That's fine — just run `memsync harvest` manually on your main machine after important sessions, and the Pi handles everything else.

---

### What you need

- Raspberry Pi 3B+ or newer (3B+ works, Pi 4 is faster)
- Raspberry Pi OS Lite (no desktop needed — lighter and faster)
- A way to access your cloud sync folder from the Pi (instructions below)
- Python 3.11+ — comes automatically with Raspberry Pi OS Bookworm (released 2023) and later

---

### Step 1: Check your Python version

SSH into your Pi and run:

```bash
python3 --version
```

If it shows 3.11 or higher, skip to Step 2.

If it shows anything lower, update:
```bash
sudo apt update && sudo apt install -y python3.11 python3.11-pip
```

---

### Step 2: Install memsync with daemon extras

```bash
pip3 install 'memsync[daemon]' --break-system-packages
```

The `--break-system-packages` flag is needed on newer Raspberry Pi OS versions. It's safe to use here.

---

### Step 3: Connect your cloud sync folder to the Pi

The Pi needs to be able to read and write your `GLOBAL_MEMORY.md` file. Choose one of these three approaches:

#### Option A: rclone — sync directly with OneDrive or Google Drive (recommended)

rclone lets the Pi talk to cloud storage directly without going through another machine.

```bash
# Install rclone
curl https://rclone.org/install.sh | sudo bash

# Set up your cloud provider (follow the prompts)
rclone config
```

When it asks for a provider, choose:
- **Microsoft OneDrive** for OneDrive
- **Google Drive** for Google Drive

The setup will ask you to open a link in a browser and sign in. Do this on your regular computer, then paste the confirmation code back into the Pi terminal.

Test that it works:
```bash
rclone ls onedrive:.claude-memory/
```

You should see your `GLOBAL_MEMORY.md` listed.

Now mount the folder so memsync can access it like a regular folder:
```bash
mkdir -p ~/OneDrive
rclone mount onedrive: ~/OneDrive --daemon --vfs-cache-mode full
```

Make it mount automatically on boot by adding to `/etc/rc.local`. Open the file:
```bash
sudo nano /etc/rc.local
```

Add this line before `exit 0`:
```bash
sudo -u pi rclone mount onedrive: /home/pi/OneDrive --daemon --vfs-cache-mode full
```

Save with Ctrl+X, then Y, then Enter.

#### Option B: rsync from your Mac or Windows machine (simplest if your main machine is always on)

This copies the memory folder from your main machine to the Pi every 15 minutes.

On the Pi, open the crontab editor:
```bash
crontab -e
```

Add this line (replace `your-mac.local` and the path with your actual machine name and path):
```bash
*/15 * * * * rsync -az your-mac.local:/Users/yourname/OneDrive/.claude-memory/ ~/claude-memory/
```

Then tell memsync to use this local copy:
```bash
memsync config set sync_root ~/claude-memory
memsync config set provider custom
```

#### Option C: NFS mount from your Mac

If you're comfortable with networking, this gives real-time access. Share the OneDrive folder from your Mac over NFS and mount it on the Pi. Instructions vary by router/setup — search "macOS NFS share Raspberry Pi" for your specific setup.

---

### Step 4: Set your API key

```bash
export ANTHROPIC_API_KEY="sk-ant-your-key-here"
```

Make it persist across reboots:
```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-your-key-here"' >> ~/.bashrc
source ~/.bashrc
```

---

### Step 5: Initialize memsync on the Pi

```bash
memsync init
```

It should detect your cloud folder. If it can't find it automatically:
```bash
memsync init --sync-root ~/OneDrive    # or ~/claude-memory if using rsync
```

Check everything looks right:
```bash
memsync status
```

Every line should show a ✓. If anything shows ✗, see the Troubleshooting section.

---

### Step 6: Install the daemon as a system service

```bash
sudo memsync daemon install
```

This registers memsync as a systemd service that starts automatically when the Pi boots.

Now add your API key to the service (systemd doesn't inherit your shell's variables):

```bash
sudo systemctl edit memsync
```

An editor opens. Type these lines exactly:

```ini
[Service]
Environment=ANTHROPIC_API_KEY=sk-ant-your-key-here
```

Replace the key with your actual key. Save with Ctrl+X, Y, Enter.

Restart the service:
```bash
sudo systemctl restart memsync
sudo systemctl status memsync
```

You should see `active (running)` in green. If it's red, see Troubleshooting.

---

### Step 7: Set your timezone

The jobs run at 2am and 11:55pm. Make sure the Pi's clock is in your timezone:

```bash
timedatectl
```

If the timezone is wrong:
```bash
sudo timedatectl set-timezone America/Los_Angeles
```

Common options: `America/Los_Angeles`, `America/New_York`, `America/Chicago`, `Europe/London`

---

### Step 8: Verify everything works

Do a quick test to make sure memsync can actually update the memory file from the Pi:

```bash
memsync refresh --notes "Pi daemon setup complete — testing end-to-end"
memsync show | head -10
```

If `show` displays your memory file, the whole chain is working: Pi → rclone → OneDrive → memory file.

Check what's scheduled:
```bash
memsync daemon schedule
```

You should see the harvest and refresh jobs listed.

---

### Step 9: Set up the web UI (optional)

The web UI is already running — you just need to open a browser on any device on your home network and go to:

```
http://raspberrypi.local:5000
```

If that doesn't work, find your Pi's IP address:
```bash
hostname -I
```

Then use `http://192.168.x.x:5000` (whatever IP it shows).

---

### Step 10: Set up mobile capture (optional)

This lets you send a quick note to memsync from your phone with one tap.

On iPhone, create a Shortcut:
1. Open the Shortcuts app → tap the **+** to create a new shortcut
2. Tap **Add Action** → search for "Get Contents of URL" → tap it
3. Tap the URL field and type: `http://raspberrypi.local:5001/note`
4. Tap **Show More** → set Method to **POST**
5. Set Request Body to **JSON**
6. Add a key `text` with value set to a **Shortcut Input** variable
7. Add a "Text" action at the very beginning of the shortcut so you can type your note

Add it to your home screen. One tap → type what you want to remember → it gets added to tonight's session log.

If you want to require a password (recommended if your home network has guests):
```bash
memsync config set capture_token your-secret-word
sudo systemctl restart memsync
```

Then add a header to the Shortcut: `X-Memsync-Token: your-secret-word`

---

## Checking daemon health (all platforms)

```bash
memsync daemon status      # is it running? what's enabled?
memsync daemon schedule    # what jobs are scheduled?
```

On Linux and Pi, check detailed logs:
```bash
sudo journalctl -u memsync -f          # live log output
sudo journalctl -u memsync --no-pager | tail -50   # last 50 lines
```

On Mac (launchd):
```bash
cat ~/Library/Logs/memsync/memsync-daemon.log
cat ~/Library/Logs/memsync/memsync-daemon.err
```

---

## Troubleshooting

### "The daemon module is not installed"

memsync is running under a different Python than where you installed the daemon extras. Find the right one:

```bash
head -1 $(which memsync)
# Returns something like: /opt/homebrew/opt/python@3.11/bin/python3.11
```

Use that Python to install:
```bash
/opt/homebrew/opt/python@3.11/bin/python3.11 -m pip install 'memsync[daemon]'
```

### Daemon won't start (Linux/Pi)

```bash
sudo journalctl -u memsync --no-pager | tail -30
```

Most common cause: `ANTHROPIC_API_KEY` not set in the systemd override. Go back to Step 6 and add it.

### Web UI not accessible from other devices

The web UI is probably only listening on localhost. Fix it:

```bash
memsync config set web_ui_host 0.0.0.0
sudo systemctl restart memsync   # Linux/Pi
```

Then try `http://raspberrypi.local:5000` again.

### Port 5000 or 5001 is already in use

```bash
memsync config set web_ui_port 5050
memsync config set capture_port 5051
sudo systemctl restart memsync
```

### OneDrive not syncing on Pi

```bash
rclone ls onedrive:.claude-memory/
```

If this returns an error, your rclone token has expired. Reconnect:
```bash
rclone config reconnect onedrive:
```

### Harvest finds no sessions on Pi

This is expected — Claude Code session files (`~/.claude/projects/`) only exist on machines where you run Claude Code interactively. The Pi won't find any because you don't use Claude Code on the Pi.

Just run `memsync harvest` on your Mac or Windows machine after sessions you care about. The Pi handles the nightly refresh (notes from mobile capture) automatically.

### "memsync daemon install" fails on Windows

This is expected — Windows service install isn't supported yet. Use Task Scheduler instead (see the Windows section above).

### Jobs show "pending" in daemon schedule

```
Next run: (pending — start daemon)
```

This is normal when running `memsync daemon schedule` separately — it creates a fresh instance to list jobs, which haven't started yet in that instance. The actual running daemon has them fully scheduled. Check with `memsync daemon status` to confirm the daemon is running.
