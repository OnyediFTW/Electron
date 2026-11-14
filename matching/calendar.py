# matching/calendar.py
# STUB: fake calendar. Swap in Google Calendar later, keep the signatures.
from datetime import datetime, timedelta

from shared.db import log_event
from shared.schema import Slot

_BOOKED: set[tuple[str, datetime]] = set()


def _busy(tech_id: str, start: datetime) -> bool:
    if (tech_id, start) in _BOOKED:
        return True
    # Fake busy pattern that differs per tech and per day
    seed = sum(ord(c) for c in tech_id)
    return (start.hour // 2 + start.day + seed) % 3 == 0


def free_slots(tech_id: str) -> list[Slot]:
    """STUB: up to 3 free 2-hour slots (8 AM to 6 PM) in the next 48 hours."""
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    t = now + timedelta(hours=1)
    if t.hour % 2:
        t += timedelta(hours=1)  # snap to an even hour
    slots: list[Slot] = []
    while t < now + timedelta(hours=48) and len(slots) < 3:
        if 8 <= t.hour <= 16 and not _busy(tech_id, t):
            slots.append(Slot(start=t, end=t + timedelta(hours=2)))
        t += timedelta(hours=2)
    return slots


def book(tech_id: str, job_id: str, slot: Slot) -> str:
    """STUB: pretends to book. Returns a fake event id."""
    _BOOKED.add((tech_id, slot.start))
    event_id = f"fake-{job_id[:8]}-{tech_id}"
    log_event(job_id, "calendar", f"STUB booked {slot.start:%a %I:%M %p} (event {event_id})")
    return event_id