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

DEFAULT_MOVIE_URL = "https://www.smcinema.com/films/Avengers-Doomsday/HO00001619"

# Poster for DEFAULT_MOVIE_URL's film, shown as the embed thumbnail.
# upload.wikimedia.org paths are derived from the MD5 of the file name, so they
# cannot be hand-written — the previous value used an invented hash directory and
# returned 404, which Discord swallows silently by rendering no thumbnail at all.
# Resolve a replacement through the API rather than guessing:
#   https://en.wikipedia.org/w/api.php?action=query&titles=File:<name>&prop=imageinfo&iiprop=url&format=json
DEFAULT_POSTER_URL = (
    "https://upload.wikimedia.org/wikipedia/en/e/ee/Avengers_Doomsday_poster.jpg"
)

# os.environ.get(key, default) only falls back when the key is ABSENT. GitHub
# Actions always defines `MOVIE_URL: ${{ secrets.MOVIE_URL }}`, and an unset
# secret expands to an empty string — so the documented default would never
# apply on Actions. Treat blank as unset.
MOVIE_URL = (os.environ.get("MOVIE_URL") or "").strip() or DEFAULT_MOVIE_URL
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
    """Load the state file that tracks the notification phase and last status."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                last_status = data.get("last_status", "unavailable")

                # Migration from legacy boolean 'notified'
                if "notify_phase" in data:
                    notify_phase = data["notify_phase"]
                elif "notified" in data:
                    notify_phase = "open" if data["notified"] else "none"
                else:
                    notify_phase = "none"

                if notify_phase not in ("none", "announced", "open"):
                    notify_phase = "none"

                # Absent on state files written before film tracking existed.
                film_id = data.get("film_id")
                if not isinstance(film_id, str) or not film_id.strip():
                    film_id = None

                return {
                    "notify_phase": notify_phase,
                    "last_status": last_status,
                    "film_id": film_id,
                }
        except Exception as e:
            print(f"[STATE] Error loading {STATE_FILE}: {e}. Initializing fresh state.")
    return {"notify_phase": "none", "last_status": "unavailable", "film_id": None}


def save_state(state):
    """Save the state back to disk."""
    try:
        payload = {
            "notify_phase": state.get("notify_phase", "none"),
            "last_status": state.get("last_status", "unavailable"),
        }
        # Records WHICH film the phase above refers to, so repointing MOVIE_URL
        # at another film cannot inherit its predecessor's "already notified".
        film_id = state.get("film_id")
        if isinstance(film_id, str) and film_id.strip():
            payload["film_id"] = film_id.strip()
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
    except Exception as e:
        print(f"[STATE] Error saving {STATE_FILE}: {e}")


def determine_booking_phase(result):
    """
    Determine the booking phase for an AVAILABLE result:
      - 'announced': Advance booking announced with startsAt in the future.
      - 'open': Booking is open now (startsAt in the past/now, or no startsAt specified).
    """
    starts_at = None
    if hasattr(result, "get"):
        starts_at = result.get("startsAt") or result.get("starts_at")
    if not starts_at and hasattr(result, "startsAt"):
        starts_at = getattr(result, "startsAt", None)
    if not starts_at and hasattr(result, "starts_at"):
        starts_at = getattr(result, "starts_at", None)

    if starts_at:
        dt = parse_iso_datetime(starts_at)
        if dt is not None and dt > get_manila_now():
            return "announced"
    return "open"


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


# SM Cinema film IDs are 'HO' followed by 8 digits.
FILM_ID_RE = re.compile(r"^HO\d{8}$", re.IGNORECASE)


def looks_like_film_id(film_id: str) -> bool:
    """True if film_id has SM Cinema's HO######## shape."""
    return bool(FILM_ID_RE.match((film_id or "").strip()))


