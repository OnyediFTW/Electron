"""rank_techs(job) -> list[RankedTech].   Owner: Person C.

Matching is an algorithm, not an LLM call. Keep the weights here, in one dict,
and return human-readable reasons that go straight into the tech's offer text.

Score = weighted sum of: skill, distance, rating, availability (next 48h),
and workload (open jobs). High-urgency jobs weight availability more.
"""
import math
from datetime import datetime

from matching.calendar import free_slots
from shared.db import list_jobs, list_techs, log_event
from shared.schema import Job, JobStatus, RankedTech, Urgency

WEIGHTS = {
    "skill": 30.0,      # has the job's trade
    "distance": 25.0,   # closer is better, 0 at >= MAX_MILES
    "rating": 15.0,     # 0-5 stars scaled
    "free": 20.0,       # sooner first opening in the next 48h is better
    "workload": 10.0,   # fewer open jobs is better
}
URGENT_FREE_MULTIPLIER = 1.5   # high urgency leans on availability
MAX_MILES = 25.0
HORIZON_HOURS = 48.0
OPEN_STATUSES = {JobStatus.accepted, JobStatus.booked}


def miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def rank_techs(job: Job) -> list[RankedTech]:
    here = (job.location.lat, job.location.lng) if job.location else None
    w = dict(WEIGHTS)
    if job.urgency == Urgency.high:
        w["free"] *= URGENT_FREE_MULTIPLIER

    # Count each tech's current jobs once, excluding the job being ranked.
    open_counts: dict[str, int] = {}
    for j in list_jobs():
        if j.id != job.id and j.status in OPEN_STATUSES and j.assigned_tech_id:
            open_counts[j.assigned_tech_id] = open_counts.get(j.assigned_tech_id, 0) + 1

    ranked = []
    for t in list_techs():
        if job.trade and job.trade not in t.skills:
            continue
        score = w["skill"] + w["rating"] * t.rating / 5

        if here:
            d = miles(here, (t.lat, t.lng))
            score += w["distance"] * max(0.0, 1 - d / MAX_MILES)
            where = f"{d:.1f} mi away"
        else:
            where = "distance unknown"

        slots = free_slots(t.id)
        if slots:
            first = slots[0].start
            hrs = max(0.0, (first - datetime.now(first.tzinfo)).total_seconds() / 3600)
            score += w["free"] * max(0.0, 1 - hrs / HORIZON_HOURS)
            when = f"free in {hrs:.0f}h"
        else:
            when = "no openings in 48h"

        n_open = open_counts.get(t.id, 0)
        score += w["workload"] / (1 + n_open)

        ranked.append(RankedTech(
            tech_id=t.id,
            score=round(score, 1),
            reasons=[f"{job.trade} skill", where, f"{t.rating} stars", when, f"{n_open} open jobs"],
        ))
    ranked.sort(key=lambda r: r.score, reverse=True)
    names = {t.id: t.name for t in list_techs()}
    lines = [f"{i}. {names.get(r.tech_id, r.tech_id)}  {r.score}  ({', '.join(r.reasons)})"
             for i, r in enumerate(ranked[:5], 1)]
    log_event(job.id, "ranking", "\n".join(lines))
    log_event(job.id, "matching", f"ranked {len(ranked)} techs, top score {ranked[0].score if ranked else '-'}")
    return ranked