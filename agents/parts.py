"""find_parts(job) -> list[Part].   Owner: Person B.

STUB: no parts lookup yet, so it returns the job's parts unchanged (usually
none). Replace with eBay Browse API and/or SerpAPI (cheapest match per part,
returned as links). Never auto-buy.
"""
from shared.db import log_event
from shared.schema import Job, Part


def find_parts(job: Job) -> list[Part]:
    log_event(job.id, "parts", "STUB: parts lookup not connected yet")
    return list(job.parts)
