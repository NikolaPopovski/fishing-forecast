"""Water temperature: the strongest single lever on temperate sport fishing, and the variable a
weather entity will get most wrong.

Water is not air with a lag. It is a heat store with a *depth*, so the same heat wave gives
+6 degC in a drainage ditch and +1 degC in a 40 m lake. The model integrates a surface heat
budget instead of offsetting air temperature:

    dT/dt = [ film(u)*(T_air - T_w) + 0.55*SW*(1-shading) + LW_net + EVAP ] / (rho*cp*D_eff)

* `D_eff` is the depth that actually takes part - the mixed layer, not the average depth of an
  Alpine lake - and comes from the configured water-body class (plus wind mixing). This single
  term is why a small pond can move 5 degC in a sunny afternoon while a large lake takes a week
  to move 1 degC, and it is the honest answer to "what does my fishing hole's size have to do
  with the weather?".
* `film(u)` is the air-water exchange coefficient: wind-driven, deliberately *not* a free
  parameter per spot, because it is the same physics everywhere.
* `SW_absorbed` uses the provider's shortwave flux when available, otherwise a clear-sky proxy
  from the Sun altitude the model already computes and the cloud field - so it works offline too.
* `LW_net` and `EVAP` are night-time radiative cooling and wind-driven latent loss. They are why
  a bright, dry, windy night can cool a shallow flat by 2 degC and kill the morning bite even
  though "the air was mild".

Where a real reading exists (a water sensor in HA, or sea-surface temperature offshore), it sets
the *level* and the model supplies the *trend and anomaly*: the fishing-relevant statement is
"this water is 2.4 degC warmer than this spot is in mid June", which no single sensor can say.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import exp, log

from . import astronomy, response as rs
from .climatology import Climatology
from .series import Sample, at, clean, mean

RHO_CP = 4.186e6  # J per cubic metre per K (water)
STEFAN_BOLTZMANN = 5.67e-8

BODY_CLASSES: dict[str, dict] = {
    # mix_m: typical depth of the layer that actually exchanges heat (m).
    # film_mult: turbulence/wave enhancement of the air-water exchange coefficient.
    # advection: how much the body's temperature is set by incoming water (a stream's
    #            temperature is its whole catchment's, not the point's).
    # inertia: 0 = tiny/flashy, 1 = huge/slow - surfaced in the UI as "water body size".
    "ephemeral_ditch": {"mix_m": 0.20, "film_mult": 1.6, "advection": 0.55, "inertia": 0.02, "shallow": True},
    "small_pond":      {"mix_m": 0.55, "film_mult": 1.2, "advection": 0.10, "inertia": 0.08, "shallow": True},
    "shallow_stream":  {"mix_m": 0.45, "film_mult": 1.8, "advection": 0.80, "inertia": 0.12, "shallow": True},
    "wetland_flat":    {"mix_m": 0.45, "film_mult": 1.3, "advection": 0.25, "inertia": 0.15, "shallow": True},
    "river":           {"mix_m": 1.20, "film_mult": 1.8, "advection": 0.85, "inertia": 0.25, "shallow": False},
    "lake_small":      {"mix_m": 2.20, "film_mult": 1.0, "advection": 0.05, "inertia": 0.45, "shallow": False},
    "reservoir":       {"mix_m": 3.00, "film_mult": 1.0, "advection": 0.15, "inertia": 0.55, "shallow": False},
    "lake_large":      {"mix_m": 6.50, "film_mult": 0.9, "advection": 0.02, "inertia": 0.75, "shallow": False},
    "estuary":         {"mix_m": 2.50, "film_mult": 1.2, "advection": 0.45, "inertia": 0.60, "shallow": False},
    "coastal_sea":     {"mix_m": 8.00, "film_mult": 1.1, "advection": 0.40, "inertia": 0.85, "shallow": False},
    "open_sea":        {"mix_m": 25.0, "film_mult": 1.1, "advection": 0.55, "inertia": 1.00, "shallow": False},
}


@dataclass
class WaterBody:
    """The spot's water body: class plus the few local knobs that genuinely matter."""

    kind: str = "lake_small"
    mean_depth_m: float | None = None
    flow_index: float = 0.0        # 0 still .. 1 torrent (overrides the class default advection)
    shading_index: float = 0.0     # 0 open .. 1 permanently shaded by trees or a canyon
    notes: list[str] = field(default_factory=list)

    @property
    def spec(self) -> dict:
        return BODY_CLASSES.get(self.kind, BODY_CLASSES["lake_small"])

    @property
    def inertia(self) -> float:
        return float(self.spec["inertia"])

    @property
    def is_shallow(self) -> bool:
        return bool(self.spec["shallow"])

    @property
    def advection(self) -> float:
        """Weight given to the catchment's lagged air temperature (flowing water)."""
        base = float(self.spec["advection"])
        return max(base, min(0.95, float(self.flow_index)))

    @property
    def mixed_layer_m(self) -> float:
        mix = float(self.spec["mix_m"])
        if self.mean_depth_m:
            mix = min(mix, max(0.15, float(self.mean_depth_m)))
        return max(0.15, mix)


