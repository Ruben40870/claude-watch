# Claude Watch

A lightweight GNOME system tray indicator that shows your Claude.ai usage in real time — rate limit consumption, weekly usage, and prepaid credit balance.

![Tray example: 5h: 42%  W: 31%  E: €3.20](https://placehold.co/400x60/1a1a1a/ffffff?text=5h%3A+42%25++W%3A+31%25++E%3A+%E2%82%AC3.20)

---

## What it shows

**Tray label** (always visible in the top bar):

```
5h: 42%  W: 31%  E: €3.20
```

| Part | Meaning |
|------|---------|
| `5h: 42%` | How much of your 5-hour rate limit you've used |
| `W: 31%` | How much of your weekly usage limit you've used |
| `E: €3.20` | Extra usage — either amount spent or remaining credit balance (optional, enable in Settings) |

**Click the tray label** to open the menu:

```
Claude Watch
─────────────────────────────
Last 5 hours:   42%  —  resets in 3h 12m
This week:      31%  —  resets in 4d 7h
Extra usage:    €3.20 remaining
─────────────────────────────
Updated: 14:32
─────────────────────────────
Refresh now
Settings
Quit
```

---

## How it works

Claude Watch reads your usage data directly from the claude.ai website using your existing Chrome or Chromium browser session — no API key required, no separate login.

1. It extracts your `sessionKey` cookie from Chrome's local cookie database (decrypting it using your GNOME keyring, the same way Chrome does).
2. It launches a headless Chrome instance via Playwright and uses your session to call claude.ai's internal API endpoints.
3. It reads the response and displays the numbers in the tray.

Your session cookie never leaves your machine — all requests go directly from your computer to claude.ai.

**API endpoints used:**
- `/api/organizations` — to get your organisation ID
- `/api/organizations/{id}/usage` — 5-hour and weekly utilisation percentages and reset times
- `/api/organizations/{id}/overage_spend_limit` — monthly extra usage spend (if enabled)
- `/api/organizations/{id}/prepaid/credits` — remaining prepaid credit balance

---

## Requirements

- Ubuntu or Debian-based Linux with GNOME Shell
- Google Chrome or Chromium installed
- Logged in to claude.ai in Chrome

---

## Installation

```bash
git clone https://github.com/Ruben40870/claude-watch.git
cd claude-watch
bash install.sh
```

The install script:
1. Installs system packages (`python3-gi`, `python3-cairo`, `python3-cryptography`, `python3-dbus`, `gir1.2-ayatanaappindicator3-0.1`, `gnome-shell-extension-appindicator`)
2. Installs Playwright via pip
3. Launches the app

> **First install only:** After running `install.sh`, log out and back in once to activate the GNOME Shell AppIndicator extension. Then run the app again:
> ```bash
> python3 ~/path/to/claude-watch/claude_watch.py
> ```

---

## Running on startup

To have Claude Watch start automatically with your desktop session, create a systemd user service:

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/claude-watch.service << EOF
[Unit]
Description=Claude Watch tray indicator
After=graphical-session.target

[Service]
ExecStart=/usr/bin/python3 /path/to/claude-watch/claude_watch.py
Restart=on-failure
Environment=DISPLAY=:0
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%U/bus

[Install]
WantedBy=graphical-session.target
EOF

systemctl --user enable --now claude-watch.service
```

Replace `/path/to/claude-watch/` with the actual path where you cloned the repo.

---

## Settings

Click the tray label → **Settings**:

| Setting | Default | Description |
|---------|---------|-------------|
| Refresh interval | 5 min | How often to fetch new data (1, 5, or 10 minutes) |
| Icon color | White (`#ffffff`) | Color of the tray text |
| Show extra usage | Off | Show prepaid/overage spend in the tray and menu |
| Display as | Amount spent | Whether to show amount spent or remaining credit balance |

Settings are saved to `~/.config/claude-watch/config.json`.

---

## Debugging

Run with `CLAUDE_WATCH_DEBUG=1` to print raw API responses to stderr:

```bash
CLAUDE_WATCH_DEBUG=1 python3 claude_watch.py
```

---

## Notes

- **Chrome only:** Firefox session support is not currently implemented. You must be logged in to claude.ai in Chrome or Chromium.
- **`--no-sandbox`:** Playwright launches Chrome with `--no-sandbox` because most Linux desktop setups do not have user namespaces configured for Chromium's sandbox. The browser only visits claude.ai.
- **Extra usage:** The "extra usage" feature only shows data if your Claude.ai account has overage spending enabled. If it shows nothing, your account may use a different billing model.
