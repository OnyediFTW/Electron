"""Seed demo techs around Princeton. Owned by C; safe to re-run.

Usage: python -m shared.seed            (adds techs if table is empty)
       python -m shared.seed --reset    (wipes all four tables first)

TECH_PHONE_1 / TECH_PHONE_2 in .env are teammates' real phones so the demo buzzes.
"""
import os
import sys
from uuid import uuid4

from shared import db

DEMO_TECHS = [
    # name, phone, skills, lat, lng, rating
    ("Marco Ruiz",  os.getenv("TECH_PHONE_1") or "+15550000001", ["plumbing", "water_heater"], 40.3573, -74.6672, 4.9),
    ("Dana Okafor", os.getenv("TECH_PHONE_2") or "+15550000002", ["plumbing", "drain"],        40.3487, -74.6593, 4.7),
    ("Sam Patel",   "+15550000003", ["plumbing"],                 40.3200, -74.6200, 4.4),
    ("Lee Chen",    "+15550000004", ["plumbing", "gas"],          40.2930, -74.6820, 4.8),
    ("Rita Gomez",  "+15550000005", ["drain", "sewer"],           40.3790, -74.6100, 4.6),
    ("Tom Walsh",   "+15550000006", ["electrical"],               40.3500, -74.6500, 4.5),
    ("Ava Brooks",  "+15550000007", ["plumbing", "water_heater"], 40.2170, -74.7430, 4.2),
]


def seed(reset: bool = False) -> None:
    if reset:
        for t in ("events", "offers", "jobs", "techs"):
            db.store.delete_all(t)
    if db.list_techs():
        print(f"[seed] techs already present ({len(db.list_techs())}), skipping")
        return
    for name, phone, skills, lat, lng, rating in DEMO_TECHS:
        db.store.insert("techs", {
            "id": str(uuid4()), "name": name, "phone": phone, "skills": skills,
            "lat": lat, "lng": lng, "rating": rating, "calendar_id": None,
        })
    print(f"[seed] inserted {len(DEMO_TECHS)} techs into {db.BACKEND}")


if __name__ == "__main__":
    seed(reset="--reset" in sys.argv)
