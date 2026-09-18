"""
SM Cinema Ticket Availability Checker
Monitors SM Cinema for movie ticket availability and sends Discord notifications.
Runs on GitHub Actions on a schedule or locally.
"""

import os
import sys
import re
import time
import random
import json
import argparse
from datetime import datetime, timezone, timedelta
try:
    import requests
    RequestTimeout = requests.Timeout
    RequestConnectionError = requests.ConnectionError
except ImportError:
    requests = None

    class RequestTimeout(Exception):
        """Fallback exception when requests is not installed."""

    class RequestConnectionError(Exception):
        """Fallback exception when requests is not installed."""


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

# Fallback user agent, used only if the browser will not report its own.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def normalize_user_agent(raw_ua):
    """
    Turn the bundled Chromium's own user agent into a plausible desktop one.

    Headless builds announce themselves as "HeadlessChrome", an obvious automation
    tell. Deriving the string from the browser instead of hardcoding it means the
    claimed Chrome version can never drift away from the real one — a stale version
    is itself something bot detection scores against you.
    """
    ua = (raw_ua or "").strip()
    if not ua:
        return DEFAULT_USER_AGENT
    return ua.replace("HeadlessChrome", "Chrome")


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


def error_result(reason, page_title="Error", hard=False):
    """
    Build an ERROR result.

    `hard` marks failures meaning the bot is blind rather than merely unlucky:
    blocked by Cloudflare, misconfigured, or reading a page it no longer
    understands. Hard failures exit non-zero so the Actions run goes red and
    GitHub emails you, instead of looking identical to "no tickets yet".
    Transient hiccups (a timeout, a half-rendered page) stay green.
    """
    return {
        "status": "ERROR",
        "available": False,
        "signals_found": [],
        "unavailable_signals": [],
        "page_title": page_title,
        "error_reason": reason,
        "hard": hard,
        "startsAt": None,
        "starts_at": None,
    }


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


# ── Browser-based Scraping (Fallback) ──────────────────────────────────────────

def check_availability_browser():
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
        return error_result("Playwright library missing", page_title="Playwright Not Installed", hard=True)

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

        # Ask the bundled Chromium what it calls itself, rather than asserting a
        # version that silently rots as the pinned Playwright release ages.
        user_agent = DEFAULT_USER_AGENT
        try:
            probe_context = browser.new_context()
            user_agent = normalize_user_agent(
                probe_context.new_page().evaluate("() => navigator.userAgent")
            )
            probe_context.close()
        except Exception as e:
            print(f"[WARN] Could not read the browser user agent ({e}); using fallback.")
        print(f"[CHECK] User agent: {user_agent}")

        context = browser.new_context(
            user_agent=user_agent,
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
                return error_result("Navigation timeout", page_title="Timeout", hard=False)

            # Check HTTP status
            if response and response.status >= 400:
                print(f"[ERROR] HTTP error {response.status}")
                return error_result(f"HTTP status {response.status}", page_title=f"HTTP {response.status}", hard=True)

            # Allow time for SPA hydration (Lumos web app)
            page.wait_for_timeout(4000)

            page_title = (page.title() or "").strip()
            print(f"[CHECK] Page title: {page_title}")

            # Check for Cloudflare challenge / Turnstile page (FIX 3)
            body_text = page.inner_text("body")
            if check_cloudflare_challenge(page_title, body_text):
                print(f"[ERROR] Cloudflare challenge detected! Access blocked (Title: '{page_title}').")
                return error_result("Cloudflare challenge block", page_title=page_title, hard=True)

            # Verify the page contains meaningful content
            if len(body_text.strip()) < 150:
                print("[ERROR] Page content too short; dynamic content failed to render.")
                return error_result("Incomplete page render", page_title=page_title, hard=False)

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
                # Detection drift: the page loaded but matched nothing we know.
                "hard": (status == "ERROR"),
                "startsAt": None,
                "starts_at": None,
            }

        except PlaywrightTimeout:
            print("[ERROR] Page operation timed out.")
            return error_result("Playwright operation timeout", page_title="Timeout", hard=False)

        except Exception as e:
            print(f"[ERROR] Unexpected error during scraping: {e}")
            return error_result(str(e), page_title="Error", hard=False)

        finally:
            browser.close()


# ── Direct JSON API Availability Check (Primary) ───────────────────────────────

