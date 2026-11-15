"""free_slots / book.   Owner: Person C.

Real Google Calendar for any tech with a calendar ID in .env (MARCO_CALENDAR_ID,
DANA_CALENDAR_ID). Everyone else, and any Google failure, uses the fake calendar
at the bottom so the demo never hangs. Set USE_GOOGLE_CALENDAR=0 to force the fake one.
"""
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from shared import db
from shared.db import log_event
from shared.schema import Slot

GOOGLE_TIMEOUT = 4      # seconds per Google call
COOLDOWN = 60           # after a failure, skip Google for this long
_down_until = 0.0
_creds = None
_BOOKED: set[tuple[str, datetime]] = set()


# ---------- real Google Calendar ----------

def _enabled() -> bool:
    return os.getenv("USE_GOOGLE_CALENDAR", "1") != "0" and time.time() >= _down_until


def _calendar_id(tech_id: str) -> str | None:
    t = db.get_tech(tech_id)
    if not t:
        return None
    return t.calendar_id or os.getenv(f"{t.name.split()[0].upper()}_CALENDAR_ID") or None


def _svc():
    """Fresh client per call (httplib2 isn't thread-safe); credentials are cached."""
    global _creds
    import google_auth_httplib2
    import httplib2
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    if _creds is None:
        key = Path(os.getenv("GOOGLE_KEY_FILE", "google-key.json"))
        if not key.is_absolute():
            key = Path(__file__).resolve().parent.parent / key
        _creds = service_account.Credentials.from_service_account_file(
            str(key), scopes=["https://www.googleapis.com/auth/calendar"])
    http = google_auth_httplib2.AuthorizedHttp(_creds, http=httplib2.Http(timeout=GOOGLE_TIMEOUT))
    return build("calendar", "v3", http=http, cache_discovery=False)


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _google_slots(cal_id: str) -> list[Slot]:
    now = datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)
    end = now + timedelta(hours=48)
    resp = _svc().freebusy().query(body={
        "timeMin": now.isoformat(), "timeMax": end.isoformat(), "items": [{"id": cal_id}],
    }).execute()
    cal = next(iter(resp["calendars"].values()))
    if cal.get("errors"):
        raise RuntimeError(f"calendar not readable (shared with the service account?): {cal['errors']}")
    busy = [(_parse(b["start"]), _parse(b["end"])) for b in cal.get("busy", [])]
    t = now + timedelta(hours=1)
    if t.hour % 2:
        t += timedelta(hours=1)  # snap to an even hour
    slots: list[Slot] = []
    while t < end and len(slots) < 3:
        s_end = t + timedelta(hours=2)
        if 8 <= t.hour <= 16 and not any(t < be and s_end > bs for bs, be in busy):
            slots.append(Slot(start=t, end=s_end))
        t = s_end
    return slots


def _fail(what: str, e: Exception) -> None:
    global _down_until
    _down_until = time.time() + COOLDOWN
    log_event(None, "calendar", f"Google Calendar {what} failed, using fake calendar for {COOLDOWN}s: {e!r}")


def free_slots(tech_id: str) -> list[Slot]:
    cal_id = _calendar_id(tech_id)
    if cal_id and _enabled():
        try:
            return _google_slots(cal_id)
        except Exception as e:
            _fail("read", e)
    return _stub_free_slots(tech_id)


def book(tech_id: str, job_id: str, slot: Slot) -> str:
    cal_id = _calendar_id(tech_id)
    if cal_id and _enabled():
        try:
            job = db.get_job(job_id)
            start = slot.start if slot.start.tzinfo else slot.start.astimezone()
            end = slot.end if slot.end.tzinfo else slot.end.astimezone()
            ev = _svc().events().insert(calendarId=cal_id, body={
                "summary": f"Electron job: {(job.issue if job else None) or 'service call'}",
                "description": (job.raw_text if job else "") or "",
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": end.isoformat()},
            }).execute()
            _BOOKED.add((tech_id, slot.start))
            log_event(job_id, "calendar", f"booked {slot.start:%a %I:%M %p} on Google Calendar (event {ev['id']})")
            return ev["id"]
        except Exception as e:
            _fail("booking", e)
    return _stub_book(tech_id, job_id, slot)


# ---------- FALLBACK: fake calendar ----------

def _busy(tech_id: str, start: datetime) -> bool:
    if (tech_id, start) in _BOOKED:
        return True
    seed = sum(ord(c) for c in tech_id)
    return (start.hour // 2 + start.day + seed) % 3 == 0


def _stub_free_slots(tech_id: str) -> list[Slot]:
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    t = now + timedelta(hours=1)
    if t.hour % 2:
        t += timedelta(hours=1)
    slots: list[Slot] = []
    while t < now + timedelta(hours=48) and len(slots) < 3:
        if 8 <= t.hour <= 16 and not _busy(tech_id, t):
            slots.append(Slot(start=t, end=t + timedelta(hours=2)))
        t += timedelta(hours=2)
    return slots


def _stub_book(tech_id: str, job_id: str, slot: Slot) -> str:
    _BOOKED.add((tech_id, slot.start))
    event_id = f"fake-{job_id[:8]}-{tech_id}"
    log_event(job_id, "calendar", f"FALLBACK booked {slot.start:%a %I:%M %p} (event {event_id})")
    return event_id