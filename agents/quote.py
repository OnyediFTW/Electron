"""quote(job, parts) -> Quote.   Owner: Person B.

labor hours x billed hourly band + parts total = low-high range.
The hourly band comes from DOL/BLS wage data for plumbers, scaled to a
billed (customer-facing) rate.
"""
from __future__ import annotations

import math

from shared.db import log_event
from shared.schema import Job, Part, Quote

# BLS OEWS, SOC 47-2152 (Plumbers, Pipefitters, and Steamfitters), May 2025,
# hourly wage percentiles. Source: BLS public API series OEUM<area>000000472152{07,09}
# (07 = 25th percentile, 09 = 75th percentile), pulled Nov 2026.
PLUMBER_WAGES = {
    # name: (lat, lng, p25, p75)
    "Trenton-Princeton": (40.2171, -74.7429, 28.93, 51.14),
    "New York-Newark-Jersey City": (40.7128, -74.0060, 29.76, 51.91),
    "Philadelphia-Camden-Wilmington": (39.9526, -75.1652, 29.28, 49.89),
}
DEFAULT_METRO = "Trenton-Princeton"
WAGE_SOURCE = "OEWS May 2025"

# ASSUMPTION: a shop bills about 2.5x the tech's wage to cover truck, tools,
# insurance, licensing, non-billable time and margin.
OVERHEAD_MULTIPLIER = 2.5

# Labor hours (low, high) by issue keyword; first match wins.
LABOR_HOURS = [
    (("water heater", "water_heater"), (2.5, 4.0)),
    (("supply line",), (0.5, 1.0)),
    (("p-trap", "trap"), (0.75, 1.5)),
    (("toilet",), (0.5, 1.5)),
    (("faucet",), (0.75, 1.5)),
    (("clog", "drain"), (1.0, 2.0)),
]
# Default by urgency when no keyword matches.
URGENCY_HOURS = {"high": (1.0, 1.5), "medium": (1.0, 2.0), "low": (0.5, 1.0)}
MIN_CONFIDENCE_FOR_LOW = 0.5


def _metro(job: Job) -> str:
    if not job.location:
        return DEFAULT_METRO
    lat, lng = job.location.lat, job.location.lng
    return min(PLUMBER_WAGES, key=lambda m: math.hypot(PLUMBER_WAGES[m][0] - lat, PLUMBER_WAGES[m][1] - lng))


def _hours(job: Job) -> tuple[float, float]:
    text = f"{job.issue or ''} {job.trade or ''}".lower()
    for keywords, hours in LABOR_HOURS:
        if any(k in text for k in keywords):
            return hours
    return URGENCY_HOURS.get(job.urgency.value if job.urgency else "medium", (1.0, 2.0))


def quote(job: Job, parts: list[Part]) -> Quote:
    metro = _metro(job)
    _, _, p25, p75 = PLUMBER_WAGES[metro]
    rate_low, rate_high = p25 * OVERHEAD_MULTIPLIER, p75 * OVERHEAD_MULTIPLIER
    h_low, h_high = _hours(job)

    # Low end counts only parts we're fairly sure of; high end counts every priced part.
    parts_low = sum(p.price for p in parts if p.price is not None and p.confidence >= MIN_CONFIDENCE_FOR_LOW)
    parts_high = sum(p.price for p in parts if p.price is not None)

    q = Quote(low=round(h_low * rate_low + parts_low), high=round(h_high * rate_high + parts_high))
    log_event(job.id, "quote",
              f"{h_low:g}-{h_high:g} h × ${rate_low:.0f}-{rate_high:.0f}/h "
              f"({metro} {WAGE_SOURCE} p25-p75 × {OVERHEAD_MULTIPLIER:g}) "
              f"+ ${parts_low:.2f}-{parts_high:.2f} parts = ${q.low:.0f}-{q.high:.0f}")
    return q