def extract_film_id(movie_url: str) -> str:
    """
    Extract film ID from MOVIE_URL's last path segment, e.g. HO00001619.
    Keeps MOVIE_URL as the single point of configuration.
    """
    clean = (movie_url or "").strip().rstrip("/")
    return clean.split("/")[-1] if "/" in clean else clean


def extract_gas_token(html_text: str):
    """
    Extract the JWT gasToken from <script id="__NEXT_DATA__" type="application/json">.
    Re-extracted on every run; never cached to disk or logged in full.
    """
    if not html_text:
        return None
    match = re.search(
        r'<script\b[^>]*\bid="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html_text,
        re.DOTALL,
    )
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
        token = (
            data.get("props", {})
            .get("pageProps", {})
            .get("environment", {})
            .get("gasToken")
        )
        if token and isinstance(token, str) and token.strip():
            return token.strip()
    except Exception:
        return None
    return None


def parse_iso_datetime(dt_str):
    """
    Parse an ISO 8601 datetime string and normalize to Asia/Manila (UTC+8).
    Returns timezone-aware datetime or None.
    """
    if not dt_str or not isinstance(dt_str, str):
        return None
    try:
        dt = datetime.fromisoformat(dt_str.strip())
        pht = timezone(timedelta(hours=8))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=pht)
        else:
            dt = dt.astimezone(pht)
        return dt
    except Exception:
        return None


def find_earliest_starts_at(advance_booking_periods):
    """
    Finds the earliest startsAt from advanceBookingPeriods.
    Returns (earliest_raw_str, earliest_dt) or (None, None).
    """
    if not isinstance(advance_booking_periods, list):
        return None, None

    parsed_periods = []
    for period in advance_booking_periods:
        if isinstance(period, dict) and "startsAt" in period:
            raw = period.get("startsAt")
            if isinstance(raw, str) and raw.strip():
                dt = parse_iso_datetime(raw)
                if dt is not None:
                    parsed_periods.append((dt, raw.strip()))

    if not parsed_periods:
        return None, None

    parsed_periods.sort(key=lambda x: x[0])
    return parsed_periods[0][1], parsed_periods[0][0]


class AvailabilityEvaluation(tuple):
    """
    Result of evaluate_availability().
    Subclasses tuple to remain 100% backwards-compatible with 4-tuple unpacking:
        status, found_avail, found_unavail, error_reason = evaluate_availability(...)
    while also exposing startsAt and dict-like indexing.
    """
    def __new__(cls, status, found_avail, found_unavail, error_reason, starts_at=None):
        return super().__new__(cls, (status, found_avail, found_unavail, error_reason))

    def __init__(self, status, found_avail, found_unavail, error_reason, starts_at=None):
        self.status = status
        self.found_avail = found_avail
        self.found_unavail = found_unavail
        self.error_reason = error_reason
        self.starts_at = starts_at
        self.startsAt = starts_at

    def __getitem__(self, item):
        if isinstance(item, str):
            if item in ("status", "found_avail", "found_unavail", "error_reason", "starts_at", "startsAt"):
                return getattr(self, item)
            raise KeyError(item)
        return super().__getitem__(item)

    def get(self, key, default=None):
        try:
            return self[key]
        except (KeyError, IndexError):
            return default