def fetch_film_title(film_id, gas_token, session, timeout=15):
    """
    Resolve a film ID to its human-readable title via /ocapi/v1/films/<id>.

    smcinema.com is a client-rendered SPA: every URL returns HTTP 200 with no
    <title>, so nothing else in the run can tell you WHICH movie was checked.
    Best-effort only — returns None on any failure rather than failing the run.
    """
    try:
        resp = session.get(
            f"https://digital-api.smcinema.com/ocapi/v1/films/{film_id}",
            headers={
                "Authorization": f"Bearer {gas_token}",
                "Accept": "application/json",
                "User-Agent": DEFAULT_USER_AGENT,
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        title = resp.json().get("film", {}).get("title", {}).get("text")
        return title.strip() if isinstance(title, str) and title.strip() else None
    except Exception:
        return None


def resolve_film_title(movie_url=None, session=None):
    """
    Resolve a film's title on its own, without running an availability check.

    Walks the same two hops the real check does (page HTML -> gasToken ->
    /ocapi/v1/films/<id>) so a simulated alert names the film exactly as a
    genuine one would. Best-effort: returns None on any failure, because a
    preview is still worth sending without the title.
    """
    target = movie_url or MOVIE_URL
    film_id = extract_film_id(target)
    if not film_id or not looks_like_film_id(film_id):
        return None

    # `requests` is only needed to build a session of our own — an injected one
    # works without it.
    if session is None:
        if requests is None:
            return None
        session = requests.Session()
    sess = session
    try:
        resp = sess.get(
            target,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=15,
        )
    except Exception:
        return None

    if getattr(resp, "status_code", None) != 200:
        return None

    gas_token = extract_gas_token(resp.text)
    if not gas_token:
        return None

    return fetch_film_title(film_id, gas_token, sess)


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
         (provisional: the API check then looks for real sessions, see
         evaluate_sessions — categories alone missed a real on-sale)
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


# ── Session Check (what categories can miss) ───────────────────────────────────

# The showtime endpoints refuse more than five cinemas per request (HTTP 400
# "Cannot filter by more than 5 site identifiers."), so a sweep of every
# cinema has to go out in batches of this size.
SITES_PER_REQUEST = 5

# Cinemas named in a session signal before the rest collapse into "+N more".
# Signals land in a Discord embed field, and Discord rejects the whole message
# when a field exceeds 1024 characters — so the list must stay bounded.
MAX_CINEMAS_NAMED = 5


def _ocapi_list(session, gas_token, path, key, params=None):
    """
    GET one digital-api endpoint and return the list stored under `key`.

    Returns (items, None), reading 204 No Content as an empty list, or
    (None, error_result). Any 4xx except a 429 throttle is hard: the API
    refused the request itself — a rejected token, or a query shape it no
    longer accepts — and no retry can fix that.
    """
    url = f"https://digital-api.smcinema.com/ocapi/v1/{path}"
    headers = {
        "Authorization": f"Bearer {gas_token}",
        "Accept": "application/json",
        "User-Agent": DEFAULT_USER_AGENT,
    }
    try:
        resp = session.get(url, params=params, headers=headers, timeout=15)
    except (RequestTimeout, RequestConnectionError) as e:
        return None, error_result(f"{path} network error: {e}", page_title="API Network Error", hard=False)
    except Exception as e:
        return None, error_result(f"{path} request error: {e}", page_title="API Error", hard=False)

    status = resp.status_code
    if status == 204:
        return [], None
    if status != 200:
        # Vista answers errors with problem+json, whose `detail` names the
        # actual complaint, e.g. "Cannot filter by more than 5 site identifiers."
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        reason = f"{path} returned HTTP {status}"
        if isinstance(detail, str) and detail.strip():
            reason += f": {detail.strip()}"
        hard = 400 <= status < 500 and status != 429
        return None, error_result(reason, page_title=f"HTTP {status}", hard=hard)

    try:
        data = resp.json()
    except Exception as e:
        return None, error_result(f"{path} response is not valid JSON: {e}", page_title="JSON Error", hard=True)

    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None, error_result(
            f"{path} response missing '{key}' list (schema drift)",
            page_title="Schema Drift",
            hard=True,
        )
    return items, None


def fetch_film_sessions(film_id, gas_token, session):
    """
    Sweep every SM Cinema site for the film's scheduled sessions.

    Mirrors what the film page does once a visitor picks a cinema:
    film-screening-dates says where and when the film plays, then
    showtimes/availability says whether those sessions still have seats. The
    page only ever asks about the cinemas a visitor selected, so nothing on it
    reveals a session at any other cinema.

    Returns ((screenings, seats, site_names), None) or (None, error_result):
      screenings  {site_id: {business_date, ...}}
      seats       showtimeAvailabilities for the cinemas that have screenings
      site_names  {site_id: cinema name}
    """
    sites, err = _ocapi_list(session, gas_token, "sites", "sites")
    if err:
        return None, err

    site_names = {}
    for site in sites:
        if isinstance(site, dict) and site.get("id"):
            name = site.get("name")
            text = name.get("text") if isinstance(name, dict) else name
            site_names[str(site["id"])] = text if isinstance(text, str) and text else str(site["id"])
    if not site_names:
        # With zero cinemas, "no sessions anywhere" would be vacuously true.
        return None, error_result(
            "sites returned no cinemas, so sessions cannot be checked",
            page_title="Schema Drift",
            hard=True,
        )

    site_ids = sorted(site_names)
    screenings = {}
    for i in range(0, len(site_ids), SITES_PER_REQUEST):
        days, err = _ocapi_list(
            session, gas_token, "film-screening-dates", "filmScreeningDates",
            params={"filmIds": film_id, "siteIds": site_ids[i:i + SITES_PER_REQUEST]},
        )
        if err:
            return None, err
        found_before = len(screenings)
        for day in days:
            if not isinstance(day, dict):
                continue
            date = day.get("businessDate")
            for screening in day.get("filmScreenings") or []:
                if not isinstance(screening, dict):
                    continue
                for site in screening.get("sites") or []:
                    if isinstance(site, dict) and site.get("siteId"):
                        dates = screenings.setdefault(str(site["siteId"]), set())
                        if isinstance(date, str):
                            dates.add(date)
        if days and len(screenings) == found_before:
            # The API says the film screens somewhere in this batch, but not in
            # a shape that says where. Guessing "nowhere" is the silent miss
            # this sweep exists to prevent.
            return None, error_result(
                "film-screening-dates listed screening days without readable cinemas (schema drift)",
                page_title="Schema Drift",
                hard=True,
            )

    seats = []
    screened_sites = sorted(screenings)
    for i in range(0, len(screened_sites), SITES_PER_REQUEST):
        items, err = _ocapi_list(
            session, gas_token, "showtimes/availability", "showtimeAvailabilities",
            params={"filmIds": film_id, "siteIds": screened_sites[i:i + SITES_PER_REQUEST]},
        )
        if err:
            return None, err
        seats.extend(item for item in items if isinstance(item, dict))

    return (screenings, seats, site_names), None


def _is_sold_out(seat_info):
    """Sold out if either field says so; a session with missing data counts as having seats."""
    if seat_info.get("isSoldOut") is True:
        return True
    summary = seat_info.get("seatSummary")
    available = summary.get("availableCount") if isinstance(summary, dict) else None
    return isinstance(available, int) and available <= 0


def _describe_screenings(screenings, site_names):
    """e.g. '2 cinemas (SM Mall of Asia, SM Megamall), from December 16, 2026'."""
    names = sorted(site_names.get(site_id, site_id) for site_id in screenings)
    shown = ", ".join(names[:MAX_CINEMAS_NAMED])
    if len(names) > MAX_CINEMAS_NAMED:
        shown += f" +{len(names) - MAX_CINEMAS_NAMED} more"
    noun = "cinema" if len(names) == 1 else "cinemas"
    text = f"{len(names)} {noun} ({shown})"

    dates = sorted(date for site_dates in screenings.values() for date in site_dates)
    if dates:
        try:
            earliest = datetime.strptime(dates[0], "%Y-%m-%d").strftime("%B %d, %Y")
        except ValueError:
            earliest = dates[0]
        text += f", from {earliest}"
    return text


def evaluate_sessions(screenings, seats, site_names):
    """
    Decide availability from the film's actual sessions.

    Returns (status, found_available, found_unavailable):
      no screening at any cinema              -> UNAVAILABLE
      any session with a seat left            -> AVAILABLE
      screenings, but no seat data for them   -> AVAILABLE (bias toward alerting)
      every session sold out                  -> UNAVAILABLE

    Sold out counts as unavailable on purpose. An alert for seats nobody can buy
    would also spend the single "open" alert, leaving nothing to fire when
    bookable sessions finally appear.
    """
    if not screenings:
        return "UNAVAILABLE", [], [f"no sessions at any of {len(site_names)} cinemas"]

    where = _describe_screenings(screenings, site_names)
    if not seats:
        return "AVAILABLE", [f"sessions scheduled at {where} (seat availability unknown)"], []

    bookable = sum(1 for seat_info in seats if not _is_sold_out(seat_info))
    if bookable:
        return "AVAILABLE", [f"{bookable} of {len(seats)} sessions bookable at {where}"], []
    return "UNAVAILABLE", [], [f"all {len(seats)} sessions sold out at {where}"]


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

    # The page fetch below cannot catch a bad URL — smcinema.com is a SPA that
    # answers 200 for any path, including invented ones. So the film ID is the
    # only thing worth validating, and it has to happen here.
    if not looks_like_film_id(film_id):
        print(f"[ERROR] [API] MOVIE_URL ends in '{film_id}', which is not an SM Cinema film ID.")
        print("[ERROR] [API] MOVIE_URL must end with the film ID, e.g.")
        print("[ERROR] [API]   https://www.smcinema.com/films/Fall-2-Deadpoint/HO00001625")
        return error_result(
            f"MOVIE_URL must end with a film ID like HO00001625, got '{film_id}'",
            page_title="Config Error",
            hard=True,
        )

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

    if api_resp.status_code == 404:
        # The film ID itself is wrong or retired. Retrying cannot fix that, and a
        # green run here would mean the bot checks nothing at all until someone
        # notices — exactly the silent blindness the hard/soft split exists to stop.
        print(f"[ERROR] [API] Film not found (HTTP 404) — MOVIE_URL points at film ID '{film_id}', which the API does not know.")
        return error_result(
            f"API film not found (HTTP 404) for film ID '{film_id}' — check MOVIE_URL",
            page_title="HTTP 404",
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

    film_title = fetch_film_title(film_id, gas_token, session)
    print(f"[CHECK] [API] Movie: {film_title or '(title unavailable)'}")
    print(f"[CHECK] [API] Film ID: {film_avail.get('filmId')}")
    print(f"[CHECK] [API] Categories: {film_avail.get('categories')}")
    print(f"[CHECK] [API] Advance booking periods: {film_avail.get('advanceBookingPeriods')}")
    print(f"[CHECK] [API] Showtime attribute IDs: {film_avail.get('showtimeAttributeIds')}")

    if status == "ERROR":
        print(f"[ERROR] [API] {error_reason}")
        return error_result(error_reason, page_title=page_title, hard=True)

    # Step 5: ['ComingSoon'] is not proof there is nothing to buy. Avengers:
    # Doomsday (Infinity Vision) had 66 sessions on sale at three cinemas while
    # this endpoint still said ['ComingSoon'] with no advance booking period —
    # film-wide and for each of those cinemas. So before calling a film
    # unavailable, look for the sessions themselves.
    if status == "UNAVAILABLE":
        print("[CHECK] [API] Categories say unavailable; checking every cinema for sessions...")
        sessions, session_error = fetch_film_sessions(film_id, gas_token, session)
        if session_error:
            print(f"[ERROR] [API] Session check failed: {session_error['error_reason']}")
            return session_error
        status, session_avail, session_unavail = evaluate_sessions(*sessions)
        found_avail = found_avail + session_avail
        found_unavail = found_unavail + session_unavail
        print(f"[CHECK] [API] Sessions: {'; '.join(session_avail + session_unavail)}")

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
        "film_title": film_title,
        "film_id": film_id,
    }


def list_films(session=None):
    """
    Print every film SM Cinema currently knows about, with its film ID.

    This is the discovery step for MOVIE_URL: the site is a SPA, so there is no
    page you can read a film ID off of. Returns a list of (film_id, title).
    """
    if session is None:
        if requests is None:
            print("[ERROR] 'requests' library not installed.")
            return []
        session = requests.Session()

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        resp = session.get("https://www.smcinema.com/movies", headers=headers, timeout=15)
    except Exception as e:
        print(f"[ERROR] Could not reach smcinema.com: {e}")
        return []

    gas_token = extract_gas_token(resp.text)
    if not gas_token:
        print("[ERROR] Could not extract gasToken from smcinema.com.")
        return []

    try:
        api_resp = session.get(
            "https://digital-api.smcinema.com/ocapi/v1/films",
            headers={
                "Authorization": f"Bearer {gas_token}",
                "Accept": "application/json",
                "User-Agent": DEFAULT_USER_AGENT,
            },
            timeout=15,
        )
    except Exception as e:
        print(f"[ERROR] Film list request failed: {e}")
        return []

    if api_resp.status_code != 200:
        print(f"[ERROR] Film list returned HTTP {api_resp.status_code}.")
        return []

    try:
        films = api_resp.json().get("films", [])
    except Exception as e:
        print(f"[ERROR] Film list is not valid JSON: {e}")
        return []

    rows = []
    for film in films:
        if not isinstance(film, dict):
            continue
        film_id = film.get("id")
        title = (film.get("title") or {}).get("text")
        if film_id and title:
            rows.append((film_id, title))

    rows.sort(key=lambda r: r[1].lower())

    print(f"{len(rows)} films currently listed on SM Cinema:\n")
    for film_id, title in rows:
        print(f"  {film_id}  {title}")
    if rows:
        example_id, example_title = rows[0]
        slug = re.sub(r"[^A-Za-z0-9]+", "-", example_title).strip("-")
        print(f"\nSet MOVIE_URL to the film page URL ending in the ID, e.g.")
        print(f"  https://www.smcinema.com/films/{slug}/{example_id}")
    return rows


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

    # Whatever film MOVIE_URL points at — not always Avengers: Doomsday.
    movie_name = result.get("film_title") or "this movie"

    title = "🧪 [TEST] SM Cinema Ticket Bot Verification" if is_test else f"🎟️ {movie_name} — Book Now!"
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
            f"Tickets for **{movie_name}** have just become available "
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
                title = f"🎟️ {movie_name} — Advance Booking Announced!"
                description = (
                    f"Advance booking for **{movie_name}** opens at **{formatted_time}** "
                    f"on SM Cinema!\n\nSet a reminder and be ready to book your seats once booking goes live."
                )
                timing_field = {
                    "name": "⏰ Booking Schedule",
                    "value": f"opens at {formatted_time}",
                    "inline": False,
                }
            else:
                description = (
                    f"Tickets for **{movie_name}** are open now on SM Cinema!\n\n"
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
            "value": movie_name,
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

    embed = {
        "title": title,
        "description": description,
        "color": 0x3498DB if is_test else 0xE40000,
        "fields": fields,
        "footer": {
            "text": "SM Cinema Ticket Bot • Runs via GitHub Actions"
        },
    }

    # Only attach the Avengers poster when that is actually the film being
    # watched — otherwise the alert illustrates the wrong movie.
    #
    # Compare film IDs rather than whole URLs. MOVIE_URL is typed by hand into a
    # secret and only its trailing film ID is meaningful to the rest of the bot,
    # so an equally valid spelling — a trailing slash, a different slug, other
    # casing — would fail an exact string match and silently drop the poster
    # from a real alert.
    watched_film_id = (result.get("film_id") or extract_film_id(MOVIE_URL) or "").strip().upper()
    if watched_film_id == extract_film_id(DEFAULT_MOVIE_URL).upper():
        embed["thumbnail"] = {"url": DEFAULT_POSTER_URL}

    return {
        "content": header_content,
        "embeds": [embed],
    }


# ── Simulated Alerts ────────────────────────────────────────────────────

SIMULATED_PHASES = ("announced", "open")

SIMULATED_SIGNAL = "SIMULATED ALERT (--simulate-alert) — not a real detection"


def build_simulated_result(phase, film_title=None, now=None):
    """
    Build a result dict that renders as a genuine alert for the given phase.

    Fidelity is the whole point: this returns the same keys
    _check_availability_api_single() returns, so build_discord_payload() cannot
    tell a simulation from a real detection and the preview matches what will
    actually land in the channel. The one deliberate tell is the signals line,
    which always says the alert was simulated.

      'announced' -> startsAt a week out    -> "ADVANCE BOOKING ANNOUNCED!"
      'open'      -> startsAt two hours ago -> "TICKETS ARE NOW AVAILABLE!"
    """
    if phase not in SIMULATED_PHASES:
        raise ValueError(
            f"unknown simulated phase {phase!r} (expected one of {SIMULATED_PHASES})"
        )

    reference = now or get_manila_now()
    if phase == "announced":
        starts_at = (reference + timedelta(days=7)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
    else:
        starts_at = reference - timedelta(hours=2)

    starts_at_iso = starts_at.isoformat()
    return {
        "status": "AVAILABLE",
        "available": True,
        "signals_found": [SIMULATED_SIGNAL],
        "unavailable_signals": [],
        "page_title": "SM Cinema",
        "error_reason": None,
        "hard": False,
        "startsAt": starts_at_iso,
        "starts_at": starts_at_iso,
        "film_title": film_title,
        "film_id": extract_film_id(MOVIE_URL),
    }


# ── Webhook Preflight Diagnostics ─────────────────────────────────────────────

WEBHOOK_URL_RE = re.compile(
    r"^https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/(\d+)/([\w-]+)$"
)


def describe_webhook_url(url):
    """
    Describe a webhook URL's SHAPE without revealing it.

    Safe to print in a public repository's Actions log: reports only lengths,
    structural checks and the last 4 characters of the (non-secret) webhook ID.
    The token is never shown. Returns a list of display lines.
    """
    raw = url or ""
    stripped = raw.strip().strip('"').strip("'")
    lines = [f"total length: {len(raw)} chars"]

    if not stripped:
        lines.append("EMPTY — the secret is unset or blank")
        return lines

    if raw != stripped:
        lines.append("WARNING: value has surrounding whitespace or quotes — strip them")

    match = WEBHOOK_URL_RE.match(stripped)
    if not match:
        lines.append("does NOT match https://discord.com/api/webhooks/<id>/<token>")
        if "discord" not in stripped.lower():
            lines.append("HINT: this does not look like a Discord URL at all")
        elif "/webhooks/" not in stripped:
            lines.append("HINT: missing the '/api/webhooks/' path segment")
        else:
            lines.append("HINT: likely truncated, or the token segment is missing")
        return lines

    webhook_id, token = match.group(1), match.group(2)
    lines.append(f"webhook id: {len(webhook_id)} digits, ends ...{webhook_id[-4:]}")
    lines.append(f"token: {len(token)} chars (not shown)")

    if not (17 <= len(webhook_id) <= 20):
        lines.append(f"WARNING: webhook id should be 17-20 digits, got {len(webhook_id)}")
    if len(token) < 60:
        lines.append(f"WARNING: token looks truncated ({len(token)} chars, expected ~68)")
    if len(webhook_id) >= 17 and len(token) >= 60:
        lines.append("shape looks correct")
    return lines


def verify_webhook(url=None, session=None):
    """
    Preflight the webhook with GET before posting anything.

    Discord's GET /webhooks/<id>/<token> returns the webhook object when it is
    live, so this cleanly separates "the webhook does not exist" from "the
    message payload was rejected". Returns True when the webhook is usable.
    """
    target = WEBHOOK_URL if url is None else url

    print("[DISCORD] Webhook preflight (no secret values are printed):")
    for line in describe_webhook_url(target):
        print(f"[DISCORD]   {line}")

    if not (target or "").strip():
        return False

    # `requests` is only needed to build a request of our own — an injected
    # session works without it. Checking the global first made the live check
    # untestable in any environment that has not installed it, which is exactly
    # what tests.yml is: it deliberately installs nothing, because the decision
    # logic is pure.
    if session is None:
        if requests is None:
            print("[DISCORD]   (skipping live check — 'requests' not installed)")
            return False
        session = requests

    sess = session
    try:
        resp = sess.get(target.strip(), timeout=10)
    except Exception as e:
        print(f"[DISCORD]   live check failed: {e}")
        return False

    if resp.status_code == 200:
        try:
            info = resp.json()
            print(f"[DISCORD]   ✅ webhook is live — name '{info.get('name')}', channel id {info.get('channel_id')}")
        except Exception:
            print("[DISCORD]   ✅ webhook is live")
        return True

    if resp.status_code == 404:
        print("[DISCORD]   ❌ Discord does not recognise this webhook (404 Unknown Webhook).")
        print("[DISCORD]      The webhook was deleted, or the URL stored in the secret is wrong.")
        print("[DISCORD]      Recreate it: channel Settings -> Integrations -> Webhooks -> New Webhook,")
        print("[DISCORD]      use 'Copy Webhook URL', and paste the whole thing into DISCORD_WEBHOOK_URL.")
        return False

    if resp.status_code in (401, 403):
        print(f"[DISCORD]   ❌ Webhook token rejected (HTTP {resp.status_code}) — re-copy the full URL.")
        return False

    print(f"[DISCORD]   ❌ Unexpected response HTTP {resp.status_code}")
    return False


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
                # Say WHY in the same log entry, so a failing scheduled run is
                # self-diagnosing instead of just repeating the same 404 forever.
                if response.status_code in (401, 403, 404):
                    for line in describe_webhook_url(WEBHOOK_URL):
                        print(f"[DISCORD]   {line}")
                    if response.status_code == 404:
                        print("[DISCORD]   -> Discord does not recognise this webhook. Recreate it in the")
                        print("[DISCORD]      channel (Integrations -> Webhooks -> New Webhook), then update")
                        print("[DISCORD]      the DISCORD_WEBHOOK_URL secret with the full 'Copy Webhook URL'.")
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


def simulate_alert(phases, session=None):
    """
    Fire real-looking availability alerts into Discord without a real detection.

    Deliberately never loads or writes state.json. A preview that advanced
    notify_phase would burn the very transition the genuine alert depends on,
    and the real "tickets are open" message would then never be sent.

    Returns True only if every requested alert was accepted by Discord.
    """
    print("[SIMULATE] Verifying Discord webhook configuration...")
    if not verify_webhook(session=session):
        print("\n[SIMULATE] ❌ Webhook is not usable — nothing sent. Fix the above, then re-run.")
        return False

    film_title = resolve_film_title(session=session)
    # Kept out of the f-string expression: a backslash escape inside one is a
    # syntax error before Python 3.12, and the workflows pin 3.11.
    title_label = film_title or 'unknown (the alert will say "this movie")'
    print(f"\n[SIMULATE] Film: {title_label}")
    print(f"[SIMULATE] Mention: {MENTION or '(none — no one will be pinged)'}")
    print("[SIMULATE] state.json is neither read nor written by a simulation.")

    all_sent = True
    for index, phase in enumerate(phases):
        if index:
            # Discord orders by arrival; a beat apart keeps the two embeds in
            # the sequence a real run would have produced them.
            time.sleep(2)
        print(f"\n[SIMULATE] Sending '{phase}' alert...")
        result = build_simulated_result(phase, film_title=film_title)
        if not send_discord_notification(result):
            all_sent = False

    return all_sent


# ── Main Entry Point ───────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(description="SM Cinema Ticket Availability Checker")
    parser.add_argument(
        "--test-discord",
        action="store_true",
        help="Send a test notification to Discord Webhook and exit",
    )
    parser.add_argument(
        "--list-films",
        action="store_true",
        help="List every film currently on SM Cinema with its film ID, and exit",
    )
    parser.add_argument(
        "--simulate-alert",
        choices=("announced", "open", "both"),
        help=(
            "Send a real-looking availability alert to Discord without a real "
            "detection, then exit. Renders identically to the genuine alert, "
            "MENTION included, so it pings exactly as the real one would. "
            "Never reads or writes state.json."
        ),
    )
    args = parser.parse_args(argv)

    if args.list_films:
        list_films()
        return

    now = get_manila_now().strftime("%Y-%m-%d %H:%M:%S PHT")

    print(f"\n{'='*60}")
    print(f"  SM Cinema Checker — {now}")
    print(f"{'='*60}\n")

    if args.test_discord:
        print("[TEST] Verifying Discord webhook configuration...")
        if not verify_webhook():
            print("\n[TEST] ❌ Webhook is not usable — not sending. Fix the above, then re-run.")
            print(f"\n{'='*60}\n")
            sys.exit(1)
        print("\n[TEST] Sending test notification to Discord...")
        send_discord_notification(
            {"signals_found": ["CLI --test-discord trigger"]},
            is_test=True,
        )
        return

    if args.simulate_alert:
        phases = (
            SIMULATED_PHASES
            if args.simulate_alert == "both"
            else (args.simulate_alert,)
        )
        print(f"[SIMULATE] Simulating alert phase(s): {', '.join(phases)}")
        print("[SIMULATE] These are previews — tickets have NOT actually been detected.")
        if not simulate_alert(phases):
            print(f"\n{'='*60}\n")
            sys.exit(1)
        print(f"\n{'='*60}\n")
        return

    state = load_state()
    old_state = dict(state)

    print(f"[STATE] Current status: {state.get('last_status')}")
    print(f"[STATE] Notification phase: {state.get('notify_phase')}")

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

    # A phase describes one film. If MOVIE_URL now points somewhere else, the
    # stored phase belongs to the previous film — carrying it over would mark
    # the new film as "already notified" and silently swallow its alert.
    current_film_id = result.get("film_id") or extract_film_id(MOVIE_URL)
    previous_film_id = old_state.get("film_id")
    if current_film_id:
        state["film_id"] = current_film_id
        if previous_film_id and previous_film_id != current_film_id:
            print(f"[STATE] Film changed ({previous_film_id} -> {current_film_id}). "
                  f"Resetting notification phase for the new film.")
            old_state = dict(old_state)
            old_state["notify_phase"] = "none"
            state["notify_phase"] = "none"
            previous_status = "unavailable"

    if status == "AVAILABLE":
        state["last_status"] = "available"
        stored_phase = old_state.get("notify_phase", "none")
        current_phase = determine_booking_phase(result)

        should_notify = (
            (stored_phase == "none" and current_phase in ("announced", "open"))
            or (stored_phase == "announced" and current_phase == "open")
        )

        if should_notify:
            starts_at = result.get("startsAt") or result.get("starts_at")
            if current_phase == "announced":
                dt = parse_iso_datetime(starts_at)
                fmt = dt.strftime("%B %d, %Y at %I:%M %p (PHT)") if dt else starts_at
                print(f"\n[ACTION] 🎉 Advance booking announced (opens at {fmt})! Sending notification...")
            else:
                if starts_at:
                    dt = parse_iso_datetime(starts_at)
                    fmt = dt.strftime("%B %d, %Y at %I:%M %p (PHT)") if dt else starts_at
                    print(f"\n[ACTION] 🎉 Tickets confirmed open now (started {fmt})! Sending notification...")
                else:
                    print("\n[ACTION] 🎉 Tickets confirmed available (open now)! Sending notification...")
            notified = send_discord_notification(result)
            # Only advance the phase once the alert has actually landed. Advancing
            # it when no webhook is configured would commit "already notified" back
            # to the repo and permanently suppress the alert — including after the
            # webhook is later fixed. A failed send must stay retryable.
            if notified:
                state["notify_phase"] = current_phase
        else:
            print(f"\n[ACTION] Tickets available ({current_phase}), but notification already sent ({stored_phase}). Skipping.")
    else:  # UNAVAILABLE
        state["last_status"] = "unavailable"
        print("\n[ACTION] Confirmed: No tickets available yet.")
        # Only reset notify_phase if tickets were PREVIOUSLY confirmed available and have now disappeared
        if old_state.get("notify_phase") != "none" and previous_status == "available":
            state["notify_phase"] = "none"
            print("[STATE] Reset notify_phase: Tickets were previously available but are now confirmed unavailable.")

    # 3. Only persist state to disk if state actually changed
    if (
        state.get("notify_phase") != old_state.get("notify_phase")
        or state.get("last_status") != old_state.get("last_status")
        or state.get("film_id") != old_state.get("film_id")
    ):
        save_state(state)
        print(f"\n[STATE] Meaningful state change detected. Saved to {STATE_FILE}: {state}")
    else:
        print(f"\n[STATE] No state change ({state.get('last_status')}, notify_phase={state.get('notify_phase')}). Zero file modifications.")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
