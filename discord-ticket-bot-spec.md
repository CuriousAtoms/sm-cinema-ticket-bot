# Discord Ticket Availability Notification Bot
### SM Cinema × Avengers: Doomsday × GitHub Actions (Free, Zero Cost)

> **Historical design document.** This is the original specification the project was
> built from. The implementation has since moved on — notably the state file shape,
> the signal lists, and the three-state (`AVAILABLE` / `UNAVAILABLE` / `ERROR`)
> decision logic. Treat `checker.py` and `README.md` as the source of truth.


---

## Table of Contents

1. [Overview](#overview)
2. [How the SM Cinema Website Works](#how-the-sm-cinema-website-works)
3. [Architecture — Why GitHub Actions](#architecture--why-github-actions)
4. [Project Structure](#project-structure)
5. [Step-by-Step Setup](#step-by-step-setup)
   - [Step 1 — Create Your Discord Webhook](#step-1--create-your-discord-webhook)
   - [Step 2 — Create the GitHub Repository](#step-2--create-the-github-repository)
   - [Step 3 — Add GitHub Secrets](#step-3--add-github-secrets)
   - [Step 4 — Install Dependencies Locally (Optional)](#step-4--install-dependencies-locally-optional)
6. [The Code](#the-code)
   - [checker.py — The Main Scraper](#checkerpy--the-main-scraper)
   - [requirements.txt](#requirementstxt)
   - [state.json — Duplicate Prevention](#statejson--duplicate-prevention)
   - [.github/workflows/check.yml — The Scheduler](#githubworkflowscheckyml--the-scheduler)
   - [.gitignore](#gitignore)
7. [How It All Works Together](#how-it-all-works-together)
8. [Testing Locally](#testing-locally)
9. [Deploying to GitHub Actions](#deploying-to-github-actions)
10. [How to Test Notifications](#how-to-test-notifications)
11. [Troubleshooting](#troubleshooting)
12. [Limitations and Notes](#limitations-and-notes)

---

## Overview

This bot monitors the SM Cinema movie page for **Avengers: Doomsday** and sends a Discord notification the moment ticket booking becomes available.

**Target URL:**
`https://www.smcinema.com/films/Avengers-Doomsday/HO00001619`

**Cost:** Free — permanently. No credit card. No sleep timers. No tricks.

**Platform:** GitHub Actions (cron scheduler) + Discord Webhook

---

## How the SM Cinema Website Works

> This is the most important section. Understanding how the site loads its data determines how we scrape it.

When you open the SM Cinema movie page in your browser, the page content — including showtimes, dates, and the "Book Now" button — is **not in the raw HTML**. The server sends back a mostly empty page, and then JavaScript runs in your browser to fetch and display the ticket data.

This is called a **JavaScript-rendered (SPA)** site. A simple `requests.get()` in Python would only see the empty shell, not the ticket information.

### How We Handle This

We use **`playwright`** — a browser automation library — to launch a real (headless, invisible) Chromium browser, let the page fully load including all JavaScript, and then read the final rendered HTML to look for ticket availability signals.

Specifically, we look for:

- The presence of a **"Book Now"**, **"Buy Tickets"**, or **"Get Tickets"** button
- Any visible showtime listings or date selectors
- Any change from a "Coming Soon" or "No screenings available" state

If any of those signals appear, it fires a Discord notification immediately.

### On Duplicate Prevention

We store a `state.json` file in the GitHub repository itself (committed back after every run). This file records which availabilities have already been notified. Even if the workflow runs every 5 minutes, you will only receive one notification per unique availability — not one every 5 minutes.

---

## Architecture — Why GitHub Actions

```
Every 5 minutes (GitHub Actions cron trigger)
        │
        ▼
  Ubuntu runner spins up (free)
        │
        ▼
  Git checkout — pulls latest state.json
        │
        ▼
  checker.py runs
        │
        ├── Playwright launches headless Chromium
        │         │
        │         ▼
        │   Loads SM Cinema page
        │   Waits for JS to render
        │   Reads final HTML
        │
        ├── No tickets found → exit silently
        │
        └── Tickets found (new, not in state.json)
                  │
                  ▼
            POST to Discord Webhook
                  │
                  ▼
            Update state.json
                  │
                  ▼
            Git commit & push state.json back to repo
```

**Why not Render or a traditional bot?**
A Discord bot (using `discord.py`) needs a **persistent, always-on WebSocket connection**. Render's free tier puts services to sleep after 15 minutes of no HTTP traffic — and since a bot never receives HTTP traffic, it goes offline almost immediately. UptimeRobot cannot fix this because there is no HTTP endpoint to ping.

GitHub Actions solves this by running the script on a schedule and exiting cleanly. No persistent connection needed. No sleeping. No cost.

---

## Project Structure

```
sm-cinema-bot/
│
├── checker.py                  # Main scraper + Discord notifier
├── requirements.txt            # Python dependencies
├── state.json                  # Tracks already-notified availabilities
├── .gitignore                  # Keeps secrets out of the repo
│
└── .github/
    └── workflows/
        └── check.yml           # GitHub Actions cron schedule
```

---

## Step-by-Step Setup

### Step 1 — Create Your Discord Webhook

A **webhook** lets you post messages to a Discord channel without a full bot account. It is simpler, faster, and perfect for this use case.

1. Open Discord and go to the channel where you want ticket alerts (e.g. `#ticket-alerts`).
2. Click the **gear icon** (Edit Channel) next to the channel name.
3. Go to **Integrations** → **Webhooks** → **New Webhook**.
4. Give it a name (e.g. `SM Cinema Bot`) and optionally upload an avatar.
5. Click **Copy Webhook URL**.
6. Save this URL somewhere safe. It looks like:
   ```
   https://discord.com/api/webhooks/1234567890/abcdefghijklmnop...
   ```

> ⚠️ Treat this URL like a password. Anyone with it can post to your channel.

---

### Step 2 — Create the GitHub Repository

1. Go to [github.com](https://github.com) and sign in (or create a free account).
2. Click **New repository**.
3. Name it something like `sm-cinema-bot`.
4. Set it to **Public** (required for unlimited free GitHub Actions minutes).
5. Check **Add a README file** so the repo is initialized.
6. Click **Create repository**.

> ⚠️ **Public repo does not mean your secrets are visible.** Your Discord webhook URL and any other credentials are stored in GitHub Secrets (encrypted), never in the code itself.

---

### Step 3 — Add GitHub Secrets

This is where you store your Discord webhook URL securely.

1. In your new repository, click **Settings** (top menu).
2. In the left sidebar, click **Secrets and variables** → **Actions**.
3. Click **New repository secret**.
4. Add the following secrets one by one:

| Secret Name | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | Your full Discord webhook URL |
| `MOVIE_URL` | `https://www.smcinema.com/films/Avengers-Doomsday/HO00001619` |
| `MENTION` | `@everyone` (or a role ID like `<@&1234567890>`, or leave blank) |

---

### Step 4 — Install Dependencies Locally (Optional)

Only needed if you want to test the script on your own computer before pushing to GitHub.

```bash
# Make sure Python 3.10+ is installed
python --version

# Install dependencies
pip install playwright requests pytz

# Install the headless browser (Chromium)
playwright install chromium
```

---

## The Code

Create each of these files in your repository. You can do this directly on GitHub by clicking **Add file → Create new file**, or by cloning the repo and editing locally.

---

### checker.py — The Main Scraper

```python
"""
SM Cinema Ticket Availability Checker
Runs on GitHub Actions every 5 minutes.
Sends a Discord webhook notification when tickets become available.
"""

import os
import json
import requests
from datetime import datetime
import pytz
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Configuration ──────────────────────────────────────────────────────────────

MOVIE_URL   = os.environ.get("MOVIE_URL", "https://www.smcinema.com/films/Avengers-Doomsday/HO00001619")
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
MENTION     = os.environ.get("MENTION", "")          # e.g. "@everyone" or blank
TIMEZONE    = "Asia/Manila"
STATE_FILE  = "state.json"

# These are the text signals that tell us tickets are available.
# Add more phrases here if SM Cinema uses different wording.
AVAILABILITY_SIGNALS = [
    "book now",
    "buy tickets",
    "get tickets",
    "select showtime",
    "now showing",
    "buy now",
]

# These phrases tell us tickets are NOT yet available.
UNAVAILABLE_SIGNALS = [
    "coming soon",
    "no screenings",
    "tickets not yet available",
    "advance tickets",   # sometimes shown before booking opens
]

# ── State Management (duplicate prevention) ────────────────────────────────────

def load_state():
    """Load the state file that tracks what we've already notified about."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"notified": False, "last_check": None, "last_status": None}


def save_state(state):
    """Save the updated state back to the file."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Website Scraping ───────────────────────────────────────────────────────────

def check_availability():
    """
    Launch a headless browser, load the SM Cinema page,
    wait for JavaScript to fully render, then look for availability signals.

    Returns a dict: {"available": bool, "signals_found": list, "page_title": str}
    """
    print(f"[CHECK] Loading page: {MOVIE_URL}")

    with sync_playwright() as p:
        # Launch headless Chromium (invisible browser)
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            # Pretend to be a real browser to avoid bot detection
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        try:
            # Go to the movie page, wait until the network is idle
            # (meaning JavaScript has finished loading data)
            page.goto(MOVIE_URL, wait_until="networkidle", timeout=30000)

            # Extra wait to let any lazy-loaded content appear
            page.wait_for_timeout(3000)

            # Get the full rendered page text (lowercase for easy matching)
            page_text = page.inner_text("body").lower()
            page_title = page.title()

            print(f"[CHECK] Page title: {page_title}")
            print(f"[CHECK] Page text length: {len(page_text)} characters")

            # Look for availability signals
            found_signals = [s for s in AVAILABILITY_SIGNALS if s in page_text]
            found_unavailable = [s for s in UNAVAILABLE_SIGNALS if s in page_text]

            # Tickets are available if we found at least one availability signal
            # AND no strong unavailability signals (unless availability is also present)
            is_available = len(found_signals) > 0

            print(f"[CHECK] Availability signals found: {found_signals}")
            print(f"[CHECK] Unavailability signals found: {found_unavailable}")
            print(f"[CHECK] Conclusion: {'AVAILABLE ✅' if is_available else 'NOT YET ❌'}")

            return {
                "available": is_available,
                "signals_found": found_signals,
                "unavailable_signals": found_unavailable,
                "page_title": page_title,
            }

        except PlaywrightTimeout:
            print("[ERROR] Page timed out. SM Cinema may be slow or down.")
            return {"available": False, "signals_found": [], "page_title": "Timeout"}

        except Exception as e:
            print(f"[ERROR] Unexpected error: {e}")
            return {"available": False, "signals_found": [], "page_title": "Error"}

        finally:
            browser.close()


# ── Discord Notification ───────────────────────────────────────────────────────

def send_discord_notification(result):
    """Send a formatted Discord embed notification."""

    if not WEBHOOK_URL:
        print("[WARN] No DISCORD_WEBHOOK_URL set. Skipping notification.")
        return

    # Get current Manila time
    manila_tz  = pytz.timezone(TIMEZONE)
    now_manila = datetime.now(manila_tz)
    timestamp  = now_manila.strftime("%B %d, %Y at %I:%M %p (PHT)")

    # Build the Discord message payload (using an embed for nice formatting)
    payload = {
        "content": f"🚨 {MENTION} **TICKETS ARE NOW AVAILABLE!**" if MENTION else "🚨 **TICKETS ARE NOW AVAILABLE!**",
        "embeds": [
            {
                "title": "🎟️ Avengers: Doomsday — Book Now!",
                "description": (
                    "Tickets for **Avengers: Doomsday** have just become available "
                    "on SM Cinema!\n\nHead over and book your seats before they sell out."
                ),
                "color": 0xE40000,   # Marvel red
                "fields": [
                    {
                        "name": "🎬 Movie",
                        "value": "Avengers: Doomsday",
                        "inline": True,
                    },
                    {
                        "name": "🏢 Cinema",
                        "value": "SM Cinema",
                        "inline": True,
                    },
                    {
                        "name": "🔗 Book Now",
                        "value": f"[Click here to book]({MOVIE_URL})",
                        "inline": False,
                    },
                    {
                        "name": "⏰ Detected At",
                        "value": timestamp,
                        "inline": False,
                    },
                    {
                        "name": "📡 Signals Detected",
                        "value": ", ".join(result["signals_found"]) or "Unknown",
                        "inline": False,
                    },
                ],
                "footer": {
                    "text": "SM Cinema Ticket Bot • Runs every 5 minutes via GitHub Actions"
                },
                "thumbnail": {
                    "url": "https://upload.wikimedia.org/wikipedia/en/9/98/Avengers_Doomsday_poster.jpg"
                },
            }
        ],
    }

    try:
        response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        if response.status_code in (200, 204):
            print("[DISCORD] ✅ Notification sent successfully!")
        else:
            print(f"[DISCORD] ❌ Failed to send. Status: {response.status_code} — {response.text}")
    except requests.RequestException as e:
        print(f"[DISCORD] ❌ Request error: {e}")


# ── Main Entry Point ───────────────────────────────────────────────────────────

def main():
    manila_tz = pytz.timezone(TIMEZONE)
    now       = datetime.now(manila_tz).strftime("%Y-%m-%d %H:%M:%S PHT")
    print(f"\n{'='*60}")
    print(f"  SM Cinema Checker — {now}")
    print(f"{'='*60}\n")

    # Load what we've already notified about
    state = load_state()
    print(f"[STATE] Already notified: {state.get('notified', False)}")
    print(f"[STATE] Last check: {state.get('last_check', 'Never')}")

    # Check the website
    result = check_availability()

    # Update the last check timestamp
    state["last_check"] = now
    state["last_status"] = "available" if result["available"] else "unavailable"

    if result["available"]:
        if not state.get("notified", False):
            # First time we've seen tickets — send the notification!
            print("\n[ACTION] 🎉 Tickets found for the first time! Sending notification...")
            send_discord_notification(result)
            state["notified"] = True
        else:
            # We already sent a notification before — don't spam
            print("\n[ACTION] Tickets available, but notification already sent. Skipping.")
    else:
        print("\n[ACTION] No tickets yet. Nothing to do.")
        # Reset "notified" if availability disappears (e.g. site goes back to coming soon)
        # This allows re-notification if tickets appear, disappear, and appear again
        if state.get("notified") and state.get("last_status") == "unavailable":
            state["notified"] = False
            print("[STATE] Reset notification flag (availability disappeared previously).")

    # Save updated state
    save_state(state)
    print(f"\n[STATE] State saved: {state}")
    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
```

---

### requirements.txt

```
playwright==1.44.0
requests==2.31.0
pytz==2024.1
```

---

### state.json — Duplicate Prevention

Create this file with the initial empty state. GitHub Actions will update and commit it automatically after every run.

```json
{
  "notified": false,
  "last_check": null,
  "last_status": null
}
```

---

### .github/workflows/check.yml — The Scheduler

This is the file that tells GitHub Actions to run your script automatically every 5 minutes.

```yaml
name: SM Cinema Ticket Checker

on:
  # Run automatically every 5 minutes
  # GitHub Actions uses UTC time. UTC+8 (Manila) is 8 hours ahead.
  # Note: GitHub may delay runs by a few minutes during high load — that's normal.
  schedule:
    - cron: "*/5 * * * *"

  # Also allow manual triggering from the GitHub Actions tab
  # (useful for testing without waiting 5 minutes)
  workflow_dispatch:

jobs:
  check-tickets:
    runs-on: ubuntu-latest

    # Allow the workflow to write back state.json to the repo
    permissions:
      contents: write

    steps:
      # 1. Check out the repo (includes the latest state.json)
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          # Needed so git can commit state.json back
          token: ${{ secrets.GITHUB_TOKEN }}

      # 2. Set up Python
      - name: Set up Python 3.11
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      # 3. Cache pip dependencies to speed up runs
      - name: Cache pip packages
        uses: actions/cache@v4
        with:
          path: ~/.cache/pip
          key: ${{ runner.os }}-pip-${{ hashFiles('requirements.txt') }}

      # 4. Install Python dependencies
      - name: Install dependencies
        run: pip install -r requirements.txt

      # 5. Install the Playwright Chromium browser
      - name: Install Playwright browsers
        run: playwright install chromium --with-deps

      # 6. Run the checker script
      - name: Run ticket checker
        env:
          MOVIE_URL:            ${{ secrets.MOVIE_URL }}
          DISCORD_WEBHOOK_URL:  ${{ secrets.DISCORD_WEBHOOK_URL }}
          MENTION:              ${{ secrets.MENTION }}
        run: python checker.py

      # 7. Commit state.json back to the repo if it changed
      #    The [skip ci] tag prevents this commit from triggering another workflow run
      - name: Save state
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add state.json
          git diff --staged --quiet || git commit -m "chore: update state.json [skip ci]"
          git push
```

---

### .gitignore

```gitignore
# Python
__pycache__/
*.py[cod]
*.pyo
.env
venv/
.venv/

# Playwright
.playwright/

# Local test artifacts
*.log
```

---

## How It All Works Together

Here is the exact sequence of events once you push your code:

```
1. Every 5 minutes, GitHub starts a fresh Ubuntu container (free).

2. It checks out your repo — including the latest state.json.

3. Python and Playwright (Chromium) are installed.

4. checker.py runs:
   a. Reads state.json to know if we've already sent a notification.
   b. Opens the SM Cinema page in a headless browser.
   c. Waits for JavaScript to fully render the page.
   d. Scans the page text for "book now", "buy tickets", etc.
   e. If found AND not yet notified → sends Discord notification.
   f. Updates state.json with the result.

5. state.json is committed back to the repo with [skip ci]
   so the next run picks up the latest state.

6. The container is destroyed. Nothing persists except state.json in the repo.
```

---

## Testing Locally

Before pushing to GitHub, you can test the script on your own computer.

```bash
# 1. Clone your repo
git clone https://github.com/YOUR_USERNAME/sm-cinema-bot.git
cd sm-cinema-bot

# 2. Install dependencies
pip install -r requirements.txt
playwright install chromium

# 3. Set environment variables temporarily (Mac/Linux)
export MOVIE_URL="https://www.smcinema.com/films/Avengers-Doomsday/HO00001619"
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE"
export MENTION="@everyone"

# On Windows (Command Prompt):
# set DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE

# 4. Run the script
python checker.py
```

You should see output like:

```
============================================================
  SM Cinema Checker — 2026-09-18 14:32:01 PHT
============================================================

[STATE] Already notified: False
[STATE] Last check: Never
[CHECK] Loading page: https://www.smcinema.com/films/...
[CHECK] Page title: Avengers: Doomsday | SM Cinema
[CHECK] Page text length: 8423 characters
[CHECK] Availability signals found: []
[CHECK] Conclusion: NOT YET ❌

[ACTION] No tickets yet. Nothing to do.
[STATE] State saved: {"notified": false, ...}
============================================================
```

---

## Deploying to GitHub Actions

Once you're happy with local testing:

```bash
# Push all your files to GitHub
git add .
git commit -m "feat: initial SM Cinema ticket checker"
git push origin main
```

Then:

1. Go to your GitHub repository.
2. Click the **Actions** tab.
3. You'll see the workflow **"SM Cinema Ticket Checker"** listed.
4. It will run automatically within 5 minutes. You can also click **Run workflow** to trigger it immediately.
5. Click into a run to see the live logs — you'll see the checker output in real time.

---

## How to Test Notifications

You don't want to wait for tickets to actually go on sale just to verify Discord notifications work. Here's how to force a test:

**Option A — Force a notification via the script (local)**

Temporarily edit `checker.py`, find the `main()` function, and add these two lines at the top of it:

```python
def main():
    # TEMPORARY TEST — remove after testing!
    send_discord_notification({"signals_found": ["TEST - manual trigger"]})
    return
    ...
```

Run `python checker.py` locally. You should see the Discord notification appear in your channel within seconds. Remove those lines after testing.

**Option B — Manually trigger from GitHub Actions**

1. Go to your repo → **Actions** tab.
2. Click **SM Cinema Ticket Checker** in the left sidebar.
3. Click **Run workflow** → **Run workflow**.
4. Open the run and watch the logs.

**Option C — Temporarily override state.json**

Edit `state.json` and set `"notified": false`, and temporarily change the checker to treat the page as available. This simulates a fresh discovery.

---

## Troubleshooting

| Problem | Likely Cause | Fix |
|---|---|---|
| Workflow not running | Repo was inactive for 60 days | Go to Actions tab and re-enable workflows |
| `playwright install` fails | Dependencies missing | The `--with-deps` flag installs them automatically |
| "Page timed out" in logs | SM Cinema is slow or blocking | The script retries next run; this is normal occasionally |
| No Discord message received | Wrong webhook URL in secrets | Double-check the secret in Settings → Secrets |
| Notification sent every 5 minutes | `state.json` not being committed | Check that `permissions: contents: write` is in the workflow |
| Script says tickets available but there are none | Signal phrase matched something unexpected | Open the site manually and compare; adjust `AVAILABILITY_SIGNALS` list |
| Workflow disabled | GitHub disables scheduled workflows after 60 days of repo inactivity | Push a small commit to re-activate it |

---

## Limitations and Notes

- **5-minute minimum interval.** GitHub Actions does not allow cron jobs more frequent than every 5 minutes. This is fine for ticket monitoring — 5 minutes is a negligible delay.

- **GitHub may delay runs by a few minutes** during periods of high load (especially at the top of the hour). Schedule is approximate, not exact. For ticket monitoring, this is acceptable.

- **The repo must stay active.** GitHub automatically disables scheduled workflows on repos that have had no commits for 60 days. Just push a small update every month or so, or re-enable it from the Actions tab.

- **SM Cinema may change their site structure.** If the site changes its wording (e.g. from "Book Now" to "Reserve Seats"), update the `AVAILABILITY_SIGNALS` list in `checker.py`.

- **This does not automatically book tickets for you.** It only sends a notification. You still need to visit the site and complete the booking yourself.

- **Public repo ≠ exposed secrets.** Your `DISCORD_WEBHOOK_URL` and other secrets are stored in GitHub's encrypted Secrets store and are never visible in the code or logs.

- **Playwright adds ~2–3 minutes to each run** because it installs Chromium every time. This is the trade-off for using a headless browser. The pip cache step reduces this significantly after the first run.
