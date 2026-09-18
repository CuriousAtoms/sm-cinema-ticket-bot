"""
SM Cinema Ticket Availability Checker
Monitors SM Cinema for movie ticket availability and sends Discord notifications.
Runs on GitHub Actions on a schedule or locally.
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone, timedelta

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def get_manila_now():
    """Returns current datetime in Philippine Time (PHT, UTC+8)."""
    pht = timezone(timedelta(hours=8))
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Manila"))
    except Exception:
        try:
            import pytz
            return datetime.now(pytz.timezone("Asia/Manila"))
        except Exception:
            return datetime.now(pht)


# ── Configuration ──────────────────────────────────────────────────────────────

MOVIE_URL = os.environ.get(
    "MOVIE_URL",
    "https://www.smcinema.com/films/Avengers-Doomsday/HO00001619"
)
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
MENTION = os.environ.get("MENTION", "")  # e.g. "@everyone" or "<@&ROLE_ID>"
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Manila")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

# Availability signals (status badges or direct action keywords)
# NOTE: In Philippine cinema ticketing (including SM Cinema), "advance tickets" or
# "advance booking" indicates that ticket booking/pre-sales are active prior to the
# official premiere date. Therefore, "advance tickets" is an availability signal.
AVAILABILITY_SIGNALS = [
    "now showing",
    "advance tickets",
    "tickets on sale",
    "book now",
    "buy tickets",
    "buy now",
    "get tickets",
    "select seats",
]

# Explicit action button phrases that confirm ticket booking is open
BOOKING_BUTTON_PHRASES = [
    "book now",
    "buy tickets",
    "buy now",
    "get tickets",
    "select seats",
]

# Signals indicating tickets are not yet released
UNAVAILABLE_SIGNALS = [
    "coming soon",
    "no screenings",
    "tickets not yet available",
]

# Cloudflare challenge indicators: title and body signatures that confirm a block page
CLOUDFLARE_TITLE_MARKERS = [
    "just a moment",
    "attention required",
    "security check",
    "access denied",
]

CLOUDFLARE_BODY_MARKERS = [
    "verify you are human",
    "challenge-running",
    "cf-turnstile",
    "cf-challenge",
    "cloudflare ray id",
    "checking your browser",
]


def check_cloudflare_challenge(page_title: str, body_text: str) -> bool:
    """
    Check if the loaded page is a Cloudflare interstitial, Turnstile challenge,
    or WAF block page rather than the actual SM Cinema website.
    """
    title_lower = (page_title or "").lower()
    body_lower = (body_text or "").lower()

    if any(marker in title_lower for marker in CLOUDFLARE_TITLE_MARKERS):
        return True

    if any(marker in body_lower for marker in CLOUDFLARE_BODY_MARKERS):
        return True

    return False


# ── Decision Logic ─────────────────────────────────────────────────────────────

def evaluate_signals(film_status, content_buttons, session_elements, cleaned_text):
    """
    Pure decision function taking extracted page data and returning:
        (status, found_available, found_unavailable, error_reason)

    Status values:
      - "AVAILABLE": Authoritative signals or active booking CTAs found.
      - "UNAVAILABLE": Authoritative unavailable signals (e.g. 'coming soon') present without booking CTAs.
      - "ERROR": No recognized availability or unavailability markers found (detection drift).
    """
    status_lower = (film_status or "").strip().lower()
    cleaned_lower = (cleaned_text or "").lower()

    # Normalize button text
    buttons = [
        (b or "").strip().lower()
        for b in (content_buttons or [])
        if (b or "").strip()
    ]

    # FIX 1: Filter session elements to non-empty strings containing at least one digit
    # e.g., '1:30 pm', '10:00', '13:45' (rejects empty shells, placeholder containers, or labels)
    valid_sessions = [
        (s or "").strip().lower()
        for s in (session_elements or [])
        if (s or "").strip() and any(c.isdigit() for c in s)
    ]

    found_available = []
    found_unavailable = []

    # 1. Evaluate film status badge
    if status_lower:
        if "coming soon" in status_lower:
            found_unavailable.append(f"film-status badge: '{status_lower}'")
        # In SM Cinema, "advance tickets" / "advance booking" indicates pre-sales are active
        for sig in ["now showing", "advance tickets", "tickets on sale", "book now"]:
            if sig in status_lower:
                found_available.append(f"film-status badge: '{status_lower}'")
                break

    # 2. Evaluate unavailable signals in scoped text
    for sig in UNAVAILABLE_SIGNALS:
        if sig in cleaned_lower:
            entry = f"page text: '{sig}'"
            if entry not in found_unavailable:
                found_unavailable.append(entry)

    # 3. Evaluate content action buttons
    has_booking_button = False
    for btn in buttons:
        for phrase in BOOKING_BUTTON_PHRASES:
            if phrase in btn:
                entry = f"booking button: '{btn}'"
                if entry not in found_available:
                    found_available.append(entry)
                has_booking_button = True

    # 4. Evaluate active showtime sessions
    if valid_sessions:
        found_available.append(f"active sessions: {len(valid_sessions)} detected")

    # 5. Evaluate availability keywords in cleaned body
    for sig in ["now showing", "tickets on sale", "advance tickets"]:
        if sig in cleaned_lower:
            entry = f"page text: '{sig}'"
            if entry not in found_available:
                found_available.append(entry)

    # Decision Logic:
    # - If strong unavailable signals exist (e.g. "coming soon"):
    #   It is UNAVAILABLE unless an explicit booking CTA button is active.
    # - Unavailable signals veto general keywords.
    if found_unavailable:
        if has_booking_button:
            return ("AVAILABLE", found_available, found_unavailable, None)
        else:
            return ("UNAVAILABLE", found_available, found_unavailable, None)
    else:
        if found_available:
            return ("AVAILABLE", found_available, found_unavailable, None)
        else:
            # FIX 2: ZERO signals of either kind -> Detection drift / no recognized markers!
            error_reason = "Detection drift: zero recognized availability or unavailability markers"
            return ("ERROR", found_available, found_unavailable, error_reason)


# ── State Management ───────────────────────────────────────────────────────────

def load_state():
    """Load the state file that tracks whether a notification has been sent."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {
                    "notified": bool(data.get("notified", False)),
                    "last_status": data.get("last_status", "unavailable"),
                }
        except Exception as e:
            print(f"[STATE] Error loading {STATE_FILE}: {e}. Initializing fresh state.")
    return {"notified": False, "last_status": "unavailable"}