@dataclass
class HeatBudget:
    """Per-step diagnostics, exposed so the card can explain the water temperature to a human."""

    when: datetime
    air_temp: float
    water_temp: float
    shortwave_wm2: float
    net_longwave_wm2: float
    evaporative_wm2: float
    sensible_wm2: float
    deep_reservoir_c: float
    deep_rate_k_per_h: float
    mixed_layer_m: float
    rate_k_per_h: float


def shortwave_proxy(when: datetime, latitude: float, longitude: float, cloud_cover: float | None, elevation_m: float = 0.0) -> float:
    """Clear-sky W/m2 on a horizontal surface from the Sun's altitude, degraded by cloud.

    Deliberately crude (no airmass fit, no aerosols): it only needs the right sign and order of
    magnitude, and it must never invent a gain the Sun cannot deliver.
    """
    alt = astronomy.sun_altitude(when, latitude, longitude)
    if alt <= 0.0:
        return 0.0
    transmittance = 0.75 if elevation_m < 1500 else 0.83
    cloud = 0.0 if cloud_cover is None else max(0.0, min(1.0, float(cloud_cover)))
    return max(0.0, 1361.0 * astronomy.sin_deg(alt) * transmittance * (1.0 - 0.85 * cloud))


def net_longwave(water_temp: float, air_temp: float, dewpoint_c: float | None, cloud: float) -> float:
    """Upward-minus-downward longwave loss, W/m2 (positive means the water is *losing* heat).

    Shaped like the real thing without pretending to a radiation scheme: emission goes as T^4,
    cloud and humidity act as blankets, so a clear dry night can pull 90 W/m2 out of a shallow
    flat while a muffled overcast night pulls almost none.
    """
    t_w = water_temp + 273.15
    td_c = air_temp - 4.0 if dewpoint_c is None else float(dewpoint_c)
    t_sky = td_c + 273.15
    # Brutsaert-style clear-sky emissivity from the vapour pressure, then cloud mixing: a full
    # overcast deck radiates like a blackbody at the dewpoint, which is exactly why an overcast
    # maritime winter night barely cools a lake at all, while a clear continental one can.
    e_a = _saturation_vp(td_c)
    emissivity_clear = min(0.95, 0.7 + 5.95e-5 * e_a)
    c = max(0.0, min(1.0, cloud))
    emissivity_sky = emissivity_clear * (1.0 - 0.84 * c) + 0.84 * c
    return STEFAN_BOLTZMANN * (_pow4(t_w) - emissivity_sky * _pow4(t_sky))


def _pow4(t_k: float) -> float:
    t2 = t_k * t_k
    return t2 * t2


def evaporative_flux(water_temp: float, air_temp: float, humidity: float | None, wind_ms: float | None) -> float:
    """Latent heat loss, W/m2 (positive = loss), from a bulk aerodynamic humidity deficit."""
    es_w = _saturation_vp(water_temp)
    rh = 0.4 if humidity is None else max(0.05, min(1.0, float(humidity)))
    es_a = _saturation_vp(_dewpoint(air_temp, rh))
    u = 2.5 if wind_ms is None else max(0.3, float(wind_ms))
    ce = 1.2e-3 + 1.4e-3 * min(1.0, u / 10.0)
    return ce * u * (es_w - es_a) * 2.45e6 / 101325.0


def sensible_flux(water_temp: float, air_temp: float, wind_ms: float | None, film_mult: float) -> float:
    """Sensible + conductive air-water exchange, W/m2 (positive = water gains heat)."""
    u = 2.5 if wind_ms is None else max(0.3, float(wind_ms))
    film = (1.2 + 2.4 * u) * film_mult
    return film * (air_temp - water_temp)


def _saturation_vp(temp_c: float) -> float:
    return 610.94 * exp(17.625 * temp_c / (temp_c + 243.04))


def _dewpoint(temp_c: float, rh: float) -> float:
    rh = max(0.05, min(1.0, rh))
    a, b = 17.625, 243.04
    gamma = log(rh) + a * temp_c / (b + temp_c)
    return b * gamma / (a - gamma)


