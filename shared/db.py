"""Shared DB access. Do not change without telling the team.

Uses Supabase when SUPABASE_URL and SUPABASE_SERVICE_KEY are set. Otherwise it
falls back to an in-memory store with the same API, so everyone can run and
test their functions before the Supabase project exists (and the demo survives
a Wi-Fi outage).
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from dotenv import load_dotenv

from shared.schema import Job, Offer, Tech

load_dotenv()

TABLES = ("techs", "jobs", "offers", "events", "contacts", "messages")  # contacts, messages: owned by A
_TIMESTAMPED = ("events", "jobs", "messages")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _MemoryStore:
    """FALLBACK: in-memory tables. Data is lost on restart."""

    def __init__(self) -> None:
        self.rows: dict[str, list[dict]] = {t: [] for t in TABLES}
        self.lock = threading.Lock()

    def insert(self, table: str, row: dict) -> dict:
        with self.lock:
            row = {"id": str(uuid4()), **row}
            if table in _TIMESTAMPED:
                row.setdefault("created_at", _now())
            self.rows[table].append(row)
            return dict(row)

    def update(self, table: str, row_id: str, fields: dict) -> Optional[dict]:
        with self.lock:
            for row in self.rows[table]:
                if row["id"] == row_id:
                    row.update(fields)
                    return dict(row)
        return None

    def select(self, table: str, **eq: Any) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.rows[table]
                    if all(r.get(k) == v for k, v in eq.items())]

    def delete_all(self, table: str) -> None:
        with self.lock:
            self.rows[table] = []


class _SupabaseStore:
    def __init__(self, url: str, key: str) -> None:
        from supabase import create_client
        self.client = create_client(url, key)

    def insert(self, table: str, row: dict) -> dict:
        return self.client.table(table).insert(row).execute().data[0]

    def update(self, table: str, row_id: str, fields: dict) -> Optional[dict]:
        data = self.client.table(table).update(fields).eq("id", row_id).execute().data
        return data[0] if data else None

    def select(self, table: str, **eq: Any) -> list[dict]:
        q = self.client.table(table).select("*")
        for k, v in eq.items():
            q = q.eq(k, v)
        if table in _TIMESTAMPED:
            q = q.order("created_at")
        return q.execute().data

    def delete_all(self, table: str) -> None:
        # Supabase requires a filter on delete; this matches every row.
        self.client.table(table).delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()


def _make_store():
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
    if url and key:
        return _SupabaseStore(url, key)
    print("[db] SUPABASE_URL/SUPABASE_SERVICE_KEY not set -> using in-memory FALLBACK store")
    return _MemoryStore()


store = _make_store()
BACKEND = "supabase" if isinstance(store, _SupabaseStore) else "memory"


def _jsonable(v: Any) -> Any:
    if hasattr(v, "model_dump"):
        return v.model_dump(mode="json")
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if hasattr(v, "value"):  # enums
        return v.value
    return v


# ---------- events (everyone) ----------

def log_event(job_id: Optional[str], agent: str, message: str) -> None:
    """One row per step so the dashboard shows the trace. Never raises."""
    print(f"[{agent}] {(job_id or '-')[:8]}: {message}")
    try:
        store.insert("events", {"job_id": job_id, "agent": agent, "message": message})
    except Exception as e:  # logging must never break the demo
        print(f"[db] log_event failed: {e}")


def list_events(job_id: Optional[str] = None) -> list[dict]:
    return store.select("events", job_id=job_id) if job_id else store.select("events")


# ---------- messages: every text in or out, for the live /phones view (A) ----------

def log_message(phone: str, direction: str, text: str, *,
                photo_url: Optional[str] = None, job_id: Optional[str] = None) -> None:
    """direction: "in" (person -> Electron) or "out" (Electron -> person). Never raises."""
    try:
        store.insert("messages", {"phone": phone, "direction": direction, "text": text,
                                  "photo_url": photo_url, "job_id": job_id})
    except Exception as e:
        print(f"[db] log_message failed: {e}")


def list_messages(since: Optional[str] = None) -> list[dict]:
    rows = store.select("messages")
    if since:
        cutoff = datetime.fromisoformat(since)
        rows = [r for r in rows if datetime.fromisoformat(r["created_at"]) > cutoff]
    return rows


# ---------- jobs ----------

def insert_job(job: Job) -> Job:
    store.insert("jobs", job.model_dump(mode="json", exclude={"follow_up_question"}))
    return job


def get_job(job_id: str) -> Optional[Job]:
    rows = store.select("jobs", id=job_id)
    return Job(**rows[0]) if rows else None


def list_jobs() -> list[Job]:
    return [Job(**r) for r in store.select("jobs")]


def update_job_fields(job_id: str, **fields: Any) -> None:
    """Triage fields (B) and assignment (A). NOT status: use messaging.state.advance()."""
    if "status" in fields:
        raise ValueError("status changes go through messaging.state.advance()")
    fields.pop("follow_up_question", None)
    store.update("jobs", job_id, {k: _jsonable(v) for k, v in fields.items()})


def _set_status(job_id: str, status: str) -> None:
    """Private. Only messaging/state.py calls this."""
    store.update("jobs", job_id, {"status": status})


# ---------- techs (C seeds) ----------

def list_techs() -> list[Tech]:
    return [Tech(**r) for r in store.select("techs")]


def get_tech(tech_id: str) -> Optional[Tech]:
    rows = store.select("techs", id=tech_id)
    return Tech(**rows[0]) if rows else None


def get_tech_by_phone(phone: str) -> Optional[Tech]:
    rows = store.select("techs", phone=phone)
    return Tech(**rows[0]) if rows else None


# ---------- offers (A) ----------

def insert_offer(offer: Offer) -> Offer:
    store.insert("offers", offer.model_dump(mode="json"))
    return offer


def update_offer(offer_id: str, response: str) -> None:
    store.update("offers", offer_id, {"response": response})


def list_offers(job_id: str) -> list[Offer]:
    return [Offer(**r) for r in store.select("offers", job_id=job_id)]
