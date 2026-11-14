"""Electron backend. Run: uvicorn main:app --reload --port 8000"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

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


@app.get("/")
def dashboard():
    return FileResponse(Path(__file__).parent / "dashboard" / "index.html")
