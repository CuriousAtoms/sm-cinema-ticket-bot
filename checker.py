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

# Primary signals that indicate tickets are ready for booking
AVAILABILITY_SIGNALS = [
    "book now",
    "buy tickets",
    "get tickets",
    "buy now",
    "select showtime",
    "select cinema",
    "select date",
]

# Signals indicating tickets are not yet released
UNAVAILABLE_SIGNALS = [
    "coming soon",
    "no screenings",
    "tickets not yet available",
    "advance tickets",
]

# ── State Management ───────────────────────────────────────────────────────────

def load_state():
    """Load the state file that tracks whether a notification has been sent."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[STATE] Error loading {STATE_FILE}: {e}. Initializing fresh state.")
    return {"notified": False, "last_check": None, "last_status": None}


def save_state(state):
    """Save the updated state back to disk."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[STATE] Error saving {STATE_FILE}: {e}")


# ── Website Scraping ───────────────────────────────────────────────────────────

def check_availability():
    """
    Launch headless Chromium, load the SM Cinema page, wait for JavaScript rendering,
    and inspect the page for ticket availability signals.

    Returns dict with availability details.
    """
    print(f"[CHECK] Loading page: {MOVIE_URL}")

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        print("[ERROR] Playwright is not installed. Run: pip install playwright && playwright install chromium")
        return {"available": False, "signals_found": [], "page_title": "Playwright Not Installed"}

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
                page.goto(MOVIE_URL, wait_until="networkidle", timeout=35000)
            except PlaywrightTimeout:
                print("[WARN] networkidle timed out, waiting for load state...")
                page.wait_for_load_state("load", timeout=10000)

            # Allow additional time for any dynamic SPA hydration
            page.wait_for_timeout(3000)

            page_title = page.title()
            print(f"[CHECK] Page title: {page_title}")

            # Extract content from main content container or fallback to body
            # Exclude header and nav elements where generic links like "Now Showing" live
            content_text = page.evaluate("""() => {
                const header = document.querySelector('header');
                const nav = document.querySelector('nav');
                const clone = document.body.cloneNode(true);
                // Remove header and nav if found in clone to avoid false positives
                const h = clone.querySelector('header');
                if (h) h.remove();
                const n = clone.querySelector('nav');
                if (n) n.remove();
                return clone.innerText.toLowerCase();
            }""")

            print(f"[CHECK] Scanned content length: {len(content_text)} characters")

            # Search for availability keywords
            found_signals = [s for s in AVAILABILITY_SIGNALS if s in content_text]
            found_unavailable = [s for s in UNAVAILABLE_SIGNALS if s in content_text]

            # Also check for explicit booking button or call-to-action elements
            cta_buttons = page.query_selector_all(
                "button, a.btn, a[class*='btn'], a[class*='book'], div[role='button']"
            )
            for btn in cta_buttons:
                try:
                    btn_text = (btn.inner_text() or "").strip().lower()
                    for sig in ["book", "buy ticket", "select showtime", "get ticket"]:
                        if sig in btn_text and sig not in found_signals:
                            found_signals.append(f"button: {btn_text}")
                except Exception:
                    continue

            # Check if tickets are available
            # Available if explicit availability signals exist
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
            print("[ERROR] Page timed out. SM Cinema may be slow or temporarily blocking.")
            return {"available": False, "signals_found": [], "page_title": "Timeout"}

        except Exception as e:
            print(f"[ERROR] Unexpected error during scraping: {e}")
            return {"available": False, "signals_found": [], "page_title": "Error"}

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
                    "text": "SM Cinema Ticket Bot • Runs every 5 minutes via GitHub Actions"
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

def main():
    parser = argparse.ArgumentParser(description="SM Cinema Ticket Availability Checker")
    parser.add_argument(
        "--test-discord",
        action="store_true",
        help="Send a test notification to Discord Webhook and exit",
    )
    args = parser.parse_args()

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
    print(f"[STATE] Already notified: {state.get('notified', False)}")
    print(f"[STATE] Last check: {state.get('last_check', 'Never')}")

    result = check_availability()

    state["last_check"] = now
    state["last_status"] = "available" if result["available"] else "unavailable"

    if result["available"]:
        if not state.get("notified", False):
            print("\n[ACTION] 🎉 Tickets found for the first time! Sending notification...")
            notified = send_discord_notification(result)
            if notified or not WEBHOOK_URL:
                state["notified"] = True
        else:
            print("\n[ACTION] Tickets available, but notification was already sent. Skipping.")
    else:
        print("\n[ACTION] No tickets available yet.")
        # If status went back to unavailable, reset notified flag so future re-releases alert
        if state.get("notified") and state.get("last_status") == "unavailable":
            state["notified"] = False
            print("[STATE] Reset notified flag (tickets were previously marked unavailable).")

    save_state(state)
    print(f"\n[STATE] Updated state: {state}")
    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
