"""rank_techs(job) -> list[RankedTech].   Owner: Person C.

Matching is an algorithm, not an LLM call. Keep the weights here, in one dict,
and return human-readable reasons that go straight into the tech's offer text.

Score = weighted sum of: skill, distance, rating, availability (next 48h),
and fair share (jobs taken in the last 24h + offers waiting on them now), so work is
spread evenly: after a plumber takes a job, the next one leans to someone else.
High-urgency jobs weight availability more.
"""
import math
from datetime import datetime, timedelta, timezone

from matching.calendar import free_slots
from shared import db
from shared.db import list_techs, log_event
from shared.schema import Job, JobStatus, RankedTech, Urgency

WEIGHTS = {
    "skill": 30.0,      # has the job's trade
    "distance": 25.0,   # closer is better, 0 at >= MAX_MILES
    "rating": 10.0,     # 0-5 stars scaled
    "free": 20.0,       # sooner first opening in the next 48h is better
    "workload": 15.0,   # fair share: fewer jobs in the last 24h (and pending offers) is better
}
FAIR_SHARE_HOURS = 24.0
# A general plumber can do drain, water heater and sewer jobs, at part of the skill points,
# so specialists still rank higher but nobody qualified is left out. Gas and electrical stay strict.
GENERALIST_COVERS = {"drain", "water_heater", "sewer"}
GENERALIST_SKILL = 2 / 3
FAIR_SHARE_STEP = 6.0   # points lost per job today: more than the gap between two nearby plumbers
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

    # Fair share: jobs each tech took in the last 24h, plus offers waiting on them right now
    # (so two jobs arriving together don't both go to the same person). Not this job.
    since = datetime.now(timezone.utc) - timedelta(hours=FAIR_SHARE_HOURS)
    open_counts: dict[str, int] = {}
    for r in db.store.select("jobs"):
        created = r.get("created_at")
        recent = created is None or datetime.fromisoformat(str(created)) >= since
        if r["id"] != job.id and r.get("assigned_tech_id") and r["status"] in {s.value for s in OPEN_STATUSES} and recent:
            open_counts[r["assigned_tech_id"]] = open_counts.get(r["assigned_tech_id"], 0) + 1
    offered_jobs = {r["id"] for r in db.store.select("jobs", status=JobStatus.offered.value) if r["id"] != job.id}
    for o in db.store.select("offers"):
        if o["job_id"] in offered_jobs and not o.get("response"):
            open_counts[o["tech_id"]] = open_counts.get(o["tech_id"], 0) + 1

    ranked = []
    for t in list_techs():
        if not job.trade or job.trade in t.skills:
            score, skill = w["skill"], f"{job.trade} skill"
        elif job.trade in GENERALIST_COVERS and "plumbing" in t.skills:
            score, skill = w["skill"] * GENERALIST_SKILL, f"general plumber, not a {job.trade} specialist"
        else:
            continue
        score += w["rating"] * t.rating / 5

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
        score += max(-w["workload"], w["workload"] - FAIR_SHARE_STEP * n_open)  # keeps rotating past 2 jobs

        ranked.append(RankedTech(
            tech_id=t.id,
            score=round(score, 1),
            reasons=[skill, where, f"{t.rating} stars", when,
                     f"{n_open} job{'' if n_open == 1 else 's'} today"],
        ))
    ranked.sort(key=lambda r: r.score, reverse=True)
    names = {t.id: t.name for t in list_techs()}
    lines = [f"{i}. {names.get(r.tech_id, r.tech_id)}  {r.score}  ({', '.join(r.reasons)})"
             for i, r in enumerate(ranked[:5], 1)]
    log_event(job.id, "ranking", "\n".join(lines))
    log_event(job.id, "matching", f"ranked {len(ranked)} techs, top score {ranked[0].score if ranked else '-'}")
    return ranked