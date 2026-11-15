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

from agents import find_parts, intake, job_prep, quote
from matching import book, free_slots, rank_techs
from messaging import ai_router, photon, tech_assistant
from messaging.send import send_text
from messaging.state import advance
from shared import db
from shared.schema import Job, JobStatus, Location, Offer, OfferResponse

OFFER_TIMEOUT_SECONDS = float(os.getenv("OFFER_TIMEOUT_SECONDS", "60"))
# Off until matching/calendar.py talks to a real calendar: its stub invents free slots,
# and we don't tell customers a made-up arrival time. Set SCHEDULING_ENABLED=true to use it.
SCHEDULING_ENABLED = os.getenv("SCHEDULING_ENABLED", "false").lower() in ("1", "true", "yes")

YES = re.compile(r"^\s*(y|ya|yes|yep|yeah|sure|ok|okay|on it|accept|i'?ll take it)\b", re.I)
NO = re.compile(r"^\s*(n|no|nope|nah|can'?t|cannot|pass|decline)\b", re.I)


def _is_yes(text: str) -> bool:
    """'yes!', 'ok', 'sure I'll take it'. A question ('ok what size trap?') is never a yes."""
    return bool(YES.match(text)) and "?" not in text and len(text.split()) <= 5


def _is_no(text: str) -> bool:
    return bool(NO.match(text)) and "?" not in text and len(text.split()) <= 8
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
        with _dispatch_lock:
            tech = db.get_tech_by_phone(phone)
            if tech:
                handle_tech_reply(tech.id, text)
                return
            open_job = None if NEW_JOB.match(text) else _open_job_for(phone)
        _handle_customer_text(phone, text, photo_url, open_job)
    except Exception as e:  # surface on the dashboard instead of dying in a background task
        db.log_event(None, "error", f"handling text from {phone}: {e!r}")
        raise


def _handle_customer_text(phone: str, text: str, photo_url: str | None, open_job: Job | None) -> None:
    """Gemini reads the text (and photo) against the customer's open job and decides:
    same job (forward to its tech), new job (full triage), or chitchat (just reply)."""
    # Waiting on this customer's OK for an estimate? A plain yes/no answers that, nothing else.
    if open_job and open_job.status == JobStatus.accepted and not photo_url:
        with _dispatch_lock:
            job = db.get_job(open_job.id)
            if job.status == JobStatus.accepted and _approves(text):
                _confirm(job)
                return
            if job.status == JobStatus.accepted and _declines(text):
                _decline_estimate(job)
                return
    photon.typing(phone)  # '...' while the AI thinks
    if NEW_JOB.match(text):
        action, reply = "new_job", None
    else:
        tech = db.get_tech(open_job.assigned_tech_id) if open_job and open_job.assigned_tech_id else None
        r = ai_router.route(open_job, text, photo_url, tech_name=tech.name if tech else None,
                            log_id=open_job.id if open_job else None)
        action, reply = r.action, r.reply

    if action == "chitchat":
        send_text(phone, reply or "Hi, this is Electron. Text a photo of the problem and I'll get a plumber on it.",
                  job_id=open_job.id if open_job else None)
        return
    with _dispatch_lock:
        if action == "same_job" and open_job:
            to_triage = handle_customer_followup(open_job, text, photo_url)
        else:
            to_triage = _new_job(phone, text, photo_url)
    if to_triage:
        _triage(to_triage)  # slow AI work, outside the lock


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
        photon.typing(job.customer_phone)  # '...' while Gemini reads the photo
        triaged = intake(job.photo_url, job.raw_text, customer_phone=job.customer_phone, job_id=job.id)
        _send_safety_once(job, triaged.safety_instruction)
        if triaged.follow_up_question:
            send_text(job.customer_phone, triaged.follow_up_question, job_id=job.id)
            return
        parts = find_parts(triaged)
        q = quote(triaged, parts)
        with _dispatch_lock:
            if db.get_job(job.id).status != JobStatus.new:
                return  # someone already moved it on
            db.update_job_fields(job.id, trade=triaged.trade, issue=triaged.issue, urgency=triaged.urgency,
                                 parts=parts, location=triaged.location or _demo_location(), quote=q,
                                 **{k: v for k, v in (("photo_description", triaged.photo_description),
                                                      ("history_note", triaged.history_note)) if v})
            advance(job.id, JobStatus.triaged)
            welcome = "Welcome back! " if triaged.history_note else ""
            send_text(job.customer_phone, f"{welcome}Got it, finding a plumber for you now.", job_id=job.id)
            offer_next(job.id)
        # Research the job while techs decide, so the prep is ready when someone says YES.
        threading.Thread(target=_run_prep, args=(job.id,), daemon=True).start()
    finally:
        with _dispatch_lock:
            _triaging.discard(job.id)


