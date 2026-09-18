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
        self._original_state_file = checker.STATE_FILE
        checker.STATE_FILE = self.test_state_file

    def tearDown(self):
        checker.STATE_FILE = self._original_state_file
        self.tmp_dir.cleanup()

    def test_routine_unavailable_run_does_not_modify_state_file(self):
        """A routine run with status 'unavailable' does NOT rewrite state.json."""
        initial = {"notify_phase": "none", "last_status": "unavailable"}
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

    def test_hard_error_preserves_state_and_fails_the_run(self):
        """A hard ERROR (Cloudflare block) keeps state AND exits non-zero so Actions goes red."""
        initial = {"notify_phase": "open", "last_status": "available"}
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
            "hard": True,
        }):
            with self.assertRaises(SystemExit) as ctx:
                checker.main([])

        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(mtime_before, os.path.getmtime(self.test_state_file))
        with open(self.test_state_file, "r", encoding="utf-8") as f:
            after = json.load(f)
        self.assertEqual(after["notify_phase"], "open")
        self.assertEqual(after["last_status"], "available")

    def test_soft_error_preserves_state_and_keeps_the_run_green(self):
        """A transient ERROR (timeout) keeps state and must NOT fail the run."""
        initial = {"notify_phase": "open", "last_status": "available"}
        with open(self.test_state_file, "w", encoding="utf-8") as f:
            json.dump(initial, f)
        mtime_before = os.path.getmtime(self.test_state_file)

        with patch.object(checker, "check_availability", return_value={
            "status": "ERROR",
            "available": False,
            "signals_found": [],
            "unavailable_signals": [],
            "page_title": "Timeout",
            "error_reason": "Navigation timeout",
            "hard": False,
        }):
            checker.main([])  # must not raise SystemExit

        self.assertEqual(mtime_before, os.path.getmtime(self.test_state_file))
        with open(self.test_state_file, "r", encoding="utf-8") as f:
            after = json.load(f)
        self.assertEqual(after["notify_phase"], "open")
        self.assertEqual(after["last_status"], "available")

    def test_notify_phase_resets_only_when_previously_available_and_now_unavailable(self):
        """notify_phase resets to 'none' ONLY when transitioning available -> unavailable."""
        initial = {"notify_phase": "open", "last_status": "available"}
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
        self.assertEqual(after["notify_phase"], "none")
        self.assertEqual(after["last_status"], "unavailable")

    def test_legacy_state_migration(self):
        """Legacy boolean 'notified' is migrated correctly on load, and save drops 'notified'."""
        # Case 1: notified=True -> notify_phase="open"
        with open(self.test_state_file, "w", encoding="utf-8") as f:
            json.dump({"notified": True, "last_status": "available"}, f)
        loaded = checker.load_state()
        self.assertEqual(loaded["notify_phase"], "open")
        self.assertEqual(loaded["last_status"], "available")

        # Case 2: notified=False -> notify_phase="none"
        with open(self.test_state_file, "w", encoding="utf-8") as f:
            json.dump({"notified": False, "last_status": "unavailable"}, f)
        loaded = checker.load_state()
        self.assertEqual(loaded["notify_phase"], "none")
        self.assertEqual(loaded["last_status"], "unavailable")

        # Case 3: Invalid notify_phase defaults to "none"
        with open(self.test_state_file, "w", encoding="utf-8") as f:
            json.dump({"notify_phase": "invalid_value", "last_status": "unavailable"}, f)
        loaded = checker.load_state()
        self.assertEqual(loaded["notify_phase"], "none")

        # Case 4: save_state only writes notify_phase and last_status
        checker.save_state({"notify_phase": "announced", "last_status": "available", "notified": True, "extra": 123})
        with open(self.test_state_file, "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved, {"notify_phase": "announced", "last_status": "available"})
        self.assertNotIn("notified", saved)
        self.assertNotIn("extra", saved)

    def test_determine_booking_phase(self):
        """determine_booking_phase correctly classifies future startsAt vs past/none."""
        # Future startsAt -> 'announced'
        res_future = {"status": "AVAILABLE", "startsAt": "2099-12-01T15:00:00+08:00"}
        self.assertEqual(checker.determine_booking_phase(res_future), "announced")

        # Past startsAt -> 'open'
        res_past = {"status": "AVAILABLE", "startsAt": "2020-01-01T10:00:00+08:00"}
        self.assertEqual(checker.determine_booking_phase(res_past), "open")

        # No startsAt -> 'open'
        res_none = {"status": "AVAILABLE"}
        self.assertEqual(checker.determine_booking_phase(res_none), "open")

        # Invalid startsAt string -> 'open'
        res_invalid = {"status": "AVAILABLE", "startsAt": "not-a-datetime"}
        self.assertEqual(checker.determine_booking_phase(res_invalid), "open")

    def test_two_phase_full_sequence_announce_then_open_then_skip(self):
        """
        Critical regression test:
        1. Booking announced for future -> 1st notification sent, state='announced'.
        2. Booking opens (startsAt in past/now) -> 2nd notification sent, state='open'.
        3. Next run (still open) -> 0 notifications sent, zero file modifications.
        """
        initial = {"notify_phase": "none", "last_status": "unavailable"}
        with open(self.test_state_file, "w", encoding="utf-8") as f:
            json.dump(initial, f)

        res_announce = {
            "status": "AVAILABLE",
            "available": True,
            "signals_found": ["AdvanceBooking in categories"],
            "unavailable_signals": [],
            "startsAt": "2099-12-01T15:00:00+08:00",
        }
        res_open = {
            "status": "AVAILABLE",
            "available": True,
            "signals_found": ["AdvanceBooking in categories"],
            "unavailable_signals": [],
            "startsAt": "2020-01-01T10:00:00+08:00",
        }

        with patch.object(checker, "send_discord_notification", return_value=True) as mock_notify:
            # Run 1: Announced
            with patch.object(checker, "check_availability", return_value=res_announce):
                checker.main([])

            self.assertEqual(mock_notify.call_count, 1)
            with open(self.test_state_file, "r", encoding="utf-8") as f:
                state1 = json.load(f)
            self.assertEqual(state1, {"notify_phase": "announced", "last_status": "available"})

            # Intermediate run: Still announced (future) -> should skip
            mtime_before_skip1 = os.path.getmtime(self.test_state_file)
            with patch.object(checker, "check_availability", return_value=res_announce):
                checker.main([])

            self.assertEqual(mock_notify.call_count, 1)  # unchanged
            self.assertEqual(os.path.getmtime(self.test_state_file), mtime_before_skip1)

            # Run 2: Booking opens now -> must notify again!
            with patch.object(checker, "check_availability", return_value=res_open):
                checker.main([])

            self.assertEqual(mock_notify.call_count, 2)
            with open(self.test_state_file, "r", encoding="utf-8") as f:
                state2 = json.load(f)
            self.assertEqual(state2, {"notify_phase": "open", "last_status": "available"})

            # Run 3: Next run (still open) -> must skip and write nothing
            mtime_before_skip2 = os.path.getmtime(self.test_state_file)
            with patch.object(checker, "check_availability", return_value=res_open):
                checker.main([])

            self.assertEqual(mock_notify.call_count, 2)  # still 2!
            self.assertEqual(os.path.getmtime(self.test_state_file), mtime_before_skip2)
            with open(self.test_state_file, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f), state2)

    def test_straight_to_open_from_none(self):
        """Tickets released straight to sale without prior announcement alert once."""
        initial = {"notify_phase": "none", "last_status": "unavailable"}
        with open(self.test_state_file, "w", encoding="utf-8") as f:
            json.dump(initial, f)

        res_open = {
            "status": "AVAILABLE",
            "available": True,
            "signals_found": ["NowShowing in categories"],
            "unavailable_signals": [],
            "startsAt": None,
        }

        with patch.object(checker, "send_discord_notification", return_value=True) as mock_notify:
            with patch.object(checker, "check_availability", return_value=res_open):
                checker.main([])

            self.assertEqual(mock_notify.call_count, 1)
            with open(self.test_state_file, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"notify_phase": "open", "last_status": "available"})

            # 2nd run: still open -> 0 notifications
            mtime_before = os.path.getmtime(self.test_state_file)
            with patch.object(checker, "check_availability", return_value=res_open):
                checker.main([])

            self.assertEqual(mock_notify.call_count, 1)
            self.assertEqual(os.path.getmtime(self.test_state_file), mtime_before)

    def test_available_then_unavailable_resets_phase_and_can_alert_again(self):
        """Tickets become available, then vanish, then return -> resets to 'none' and alerts again."""
        initial = {"notify_phase": "none", "last_status": "unavailable"}
        with open(self.test_state_file, "w", encoding="utf-8") as f:
            json.dump(initial, f)

        res_avail = {
            "status": "AVAILABLE",
            "available": True,
            "signals_found": ["NowShowing in categories"],
            "unavailable_signals": [],
            "startsAt": None,
        }
        res_unavail = {
            "status": "UNAVAILABLE",
            "available": False,
            "signals_found": [],
            "unavailable_signals": ["ComingSoon"],
        }

        with patch.object(checker, "send_discord_notification", return_value=True) as mock_notify:
            # 1. Available -> notify, phase='open'
            with patch.object(checker, "check_availability", return_value=res_avail):
                checker.main([])
            self.assertEqual(mock_notify.call_count, 1)

            # 2. Unavailable -> resets phase to 'none'
            with patch.object(checker, "check_availability", return_value=res_unavail):
                checker.main([])
            with open(self.test_state_file, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"notify_phase": "none", "last_status": "unavailable"})

            # 3. Available again -> notifies a 2nd time!
            with patch.object(checker, "check_availability", return_value=res_avail):
                checker.main([])
            self.assertEqual(mock_notify.call_count, 2)
            with open(self.test_state_file, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"notify_phase": "open", "last_status": "available"})