def save_state(state):
    """Save the state back to disk."""
    try:
        payload = {
            "notified": bool(state.get("notified", False)),
            "last_status": state.get("last_status", "unavailable"),
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
    except Exception as e:
        print(f"[STATE] Error saving {STATE_FILE}: {e}")


# ── Website Scraping ───────────────────────────────────────────────────────────

def check_availability():
    """
    Launch headless Chromium, load the SM Cinema page, wait for JavaScript rendering,
    and inspect the page for ticket availability signals.

    Distinguishes:
      - "AVAILABLE": Page loaded successfully and tickets are available.
      - "UNAVAILABLE": Page loaded successfully and confirmed tickets are not available.
      - "ERROR": Scraping failed, timed out, or encountered Cloudflare challenges / detection drift.

    Returns dict with status, availability bool, signals found, and error reason.
    """
    print(f"[CHECK] Loading page: {MOVIE_URL}")

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        print("[ERROR] Playwright is not installed. Run: pip install playwright && playwright install chromium")
        return {
            "status": "ERROR",
            "available": False,
            "signals_found": [],
            "unavailable_signals": [],
            "page_title": "Playwright Not Installed",
            "error_reason": "Playwright library missing",
        }

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id=TIMEZONE,
        )

        # Mask webdriver property to reduce Cloudflare bot detection triggers
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        page = context.new_page()

        try:
            try:
                response = page.goto(MOVIE_URL, wait_until="domcontentloaded", timeout=35000)
            except PlaywrightTimeout:
                print("[ERROR] Page navigation timed out.")
                return {
                    "status": "ERROR",
                    "available": False,
                    "signals_found": [],
                    "unavailable_signals": [],
                    "page_title": "Timeout",
                    "error_reason": "Navigation timeout",
                }

            # Check HTTP status
            if response and response.status >= 400:
                print(f"[ERROR] HTTP error {response.status}")
                return {
                    "status": "ERROR",
                    "available": False,
                    "signals_found": [],
                    "unavailable_signals": [],
                    "page_title": f"HTTP {response.status}",
                    "error_reason": f"HTTP status {response.status}",
                }

            # Allow time for SPA hydration (Lumos web app)
            page.wait_for_timeout(4000)

            page_title = (page.title() or "").strip()
            print(f"[CHECK] Page title: {page_title}")

            # Check for Cloudflare challenge / Turnstile page (FIX 3)
            body_text = page.inner_text("body")
            if check_cloudflare_challenge(page_title, body_text):
                print(f"[ERROR] Cloudflare challenge detected! Access blocked (Title: '{page_title}').")
                return {
                    "status": "ERROR",
                    "available": False,
                    "signals_found": [],
                    "unavailable_signals": [],
                    "page_title": page_title,
                    "error_reason": "Cloudflare challenge block",
                }

            # Verify the page contains meaningful content
            if len(body_text.strip()) < 150:
                print("[ERROR] Page content too short; dynamic content failed to render.")
                return {
                    "status": "ERROR",
                    "available": False,
                    "signals_found": [],
                    "unavailable_signals": [],
                    "page_title": page_title,
                    "error_reason": "Incomplete page render",
                }

            # Scoped evaluation of main movie content
            scraped = page.evaluate("""() => {
                // 1. Check film status badge (e.g. .v-film-status)
                const statusEl = document.querySelector('.v-film-status');
                const filmStatus = statusEl ? statusEl.innerText.trim().toLowerCase() : '';

                // 2. Buttons in main film content (exclude site header, footer, loyalty modal)
                const contentButtons = Array.from(
                    document.querySelectorAll(
                        '.v-film-details-banner button, .v-showtime-picker button, [class*="film-details"] button'
                    )
                ).map(b => (b.innerText || '').trim().toLowerCase()).filter(t => t.length > 0);

                // 3. Active session or showtime elements (filter out empty strings)
                const sessionElements = Array.from(
                    document.querySelectorAll(
                        '[class*="session-button"], [data-session-id], [class*="showtime-time"]'
                    )
                ).map(s => (s.innerText || '').trim().toLowerCase()).filter(t => t.length > 0);

                // 4. Cleaned page text (strip site-wide navigation, header, and footer)
                const clone = document.body.cloneNode(true);
                const removeSelectors = [
                    'header',
                    'footer',
                    'nav',
                    '.header-additional-menu',
                    '.header-modal-and-button',
                    '.header-loyalty-section'
                ];
                removeSelectors.forEach(sel => {
                    clone.querySelectorAll(sel).forEach(el => el.remove());
                });

                return {
                    filmStatus: filmStatus,
                    contentButtons: contentButtons,
                    sessionElements: sessionElements,
                    cleanedText: clone.innerText.toLowerCase()
                };
            }""")

            film_status = scraped["filmStatus"]
            content_buttons = scraped["contentButtons"]
            session_elements = scraped["sessionElements"]
            cleaned_text = scraped["cleanedText"]

            print(f"[CHECK] Film status badge: '{film_status}'")
            print(f"[CHECK] Content action buttons: {content_buttons}")
            print(f"[CHECK] Scoped session elements: {session_elements}")

            # Pure decision evaluation
            status, found_available, found_unavailable, error_reason = evaluate_signals(
                film_status, content_buttons, session_elements, cleaned_text
            )

            if status == "ERROR":
                print(f"[ERROR] {error_reason}")
            elif status == "AVAILABLE":
                print("[CHECK] Conclusion: AVAILABLE ✅")
            else:
                print(f"[CHECK] Unavailable signals veto availability: {found_unavailable}")
                print("[CHECK] Conclusion: CONFIRMED UNAVAILABLE ❌")

            print(f"[CHECK] Availability signals found: {found_available}")
            print(f"[CHECK] Unavailability signals found: {found_unavailable}")

            return {
                "status": status,
                "available": (status == "AVAILABLE"),
                "signals_found": found_available,
                "unavailable_signals": found_unavailable,
                "page_title": page_title,
                "error_reason": error_reason,
            }

        except PlaywrightTimeout:
            print("[ERROR] Page operation timed out.")
            return {
                "status": "ERROR",
                "available": False,
                "signals_found": [],
                "unavailable_signals": [],
                "page_title": "Timeout",
                "error_reason": "Playwright operation timeout",
            }

        except Exception as e:
            print(f"[ERROR] Unexpected error during scraping: {e}")
            return {
                "status": "ERROR",
                "available": False,
                "signals_found": [],
                "unavailable_signals": [],
                "page_title": "Error",
                "error_reason": str(e),
            }

        finally:
            browser.close()


