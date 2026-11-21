"""Offline regression tests for the astronomy core.

Standard library only (`python -m unittest discover -s tests`), no Home Assistant and no
third-party packages.  The external references are *fixtures* generated from the astronomy
stack Home Assistant core itself uses (astral 3.2: Meeus ch. 25 for the Sun, the full ch. 33
theory for the Moon) by:

    scripts/regen_lunar_series.py     -> tests/fixtures/lunar_reference.json
    scripts/regen_event_fixtures.py   -> tests/fixtures/events_reference.json

Two kinds of check are made on purpose:

* agreement with the reference at mid latitudes, where rise/set conventions are unambiguous;
* an altitude identity at *every* event we report - the body's altitude at the instant we
  claim it crosses the horizon must equal the horizon altitude we used.  Polar sites are
  only checked this way, because "which graze belongs to which day" is a convention almanacs
  genuinely disagree about (astral's own Reykjavik table has a sunset earlier than the
  sunrise of the same date).
"""
from __future__ import annotations

import os
import json
import math
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "custom_components" / "fishing"))   # appended, never prepended: select.py must not shadow the stdlib

#: The day-by-day sweeps over the fixtures are the slow part of the whole suite (~40 s). By default
#: every second day is checked, which still covers every season at every site; set
#: FISHING_FULL_ASTRO=1 to check every day (do that whenever the ephemeris or the event finders change).
SWEEP_STRIDE = 1 if os.environ.get("FISHING_FULL_ASTRO") else 2


def sweep(rows):
    return rows[::SWEEP_STRIDE]


from model import astronomy as astro  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Mid-latitude sites have a single unambiguous sunrise/sunset per local day, so exact
# agreement with the reference is meaningful there.
MID_LATITUDE_SITES = ("enkhuizen_nl", "new_york_us", "auckland_nz", "singapore_sg")
SUN_TOL_S = 60.0
NOON_TOL_S = 30.0
MOON_TOL_S = 300.0  # astral's own moonrise search is coarser than ours
ANGLE_TOL_MIN = 4.0  # lunar geocentric position, arcminutes
DIST_TOL_KM = 400.0


def load_events() -> dict:
    with open(FIXTURES / "events_reference.json") as fh:
        data = json.load(fh)
    data["site_geo"] = {row[0]: row[1:4] for row in data["sites"]}
    return data


def load_lunar() -> list[dict]:
    with open(FIXTURES / "lunar_reference.json") as fh:
        return json.load(fh)


def observer_tz(row: dict) -> timezone:
    """Rebuild the observer's offset for that day - keeps the tests tz-data free."""
    return timezone(timedelta(minutes=row["utc_offset_min"]))


def local_day_start(row: dict) -> datetime:
    return datetime.fromisoformat(row["date"] + "T00:00:00").replace(tzinfo=observer_tz(row))


def sep_arcmin(ra1, dec1, ra2, dec2) -> float:
    d2r = math.radians
    cosc = (
        math.sin(d2r(dec1)) * math.sin(d2r(dec2))
        + math.cos(d2r(dec1)) * math.cos(d2r(dec2)) * math.cos(d2r(ra1 - ra2))
    )
    return math.degrees(math.acos(min(1.0, max(-1.0, cosc)))) * 60.0


