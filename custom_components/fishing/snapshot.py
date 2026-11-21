"""One computed view of the spot, as plain JSON-friendly data: what every entity reads.

Home Assistant entities should be dumb: look a value up in the latest snapshot and show it. All
the thinking happens here (and in `model/`), in one place that can be run and tested without Home
Assistant: score, confidence, advice, the 48 h curve and the best windows for one species.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .model import advice, scoring
from .model import species as sp
from .model.context import SpotContext

FEEDING_BANDS = ("fair", "good", "excellent")


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat()


def _with_utc(ctx: SpotContext, span: scoring.Span) -> dict[str, Any]:
    """A span as a dict, plus its start and end as UTC instants (entities need timezone-aware times)."""
    out = span.as_dict()
    out["start_utc"] = scoring.to_aware(ctx, span.start).astimezone(timezone.utc).isoformat()
    out["end_utc"] = scoring.to_aware(ctx, span.end).astimezone(timezone.utc).isoformat()
    return out


def compute(ctx: SpotContext, now: datetime, species_id: str, *, hours: int = 48, step_min: int = 60) -> dict[str, Any]:
    """Score `now` and the next `hours` hours for one species; nothing here raises on missing data."""
    prof = sp.profile(species_id)
    now = scoring.to_local(ctx, now)
    prepared = scoring.prepare(ctx, now, now=now)
    ev = scoring.evaluate(ctx, now, prof, prepared)
    curve = scoring.hourly_curve(ctx, now, hours, prof, step_min)
    spans = scoring.spans_from_curve(curve, 6.0, step_min)
    text = advice.as_dict(ctx, ev, prof)

    water, pa, sol = ev.water, ev.pressure, ev.solunar
    in_golden = any(
        scoring.local_event(ctx, a) <= now <= scoring.local_event(ctx, b) for a, b, _ in ctx.astronomy_for(now).golden
    )
    return {
        "when": _iso(now),
        "species": prof.id,
        "species_name": prof.name,
        "score": round(ev.score, 1),
        "band": ev.band,
        "confidence": round(ev.confidence, 2),
        "coverage": round(ev.coverage, 2),
        "headline": text["headline"],
        "reasons": text["reasons"],
        "tactics": text["tactics"],
        "window": text["window"],
        "conditions": text["conditions"],
        "notes": text["notes"],
        "factors": [f.as_dict() for f in ev.factors],
        "water": {
            "temp": None if water is None or water.temp is None else round(water.temp, 1),
            "source": None if water is None else water.source,
            "change_24h": None if water is None or water.change_24h is None else round(water.change_24h, 1),
            "trend": None if water is None else water.trend,
            "anomaly_sigma": None if water is None or water.anomaly_sigma is None else round(water.anomaly_sigma, 2),
        },
        "pressure": {
            "value": None if pa is None or pa.value is None else round(pa.value, 1),
            "tendency_3h": None if pa is None or pa.tendency_3h is None else round(pa.tendency_3h, 2),
            "trend": None if pa is None else pa.trend_label,
            "front_index": None if pa is None or pa.front_index is None else round(pa.front_index, 2),
            "front_eta_hours": None if pa is None or pa.front_eta_hours is None else round(pa.front_eta_hours, 1),
        },
        "solunar": {
            "in_window": None if sol is None else sol.in_window,
            "intensity": None if sol is None else round(sol.intensity, 2),
            "label": None if sol is None else sol.label,
        },
        "sun_altitude": round(ev.sun_altitude, 1),
        "moonshine": round(ev.moonshine, 2),
        "feeding_time": bool((sol is not None and sol.in_window) or in_golden) and ev.band in FEEDING_BANDS,
        "forecast": [{"time": _iso(t), "score": round(e.score, 1)} for t, e in curve],
        "best_windows": [_with_utc(ctx, s) for s in spans],
        "next_best": None if not spans else _with_utc(ctx, spans[0]),
        "context": ctx.summary(),
    }
