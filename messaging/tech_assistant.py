"""Answers a tech's questions about a job, inside iMessage. Owner: Person A.

answer(job, tech, question, accepted) -> str

Gemini gets only the job's facts (and the photo) and must answer from them. The
customer's number is only in the facts once this tech has accepted. Directions are an
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

PROMPT = """You are Electron, texting a plumber about one job over iMessage. Answer the plumber's
message using ONLY the facts below (and the photo, if attached). Keep it under 60 words, plain
sentences, no Markdown. If the facts don't contain the answer, say you don't know yet. Never invent
an address, time, price, part size or phone number. {closing}

FACTS
{facts}

PLUMBER'S MESSAGE: "{question}\""""


def maps_link(job: Job) -> Optional[str]:
    return f"https://maps.apple.com/?ll={job.location.lat},{job.location.lng}&q=Customer" if job.location else None


def _facts(job: Job, tech, accepted: bool) -> str:
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
    if job.location:
        lines.append(f"Distance from the plumber: {miles((job.location.lat, job.location.lng), (tech.lat, tech.lng)):.1f} mi")
        lines.append(f"Directions link (approximate customer location, no street address yet): {maps_link(job)}")
    lines.append("Arrival time: not scheduled; the plumber and customer agree on it by text")
    lines.append(f"Customer's phone: {job.customer_phone}" if accepted
                 else "Customer's phone: only shared after the plumber accepts the job")
    return "\n".join(lines)


def _fallback(job: Job, tech, question: str, accepted: bool) -> str:
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
        return f"Customer's number: {job.customer_phone}" if accepted else "You'll get the customer's number once you accept."
    return "I can tell you the parts, what the photo shows, where it is, or the quote."


def answer(job: Job, tech, question: str, *, accepted: bool) -> str:
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
        prompt = PROMPT.format(closing=closing, facts=_facts(job, tech, accepted), question=question)
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
        reply = _fallback(job, tech, question, accepted)
        if not accepted:
            reply += " Reply YES to take it or NO to pass."
        log_event(job.id, "assistant", f"FALLBACK answer for {tech.name}: {question!r}")
    # The model must not leak the number before acceptance, whatever it was asked.
    if not accepted and job.customer_phone in reply:
        reply = reply.replace(job.customer_phone, "(shared once you accept)")
    return reply