def evaluate_availability(film_availability):
    """
    Evidence-based decision function evaluating the SM Cinema availability API payload.
    Derived from observed category values across all 46 live films:
      ['ComingSoon']                    x22
      ['NowShowing']                    x18
      ['ComingSoon', 'AdvanceBooking']  x6

    Decision rule:
      1. 'AdvanceBooking' in categories, or advanceBookingPeriods non-empty -> AVAILABLE
      2. 'NowShowing' in categories                                        -> AVAILABLE
      3. categories == ['ComingSoon'] alone                                -> UNAVAILABLE
      4. showtimeAttributeIds is completely IGNORED (these are format tags
         like 2D/3D/IMAX assigned long before tickets go on sale).
      5. A category value outside {ComingSoon, NowShowing, AdvanceBooking} -> AVAILABLE
         (logged loudly as an unrecognised value; bias stays toward alerting).
      6. categories missing, empty, or not a list                          -> ERROR (hard)
    """
    if not isinstance(film_availability, dict):
        return AvailabilityEvaluation("ERROR", [], [], "filmAvailability is not a dictionary or missing")

    if "categories" not in film_availability:
        return AvailabilityEvaluation("ERROR", [], [], "filmAvailability missing 'categories' key")

    categories = film_availability.get("categories")
    if not isinstance(categories, list) or len(categories) == 0:
        return AvailabilityEvaluation("ERROR", [], [], "categories missing, empty, or not a list")

    advance_booking = film_availability.get("advanceBookingPeriods")
    if not isinstance(advance_booking, list):
        advance_booking = []

    earliest_starts_at, _ = find_earliest_starts_at(advance_booking)

    # 1. 'AdvanceBooking' in categories, or advanceBookingPeriods non-empty -> AVAILABLE
    if "AdvanceBooking" in categories or len(advance_booking) > 0:
        signals = []
        if "AdvanceBooking" in categories:
            signals.append(f"AdvanceBooking in categories: {categories}")
        if len(advance_booking) > 0:
            signals.append(f"advanceBookingPeriods non-empty (count {len(advance_booking)})")
        if earliest_starts_at:
            signals.append(f"earliest startsAt: {earliest_starts_at}")
        return AvailabilityEvaluation("AVAILABLE", signals, [], None, starts_at=earliest_starts_at)

    # 2. 'NowShowing' in categories -> AVAILABLE
    if "NowShowing" in categories:
        return AvailabilityEvaluation("AVAILABLE", [f"NowShowing in categories: {categories}"], [], None)

    # 5. A category value outside {ComingSoon, NowShowing, AdvanceBooking} -> AVAILABLE
    known_categories = {"ComingSoon", "NowShowing", "AdvanceBooking"}
    unknown_categories = [c for c in categories if c not in known_categories]
    if unknown_categories:
        print(f"[WARN] [API] Unrecognised category value(s) detected: {unknown_categories}")
        return AvailabilityEvaluation(
            "AVAILABLE",
            [f"Unrecognised category value(s): {unknown_categories}"],
            [],
            None,
            starts_at=earliest_starts_at,
        )

    # 3. categories == ['ComingSoon'] alone -> UNAVAILABLE
    if set(categories) == {"ComingSoon"}:
        return AvailabilityEvaluation("UNAVAILABLE", [], [f"categories: {categories}"], None)

    return AvailabilityEvaluation("ERROR", [], [], f"Unhandled categories shape: {categories}")


