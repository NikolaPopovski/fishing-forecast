"""Offline tests for model.context: the HA<->model seam (series cache, local time, astronomy cache)."""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "custom_components" / "fishing"))   # appended, never prepended: select.py must not shadow the stdlib

from model import climatology as cl  # noqa: E402
from model import context as cx  # noqa: E402

SYDNEY = dict(name="bay", lat=-33.9, lon=151.2, tz_offset_min=600)      # UTC+10, no tz database needed
DUTCH = dict(name="lake", lat=52.7, lon=5.3, tz_offset_min=120)
T0 = datetime(2025, 6, 10)


class SeriesCacheTest(unittest.TestCase):
    def test_get_cleans_sorts_and_never_raises_for_a_missing_series(self):
        ctx = cx.SpotContext(**DUTCH)
        ctx.series["pressure"] = [(T0 + timedelta(hours=2), 1010.0), (T0, float("nan")), (T0 + timedelta(hours=1), 1011.0), (T0 + timedelta(hours=1), 1012.0)]
        self.assertEqual(ctx.get("pressure"), [(T0 + timedelta(hours=1), 1012.0), (T0 + timedelta(hours=2), 1010.0)])
        self.assertEqual(ctx.get("discharge"), [])
        self.assertEqual(ctx.get("not_a_series_at_all"), [])

    def test_cache_holds_until_invalidated(self):
        ctx = cx.SpotContext(**DUTCH)
        ctx.series["air_temp"] = [(T0, 10.0)]
        first = ctx.get("air_temp")
        self.assertIs(ctx.get("air_temp"), first)
        ctx.series["air_temp"] = [(T0, 20.0)]
        self.assertEqual(ctx.get("air_temp"), [(T0, 10.0)])            # stale on purpose: replaced series need invalidate()
        ctx.invalidate()
        self.assertEqual(ctx.get("air_temp"), [(T0, 20.0)])

    def test_series_names_cover_what_the_factor_modules_read(self):
        for needed in ("air_temp", "pressure", "wind_speed", "wind_dir", "cloud", "precip", "uv_index", "water_temp", "discharge", "humidity", "shortwave"):
            self.assertIn(needed, cx.SERIES_NAMES)


class TimeZoneTest(unittest.TestCase):
    def test_fixed_offset_wins_and_needs_no_tz_database(self):
        ctx = cx.SpotContext(name="x", lat=0.0, lon=0.0, tz_name="Europe/Amsterdam", tz_offset_min=120)
        self.assertEqual(ctx.tz.utcoffset(None), timedelta(hours=2))

    def test_unknown_zone_falls_back_to_utc(self):
        self.assertEqual(cx.SpotContext(name="x", lat=0.0, lon=0.0, tz_name="Not/AZone").tz, timezone.utc)

    def test_local_midnight_is_aware_and_accepts_date_or_datetime(self):
        ctx = cx.SpotContext(**SYDNEY)
        a = ctx.local_midnight(date(2025, 6, 10))
        b = ctx.local_midnight(datetime(2025, 6, 10, 17, 45))
        self.assertEqual(a, b)
        self.assertEqual((a.hour, a.minute), (0, 0))
        self.assertEqual(a.utcoffset(), timedelta(hours=10))


class AstronomyCacheTest(unittest.TestCase):
    def test_naive_and_aware_callers_get_the_same_local_day(self):
        # regression: a naive `when` used to be anchored on UTC midnight, shifting the "day" by the
        # offset and reporting the wrong moonset (06:51 tomorrow instead of 05:52 today in Sydney)
        ctx = cx.SpotContext(**SYDNEY)
        naive = ctx.astronomy_for(datetime(2025, 6, 10, 12))
        ctx.invalidate()
        aware = ctx.astronomy_for(datetime(2025, 6, 10, 12, tzinfo=ctx.tz))
        self.assertEqual(naive.moon_events.rise, aware.moon_events.rise)
        self.assertEqual(naive.moon_events.set, aware.moon_events.set)
        self.assertEqual(naive.light.sunrise, aware.light.sunrise)
        start = ctx.local_midnight(date(2025, 6, 10))
        for event in (naive.moon_events.rise, naive.moon_events.set):
            self.assertTrue(start <= event < start + timedelta(days=1), event)

    def test_day_is_cached_and_distinct_per_day(self):
        ctx = cx.SpotContext(**DUTCH)
        a = ctx.astronomy_for(datetime(2025, 6, 10, 3))
        self.assertIs(ctx.astronomy_for(datetime(2025, 6, 10, 22)), a)
        self.assertIsNot(ctx.astronomy_for(datetime(2025, 6, 11, 3)), a)
        ctx.invalidate()
        self.assertIsNot(ctx.astronomy_for(datetime(2025, 6, 10, 3)), a)

    def test_the_day_object_is_coherent(self):
        ctx = cx.SpotContext(**DUTCH)
        day = ctx.astronomy_for(datetime(2025, 6, 10, 12))
        self.assertLessEqual(day.light.sunrise, day.light.solar_noon)
        self.assertLessEqual(day.light.solar_noon, day.light.sunset)
        self.assertTrue(day.golden)
        self.assertGreater(day.sun_altitude(day.light.solar_noon, ctx.lat, ctx.lon), 50.0)
        self.assertLess(day.sun_altitude(day.light.solar_noon + timedelta(hours=12), ctx.lat, ctx.lon), 0.0)

    def test_solunar_windows_cover_two_local_days_without_duplicates(self):
        ctx = cx.SpotContext(**DUTCH)
        wins = ctx.solunar_windows(datetime(2025, 6, 10, 9))
        self.assertGreaterEqual(len(wins), 7)
        self.assertEqual([w.start for w in wins], sorted(w.start for w in wins))
        for i, a in enumerate(wins):
            for b in wins[i + 1:]:
                self.assertGreater(abs((a.peak - b.peak).total_seconds()), 900)
        self.assertLess(len(ctx.solunar_windows(datetime(2025, 6, 10, 9), days=1)), len(wins))


