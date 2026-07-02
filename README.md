# 🎫 Ticket Monitor

A friendly desktop app that watches Ticketmaster's Face Value Exchange 24/7 and sends you a Discord notification the moment tickets become available — so you never have to sit and refresh the page yourself.

> **Designed for sold-out shows on Face Value Exchange.** Ticketmaster's Face Value Exchange lets original buyers re-list tickets at the original purchase price. Tickets appear and disappear quickly — this tool catches them the second they go up.

---

## 📋 Recent Changes

<!-- CHANGELOG_START -->
- `bf58e25`  2026-07-02  Fix egress diagnostic: resolve IP over HTTPS before ip-api lookup
- `a7ea8c1`  2026-07-02  Add one-command sudo setup script for self-heal reboot
- `b1f4b1a`  2026-07-02  Slow cadence to 60-120s, variation probe, reboot self-heal, boot persistence
- `159b288`  2026-07-01  Audit fixes: atomic JSON writes, gitignore, robustness, tests
- `f6d2196`  2026-07-01  Add per-event Ticketmaster scheduler
- `c57c29d`  2026-07-01  Fix session health block handling
- `b31a0f3`  2026-07-01  Add Uptime tab, fix History-tab crash, ticket-seen stats, ntfy push UI
- `dc7fefd`  2026-06-24  Anti-block: headful Chrome, human-like nav, fast adaptive cadence
- `809c90f`  2026-06-24  Honest alert delivery, startup warmup grace, real login verification
- `f1b7054`  2026-06-24  Sync GUI/ping degraded state, add in-app fixes, challenge cooldown

Full history: [CHANGELOG.md](CHANGELOG.md)
<!-- CHANGELOG_END -->

---

## ✨ Features

- **GUI app** — no command-line experience needed
- **Any event** — paste any Ticketmaster URL
- **Multiple BINGO configs** — track different ticket categories, like preferred sections and budget seats
- **Discord alerts** — 🟢 BINGO leads the message when tickets match, 🟡 notice for anything else
- **Runs 24/7** — self-healing with automatic browser recycling and error recovery
- **Works on Mac and Windows**

---

## 🚀 Getting Started

### Step 1 — Install Python

> **Already have Python 3.11+? Skip to Step 2.**

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

## 📲 Setting Up Phone Push Alerts for Friends (ntfy.sh)

Want friends notified too — without making them use Discord? The monitor can also push
ticket alerts to **[ntfy.sh](https://ntfy.sh)**, a free push app. Anyone who subscribes
to your topic gets a phone notification the moment tickets are detected.

**You (once):** the easiest way is the app's **Notifications** tab → **ntfy Push** section:
flip **Enable ntfy push** on, pick a hard-to-guess **Topic**, and choose a **Priority**. Or set
the same `ntfy:` block in `config.yaml` directly:
```yaml
ntfy:
  enabled: true
  topic: "bingo-tix-SOMETHING-UNGUESSABLE"   # treat like a password — anyone with it can read alerts
  priority: "high"
  app_deep_link: "https://ticketmaster.onelink.me/7u25/edpUS?deep_link_value={url_encoded}&af_force_deeplink=true&is_retargeting=true"
```

**Your friends (once each):** install **ntfy** ([iOS](https://apps.apple.com/us/app/ntfy/id1625396347) /
[Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)) → tap **＋ Subscribe to topic**
→ enter your exact topic name → allow notifications.

**Test it:** double-click `test_notifications.command` (or run `python monitor.py --test-ticket-alert`).
It sends a sample alert to Discord **and** everyone subscribed.

Friends' pushes are throttled to match Discord's @-mention burst (loud at first, then quiet) —
so a lingering listing won't spam their phones.

### How the "open the Ticketmaster app" link works (the finicky part)

iOS makes this harder than it should be — notes so we don't re-derive it:

- **ntfy opens links in its own in-app browser**, which silently bypasses iOS
  **Universal Links**. So a plain `ticketmaster.com/event/...` URL never hands off to the app.
- **`ticketmaster://` only opens the app to its home screen** — there's no working event-path
  custom scheme.
- **The thing that works** is Ticketmaster's **AppsFlyer OneLink** (`ticketmaster.onelink.me/...`):
  its JS redirect + `af_force_deeplink=true` can open the app *on the specific event*.
- **But only from real Safari, not ntfy's in-app browser.** The trick: the notification's
  **body tap (`click`) opens in real Safari**, whereas the action **buttons** open the in-app
  browser. So the OneLink is wired to the **body tap**, and the **🌐 Open in Safari button** is
  the plain event URL (a reliable fallback that also works without the app / on Android).
- Net result: **tap the notification body → Ticketmaster app on the event** (occasionally needs a
  second tap — that's AppsFlyer being flaky, not the monitor). The OneLink template lives in
  `config.yaml` (`app_deep_link`), so if Ticketmaster ever changes it you can update it there with
  no code changes.

---

## 🎵 Adding Events

1. Go to your event on Ticketmaster.com
2. Copy the URL from your browser's address bar
3. In the app, click **Events** → **＋ Add Event URL** → paste and confirm

The monitor will automatically detect the event name and date from the URL.

You can add multiple events (e.g., Night 1 and Night 2) — all checked simultaneously.

---

## 🎫 Setting Your BINGO Configs

In the **Preferences** tab:

| Setting | What it does |
|---------|-------------|
| **Config name** | Label shown in Discord, e.g. `LOGE pairs` or `Budget triples` |
| **Tickets needed together** | Minimum number of seats that must be available in the same group |
| **Max price per ticket** | Only BINGO if face value is at or below this amount |
| **Preferred sections** | Comma-separated section names, e.g. `LOGE, FLOOR, PIT` (optional) |
| **Require preferred section** | If on, only BINGO when a preferred section is available |
| **Also alert on non-matching** | Get an orange 🟡 alert even when tickets don't match your preferences |

Click **Add BINGO Config** to watch for another category of tickets. Every config is checked for a BINGO; if more than one matches at once, the first matching config is the one named in Discord.

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
| 🟢 Green — **BINGO** | Tickets match one of your BINGO configs — go buy now! |
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

**Getting "activity paused" / blocked a lot (staying healthy)**
Ticketmaster's Akamai bot wall scores your **home IP + account together**, so the goal is to
look like one calm human, not a poller:
- The monitor now checks **one event at a time with a randomized 60–120s gap between any two
  checks** (config: `per_event_min_gap_between_checks_seconds` / `per_event_max_gap_between_checks_seconds`).
  Slower is healthier — don't lower these.
- When it detects a flag/pause it now goes **fully silent** for a cooldown so the block can
  actually decay, then resumes once Akamai's `_abck` trust cookie reads healthy again. (Watch the
  `abck=trusted/flagged` field and `Egress network:` line in `logs/monitor.log`.)
- **Do not stay logged into the same Ticketmaster account on your phone or other browsers while
  the monitor runs** — simultaneous sessions on one account read as bot-like coordination and
  raise your risk score. One account, one device (the monitor), at a time.
- Don't add privacy extensions/adblockers or a VPN to the monitor's Chrome — Ticketmaster treats
  those as bot signals; a clean, normal browser is safer.

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
- The monitor checks one due event at a time; each active event is rescheduled randomly every 45–105 seconds, with at least 20 seconds between event checks by default
- The browser keeps one active Ticketmaster event tab and navigates it between events instead of keeping every event page hot
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
