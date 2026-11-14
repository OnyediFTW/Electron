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
from pathlib import Path
from typing import Literal, Optional

import httpx
from pydantic import BaseModel, ValidationError

from shared.db import log_event
from shared.schema import Job, Part, Urgency

# Tried in order: if one is overloaded (503) or fails, the next one answers.
GEMINI_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_MODELS", "gemini-3.5-flash-lite,gemini-3.6-flash").split(",") if m.strip()]
MAX_EDGE_PX = 1568
DEFAULT_FOLLOW_UP = "Can you send a closer photo of where the water is coming from?"
PLUMBING_TRADES = {"plumbing", "drain", "water_heater", "gas", "sewer"}

SYSTEM_PROMPT = """You triage service requests for a small plumbing company. A customer \
texted a photo (sometimes missing) and a short description. Most customers don't know \
part names, so work out what is actually broken.

Return:
- trade: plumbing | drain | water_heater | gas | sewer | electrical.
  drain = clogs and slow drains. water_heater = tank or tankless heaters. sewer = main \
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
customer (e.g. "Is water actively leaking right now?"). Otherwise null."""


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

def _call_llm(model: str, image: Optional[tuple[str, bytes]], text: str) -> IntakeResult:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"),
                          http_options=types.HttpOptions(timeout=10_000))  # ms (Gemini rejects deadlines under 10 s)
    contents: list = []
    if image:
        mime_type, data = image
        contents.append(types.Part.from_bytes(data=data, mime_type=mime_type))
    contents.append(f"Customer text: {text or '(no text)'}" + ("" if image else "\n(No photo was attached.)"))

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

    result: Optional[IntakeResult] = None
    path = "vision" if image else "text-only"
    used_model = None
    started = time.monotonic()
    if os.getenv("GEMINI_API_KEY"):
        for model in GEMINI_MODELS:  # on any failure (overloaded, timeout, bad output) try the next model
            try:
                result = _call_llm(model, image, text)
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
    )
    model_note = f"{used_model}, {latency:.1f}s" if path in ("vision", "text-only") else "no LLM"
    log_event(job.id, "intake",
              f"[{path}, {model_note}] {job.trade} / {job.urgency.value} urgency: {job.issue}; "
              f"{len(job.parts)} likely parts" + (f"; asking: {follow_up!r}" if follow_up else ""))
    return job
