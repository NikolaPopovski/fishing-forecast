"""Ad-hoc validation of the hand-rolled ephemeris against independent references."""
import sys, json, urllib.request
from datetime import datetime, timezone, timedelta
sys.path.insert(0, "custom_components/fishing")
from model import astronomy as astro


def ref_sun(lat, lon, date_str):
    url = f"https://api.sunrise-sunset.org/json?lat={lat}&lng={lon}&date={date_str}&tzid=UTC"
    with urllib.request.urlopen(url, timeout=25) as r:
        return json.loads(r.read())["results"]


def parse_utc(hhmmss_ampm, date_str):
    dt = datetime.strptime(f"{date_str} {hhmmss_ampm}", "%Y-%m-%d %I:%M:%S %p")
    return dt.replace(tzinfo=timezone.utc)


CASES = [(52.70, 5.29, "2026-06-21"), (52.70, 5.29, "2026-12-21"), (40.71, -74.01, "2026-03-20"),
         (-33.86, 151.21, "2026-07-15"), (64.14, -21.90, "2026-06-21"), (78.22, 15.65, "2026-03-09")]

print("== SUN vs sunrise-sunset.org (UTC) ==")
worst = 0
for lat, lon, ds in CASES:
    ref = ref_sun(lat, lon, ds)
    day = datetime.strptime(ds, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    mine = astro.day_light(day, lat, lon, tz=timezone.utc)
    for key, attr in (("sunrise", "sunrise"), ("sunset", "sunset"), ("solar_noon", "solar_noon"),
                      ("civil_twilight_begin", "dawn"), ("civil_twilight_end", "dusk"),
                      ("nautical_twilight_begin", "nautical_dawn"), ("nautical_twilight_end", "nautical_dusk")):
        rv = ref.get(key)
        mv = getattr(mine, attr)
        if rv in (None, "nan") or mv is None:
            print(f"  {ds} {lat},{lon} {key}: ref=none mine={mv}")
            continue
        d = abs((mv - parse_utc(rv, ds)).total_seconds())
        worst = max(worst, d)
        print(f"  {ds} {lat:6.2f},{lon:7.2f} {key:24s} ref={rv:>12s} mine={mv.strftime('%H:%M:%S')}  diff={d:6.1f}s")
print("worst diff %.1f s" % worst)

print("\n== PHASES: 2026 full/new moons (UTC) ==")
t = datetime(2026, 1, 1, tzinfo=timezone.utc)
for kind in ("full", "new"):
    out = []
    cur = t
    for _ in range(13):
        nxt = astro.find_phase_crossing(kind, cur)
        if not nxt:
            break
        out.append(nxt.strftime("%m-%d %H:%M"))
        cur = nxt + timedelta(minutes=30)
    print(f"  {kind:5s}: " + ", ".join(out))

print("\n== sanity: J2000 known new moon 2000-01-06 18:14 UTC ==")
print("  computed:", astro.find_phase_crossing("new", datetime(2000, 1, 1, tzinfo=timezone.utc)))

print("\n== moon distance / declination extremes over 2 years ==")
dmin = dmax = None
decmin = decmax = None
jd = astro.to_jd(datetime(2026, 1, 1, tzinfo=timezone.utc))
end = jd + 730
step = 0.05
while jd < end:
    m = astro.moon_position(jd)
    if dmin is None or m.dist_km < dmin:
        dmin = m.dist_km
    if dmax is None or m.dist_km > dmax:
        dmax = m.dist_km
    if decmin is None or m.dec < decmin:
        decmin = m.dec
    if decmax is None or m.dec > decmax:
        decmax = m.dec
    jd += step
print(f"  distance {dmin:,.0f} .. {dmax:,.0f} km  (real: ~356,000 .. 406,700)")
print(f"  declination {decmin:+.2f} .. {decmax:+.2f} deg (real: +/-28.7 at standstill)")

print("\n== moonrise/set sanity: full moon rises near sunset ==")
fm = astro.find_phase_crossing("full", datetime(2026, 6, 1, tzinfo=timezone.utc))
day = (fm - timedelta(hours=12)).replace(hour=0, minute=0, second=0, microsecond=0)
lat, lon = 52.70, 5.29
mr = astro.body_events(day, lat, lon, moon=True, tz=timezone.utc)
dl = astro.day_light(day, lat, lon, tz=timezone.utc)
print(f"  full moon {fm}, day {day.date()}")
print(f"  moonrise {mr.rise}  sunset {dl.sunset}  moon transit {mr.transit}")
print(f"  phase: {astro.moon_phase_info(astro.to_jd(fm))}")

print("\n== tide state sweep (North Sea, 1 spring month) ==")
t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
for h in range(0, 24 * 15, 24 * 3):
    ts = t0 + timedelta(hours=h)
    t = astro.tide_state(ts, 4.0)
    print(f"  {ts:%m-%d %H:%M} level={t.level:+.2f} range={t.range_index:.2f} forcing={t.forcing:.3f} {t.label}")

print("\n== timing: 99 days of events ==")
import time
t0 = time.time()
day = datetime(2026, 8, 1, tzinfo=timezone.utc)
for i in range(99):
    d = day + timedelta(days=i)
    astro.day_light(d, 52.7, 5.29)
    astro.body_events(d, 52.7, 5.29, moon=True)
print(f"  99 days sun+moon events in {time.time()-t0:.2f}s")
