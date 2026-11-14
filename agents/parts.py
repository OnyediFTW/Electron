"""find_parts(job) -> list[Part].   Owner: Person B.

STUB: hardcoded catalog. Replace with eBay Browse API and/or SerpAPI
(cheapest match per part, returned as links). Never auto-buy.
Keep the catalog as the FALLBACK if the APIs fail.
"""
from shared.db import log_event
from shared.schema import Job, Part

FALLBACK_CATALOG = {
    "1-1/2 in P-trap kit": (8.97, "https://www.homedepot.com/s/p-trap%201-1%2F2"),
    "1-1/2 in slip-joint washer": (2.48, "https://www.homedepot.com/s/slip%20joint%20washer"),
}


def find_parts(job: Job) -> list[Part]:
    out = []
    for p in job.parts:
        price, url = FALLBACK_CATALOG.get(p.name, (None, None))
        out.append(p.model_copy(update={"price": price, "url": url}))
    log_event(job.id, "parts", f"STUB catalog matched {sum(p.price is not None for p in out)}/{len(out)} parts")
    return out