class TestUserAgentNormalisation(unittest.TestCase):
    """Unit tests for normalize_user_agent(), which keeps the UA in step with the browser."""

    def test_headless_marker_is_removed(self):
        """'HeadlessChrome' is an automation tell and must be rewritten to 'Chrome'."""
        raw = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "HeadlessChrome/141.0.0.0 Safari/537.36"
        )
        ua = checker.normalize_user_agent(raw)
        self.assertNotIn("Headless", ua)
        self.assertIn("Chrome/141.0.0.0", ua)

    def test_plain_chrome_user_agent_is_left_alone(self):
        raw = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
        )
        self.assertEqual(checker.normalize_user_agent(raw), raw)

    def test_empty_or_missing_probe_falls_back_to_default(self):
        self.assertEqual(checker.normalize_user_agent(""), checker.DEFAULT_USER_AGENT)
        self.assertEqual(checker.normalize_user_agent("   "), checker.DEFAULT_USER_AGENT)
        self.assertEqual(checker.normalize_user_agent(None), checker.DEFAULT_USER_AGENT)


class TestEvaluateAvailability(unittest.TestCase):
    """
    Unit tests for the evidence-based evaluate_availability() function evaluating API payloads.
    Derived from observed category values across 46 live films.
    """

    # Verbatim real fixtures from live endpoint probes
    FIXTURE_URANUS_2324 = {
        "categories": ["ComingSoon"],
        "advanceBookingPeriods": [],
        "showtimeAttributeIds": ["0000000001", "0000000006", "0000000011"],
    }

    FIXTURE_SB19 = {
        "categories": ["ComingSoon", "AdvanceBooking"],
        "advanceBookingPeriods": [
            {
                "startsAt": "2026-09-10T15:00:00+08:00",
                "orderBookingModes": ["Paid", "Unpaid", "UnpaidConfirmed"],
                "restriction": "None",
                "rewardId": None,
            }
        ],
        "showtimeAttributeIds": [
            "0000000001",
            "0000000006",
            "0000000018",
            "0000000030",
            "0000000011",
            "0000000005",
        ],
    }

    FIXTURE_FORGOTTEN_ISLAND = {
        "categories": ["NowShowing"],
        "advanceBookingPeriods": [],
        "showtimeAttributeIds": ["0000000001", "0000000005"],
    }

    FIXTURE_AVENGERS_DOOMSDAY = {
        "categories": ["ComingSoon"],
        "advanceBookingPeriods": [],
        "showtimeAttributeIds": [],
    }

    def test_uranus_2324_regression_showtime_attribute_ids_ignored_is_unavailable(self):
        """Regression test: showtimeAttributeIds is format tags (2D/3D/IMAX) and MUST be ignored."""
        status, found_avail, found_unavail, err = checker.evaluate_availability(self.FIXTURE_URANUS_2324)
        self.assertEqual(status, "UNAVAILABLE")
        self.assertEqual(found_avail, [])
        self.assertTrue(any("ComingSoon" in u for u in found_unavail))
        self.assertIsNone(err)

    def test_sb19_advance_booking_open_is_available_and_carries_starts_at(self):
        """SB19 has active booking: 'AdvanceBooking' in categories and carries startsAt."""
        res = checker.evaluate_availability(self.FIXTURE_SB19)
        status, found_avail, found_unavail, err = res
        self.assertEqual(status, "AVAILABLE")
        self.assertEqual(res.startsAt, "2026-09-10T15:00:00+08:00")
        self.assertEqual(res["startsAt"], "2026-09-10T15:00:00+08:00")
        self.assertTrue(any("AdvanceBooking" in a for a in found_avail))
        self.assertIsNone(err)

    def test_forgotten_island_now_showing_is_available(self):
        """Forgotten Island is in cinemas now: 'NowShowing' in categories -> AVAILABLE."""
        status, found_avail, found_unavail, err = checker.evaluate_availability(self.FIXTURE_FORGOTTEN_ISLAND)
        self.assertEqual(status, "AVAILABLE")
        self.assertTrue(any("NowShowing" in a for a in found_avail))
        self.assertEqual(found_unavail, [])
        self.assertIsNone(err)

    def test_avengers_doomsday_coming_soon_is_unavailable(self):
        """Avengers: Doomsday has categories ['ComingSoon'] and empty booking/showtimes -> UNAVAILABLE."""
        status, found_avail, found_unavail, err = checker.evaluate_availability(self.FIXTURE_AVENGERS_DOOMSDAY)
        self.assertEqual(status, "UNAVAILABLE")
        self.assertEqual(found_avail, [])
        self.assertTrue(any("ComingSoon" in u for u in found_unavail))
        self.assertIsNone(err)

    def test_advance_booking_periods_alone_triggers_available(self):
        """advanceBookingPeriods non-empty even without AdvanceBooking in categories -> AVAILABLE."""
        avail = {
            "categories": ["ComingSoon"],
            "advanceBookingPeriods": [{"startsAt": "2026-10-01T10:00:00+08:00"}],
        }
        res = checker.evaluate_availability(avail)
        self.assertEqual(res.status, "AVAILABLE")
        self.assertEqual(res.startsAt, "2026-10-01T10:00:00+08:00")

    def test_unrecognised_category_value_is_available_with_warning(self):
        """Unrecognised category outside {ComingSoon, NowShowing, AdvanceBooking} -> AVAILABLE."""
        avail = {
            "categories": ["SecretScreening"],
            "advanceBookingPeriods": [],
        }
        status, found_avail, found_unavail, err = checker.evaluate_availability(avail)
        self.assertEqual(status, "AVAILABLE")
        self.assertTrue(any("SecretScreening" in a for a in found_avail))
        self.assertIsNone(err)

    def test_empty_categories_list_is_hard_error(self):
        status, _, _, err = checker.evaluate_availability({"categories": []})
        self.assertEqual(status, "ERROR")
        self.assertIn("empty", err.lower())

    def test_missing_key_or_invalid_shape_is_hard_error(self):
        status1, _, _, err1 = checker.evaluate_availability({"filmId": "HO00001619"})
        self.assertEqual(status1, "ERROR")
        self.assertIn("categories", err1.lower())

        status2, _, _, err2 = checker.evaluate_availability(None)
        self.assertEqual(status2, "ERROR")

        status3, _, _, err3 = checker.evaluate_availability("not a dict")
        self.assertEqual(status3, "ERROR")


