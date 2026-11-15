"""intake(photo_url, text) -> Job.   Owner: Person B.

Vision LLM triage: photo + customer text -> structured Job (trade, issue,
urgency, likely parts with confidence). If the photo is unclear or not
plumbing, sets job.follow_up_question instead of guessing.

Uses Gemini (google-genai) with a JSON response schema.

FALLBACK: with no GEMINI_API_KEY, or if the API call fails, a keyword
table triages from the text alone so the demo loop never stops.
"""
from __future__ import annotations

import io
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import httpx
from pydantic import BaseModel, ValidationError

from shared import db
from shared.db import log_event
from shared.schema import Job, Location, Part, Urgency

# Tried in order: if one is overloaded (503) or fails, the next one answers.
GEMINI_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.6-flash").split(",") if m.strip()]
MAX_EDGE_PX = 1568
DEFAULT_FOLLOW_UP = "Can you send a closer photo of where the water is coming from?"
PLUMBING_TRADES = {"plumbing", "drain", "water_heater", "gas", "sewer"}

# Gas is the one case where the AI must never decide alone: this exact text always goes out.
GAS_WORDS = ("smell gas", "gas smell", "smells like gas", "gas leak", "rotten egg", "hissing gas")
GAS_SAFETY = ("If you smell gas, please leave the house now. Don't flip light switches or use "
              "anything with a flame, and call your gas company or 911 from outside. "
              "We'll send a plumber once it's safe.")
# FALLBACK safety texts when the LLM is down, by trade, only for high urgency.
FALLBACK_SAFETY = {
    "plumbing": ("While you wait: if water is still running, turn the shutoff valve under the "
                 "fixture clockwise until it stops. If you can't find it, use the main shutoff "
                 "where the water line enters the house."),
    "water_heater": ("While you wait: if the water heater is leaking, turn off its power or gas "
                     "and close the cold-water valve on top of the tank."),
    "sewer": "While you wait: please don't run water or flush toilets, so it doesn't back up further.",
}
MAX_HISTORY_JOBS = 5

SYSTEM_PROMPT = """You triage service requests for a small plumbing company. A customer \
texted a photo (sometimes missing) and a short description. Most customers don't know \
part names, so work out what is actually broken.

Return:
- trade: plumbing | drain | water_heater | gas | sewer | electrical.
  drain = clogs and slow drains ONLY. A leak from any pipe, trap, joint or fitting is \
plumbing, even on a drain line (a leaking P-trap is plumbing, a clogged P-trap is drain). water_heater = tank or tankless heaters. sewer = main \
line backups. gas = gas lines or smell of gas. plumbing = everything else with water \
supply, fixtures, traps, valves. Anything that is not plumbing (outlets, wiring, \
breakers) is electrical.
- issue: one short line a technician would write, naming the fixture and the failure \
(e.g. "leaking P-trap under kitchen sink").
- urgency: high if water is leaking or dripping from a pipe, trap, valve or supply line \
(under a sink counts), there is no water, no hot water, sewage backing up, or gas smell; \
medium for slow drains, running toilets, a dripping faucet spout; low for cosmetic or \
non-urgent issues.
- parts: 0-4 parts the tech should likely bring, with real trade names and sizes \
(kitchen P-trap is usually 1-1/2 in, bathroom sink P-trap 1-1/4 in, toilet flapper \
2 in or 3 in, supply lines 3/8 in compression). confidence is honest: a size read from \
a photo is at most 0.7 unless it is clearly visible or stated.
- clear_enough: false if the photo is too blurry/dark to see the problem AND the text \
doesn't name it, or if the job is not plumbing. The description alone is enough when \
it clearly names the problem.
- follow_up_question: when clear_enough is false, one short, friendly question for the \
customer (e.g. "Is water actively leaking right now?"). Otherwise null.
- photo_description: only if a photo is attached, 2-3 plain sentences for the technician \
who will get this job, describing only what is visible: the fixture and room, pipe and \
fitting materials (PVC, copper, PEX, chrome, galvanized), visible sizes, where the water, \
damage or clog is, and anything that changes the job (corrosion, cramped access, shutoff \
valve visible or not). Don't guess at what you can't see. Null if there is no photo.
- safety_instruction: only if there is danger or damage happening right now (water \
actively leaking or spraying, flooding, sewage backing up, a leaking water heater), one \
or two short, calm sentences telling the customer what to do before the plumber arrives, \
starting with "While you wait:". If water is actively leaking, spraying or flooding, \
always start with stopping the water: close the shutoff valve under that fixture (turn \
clockwise), or the main shutoff where the water line enters the house. Other safe steps: \
turn off power or gas to a leaking water heater, stop using drains during a backup, move \
valuables away from water. Never suggest repairs. Null when nothing urgent is happening.
- history_note: if earlier jobs for this customer are listed, and one is plausibly related \
(same fixture or room, the same problem coming back, work we did recently), one short \
sentence for the technician that says which earlier job and when, using only what the \
list says (don't invent what was done), e.g. "Repeat customer: we had a water heater job \
here on Jan 14 (tech Sam), possibly the same unit." Null if no earlier job is relevant."""