#: the air-side filter: soil and groundwater are a much deeper low-pass filter than the water
#: body itself, so air is smoothed twice (see docs "water temperature model")
AIR_SLOW_TAU_DAYS = 30.0


def deep_reservoir_tau_days(inertia: float) -> float:
    """Time constant of the slow heat store: sediment, groundwater and (for big lakes) the water
    below the seasonal thermocline.

    Its input is the air temperature filtered twice, because that is what soil does: deep
    groundwater sits near the *annual* air mean with only a couple of degrees of swing. Without
    this store a single-layer model tracks a February frost too faithfully and reports 0 degC,
    which is exactly the kind of error this module exists to avoid - and it is also the reason a
    shallow basin is already 8 degC in March while the air is still at 5.
    """
    return 30.0 + 55.0 * inertia


def deep_coupling_per_h(inertia: float) -> float:
    """Exchange rate between the surface layer and that slow store (1/h).

    Shallow, fast water sits on warm mud and exchanges quickly; a large lake touches it only
    through the mixed layer. Deliberately expressed as a rate rather than a conductance so the
    term does not fight with the mixed-layer depth (double counting the same physics).
    """
    return 0.0060 * (1.0 - 0.75 * inertia) + 0.0008


def stratification_factor(water_temp: float, is_shallow: bool) -> float:
    """How much of the nominal mixed layer actually moves seasonally.

    A single-layer model with one fixed depth cannot be right all year: a large lake mixes to
    15-20 m in a stormy March but holds a warm skin on top of a cold thermocline in August, so
    its *effective* layer in summer is a couple of metres. Without this, a one-layer model is
    far too cold in summer and that error propagates straight into every water-temperature
    factor. The factor shrinks as the surface warms past ~8 degC and returns to full mixing in
    autumn/winter; shallow basins mix to the bottom all year.
    """
    if is_shallow:
        return 1.0
    if water_temp <= 8.0:
        return 1.0
    return max(0.18, 1.0 - 0.055 * (water_temp - 8.0))


def inverse_stratification_factor(water_temp: float, mixed_layer_m: float) -> float:
    """Below 4 degC fresh water is stably stratified *upside down*, so the cold skin decouples
    from the water (and sediment) underneath and a deep pool keeps ~4 degC while a shallow bay
    freezes. A single mixed layer cannot express that unless the effective depth collapses as the
    surface cools - which is what this does. Ignoring it makes winter water temperatures far too
    cold, and winter is when the difference between 1 degC and 4 degC decides whether a slow-food
    species feeds at all."""
    if mixed_layer_m < 1.0:
        return 1.0                      # too shallow to hold an inverse gradient: it freezes
    cold = max(0.0, min(1.0, (4.0 - water_temp) / 3.5))
    depth_factor = min(1.0, mixed_layer_m / 6.0)
    return max(0.12, 1.0 - (1.0 - 0.12) * cold * depth_factor)


def catchment_temperature(air: list[Sample], when: datetime, inertia: float) -> float | None:
    """Lagged air mean: the temperature flowing water arrives with (its catchment's average).

    A stream at the foot of a 300 m valley is not at the local afternoon air temperature; it is
    at the mean of the whole catchment over the last day or two. The body's inertia sets the
    lookback window.
    """
    window = 18.0 + 150.0 * inertia
    return mean(air, when, window)


