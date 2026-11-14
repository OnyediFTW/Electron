"""rank_techs(job) -> list[RankedTech].   Owner: Person C.

Matching is an algorithm, not an LLM call. Keep the weights here, in one dict,
and return human-readable reasons that go straight into the tech's offer text.

STUB: skill filter + distance + rating only. Still to add: calendar
free/busy in the next 48h and current open jobs.
"""
import math

from shared.db import list_techs, log_event
from shared.schema import Job, RankedTech

WEIGHTS = {
    "skill": 40.0,      # has the job's trade
    "distance": 30.0,   # closer is better, 0 at >= MAX_MILES
    "rating": 20.0,     # 0-5 stars scaled
    "free": 0.0,        # TODO(C): free/busy next 48h
    "workload": 0.0,    # TODO(C): fewer open jobs is better
}
MAX_MILES = 25.0


def miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def rank_techs(job: Job) -> list[RankedTech]:
    here = (job.location.lat, job.location.lng) if job.location else None
    ranked = []
    for t in list_techs():
        if job.trade and job.trade not in t.skills:
            continue
        score = WEIGHTS["skill"] + WEIGHTS["rating"] * t.rating / 5
        if here:
            d = miles(here, (t.lat, t.lng))
            score += WEIGHTS["distance"] * max(0.0, 1 - d / MAX_MILES)
            where = f"{d:.1f} mi away"
        else:
            where = "distance unknown"
        ranked.append(RankedTech(tech_id=t.id, score=round(score, 1),
                                 reasons=[f"{job.trade} skill", where, f"{t.rating} stars"]))
    ranked.sort(key=lambda r: r.score, reverse=True)
    log_event(job.id, "matching", f"ranked {len(ranked)} techs, top score {ranked[0].score if ranked else '-'}")
    return ranked