def _check_availability_api_single(movie_url, session=None):
    """
    Execute a single browserless API check:
      1. Fetch HTML page with desktop UA.
      2. Extract gasToken from __NEXT_DATA__.
      3. Call digital-api availability endpoint.
      4. Evaluate availability.
    """
    if session is None:
        if requests is None:
            print("[ERROR] [API] 'requests' library not installed.")
            return error_result("'requests' library not installed", page_title="Missing Dependency", hard=True)
        session = requests.Session()

    film_id = extract_film_id(movie_url)
    if not film_id:
        return error_result("Could not extract film ID from MOVIE_URL", page_title="Config Error", hard=True)

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # Step 1: Fetch film page
    print(f"[CHECK] [API] Fetching film page: {movie_url}")
    try:
        resp = session.get(movie_url, headers=headers, timeout=15)
    except (RequestTimeout, RequestConnectionError) as e:
        print(f"[ERROR] [API] Page fetch network error: {e}")
        return error_result(f"Page fetch network error: {e}", page_title="Network Error", hard=False)
    except Exception as e:
        print(f"[ERROR] [API] Page fetch unexpected error: {e}")
        return error_result(f"Page fetch error: {e}", page_title="Fetch Error", hard=False)

    if resp.status_code == 404:
        print("[ERROR] [API] Movie URL returned HTTP 404.")
        return error_result("Movie URL returned HTTP 404 (film not found)", page_title="HTTP 404", hard=True)

    if resp.status_code in (403, 429):
        print(f"[ERROR] [API] Cloudflare throttle / challenge (HTTP {resp.status_code}).")
        return error_result(
            f"Page fetch Cloudflare throttle/block (HTTP {resp.status_code})",
            page_title=f"HTTP {resp.status_code}",
            hard=False,
        )

    if resp.status_code >= 500:
        print(f"[ERROR] [API] Page fetch server error (HTTP {resp.status_code}).")
        return error_result(
            f"Page fetch server error (HTTP {resp.status_code})",
            page_title=f"HTTP {resp.status_code}",
            hard=False,
        )

    if resp.status_code != 200:
        print(f"[ERROR] [API] Page fetch unexpected HTTP status: {resp.status_code}.")
        return error_result(
            f"Page fetch unexpected status HTTP {resp.status_code}",
            page_title=f"HTTP {resp.status_code}",
            hard=False,
        )

    # Step 2: Extract gasToken
    gas_token = extract_gas_token(resp.text)
    if not gas_token:
        print("[ERROR] [API] __NEXT_DATA__ missing or gasToken absent from page HTML.")
        return error_result(
            "__NEXT_DATA__ missing or gasToken absent from page HTML",
            page_title="Structure Changed",
            hard=True,
        )

    print(f"[CHECK] [API] gasToken acquired (length {len(gas_token)}, prefix '{gas_token[:8]}...')")

    # Step 3: Query digital-api availability endpoint
    api_url = f"https://digital-api.smcinema.com/ocapi/v1/films/{film_id}/availability"
    api_headers = {
        "Authorization": f"Bearer {gas_token}",
        "Accept": "application/json",
        "User-Agent": DEFAULT_USER_AGENT,
    }

    print(f"[CHECK] [API] Requesting availability: {api_url}")
    try:
        api_resp = session.get(api_url, headers=api_headers, timeout=15)
    except (RequestTimeout, RequestConnectionError) as e:
        print(f"[ERROR] [API] API request network error: {e}")
        return error_result(f"API request network error: {e}", page_title="API Network Error", hard=False)
    except Exception as e:
        print(f"[ERROR] [API] API request unexpected error: {e}")
        return error_result(f"API request error: {e}", page_title="API Error", hard=False)

    if api_resp.status_code in (401, 403):
        print(f"[ERROR] [API] API authorization error (HTTP {api_resp.status_code}).")
        return error_result(
            f"API authorization error (HTTP {api_resp.status_code})",
            page_title=f"HTTP {api_resp.status_code}",
            hard=True,
        )

    if api_resp.status_code >= 500:
        print(f"[ERROR] [API] API server error (HTTP {api_resp.status_code}).")
        return error_result(
            f"API server error (HTTP {api_resp.status_code})",
            page_title=f"HTTP {api_resp.status_code}",
            hard=False,
        )

    if api_resp.status_code != 200:
        print(f"[ERROR] [API] API unexpected HTTP status: {api_resp.status_code}.")
        return error_result(
            f"API unexpected status HTTP {api_resp.status_code}",
            page_title=f"HTTP {api_resp.status_code}",
            hard=False,
        )

    try:
        data = api_resp.json()
    except Exception as e:
        print(f"[ERROR] [API] Failed to parse API JSON: {e}")
        return error_result(f"API response is not valid JSON: {e}", page_title="JSON Error", hard=True)

    film_avail = data.get("filmAvailability")
    if film_avail is None or not isinstance(film_avail, dict):
        print("[ERROR] [API] API response missing 'filmAvailability' key (schema drift).")
        return error_result(
            "API response missing 'filmAvailability' key (schema drift)",
            page_title="Schema Drift",
            hard=True,
        )

    # Step 4: Evaluate availability
    eval_result = evaluate_availability(film_avail)
    status, found_avail, found_unavail, error_reason = eval_result
    earliest_starts_at = getattr(eval_result, "startsAt", None)

    title_match = re.search(r"<title>(.*?)</title>", resp.text, re.IGNORECASE)
    page_title = title_match.group(1).strip() if title_match else "SM Cinema"

    print(f"[CHECK] [API] Film ID: {film_avail.get('filmId')}")
    print(f"[CHECK] [API] Categories: {film_avail.get('categories')}")
    print(f"[CHECK] [API] Advance booking periods: {film_avail.get('advanceBookingPeriods')}")
    print(f"[CHECK] [API] Showtime attribute IDs: {film_avail.get('showtimeAttributeIds')}")

    if status == "ERROR":
        print(f"[ERROR] [API] {error_reason}")
        return error_result(error_reason, page_title=page_title, hard=True)

    if status == "AVAILABLE":
        if earliest_starts_at:
            dt = parse_iso_datetime(earliest_starts_at)
            if dt and dt > get_manila_now():
                fmt = dt.strftime("%B %d, %Y at %I:%M %p (PHT)")
                print(f"[CHECK] [API] Advance booking opens in the future: opens at {fmt}")
                print(f"[CHECK] Conclusion: AVAILABLE (opens at {fmt}) ✅")
            elif dt:
                fmt = dt.strftime("%B %d, %Y at %I:%M %p (PHT)")
                print(f"[CHECK] [API] Advance booking is active: open now (started {fmt})")
                print(f"[CHECK] Conclusion: AVAILABLE (open now) ✅")
            else:
                print(f"[CHECK] [API] Advance booking startsAt: {earliest_starts_at}")
                print("[CHECK] Conclusion: AVAILABLE ✅")
        else:
            print("[CHECK] Conclusion: AVAILABLE ✅")
    else:
        print("[CHECK] Conclusion: CONFIRMED UNAVAILABLE ❌")

    print(f"[CHECK] Availability signals found: {found_avail}")
    print(f"[CHECK] Unavailability signals found: {found_unavail}")

    return {
        "status": status,
        "available": (status == "AVAILABLE"),
        "signals_found": found_avail,
        "unavailable_signals": found_unavail,
        "page_title": page_title,
        "error_reason": None,
        "hard": False,
        "startsAt": earliest_starts_at,
        "starts_at": earliest_starts_at,
    }