def model_series(
    air_temp_hourly: list[Sample],
    body: WaterBody,
    latitude: float,
    longitude: float,
    *,
    start_water_temp: float | None = None,
    start_at: datetime | None = None,
    shortwave: list[Sample] | None = None,
    cloud: list[Sample] | None = None,
    humidity: list[Sample] | None = None,
    wind_speed: list[Sample] | None = None,
    dewpoint: list[Sample] | None = None,
    elevation_m: float = 0.0,
    steps_per_day: int = 24,
) -> tuple[list[Sample], list[HeatBudget]]:
    """Integrate the heat budget over the supplied air series (ideally >= 30 days of it).

    Returns (water series, diagnostics). With no *start_water_temp* the model seeds itself from
    the air series, which is how a fresh config entry gets a usable water temperature on day one.

    *cloud* and *humidity* are percent (0-100), like every other series in the model; *wind_speed*
    is m/s, *shortwave* W/m2, *dewpoint* degC.
    """
    air = clean(air_temp_hourly)
    if len(air) < 24:
        return [], []
    shortwave = clean(shortwave or [])
    cloud_s = clean(cloud or [])
    humidity_s = clean(humidity or [])
    wind_s = clean(wind_speed or [])
    dew_s = clean(dewpoint or [])
    spec = body.spec

    if start_water_temp is None:
        seed = mean(air, air[0][0] + timedelta(hours=1), 72.0 + 240.0 * body.inertia)
        if seed is None:
            head = [v for _, v in air[: min(len(air), 48)]]
            seed = sum(head) / len(head)
        t_w = seed - 0.8 * body.inertia
    else:
        t_w = float(start_water_temp)

    start = start_at or air[0][0]
    series: list[Sample] = []
    diag: list[HeatBudget] = []
    prev_when: datetime | None = None
    # the slow store, seeded at the annual air mean of the supplied series
    t_deep = mean(air, air[-1][0], 24.0 * 365.0) or t_w
    t_air_slow = t_deep
    tau_deep_h = 24.0 * deep_reservoir_tau_days(body.inertia)
    tau_air_h = 24.0 * AIR_SLOW_TAU_DAYS
    k_deep = deep_coupling_per_h(body.inertia)

    for when, air_t in [(t, v) for t, v in air if t >= start]:
        dt_h = 1.0 if prev_when is None else max(0.05, min(6.0, (when - prev_when).total_seconds() / 3600.0))
        prev_when = when
        # the project's series convention is percent (0-100); the flux functions work in fractions
        # and clamp to 0..1, so un-converted percent would read as "fully overcast" for any cloud
        cl = at(cloud_s, when, 3.0)
        cl = None if cl is None else cl / 100.0
        sw = at(shortwave, when, 3.0)
        if sw is None:
            sw = shortwave_proxy(when, latitude, longitude, cl, elevation_m)
        rh = at(humidity_s, when, 3.0)
        rh = None if rh is None else rh / 100.0
        u = at(wind_s, when, 3.0)
        td = at(dew_s, when, 3.0)

        # the mixed layer deepens with wind mixing and shoals under a calm, sunny sky
        wind_for_mix = 2.0 if u is None else max(0.3, float(u))
        d_eff = body.mixed_layer_m * (0.45 + 0.55 * min(1.0, (wind_for_mix / 8.0) ** 0.5))
        if body.is_shallow and body.mean_depth_m:
            d_eff = min(d_eff, max(0.15, float(body.mean_depth_m)))
        d_eff = max(0.15, d_eff * stratification_factor(t_w, body.is_shallow))
        cold_factor = inverse_stratification_factor(t_w, body.mixed_layer_m)
        d_eff = max(0.15, d_eff * cold_factor)

        # the slow store relaxes toward the recent air mean (exponentially, so gaps are safe)
        air_recent = mean(air, when, 48.0) or air_t
        t_air_slow += (air_recent - t_air_slow) * (1.0 - exp(-dt_h / tau_air_h))
        t_deep += (t_air_slow - t_deep) * (1.0 - exp(-dt_h / tau_deep_h))
        # under ice or an inverse layer the exchange with the store below is proportionally more
        # important, because the volume being cooled is tiny
        deep_rate = k_deep * (1.0 + 4.0 * (1.0 - cold_factor)) * (t_deep - t_w)

        sw_abs = 0.55 * sw * (1.0 - 0.6 * max(0.0, min(1.0, body.shading_index)))
        lw = -net_longwave(t_w, air_t, td, 0.0 if cl is None else cl)   # sign: gain positive
        ev = -evaporative_flux(t_w, air_t, rh, u)
        sens = sensible_flux(t_w, air_t, u, float(spec["film_mult"]))
        flux = sw_abs + lw + ev + sens
        rate = flux * 3600.0 / (RHO_CP * d_eff) + deep_rate   # K per hour

        t_w += rate * dt_h
        # advection: flowing water is dragged toward its catchment's lagged temperature
        adv = body.advection
        if adv > 0:
            up = catchment_temperature(air, when, body.inertia)
            if up is not None:
                blend = min(0.95, adv * (dt_h / (dt_h + 3.0)))
                t_w = t_w * (1.0 - blend) + up * blend
        # phase limits: liquid water stays in its window; the model does not simulate ice or
        # deep winter mixing, which the integration surfaces as a limitation instead of a lie
        t_w = max(0.0, min(42.0, t_w))

        series.append((when, round(t_w, 3)))
        diag.append(
            HeatBudget(
                when=when,
                air_temp=air_t,
                water_temp=t_w,
                shortwave_wm2=sw,
                net_longwave_wm2=lw,
                evaporative_wm2=ev,
                sensible_wm2=sens,
                deep_reservoir_c=t_deep,
                deep_rate_k_per_h=deep_rate,
                mixed_layer_m=d_eff,
                rate_k_per_h=rate,
            )
        )
    return series, diag


