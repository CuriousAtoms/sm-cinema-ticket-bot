# SM Cinema Ticket Availability Bot

An automated ticket availability monitor for **Avengers: Doomsday** at **SM Cinema**, running completely free via **GitHub Actions** and sending instant notifications through **Discord Webhooks**.

---

## 🚀 Features

- **Automated Monitoring:** Runs every 5 minutes on GitHub Actions.
- **Client-Side Rendering Support:** Uses headless Playwright (Chromium) to handle JavaScript SPA hydration.
- **Anti-Bot & Evasion Protections:** Masks `navigator.webdriver` and sets realistic viewport, locale, and user agents to avoid Cloudflare bot blocking.
- **Three-State Detection:** Every check resolves to `AVAILABLE`, `UNAVAILABLE`, or `ERROR`. A timeout, HTTP error, Cloudflare challenge, or unrecognisable page is an `ERROR` — never a confident "no tickets yet".
- **Scoped Signal Matching:** Reads the film status badge and buttons inside the film-details region only, so site-wide navigation and footer links cannot trigger a false alarm. Showtime elements must contain a digit to count as a real session.
- **Detection Drift Guard:** If a page loads fine but matches no known availability *or* unavailability markers, the run is reported as `ERROR` rather than silently assuming tickets aren't out.
- **Duplicate Prevention:** Alert status is tracked in `state.json`, which is committed back to the repository **only when the status actually changes** — routine checks write nothing.
- **Schedule Keepalive:** A monthly empty commit stops GitHub from auto-disabling the cron after 60 days of repository inactivity.
- **Zero Cost:** 100% free with no credit card required.

---

## 🛠️ GitHub Repository Setup

1. **Create a Public GitHub Repository**
   - Push this codebase to your repository. A **public** repository is effectively required: private repositories on the Free plan get 2,000 Actions minutes per month, and a 5-minute cadence burns that in roughly two days.

2. **Create a Discord Webhook**
   - In Discord, go to your target channel settings -> **Integrations** -> **Webhooks** -> **New Webhook**.
   - Copy the Webhook URL.

3. **Configure Repository Secrets**
   - In your GitHub repo, navigate to **Settings** -> **Secrets and variables** -> **Actions** -> **New repository secret**.

| Secret Name | Value | Required |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | Your Discord webhook URL | Yes |
| `MOVIE_URL` | `https://www.smcinema.com/films/Avengers-Doomsday/HO00001619` | Optional (defaults to Avengers: Doomsday) |
| `MENTION` | `@everyone` or `<@&ROLE_ID>` | Optional |

4. **Workflow Permissions**
   - Both workflows declare `permissions: contents: write` themselves, so no repository setting is normally needed.
   - If the "Save state" step ever fails to push, go to **Settings** -> **Actions** -> **General** and select **Read and write permissions**.

---

## 💻 Local Development & Testing

### 1. Install Dependencies
```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Run the Test Suite
The decision logic is a pure function with no browser dependency, so the tests run in well under a second:
```bash
python -m unittest discover
```

### 3. Test the Discord Webhook
Verify your webhook configuration without scraping.

PowerShell:
```powershell
$env:DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."
python checker.py --test-discord
```

cmd.exe:
```cmd
set DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
python checker.py --test-discord
```

### 4. Run the Scraper Locally
Point `STATE_FILE` somewhere temporary so your committed `state.json` is left alone:
```powershell
$env:STATE_FILE = "$env:TEMP\state-test.json"
python checker.py
```

---

## 🧠 How It Decides

| Situation | Result |
|---|---|
| Booking CTA (`book now`, `buy tickets`, ...) inside the film-details region | `AVAILABLE` |
| Showtime session elements containing a time | `AVAILABLE` |
| Status badge reads `now showing` / `advance tickets` / `tickets on sale` | `AVAILABLE` |
| Status badge or page text says `coming soon` and no booking CTA is present | `UNAVAILABLE` |
| Cloudflare challenge, HTTP error, timeout, or blank render | `ERROR` — state untouched |
| Page loads but matches nothing recognisable | `ERROR` — detection drift |

`ERROR` never resets the notification flag and never writes state, so a temporary block cannot cause a repeat alert.

---

## 📁 Project Structure

```
.
├── .github/
│   └── workflows/
│       ├── check.yml       # 5-minute cron scheduler
│       └── keepalive.yml   # Monthly commit so the cron is not auto-disabled
├── .gitignore              # Ignored files
├── checker.py              # Scraper, decision logic & Discord notifier
├── test_checker.py         # Unit tests for the decision logic (stdlib unittest)
├── requirements.txt        # Python dependencies
├── state.json              # Notification status tracker
├── discord-ticket-bot-spec.md  # Original design document (historical)
└── README.md               # Documentation
```
