"""Offline tests for fishing.refresh: the coordinator's decisions, and spot.local_now."""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import glue  # noqa: E402,F401  (registers the `fishing` stand-in package)

from fishing import const, refresh, spot  # noqa: E402
from fishing.api import open_meteo as om  # noqa: E402

T = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


class NeedsFetchTest(unittest.TestCase):
    def test_no_data_yet_fetches(self):
        self.assertTrue(refresh.needs_fetch(None, T))

    def test_inside_the_interval_does_not_touch_the_network(self):
        self.assertFalse(refresh.needs_fetch(T - timedelta(minutes=5), T))
        self.assertFalse(refresh.needs_fetch(T - const.UPDATE_INTERVAL + timedelta(seconds=1), T))

    def test_at_or_past_the_interval_fetches(self):
        self.assertTrue(refresh.needs_fetch(T - const.UPDATE_INTERVAL, T))
        self.assertTrue(refresh.needs_fetch(T - timedelta(hours=5), T))

    def test_a_clock_that_stepped_backwards_refetches_rather_than_waiting_for_the_future(self):
        self.assertTrue(refresh.needs_fetch(T + timedelta(hours=2), T))

    def test_the_rescore_interval_is_much_shorter_than_the_fetch_interval(self):
        self.assertLess(const.RESCORE_INTERVAL * 4, const.UPDATE_INTERVAL)


class DataStateTest(unittest.TestCase):
    def test_states_by_age(self):
        self.assertEqual(refresh.data_state(None, T), "none")
        self.assertEqual(refresh.data_state(T - timedelta(minutes=40), T), "fresh")
        self.assertEqual(refresh.data_state(T - refresh.STALE_AFTER, T), "stale")
        self.assertEqual(refresh.data_state(T - timedelta(hours=8), T), "stale")
        self.assertEqual(refresh.data_state(T - refresh.EXPIRE_AFTER, T), "expired")
        self.assertEqual(refresh.data_state(T + timedelta(hours=1), T), "fresh")        # clock step: not an error

    def test_thresholds_are_ordered(self):
        self.assertLess(const.UPDATE_INTERVAL, refresh.STALE_AFTER)
        self.assertLess(refresh.STALE_AFTER, refresh.EXPIRE_AFTER)


class ArchiveWindowTest(unittest.TestCase):
    TODAY = date(2026, 11, 21)

    def test_first_run_asks_for_the_configured_years_up_to_the_lag(self):
        start, end = refresh.archive_window(None, self.TODAY)
        self.assertEqual(end, self.TODAY - timedelta(days=refresh.ARCHIVE_LAG_DAYS))
        self.assertEqual(start, self.TODAY - timedelta(days=365 * const.CLIMATOLOGY_YEARS))

    def test_later_runs_only_ask_for_days_after_the_last_ingested(self):
        last = date(2026, 11, 10)
        self.assertEqual(refresh.archive_window(last, self.TODAY), (date(2026, 11, 11), date(2026, 11, 16)))

    def test_up_to_date_asks_for_nothing(self):
        self.assertIsNone(refresh.archive_window(date(2026, 11, 16), self.TODAY))
        self.assertIsNone(refresh.archive_window(date(2026, 11, 20), self.TODAY))

    def test_a_day_is_never_requested_twice(self):
        # ingesting a day twice would bias a running average: the window must start strictly after
        for last in (date(2026, 5, 1), date(2026, 11, 1), date(2026, 11, 15)):
            window = refresh.archive_window(last, self.TODAY)
            self.assertTrue(window is None or window[0] > last)

    def test_stored_dates_that_are_corrupt_are_ignored(self):
        self.assertEqual(refresh.parse_day("2026-11-10"), date(2026, 11, 10))
        self.assertEqual(refresh.parse_day("2026-11-10T00:00:00"), date(2026, 11, 10))
        for bad in (None, 7, "", "yesterday", "2026-13-40"):
            self.assertIsNone(refresh.parse_day(bad))


class DailyRowsTest(unittest.TestCase):
    @staticmethod
    def hours(day, n=24, value=10.0):
        return [(datetime(day.year, day.month, day.day, h), value + h) for h in range(n)]

    def test_daily_means_for_complete_days(self):
        series = self.hours(date(2026, 11, 18)) + self.hours(date(2026, 11, 19))
        rows = refresh.daily_rows(series, "water_temp", None, date(2026, 11, 21))
        self.assertEqual([d for d, _ in rows], [date(2026, 11, 18), date(2026, 11, 19)])
        self.assertAlmostEqual(rows[0][1]["water_temp"], 10.0 + 11.5)

    def test_only_days_after_the_last_ingested(self):
        series = self.hours(date(2026, 11, 18)) + self.hours(date(2026, 11, 19))
        rows = refresh.daily_rows(series, "water_temp", date(2026, 11, 18), date(2026, 11, 21))
        self.assertEqual([d for d, _ in rows], [date(2026, 11, 19)])

    def test_today_and_the_future_are_excluded(self):
        series = self.hours(date(2026, 11, 20)) + self.hours(date(2026, 11, 21)) + self.hours(date(2026, 11, 22))
        rows = refresh.daily_rows(series, "water_temp", None, date(2026, 11, 21))
        self.assertEqual([d for d, _ in rows], [date(2026, 11, 20)])

    def test_gappy_days_are_skipped_not_averaged(self):
        series = self.hours(date(2026, 11, 18), n=10) + self.hours(date(2026, 11, 19))
        rows = refresh.daily_rows(series, "water_temp", None, date(2026, 11, 21))
        self.assertEqual([d for d, _ in rows], [date(2026, 11, 19)])

    def test_empty(self):
        self.assertEqual(refresh.daily_rows([], "water_temp", None, date(2026, 11, 21)), [])


class LocalNowTest(unittest.TestCase):
    def test_fixed_offset_fallback(self):
        fc = om.Forecast(timezone="Not/AZone", utc_offset_seconds=3600)
        self.assertEqual(spot.local_now(fc, T), datetime(2026, 11, 21, 13, 0))

    def test_utc(self):
        self.assertEqual(spot.local_now(om.Forecast(timezone="UTC"), T), datetime(2026, 11, 21, 12, 0))
        self.assertEqual(spot.local_now(om.Forecast(), T), datetime(2026, 11, 21, 12, 0))

    def test_result_is_naive_and_handles_a_non_utc_input_zone(self):
        plus_two = datetime(2026, 11, 21, 14, 0, tzinfo=timezone(timedelta(hours=2)))      # the same instant as T
        out = spot.local_now(om.Forecast(timezone="Not/AZone", utc_offset_seconds=7200), plus_two)
        self.assertIsNone(out.tzinfo)
        self.assertEqual(out, datetime(2026, 11, 21, 14, 0))

    def test_offset_can_be_negative_and_cross_midnight(self):
        fc = om.Forecast(timezone="Not/AZone", utc_offset_seconds=-5 * 3600)
        self.assertEqual(spot.local_now(fc, datetime(2026, 11, 21, 3, 0, tzinfo=timezone.utc)), datetime(2026, 11, 20, 22, 0))


if __name__ == "__main__":
    unittest.main()
