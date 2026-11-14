# Electron

AI dispatcher for small trade businesses (plumbing first), run entirely over
iMessage via Photon. Built at HackPrinceton, Nov 14-15 2026. Hard deadline
8:00 AM Sunday; we submit at 7:30.

## The problem

Small trade shops (1 owner, a handful of techs) run dispatch by phone and
text. Every new job costs the owner time:

- Customers can't describe the problem. Most don't know a P-trap from a
  supply line, so the first call is a back-and-forth to figure out what's
  broken, how urgent it is, and which tech should go.
- Matching is manual. The owner checks who has the right skills, who is free,
  and who is closest, usually from memory or a paper calendar.
- Phone tag. Offers to techs and confirmations to customers happen one call
  or text at a time. If a tech doesn't answer, the owner starts over.
- Wasted trips. Techs arrive without the right part and have to drive to the
  supply house, which costs a second visit or an hour of the job.

General marketplaces (Facebook Marketplace, Craigslist, Nextdoor) make it
worse: no photos of the actual problem in a structured way, slow or no
responses, and no scheduling.

Validation: we called the owner of a local plumbing company. He said this
would save him a lot of time and help his customers. [ADD: his name, role,
and exact words about which step costs him the most time.]

## Who uses it

- Customer: texts a photo + a sentence. Wants zero friction: no app, no
  account, no forms.
- Tech: gets a text with the job summary, photo, likely parts, and a quote.
  Replies YES or NO. That's all.
- Owner: watches the dashboard. Steps in only when something fails.

## What Electron does

1. Customer texts a photo + description to the Photon number.
2. Intake agent (vision LLM) turns it into a structured Job: trade, issue,
   urgency, likely parts with confidence.
3. Matching ranks techs with a deterministic, explainable score: skill match,
   calendar free/busy, distance, open jobs, rating.
4. Top tech gets an iMessage offer. NO or no reply within the timeout moves
   to the next tech.
5. Customer gets a confirmed time and a price range.
6. A Google Calendar event is created and the job is marked booked.

After the core loop: parts agent (cheapest links, never auto-buy), quote
agent (labor checked against DOL wage data for plumbers, SOC 47-2152), live
dashboard of each agent step.

## Design principles

- The AI suggests, a human confirms. Parts lists are labeled "likely" with a
  confidence score. The tech confirms before anything is ordered. Never place
  real orders or move real money.
- Matching is an algorithm, not an LLM call. The LLM extracts features, the
  scoring function ranks, and the reasons are shown to the tech.
- Every step is visible. Each function calls `log_event(job_id, agent,
  message)` so the dashboard shows the trace.
- The demo must not break. Prefer a hardcoded fallback over a fragile
  integration.

## Tech stack

Python + FastAPI backend, Supabase (Postgres), Photon iMessage SDK, Google
Calendar API, a vision-capable LLM API, eBay Browse API and or SerpAPI for
parts. Dashboard is a simple web page that polls Supabase.

## Repo layout and ownership

- messaging/ (Person A): Photon webhook, sender routing, dispatch loop, job
  state machine
- agents/ (Person B): intake(), find_parts(), quote()
- matching/ (Person C): rank_techs(), free_slots(), book()
- dashboard/ (Person C)
- shared/: schema.py (Job model), db.py (Supabase client). Do not change
  without telling the team.

## Contract (do not change without telling the team)

Job fields: id, customer_phone, photo_url, raw_text, trade, issue, urgency
(high|medium|low), parts [{name, confidence}], location {lat, lng}, status
(new|triaged|offered|accepted|booked|failed), assigned_tech_id, quote {low,
high}.

Tables: techs, jobs, offers, events.

Signatures:
- intake(photo_url, text) -> Job
- find_parts(job) -> list[Part]
- quote(job, parts) -> Quote
- rank_techs(job) -> list[{tech_id, score, reasons}]
- free_slots(tech_id) -> list[slot]
- book(tech_id, job_id, slot) -> event_id
- log_event(job_id, agent, message)

## Rules for coding agents

- Stay inside the folder for the task you were given. Don't refactor other
  people's folders.
- Keep functions pure where possible so they can be tested from a script
  without Photon.
- Only one function moves job status forward. Don't change status from
  multiple handlers.
- Secrets go in .env, never in code. Keep .env.example up to date.
- Small commits, clear messages. Don't touch the core-demo tag.
- If an external API isn't working, add a clearly marked fallback (hardcoded
  data) instead of blocking.

## How the code implements the contract

- `shared/schema.py`: Pydantic models (Job, Part, Quote, Tech, RankedTech, Slot, Offer)
  and `TRANSITIONS`, the legal status moves.
- `shared/db.py`: all reads/writes plus `log_event`. Uses Supabase when
  `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` are set, else an in-memory FALLBACK store.
- `messaging/state.py:advance()` is the only function that changes job status.
  `db.update_job_fields()` refuses `status`.
- Every stub is marked `STUB` or `FALLBACK` in its docstring. Replace the body,
  keep the signature.
- `python -m scripts.smoke_test` runs the full loop (NO -> next tech -> YES -> booked)
  with no network. It must pass before every merge to main.
- Run the server: `uvicorn main:app --reload --port 8000`; dashboard at `/`,
  simulate a text with `POST /dev/inbound {"phone","text","photo_url"}`.