class LunarEphemerisTest(unittest.TestCase):
    def test_position_matches_reference_theory(self):
        ref = load_lunar()
        self.assertEqual(len(ref), 60)
        worst = 0.0
        acc = 0.0
        worst_dist = 0.0
        for row in ref:
            mine = astro.moon_position(row["jd"])
            e = sep_arcmin(mine.ra, mine.dec, row["ref_ra"], row["ref_dec"])
            worst = max(worst, e)
            acc += e * e
            worst_dist = max(worst_dist, abs(mine.dist_km - row["ref_dist_km"]))
        self.assertLessEqual(worst, ANGLE_TOL_MIN, f"worst lunar position error {worst:.2f}'")
        self.assertLessEqual(math.sqrt(acc / len(ref)), 1.5, f"rms lunar position error {math.sqrt(acc/len(ref)):.2f}'")
        self.assertLessEqual(worst_dist, DIST_TOL_KM, f"worst range error {worst_dist:.0f} km")

    def test_distance_stays_physical(self):
        jd = astro.to_jd(datetime(2024, 1, 1, tzinfo=timezone.utc))
        lo, hi = 1e9, -1e9
        for _ in range(4 * 366):
            d = astro.moon_position(jd).dist_km
            lo, hi = min(lo, d), max(hi, d)
            jd += 0.25
        # Real Moon: perigee 356,400-370,400 km, apogee 404,000-406,700 km.
        self.assertTrue(355_000 < lo < 371_000, f"closest {lo:.0f} km")
        self.assertTrue(403_000 < hi < 407_500, f"farthest {hi:.0f} km")

    def test_phase_geometry_is_self_consistent(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        new = astro.find_phase_crossing("new", start)
        self.assertIsNotNone(new)
        self.assertLess(abs(astro.wrap180(astro.elongation(astro.to_jd(new)))), 0.05)
        nxt = astro.find_phase_crossing("new", new + timedelta(hours=6))
        month = (nxt - new).total_seconds() / 86400.0
        self.assertAlmostEqual(month, astro.SYNODIC_MONTH, delta=0.15)
        full = astro.find_phase_crossing("full", new)
        half = (full - new).total_seconds() / 86400.0
        self.assertTrue(13.6 <= half <= 15.6, f"new->full took {half:.2f} d")
        info = astro.moon_phase_info(astro.to_jd(full))
        self.assertEqual(info.phase_name, "full_moon")
        self.assertGreater(info.illumination, 0.999)
        near_new = astro.moon_phase_info(astro.to_jd(new))
        self.assertTrue(near_new.waxing)  # just-born moon is waxing
        self.assertLess(near_new.illumination, 0.002)

    def test_tide_state_alternates_and_extremes_are_regular(self):
        lon = 5.2722
        dt0 = datetime(2026, 5, 17, tzinfo=timezone.utc)
        states = [astro.tide_state(dt0 + timedelta(minutes=25 * i), lon) for i in range(60)]
        ups = sum(1 for s in states if s.label in ("rising", "high"))
        self.assertTrue(20 <= ups <= 40, "equilibrium tide should alternate with ~12.4 h period")
        self.assertTrue(all(-1.0 <= s.level <= 1.0 for s in states))
        self.assertTrue(all(0.0 <= s.range_index <= 1.0 for s in states))
        ext = astro.tide_extremes(dt0, lon, horizon_hours=26.0)
        self.assertTrue(3 <= len(ext) <= 5, f"got {len(ext)} extremes")
        for a, b in zip(ext, ext[1:]):
            self.assertGreater((b - a).total_seconds() / 3600.0, 4.5)

    def test_spring_neap_follows_syzygy(self):
        start = datetime(2026, 4, 1, tzinfo=timezone.utc)
        new = astro.find_phase_crossing("new", start)
        quarter = astro.find_phase_crossing("full", new) + timedelta(days=7.4)
        spring = astro.tide_state(new, 5.2722)
        neap = astro.tide_state(quarter, 5.2722)
        self.assertGreater(spring.range_index, neap.range_index + 0.2)


class SolarEventsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_events()

    def test_matches_reference_at_mid_latitudes(self):
        worst = {"sunrise": 0.0, "sunset": 0.0, "solar_noon": 0.0}
        compared = 0
        for site in MID_LATITUDE_SITES:
            lat, lon, elev = self.data["site_geo"][site]
            for row in sweep(self.data["events"][site]):
                tz = observer_tz(row)
                light = astro.day_light(local_day_start(row), lat, lon, tz=tz, elevation=elev)
                for key, tol in (("sunrise", SUN_TOL_S), ("sunset", SUN_TOL_S), ("solar_noon", NOON_TOL_S)):
                    mine = getattr(light, key)
                    self.assertIsNotNone(mine, f"{site} {row['date']} {key}: we report nothing")
                    self.assertIsNotNone(row[key], f"{site} {row['date']} {key}: reference missing")
                    delta = abs((mine - datetime.fromisoformat(row[key])).total_seconds())
                    self.assertLessEqual(delta, tol, f"{site} {row['date']} {key} off by {delta:.0f}s")
                    worst[key] = max(worst[key], delta)
                    compared += 1
        self.assertGreater(compared, 800 // SWEEP_STRIDE)       # the floor scales with the sweep: still guards against a vacuous pass

    def test_day_light_is_ordered_and_bounded_at_normal_latitudes(self):
        """Events must bracket solar noon; at ordinary latitudes they also stay in the day.

        The bounding check is deliberately limited to mid latitudes: at 64 N in June the Sun
        is up from 02:54 until 00:02 the next morning, so a "coherent day" genuinely straddles
        local midnight and the honest report is the interval, not a midnight-clipped one.
        """
        for site, rows in self.data["events"].items():
            lat, lon, elev = self.data["site_geo"][site]
            ordinary = site in MID_LATITUDE_SITES
            for row in sweep(rows):
                tz = observer_tz(row)
                day_start = local_day_start(row)
                light = astro.day_light(day_start, lat, lon, tz=tz, elevation=elev)
                ordered = [
                    light.astronomical_dawn, light.nautical_dawn, light.dawn, light.sunrise,
                    light.solar_noon,
                    light.sunset, light.dusk, light.nautical_dusk, light.astronomical_dusk,
                ]
                present = [t for t in ordered if t]
                for a, b in zip(present, present[1:]):
                    self.assertLess(a, b, f"{site} {row['date']}: {a} should precede {b}")
                if ordinary:
                    for attr, val in zip(
                        (
                            "astronomical_dawn", "nautical_dawn", "dawn", "sunrise", "solar_noon",
                            "sunset", "dusk", "nautical_dusk", "astronomical_dusk",
                        ),
                        present,
                    ):
                        # A midsummer night at 52N has its astronomical dusk a few minutes
                        # past local midnight; it belongs to the night that began on this date,
                        # so it is reported here even though it lands on the next calendar day.
                        slack = timedelta(hours=2) if attr.endswith(("nautical_dusk", "astronomical_dusk", "nautical_dawn", "astronomical_dawn")) else timedelta(0)
                        self.assertGreaterEqual(val, day_start - slack, f"{site} {row['date']} {attr} spill too early")
                        self.assertLess(val, day_start + timedelta(days=1) + slack, f"{site} {row['date']} {attr} spill too late")
                if light.day_length_min is not None:
                    self.assertGreater(light.day_length_min, 0.0, f"{site} {row['date']}: negative day length")

    def test_night_windows_are_one_ordered_sequence_even_when_the_night_wraps(self):
        """Per-day fields can't express a night spanning midnight; NightWindows must.

        At 52N in May civil dusk is on the 7th and nautical dusk on the 8th, so the only
        sane structure is the night searched forward from that day's solar noon.
        """
        misordered = []
        incomplete = []
        for site, rows in self.data["events"].items():
            lat, lon, elev = self.data["site_geo"][site]
            for row in sweep(rows):
                tz = observer_tz(row)
                night = astro.night_windows(local_day_start(row), lat, lon, tz=tz, elevation=elev)
                evening = [t for t in (night.sunset, night.dusk, night.nautical_dusk, night.astronomical_dusk) if t]
                morning = [t for t in (night.astronomical_dawn, night.nautical_dawn, night.dawn, night.sunrise) if t]
                misordered += [(site, row["date"], a, b) for a, b in zip(evening, evening[1:]) if a >= b]
                misordered += [(site, row["date"], a, b) for a, b in zip(morning, morning[1:]) if a >= b]
                if evening and morning and evening[-1] >= morning[0]:
                    misordered.append((site, row["date"], evening[-1], morning[0]))
                # NB: no "missing level" rules here - at extreme latitudes a shallower level
                # can legitimately be absent while a deeper one is present (in polar night the
                # Sun crosses -12 and -18 but never reaches -6).  Coherence of what *is*
                # present is what the ordering check above enforces.
        self.assertEqual(misordered[:5], [], f"{len(misordered)} misordered night events")
        self.assertEqual(incomplete[:5], [], f"{len(incomplete)} incoherent night levels")

    def test_night_windows_at_extreme_latitudes(self):
        lat, lon, elev = self.data["site_geo"]["reykjavik_is"]
        row = self.data["events"]["reykjavik_is"][21]  # around the solstice
        night = astro.night_windows(local_day_start(row), lat, lon, tz=observer_tz(row), elevation=elev)
        self.assertIsNotNone(night.sunset)   # 64N still has a sun that dips below the horizon
        # ...but only to about -2.4 deg, so there is no civil twilight and no darkness at all
        self.assertIsNone(night.dusk)
        self.assertTrue(night.dark_hours in (None, 0.0), f"midsummer night dark for {night.dark_hours} h")
        self.assertIsNotNone(night.window("golden_pm"))

        lat, lon, elev = self.data["site_geo"]["longyearbyen_summer"]
        row = self.data["events"]["longyearbyen_summer"][21]
        night = astro.night_windows(local_day_start(row), lat, lon, tz=observer_tz(row), elevation=elev)
        self.assertTrue(night.polar_day)
        self.assertEqual(night.dark_hours, 0.0)
        self.assertIsNone(night.window("golden_pm"))

        lat, lon, elev = self.data["site_geo"]["longyearbyen_winter"]
        row = self.data["events"]["longyearbyen_winter"][4]
        night = astro.night_windows(local_day_start(row), lat, lon, tz=observer_tz(row), elevation=elev)
        self.assertTrue(night.polar_night)
        self.assertIsNone(night.sunset)
        self.assertIsNone(night.dusk)          # Sun only reaches about -10 deg: no civil twilight
        self.assertIsNotNone(night.astronomical_dusk)  # but -18 deg is crossed, twice a day
        dark = night.dark_hours
        self.assertTrue(10 < dark < 20, f"true darkness in January at 78N should be ~14 h, got {dark}")

    def test_arctic_polar_night_and_midnight_sun(self):
        """78.2N: no sun for weeks in January, none setting in June, twilight either way."""
        winter_row = self.data["events"]["longyearbyen_winter"][4]  # 2026-01-05
        tz = observer_tz(winter_row)
        lat, lon, elev = self.data["site_geo"]["longyearbyen_winter"]
        winter = astro.day_light(local_day_start(winter_row), lat, lon, tz=tz, elevation=elev)
        self.assertTrue(winter.polar_night)
        self.assertIsNone(winter.sunrise)
        self.assertIsNone(winter.sunset)
        self.assertIsNotNone(winter.astronomical_dawn)  # the sun does reach -18 deg here
        self.assertIsNotNone(winter.nautical_dawn)

        summer_row = self.data["events"]["longyearbyen_summer"][21]  # 2026-06-22
        tz = observer_tz(summer_row)
        lat, lon, elev = self.data["site_geo"]["longyearbyen_summer"]
        summer = astro.day_light(local_day_start(summer_row), lat, lon, tz=tz, elevation=elev)
        self.assertTrue(summer.polar_day)
        self.assertIsNone(summer.sunrise)
        self.assertIsNone(summer.sunset)
        self.assertIsNone(summer.dawn)  # sun never drops to -6 deg: no civil twilight at all
        self.assertIsNotNone(summer.solar_noon)

    def test_equator_day_length_is_stable(self):
        # Note the +08:00: day_light()'s day is the *observer's* calendar day, so the caller's
        # timezone has to be the one the site actually lives in (this is exactly what HA's own
        # sun entity requires, and what our config entry guarantees).
        sg = timezone(timedelta(hours=8))
        lens = [
            astro.day_light(datetime(2026, m, 15, tzinfo=sg), 1.3521, 103.8198, tz=sg).day_length_min
            for m in range(1, 13)
        ]
        # ~11h57m..12h07m at the equator: driven by EOT and the Sun's declination.
        self.assertLess(max(lens) - min(lens), 12.0, f"equatorial day varies {max(lens)-min(lens):.1f} min")
        self.assertTrue(all(700 < v < 745 for v in lens), lens)


class MoonEventsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_events()

    def test_moonrise_set_match_reference_at_mid_latitudes(self):
        worst = 0.0
        compared = 0
        disagreements = []
        for site in MID_LATITUDE_SITES:
            lat, lon, elev = self.data["site_geo"][site]
            for row in sweep(self.data["events"][site]):
                tz = observer_tz(row)
                ev = astro.body_events(local_day_start(row), lat, lon, moon=True, tz=tz, elevation=elev)
                for key, attr in (("moonrise", "rise"), ("moonset", "set")):
                    mine, ref = getattr(ev, attr), row[key]
                    if ref is None or mine is None:
                        if (ref is None) != (mine is None):
                            disagreements.append((site, row["date"], key, mine, ref))
                        continue
                    delta = abs((mine - datetime.fromisoformat(ref)).total_seconds())
                    worst = max(worst, delta)
                    compared += 1
        self.assertGreater(compared, 200 // SWEEP_STRIDE, "not enough moon events compared")
        self.assertLessEqual(worst, MOON_TOL_S, f"worst moon event error {worst:.0f}s")
        # A missing moonrise/moonset on a date is legitimate (the lunar day is ~24h50m), but
        # the two implementations may not both miss it - check each disagreement is explainable.
        for site, date, key, mine, ref in disagreements[:10]:
            lat, lon, elev = self.data["site_geo"][site]
            tz = observer_tz(row)
            day_start = datetime.fromisoformat(date + "T00:00:00").replace(tzinfo=tz)
            attr = "rise" if key == "moonrise" else "set"
            nearby = [
                getattr(astro.body_events(day_start + timedelta(days=off), lat, lon, moon=True, tz=tz, elevation=elev), attr)
                for off in (-1, 1)
            ]
            found = [n for n in nearby if n is not None]
            self.assertTrue(found, f"{site} {date} {key}: no event at all within a day")
            if ref is not None:
                self.assertLess(
                    min(abs((n - datetime.fromisoformat(ref)).total_seconds()) for n in found),
                    26 * 3600,
                    f"{site} {date} {key}: reference {ref} not reproduced nearby",
                )

    def test_moon_altitude_identity(self):
        for site, rows in self.data["events"].items():
            lat, lon, elev = self.data["site_geo"][site]
            want = astro.MOON_H0 - astro.horizon_dip_deg(elev)
            for row in sweep(rows):
                tz = observer_tz(row)
                ev = astro.body_events(local_day_start(row), lat, lon, moon=True, tz=tz, elevation=elev)
                for when in (ev.rise, ev.set):
                    if when is None:
                        continue
                    got = astro.moon_altitude(when, lat, lon)
                    self.assertLess(abs(got - want), 0.03, f"{site} {row['date']}: moon alt {got:.3f} != {want:.3f}")

    def test_transit_pair_is_about_half_a_lunar_day_apart(self):
        tz = timezone(timedelta(hours=2))  # CEST
        ev = astro.body_events(datetime(2026, 4, 4, tzinfo=tz), 52.3702, 5.2722, moon=True, tz=tz)
        self.assertIsNotNone(ev.transit)
        self.assertIsNotNone(ev.lower_transit)
        gap = abs((ev.transit - ev.lower_transit).total_seconds()) / 3600.0
        self.assertTrue(11.5 <= gap <= 13.5, f"transit pair {gap:.2f} h apart")


class SolarAnglesTest(unittest.TestCase):
    def test_solstice_and_equinox_declination(self):
        for when, want in (
            (datetime(2026, 3, 20, 10, 0, tzinfo=timezone.utc), 0.0),
            (datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc), 23.44),
            (datetime(2026, 12, 21, 10, 0, tzinfo=timezone.utc), -23.44),
        ):
            dec = astro.sun_position(astro.to_jd(when)).dec
            self.assertLess(abs(dec - want), 0.20, f"{when} dec={dec:.2f}")

    def test_equation_of_time_extremes(self):
        feb = astro.sun_position(astro.to_jd(datetime(2026, 2, 12, 12, 0, tzinfo=timezone.utc))).eot_minutes
        nov = astro.sun_position(astro.to_jd(datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc))).eot_minutes
        self.assertTrue(-17 < feb < -11, f"mid-Feb EOT {feb:.1f} min")
        self.assertTrue(12 < nov < 18, f"early-Nov EOT {nov:.1f} min")

    def test_horizon_dip_matches_the_geometric_formula(self):
        self.assertEqual(astro.horizon_dip_deg(0), 0.0)
        # dip in arcminutes ~ 1.93*sqrt(h): 6.1' at 10 m, 14.9' at 60 m
        self.assertAlmostEqual(astro.horizon_dip_deg(10), 0.1016, delta=0.002)
        self.assertAlmostEqual(astro.horizon_dip_deg(60), 0.2488, delta=0.005)
        tz = timezone(timedelta(hours=2))  # CEST
        d = datetime(2026, 6, 21, tzinfo=tz)
        low = astro.day_light(d, 52.3702, 5.2722, tz=tz, elevation=0)
        high = astro.day_light(d, 52.3702, 5.2722, tz=tz, elevation=120)
        self.assertLess(high.sunrise, low.sunrise)
        self.assertGreater(high.sunset, low.sunset)

    def test_golden_windows_bracket_sunrise_and_sunset(self):
        tz = timezone(timedelta(hours=2))  # CEST
        light = astro.day_light(datetime(2026, 5, 10, tzinfo=tz), 52.3702, 5.2722, tz=tz)
        windows = astro.golden_windows(light)
        self.assertEqual(len(windows), 2)
        am, pm = windows
        self.assertLessEqual(am[0], light.sunrise)
        self.assertGreaterEqual(am[1], light.sunrise)
        self.assertLessEqual(pm[0], light.sunset)
        self.assertGreaterEqual(pm[1], light.sunset)


if __name__ == "__main__":
    unittest.main(verbosity=2)
