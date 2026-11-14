"""rank_techs(job) -> list[RankedTech].   Owner: Person C.

Matching is an algorithm, not an LLM call. Keep the weights here, in one dict,
and return human-readable reasons that go straight into the tech's offer text.

Skill filter + distance + rating + workload. Still to add: calendar free/busy
in the next 48h (weight 0 until matching/calendar.py is real).
"""
import math
from collections import Counter

from shared.db import list_jobs, list_techs, log_event
from shared.schema import Job, RankedTech

WEIGHTS = {
    "skill": 40.0,      # has the job's trade
    "distance": 30.0,   # closer is better, 0 at >= MAX_MILES
    "rating": 20.0,     # 0-5 stars scaled
    "free": 0.0,        # TODO(C): free/busy next 48h
    "workload": 15.0,   # subtracted per job the tech already has (accepted or booked)
}
MAX_MILES = 25.0


def miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def rank_techs(job: Job) -> list[RankedTech]:
    here = (job.location.lat, job.location.lng) if job.location else None
    busy = Counter(j.assigned_tech_id for j in list_jobs()
                   if j.assigned_tech_id and j.status.value in ("accepted", "booked") and j.id != job.id)
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
        n = busy.get(t.id, 0)
        score -= WEIGHTS["workload"] * n
        load = "free now" if n == 0 else f"on {n} job" + ("s" if n > 1 else "")
        ranked.append(RankedTech(tech_id=t.id, score=round(score, 1),
                                 reasons=[f"{job.trade} skill", where, f"{t.rating} stars", load]))
    ranked.sort(key=lambda r: r.score, reverse=True)
    log_event(job.id, "matching", f"ranked {len(ranked)} techs, top score {ranked[0].score if ranked else '-'}")
    return ranked
