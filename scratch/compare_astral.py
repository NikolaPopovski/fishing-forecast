"""Cross-check the in-repo ephemeris against `astral` (scratch/venv only - not a runtime dep).

Run:  scratch/venv/Scripts/python.exe scratch/compare_astral.py
"""
import math
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "custom_components/fishing")
from model import astronomy as astro

from astral import Observer
from astral.moon import moon_position as astral_moon_position
from astral.moon import moonrise, moonset
from astral.sun import sunrise, sunset, noon


def jd2000_of(dt):
    return astro.to_jd(dt) - 2451545.0


print("== moon RA/Dec vs astral (degrees) ==")
worst_ra = worst_dec = 0.0
acc = 0.0
n = 0
t = datetime(2026, 1, 1, tzinfo=timezone.utc)
for i in range(0, 400):  # ~400 x 12h = 200 days
    inst = t + timedelta(hours=12 * i)
    mine = astro.moon_position(astro.to_jd(inst))
    a = astral_moon_position(jd2000_of(inst))
    dra = astro.wrap180(mine.ra - math.degrees(a.right_ascension))
    ddec = mine.dec - math.degrees(a.declination)
    worst_ra = max(worst_ra, abs(dra))
    worst_dec = max(worst_dec, abs(ddec))
    acc += dra * dra + ddec * ddec
    n += 1
print(f"  max dRA {worst_ra*60:.2f}' max dDec {worst_dec*60:.2f}' rms {math.sqrt(acc/n)*60:.2f}'")


def cmp_events(lat, lon, label, tz, start, days):
    obs = Observer(lat, lon, 0)
    dr = ds = 0.0
    cnt = 0
    for i in range(days):
        d0 = (start + timedelta(days=i)).date()
        mine = astro.body_events(
            datetime(d0.year, d0.month, d0.day, tzinfo=timezone.utc), lat, lon, moon=True, tz=timezone.utc
        )
        try:
            ar = moonrise(obs, d0, timezone.utc)
        except ValueError:
            ar = None
        try:
            ase = moonset(obs, d0, timezone.utc)
        except ValueError:
            ase = None
        if ar and mine.rise:
            dr = max(dr, abs((mine.rise - ar).total_seconds()))
            cnt += 1
        if ase and mine.set:
            ds = max(ds, abs((mine.set - ase).total_seconds()))
            cnt += 1
    print(f"  {label}: rise<= {dr/60:5.1f} min, set<= {ds/60:5.1f} min  ({cnt} matched events)")


print("\n== moonrise/moonset vs astral ==")
cmp_events(52.70, 5.29, "Enkhuizen NL ", timezone.utc, datetime(2026, 6, 1, tzinfo=timezone.utc), 40)
cmp_events(-36.85, 174.76, "Auckland NZ", timezone.utc, datetime(2026, 1, 1, tzinfo=timezone.utc), 40)
cmp_events(64.14, -21.90, "Reykjavik  ", timezone.utc, datetime(2026, 3, 1, tzinfo=timezone.utc), 40)
cmp_events(1.29, 103.85, "Singapore  ", timezone.utc, datetime(2026, 9, 1, tzinfo=timezone.utc), 40)

print("\n== sunrise/sunset vs astral ==")
for lat, lon, label in ((52.70, 5.29, "NL"), (40.71, -74.01, "NYC"), (-33.86, 151.21, "Sydney"), (64.14, -21.90, "Iceland")):
    obs = Observer(lat, lon, 0)
    worst = 0.0
    for i in range(200):
        d0 = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)).date()
        mine = astro.day_light(datetime(d0.year, d0.month, d0.day, tzinfo=timezone.utc), lat, lon, tz=timezone.utc)
        try:
            refs = (sunrise(obs, d0, timezone.utc), sunset(obs, d0, timezone.utc), noon(obs, d0, timezone.utc))
        except ValueError:
            continue  # polar day / polar night at this date
        for attr, ref in zip(("sunrise", "sunset", "solar_noon"), refs):
            mv = getattr(mine, attr)
            if mv and ref:
                worst = max(worst, abs((mv - ref).total_seconds()))
    print(f"  {label:8s} worst {worst:6.1f} s")

print("\n== sun position vs astral-ish: moon phase date sanity ==")
loc_tz = timezone.utc
for kind in ("new", "full"):
    cur = datetime(2026, 1, 1, tzinfo=loc_tz)
    vals = []
    for _ in range(6):
        nxt = astro.find_phase_crossing(kind, cur)
        if not nxt:
            break
        vals.append(nxt.strftime("%Y-%m-%d %H:%M"))
        cur = nxt + timedelta(hours=1)
    print(f"  {kind}: {', '.join(vals)}")
print(f"  moon distance range check done in validate_astro; mean={astro.MEAN_MOON_DISTANCE}")
