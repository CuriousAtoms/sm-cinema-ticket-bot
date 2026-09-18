"""
Unit tests for SM Cinema ticket availability checker and decision logic.
Uses Python standard library unittest.
"""

import os
import json
import tempfile
import unittest
from unittest.mock import patch

import checker


class TestEvaluateSignals(unittest.TestCase):
    """Unit tests covering the pure decision logic in evaluate_signals()."""

    def test_todays_real_page_data_is_unavailable(self):
        """Today's live page: 'coming soon' badge, nav buttons, no sessions -> UNAVAILABLE."""
        film_status = "coming soon"
        buttons = ["view showtimes", "add to watchlist", "add cinemas"]
        sessions = []
        cleaned_text = (
            "coming soon avengers: doomsdaytbc view showtimes add to watchlist "
            "runtime 2h 45m release date 16 december 2026 showtimes no cinemas selected"
        )

        status, found_avail, found_unavail, error_reason = checker.evaluate_signals(
            film_status, buttons, sessions, cleaned_text
        )

        self.assertEqual(status, "UNAVAILABLE")
        self.assertFalse(found_avail)
        self.assertTrue(any("coming soon" in u for u in found_unavail))
        self.assertIsNone(error_reason)

    def test_empty_string_sessions_do_not_count_as_available(self):
        """Regression test for FIX 1: Empty shells/whitespace sessions do not trigger availability."""
        film_status = "coming soon"
        buttons = ["view showtimes"]
        sessions = ["", "   ", "\n\t"]
        cleaned_text = "coming soon view showtimes"

        status, found_avail, found_unavail, _ = checker.evaluate_signals(
            film_status, buttons, sessions, cleaned_text
        )

        self.assertEqual(status, "UNAVAILABLE")
        self.assertFalse(found_avail)

    def test_sessions_without_digits_do_not_count_as_available(self):
        """Regression test for FIX 1: Session labels without time/digits are rejected."""
        film_status = "coming soon"
        buttons = []
        sessions = ["no showtimes available", "select time"]
        cleaned_text = "coming soon"

        status, found_avail, found_unavail, _ = checker.evaluate_signals(
            film_status, buttons, sessions, cleaned_text
        )

        self.assertEqual(status, "UNAVAILABLE")
        self.assertFalse(found_avail)

    def test_real_showtime_session_triggers_available(self):
        """Regression test for session detection: Strings with showtime digits indicate availability."""
        film_status = ""
        buttons = []
        sessions = ["1:30 pm", "4:45 pm", "8:00 pm"]
        cleaned_text = "select showtime"

        status, found_avail, found_unavail, error_reason = checker.evaluate_signals(
            film_status, buttons, sessions, cleaned_text
        )

        self.assertEqual(status, "AVAILABLE")
        self.assertTrue(any("active sessions: 3 detected" in a for a in found_avail))
        self.assertIsNone(error_reason)

    def test_zero_signals_returns_error_detection_drift(self):
        """Regression test for FIX 2: Zero availability and zero unavailability markers returns ERROR."""
        film_status = ""
        buttons = ["view showtimes", "add to watchlist"]  # non-booking buttons
        sessions = []
        cleaned_text = "some generic promotional text without any film status or signals"

        status, found_avail, found_unavail, error_reason = checker.evaluate_signals(
            film_status, buttons, sessions, cleaned_text
        )

        self.assertEqual(status, "ERROR")
        self.assertFalse(found_avail)
        self.assertFalse(found_unavail)
        self.assertIsNotNone(error_reason)
        self.assertIn("Detection drift", error_reason)

    def test_coming_soon_with_explicit_book_now_is_available(self):
        """Deliberate override: 'coming soon' badge with an explicit 'book now' CTA is AVAILABLE."""
        film_status = "coming soon"
        buttons = ["book now", "add to watchlist"]
        sessions = []
        cleaned_text = "coming soon book now"

        status, found_avail, found_unavail, error_reason = checker.evaluate_signals(
            film_status, buttons, sessions, cleaned_text
        )

        self.assertEqual(status, "AVAILABLE")
        self.assertTrue(any("book now" in a for a in found_avail))
        self.assertTrue(any("coming soon" in u for u in found_unavail))
        self.assertIsNone(error_reason)

    def test_now_showing_badge_is_available(self):
        """'now showing' status badge without unavailable signals is AVAILABLE."""
        film_status = "now showing"
        buttons = ["view showtimes"]
        sessions = []
        cleaned_text = "now showing in cinemas"

        status, found_avail, _, _ = checker.evaluate_signals(
            film_status, buttons, sessions, cleaned_text
        )

        self.assertEqual(status, "AVAILABLE")
        self.assertTrue(any("now showing" in a for a in found_avail))

    def test_advance_tickets_badge_is_available(self):
        """'advance tickets' status badge confirms pre-sales are active -> AVAILABLE."""
        film_status = "advance tickets"
        buttons = []
        sessions = []
        cleaned_text = "advance tickets open"

        status, found_avail, _, _ = checker.evaluate_signals(
            film_status, buttons, sessions, cleaned_text
        )

        self.assertEqual(status, "AVAILABLE")
        self.assertTrue(any("advance tickets" in a for a in found_avail))


