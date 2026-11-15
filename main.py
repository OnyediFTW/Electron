"""Electron backend. Run: uvicorn main:app --reload --port 8000"""
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from dashboard.api import router as owner_router
from messaging import photon
from messaging.dispatch import resume_offers
from messaging.jobcard import router as jobcard_router
from messaging.webhook import router as messaging_router
from shared import db
from shared.seed import seed

app = FastAPI(title="Electron")
app.include_router(messaging_router)
app.include_router(owner_router)
app.include_router(jobcard_router)

if db.BACKEND == "memory":
    seed()  # FALLBACK store starts empty every run

try:  # offers made before a restart would otherwise wait forever (timers live in memory)
    resume_offers()
except Exception as e:
    print(f"[startup] resume_offers failed: {e!r}")


@app.get("/health")
def health():
    return {"ok": True, "db": db.BACKEND}


# ---------- read-only API for the dashboard (Person C) ----------

@app.get("/api/jobs")
def api_jobs():
    techs = {t.id: t.name for t in db.list_techs()}
    jobs = [j.model_dump(mode="json") | {"assigned_tech_name": techs.get(j.assigned_tech_id)}
            for j in db.list_jobs()]
    return list(reversed(jobs))


@app.get("/api/jobs/{job_id}/events")
def api_job_events(job_id: str):
    return db.list_events(job_id)


@app.get("/api/techs")
def api_techs():
    return [t.model_dump() for t in db.list_techs()]


@app.get("/api/threads")
def api_threads(since: str | None = None):
    """Live three-phone view: messages after `since` for the customer and the two demo techs.
    No `since` = just the server time, so the page starts blank. No raw phone numbers out:
    the tunnel makes this public."""
    if since:
        msgs = db.list_messages(since)
        now = msgs[-1]["created_at"] if msgs else since
    else:
        # Start the page's cursor at the newest stored message (database clock), not this
        # machine's clock, so clock drift between the laptop and Supabase can't hide texts.
        latest = db.list_messages()
        now = latest[-1]["created_at"] if latest else datetime.now(timezone.utc).isoformat()
        msgs = []
    tech_phones = [p for p in (os.getenv("TECH_PHONE_1"), os.getenv("TECH_PHONE_2")) if p]
    names = {t.phone: t.name for t in db.list_techs()}
    customers = [m["phone"] for m in msgs if m["phone"] not in tech_phones]
    customer = customers[-1] if customers else os.getenv("DEMO_CUSTOMER_PHONE", "")
    lines = photon.line_numbers()

    def thread(role: str, label: str, phone: str) -> dict:
        return {"role": role, "label": label, "line": lines.get(phone),
                "messages": [{"direction": m["direction"], "text": m["text"],
                              "photo_url": m.get("photo_url"), "created_at": m["created_at"]}
                             for m in msgs if m["phone"] == phone]}

    # The nearer plumber phone = whoever got the first offer this session (the ranking decides);
    # before any offer, keep the .env order. Both are labeled Plumber: neither is a fixed backup.
    first_out = {}
    for m in msgs:
        if m["direction"] == "out" and m["phone"] in tech_phones:
            first_out.setdefault(m["phone"], m["created_at"])
    ordered = sorted(tech_phones, key=lambda p: (p not in first_out, first_out.get(p, ""), tech_phones.index(p)))

    phones = [{**thread("customer", "Customer · you", customer), "key": "customer"}]
    for i, p in enumerate(ordered):
        first = names.get(p, "tech").split()[0]
        role, label = ("tech" if i == 0 else "backup"), f"Plumber · {first}"  # equals; the board ranks them
        phones.append({**thread(role, label, p), "key": first})
    return {"server_time": now, "phones": phones, "photo_labels": _photo_labels()}