class TestDiscordPayloadTiming(unittest.TestCase):
    """Unit tests for distinguishing future startsAt ('opens at') vs active startsAt ('open now')."""

    def test_future_starts_at_renders_opens_at(self):
        result = {
            "status": "AVAILABLE",
            "signals_found": ["AdvanceBooking in categories"],
            "startsAt": "2099-12-01T15:00:00+08:00",
        }
        payload = checker.build_discord_payload(result)
        desc = payload["embeds"][0]["description"]
        self.assertIn("opens at", desc)

        sched_field = next(f for f in payload["embeds"][0]["fields"] if "Booking Schedule" in f["name"])
        self.assertIn("opens at", sched_field["value"])

    def test_past_starts_at_renders_open_now(self):
        result = {
            "status": "AVAILABLE",
            "signals_found": ["AdvanceBooking in categories"],
            "startsAt": "2020-01-01T10:00:00+08:00",
        }
        payload = checker.build_discord_payload(result)
        desc = payload["embeds"][0]["description"]
        self.assertIn("open now", desc)

        sched_field = next(f for f in payload["embeds"][0]["fields"] if "Booking Schedule" in f["name"])
        self.assertIn("open now", sched_field["value"])



class TestTokenAndFilmIdExtraction(unittest.TestCase):
    """Unit tests for URL film ID extraction and __NEXT_DATA__ token parsing."""

    def test_extract_film_id(self):
        self.assertEqual(
            checker.extract_film_id("https://www.smcinema.com/films/Avengers-Doomsday/HO00001619"),
            "HO00001619",
        )
        self.assertEqual(
            checker.extract_film_id("https://www.smcinema.com/films/Avengers-Doomsday/HO00001619/"),
            "HO00001619",
        )
        self.assertEqual(checker.extract_film_id("HO00001619"), "HO00001619")
        self.assertEqual(checker.extract_film_id(""), "")

    def test_extract_gas_token_success(self):
        html = """
        <html>
        <head><title>Avengers: Doomsday</title></head>
        <body>
        <script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"environment":{"gasToken":"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy.signature"}}}}
        </script>
        </body>
        </html>
        """
        token = checker.extract_gas_token(html)
        self.assertEqual(token, "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy.signature")

    def test_extract_gas_token_missing_script(self):
        html = "<html><body>No Next.js script here</body></html>"
        self.assertIsNone(checker.extract_gas_token(html))

    def test_extract_gas_token_missing_token_key(self):
        html = '<script id="__NEXT_DATA__" type="application/json">{"props":{}}</script>'
        self.assertIsNone(checker.extract_gas_token(html))

    def test_extract_gas_token_malformed_json(self):
        html = '<script id="__NEXT_DATA__" type="application/json">{invalid json}</script>'
        self.assertIsNone(checker.extract_gas_token(html))


