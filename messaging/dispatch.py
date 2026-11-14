"""Router + dispatch loop. Owner: Person A.

handle_inbound() is the single entry point for every text, from the Photon
webhook or the /dev/inbound simulator.

Concurrency: routing and every status change happen under _dispatch_lock; the
slow AI work (intake, parts, quote) runs OUTSIDE it, so a tech's YES is never
stuck behind another customer's triage. A job being triaged is in _triaging;
a follow-up text for it is attached, never triaged twice.

Message copy never invents facts: anything not known yet (issue, parts,
price, time) is said plainly as unknown.
"""
import os
import re
import threading
from datetime import datetime, timezone

from agents import find_parts, intake, quote
from matching import book, free_slots, rank_techs
from messaging import photon
from messaging.send import send_text
from messaging.state import advance
from shared import db
from shared.schema import Job, JobStatus, Location, Offer, OfferResponse

OFFER_TIMEOUT_SECONDS = float(os.getenv("OFFER_TIMEOUT_SECONDS", "60"))
# Off until matching/calendar.py talks to a real calendar: its stub invents free slots,
# and we don't tell customers a made-up arrival time. Set SCHEDULING_ENABLED=true to use it.
SCHEDULING_ENABLED = os.getenv("SCHEDULING_ENABLED", "false").lower() in ("1", "true", "yes")

YES = re.compile(r"^\s*(y|ya|yes|yep|yeah|sure|ok|okay|on it|accept)\b", re.I)
NO = re.compile(r"^\s*(n|no|nope|nah|can'?t|cannot|pass|decline)\b", re.I)
NEW_JOB = re.compile(r"^\s*new job\b", re.I)  # escape hatch: always open a fresh job

# A customer's text joins their open job instead of starting a second one.
OPEN_STATUSES = {"new", "triaged", "offered", "accepted", "booked"}
JOB_WINDOW_HOURS = float(os.getenv("JOB_WINDOW_HOURS", "12"))

_timers: dict[str, threading.Timer] = {}
_dispatch_lock = threading.RLock()
_triaging: set[str] = set()  # job ids whose agents are running right now


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


def _reachable(phone: str) -> bool:
    """Photon can only message people who have texted the line. Without Photon (dev), everyone is."""
    return not photon.configured() or photon.get_contact(phone) is not None


def handle_inbound(phone: str, text: str, photo_url: str | None = None) -> None:
    try:
        db.log_message(phone, "in", text, photo_url=photo_url)
        to_triage = None
        with _dispatch_lock:
            tech = db.get_tech_by_phone(phone)
            if tech:
                handle_tech_reply(tech.id, text)
                return
            job = None if NEW_JOB.match(text) else _open_job_for(phone)
            if job:
                to_triage = handle_customer_followup(job, text, photo_url)
            else:
                to_triage = _new_job(phone, text, photo_url)
        if to_triage:
            _triage(to_triage)  # slow AI work, outside the lock
    except Exception as e:  # surface on the dashboard instead of dying in a background task
        db.log_event(None, "error", f"handling text from {phone}: {e!r}")
        raise


# ---------- customer flow ----------

def _open_job_for(phone: str) -> Job | None:
    """The customer's newest job that is still in progress and recent, else None."""
    cutoff = datetime.now(timezone.utc).timestamp() - JOB_WINDOW_HOURS * 3600
    for row in reversed(db.store.select("jobs", customer_phone=phone)):  # newest first
        created = row.get("created_at")
        if row["status"] in OPEN_STATUSES and created and datetime.fromisoformat(created).timestamp() > cutoff:
            return Job(**row)
    return None


def _new_job(phone: str, text: str, photo_url: str | None) -> Job:
    job = Job(customer_phone=phone, photo_url=photo_url, raw_text=text)
    db.insert_job(job)
    db.log_event(job.id, "messaging", f"new text from {phone}: {text!r}" + (" + photo" if photo_url else ""))
    return job


def handle_customer(phone: str, text: str, photo_url: str | None) -> Job:
    """Start a job and triage it now (scripts/tests). Live texts go through handle_inbound."""
    job = _new_job(phone, text, photo_url)
    _triage(job)
    return job


def _triage(job: Job) -> None:
    """Run the agents on everything the customer has said so far, then offer the job.
    If intake needs more information it asks a follow-up and the job stays `new`; the
    answer comes back through handle_customer_followup, which triages again."""
    with _dispatch_lock:
        if job.id in _triaging:
            return
        _triaging.add(job.id)
    try:
        job = db.get_job(job.id) or job  # pick up text attached since it was queued
        triaged = intake(job.photo_url, job.raw_text, customer_phone=job.customer_phone, job_id=job.id)
        if triaged.follow_up_question:
            send_text(job.customer_phone, triaged.follow_up_question, job_id=job.id)
            return
        parts = find_parts(triaged)
        q = quote(triaged, parts)
        with _dispatch_lock:
            if db.get_job(job.id).status != JobStatus.new:
                return  # someone already moved it on
            db.update_job_fields(job.id, trade=triaged.trade, issue=triaged.issue, urgency=triaged.urgency,
                                 parts=parts, location=triaged.location or _demo_location(), quote=q)
            advance(job.id, JobStatus.triaged)
            send_text(job.customer_phone, "Got it, finding a plumber for you now.", job_id=job.id)
            offer_next(job.id)
    finally:
        with _dispatch_lock:
            _triaging.discard(job.id)


