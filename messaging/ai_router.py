"""Gemini decides what a customer's text means before dispatch acts on it. Owner: Person A.

route(open_job, text, photo_url) -> Route
  same_job  the text/photo adds to the customer's open job -> forward to that job's tech
  new_job   a different problem (or no open job and a real request) -> new job + full triage
  chitchat  greeting / thanks / no new information -> reply, touch nothing

FALLBACK: with no GEMINI_API_KEY or if every model fails, simple rules decide, so the
demo never stops: a photo while a job is already taken -> new job; greetings -> chitchat;
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

PROMPT = """You route text messages for Electron, a plumbing dispatch service run over iMessage.

{context}

The customer just sent: "{text}"{photo_note}

Decide what this message is:
- "same_job": it adds to the open job above (more detail, a new photo of the same problem,
  a question about timing or the tech, "it's getting worse").
- "new_job": it describes a different problem, fixture or room than the open job, or there
  is no open job and it asks for help with something. A photo of something unrelated to
  the open job is a new job.
- "chitchat": a greeting, thanks or small talk with no new problem in it.

For "chitchat" also write `reply`: one or two short, warm sentences from Electron, in plain
text, no emoji. {reply_hint} Otherwise reply is null."""


class Route(BaseModel):
    action: Literal["same_job", "new_job", "chitchat"]
    reply: Optional[str] = None
    why: str = ""


def _context(job: Optional[Job], tech_name: Optional[str]) -> tuple[str, str]:
    if job is None:
        return ("The customer has no open job.",
                "Say hi, say you're Electron and can get a plumber out, and ask them to text a photo "
                "and a sentence about the problem.")
    status = {"new": "being looked at", "triaged": "being matched to a plumber", "offered": "offered to a plumber",
              "accepted": f"taken by {tech_name or 'a plumber'}", "booked": f"booked with {tech_name or 'a plumber'}"}
    what = job.issue or (job.raw_text or "").split("\n")[0][:120] or "a plumbing problem"
    where = status.get(job.status.value, job.status.value)
    next_step = (f"{tech_name or 'The plumber'} will text them to set a time" if job.status.value in ("accepted", "booked")
                 else "we're finding a plumber")
    return (f'The customer has an open job: "{what}". It is {where}.',
            f"Say where it stands ({next_step}) and that they can send a photo if something new comes up. "
            "Only state those facts: never say anyone is on the way, never give or guess a time, price or ETA.")


def _fallback(job: Optional[Job], text: str, photo_url: Optional[str], tech_name: Optional[str]) -> Route:
    if photo_url and job and job.status.value in ("accepted", "booked"):
        return Route(action="new_job", why="FALLBACK: new photo while the open job is already taken")
    if not photo_url and GREETING.match(text or ""):
        if job:
            who = tech_name or "your plumber"
            reply = (f"Hi! {who} has your request and will text you to set a time. "
                     "Send a photo anytime if something new comes up.")
        else:
            reply = "Hi, this is Electron. Text a photo and a sentence about the problem and I'll get a plumber on it."
        return Route(action="chitchat", reply=reply, why="FALLBACK: greeting")
    return Route(action="same_job" if job else "new_job", why="FALLBACK: default")


def route(job: Optional[Job], text: str, photo_url: Optional[str] = None, *,
          tech_name: Optional[str] = None, log_id: Optional[str] = None) -> Route:
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
    context, hint = _context(job, tech_name)
    prompt = PROMPT.format(context=context, text=text or "(no text)", reply_hint=hint,
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
            if r.action == "chitchat" and not r.reply:
                r.reply = _fallback(job, "hi", None, tech_name).reply
            log_event(log_id, "router", f"[{model}] {r.action}" + (f": {r.why}" if r.why else ""))
            return r
        except Exception as e:
            log_event(log_id, "router", f"{model} failed ({e.__class__.__name__}), trying next")
    r = _fallback(job, text, photo_url, tech_name)
    log_event(log_id, "router", f"{r.action} ({r.why})")
    return r