class MockResponse:
    def __init__(self, status_code, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def json(self):
        if self._json_data is not None:
            return self._json_data
        raise ValueError("No JSON")


class TestApiFailureClassification(unittest.TestCase):
    """
    Unit tests for Task 4 failure classifications:
      page fetch 403 or 429                     -> soft (Cloudflare throttle)
      page fetch 5xx, timeout, connection error -> soft
      page fetch 404                            -> hard (film URL is wrong/gone)
      __NEXT_DATA__ missing, or gasToken absent -> hard (page structure changed)
      API 401/403                               -> hard (token flow changed)
      API 200 but no filmAvailability key       -> hard (schema drift)
      API 5xx or timeout                        -> soft
    """

    VALID_HTML = """
    <html><head><title>Avengers</title></head><body>
    <script id="__NEXT_DATA__" type="application/json">
    {"props":{"pageProps":{"environment":{"gasToken":"valid_token_12345"}}}}
    </script>
    </body></html>
    """

    def test_page_fetch_403_is_soft(self):
        session = unittest.mock.MagicMock()
        session.get.return_value = MockResponse(403, "Access Denied")
        res = checker._check_availability_api_single("https://www.smcinema.com/films/test/HO00001619", session=session)
        self.assertEqual(res["status"], "ERROR")
        self.assertFalse(res["hard"])
        self.assertIn("Cloudflare", res["error_reason"])

    def test_page_fetch_429_is_soft(self):
        session = unittest.mock.MagicMock()
        session.get.return_value = MockResponse(429, "Too Many Requests")
        res = checker._check_availability_api_single("https://www.smcinema.com/films/test/HO00001619", session=session)
        self.assertEqual(res["status"], "ERROR")
        self.assertFalse(res["hard"])

    def test_page_fetch_5xx_is_soft(self):
        session = unittest.mock.MagicMock()
        session.get.return_value = MockResponse(502, "Bad Gateway")
        res = checker._check_availability_api_single("https://www.smcinema.com/films/test/HO00001619", session=session)
        self.assertEqual(res["status"], "ERROR")
        self.assertFalse(res["hard"])

    def test_page_fetch_timeout_is_soft(self):
        session = unittest.mock.MagicMock()
        session.get.side_effect = checker.RequestTimeout("Connection timed out")
        res = checker._check_availability_api_single("https://www.smcinema.com/films/test/HO00001619", session=session)
        self.assertEqual(res["status"], "ERROR")
        self.assertFalse(res["hard"])

    def test_page_fetch_404_is_hard(self):
        session = unittest.mock.MagicMock()
        session.get.return_value = MockResponse(404, "Not Found")
        res = checker._check_availability_api_single("https://www.smcinema.com/films/test/HO00001619", session=session)
        self.assertEqual(res["status"], "ERROR")
        self.assertTrue(res["hard"])
        self.assertIn("404", res["error_reason"])

    def test_next_data_missing_is_hard(self):
        session = unittest.mock.MagicMock()
        session.get.return_value = MockResponse(200, "<html><body>No script</body></html>")
        res = checker._check_availability_api_single("https://www.smcinema.com/films/test/HO00001619", session=session)
        self.assertEqual(res["status"], "ERROR")
        self.assertTrue(res["hard"])
        self.assertIn("gasToken absent", res["error_reason"])

    def test_api_401_or_403_is_hard(self):
        session = unittest.mock.MagicMock()
        # Page returns 200 with valid token, but API returns 401
        session.get.side_effect = [
            MockResponse(200, self.VALID_HTML),
            MockResponse(401, "Unauthorized"),
        ]
        res = checker._check_availability_api_single("https://www.smcinema.com/films/test/HO00001619", session=session)
        self.assertEqual(res["status"], "ERROR")
        self.assertTrue(res["hard"])
        self.assertIn("authorization", res["error_reason"].lower())

    def test_api_200_missing_film_availability_key_is_hard(self):
        session = unittest.mock.MagicMock()
        session.get.side_effect = [
            MockResponse(200, self.VALID_HTML),
            MockResponse(200, json_data={"unexpectedKey": 123}),
        ]
        res = checker._check_availability_api_single("https://www.smcinema.com/films/test/HO00001619", session=session)
        self.assertEqual(res["status"], "ERROR")
        self.assertTrue(res["hard"])
        self.assertIn("schema drift", res["error_reason"].lower())

    def test_api_5xx_is_soft(self):
        session = unittest.mock.MagicMock()
        session.get.side_effect = [
            MockResponse(200, self.VALID_HTML),
            MockResponse(500, "Internal Server Error"),
        ]
        res = checker._check_availability_api_single("https://www.smcinema.com/films/test/HO00001619", session=session)
        self.assertEqual(res["status"], "ERROR")
        self.assertFalse(res["hard"])

    def test_api_timeout_is_soft(self):
        session = unittest.mock.MagicMock()
        session.get.side_effect = [
            MockResponse(200, self.VALID_HTML),
            checker.RequestTimeout("API timeout"),
        ]
        res = checker._check_availability_api_single("https://www.smcinema.com/films/test/HO00001619", session=session)
        self.assertEqual(res["status"], "ERROR")
        self.assertFalse(res["hard"])

    def test_api_single_result_carries_starts_at(self):
        session = unittest.mock.MagicMock()
        session.get.side_effect = [
            MockResponse(200, self.VALID_HTML),
            MockResponse(200, json_data={"filmAvailability": TestEvaluateAvailability.FIXTURE_SB19}),
        ]
        res = checker._check_availability_api_single("https://www.smcinema.com/films/test/HO00001619", session=session)
        self.assertEqual(res["status"], "AVAILABLE")
        self.assertEqual(res["startsAt"], "2026-09-10T15:00:00+08:00")



class TestApiRetryAndDispatcher(unittest.TestCase):
    """Unit tests for retrying soft failures, aborting on hard failures, and fallback dispatching."""

    def test_retries_on_soft_failure_and_succeeds(self):
        soft_err = checker.error_result("Cloudflare throttle", hard=False)
        success = {"status": "UNAVAILABLE", "available": False, "signals_found": [], "unavailable_signals": []}

        with patch.object(checker, "_check_availability_api_single", side_effect=[soft_err, soft_err, success]) as mock_single:
            res = checker.check_availability_api(retry_backoffs=(0, 0, 0))
            self.assertEqual(res["status"], "UNAVAILABLE")
            self.assertEqual(mock_single.call_count, 3)

    def test_aborts_immediately_on_hard_failure(self):
        hard_err = checker.error_result("404 Not Found", hard=True)

        with patch.object(checker, "_check_availability_api_single", return_value=hard_err) as mock_single:
            res = checker.check_availability_api(retry_backoffs=(0, 0, 0))
            self.assertEqual(res["status"], "ERROR")
            self.assertTrue(res["hard"])
            self.assertEqual(mock_single.call_count, 1)

    def test_dispatcher_defaults_to_api(self):
        with patch.dict(os.environ, {"USE_BROWSER_FALLBACK": "0"}), \
             patch.object(checker, "check_availability_api", return_value={"status": "API"}) as mock_api, \
             patch.object(checker, "check_availability_browser", return_value={"status": "BROWSER"}) as mock_browser:
            res = checker.check_availability()
            self.assertEqual(res["status"], "API")
            mock_api.assert_called_once()
            mock_browser.assert_not_called()

    def test_dispatcher_uses_browser_when_enabled(self):
        with patch.dict(os.environ, {"USE_BROWSER_FALLBACK": "1"}), \
             patch.object(checker, "check_availability_api", return_value={"status": "API"}) as mock_api, \
             patch.object(checker, "check_availability_browser", return_value={"status": "BROWSER"}) as mock_browser:
            res = checker.check_availability()
            self.assertEqual(res["status"], "BROWSER")
            mock_browser.assert_called_once()
            mock_api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