@dataclass
class WaterState:
    when: datetime
    temp: float | None
    source: str                     # sensor | sst | model | sensor_stale | none
    change_24h: float | None
    change_7d: float | None
    anomaly_sigma: float | None
    anomaly_raw: float | None
    normal: float | None
    trend: str
    shock: float | None = None
    score: float = 0.0
    species_offset_c: float | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "when": self.when.isoformat(),
            "temp": self.temp,
            "source": self.source,
            "change_24h": self.change_24h,
            "change_7d": self.change_7d,
            "anomaly_sigma": self.anomaly_sigma,
            "anomaly_raw": self.anomaly_raw,
            "normal": self.normal,
            "trend": self.trend,
            "shock": self.shock,
            "score": self.score,
            "species_offset_c": self.species_offset_c,
            "notes": self.notes,
        }


def fuse_water_temp(
    sensor: float | None,
    sea_surface_temp: float | None,
    modelled: float | None,
    *,
    sensor_age_hours: float = 0.0,
    sensor_max_age_hours: float = 30.0,
) -> tuple[float | None, str]:
    """Pick the best available water temperature with a precedence the UI can state out loud.

    A direct reading at the spot beats a satellite pixel tens of kilometres away, which beats the
    model - but a stale sensor is worse than the model, so age is part of the rule, not a hope.
    """
    if sensor is not None and sensor_age_hours <= sensor_max_age_hours:
        return float(sensor), "sensor"
    if sea_surface_temp is not None:
        return float(sea_surface_temp), "sst"
    if modelled is not None:
        return float(modelled), "model"
    if sensor is not None:
        return float(sensor), "sensor_stale"
    return None, "none"


def analyse(
    series: list[Sample],
    when: datetime,
    *,
    clim: Climatology | None = None,
    fused_value: float | None = None,
    source: str = "model",
    species_optimum_c: float | None = None,
) -> WaterState:
    """Level + trend + anomaly for the fishing-relevant question: is this water *normal*?"""
    series = clean(series)
    notes: list[str] = []
    temp = fused_value if fused_value is not None else at(series, when, max_age_hours=12.0)
    if temp is None:
        return WaterState(when, None, "none", None, None, None, None, None, "unknown", None, 0.0, None, ["no water temperature available"])

    prev24 = at(series, when - timedelta(hours=24), max_age_hours=8.0)
    prev7d = at(series, when - timedelta(days=7), max_age_hours=36.0)
    change_24h = None if prev24 is None else temp - prev24
    change_7d = None if prev7d is None else temp - prev7d
    if source in ("sensor", "sst") and len(series) >= 3:
        notes.append("level from external source; trend and anomaly from the modelled series")

    nrm = clim.normal("water_temp", when) if clim else None
    anomaly = clim.anomaly("water_temp", temp, when) if clim else None
    anomaly_raw = clim.anomaly_raw("water_temp", temp, when) if clim else None
    if clim is not None and clim.coverage("water_temp") == 0:
        notes.append("no water climatology yet (needs ~10 days of values)")

    trend = "steady"
    if change_7d is not None:
        if change_7d > 1.6:
            trend = "warming fast"
        elif change_7d > 0.4:
            trend = "warming"
        elif change_7d < -1.6:
            trend = "cooling fast"
        elif change_7d < -0.4:
            trend = "cooling"

    shock = None
    if change_24h is not None and abs(change_24h) >= 2.5:
        shock = change_24h
        notes.append(f"thermal shock: {change_24h:+.1f} degC in 24 h")

    offset = None if species_optimum_c is None else temp - species_optimum_c
    parts: dict[str, float] = {}
    if offset is not None:
        parts["suitability"] = rs.WATER_TEMP_SUITABILITY(offset)
    if change_7d is not None:
        parts["change"] = rs.WATER_TEMP_CHANGE_7D(change_7d)
    score = 0.0
    if parts:
        weights = {"suitability": 0.62, "change": 0.38}
        used = sum(weights[k] for k in parts)
        score = sum(weights[k] * v for k, v in parts.items()) / used

    return WaterState(
        when=when,
        temp=temp,
        source=source,
        change_24h=change_24h,
        change_7d=change_7d,
        anomaly_sigma=anomaly,
        anomaly_raw=anomaly_raw,
        normal=None if nrm is None else nrm.value,
        trend=trend,
        shock=shock,
        score=score,
        species_offset_c=offset,
        notes=notes,
    )