def _photo_labels() -> dict[str, str]:
    """{"/photos/<id>": "AI read: leaking P-trap under kitchen sink · urgent"} for jobs from the last
    12 hours, so the phones page can tag each customer photo once intake has read it."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    out = {}
    for j in db.store.select("jobs"):
        url, issue = j.get("photo_url") or "", j.get("issue")
        if "/photos/" in url and issue and str(j.get("created_at") or "") >= cutoff:
            out["/photos/" + url.split("/photos/", 1)[1]] = (
                f"AI read: {issue}" + (" · urgent" if j.get("urgency") == "high" else ""))
    return out


_RANK_LINE = re.compile(r"^\d+\.\s+(.+?)\s{2,}([\d.]+)\s{2,}\((.*)\)$")


@app.get("/api/ranking")
def api_ranking(after: str | None = None):
    """The newest job's matching for the ranking board on /phones: each scored plumber, the points
    each factor gave them (recomputed from rank_techs' reasons with its own weights), and what
    happened to their offer. Names only, no phone numbers: the tunnel makes this public."""
    from matching.rank import (FAIR_SHARE_STEP, GENERALIST_COVERS, GENERALIST_SKILL, HORIZON_HOURS, MAX_MILES,
                               URGENT_FREE_MULTIPLIER, WEIGHTS)

    rows = [r for r in db.store.select("jobs") if not after or str(r.get("created_at") or "") > after]
    if not rows:
        return {"job": None, "techs": [], "weights": WEIGHTS}
    job = max(rows, key=lambda r: str(r.get("created_at") or ""))
    events = db.list_events(job["id"])
    ranking = [e for e in events if e.get("agent") == "ranking"]
    urgent = job.get("urgency") == "high"
    w = {**WEIGHTS, "free": WEIGHTS["free"] * (URGENT_FREE_MULTIPLIER if urgent else 1)}
    info = {"id": job["id"], "issue": job.get("issue") or "Reading the photo…", "urgency": job.get("urgency"),
            "status": job["status"], "trade": job.get("trade"), "weights": w,
            "urgent_boost": URGENT_FREE_MULTIPLIER if urgent else None}
    if not ranking:
        return {"job": info, "techs": [], "weights": w}

    by_name = {t.name: t for t in db.list_techs()}
    offers: dict[str, dict] = {}
    for o in db.store.select("offers", job_id=job["id"]):
        offers[o["tech_id"]] = o  # latest offer per tech
    skipped: set[str] = set()
    for e in events:
        m = re.match(r"^skipped (.+?): ", e.get("message") or "")
        if m:
            skipped.update(n.strip() for n in m.group(1).split(","))

    def num(pattern: str, text: str) -> float | None:
        m = re.search(pattern, text)
        return float(m.group(1)) if m else None

    techs = []
    for line in ranking[-1]["message"].split("\n"):
        m = _RANK_LINE.match(line.strip())
        if not m:
            continue
        name, score, reasons = m.group(1), float(m.group(2)), m.group(3)
        d, stars = num(r"([\d.]+) mi away", reasons), num(r"([\d.]+) stars", reasons)
        hrs, n_open = num(r"free in (\d+)h", reasons), num(r"(\d+) jobs? today", reasons)
        points = {
            "skill": w["skill"] * (GENERALIST_SKILL if "general plumber" in reasons else 1),
            "distance": w["distance"] * max(0.0, 1 - d / MAX_MILES) if d is not None else 0.0,
            "free": w["free"] * max(0.0, 1 - hrs / HORIZON_HOURS) if hrs is not None else 0.0,
            "rating": w["rating"] * (stars or 0) / 5,
            "workload": max(-w["workload"], w["workload"] - FAIR_SHARE_STEP * (n_open or 0)),
        }
        t = by_name.get(name)
        o = offers.get(t.id) if t else None
        if t and job.get("assigned_tech_id") == t.id and job["status"] in ("accepted", "booked"):
            state = "booked" if job["status"] == "booked" else "took it"
        elif o and o.get("response") == "no":
            state = "passed"
        elif o and o.get("response") == "timeout":
            state = "no reply"
        elif o and not o.get("response") and job["status"] == "offered":
            state = "offered"
        elif name in skipped:
            state = "not on iMessage"
        else:
            state = "in line"
        techs.append({
            "name": name, "score": score, "state": state, "skills": t.skills if t else [],
            "points": {k: round(v, 1) for k, v in points.items()},
            "facts": {"miles": d, "stars": stars, "free_in_h": hrs, "jobs_today": n_open,
                      "generalist": "general plumber" in reasons},
        })
    # Techs the ranker left out (no skill for this trade), so the board answers "where is X?"
    ranked_names = {t["name"] for t in techs}
    trade = job.get("trade")
    for t in by_name.values():
        if t.name not in ranked_names and trade:
            covers = trade in GENERALIST_COVERS and "plumbing" in t.skills
            if trade not in t.skills and not covers:
                techs.append({"name": t.name, "score": None, "state": f"no {trade.replace('_', ' ')} skill",
                              "skills": t.skills, "points": {}, "facts": {}})
    return {"job": info, "techs": techs, "weights": w}


@app.get("/phones")
def phones_page():
    return FileResponse(Path(__file__).parent / "dashboard" / "phones.html")


@app.get("/")
def dashboard():
    return FileResponse(Path(__file__).parent / "dashboard" / "index.html")
