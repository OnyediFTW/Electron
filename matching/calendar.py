"""free_slots(tech_id) -> list[Slot], book(tech_id, job_id, slot) -> event_id.
Owner: Person C.

STUB: fake slots tomorrow, fake event id. Replace with Google Calendar
freebusy + events.insert on each tech's calendar_id. Keep the stub as FALLBACK.
"""
from datetime import datetime, timedelta
from uuid import uuid4

from shared.db import log_event
from shared.schema import Slot


def free_slots(tech_id: str) -> list[Slot]:
    tomorrow = (datetime.now() + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    return [Slot(start=tomorrow.replace(hour=h), end=tomorrow.replace(hour=h + 2)) for h in (9, 13, 15)]


def book(tech_id: str, job_id: str, slot: Slot) -> str:
    event_id = f"stub-{uuid4().hex[:8]}"
    log_event(job_id, "calendar", f"STUB booked {slot.start:%a %I:%M %p} (event {event_id})")
    return event_id
