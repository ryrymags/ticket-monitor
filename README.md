# 🎫 Ticket Monitor

A friendly desktop app that watches Ticketmaster's Face Value Exchange 24/7 and sends you a Discord notification the moment tickets become available — so you never have to sit and refresh the page yourself.

> **Designed for sold-out shows on Face Value Exchange.** Ticketmaster's Face Value Exchange lets original buyers re-list tickets at the original purchase price. Tickets appear and disappear quickly — this tool catches them the second they go up.

---

## 📋 Recent Changes

<!-- CHANGELOG_START -->
- `27aa5f6`  2026-04-01  Add PostToolUse CI check hook
- `e1e69cb`  2026-04-01  Fix CI: lint error, deploy guard, remove dead auto-merge workflow
- `36f6dff`  2026-04-01  Initial release: v1.3.0

Full history: [CHANGELOG.md](CHANGELOG.md)
<!-- CHANGELOG_END -->

---

## ✨ Features

- **GUI app** — no command-line experience needed
- **Any event** — paste any Ticketmaster URL
- **Configurable preferences** — set how many tickets you need, your max price, and preferred sections
- **Discord alerts** — 🟢 BINGO when tickets match your criteria, 🟡 notice for anything else
- **Runs 24/7** — self-healing with automatic browser recycling and error recovery
- **Works on Mac and Windows**

---

## 🚀 Getting Started

### Step 1 — Install Python

> **Already have Python 3.9+? Skip to Step 2.**

- **Mac:** Download from [python.org/downloads](https://www.python.org/downloads/) and run the installer
- **Windows:** Download from [python.org/downloads](https://www.python.org/downloads/) — **make sure to check "Add Python to PATH"** during install

### Step 2 — Download this project

Click the green **Code** button on GitHub → **Download ZIP** → unzip somewhere easy to find (like your Desktop or Documents).

> **Git users:** `git clone https://github.com/YOUR_USERNAME/ticket-monitor.git`

### Step 3 — Run Setup (once)

| Platform | Double-click... |
|----------|----------------|
| Mac      | `setup_mac.command` |
| Windows  | `setup_windows.bat` |

This installs all dependencies. Takes about 1–2 minutes. You only need to do this once.

> **Mac note:** If macOS blocks the file the first time, right-click it → Open → Open anyway.

### Step 4 — Open the app

| Platform | Double-click... |
|----------|----------------|
| Mac      | `launch_mac.command` |
| Windows  | `launch_windows.bat` |

---

## 📱 Setting Up Discord Notifications

You need a free Discord server and a Webhook URL.

1. Open Discord → click your server name → **Server Settings** → **Integrations** → **Webhooks**
2. Click **New Webhook** → give it a name → copy the **Webhook URL**
3. Paste the URL in the app's **Notifications** tab
4. (Optional) Add your Discord User ID for @mention pings when tickets appear
   - Enable **User Settings → Advanced → Developer Mode** in Discord, then right-click your name → **Copy User ID**

---

## 🎵 Adding Events

1. Go to your event on Ticketmaster.com
2. Copy the URL from your browser's address bar
3. In the app, click **Events** → **＋ Add Event URL** → paste and confirm

The monitor will automatically detect the event name and date from the URL.

You can add multiple events (e.g., Night 1 and Night 2) — all checked simultaneously.

---

## 🎫 Setting Your Ticket Preferences

In the **Preferences** tab:

| Setting | What it does |
|---------|-------------|
| **Tickets needed together** | Minimum number of seats that must be available in the same group |
| **Max price per ticket** | Only BINGO if face value is at or below this amount |
| **Preferred sections** | Comma-separated section names, e.g. `LOGE, FLOOR, PIT` (optional) |
| **Require preferred section** | If on, only BINGO when a preferred section is available |
| **Also alert on non-matching** | Get an orange 🟡 alert even when tickets don't match your preferences |

> **Tip:** Turn on "Also alert on non-matching" — you'll always know when anything is available, even if it's not exactly what you wanted.

---

## 🔐 Logging In to Ticketmaster

The monitor uses your Ticketmaster account to stay logged in while checking pages. This helps avoid bot-detection.

1. Go to the **Login** tab
2. Click **Log In to Ticketmaster**
3. A browser window opens — log in normally
4. Come back to the app and click **Done — I'm Logged In**

Your login is saved locally on your computer only. You may need to repeat this every few weeks.

---

## ▶ Starting the Monitor

Once you've set up your events, Discord, and logged in:

1. Click **Monitor** in the sidebar (or use the bottom bar)
2. Click **▶ Start Monitor**
3. You'll see live logs in the app and get Discord alerts when tickets appear

> **Keep the app running** — the monitor stops when you close the app. For 24/7 monitoring, leave your computer on and the app open.

---

## 📲 Discord Alert Types

| Color | Meaning |
|-------|---------|
| 🟢 Green — **BINGO** | Tickets match all your preferences — go buy now! |
| 🟡 Orange — **Available** | Tickets are up but don't match your preferences |
| 🔵 Blue | Monitor status updates (heartbeat, auto-fix actions) |
| 🔴 Red | Error or sold-out again |

---

## ❓ Troubleshooting

**"No session found" in the Login tab**
→ Click "Log In to Ticketmaster" and complete the login.

**Monitor starts but no events are being checked**
→ Make sure you've added event URLs in the Events tab and saved.

**Discord test fails**
→ Double-check your Webhook URL — it should start with `https://discord.com/api/webhooks/`.

**App won't open on Mac**
→ Right-click `launch_mac.command` → Open → click Open in the dialog.

**Monitor crashes repeatedly**
→ Re-run `setup_mac.command` / `setup_windows.bat` to reinstall dependencies, then try again.

---

## ⚙️ Advanced: Running from Command Line

If you prefer the terminal:

```bash
# Activate the virtual environment
source venv/bin/activate   # Mac/Linux
venv\Scripts\activate      # Windows

# Run the GUI
python app.py

# Or run the monitor directly (headless)
python monitor.py --config config.yaml

# Test your Discord webhook
python monitor.py --test

# Validate config + browser session
python monitor.py --doctor
```

---

## 📝 Notes

- This tool only **notifies** you — it does not buy tickets automatically
- The monitor refreshes event pages every 45–75 seconds (randomized to be polite)
- Ticketmaster's Face Value Exchange tickets sell out fast — act quickly when you get a BINGO!
- Your Ticketmaster session may expire after a few weeks — re-login from the Login tab if you stop getting proper checks

---

## ↩️ Reverting to a Previous Version

Every commit is tracked in git. To undo something:

```bash
# Browse history
git log --oneline

# Safely undo a specific commit (creates a new "undo" commit — safe for shared repos)
git revert <commit-hash>

# Restore a single file to how it looked in a past commit
git checkout <commit-hash> -- path/to/file.py

# Nuclear option: reset your entire working tree to a past commit (destructive — local only)
git reset --hard <commit-hash>
```

Full change history: [CHANGELOG.md](CHANGELOG.md)

---

*Built with Python, Playwright, CustomTkinter, and Discord Webhooks.*
