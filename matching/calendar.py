"""free_slots(tech_id) -> list[Slot], book(tech_id, job_id, slot) -> event_id.
Owner: Person C.

STUB: scheduling isn't connected, so free_slots() returns [] and dispatch tells
the customer and tech that the time still needs to be arranged. Replace with
Google Calendar freebusy + events.insert on each tech's calendar_id.
"""
from uuid import uuid4

from shared.db import log_event
from shared.schema import Slot


def free_slots(tech_id: str) -> list[Slot]:
    return []


def book(tech_id: str, job_id: str, slot: Slot) -> str:
    event_id = f"stub-{uuid4().hex[:8]}"
    log_event(job_id, "calendar", f"STUB booked {slot.start:%a %I:%M %p} (event {event_id})")
    return event_id