class _IntakePart(BaseModel):
    name: str
    confidence: float


class IntakeResult(BaseModel):
    trade: Literal["plumbing", "drain", "water_heater", "gas", "sewer", "electrical"]
    issue: str
    urgency: Literal["high", "medium", "low"]
    parts: list[_IntakePart]
    clear_enough: bool
    follow_up_question: Optional[str] = None
    photo_description: Optional[str] = None
    safety_instruction: Optional[str] = None
    history_note: Optional[str] = None


# ---------- image loading ----------

def _load_image(photo_url: Optional[str]) -> Optional[tuple[str, bytes]]:
    """Return (mime_type, jpeg_bytes) or None. Accepts http(s) URLs and local paths."""
    if not photo_url:
        return None
    if photo_url.startswith(("http://", "https://")):
        r = httpx.get(photo_url, timeout=5.0, follow_redirects=True)
        r.raise_for_status()
        raw = r.content
    else:
        raw = Path(photo_url.removeprefix("file://")).expanduser().read_bytes()

    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener  # iMessage photos are often HEIC
        register_heif_opener()
    except ImportError:
        pass

    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    img.thumbnail((MAX_EDGE_PX, MAX_EDGE_PX))
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return "image/jpeg", buf.getvalue()


# ---------- LLM call ----------

def _call_llm(model: str, image: Optional[tuple[str, bytes]], text: str,
              history: Optional[list[str]] = None) -> IntakeResult:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"),
                          http_options=types.HttpOptions(timeout=10_000))  # ms (Gemini rejects deadlines under 10 s)
    contents: list = []
    if image:
        mime_type, data = image
        contents.append(types.Part.from_bytes(data=data, mime_type=mime_type))
    prompt = f"Customer text: {text or '(no text)'}" + ("" if image else "\n(No photo was attached.)")
    if history:
        prompt += "\n\nEarlier jobs for this customer (oldest first):\n" + "\n".join(f"- {h}" for h in history)
    contents.append(prompt)

    resp = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=IntakeResult,
            temperature=0.2,
        ),
    )
    if isinstance(resp.parsed, IntakeResult):
        return resp.parsed
    if resp.text:  # schema parse failed in the SDK: validate ourselves (raises ValidationError)
        return IntakeResult.model_validate_json(resp.text)
    raise ValueError("empty response (possibly blocked by safety filters)")


# ---------- FALLBACK: keyword triage ----------

# (keywords, trade, issue, urgency, parts)
KEYWORD_TABLE = [
    (("water heater", "no hot water", "hot water heater", "tankless"), "water_heater",
     "water heater not producing hot water", "high",
     [("40 gal water heater thermocouple", 0.4), ("3/4 in water heater flex connector", 0.4)]),
    (("gas smell", "smell gas", "gas leak"), "gas", "possible gas leak", "high", []),
    (("sewer", "sewage", "backing up"), "sewer", "main sewer line backup", "high", []),
    (("burst", "supply line", "spraying"), "plumbing", "burst supply line", "high",
     [("3/8 in compression x 1/2 in FIP braided supply line", 0.6)]),
    (("toilet", "running", "keeps running"), "plumbing", "running toilet", "medium",
     [("2 in toilet flapper", 0.6), ("universal toilet fill valve", 0.5)]),
    (("clog", "clogged", "slow", "backed up", "won't drain"), "drain",
     "clogged drain", "medium", [("1/4 in x 25 ft drain auger", 0.4)]),
    (("faucet",), "plumbing", "dripping faucet", "medium",
     [("single-handle faucet cartridge", 0.5), ("faucet O-ring kit", 0.4)]),
    (("sink", "leak", "leaking", "drip", "p-trap", "trap"), "plumbing", "leaking P-trap under sink", "high",
     [("1-1/2 in P-trap kit", 0.6), ("1-1/2 in slip-joint washer", 0.5)]),
    (("outlet", "breaker", "wiring", "electrical", "sparks"), "electrical",
     "electrical issue (not plumbing)", "medium", []),
]


def _keyword_triage(text: str) -> Optional[IntakeResult]:
    t = (text or "").lower()
    for keywords, trade, issue, urgency, parts in KEYWORD_TABLE:
        if any(k in t for k in keywords):
            not_plumbing = trade not in PLUMBING_TRADES
            return IntakeResult(
                trade=trade, issue=issue, urgency=urgency,
                parts=[_IntakePart(name=n, confidence=c) for n, c in parts],
                clear_enough=not not_plumbing,
                follow_up_question=("We're a plumbing company. Is there a water or drain problem we can help with?"
                                    if not_plumbing else None))
    return None


