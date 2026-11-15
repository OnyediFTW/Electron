"""Owner's view API (read-only). Owner: Person C. Mounted in main.py.

/api/owner/summary     numbers for the top of the dashboard + "needs you" list
/api/owner/associates  incoming customers -> associates -> each associate's customers
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from shared import db

router = APIRouter(prefix="/api/owner")

OPEN = {"new", "triaged", "offered"}
WORKING = {"accepted", "booked"}
OFFER_TIMEOUT = float(os.getenv("OFFER_TIMEOUT_SECONDS", "60"))


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _issue(j: dict) -> str:
    return j.get("issue") or (j.get("raw_text") or "").split("\n")[0][:80] or "(no description)"


def _job_card(j: dict, names: dict) -> dict:
    return {"id": j["id"], "customer": j["customer_phone"], "issue": _issue(j), "status": j["status"],
            "urgency": j.get("urgency"), "quote": j.get("quote"), "created_at": j.get("created_at"),
            "tech": names.get(j.get("assigned_tech_id"))}


def _needs(jobs: list[dict], offers: list[dict], now: datetime) -> dict[str, str]:
    """job id -> what the owner should do about it."""
    day_ago = now - timedelta(hours=24)
    out = {}
    for j in jobs:
        created = _ts(j.get("created_at")) or now
        if j["status"] == "failed" and created >= day_ago:
            out[j["id"]] = ("Customer declined the estimate. Call to win it back?" if j.get("assigned_tech_id")
                            else "No tech took it. Call the customer.")
        elif j["status"] == "new" and now - created > timedelta(minutes=5):
            out[j["id"]] = "Waiting on the customer's answer for over 5 min."
        elif j["status"] == "offered":
            pending = [o for o in offers if o["job_id"] == j["id"] and not o.get("response")]
            if pending and now - _ts(pending[-1]["sent_at"]) > timedelta(seconds=OFFER_TIMEOUT * 3):
                out[j["id"]] = "Offer has gone unanswered for a while."
        elif j["status"] == "accepted" and now - created > timedelta(minutes=30):
            out[j["id"]] = "Customer hasn't OK'd the estimate yet. Follow up?"
    return out


@router.get("/board")
def board():
    """Jobs grouped into the overview's columns. Dropped only shows the last 24 hours."""
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)
    jobs = db.store.select("jobs")
    offers = db.store.select("offers")
    names = {t.id: t.name for t in db.list_techs()}
    needs = _needs(jobs, offers, now)
    cols: dict[str, list] = {"waiting": [], "offered": [], "taken": [], "dropped": []}
    for j in reversed(jobs):  # newest first
        card = {**_job_card(j, names), "needs": needs.get(j["id"])}
        s = j["status"]
        if s in ("new", "triaged"):
            cols["waiting"].append(card)
        elif s == "offered":
            pending = [o for o in offers if o["job_id"] == j["id"] and not o.get("response")]
            card["offered_to"] = names.get(pending[-1]["tech_id"]) if pending else None
            cols["offered"].append(card)
        elif s in WORKING:
            cols["taken"].append(card)
        elif s == "failed" and (_ts(j.get("created_at")) or now) >= day_ago:
            cols["dropped"].append(card)
    return cols


@router.get("/summary")
def summary():
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)
    jobs = db.store.select("jobs")
    offers = db.store.select("offers")
    names = {t.id: t.name for t in db.list_techs()}
    recent = [j for j in jobs if (_ts(j.get("created_at")) or now) >= day_ago]

    responded = [o for o in offers if o.get("response")]
    yes = sum(o["response"] == "yes" for o in responded)
    working = [j for j in jobs if j["status"] in WORKING and j.get("quote")]
    needs = _needs(jobs, offers, now)
    needs_you = [{**_job_card(j, names), "reason": needs[j["id"]]} for j in jobs if j["id"] in needs]

    return {
        "open": sum(j["status"] in OPEN for j in jobs),
        "working": sum(j["status"] in WORKING for j in jobs),
        "accepted_24h": sum(j["status"] in WORKING for j in recent),
        "failed_24h": sum(j["status"] == "failed" for j in recent),
        "jobs_24h": len(recent),
        "acceptance_rate": round(yes / len(responded), 2) if responded else None,
        "offers_responded": len(responded),
        "pipeline": {"low": sum(j["quote"]["low"] for j in working), "high": sum(j["quote"]["high"] for j in working),
                     "jobs": len(working)},
        "needs_you": needs_you,
        "recent": [_job_card(j, names) for j in reversed(jobs[-12:])],
    }


@router.get("/associates")
def associates():
    jobs = db.store.select("jobs")
    offers = db.store.select("offers")
    techs = db.list_techs()
    names = {t.id: t.name for t in techs}
    status_of = {j["id"]: j["status"] for j in jobs}

    incoming = []
    for j in jobs:
        if j["status"] in OPEN:
            pending = [o for o in offers if o["job_id"] == j["id"] and not o.get("response")]
            card = _job_card(j, names)
            card["offered_to"] = names.get(pending[-1]["tech_id"]) if pending else None
            incoming.append(card)

    out = []
    for t in techs:
        mine = [o for o in offers if o["tech_id"] == t.id]
        customers = [_job_card(j, names) for j in jobs if j.get("assigned_tech_id") == t.id and j["status"] in WORKING]
        pending = [o for o in mine if not o.get("response") and status_of.get(o["job_id"]) == "offered"]
        state = "on a job" if customers else "offer pending" if pending else "available"
        out.append({
            "id": t.id, "name": t.name, "description": t.description, "skills": t.skills, "rating": t.rating,
            "state": state, "customers": customers,
            "stats": {"offered": len(mine), "yes": sum(o.get("response") == "yes" for o in mine),
                      "no": sum(o.get("response") == "no" for o in mine),
                      "timeout": sum(o.get("response") == "timeout" for o in mine)},
        })
    rank = {"on a job": 0, "offer pending": 1, "available": 2}
    out.sort(key=lambda a: (rank[a["state"]], -len(a["customers"]), -a["rating"]))
    return {"incoming": incoming, "associates": out}


@router.get("/jobs/{job_id}/ranking")
def job_ranking(job_id: str):
    """Latest ranking logged for a job, best first."""
    evs = [e for e in db.list_events(job_id) if e.get("agent") == "ranking"]
    return {"lines": evs[-1]["message"].split("\n") if evs else []}