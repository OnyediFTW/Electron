"""quote(job, parts) -> Quote | None.   Owner: Person B.

STUB: returns None (no estimate) until the real quote ships, so nobody is shown
a made-up price. Replace with labor hours x DOL OEWS wage band for plumbers
(SOC 47-2152) in the demo metros, plus parts total, as a low-high range.
"""
from typing import Optional

from shared.db import log_event
from shared.schema import Job, Part, Quote


def quote(job: Job, parts: list[Part]) -> Optional[Quote]:
    log_event(job.id, "quote", "STUB: no price estimate yet")
    return None