def check_availability_api(retry_backoffs=None):
    """
    Check ticket availability directly via SM Cinema's internal JSON API.
    Retries up to 3 times on soft failures with growing backoff (roughly 0s, 30s, 90s).
    """
    # Small random jitter (0-60s) on GitHub Actions to desynchronize cron runs
    max_jitter = int(os.environ.get("INITIAL_DELAY_MAX", "60" if os.environ.get("GITHUB_ACTIONS") else "0"))
    if max_jitter > 0 and not os.environ.get("SKIP_INITIAL_DELAY"):
        jitter = random.uniform(0, max_jitter)
        print(f"[CHECK] Initial random jitter: sleeping {jitter:.1f}s...")
        time.sleep(jitter)

    if retry_backoffs is None:
        retry_backoffs = (0, 30, 90)

    session = requests.Session() if requests is not None else None
    last_result = None

    for attempt, delay in enumerate(retry_backoffs, start=1):
        if delay > 0:
            print(f"[CHECK] Soft failure on previous attempt. Retrying in {delay}s (attempt {attempt}/{len(retry_backoffs)})...")
            time.sleep(delay)
        else:
            print(f"[CHECK] Checking availability via API (attempt {attempt}/{len(retry_backoffs)})...")

        result = _check_availability_api_single(MOVIE_URL, session=session)

        if result["status"] != "ERROR":
            return result

        last_result = result
        if result.get("hard", False):
            print(f"[CHECK] Hard failure encountered ({result.get('error_reason')}). Aborting retries.")
            return result

    return last_result


def check_availability():
    """
    Main entry point for checking ticket availability.
    Defaults to lightweight JSON API check.
    Set USE_BROWSER_FALLBACK=1 to use headless Playwright browser check.
    """
    if os.environ.get("USE_BROWSER_FALLBACK", "").lower() in ("1", "true", "yes"):
        print("[CHECK] Mode: Browser fallback (Playwright)")
        return check_availability_browser()
    return check_availability_api()


# ── Discord Notification ───────────────────────────────────────────────────────

