"""Answers a tech's questions about a job, inside iMessage. Owner: Person A.

answer(job, tech, question, accepted, share_contact) -> str

Gemini gets only the job's facts (and the photo) and must answer from them. The
customer's number is only in the facts once the job is booked. Directions are an
Apple Maps link built here, never by the model.

FALLBACK (no GEMINI_API_KEY or every model fails): keyword answers for parts, photo,
directions, price and the customer's number.
"""
from __future__ import annotations

import os
from typing import Optional

from matching.rank import miles
from shared.db import log_event
from shared.schema import Job

PROMPT = """You are Electron, a dispatcher texting a technician about one job. Reply to the technician's
message below using only the facts (and the photo, if attached). Rules:
- At most 2 short sentences, under 40 words. Plain text, no Markdown, no greetings or sign-offs.
- Reply to what they actually said. Do not repeat the job summary.
- A question the facts answer: answer it directly with the specific detail (part, size, price, cause).
- A question the facts don't answer: say that detail isn't known yet, plus one related fact that helps.
- They report a finding or rule something out: acknowledge it in a few words and name the next
  likely cause from the facts and how to check it.
- Off-topic chat: one short friendly line that steers back to the job.
Never invent an address, time, price, part size or phone number. {closing}

FACTS
{facts}

TECHNICIAN'S MESSAGE: "{question}\""""


def maps_link(job: Job) -> Optional[str]:
    return f"https://maps.apple.com/?ll={job.location.lat},{job.location.lng}&q=Customer" if job.location else None


def _facts(job: Job, tech, share_contact: bool) -> str:
    lines = [f"Problem: {job.issue or 'not identified yet'}",
             f"Urgency: {job.urgency.value if job.urgency else 'unknown'}",
             f'Customer said: "{job.raw_text}"']
    if job.photo_description:
        lines.append(f"Photo shows: {job.photo_description}")
    for p in job.parts:
        price = f"${p.price:.2f}" if p.price else "no price"
        lines.append(f"Likely part: {p.name} ({p.confidence:.0%} likely, {price}{', ' + p.url if p.url else ''})")
    if not job.parts:
        lines.append("Likely parts: none identified")
    lines.append(f"Quote for the customer: ${job.quote.low:.0f}-${job.quote.high:.0f}" if job.quote
                 else "Quote: not available yet")
    if job.prep:  # research from agents/job_prep.py (model knowledge, estimates)
        for c in job.prep.get("causes", []):
            lines.append(f"Likely cause ({c['likelihood']:.0%}): {c['cause']}; confirm by: {c['confirm']}")
        for p in job.prep.get("parts_plan", []):
            live = p.get("live")
            price = (f"${live['price']:.2f} at {live['store']}, buy link {live['link']}" if live
                     else f"est. ${p['est_low']:.0f}-{p['est_high']:.0f}, no live listing")
            lines.append(f"Recommended part: {p['pick']} for the {p['part']} ({price}; {p['buy']}; {p['why']}"
                         + (f"; skip {p['avoid']}" if p.get("avoid") else "") + ")")
        if job.prep.get("tools"):
            lines.append(f"Tools to bring: {', '.join(job.prep['tools'])}")
        if job.prep.get("watch_out"):
            lines.append(f"Watch out: {' '.join(job.prep['watch_out'])}")
    if job.location:
        lines.append(f"Distance from the technician: {miles((job.location.lat, job.location.lng), (tech.lat, tech.lng)):.1f} mi")
        lines.append(f"Directions link (approximate customer location, no street address yet): {maps_link(job)}")
    lines.append("Arrival time: booked; see the booking text" if job.status.value == "booked"
                 else "Arrival time: not booked yet (the customer must OK the estimate first)")
    lines.append(f"Customer's phone: {job.customer_phone}" if share_contact
                 else "Customer's phone: shared once the job is booked")
    return "\n".join(lines)


def _fallback(job: Job, tech, question: str, share_contact: bool) -> str:
    q = question.lower()
    if any(w in q for w in ("part", "bring", "need", "size")):
        parts = "; ".join(f"{p.name} ({p.confidence:.0%} likely" + (f", ${p.price:.2f}" if p.price else "") + ")"
                          for p in job.parts)
        return f"Likely parts: {parts}." if parts else "No parts identified yet."
    if any(w in q for w in ("where", "address", "direction", "map", "far")):
        link = maps_link(job)
        return f"Here's the approximate location (no street address yet): {link}" if link else "No location yet."
    if any(w in q for w in ("photo", "picture", "look", "see")):
        return f"The photo shows: {job.photo_description}" if job.photo_description else "No photo on this job."
    if any(w in q for w in ("price", "quote", "cost", "pay")):
        return f"Quote for the customer: ${job.quote.low:.0f}-${job.quote.high:.0f}." if job.quote else "No quote yet."
    if any(w in q for w in ("number", "phone", "call", "contact")):
        return (f"Customer's number: {job.customer_phone}" if share_contact
                else "You'll get the customer's number once the job is booked.")
    return "I can tell you the parts, what the photo shows, where it is, or the quote."


def answer(job: Job, tech, question: str, *, accepted: bool, share_contact: bool = False) -> str:
    """accepted: the tech has taken the job (else every reply ends with the YES/NO prompt).
    share_contact: the job is booked, so the customer's phone may be given out."""
    closing = ("" if accepted else
               'End with: "Reply YES to take it or NO to pass."')
    reply = None
    if os.getenv("GEMINI_API_KEY"):
        from google import genai
        from google.genai import types

        from agents.intake import GEMINI_MODELS, _load_image  # B's model list + HEIC-safe loader

        image = None
        try:
            image = _load_image(job.photo_url)
        except Exception:
            pass
        prompt = PROMPT.format(closing=closing, facts=_facts(job, tech, share_contact), question=question)
        contents = ([types.Part.from_bytes(data=image[1], mime_type=image[0])] if image else []) + [prompt]
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"), http_options=types.HttpOptions(timeout=10_000))
        for model in GEMINI_MODELS:
            try:
                resp = client.models.generate_content(model=model, contents=contents,
                                                      config=types.GenerateContentConfig(temperature=0.2))
                reply = (resp.text or "").strip()
                if reply:
                    log_event(job.id, "assistant", f"[{model}] {tech.name} asked {question!r}")
                    break
            except Exception as e:
                log_event(job.id, "assistant", f"{model} failed ({e.__class__.__name__}), trying next")
    if not reply:
        reply = _fallback(job, tech, question, share_contact)
        if not accepted:
            reply += " Reply YES to take it or NO to pass."
        log_event(job.id, "assistant", f"FALLBACK answer for {tech.name}: {question!r}")
    # The model must not leak the number before booking, whatever it was asked.
    if not share_contact and job.customer_phone in reply:
        reply = reply.replace(job.customer_phone, "(shared once the job is booked)")
    return reply