# ---------- customer memory ----------

def _customer_history(phone: str, current_job_id: Optional[str]) -> tuple[list[str], Optional[Location]]:
    """Earlier jobs for this phone number as short lines for the prompt, plus the most recent
    known location. Never raises: memory is a bonus, not a dependency."""
    if not phone:
        return [], None
    try:
        rows = [r for r in db.list_customer_jobs(phone) if r.get("id") != current_job_id and r.get("issue")]
    except Exception as e:
        log_event(current_job_id, "intake", f"couldn't read customer history: {e.__class__.__name__}")
        return [], None
    lines, location = [], None
    for r in rows[-MAX_HISTORY_JOBS:]:
        try:
            when = datetime.fromisoformat(str(r["created_at"])).strftime("%b %d, %Y")
        except (KeyError, ValueError):
            when = "earlier"
        tech = ""
        if r.get("assigned_tech_id"):
            t = db.get_tech(r["assigned_tech_id"])
            tech = f", tech {t.name}" if t else ""
        lines.append(f"{when}: {r['issue']} ({r.get('status', '?')}{tech})")
        if r.get("location"):
            location = Location(**r["location"])
    return lines, location


def _fallback_history_note(history: list[str]) -> Optional[str]:
    if not history:
        return None
    return f"Repeat customer: {len(history)} earlier job(s), most recent {history[-1]}."


def _safety_instruction(result: IntakeResult, text: str, used_llm: bool) -> Optional[str]:
    t = (text or "").lower()
    if result.trade == "gas" or any(w in t for w in GAS_WORDS):
        return GAS_SAFETY  # always the fixed text, never the model's wording
    if used_llm:
        return result.safety_instruction or None
    return FALLBACK_SAFETY.get(result.trade) if result.urgency == "high" else None


# ---------- entry point ----------

def intake(photo_url: Optional[str], text: str, *, customer_phone: str = "",
           job_id: Optional[str] = None) -> Job:
    job_kwargs = {"id": job_id} if job_id else {}
    log_id = job_id

    image = None
    try:
        image = _load_image(photo_url)
    except Exception as e:
        log_event(log_id, "intake", f"couldn't load photo ({e.__class__.__name__}), triaging from text only")

    history, last_location = _customer_history(customer_phone, job_id)
    if history:
        log_event(log_id, "intake", f"repeat customer: {len(history)} earlier job(s)")

    result: Optional[IntakeResult] = None
    path = "vision" if image else "text-only"
    used_model = None
    started = time.monotonic()
    if os.getenv("GEMINI_API_KEY"):
        for model in GEMINI_MODELS:  # on any failure (overloaded, timeout, bad output) try the next model
            try:
                result = _call_llm(model, image, text, history)
                used_model = model
                break
            except (ValidationError, ValueError) as e:
                log_event(log_id, "intake", f"{model} output invalid ({e.__class__.__name__}), trying next")
            except Exception as e:
                log_event(log_id, "intake", f"{model} failed: {e.__class__.__name__}: {str(e)[:100]}")
    latency = time.monotonic() - started

    if result is None:
        path = "FALLBACK keyword"
        result = _keyword_triage(text)
    if result is None:
        path = "FALLBACK follow-up"
        result = IntakeResult(trade="plumbing", issue="unclear plumbing issue", urgency="medium",
                              parts=[], clear_enough=False, follow_up_question=DEFAULT_FOLLOW_UP)

    follow_up = None
    if not result.clear_enough or result.trade not in PLUMBING_TRADES:
        follow_up = result.follow_up_question or DEFAULT_FOLLOW_UP

    job = Job(
        **job_kwargs,
        customer_phone=customer_phone,
        photo_url=photo_url,
        raw_text=text,
        trade=result.trade,
        issue=result.issue,
        urgency=Urgency(result.urgency),
        parts=[Part(name=p.name, confidence=min(1.0, max(0.0, p.confidence))) for p in result.parts],
        follow_up_question=follow_up,
        photo_description=(result.photo_description or None) if image else None,
        history_note=(result.history_note or None) if used_model else _fallback_history_note(history),
        safety_instruction=_safety_instruction(result, text, used_llm=used_model is not None),
        location=last_location,
    )
    model_note = f"{used_model}, {latency:.1f}s" if path in ("vision", "text-only") else "no LLM"
    log_event(job.id, "intake",
              f"[{path}, {model_note}] {job.trade} / {job.urgency.value} urgency: {job.issue}; "
              f"{len(job.parts)} likely parts" + (f"; asking: {follow_up!r}" if follow_up else ""))
    if job.photo_description:
        log_event(job.id, "intake", f"photo: {job.photo_description}")
    if job.history_note:
        log_event(job.id, "intake", f"memory: {job.history_note}")
    if job.safety_instruction:
        log_event(job.id, "intake", f"safety: {job.safety_instruction}")
    return job