def _run_prep(job_id: str) -> None:
    try:
        prep = job_prep.prepare(db.get_job(job_id))
        if prep:
            with _dispatch_lock:
                db.update_job_fields(job_id, prep=prep)
            _maybe_send_prep(job_id)
    except Exception as e:
        db.log_event(job_id, "error", f"job prep failed: {e!r}")


def _maybe_send_prep(job_id: str) -> None:
    """Text the prep to the tech who took the job, once: on accept if it's ready, else when it's done."""
    with _dispatch_lock:
        job = db.get_job(job_id)
        if not job or not job.prep or job.prep.get("sent_to") or not job.assigned_tech_id \
                or job.status not in (JobStatus.accepted, JobStatus.booked):
            return
        tech = db.get_tech(job.assigned_tech_id)
        db.update_job_fields(job_id, prep={**job.prep, "sent_to": tech.id})
    send_text(tech.phone, job_prep.tech_message(job, job.prep), job_id=job_id, markdown=True)


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
            reminder = (f" To confirm the {_quote_text(job)} estimate, reply OK." if job.status == JobStatus.accepted
                        else "")
            send_text(job.customer_phone, f"Thanks, I passed that on to {_first(tech.name)}.{reminder}", job_id=job.id)
        else:
            send_text(job.customer_phone, "Thanks, I added that to your request.", job_id=job.id)
    return None


def _start_timer(offer_id: str, job_id: str, delay: float = OFFER_TIMEOUT_SECONDS) -> None:
    t = threading.Timer(max(delay, 0.1), _on_timeout, args=(offer_id, job_id))
    t.daemon = True
    _timers[offer_id] = t
    t.start()


def _send_safety_once(job: Job, instruction: str | None) -> None:
    """Danger first: text the customer what to do right now, before any follow-up or dispatch.
    Once per job, even when a follow-up answer triggers triage again."""
    if not instruction or any(e.get("agent") == "safety" for e in db.list_events(job.id)):
        return
    db.log_event(job.id, "safety", f"told customer: {instruction}")
    send_text(job.customer_phone, instruction, job_id=job.id)


URGENCY_LABEL = {"high": "🔴 Urgent", "medium": "🟡 Soon", "low": "🟢 Whenever"}


def _offer_text(job: Job, reasons: list[str]) -> str:
    """Light Markdown: Photon renders **bold** as iMessage formatting (plain-text fallback strips it)."""
    head = f"**New job · {job.issue}**" if job.issue else "**New job · problem not identified yet**"
    urgency = URGENCY_LABEL.get(job.urgency.value) if job.urgency else None
    parts = "; ".join(f"{p.name} ({p.confidence:.0%} likely)" for p in job.parts) or "unknown"
    lines = [head, *([urgency] if urgency else []), "",
             f'**Customer said:** "{job.raw_text}"',
             *([f"**Photo shows:** {job.photo_description}"] if job.photo_description else []),
             *([f"**History:** {job.history_note}"] if job.history_note else []),
             f"**Likely parts:** {parts}",
             f"**Quote:** {_quote_text(job)}",
             f"**Why you:** {', '.join(reasons)}", "",
             "Reply **YES** or **NO**. Or ask me anything about it: parts, the photo, where it is."]
    return "\n".join(lines)


# Off by default: everything stays inside iMessage. JOB_CARD_LINKS=true also sends the web job card.
JOB_CARD_LINKS = os.getenv("JOB_CARD_LINKS", "false").lower() in ("1", "true", "yes")


def job_card_url(offer_id: str) -> str | None:
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}/j/{offer_id}" if base else None


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
    send_text(tech.phone, _offer_text(job, top.reasons), job_id=job_id, photo_url=job.photo_url, markdown=True)
    if JOB_CARD_LINKS and (card := job_card_url(offer.id)):
        send_text(tech.phone, card, job_id=job_id)  # its own message so iMessage shows it as a card
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


def _current_job_for(tech_id: str) -> Job | None:
    """The newest recent job this tech has taken (accepted or booked)."""
    cutoff = datetime.now(timezone.utc).timestamp() - JOB_WINDOW_HOURS * 3600
    for row in reversed(db.store.select("jobs", assigned_tech_id=tech_id)):
        created = row.get("created_at")
        if row["status"] in ("accepted", "booked") and created and datetime.fromisoformat(created).timestamp() > cutoff:
            return Job(**row)
    return None


def handle_tech_reply(tech_id: str, text: str) -> None:
    offer = _pending_offer_for(tech_id)
    tech = db.get_tech(tech_id)
    if offer is None:
        job = _current_job_for(tech_id)
        if job and text.strip():  # questions about the job they already took
            send_text(tech.phone, tech_assistant.answer(job, tech, text, accepted=True), job_id=job.id)
        else:
            send_text(tech.phone, "No open offers for you right now.")
        return
    if timer := _timers.pop(offer.id, None):
        timer.cancel()

    if _is_yes(text):
        _accept(offer, tech, "said yes")
    elif _is_no(text):
        _decline(offer, tech, "declined")
    else:  # a question about the offer: answer it in the chat, keep the offer open
        _start_timer(offer.id, offer.job_id)
        photon.typing(tech.phone)
        send_text(tech.phone, tech_assistant.answer(db.get_job(offer.job_id), tech, text, accepted=False),
                  job_id=offer.job_id)


