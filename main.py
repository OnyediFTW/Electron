"""Electron backend. Run: uvicorn main:app --reload --port 8000"""
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from messaging import photon
from messaging.webhook import router as messaging_router
from shared import db
from shared.seed import seed

app = FastAPI(title="Electron")
app.include_router(messaging_router)

if db.BACKEND == "memory":
    seed()  # FALLBACK store starts empty every run


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
    now = datetime.now(timezone.utc).isoformat()
    msgs = db.list_messages(since) if since else []
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

    phones = [thread("customer", "Customer · you", customer)]
    for i, p in enumerate(tech_phones):
        role = "tech" if i == 0 else "backup"
        phones.append(thread(role, f"{'Tech' if i == 0 else 'Backup'} · {names.get(p, 'tech').split()[0]}", p))
    return {"server_time": now, "phones": phones}


@app.get("/phones")
def phones_page():
    return FileResponse(Path(__file__).parent / "dashboard" / "phones.html")


@app.get("/")
def dashboard():
    return FileResponse(Path(__file__).parent / "dashboard" / "index.html")