def handle_customer_followup(job: Job, text: str, photo_url: str | None) -> Job | None:
    """A second text from a customer with an open job: add it to that job, never start another.
    Returns the job if it should be triaged again (caller does that outside the lock)."""
    fields: dict = {"raw_text": "\n".join(t for t in (job.raw_text, text) if t)}
    if photo_url and not job.photo_url:
        fields["photo_url"] = photo_url
    db.update_job_fields(job.id, **fields)
    db.log_event(job.id, "messaging", f"customer follow-up added to this job: {text!r}"
                 + (" + photo" if photo_url else ""))
    job = db.get_job(job.id)

    if job.status == JobStatus.new:
        # Waiting on an answer to intake's follow-up question: triage again with everything so far,
        # unless the first triage is still running (it re-reads the job, so it sees this text).
        return None if job.id in _triaging else job

    update = f'Update from the customer: "{text}"' if text else "The customer sent another photo."
    if job.status in (JobStatus.triaged, JobStatus.offered):
        pending = [o for o in db.list_offers(job.id) if o.response is None]
        if pending and (tech := db.get_tech(pending[-1].tech_id)):
            send_text(tech.phone, update, job_id=job.id, photo_url=photo_url)
        send_text(job.customer_phone, "Thanks, I added that to your request. Still finding a plumber.",
                  job_id=job.id)
    else:  # accepted / booked: the assigned tech owns it now
        tech = db.get_tech(job.assigned_tech_id) if job.assigned_tech_id else None
        if tech:
            send_text(tech.phone, update, job_id=job.id, photo_url=photo_url)
            send_text(job.customer_phone, f"Thanks, I passed that on to {_first(tech.name)}.", job_id=job.id)
        else:
            send_text(job.customer_phone, "Thanks, I added that to your request.", job_id=job.id)
    return None


def _start_timer(offer_id: str, job_id: str, delay: float = OFFER_TIMEOUT_SECONDS) -> None:
    t = threading.Timer(max(delay, 0.1), _on_timeout, args=(offer_id, job_id))
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
    return "\n".join([head, f'Customer said: "{job.raw_text}"', f"Parts: {parts}",
                      f"Quote: {_quote_text(job)}", f"Why you: {', '.join(reasons)}", "Reply YES or NO."])


def offer_next(job_id: str) -> None:
    """Offer the job to the best-ranked tech who hasn't seen it and can actually be texted."""
    job = db.get_job(job_id)
    tried = {o.tech_id for o in db.list_offers(job_id)}
    top, tech, skipped = None, None, []
    for r in rank_techs(job):
        if r.tech_id in tried:
            continue
        t = db.get_tech(r.tech_id)
        if not _reachable(t.phone):
            skipped.append(t.name)
            continue
        top, tech = r, t
        break
    if skipped:
        db.log_event(job_id, "messaging", f"skipped {', '.join(skipped)}: they haven't texted the Electron line, "
                                          "so they can't be messaged")
    if not tech:
        advance(job_id, JobStatus.failed, "no reachable techs left")
        send_text(job.customer_phone, "Sorry, no one is free right now. The owner will call you.", job_id=job_id)
        return

    offer = db.insert_offer(Offer(job_id=job_id, tech_id=tech.id, sent_at=datetime.now(timezone.utc)))
    advance(job_id, JobStatus.offered, f"to {tech.name}")
    send_text(tech.phone, _offer_text(job, top.reasons), job_id=job_id, photo_url=job.photo_url)
    _start_timer(offer.id, job_id)


def _on_timeout(offer_id: str, job_id: str) -> None:
    with _dispatch_lock:
        _timers.pop(offer_id, None)
        pending = [o for o in db.list_offers(job_id) if o.id == offer_id and o.response is None]
        if not pending or db.get_job(job_id).status != JobStatus.offered:
            return  # tech already answered, or the job moved on
        db.update_offer(offer_id, OfferResponse.timeout.value)
        db.log_event(job_id, "messaging", f"no reply in {OFFER_TIMEOUT_SECONDS:.0f}s, moving on")
        offer_next(job_id)


def resume_offers() -> int:
    """After a restart, re-arm timers for offers still waiting (or time out overdue ones).
    Timers live in memory, so without this an offer made before a restart waits forever."""
    now = datetime.now(timezone.utc)
    resumed = 0
    with _dispatch_lock:
        for job in db.list_jobs():
            if job.status != JobStatus.offered:
                continue
            for o in db.list_offers(job.id):
                if o.response is None and o.id not in _timers:
                    sent = o.sent_at if o.sent_at.tzinfo else o.sent_at.replace(tzinfo=timezone.utc)
                    _start_timer(o.id, job.id, OFFER_TIMEOUT_SECONDS - (now - sent).total_seconds())
                    resumed += 1
    if resumed:
        db.log_event(None, "messaging", f"resumed {resumed} pending offer timer(s) after restart")
    return resumed


# ---------- tech flow ----------

def _pending_offer_for(tech_id: str) -> Offer | None:
    """This tech's newest unanswered offer on a job that is still being offered."""
    rows = [r for r in db.store.select("offers", tech_id=tech_id) if not r.get("response")]
    for row in sorted(rows, key=lambda r: r["sent_at"], reverse=True):
        job = db.get_job(row["job_id"])
        if job and job.status == JobStatus.offered:
            return Offer(**row)
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
    slots = free_slots(tech_id) if SCHEDULING_ENABLED else []
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
