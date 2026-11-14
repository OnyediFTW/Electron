"""Router + dispatch loop. Owner: Person A.

handle_inbound() is the single entry point for every text, from the Photon
webhook or the /dev/inbound simulator.

Message copy never invents facts: anything a stub doesn't know yet (issue,
parts, price, time) is said plainly as unknown.
"""
import os
import re
import threading
from datetime import datetime, timezone

from agents import find_parts, intake, quote
from matching import book, free_slots, rank_techs
from messaging.send import send_text
from messaging.state import advance
from shared import db
from shared.schema import Job, JobStatus, Location, Offer, OfferResponse

OFFER_TIMEOUT_SECONDS = float(os.getenv("OFFER_TIMEOUT_SECONDS", "60"))

YES = re.compile(r"^\s*(y|ya|yes|yep|yeah|sure|ok|okay|on it|accept)\b", re.I)
NO = re.compile(r"^\s*(n|no|nope|nah|can'?t|cannot|pass|decline)\b", re.I)

_timers: dict[str, threading.Timer] = {}
# One inbound event at a time, so a YES and a timeout can't both fire offer_next.
_dispatch_lock = threading.RLock()


def _demo_location() -> Location | None:
    """DEMO_CUSTOMER_LOCATION="lat,lng" until intake can read a real location."""
    raw = os.getenv("DEMO_CUSTOMER_LOCATION", "")
    try:
        lat, lng = (float(x) for x in raw.split(","))
        return Location(lat=lat, lng=lng)
    except ValueError:
        return None


def _quote_text(job: Job) -> str:
    return f"${job.quote.low:.0f}-${job.quote.high:.0f}" if job.quote else "not available yet"


def _first(name: str) -> str:
    return name.split()[0]


def handle_inbound(phone: str, text: str, photo_url: str | None = None) -> None:
    try:
        db.log_message(phone, "in", text, photo_url=photo_url)
        with _dispatch_lock:
            tech = db.get_tech_by_phone(phone)
            if tech:
                handle_tech_reply(tech.id, text)
            else:
                handle_customer(phone, text, photo_url)
    except Exception as e:  # surface on the dashboard instead of dying in a background task
        db.log_event(None, "error", f"handling text from {phone}: {e!r}")
        raise


# ---------- customer flow ----------

def handle_customer(phone: str, text: str, photo_url: str | None) -> Job:
    job = Job(customer_phone=phone, photo_url=photo_url, raw_text=text)
    db.insert_job(job)
    db.log_event(job.id, "messaging", f"new text from {phone}: {text!r}" + (" + photo" if photo_url else ""))

    triaged = intake(photo_url, text, customer_phone=phone, job_id=job.id)
    if triaged.follow_up_question:
        send_text(phone, triaged.follow_up_question, job_id=job.id)
        return job  # TODO(A): attach the customer's answer to this job

    parts = find_parts(triaged)
    q = quote(triaged, parts)
    db.update_job_fields(job.id, trade=triaged.trade, issue=triaged.issue, urgency=triaged.urgency,
                         parts=parts, location=triaged.location or _demo_location(), quote=q)
    advance(job.id, JobStatus.triaged)
    send_text(phone, "Got it, finding a plumber for you now.", job_id=job.id)
    offer_next(job.id)
    return job


def _start_timer(offer_id: str, job_id: str) -> None:
    t = threading.Timer(OFFER_TIMEOUT_SECONDS, _on_timeout, args=(offer_id, job_id))
    t.daemon = True
    _timers[offer_id] = t
    t.start()


def _offer_text(job: Job, reasons: list[str]) -> str:
    if job.issue:
        urgency = f" [{job.urgency.value} urgency]" if job.urgency else ""
        head = f"New job: {job.issue}{urgency}"
    else:
        head = "New job: we don't know what the problem is yet"
    parts = "; ".join(f"{p.name} (likely, {p.confidence:.0%})" for p in job.parts) or "unknown"
    lines = [head, f'Customer said: "{job.raw_text}"']
    if job.photo_url:
        lines.append(f"Photo: {job.photo_url}")
    lines += [f"Parts: {parts}", f"Quote: {_quote_text(job)}",
              f"Why you: {', '.join(reasons)}", "Reply YES or NO."]
    return "\n".join(lines)


