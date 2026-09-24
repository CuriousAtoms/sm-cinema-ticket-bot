# SM Cinema Ticket Availability Bot

An automated ticket availability monitor for **Avengers: Doomsday** at **SM Cinema**, running completely free via **GitHub Actions** and sending instant notifications through **Discord Webhooks**.

---

## 🚀 Features

- **Automated Monitoring:** Checks every 5 minutes, from a loop inside a long-running GitHub Actions job rather than from the cron itself — see [Why the cron is only a starter](#-why-the-cron-is-only-a-starter).
- **Direct JSON API First:** Directly queries SM Cinema's internal backend (`digital-api.smcinema.com/ocapi/v1/films/<ID>/availability`) using the JWT `gasToken` extracted from the page's Next.js SSR hydration data (`__NEXT_DATA__`). Completes in a few seconds with minimal bandwidth and zero browser footprint.
- **Session Sweep:** Every check looks for real sessions at every cinema — SM Cinema can put tickets on sale without changing the category. See [Why categories are not enough](#why-categories-are-not-enough).
- **Cinema Links & New-Cinema Alerts:** Alerts link each cinema you can book at. A cinema that starts selling after the first alert gets an alert of its own, naming just that cinema.
- **Every Listing of the Film:** SM Cinema lists each format (IMAX, Infinity Vision, ...) as a separate film. `WATCH_TITLE` makes the bot watch every listing with that in its title, each with its own alerts — see [Watching every listing](#watching-every-listing-of-a-film).
- **Opt-In Browser Fallback:** Retains the full Playwright headless browser implementation (with bot-evasion masks) via `USE_BROWSER_FALLBACK=1` and a manual `check-browser.yml` workflow.
- **Robust Error Classification:** Soft errors (network timeouts, Cloudflare 403/429 rate limits, 5xx server errors) are automatically retried with exponential backoff. Hard errors (404 Not Found, auth rejection, schema drift) fail loudly to trigger GitHub alert notifications.
- **Scoped Signal Matching & Interim Decision Rules:** Evaluates film availability categories, advance booking periods, and showtimes with conservative defaults biased toward alerting.
- **Two-Phase Alert System & Duplicate Prevention:** Alert phase is tracked in `state.json` (`"notify_phase": "none" | "announced" | "open"`). If an advance booking announcement was already sent in the past, an "open now" notification still fires the moment booking seats become live. State is committed back to the repository **only when the phase, status or announced cinemas actually change** — routine checks write nothing.
- **Schedule Keepalive:** A monthly empty commit stops GitHub from auto-disabling the cron after 60 days of repository inactivity.
- **Zero Cost:** 100% free with no credit card required.

---

## 🛠️ GitHub Repository Setup

1. **Create a Public GitHub Repository**
   - Push this codebase to your repository. A **public** repository is effectively required: private repositories on the Free plan get 2,000 Actions minutes per month, and this bot holds a runner open for hours at a time (see below), which would exhaust that almost immediately. Public repositories get unlimited free Actions minutes.

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

## ⏱️ Why the Cron Is Only a Starter

`check.yml` declares `cron: "0 * * * *"` — hourly — yet the bot checks every five
minutes. That is deliberate, and it is worth understanding before changing either
number.

GitHub runs `schedule` events on a **best-effort queue**, and drops most of a
high-frequency cron on free and public repositories. This repo previously declared
`*/5 * * * *`. Measured over 65 hours of real runs, it actually delivered:

| | |
|---|---|
| Declared | every 5 min |
| Median gap | **148 min** |
| Worst gap | **330 min** (5h30m) |
| Runs fired | **22 of ~774 expected (2.8%)** |

For a bot whose only job is noticing a ticket drop quickly, that is the difference
between five minutes late and half a day late — and nothing in the logs said so,
because every run that *did* fire succeeded.

So the cadence no longer comes from the cron. Each job loops internally, checking
every 5 minutes for 5h40m before exiting, and `concurrency` keeps exactly one
checker alive:

- The hourly cron just needs to land often enough that a run is always queued.
- GitHub keeps **one** run pending per concurrency group and cancels older pending
  ones. Those cancellations are normal, not failures.
- When the active run hits its deadline, the queued run takes over immediately, so
  coverage is continuous no matter how late any individual cron fires.

Two consequences worth knowing:

- **`state.json` is committed inside the loop**, not at job end. `checker.py` sends
  the alert and advances `notify_phase` together; if that phase only reached the
  repo hours later, a cancelled job would lose it and the next run would replay the
  same alert.
- **A hard failure ends the run immediately** so it shows red, rather than looping
  quietly on a bot that is checking nothing. Soft failures are already retried
  inside `checker.py` and do not end the loop.

---

## 🧪 Previewing the Real Alert Before Tickets Exist

`--test-discord` proves the webhook works, but it sends the blue 🧪 test embed — not
the alert people will actually receive. The genuine event cannot be scheduled, so
while the film is still `ComingSoon` the only way to review the real thing is to
simulate it.

Run the **Simulate Ticket Alert** workflow (Actions tab → *Simulate Ticket Alert*
→ **Run workflow**):

| Input | Effect |
|---|---|
| `phase: announced` | Sends 🚨 **ADVANCE BOOKING ANNOUNCED!** with a `startsAt` one week out. |
| `phase: open` | Sends 🚨 **TICKETS ARE NOW AVAILABLE!** with a `startsAt` two hours ago. |
| `phase: both` | Sends both, in the order a real run would have produced them. |
| `mention: true` | Includes the `MENTION` ping — lands exactly as the real alert will. |
| `mention: false` | Blanks `MENTION` for that run only: identical embed, nobody pinged. |

The simulation renders through the same `build_discord_payload()` as a genuine
detection, so what you see is what you will get. The deliberate tells are the
**Signals Detected** field, which always reads
`SIMULATED ALERT (--simulate-alert) — not a real detection`, and the single
`Example Cinema (simulated)` listed under **Book at**.

It is safe to run at any time. The workflow declares `contents: read`, and
`--simulate-alert` never loads or writes `state.json` — so a preview cannot
advance `notify_phase` and swallow the real alert when tickets actually go live.

The same thing runs locally once `DISCORD_WEBHOOK_URL` is exported:

```bash
python checker.py --simulate-alert both     # or: announced / open
```

---

## 🔬 End-to-End Detection Test

`--simulate-alert` builds its result by hand. That makes it perfect for checking
how the message *looks*, and useless for checking whether the bot would ever
*decide* to send one — it skips `evaluate_availability`,
`determine_booking_phase` and the `notify_phase` state machine entirely.

Those three only ever run in production against a film that says `ComingSoon`,
so the "yes, alert" branch can sit unexercised for months. The **End-to-End
Detection Test** workflow closes that gap by pointing the real checker at a film
whose tickets are genuinely on sale:

```bash
gh workflow run detect-test.yml \
  -f film_url="https://www.smcinema.com/films/Fall-2-Deadpoint/HO00001625" \
  -f mention=false
```

Nothing is faked. The live API returns a real "available" payload, the real
decision logic reaches its own verdict, and a real alert is sent — so the
Discord message is *true*: it names that film, whose tickets really are
available. Find a film to point it at with `python checker.py --list-films`.

| | `--simulate-alert` | `detect-test.yml` |
|---|---|---|
| Availability payload | fabricated | live from the API |
| `evaluate_availability` | skipped | runs |
| `determine_booking_phase` | skipped | runs |
| `notify_phase` transition | skipped | runs |
| Discord message | marked `SIMULATED ALERT` | a real, true alert |

### Why it cannot corrupt the real alert

A positive detection is exactly the thing that advances `notify_phase`, and a
leaked `"notify_phase": "open"` would silently swallow the genuine alert when
tickets finally drop. So three independent guards apply, not one:

1. `permissions: contents: read` — the job structurally cannot push.
2. `STATE_FILE` points outside the workspace, so `state.json` is never written.
3. There is no "Save state" step in this workflow at all.

A final step then asserts the committed `state.json` is byte-identical
afterwards, and fails the run if it is not:

```
state.json unchanged -- the real alert is still armed.
```

A fresh `STATE_FILE` each run also means the `none -> open` transition actually
fires, instead of being skipped as already-notified.

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

### Watching every listing of a film

SM Cinema lists each format of a film as a film of its own, with its own ID and its
own sessions — *Avengers Endgame: Encore* is three: standard, IMAX and Infinity
Vision. `MOVIE_URL` alone only ever sees one of them.

`WATCH_TITLE` adds every listing whose title contains it (case-insensitive), checked
alongside `MOVIE_URL`'s film. `check.yml` sets `WATCH_TITLE: Doomsday`, so a standard
or IMAX *Avengers: Doomsday* listing is picked up the first time it appears, with
alerts of its own. Every run logs what it checked:

```
[WATCH] Checking 1 film(s): MOVIE_URL's, plus every listing with 'Doomsday' in its title.
[FILM] Avengers: Doomsday (Infinity Vision) (HO00001619)
```

Leave `WATCH_TITLE` blank to watch `MOVIE_URL`'s film alone — the default, and what
the manual workflows use. If the film list cannot be read, that run checks
`MOVIE_URL`'s film alone and keeps every other listing's state as it was.

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
| `categories` contains only `'ComingSoon'` | Not trusted on its own — decided by the session sweep below |
| `showtimeAttributeIds` | Completely ignored (format tags like 2D/3D/IMAX) |
| Category value outside `{ComingSoon, NowShowing, AdvanceBooking}` | `AVAILABLE` (logged loudly as unrecognised) |
| Missing `filmAvailability`, unrecognised shape, or 401/403/404 | `ERROR` (hard) — run fails, state untouched |
| Network timeout, Cloudflare 403/429, or 5xx server error | `ERROR` (soft) — auto-retried up to 3 times with backoff |

### Session Sweep (every check)
While the categories say only `ComingSoon`, the sweep decides:

| Situation | Result |
|---|---|
| Any session at any cinema still has seats | `AVAILABLE` (open now) — the alert links each cinema with seats |
| Sessions exist, but every one is sold out | `UNAVAILABLE` — nothing to buy, so the alert is saved for when seats appear |
| No sessions at any cinema | `UNAVAILABLE` |
| Screenings listed, but no seat data returned for any cinema | `AVAILABLE` (bias toward alerting) |
| A sweep request is refused (4xx), or its response changes shape | `ERROR` (hard) — the `detail` from the API is logged |
| A sweep request times out, is throttled (429), or gets a 5xx | `ERROR` (soft) — retried with the rest of the check |

Once the categories already say available, the sweep only adds where to book. If it
fails then, the alert still goes out, just without cinema links; a hard sweep failure
fails the run afterwards, because new cinemas could no longer be noticed.

#### Why categories are not enough
Avengers: Doomsday (Infinity Vision) went on sale at SM Megamall, SM City Fairview
and SM Mall of Asia while `/availability` still said `['ComingSoon']` with no
advance booking period — for the film, and for each of those cinemas. The film page
did not help either: it only lists sessions for the cinemas a visitor has picked.
So the bot reported "no tickets" every five minutes while 66 sessions were on sale.

The sweep asks the questions the film page asks, but for every cinema:
`/sites`, then `/film-screening-dates` to see where and when the film plays, then
`/showtimes/availability` for seats at just those cinemas. Both showtime endpoints
refuse more than five cinemas per request, so all ~78 cinemas take 16 batched calls.
Seat records carry no cinema ID, so they are placed by their showtime ID, which starts
with it (`2022-36756` is SM Mall of Asia's); if that ever stops holding, the bot asks
one cinema at a time instead of guessing.

Cinema links point at the cinema's page, `https://www.smcinema.com/sites/<Name>/<ID>`,
built the same way as the site's own links.

### Fallback: Headless Browser (`USE_BROWSER_FALLBACK=1`)
| Situation | Result |
|---|---|
| Booking CTA (`book now`, `buy tickets`, ...) inside film-details | `AVAILABLE` |
| Showtime session elements containing a time digit | `AVAILABLE` |
| Status badge reads `now showing` / `advance tickets` / `tickets on sale` | `AVAILABLE` |
| Status badge or page text says `coming soon` without booking CTA | `UNAVAILABLE` |
| Cloudflare challenge, unrecognised page, or HTTP error | `ERROR` (hard) — run fails, state untouched |

### Switching Which Film Is Watched

`state.json` keeps each film's state under its own film ID:

```json
{"films": {"HO00001619": {"notify_phase": "open", "last_status": "available", "alerted_sites": ["2007", "2022", "2102"]}}}
```

A phase can therefore only ever describe the film it was recorded for: repoint
`MOVIE_URL` at a different film and that film starts from `none`. Without this,
switching to a film whose tickets are *already* on sale would inherit the previous
film's `"open"` phase and silently swallow the alert. A film keeps its state even in
runs that do not check it — `check-browser.yml` watches `MOVIE_URL`'s film alone and
commits `state.json` too, and dropping the others there would make the scheduled
checker re-announce all their cinemas. Films with nothing recorded yet are left out.
Older single-film state files still load, and are rewritten in this shape the next
time something changes.

### Multi-Phase Notification Tracking (`state.json`, per film)
- `none` → `announced`: Advance booking schedule announced (`startsAt` in the future) → sends "Advance booking announced" alert.
- `announced` → `open`: Advance booking time has arrived (`startsAt` in past/now) → sends "Tickets open now!" alert.
- `none` → `open`: Tickets released straight to sale without advance announcement → sends "Tickets open now!" alert.
- `announced` → `announced` or `open` → `open`: No repeated alerts for the same phase.
- While `open`: a cinema that starts selling gets a "Now booking at more cinemas" alert naming just the new cinemas. `alerted_sites` records every cinema announced so far, so each is announced once — even if it sells out and reopens.
- `ERROR` never touches that film's state, so a temporary network failure cannot cause duplicate alerts. The other watched films are still checked, and a hard failure fails the run only after all of them are done and saved.
- Confirmed `UNAVAILABLE` resets `notify_phase` to `none` only if tickets were previously confirmed available, and clears `alerted_sites` with it.
- No `alerted_sites` yet counts as nothing announced, so the next check announces every cinema selling. Assuming an earlier alert covered them all would silently swallow any cinema that started selling since — SM Mall of Asia did just that the day this was written.

---

## 📁 Project Structure

```
.
├── .github/
│   └── workflows/
│       ├── check.yml           # Hourly starter + in-job 5-minute loop (JSON API)
│       ├── check-browser.yml   # Manual trigger for browser fallback verification
│       ├── verify-webhook.yml  # Manual Discord webhook preflight (read-only)
│       ├── simulate-alert.yml  # Manual real-looking alert preview (read-only)
│       ├── detect-test.yml     # Manual end-to-end positive detection (read-only)
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

