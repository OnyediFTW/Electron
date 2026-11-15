"""Electron backend. Run: uvicorn main:app --reload --port 8000"""
import os
from datetime import datetime, timezone
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

    # Middle phone = the plumber who got the first offer this session (ranking decides who
    # that is); before any offer, keep the .env order.
    first_out = {}
    for m in msgs:
        if m["direction"] == "out" and m["phone"] in tech_phones:
            first_out.setdefault(m["phone"], m["created_at"])
    ordered = sorted(tech_phones, key=lambda p: (p not in first_out, first_out.get(p, ""), tech_phones.index(p)))

    phones = [{**thread("customer", "Customer · you", customer), "key": "customer"}]
    for i, p in enumerate(ordered):
        first = names.get(p, "tech").split()[0]
        role, label = ("tech", f"Plumber · {first}") if i == 0 else ("backup", f"Backup · {first}")
        phones.append({**thread(role, label, p), "key": first})
    return {"server_time": now, "phones": phones}


@app.get("/phones")
def phones_page():
    return FileResponse(Path(__file__).parent / "dashboard" / "phones.html")


@app.get("/")
def dashboard():
    return FileResponse(Path(__file__).parent / "dashboard" / "index.html")
