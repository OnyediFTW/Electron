"""Job prep: research, cost and a trusted parts plan for a job. Added by Person A for the team;
lives in agents/ next to intake/parts/quote. Owner: Person B (review welcome).

prepare(job) -> dict | None, stored as Job.prep:
  causes      likely root causes, ranked, each with how to confirm it on site in a couple of minutes
  tools       what to bring
  watch_out   safety / gotchas
  parts_plan  per part: a trusted brand/model, why it's trusted, est. price, where to buy given the
              urgency (a store on the way vs. order online), and what to avoid
  economics   owner-only: the shop's cost (tech wages + parts) vs. the customer quote = margin
  source      "gemini knowledge" (no live web search: grounding is not on the free tier)

Gemini answers from its own knowledge; prices are estimates and say so. Economics are computed
here, not by the model. Without GEMINI_API_KEY this returns None (no prep), never a guess.
"""
from __future__ import annotations

import os
import time
from typing import Literal, Optional

from pydantic import BaseModel, Field

from shared.db import log_event
from shared.schema import Job

PROMPT = """You are a master plumber preparing a junior plumber for a service call. Be practical and
specific. Use trade names and real, widely sold brands. Prices are typical US retail estimates.

JOB
Problem: {issue}
Trade: {trade}
Urgency: {urgency}
Customer said: "{text}"
{photo}
Likely parts so far: {parts}

Return:
- causes: the 1-3 most likely root causes, most likely first, each with a likelihood (0-1) and
  `confirm`: how to confirm it on site in under 2 minutes.
- tools: up to 6 tools to bring.
- watch_out: up to 3 safety notes or gotchas specific to this job.
- parts_plan: for each part worth bringing (max 4): `part`, `pick` (a specific trusted brand/model),
  `why` (why it's the trusted value choice, under 15 words), `est_low`/`est_high` (USD),
  `buy` ("pick up on the way" if urgency is high or it's commonly stocked at big-box stores, else "order ahead"),
  and `avoid` (a cheaper option to skip and why, or null).
- summary: one sentence a plumber would text a colleague about this job."""


class Cause(BaseModel):
    cause: str
    likelihood: float = Field(ge=0, le=1)
    confirm: str


class PartPick(BaseModel):
    part: str
    pick: str
    why: str
    est_low: float
    est_high: float
    buy: Literal["pick up on the way", "order ahead"]
    avoid: Optional[str] = None


class Prep(BaseModel):
    summary: str
    causes: list[Cause]
    tools: list[str]
    watch_out: list[str]
    parts_plan: list[PartPick]


def _economics(job: Job, plan: list[PartPick]) -> Optional[dict]:
    """Owner-only: what the job costs the shop vs. what the customer is quoted."""
    if not job.quote:
        return None
    from agents.quote import PLUMBER_WAGES, _hours, _metro  # B's BLS wage table and labor hours
    metro = _metro(job)
    _, _, p25, p75 = PLUMBER_WAGES[metro]
    h_low, h_high = _hours(job)
    labor_low, labor_high = h_low * p25, h_high * p75          # the tech's wages, not the billed rate
    parts_low = sum(p.est_low for p in plan)
    parts_high = sum(p.est_high for p in plan)
    return {
        "quote_low": round(job.quote.low), "quote_high": round(job.quote.high),
        "labor_cost_low": round(labor_low), "labor_cost_high": round(labor_high),
        "parts_cost_low": round(parts_low), "parts_cost_high": round(parts_high),
        "margin_low": round(job.quote.low - labor_high - parts_high),
        "margin_high": round(job.quote.high - labor_low - parts_low),
        "basis": f"{h_low:g}-{h_high:g} h at {metro} plumber wages ${p25:.0f}-{p75:.0f}/h (BLS), "
                 "before truck, insurance and overhead",
    }


def prepare(job: Job) -> Optional[dict]:
    if not os.getenv("GEMINI_API_KEY"):
        log_event(job.id, "prep", "skipped: no Gemini key, so no research brief (nothing guessed)")
        return None

    from google import genai
    from google.genai import types

    from agents.intake import GEMINI_MODELS

    parts = "; ".join(f"{p.name} ({p.confidence:.0%})" for p in job.parts) or "none identified"
    prompt = PROMPT.format(issue=job.issue or "not identified", trade=job.trade or "plumbing",
                           urgency=job.urgency.value if job.urgency else "unknown", text=job.raw_text,
                           photo=f"Photo shows: {job.photo_description}" if job.photo_description else "",
                           parts=parts)
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"), http_options=types.HttpOptions(timeout=20_000))
    started = time.monotonic()
    for model in GEMINI_MODELS:
        try:
            resp = client.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json",
                                                   response_schema=Prep, temperature=0.2))
            prep = resp.parsed if isinstance(resp.parsed, Prep) else Prep.model_validate_json(resp.text)
            out = prep.model_dump()
            out["causes"] = sorted(out["causes"], key=lambda c: -c["likelihood"])[:3]
            out["economics"] = _economics(job, prep.parts_plan)
            out["source"] = f"{model} knowledge (no live web search); prices are estimates"
            log_event(job.id, "prep", f"[{model}, {time.monotonic() - started:.1f}s] {prep.summary} "
                                      f"| {len(out['causes'])} causes, {len(prep.parts_plan)} parts planned")
            return out
        except Exception as e:
            log_event(job.id, "prep", f"{model} failed ({e.__class__.__name__}), trying next")
    log_event(job.id, "prep", "no prep: every model failed")
    return None


def tech_message(job: Job, prep: dict) -> str:
    """The prep as one iMessage (light Markdown). Owner economics stay out of it."""
    lines = [f"**Job prep · {job.issue or 'your job'}**", prep["summary"], "", "**Check first:**"]
    for i, c in enumerate(prep["causes"], 1):
        lines.append(f"{i}. {c['cause']} ({c['likelihood']:.0%}): {c['confirm']}")
    if prep["parts_plan"]:
        lines += ["", "**Parts plan:**"]
        for p in prep["parts_plan"]:
            where = p["buy"]
            lines.append(f"• {p['pick']} for the {p['part']}: ~${p['est_low']:.0f}-{p['est_high']:.0f}, {where}. {p['why']}")
            if p.get("avoid"):
                lines.append(f"  Skip: {p['avoid']}")
    if prep["tools"]:
        lines += ["", f"**Bring:** {', '.join(prep['tools'])}"]
    if prep["watch_out"]:
        lines += [f"**Watch out:** {' '.join(prep['watch_out'])}"]
    lines += ["", "Prices are estimates. Ask me anything about the job."]
    return "\n".join(lines)
