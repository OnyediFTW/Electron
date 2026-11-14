# Person B plan: AI agents

**You own:** `agents/` (`intake.py`, `parts.py`, `quote.py`), plus `test_photos/` and test scripts.
**Branch:** `b/agents` (merge small and often into `main`)
**Your job in one line:** a photo and a sentence become a correct, structured Job with likely parts
and an honest quote. Pure functions, no messaging code.

## Your contract (don't change without telling the team)

```python
intake(photo_url, text, *, customer_phone="", job_id=None) -> Job   # agents/intake.py
find_parts(job) -> list[Part]                                       # agents/parts.py
quote(job, parts) -> Quote                                          # agents/quote.py
```

- `Job`, `Part`, `Quote` are in `shared/schema.py`. Fill `trade`, `issue`, `urgency`
  (`high|medium|low`), `parts[{name, confidence}]`.
- When the photo is unclear or not plumbing, set `job.follow_up_question` (for example "Is water
  actively leaking right now?") instead of guessing. A sends it to the customer.
- Call `log_event(job.id, "intake" | "parts" | "quote", "...")` for each step. It's the dashboard trace.
- Stubs already exist and the loop runs on them. Replace each body and keep the signature.

## Plan

### By 4:30 PM: setup
1. **API keys first, because approval takes time:**
   - eBay: developer.ebay.com → create a production keyset → `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`.
   - SerpAPI: serpapi.com → free plan → `SERPAPI_KEY`.
   - Vision LLM: `ANTHROPIC_API_KEY` (or whichever provider the team picks).
2. **Test photos:** 8 to 10 in `test_photos/` with a `labels.json` of expected answers:
   leaking P-trap, clogged drain, running toilet, water heater, dripping faucet, burst supply line,
   one non-plumbing photo (outlet) and one blurry photo. Use your own photos or freely licensed ones.
3. **`scripts/test_agents.py`:** runs `intake → find_parts → quote` on every photo and prints a table
   of expected vs actual. This is how you prove it works without Photon.

### By 7:00 PM: real `intake()` (the 7 PM checkpoint is "a real photo picks the right tech")
4. Vision LLM call (for example `claude-sonnet-5-5`, fast enough for a live demo):
   - Download `photo_url` yourself (A serves it through a tunnel) and send it as **base64 image
     content**. Don't rely on the model fetching the URL.
   - Force structured output with a single tool or JSON schema: `trade`, `issue`, `urgency`,
     `parts[{name, confidence}]`, `clear_enough: bool`, `follow_up_question`.
   - Validate with Pydantic. On a parse failure, retry once, then fall back to the stub's
     shape with `follow_up_question = "Can you send a closer photo of where the water is coming from?"`.
5. Prompt rules that make the demo look smart:
   - Use real trade names and sizes (kitchen P-trap is usually 1-1/2 in, bathroom 1-1/4 in).
   - Confidence is honest: a size read from a photo is ≤0.7 unless clearly visible.
   - Urgency: active leak or no water = high; slow drain = medium; cosmetic = low.
   - Non-plumbing (the outlet photo) → `trade = "electrical"`, and ask a follow-up instead of guessing.
6. **Target:** 7 of 8 test photos correct on `trade` + `urgency`, and a sensible `issue`.
   Time the call (aim for under 6 s).

### By 1:00 AM: `find_parts()` and `quote()`
7. **`find_parts()`**: for each part, search and keep the cheapest reasonable match as `url` + `price`.
   - eBay Browse API: get an app token (OAuth client-credentials, scope
     `https://api.ebay.com/oauth/api_scope`), then
     `GET https://api.ebay.com/buy/browse/v1/item_summary/search?q=<part>&sort=price&limit=5`.
   - Or SerpAPI `engine=google_shopping`.
   - 3 s timeout per call, cache results by part name, fall back to `FALLBACK_CATALOG`.
     **Links only. Never buy.**
   - If neither key works by 9 PM, grow the hardcoded catalog to cover your test photos.
8. **`quote()`**: labor hours × hourly band + parts total = low-high range.
   - Look up BLS OEWS hourly wages for plumbers (SOC 47-2152) in 2 to 3 demo metros (Trenton-Princeton,
     New York, Philadelphia) on bls.gov/oes. Hardcode the 25th and 75th percentiles in a dict with the
     source year in a comment.
   - Billed rate = wage × overhead multiplier (state the multiplier as an assumption).
   - Labor hours per issue type in a small table. Log the math:
     `"1.0-1.5 h × $95-140/h + $11 parts"`.

### After 1 AM (branch only)
- Stretch: LangChain deepagents wrapper, only if we enter that track.

## What you need from others
- **A:** a public `photo_url` (tunnel or Supabase Storage). Until then, test from local files.
- **C:** nothing blocking. `rank_techs` filters on `job.trade`, so keep trade values to
  `plumbing`, `drain`, `water_heater`, `gas`, `sewer`, `electrical`.

## Paste into your coding agent
> I'm Person B on Electron. Read CLAUDE.md and docs/plans/person-b.md. Work only in `agents/`,
> `test_photos/` and `scripts/test_agents.py`. Start by replacing the stub in `agents/intake.py`
> with a vision LLM call that returns a validated Job, keeping the signature. Keep
> `python -m scripts.smoke_test` passing.
