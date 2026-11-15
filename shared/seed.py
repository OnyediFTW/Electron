"""Seed demo techs around Princeton. Owned by C; safe to re-run.

Usage: python -m shared.seed            (adds techs if empty; refreshes descriptions and skills)
       python -m shared.seed --reset    (wipes all job data and techs first)

TECH_PHONE_1 / TECH_PHONE_2 in .env are teammates' real phones so the demo buzzes.
If DEMO_CUSTOMER_PHONE is set, that customer also gets one past job (Person B) so the
customer-memory feature has history to show.
"""
import os
import sys
from uuid import uuid4

from shared import db

DEMO_TECHS = [
    # name, phone, skills, lat, lng, rating, description
    ("Marco Ruiz",  os.getenv("TECH_PHONE_1") or "+15550000001", ["plumbing", "water_heater"],
     40.3573, -74.6672, 4.9, "Plumber · leaks, fixtures and water heaters"),
    ("Dana Okafor", os.getenv("TECH_PHONE_2") or "+15550000002", ["plumbing", "drain", "painting"],
     40.3487, -74.6593, 4.7, "Painting and general repair · interior paint, drywall patches, small drain clogs"),
    ("Sam Patel",   "+15550000003", ["plumbing"],
     40.3200, -74.6200, 4.4, "Plumber · faucets, toilets and supply lines"),
    ("Lee Chen",    "+15550000004", ["plumbing", "gas"],
     40.2930, -74.6820, 4.8, "Plumber and gas fitter · gas lines and appliance hookups"),
    ("Rita Gomez",  "+15550000005", ["drain", "sewer"],
     40.3790, -74.6100, 4.6, "Drain and sewer · clogs and main-line backups"),
    ("Tom Walsh",   "+15550000006", ["electrical"],
     40.3500, -74.6500, 4.5, "Electrician · outlets, breakers and fixtures"),
    ("Ava Brooks",  "+15550000007", ["plumbing", "water_heater"],
     40.2170, -74.7430, 4.2, "Plumber · water heater installs and repipes"),
]


# Person B: one finished job for the demo customer, from last summer.
DEMO_PAST_JOB = {
    "raw_text": "pipe under my kitchen sink is dripping",
    "trade": "plumbing",
    "issue": "leaking P-trap under kitchen sink",
    "urgency": "high",
    "parts": [{"name": "1-1/2 in P-trap kit", "confidence": 0.7, "url": None, "price": 8.97}],
    "status": "booked",
    "quote": {"low": 85.0, "high": 210.0},
    "created_at": "2026-08-12T14:30:00+00:00",
    "tech_name": "Marco Ruiz",
}


def seed_demo_history() -> None:
    """Give DEMO_CUSTOMER_PHONE one past job so memory shows up in the demo. Safe to re-run."""
    phone = os.getenv("DEMO_CUSTOMER_PHONE", "").strip()
    if not phone or db.list_customer_jobs(phone):
        return
    job = {k: v for k, v in DEMO_PAST_JOB.items() if k != "tech_name"}
    tech = next((t for t in db.list_techs() if t.name == DEMO_PAST_JOB["tech_name"]), None)
    raw = os.getenv("DEMO_CUSTOMER_LOCATION", "")
    try:
        lat, lng = (float(x) for x in raw.split(","))
    except ValueError:
        lat, lng = 40.3431, -74.6551  # downtown Princeton
    db.store.insert("jobs", {**job, "id": str(uuid4()), "customer_phone": phone,
                             "assigned_tech_id": tech.id if tech else None,
                             "location": {"lat": lat, "lng": lng}})
    print(f"[seed] added a past job for demo customer {phone}")


def seed(reset: bool = False) -> None:
    if reset:
        for t in ("events", "offers", "messages", "jobs", "techs"):
            db.store.delete_all(t)
    _seed_techs()
    seed_demo_history()


def _seed_techs() -> None:
    existing = {t.name: t for t in db.list_techs()}
    if existing:
        updated = 0
        for name, _phone, skills, _lat, _lng, _rating, description in DEMO_TECHS:
            if name in existing:
                db.store.update("techs", existing[name].id, {"skills": skills, "description": description})
                updated += 1
        print(f"[seed] techs already present ({len(existing)}); refreshed skills + descriptions for {updated}")
        return
    for name, phone, skills, lat, lng, rating, description in DEMO_TECHS:
        db.store.insert("techs", {
            "id": str(uuid4()), "name": name, "phone": phone, "skills": skills,
            "lat": lat, "lng": lng, "rating": rating, "calendar_id": None, "description": description,
        })
    print(f"[seed] inserted {len(DEMO_TECHS)} techs into {db.BACKEND}")


if __name__ == "__main__":
    seed(reset="--reset" in sys.argv)