def _accept(offer: Offer, tech, how: str) -> None:
    db.update_offer(offer.id, OfferResponse.yes.value)
    db.update_job_fields(offer.job_id, assigned_tech_id=tech.id)
    advance(offer.job_id, JobStatus.accepted, f"{tech.name} {how}")
    _request_approval(offer.job_id, tech)
    _maybe_send_prep(offer.job_id)


def _decline(offer: Offer, tech, how: str) -> None:
    db.update_offer(offer.id, OfferResponse.no.value)
    db.log_event(offer.job_id, "messaging", f"{tech.name} {how}")
    offer_next(offer.job_id)


def respond_to_offer(offer_id: str, accept: bool) -> str:
    """The job card's Accept / Pass buttons. Same effect as texting YES / NO.
    Returns what happened: "accepted", "passed", or why nothing changed."""
    with _dispatch_lock:
        rows = db.store.select("offers", id=offer_id)
        if not rows:
            return "not found"
        offer = Offer(**rows[0])
        if offer.response is not None:
            return f"already {offer.response.value}"
        if db.get_job(offer.job_id).status != JobStatus.offered:
            return "no longer open"
        tech = db.get_tech(offer.tech_id)
        if timer := _timers.pop(offer.id, None):
            timer.cancel()
        if accept:
            _accept(offer, tech, "accepted from the job card")
            return "accepted"
        _decline(offer, tech, "passed from the job card")
        return "passed"


# ---------- customer approves the estimate before the truck rolls ----------
# accepted = a tech took it and we're waiting on the customer's OK; booked = both sides confirmed.

APPROVE = re.compile(r"^\s*(ok|okay|k|yes|yep|yeah|sure|confirm(ed)?|approved?|sounds good|go ahead|do it|deal|book it)\b", re.I)
DECLINE = re.compile(r"^\s*(no|nope|nah|cancel|too (much|expensive|pricey)|not now|never ?mind|don'?t)\b", re.I)


def _approves(text: str) -> bool:
    return bool(APPROVE.match(text)) and "?" not in text and len(text.split()) <= 6


def _declines(text: str) -> bool:
    return bool(DECLINE.match(text)) and "?" not in text and len(text.split()) <= 10


def _request_approval(job_id: str, tech) -> None:
    job = db.get_job(job_id)
    what = f" ({job.issue})" if job.issue else ""
    send_text(job.customer_phone,
              f"Good news: {tech.name} can take your job{what}. Estimated price: {_quote_text(job)} "
              f"for labor and parts. Reply OK to confirm, or NO to cancel.",
              job_id=job_id)
    send_text(tech.phone,
              f"The job is yours. I've asked the customer to OK the {_quote_text(job)} estimate "
              f"and I'll text you the moment they do.",
              job_id=job_id)
    db.log_event(job_id, "messaging", "waiting for the customer to approve the estimate")


def _confirm(job: Job) -> None:
    """Customer said OK: the job is booked. With real scheduling, the time slot is booked here too."""
    tech = db.get_tech(job.assigned_tech_id)
    slots = free_slots(tech.id) if SCHEDULING_ENABLED else []
    if slots:
        slot = slots[0]
        book(tech.id, job.id, slot)
        advance(job.id, JobStatus.booked, f"customer approved; {slot.start:%a %I:%M %p}")
        send_text(job.customer_phone, f"You're booked: {tech.name} arrives {slot.start:%a %b %d, %I:%M %p}. "
                                      f"Price: {_quote_text(job)}.", job_id=job.id)
        send_text(tech.phone, f"Customer approved the {_quote_text(job)} estimate. Booked for "
                              f"{slot.start:%a %I:%M %p}, added to your calendar.", job_id=job.id)
        return
    # Scheduling isn't connected yet: confirm the job, and say plainly that the time is set by text.
    advance(job.id, JobStatus.booked, "customer approved the estimate")
    send_text(job.customer_phone, f"Confirmed. {tech.name} will text you shortly to set a time.", job_id=job.id)
    send_text(tech.phone, f"Customer approved the {_quote_text(job)} estimate. Text them at "
                          f"{job.customer_phone} to set a time.", job_id=job.id)


def _decline_estimate(job: Job) -> None:
    tech = db.get_tech(job.assigned_tech_id) if job.assigned_tech_id else None
    advance(job.id, JobStatus.failed, "customer declined the estimate")
    send_text(job.customer_phone, "No problem, I've cancelled it. Text me anytime if you need a plumber.",
              job_id=job.id)
    if tech:
        send_text(tech.phone, "The customer passed on the estimate, so this job is cancelled. "
                              "Thanks for taking it.", job_id=job.id)