class ShelterTest(unittest.TestCase):
    def test_sector_mapping_wraps_at_north(self):
        self.assertEqual([cx._sector(d) for d in (0, 44, 46, 90, 225, 350, 359.9)], ["N", "NE", "NE", "E", "SW", "N", "N"])

    def test_unknown_wind_is_none(self):
        self.assertIsNone(cx.ShelterGeometry().shelteredness(None))

    def test_open_vs_screened_sectors(self):
        geo = cx.ShelterGeometry(exposure={s: 0.0 for s in cx.SECTOR_ORDER})
        geo.exposure.update({"SW": 1.0, "S": 1.0, "W": 1.0})                # a long fetch to the south-west
        self.assertLess(geo.shelteredness(225.0), 0.1)
        self.assertGreater(geo.shelteredness(45.0), 0.9)

    def test_neighbouring_sectors_bleed_in(self):
        geo = cx.ShelterGeometry(exposure={s: 0.0 for s in cx.SECTOR_ORDER})
        geo.exposure["N"] = 1.0
        # an NE wind is mostly screened, but the exposed N neighbour still counts a little
        self.assertLess(geo.shelteredness(45.0), 1.0)
        self.assertGreater(geo.shelteredness(45.0), geo.shelteredness(0.0))

    def test_bank_facing_the_wind_gets_no_protection(self):
        sheltered = cx.ShelterGeometry(exposure={s: 0.0 for s in cx.SECTOR_ORDER}, bank_aspect_deg=None)
        facing = cx.ShelterGeometry(exposure={s: 0.0 for s in cx.SECTOR_ORDER}, bank_aspect_deg=90.0)
        self.assertGreater(sheltered.shelteredness(90.0), 0.9)
        self.assertLess(facing.shelteredness(90.0), 0.1)

    def test_always_in_unit_range(self):
        geo = cx.ShelterGeometry(exposure={s: 2.0 for s in cx.SECTOR_ORDER}, bank_aspect_deg=0.0)
        for d in range(0, 360, 15):
            self.assertTrue(0.0 <= geo.shelteredness(float(d)) <= 1.0)

    def test_round_trip_and_empty_payload(self):
        geo = cx.ShelterGeometry(exposure={**{s: 0.2 for s in cx.SECTOR_ORDER}, "SW": 0.9}, bank_aspect_deg=200.0)
        back = cx.ShelterGeometry.from_dict(json.loads(json.dumps(geo.as_dict())))
        self.assertEqual(back, geo)
        self.assertEqual(cx.ShelterGeometry.from_dict(None), cx.ShelterGeometry())
        self.assertEqual(cx.ShelterGeometry.from_dict({"exposure": {"N": 1.0}}).exposure["S"], 0.5)


class SummaryTest(unittest.TestCase):
    def test_summary_is_json_friendly_and_counts_series_days(self):
        ctx = cx.SpotContext(**DUTCH, clim=cl.Climatology(52.7, 0.0, "lake"))
        ctx.series["air_temp"] = [(T0 + timedelta(hours=i), 10.0) for i in range(72)]
        s = ctx.summary()
        json.dumps(s)
        self.assertEqual(s["series_days"], {"air_temp": 3})
        self.assertEqual(s["spot_kind"], "lake")
        self.assertIsNotNone(s["climatology"])
        self.assertIsNone(cx.SpotContext(**DUTCH).summary()["climatology"])


if __name__ == "__main__":
    unittest.main()
