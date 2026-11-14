# Electron

An AI dispatcher for small trade businesses (plumbing first), run entirely over
iMessage. A customer texts a photo of the problem, and Electron triages it, picks
the right tech, offers them the job, and books it, with a likely parts list and a
quote. Built at HackPrinceton, Nov 14-15 2026.

## The problem

Small trade shops (one owner, a handful of techs) run dispatch by phone and text.
Every new job costs the owner time:

- **Customers can't describe the problem.** Most don't know a P-trap from a supply
  line, so the first call is a back-and-forth about what's broken, how urgent it is,
  and which tech should go.
- **Matching is manual.** The owner checks who has the right skills, who is free,
  and who is closest, usually from memory or a paper calendar.
- **Phone tag.** Offers to techs and confirmations to customers happen one call or
  text at a time. If a tech doesn't answer, the owner starts over.
- **Wasted trips.** Techs arrive without the right part and drive to the supply
  house, which costs a second visit or an hour of the job.

General marketplaces (Facebook Marketplace, Craigslist, Nextdoor) make it worse: no
structured photos of the actual problem, slow or no responses, and no scheduling.

**Validation:** we called the owner of a local plumbing company. He said this would
save him a lot of time and help his customers. _(TODO: name, role, and his exact
words about which step costs him the most time.)_

## Who uses it

- **Customer:** texts a photo and a sentence. No app, no account, no forms.
- **Tech:** gets a text with the job summary, photo, likely parts, and a quote.
  Replies YES or NO. That's all.
- **Owner:** watches the dashboard. Steps in only when something fails.

## What Electron does

1. Customer texts a photo and description to the Photon number.
2. The intake agent (vision LLM) turns it into a structured Job: trade, issue,
   urgency, likely parts with confidence.
3. Matching ranks techs with a deterministic, explainable score: skill match,
   calendar free/busy, distance, open jobs, rating.
4. The top tech gets an iMessage offer. NO, or no reply within the timeout, moves
   to the next tech.
5. The customer gets a confirmed time and a price range.
6. A Google Calendar event is created and the job is marked booked.

After the core loop: parts agent (cheapest links, never auto-buy), quote agent
(labor checked against DOL wage data for plumbers, SOC 47-2152), and a live
dashboard of every agent step.

**Design principles**

- **The AI suggests, a human confirms.** Parts are labeled "likely" with a
  confidence score. Never place real orders or move real money.
- **Matching is an algorithm, not an LLM call.** The LLM extracts features, a
  scoring function ranks, and the reasons are shown to the tech.
- **Every step is visible.** Every function calls `log_event(job_id, agent, message)`.
- **The demo must not break.** A hardcoded fallback beats a fragile integration.

**Out of scope:** real part ordering, real payments, Facebook Marketplace, user
accounts or a signup UI. Techs are seeded in the database.

## Setup

```bash
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt
cp .env.example .env          # fill in what you have; blanks fall back to stubs
python -m scripts.smoke_test  # full loop, no network. Must pass before every merge
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000 for the dashboard. Simulate a customer text:

```bash
curl -X POST localhost:8000/dev/inbound -H 'content-type: application/json' \
  -d '{"phone":"+16095550100","text":"kitchen sink leaking underneath","photo_url":"https://example.com/p.jpg"}'
```

Reply as a tech (Marco is `+15550000001` unless `TECH_PHONE_1` is set):

```bash
curl -X POST localhost:8000/dev/inbound -H 'content-type: application/json' \
  -d '{"phone":"+15550000001","text":"yes"}'
```

Without Supabase keys, everything runs on an in-memory store that is reseeded on
every start. To use Supabase:

1. Create a project, open the SQL editor, and run `supabase/schema.sql`.
2. Put `SUPABASE_URL` and the service-role key (`SUPABASE_SERVICE_KEY`) in `.env`.
3. Run `python -m shared.seed` to add the demo techs (`--reset` wipes all tables).

## Architecture

```
iMessage ──Photon──> /webhook/photon ──> messaging.dispatch.handle_inbound
                                              │
               sender is a tech? ──yes──> handle_tech_reply ── YES ─> free_slots, book, advance(booked)
                     │ no                                      └ NO/timeout ─> offer_next
                     ▼
              handle_customer ─> intake ─> find_parts ─> quote ─> advance(triaged)
                                                                     │
                                         rank_techs ─> offer_next ─> advance(offered) ─> text tech
