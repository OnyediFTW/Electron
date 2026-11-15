"""Job prep: research, cost and a live-priced parts plan for a job. Added by Person A for the team;
lives in agents/ next to intake/parts/quote. Owner: Person B (review welcome).

prepare(job) -> dict | None, stored as Job.prep:
  causes      likely root causes, ranked, each with how to confirm it on site in a couple of minutes
  tools       what to bring
  watch_out   safety / gotchas
  parts_plan  every individual part to bring (specific brand + model, not generic kits), each with:
                why it's the trusted pick, est. price range, pick up on the way vs. order ahead,
                what to avoid, and `live`: a real Google Shopping listing for THAT product
                (price, store, rating, buy link) plus the cheapest trusted alternative
  economics   owner-only: the shop's cost (tech wages + parts, live prices when found) vs. the quote
  source      where the research and prices came from

Research is Gemini's own trade knowledge (Google Search grounding is not on the free tier).
Prices: live from SerpAPI Google Shopping when SERPAPI_KEY is set (cached, ~1 search per part),
otherwise Gemini's estimates, labeled as such. Without GEMINI_API_KEY there is no prep at all.
"""
from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Literal, Optional
from urllib.parse import quote, quote_plus

import httpx
from pydantic import BaseModel, Field

from shared.db import log_event
from shared.schema import Job

PROMPT = """You are a master plumber preparing a junior plumber for a service call. Be practical and
specific. Use real, widely sold brands and model numbers. Prices are typical US retail estimates.

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
- parts_plan: EVERY individual part the plumber should bring to finish this job in one visit
  (max 5). List components separately, not a generic "repair kit" (e.g. flapper AND flush lever,
  not "toilet repair kit"). Include small consumables (wax ring, supply line, thread seal tape,
  slip-joint washers) only when this job actually needs them. For each:
    `part`: plain name, e.g. "toilet flapper"
    `pick`: a specific trusted product, brand + model, e.g. "Fluidmaster 502P21 flapper"
    `search`: what to type into Google Shopping to find exactly that product, e.g.
              "Fluidmaster 502P21 toilet flapper"
    `why`: why it's the trusted value choice, under 15 words
    `est_low`/`est_high`: typical US retail price range in USD
    `buy`: "pick up on the way" if the job is urgent or the part is stocked at big-box stores,
           else "order ahead"
    `avoid`: a cheaper option to skip and why, or null
- summary: one sentence a plumber would text a colleague about this job."""


class Cause(BaseModel):
    cause: str
    likelihood: float = Field(ge=0, le=1)
    confirm: str


class PartPick(BaseModel):
    part: str
    pick: str
    search: str
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


# ---------- live prices (SerpAPI Google Shopping) ----------

# Retailers with physical stores a tech can stop at on the way.
WALK_IN = ("home depot", "lowe's", "lowes", "ace hardware", "walmart", "menards", "true value")
# Retailers trusted for genuine parts and returns, in-store or online.
TRUSTED = WALK_IN + ("amazon", "ferguson", "supplyhouse", "target", "build.com", "grainger", "zoro")
_cache: dict[str, list[dict]] = {}


def _shopping(query: str) -> list[dict]:
    """Google Shopping results for a query; cached so a product is searched once per server run."""
    key = os.getenv("SERPAPI_KEY")
    if not key:
        return []
    if query in _cache:
        return _cache[query]
    r = httpx.get("https://serpapi.com/search.json", timeout=8,
                  params={"engine": "google_shopping", "q": query, "api_key": key, "gl": "us", "hl": "en"})
    r.raise_for_status()
    _cache[query] = r.json().get("shopping_results", [])
    return _cache[query]


def _model_tokens(pick: str) -> tuple[str, list[str]]:
    words = re.findall(r"[A-Za-z0-9-]+", pick)
    brand = words[0].lower() if words else ""
    models = [w.lower() for w in words if re.search(r"\d", w) and len(w) >= 3]
    return brand, models


_GENERIC = {"universal", "polypropylene", "plastic", "chrome", "white", "brass", "inch", "with", "for",
            "kit", "replacement", "standard", "heavy", "duty", "adjustable", "toilet", "sink", "kitchen"}
MARKETPLACES = ("ebay", "aliexpress", "temu", "wish.com", "mercari", "poshmark", "facebook")
_MULTIPACK = re.compile(r"\b(lot of|pack of|set of \d|\d+\s*-?\s*pack|\d+\s*pcs|\d+\s*pieces)\b|^\d+\s", re.I)