class TestCloudflareChallengeDetection(unittest.TestCase):
    """Unit tests for check_cloudflare_challenge() covering all Cloudflare markers."""

    def test_cf_1020_attention_required_title(self):
        """Regression test for FIX 3: 'Attention Required! | Cloudflare' is detected."""
        self.assertTrue(
            checker.check_cloudflare_challenge("Attention Required! | Cloudflare", "Access denied")
        )

    def test_cf_just_a_moment_title(self):
        """'Just a moment...' challenge title is detected."""
        self.assertTrue(
            checker.check_cloudflare_challenge("Just a moment...", "Checking your browser")
        )

    def test_cf_turnstile_in_body(self):
        """Turnstile widget markers in body are detected."""
        self.assertTrue(
            checker.check_cloudflare_challenge("SM Cinema", "cf-turnstile-wrapper verify you are human")
        )

    def test_cf_challenge_running_in_body(self):
        """Cloudflare challenge-running marker in body is detected."""
        self.assertTrue(
            checker.check_cloudflare_challenge("SM Cinema", "challenge-running please wait")
        )

    def test_normal_page_is_not_flagged(self):
        """Normal SM Cinema page title and content does not trigger Cloudflare detection."""
        self.assertFalse(
            checker.check_cloudflare_challenge("SM Cinema", "Avengers: Doomsday Coming Soon")
        )


class TestStateManagementAndWorkflowSafety(unittest.TestCase):
    """Unit tests ensuring state.json stability and error safety in main()."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.test_state_file = os.path.join(self.tmp_dir.name, "test_state.json")
        checker.STATE_FILE = self.test_state_file

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_routine_unavailable_run_does_not_modify_state_file(self):
        """A routine run with status 'unavailable' does NOT rewrite state.json."""
        initial = {"notified": False, "last_status": "unavailable"}
        with open(self.test_state_file, "w", encoding="utf-8") as f:
            json.dump(initial, f)
        mtime_before = os.path.getmtime(self.test_state_file)

        with patch.object(checker, "check_availability", return_value={
            "status": "UNAVAILABLE",
            "available": False,
            "signals_found": [],
            "unavailable_signals": ["coming soon"],
            "page_title": "SM Cinema",
            "error_reason": None,
        }):
            checker.main([])

        mtime_after = os.path.getmtime(self.test_state_file)
        self.assertEqual(mtime_before, mtime_after)
        with open(self.test_state_file, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), initial)

    def test_error_status_preserves_notified_flag_and_does_not_save(self):
        """Scrape ERROR does NOT clear notified=True and does NOT modify state.json."""
        initial = {"notified": True, "last_status": "available"}
        with open(self.test_state_file, "w", encoding="utf-8") as f:
            json.dump(initial, f)
        mtime_before = os.path.getmtime(self.test_state_file)

        with patch.object(checker, "check_availability", return_value={
            "status": "ERROR",
            "available": False,
            "signals_found": [],
            "unavailable_signals": [],
            "page_title": "Attention Required! | Cloudflare",
            "error_reason": "Cloudflare challenge block",
        }):
            checker.main([])

        mtime_after = os.path.getmtime(self.test_state_file)
        self.assertEqual(mtime_before, mtime_after)
        with open(self.test_state_file, "r", encoding="utf-8") as f:
            after = json.load(f)
        self.assertTrue(after["notified"])
        self.assertEqual(after["last_status"], "available")

    def test_notified_resets_only_when_previously_available_and_now_unavailable(self):
        """notified=True resets to False ONLY when transitioning available -> unavailable."""
        initial = {"notified": True, "last_status": "available"}
        with open(self.test_state_file, "w", encoding="utf-8") as f:
            json.dump(initial, f)

        with patch.object(checker, "check_availability", return_value={
            "status": "UNAVAILABLE",
            "available": False,
            "signals_found": [],
            "unavailable_signals": ["coming soon"],
            "page_title": "SM Cinema",
            "error_reason": None,
        }):
            checker.main([])

        with open(self.test_state_file, "r", encoding="utf-8") as f:
            after = json.load(f)
        self.assertFalse(after["notified"])
        self.assertEqual(after["last_status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