```

- Job status only changes in `messaging/state.py:advance()`, which rejects illegal
  moves (`new → triaged → offered → accepted → booked`, any → `failed`).
- Every step writes an `events` row; the dashboard polls `/api/...` every 2 seconds.
- Every stub is marked `STUB` or `FALLBACK` in its docstring. Replace the body,
  keep the signature.

## The contract (`shared/`, don't change without telling the team)

**Job** (`shared/schema.py`):

```json
{
  "id": "uuid",
  "customer_phone": "+1...",
  "photo_url": "https://...",
  "raw_text": "kitchen sink leaking underneath",
  "trade": "plumbing",
  "issue": "leaking P-trap under kitchen sink",
  "urgency": "high | medium | low",
  "parts": [{"name": "1-1/2 in P-trap kit", "confidence": 0.7}],
  "location": {"lat": 40.35, "lng": -74.66},
  "status": "new | triaged | offered | accepted | booked | failed",
  "assigned_tech_id": null,
  "quote": {"low": 150, "high": 250}
}
```

**Tables** (`supabase/schema.sql`):

| Table | Key columns | Written by |
|---|---|---|
| `techs` | id, name, phone, skills[], lat, lng, rating, calendar_id | C (seed) |
| `jobs` | the Job fields above | A (status), B (triage fields) |
| `offers` | id, job_id, tech_id, sent_at, response (yes, no, timeout) | A |
| `events` | id, job_id, agent, message, created_at | everyone, one row per step |

**Function signatures** (the only seams between people):

| Owner | Function |
|---|---|
| B | `intake(photo_url, text) -> Job` |
| B | `find_parts(job) -> list[Part]` |
| B | `quote(job, parts) -> Quote` |
| C | `rank_techs(job) -> list[{tech_id, score, reasons}]` |
| C | `free_slots(tech_id) -> list[Slot]` |
| C | `book(tech_id, job_id, slot) -> event_id` |
| everyone | `log_event(job_id, agent, message)` |

## Who does what

### Person A: messaging and dispatch (the orchestrator)

Owns `messaging/`: the flow from text in to booking out. Calls B's and C's
functions and only needs them to return the agreed shapes.

- [x] Repo, FastAPI app, `shared/db.py`, the four tables (in-memory now; Supabase project still to create)
- [ ] Photon webhook receiving texts and photos, storing the photo URL (`/webhook/photon` exists but guesses the payload shape)
- [ ] Photon outbound send in `messaging/send.py` (currently logs instead of sending)
- [x] Router: a sender phone in `techs` goes to the tech flow, everyone else to the customer flow
- [x] Customer flow: create job, `intake()`, `rank_techs()`, offer to the top tech
- [x] Tech flow: forgiving YES/NO parsing ("y", "yes!", "sure", "nope")
- [x] Timeout: no reply in `OFFER_TIMEOUT_SECONDS` (60 for the demo) offers to the next tech
- [x] On YES: `free_slots()`, text the customer the time and quote, `book()`, status → booked
- [ ] Handle the customer's answer to an intake follow-up question
- [ ] Stretch: Photon voice call (Best Voice Agent prize)

### Person B: AI agents (the brains)

Owns `agents/`: pure functions with no messaging code, tested from a script
against a folder of saved photos.

- [ ] Collect 8 to 10 test photos: leaking P-trap, clogged drain, running toilet,
      water heater, plus one non-plumbing photo (outlet) and one blurry photo
- [x] Stub `intake()` returning hardcoded JSON so A can wire the loop
- [ ] Real `intake()`: vision LLM with a strict JSON prompt, validated against `Job`
- [ ] Unclear photo: set `follow_up_question` ("Is water actively leaking?") instead of guessing
- [ ] `find_parts()`: eBay Browse API and/or SerpAPI, cheapest match per part as links. Never auto-buy. Keep `FALLBACK_CATALOG`
- [ ] `quote()`: labor hours × hourly wage band for plumbers (SOC 47-2152) in 2 to 3 demo metros, plus parts total, as a low-high range
- [ ] Apply for eBay and SerpAPI keys in the first hour. If neither works by 9 PM, use the hardcoded catalog
- [ ] Stretch: wrap the agents with LangChain deepagents

Tip: put a confidence score on every part and show it in the tech's message. A
wrong part size labeled as a guess looks smart; the same mistake stated as fact
looks broken.

### Person C: matching, calendar, dashboard, demo (logistics and the face)

Owns `matching/` and `dashboard/`: how techs are picked, how jobs are scheduled,
and everything judges see besides the phones.

- [x] Seed 7 techs around Princeton (`shared/seed.py`). Put teammates' real phones in `TECH_PHONE_1` / `TECH_PHONE_2` so the demo buzzes
- [ ] Create a Google Calendar per demo tech with some busy blocks
- [x] `rank_techs()` with weights in one dict and reasons ("plumbing skill, 0.7 mi away, 4.7 stars"). Skill, distance, rating done
- [ ] Add calendar free/busy (next 48 hours) and current open jobs to the score
- [ ] `free_slots()` and `book()` on Google Calendar (stubs return fake slots)
- [x] Dashboard: jobs list plus a live event feed per job, polling every 2 seconds
- [ ] Peri evidence: call the plumbing owner back for his name, role, and a quote or screenshot
- [ ] Devpost page, 2-minute video, demo script

Tip: judges reward a visible algorithm over a hidden LLM choice, so the reasons
go straight into the tech's offer.

## How we build

Stubs first, then swap real pieces in one at a time. The loop runs end to end with
fake data now and should never break after that.

| Time (Sat → Sun) | Checkpoint | Everyone confirms |
|---|---|---|
| 3:00 PM | Contract, tables, repo agreed | Schema merged in `shared/` |
| 4:00 PM | Stub loop works | Real text in, fake job, real offer text out, YES books a fake slot |
| 7:00 PM | Real intake and real matching swapped in | A real photo picks the right seeded tech |
| 9:00 PM | Core loop live on phones | Full demo flow works 3 times in a row. Tag it `core-demo` |
| 1:00 AM | Parts, quote, dashboard done | Tech offer shows parts and quote, dashboard shows the trace |
| 2:00 AM | Feature freeze | Only bug fixes after this |
| 5:00 AM | Video recorded, Devpost drafted | Everyone reviews |
| 7:30 AM | Submitted | Hard deadline is 8:00 AM |

**Rules**

- Merge small and often into `main`. Pull before you start each task.
- Stay in your own folder. Anything touching `shared/` gets a message in the group chat first.
- Run `python -m scripts.smoke_test` before every merge. A tests the full loop after every merge.
- After 9 PM, extras go on branches and only merge if the `core-demo` flow still passes.
- Secrets go in `.env`, never in code. Keep `.env.example` up to date.
- Stuck for 30 minutes? Say so and swap in the stub or a hardcoded fallback.

## Tracks

- Main: Business and Enterprise
- Peri Forward Deployed (business name, contact's name and role, one piece of evidence)
- Photon messaging agent (plus Best Voice Agent if voice ships)
- Amazon Best Practical AI
- LangChain deepagents (only if B builds on it), Capital One Nessie (only if a mock payment ships)

## Demo script (2 minutes)

1. **(15 s) Problem:** small trade shops lose hours to phone tag, and customers can't describe what's broken.
2. **(15 s)** The owner we talked to and what he said.
3. **(60 s) Live:** text a photo of a leaking P-trap. The tech's phone buzzes with the
   summary, likely parts, and quote. Tech replies YES. Customer gets a confirmed
   time and price. Calendar event appears. Dashboard shows each agent step.
4. **(15 s) Fallback:** a second job where the first tech says NO and it goes to the next one.
5. **(15 s) What's next:** techs' own agents, opening the network to freelancers.
