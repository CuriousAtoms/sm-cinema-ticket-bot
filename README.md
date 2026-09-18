# SM Cinema Ticket Availability Bot

An automated ticket availability monitor for **Avengers: Doomsday** at **SM Cinema**, running completely free via **GitHub Actions** and sending instant notifications through **Discord Webhooks**.

---

## 🚀 Features

- **Automated Monitoring:** Runs every 5 minutes on GitHub Actions.
- **Client-Side Rendering Support:** Uses headless Playwright (Chromium) to handle JavaScript SPA hydration.
- **Anti-Bot & Evasion Protections:** Masks `navigator.webdriver` and sets realistic viewport, locale, and user agents to avoid Cloudflare bot blocking.
- **Smart Signal Matching:** Focuses on booking CTAs and excludes global navigation items (preventing false positives from "Now Showing" nav items).
- **Duplicate Prevention:** Keeps track of alert status via `state.json` committed back to the repository.
- **Zero Cost:** 100% free with no credit card required.

---

## 🛠️ GitHub Repository Setup

1. **Create a Public GitHub Repository**
   - Push this codebase to your repository. (A public repository provides unlimited free GitHub Actions minutes).

2. **Create a Discord Webhook**
   - In Discord, go to your target channel settings -> **Integrations** -> **Webhooks** -> **New Webhook**.
   - Copy the Webhook URL.

3. **Configure GitHub Repository Secrets**
   - In your GitHub repo, navigate to **Settings** -> **Secrets and variables** -> **Actions** -> **New repository secret**.
   - Add the following secrets:

| Secret Name | Value | Required |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | Your Discord webhook URL | Yes |
| `MOVIE_URL` | `https://www.smcinema.com/films/Avengers-Doomsday/HO00001619` | Optional (defaults to Avengers: Doomsday) |
| `MENTION` | `@everyone` or `<@&ROLE_ID>` | Optional |

4. **Workflow Permissions**
   - In your GitHub repo, go to **Settings** -> **Actions** -> **General**.
   - Under **Workflow permissions**, select **Read and write permissions** so the bot can save `state.json`.

---

## 💻 Local Development & Testing

### 1. Install Dependencies
```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Test Discord Webhook
Verify your webhook configuration without scraping:
```bash
set DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
python checker.py --test-discord
```

### 3. Run Scraper Locally
```bash
python checker.py
```

---

## 📁 Project Structure

```
.
├── .github/
│   └── workflows/
│       └── check.yml       # 5-minute cron scheduler
├── .gitignore              # Ignored files
├── checker.py              # Main scraper & Discord notifier
├── requirements.txt        # Python dependencies
├── state.json              # Notification status tracker
└── README.md               # Documentation
```