def offer_next(job_id: str) -> None:
    job = db.get_job(job_id)
    tried = {o.tech_id for o in db.list_offers(job_id)}
    candidates = [r for r in rank_techs(job) if r.tech_id not in tried]
    if not candidates:
        advance(job_id, JobStatus.failed, "no techs left")
        send_text(job.customer_phone, "Sorry, no one is free right now. The owner will call you.", job_id=job_id)
        return

    top = candidates[0]
    tech = db.get_tech(top.tech_id)
    offer = db.insert_offer(Offer(job_id=job_id, tech_id=tech.id, sent_at=datetime.now(timezone.utc)))
    advance(job_id, JobStatus.offered, f"to {tech.name}")
    send_text(tech.phone, _offer_text(job, top.reasons), job_id=job_id, photo_url=job.photo_url)
    _start_timer(offer.id, job_id)


def _on_timeout(offer_id: str, job_id: str) -> None:
    with _dispatch_lock:
        _timers.pop(offer_id, None)
        pending = [o for o in db.list_offers(job_id) if o.id == offer_id and o.response is None]
        if not pending:
            return  # tech already answered
        db.update_offer(offer_id, OfferResponse.timeout.value)
        db.log_event(job_id, "messaging", f"no reply in {OFFER_TIMEOUT_SECONDS:.0f}s, moving on")
        offer_next(job_id)


# ---------- tech flow ----------

def _pending_offer_for(tech_id: str) -> Offer | None:
    for job in db.list_jobs():
        if job.status != JobStatus.offered:
            continue
        for o in db.list_offers(job.id):
            if o.tech_id == tech_id and o.response is None:
                return o
    return None


def handle_tech_reply(tech_id: str, text: str) -> None:
    offer = _pending_offer_for(tech_id)
    tech = db.get_tech(tech_id)
    if offer is None:
        send_text(tech.phone, "No open offers for you right now.")
        return
    if timer := _timers.pop(offer.id, None):
        timer.cancel()

    if YES.match(text):
        db.update_offer(offer.id, OfferResponse.yes.value)
        db.update_job_fields(offer.job_id, assigned_tech_id=tech_id)
        advance(offer.job_id, JobStatus.accepted, f"{tech.name} said yes")
        _schedule(offer.job_id, tech_id)
    elif NO.match(text):
        db.update_offer(offer.id, OfferResponse.no.value)
        db.log_event(offer.job_id, "messaging", f"{tech.name} declined")
        offer_next(offer.job_id)
    else:
        _start_timer(offer.id, offer.job_id)
        send_text(tech.phone, "Sorry, reply YES or NO.", job_id=offer.job_id)


def _schedule(job_id: str, tech_id: str) -> None:
    job, tech = db.get_job(job_id), db.get_tech(tech_id)
    slots = free_slots(tech_id)
    if not slots:
        # Scheduling isn't connected yet: say so instead of inventing a time.
        db.log_event(job_id, "calendar", "no scheduling yet: time to be arranged between tech and customer")
        send_text(job.customer_phone,
                  f"{tech.name} accepted your job. We can't schedule a time automatically yet, "
                  f"so {_first(tech.name)} will text you to set one. Price: {_quote_text(job)}.",
                  job_id=job_id)
        send_text(tech.phone,
                  f"The job is yours. Scheduling isn't set up yet, so text the customer at "
                  f"{job.customer_phone} to set a time.",
                  job_id=job_id)
        return

    slot = slots[0]
    book(tech_id, job_id, slot)
    advance(job_id, JobStatus.booked, f"{slot.start:%a %I:%M %p}")
    send_text(job.customer_phone,
              f"You're booked: {tech.name} arrives {slot.start:%a %b %d, %I:%M %p}. Price: {_quote_text(job)}.",
              job_id=job_id)
    send_text(tech.phone, f"Confirmed for {slot.start:%a %I:%M %p}. Added to your calendar.", job_id=job_id)