# ── Discord Notification ───────────────────────────────────────────────────────

def send_discord_notification(result, is_test=False):
    """Send a formatted Discord embed notification via Webhook."""
    if not WEBHOOK_URL:
        print("[WARN] No DISCORD_WEBHOOK_URL set. Notification cannot be sent.")
        return False

    now_manila = get_manila_now()
    timestamp = now_manila.strftime("%B %d, %Y at %I:%M %p (PHT)")

    title = "🧪 [TEST] SM Cinema Ticket Bot Verification" if is_test else "🎟️ Avengers: Doomsday — Book Now!"
    header_content = (
        "🧪 **Test alert from SM Cinema Ticket Bot**"
        if is_test
        else (f"🚨 {MENTION} **TICKETS ARE NOW AVAILABLE!**" if MENTION else "🚨 **TICKETS ARE NOW AVAILABLE!**")
    )

    signals_text = ", ".join(result.get("signals_found", [])) or "Manual test trigger"

    payload = {
        "content": header_content,
        "embeds": [
            {
                "title": title,
                "description": (
                    "This is a test notification confirming your Discord webhook configuration works!"
                    if is_test
                    else (
                        "Tickets for **Avengers: Doomsday** have just become available "
                        "on SM Cinema!\n\nHead over and book your seats before they sell out."
                    )
                ),
                "color": 0x3498DB if is_test else 0xE40000,  # Blue for test, Marvel Red for alert
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
                        "value": signals_text,
                        "inline": False,
                    },
                ],
                "footer": {
                    "text": "SM Cinema Ticket Bot • Runs via GitHub Actions"
                },
                "thumbnail": {
                    "url": "https://upload.wikimedia.org/wikipedia/en/9/98/Avengers_Doomsday_poster.jpg"
                },
            }
        ],
    }

    try:
        try:
            import requests
            response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
            if response.status_code in (200, 204):
                print("[DISCORD] ✅ Notification sent successfully!")
                return True
            else:
                print(f"[DISCORD] ❌ Failed to send. Status: {response.status_code} — {response.text}")
                return False
        except ImportError:
            import urllib.request
            req = urllib.request.Request(
                WEBHOOK_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "SM-Cinema-Bot/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status in (200, 204):
                    print("[DISCORD] ✅ Notification sent successfully (via urllib)!")
                    return True
                else:
                    print(f"[DISCORD] ❌ Failed to send. Status: {resp.status}")
                    return False
    except Exception as e:
        print(f"[DISCORD] ❌ Request error: {e}")
        return False


# ── Main Entry Point ───────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(description="SM Cinema Ticket Availability Checker")
    parser.add_argument(
        "--test-discord",
        action="store_true",
        help="Send a test notification to Discord Webhook and exit",
    )
    args = parser.parse_args(argv)

    now = get_manila_now().strftime("%Y-%m-%d %H:%M:%S PHT")

    print(f"\n{'='*60}")
    print(f"  SM Cinema Checker — {now}")
    print(f"{'='*60}\n")

    if args.test_discord:
        print("[TEST] Sending test notification to Discord...")
        send_discord_notification(
            {"signals_found": ["CLI --test-discord trigger"]},
            is_test=True,
        )
        return

    state = load_state()
    old_state = dict(state)

    print(f"[STATE] Current status: {state.get('last_status')}")
    print(f"[STATE] Already notified: {state.get('notified')}")

    result = check_availability()
    status = result["status"]

    # 1. Handle scrape / network / Cloudflare errors or detection drift
    if status == "ERROR":
        print(f"\n[ACTION] Scrape resulted in ERROR ({result.get('error_reason')}).")
        print("[ACTION] Preserving existing notification state. No state changes saved.")
        print(f"\n{'='*60}\n")
        return

    # 2. Genuine page reading obtained
    previous_status = old_state.get("last_status", "unavailable")

    if status == "AVAILABLE":
        state["last_status"] = "available"
        if not state.get("notified", False):
            print("\n[ACTION] 🎉 Tickets confirmed available for the first time! Sending notification...")
            notified = send_discord_notification(result)
            if notified or not WEBHOOK_URL:
                state["notified"] = True
        else:
            print("\n[ACTION] Tickets available, but notification was already sent. Skipping.")
    else:  # UNAVAILABLE
        state["last_status"] = "unavailable"
        print("\n[ACTION] Confirmed: No tickets available yet.")
        # Only reset notified flag if tickets were PREVIOUSLY confirmed available and have now disappeared
        if state.get("notified", False) and previous_status == "available":
            state["notified"] = False
            print("[STATE] Reset notified flag: Tickets were previously available but are now confirmed unavailable.")

    # 3. Only persist state to disk if state actually changed
    if (
        state.get("notified") != old_state.get("notified")
        or state.get("last_status") != old_state.get("last_status")
    ):
        save_state(state)
        print(f"\n[STATE] Meaningful state change detected. Saved to {STATE_FILE}: {state}")
    else:
        print(f"\n[STATE] No state change ({state.get('last_status')}, notified={state.get('notified')}). Zero file modifications.")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