def _keywords(part: str) -> list[str]:
    """The part-type words of a part name, stemmed: '1-1/2 in Slip-Joint Washers' -> ['slip', 'joint', 'washe']."""
    words = [w for w in re.findall(r"[a-z]+", part.lower()) if len(w) > 3 and w not in _GENERIC]
    return [w[:5] for w in words] or [w[:5] for w in re.findall(r"[a-z]+", part.lower()) if len(w) > 3]


def _match_level(item: dict, pick: str, part: str) -> int | None:
    """0 = exact product (brand + model), 1 = same brand and part type, 2 = same part type, None = other."""
    title = (item.get("title") or "").lower()
    brand, models = _model_tokens(pick)
    has_type = any(k in title for k in _keywords(part))
    if brand and brand in title:
        if models and any(m in title or m.split("-")[0] in title for m in models):
            return 0
        if has_type:
            return 1
    return 2 if has_type else None


def _tier(item: dict) -> int:
    """0 = trusted retailer, 1 = well-reviewed store, 2 = other store. Marketplaces never qualify."""
    store = (item.get("source") or "").lower()
    if any(s in store for s in TRUSTED):
        return 0
    if (item.get("rating") or 0) >= 4.0 and (item.get("reviews") or 0) >= 50:
        return 1
    return 2


def _usable(item: dict, pick: "PartPick") -> bool:
    price = item.get("extracted_price")
    store = (item.get("source") or "").lower()
    return (isinstance(price, (int, float)) and not any(m in store for m in MARKETPLACES)
            and not _MULTIPACK.search(item.get("title") or "")
            and pick.est_low / 4 <= price <= max(pick.est_high * 3, pick.est_high + 15))


def _short_link(item: dict) -> Optional[str]:
    """Google Shopping's product page (all stores and prices) in a link short enough for a text.
    SerpAPI's product_link carries ~400 chars of tracking ids; title + catalog id opens the same page."""
    if item.get("product_id") and item.get("title"):
        return ("https://www.google.com/search?ibp=oshop&q=" + quote_plus(item["title"])
                + "&prds=catalogid:" + str(item["product_id"]))
    link = item.get("product_link") or item.get("link") or ""
    return quote(link, safe=":/?&=%#+,;@!$'()*") if link else None


def _listing(item: dict) -> dict:
    return {"title": item.get("title"), "price": item.get("extracted_price"), "store": item.get("source"),
            "rating": item.get("rating"), "reviews": item.get("reviews"), "link": _short_link(item),
            "image": item.get("serpapi_thumbnail") or item.get("thumbnail")}


MATCH_LABEL = {0: "exact", 1: "same brand", 2: "similar item"}


def _choose(items: list[dict], pick: PartPick) -> Optional[dict]:
    """Best match level first; within it, trusted stores first, then cheapest. Walk-in stores win
    when the part should be picked up on the way. Adds the cheapest same-level option as `alt`."""
    if not items:
        return None
    level = min(lvl for _, lvl in items)
    pool = [i for i, lvl in items if lvl == level]
    ranked = sorted(pool, key=lambda i: (_tier(i), i["extracted_price"]))
    walk_in = [i for i in ranked if any(s in (i.get("source") or "").lower() for s in WALK_IN)]
    best = walk_in[0] if pick.buy == "pick up on the way" and walk_in else ranked[0]
    out = {**_listing(best), "match": MATCH_LABEL[level], "trusted": _tier(best) == 0}
    cheapest = min(pool, key=lambda i: i["extracted_price"])
    if cheapest is not best and cheapest["extracted_price"] < best["extracted_price"] - 0.5:
        out["alt"] = _listing(cheapest)
    return out


def _live(pick: PartPick) -> Optional[dict]:
    """A real listing for this product: the exact model if sold, else the same brand's part, else a
    similar part from a generic search (labeled). Never marketplaces, multi-packs or odd prices."""
    def scored(query: str) -> list[tuple[dict, int]]:
        out = []
        for i in _shopping(query):
            lvl = _match_level(i, pick.pick, pick.part)
            if lvl is not None and _usable(i, pick):
                out.append((i, lvl))
        return out
    try:
        found = scored(pick.search)
        if not any(lvl < 2 for _, lvl in found):  # no exact or same-brand hit: try the generic part
            found += scored(pick.part)
        return _choose(found, pick)
    except Exception:
        return None


def _cost(p: dict) -> tuple[float, float]:
    live = p.get("live")
    return (live["price"], live["price"]) if live else (p["est_low"], p["est_high"])