def build_discord_payload(result, is_test=False):
    """
    Build the formatted Discord embed payload.
    Uses startsAt to distinguish booking in the future ('opens at <time>') from active booking ('open now').
    """
    now_manila = get_manila_now()
    timestamp = now_manila.strftime("%B %d, %Y at %I:%M %p (PHT)")

    title = "🧪 [TEST] SM Cinema Ticket Bot Verification" if is_test else "🎟️ Avengers: Doomsday — Book Now!"
    header_content = (
        "🧪 **Test alert from SM Cinema Ticket Bot**"
        if is_test
        else (f"🚨 {MENTION} **TICKETS ARE NOW AVAILABLE!**" if MENTION else "🚨 **TICKETS ARE NOW AVAILABLE!**")
    )

    signals_text = ", ".join(result.get("signals_found", [])) or "Manual test trigger"

    description = (
        "This is a test notification confirming your Discord webhook configuration works!"
        if is_test
        else (
            "Tickets for **Avengers: Doomsday** have just become available "
            "on SM Cinema!\n\nHead over and book your seats before they sell out."
        )
    )

    timing_field = None
    starts_at_str = result.get("startsAt") or result.get("starts_at")
    if not is_test and starts_at_str:
        starts_dt = parse_iso_datetime(starts_at_str)
        if starts_dt:
            formatted_time = starts_dt.strftime("%B %d, %Y at %I:%M %p (PHT)")
            if starts_dt > now_manila:
                header_content = (
                    f"🚨 {MENTION} **ADVANCE BOOKING ANNOUNCED!**"
                    if MENTION
                    else "🚨 **ADVANCE BOOKING ANNOUNCED!**"
                )
                title = "🎟️ Avengers: Doomsday — Advance Booking Announced!"
                description = (
                    f"Advance booking for **Avengers: Doomsday** opens at **{formatted_time}** "
                    f"on SM Cinema!\n\nSet a reminder and be ready to book your seats once booking goes live."
                )
                timing_field = {
                    "name": "⏰ Booking Schedule",
                    "value": f"opens at {formatted_time}",
                    "inline": False,
                }
            else:
                description = (
                    f"Tickets for **Avengers: Doomsday** are open now on SM Cinema!\n\n"
                    f"Head over and book your seats before they sell out."
                )
                timing_field = {
                    "name": "⏰ Booking Schedule",
                    "value": f"open now (booking began {formatted_time})",
                    "inline": False,
                }

    fields = [
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
    ]

    if timing_field:
        fields.append(timing_field)

    fields.append(
        {
            "name": "📡 Signals Detected",
            "value": signals_text,
            "inline": False,
        }
    )

    return {
        "content": header_content,
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": 0x3498DB if is_test else 0xE40000,
                "fields": fields,
                "footer": {
                    "text": "SM Cinema Ticket Bot • Runs via GitHub Actions"
                },
                "thumbnail": {
                    "url": "https://upload.wikimedia.org/wikipedia/en/9/98/Avengers_Doomsday_poster.jpg"
                },
            }
        ],
    }


def send_discord_notification(result, is_test=False):
    """Send a formatted Discord embed notification via Webhook."""
    if not WEBHOOK_URL:
        print("[WARN] No DISCORD_WEBHOOK_URL set. Notification cannot be sent.")
        return False

    payload = build_discord_payload(result, is_test=is_test)

    starts_at_str = result.get("startsAt") or result.get("starts_at")
    if starts_at_str:
        starts_dt = parse_iso_datetime(starts_at_str)
        if starts_dt:
            formatted = starts_dt.strftime("%B %d, %Y at %I:%M %p (PHT)")
            if starts_dt > get_manila_now():
                print(f"[DISCORD] Notification: Advance booking opens at {formatted}")
            else:
                print(f"[DISCORD] Notification: Booking is open now (started {formatted})")


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
        if result.get("hard"):
            # Blocked, misconfigured, or no longer able to read the page: fail the
            # run so Actions shows red and GitHub emails, instead of a green tick
            # that is indistinguishable from "no tickets yet".
            print("[ACTION] Hard failure — exiting non-zero so this run shows as failed.")
            print("\n" + "=" * 60 + "\n")
            sys.exit(1)
        print("[ACTION] Transient failure — this run stays green.")
        print(f"\n{'='*60}\n")
        return

    # 2. Genuine page reading obtained
    previous_status = old_state.get("last_status", "unavailable")

    if status == "AVAILABLE":
        state["last_status"] = "available"
        if not state.get("notified", False):
            starts_at = result.get("startsAt") or result.get("starts_at")
            if starts_at:
                dt = parse_iso_datetime(starts_at)
                if dt and dt > get_manila_now():
                    fmt = dt.strftime("%B %d, %Y at %I:%M %p (PHT)")
                    print(f"\n[ACTION] 🎉 Advance booking announced (opens at {fmt})! Sending notification...")
                elif dt:
                    fmt = dt.strftime("%B %d, %Y at %I:%M %p (PHT)")
                    print(f"\n[ACTION] 🎉 Tickets confirmed open now (started {fmt})! Sending notification...")
                else:
                    print(f"\n[ACTION] 🎉 Tickets confirmed available (startsAt {starts_at})! Sending notification...")
            else:
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
