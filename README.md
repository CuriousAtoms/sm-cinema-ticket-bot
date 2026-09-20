# SM Cinema Ticket Availability Bot

An automated ticket availability monitor for **Avengers: Doomsday** at **SM Cinema**, running completely free via **GitHub Actions** and sending instant notifications through **Discord Webhooks**.

---

## 🚀 Features

- **Automated Monitoring:** Runs every 5 minutes on GitHub Actions.
- **Direct JSON API First:** Directly queries SM Cinema's internal backend (`digital-api.smcinema.com/ocapi/v1/films/<ID>/availability`) using the JWT `gasToken` extracted from the page's Next.js SSR hydration data (`__NEXT_DATA__`). Completes in ~1-2 seconds with minimal bandwidth and zero browser footprint.
- **Opt-In Browser Fallback:** Retains the full Playwright headless browser implementation (with bot-evasion masks) via `USE_BROWSER_FALLBACK=1` and a manual `check-browser.yml` workflow.
- **Robust Error Classification:** Soft errors (network timeouts, Cloudflare 403/429 rate limits, 5xx server errors) are automatically retried with exponential backoff. Hard errors (404 Not Found, auth rejection, schema drift) fail loudly to trigger GitHub alert notifications.
- **Scoped Signal Matching & Interim Decision Rules:** Evaluates film availability categories, advance booking periods, and showtimes with conservative defaults biased toward alerting.
- **Two-Phase Alert System & Duplicate Prevention:** Alert phase is tracked in `state.json` (`"notify_phase": "none" | "announced" | "open"`). If an advance booking announcement was already sent in the past, an "open now" notification still fires the moment booking seats become live. State is committed back to the repository **only when the phase or status actually changes** — routine checks write nothing.
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
| `MOVIE_URL` | `https://www.smcinema.com/films/Avengers-Doomsday/HO00001619` | Optional (defaults to Avengers: Doomsday) — **must end with the film ID**, see below |
| `MENTION` | `@everyone` or `<@&ROLE_ID>` | Optional |

4. **Workflow Permissions**
   - Both workflows declare `permissions: contents: write` themselves, so no repository setting is normally needed.
   - If the "Save state" step ever fails to push, go to **Settings** -> **Actions** -> **General** and select **Read and write permissions**.

---

## 🚑 Troubleshooting: Tickets Detected but No Discord Message

The run log tells you which half failed. Check the Actions log for the `[DISCORD]` line:

| Log line | Meaning | Fix |
|---|---|---|
| `❌ Failed to send. Status: 404 — {"message": "Unknown Webhook", "code": 10015}` | The webhook no longer exists — deleted, regenerated, or the URL in the secret is truncated/mistyped. | Recreate the webhook in Discord and update the `DISCORD_WEBHOOK_URL` secret. |
| `❌ Failed to send. Status: 401/403` | Webhook token is wrong. | Re-copy the full webhook URL into the secret. |
| `❌ Failed to send. Status: 400` | Malformed payload. | Check `MENTION` is a valid `@everyone` or `<@&ROLE_ID>`. |
| `⚠️ No DISCORD_WEBHOOK_URL set` | The secret is missing or empty. | Add the secret; confirm the name matches exactly. |
| `✅ Notification sent successfully!` | Discord accepted it. | If you still see nothing, check the channel the webhook targets. |

A failed send never advances `notify_phase`, so the alert stays pending and fires on the
next run once the webhook works — you do not lose the notification.

### Testing the secret as actually stored

You cannot read a GitHub secret back, so a webhook that was pasted in wrong looks
identical to one that works. Run the **Verify Discord Webhook** workflow
(Actions tab → *Verify Discord Webhook* → **Run workflow**) to check the stored value
directly. It is read-only, never touches `state.json`, and prints only the URL's
*shape* — never the token:

```
[DISCORD] Webhook preflight (no secret values are printed):
[DISCORD]   total length: 121 chars
[DISCORD]   webhook id: 19 digits, ends ...6789
[DISCORD]   token: 68 chars (not shown)
[DISCORD]   shape looks correct
[DISCORD]   ✅ webhook is live — name 'ticket-bot', channel id 1234567890
```

A healthy webhook URL is ~121 characters: a 17–20 digit ID and a ~68 character token.
A short token means the value was truncated when pasted.

The same preflight runs locally against your own copy of the URL:

```bash
python checker.py --test-discord
```

---

## 🎯 Pointing the Bot at a Different Movie

`MOVIE_URL` **must end with the SM Cinema film ID** (`HO` + 8 digits). The film ID is
the only part the bot uses — the slug before it is ignored.

```
https://www.smcinema.com/films/Fall-2-Deadpoint/HO00001625
                                                └── the part that matters
```

This matters more than it looks. `smcinema.com` is a client-rendered single-page app:
**every** URL returns HTTP 200 with no `<title>`, including invented ones like
`/films/Totally-Made-Up-Slug/HO00001625`. A wrong URL therefore cannot be caught by
loading the page — only the film ID can be validated, so a URL that does not end in one
fails the run immediately with an explanatory error.

To find a film ID, list every film currently in the system:

```bash
python checker.py --list-films
```

Every run also prints the resolved title of the film it actually checked:

```
[CHECK] [API] Movie: Fall 2: Deadpoint
[CHECK] [API] Film ID: HO00001625
[CHECK] [API] Categories: ['NowShowing']
[CHECK] Conclusion: AVAILABLE ✅
```

If that `Movie:` line is not the film you expected, `MOVIE_URL` is not reaching the
script — check the secret name, or that your shell actually exported it.

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

### Primary: Direct JSON API (`filmAvailability`)
| Situation | Result |
|---|---|
| `'AdvanceBooking'` in categories, or `advanceBookingPeriods` non-empty | `AVAILABLE` (carries earliest `startsAt`) |
| `'NowShowing'` in categories | `AVAILABLE` |
| `categories` contains only `'ComingSoon'` alone | `UNAVAILABLE` |
| `showtimeAttributeIds` | Completely ignored (format tags like 2D/3D/IMAX) |
| Category value outside `{ComingSoon, NowShowing, AdvanceBooking}` | `AVAILABLE` (logged loudly as unrecognised) |
| Missing `filmAvailability`, unrecognised shape, or 401/403/404 | `ERROR` (hard) — run fails, state untouched |
| Network timeout, Cloudflare 403/429, or 5xx server error | `ERROR` (soft) — auto-retried up to 3 times with backoff |

### Fallback: Headless Browser (`USE_BROWSER_FALLBACK=1`)
| Situation | Result |
|---|---|
| Booking CTA (`book now`, `buy tickets`, ...) inside film-details | `AVAILABLE` |
| Showtime session elements containing a time digit | `AVAILABLE` |
| Status badge reads `now showing` / `advance tickets` / `tickets on sale` | `AVAILABLE` |
| Status badge or page text says `coming soon` without booking CTA | `UNAVAILABLE` |
| Cloudflare challenge, unrecognised page, or HTTP error | `ERROR` (hard) — run fails, state untouched |

### Multi-Phase Notification Tracking (`state.json`)
- `none` → `announced`: Advance booking schedule announced (`startsAt` in the future) → sends "Advance booking announced" alert.
- `announced` → `open`: Advance booking time has arrived (`startsAt` in past/now) → sends "Tickets open now!" alert.
- `none` → `open`: Tickets released straight to sale without advance announcement → sends "Tickets open now!" alert.
- `announced` → `announced` or `open` → `open`: No repeated alerts for the same phase.
- `ERROR` never resets the notification phase and never writes state, so a temporary network failure cannot cause duplicate alerts.
- Confirmed `UNAVAILABLE` resets `notify_phase` to `none` only if tickets were previously confirmed available.

---

## 📁 Project Structure

```
.
├── .github/
│   └── workflows/
│       ├── check.yml           # 5-minute cron scheduler (lightweight JSON API)
│       ├── check-browser.yml   # Manual trigger for browser fallback verification
│       ├── verify-webhook.yml  # Manual Discord webhook preflight (read-only)
│       ├── keepalive.yml       # Monthly commit so the cron is not auto-disabled
│       └── tests.yml           # Unit tests on every push (stdlib unittest)
├── .gitignore                  # Ignored files
├── checker.py                  # API/scraper client, decision logic & Discord notifier
├── test_checker.py             # Complete test suite (stdlib unittest, no external deps)
├── requirements.txt            # Python dependencies
├── state.json                  # Notification status tracker
├── discord-ticket-bot-spec.md  # Original design document (historical)
└── README.md                   # Documentation
```