def _economics(job: Job, plan: list[dict]) -> Optional[dict]:
    """Owner-only: what the job costs the shop vs. what the customer is quoted."""
    if not job.quote:
        return None
    from agents.quote import PLUMBER_WAGES, _hours, _metro  # B's BLS wage table and labor hours
    metro = _metro(job)
    _, _, p25, p75 = PLUMBER_WAGES[metro]
    h_low, h_high = _hours(job)
    labor_low, labor_high = h_low * p25, h_high * p75          # the tech's wages, not the billed rate
    parts_low = sum(_cost(p)[0] for p in plan)
    parts_high = sum(_cost(p)[1] for p in plan)
    live = sum(1 for p in plan if p.get("live"))
    return {
        "quote_low": round(job.quote.low), "quote_high": round(job.quote.high),
        "labor_cost_low": round(labor_low), "labor_cost_high": round(labor_high),
        "parts_cost_low": round(parts_low), "parts_cost_high": round(parts_high),
        "margin_low": round(job.quote.low - labor_high - parts_high),
        "margin_high": round(job.quote.high - labor_low - parts_low),
        "basis": f"{h_low:g}-{h_high:g} h at {metro} plumber wages ${p25:.0f}-{p75:.0f}/h (BLS); parts: "
                 f"{live}/{len(plan)} live prices; before truck, insurance and overhead",
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
            break
        except Exception as e:
            log_event(job.id, "prep", f"{model} failed ({e.__class__.__name__}), trying next")
    else:
        log_event(job.id, "prep", "no prep: every model failed")
        return None

    picks = prep.parts_plan[:5]
    with ThreadPoolExecutor(max_workers=max(1, len(picks))) as pool:
        lives = list(pool.map(_live, picks))
    plan = [{**p.model_dump(), "live": live} for p, live in zip(picks, lives, strict=True)]
    for p in plan:
        live = p["live"]
        log_event(job.id, "prep", f"{p['pick']}: " + (f"${live['price']:.2f} at {live['store']}" if live
                                                      else f"no trusted listing found, est. ${p['est_low']:.0f}-{p['est_high']:.0f}"))
    out = prep.model_dump()
    out["causes"] = sorted(out["causes"], key=lambda c: -c["likelihood"])[:3]
    out["parts_plan"] = plan
    out["economics"] = _economics(job, plan)
    found = sum(1 for p in plan if p["live"])
    out["source"] = (f"{model} knowledge (no live web search); prices: {found}/{len(plan)} live from Google "
                     f"Shopping" + (", the rest estimates" if found < len(plan) else ""))
    log_event(job.id, "prep", f"[{model}, {time.monotonic() - started:.1f}s] {prep.summary} | "
                              f"{len(out['causes'])} causes, {len(plan)} parts, {found} live prices")
    return out


def _price_line(p: dict) -> str:
    live = p.get("live")
    if not live:
        return f"• **{p['pick']}** ({p['part']}): ~${p['est_low']:.0f}-{p['est_high']:.0f} est., {p['buy']}"
    stars = f", {live['rating']}★" if live.get("rating") else ""
    name = p["pick"] if live.get("match") == "exact" else f"{live['title']} (similar to {p['pick']})" \
        if live.get("match") == "similar item" else live["title"]
    line = (f"• **{name}** ({p['part']}): **${live['price']:.2f} at {live['store']}**{stars}, {p['buy']}"
            + (f". [Buy]({live['link']})" if live.get("link") else ""))
    if live.get("alt"):
        a = live["alt"]
        line += f"\n  Cheaper online: ${a['price']:.2f} at {a['store']}" + (f" [link]({a['link']})" if a.get("link") else "")
    return line


def part_photos(prep: dict, limit: int = 3) -> list[tuple[str, str]]:
    """(image url, caption) for each part with a live listing, so the tech sees what to grab."""
    out = []
    for p in prep.get("parts_plan") or []:
        live = p.get("live") or {}
        if live.get("image"):
            note = "" if live.get("match") in (None, "exact") else " (closest match, check the size)"
            out.append((live["image"], f"{live['title']}: ${live['price']:.2f} at {live['store']}{note}"))
    return out[:limit]


def tech_message(job: Job, prep: dict) -> str:
    """The prep as one iMessage (light Markdown). Owner economics stay out of it."""
    lines = [f"**Job prep · {job.issue or 'your job'}**", prep["summary"], "", "**Check first:**"]
    for i, c in enumerate(prep["causes"], 1):
        lines.append(f"{i}. {c['cause']} ({c['likelihood']:.0%}): {c['confirm']}")
    if prep["parts_plan"]:
        lines += ["", "**Parts to bring:**"]
        for p in prep["parts_plan"]:
            lines.append(_price_line(p))
            if p.get("avoid"):
                lines.append(f"  Skip: {p['avoid']}")
    if prep["tools"]:
        lines += ["", f"**Bring:** {', '.join(prep['tools'])}"]
    if prep["watch_out"]:
        lines += [f"**Watch out:** {' '.join(prep['watch_out'])}"]
    lines += ["", "Ask me anything about the job."]
    return "\n".join(lines)
