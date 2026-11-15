"""Gemini decides what a customer's text means before dispatch acts on it. Owner: Person A.

route(open_job, text, photo_url, booked_at=...) -> Route
  same_job  adds to the customer's open job (more detail, an answer to our question, a photo
            of the same problem) -> forward to that job's tech
  new_job   a plumbing problem different from the open job, or no open job -> full triage.
            dispatch only starts a job when there is a photo (texts alone wait for one)
  reply     a question about their open job (status, timing, price) -> answer from the facts
  ignore    unrelated to plumbing or their job (greetings, thanks, spam, off-topic) -> no reply

FALLBACK: with no GEMINI_API_KEY or if every model fails, simple rules decide, so the
demo never stops: a photo while a job is already taken -> new job; greetings -> ignore;
otherwise same job (or new job when there is no open job).
"""
from __future__ import annotations

import os
import re
from typing import Literal, Optional

from pydantic import BaseModel

from shared.db import log_event
from shared.schema import Job

GREETING = re.compile(r"^\s*(hi|hello|hey|yo|thanks|thank you|thx|ok|okay|cool|great|good morning|good evening)\b[\s!.]*$", re.I)

PROMPT = """You sort incoming text messages for Electron, a plumbing dispatch service that customers
reach only by iMessage. You decide what to do with each message; you never chat for fun.

WHAT YOU KNOW ABOUT THIS CUSTOMER
{context}

THE NEW MESSAGE
"{text}"{photo_note}

Choose exactly one action:
- "same_job": the message adds to the open job: more detail about the same problem, an answer to a
  question we asked, a new photo of the same fixture, or "it's getting worse".
- "new_job": a plumbing, drain, water heater, gas or sewer problem that is different from the open
  job (another fixture or room), or any such problem when there is no open job.
- "reply": a question about the open job that Electron can answer from the facts above (where it
  stands, who is coming, when, the price).
- "ignore": anything not about a plumbing problem or their job: greetings, thanks, emoji, jokes,
  spam, questions about other topics, and photos that don't show plumbing.
When unsure between "reply" and "ignore", choose "ignore". Use "reply" only with an open job.

For "reply" also write `reply`: at most two short sentences from Electron, plain text, no emoji,
stating only facts listed above. Never promise an arrival, time, price or ETA that is not listed.
For every other action, `reply` is null. `why`: a few words on your choice."""


class Route(BaseModel):
    action: Literal["same_job", "new_job", "reply", "ignore"]
    reply: Optional[str] = None
    why: str = ""


def _context(job: Optional[Job], tech_name: Optional[str], booked_at: Optional[str] = None) -> str:
    if job is None:
        return "No open job. They have not sent us a problem yet."
    status = {"new": "being looked at by Electron", "triaged": "being matched to a plumber",
              "offered": "offered to a plumber, waiting for them to accept",
              "accepted": f"accepted by {tech_name or 'a plumber'}, waiting for the customer to OK the estimate",
              "booked": f"booked with {tech_name or 'a plumber'}"}
    lines = [f'Open job: "{job.issue or (job.raw_text or "").split(chr(10))[0][:120] or "a plumbing problem"}"',
             f"Status: {status.get(job.status.value, job.status.value)}"]
    if job.quote:
        lines.append(f"Estimate: ${job.quote.low:.0f}-${job.quote.high:.0f} for labor and parts")
    if job.status.value == "booked":
        lines.append(f"Arrival: {booked_at}" if booked_at
                     else f"Arrival: not set; {tech_name or 'the plumber'} will text them a time")
    else:
        lines.append("Arrival: not scheduled yet")
    return "\n".join(lines)


def _fallback(job: Optional[Job], text: str, photo_url: Optional[str], tech_name: Optional[str]) -> Route:
    if photo_url and job and job.status.value in ("accepted", "booked"):
        return Route(action="new_job", why="FALLBACK: new photo while the open job is already taken")
    if not photo_url and GREETING.match(text or ""):
        return Route(action="ignore", why="FALLBACK: greeting or thanks")
    return Route(action="same_job" if job else "new_job", why="FALLBACK: default")


def route(job: Optional[Job], text: str, photo_url: Optional[str] = None, *,
          tech_name: Optional[str] = None, log_id: Optional[str] = None,
          booked_at: Optional[str] = None) -> Route:
    if not os.getenv("GEMINI_API_KEY"):
        return _fallback(job, text, photo_url, tech_name)

    from google import genai
    from google.genai import types

    from agents.intake import GEMINI_MODELS, _load_image  # B's model list + HEIC-safe loader

    image = None
    try:
        image = _load_image(photo_url)
    except Exception:
        pass
    prompt = PROMPT.format(context=_context(job, tech_name, booked_at), text=text or "(no text)",
                           photo_note="\n(They also attached the photo shown.)" if image else "")
    contents: list = [types.Part.from_bytes(data=image[1], mime_type=image[0])] if image else []
    contents.append(prompt)

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"),
                          http_options=types.HttpOptions(timeout=10_000))
    for model in GEMINI_MODELS:
        try:
            resp = client.models.generate_content(
                model=model, contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json",
                                                   response_schema=Route, temperature=0.1))
            r = resp.parsed if isinstance(resp.parsed, Route) else Route.model_validate_json(resp.text)
            if r.action == "reply" and (not r.reply or job is None):
                r.action, r.reply = "ignore", None  # nothing grounded to say: stay quiet
            log_event(log_id, "router", f"[{model}] {r.action}" + (f": {r.why}" if r.why else ""))
            return r
        except Exception as e:
            log_event(log_id, "router", f"{model} failed ({e.__class__.__name__}), trying next")
    r = _fallback(job, text, photo_url, tech_name)
    log_event(log_id, "router", f"{r.action} ({r.why})")
    return r
