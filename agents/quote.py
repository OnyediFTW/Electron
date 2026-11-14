"""quote(job, parts) -> Quote.   Owner: Person B.

STUB: fixed labor-hours x hourly band + parts total. Replace the band with
DOL OEWS wages for plumbers (SOC 47-2152) in the 2-3 demo metros.
"""
from shared.db import log_event
from shared.schema import Job, Part, Quote

# STUB numbers. Billed rate, not the tech's wage: swap in DOL band x markup.
HOURLY_BAND = (95.0, 140.0)
LABOR_HOURS = {"high": (1.0, 1.5), "medium": (1.0, 2.0), "low": (0.5, 1.0)}


def quote(job: Job, parts: list[Part]) -> Quote:
    hours = LABOR_HOURS.get(job.urgency.value if job.urgency else "medium", (1.0, 2.0))
    parts_total = sum(p.price or 0 for p in parts)
    q = Quote(low=round(hours[0] * HOURLY_BAND[0] + parts_total),
              high=round(hours[1] * HOURLY_BAND[1] + parts_total))
    log_event(job.id, "quote", f"STUB quote ${q.low:.0f}-${q.high:.0f} (parts ${parts_total:.2f})")
    return q
