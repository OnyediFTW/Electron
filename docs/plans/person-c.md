# Person C plan: matching, calendar, dashboard, demo

**You own:** `matching/` (`rank.py`, `calendar.py`), `dashboard/`, `shared/seed.py` data, and the
Devpost, video and Peri evidence.
**Branch:** `c/matching` (merge small and often into `main`)
**Your job in one line:** pick the right tech for a visible reason, put it on a real calendar, and make
everything judges see besides the phones look good.

## Your contract (don't change without telling the team)

```python
rank_techs(job) -> list[RankedTech]              # [{tech_id, score, reasons}], best first
free_slots(tech_id) -> list[Slot]                 # [{start, end}], soonest first
book(tech_id, job_id, slot) -> str                # calendar event id
```

- Call `log_event(job.id, "matching" | "calendar", "...")` at each step.
- Matching is an algorithm, not an LLM call. The `reasons` list goes straight into the tech's offer
  text, so write it for a plumber: `"closest free plumber, 2.1 mi"`.

## Already done (on `main`)
- 7 seeded techs around Princeton (`shared/seed.py`), 2 using `TECH_PHONE_1` / `TECH_PHONE_2`
- `rank_techs` with a `WEIGHTS` dict: skill, distance, rating. `free` and `workload` are at 0 (TODO)
- Stub `free_slots` / `book` (fake slots tomorrow, fake event id)
- Dashboard at `/`: jobs list and a live trace, polling every 2 seconds

## Plan

### By 4:30 PM: data
1. Get teammates' real phone numbers into `.env` (`TECH_PHONE_1`, `TECH_PHONE_2`) in E.164 (`+1609...`),
   then `python -m shared.seed --reset` once A shares the Supabase keys.
   These must exactly match what Photon sends.
2. Make the seed tell a story: one clear best plumber, one close but busy, one far away, one
   electrician (so the outlet photo shows "no skill match").

### By 7:00 PM: real `rank_techs()` (the 7 PM checkpoint is "a real photo picks the right seeded tech")
3. Fill in the two TODO weights:
   - `free`: has a free slot in the next 48 h (from `free_slots`); sooner is better. For `high`
     urgency, weight soonest-free more.
   - `workload`: fewer jobs with status `accepted`/`booked` assigned to this tech is better.
4. Keep all weights in the one `WEIGHTS` dict at the top. Show the breakdown in `reasons`.
5. `scripts/test_rank.py`: 4 to 5 fake jobs with an expected top tech each. Print scores so you can
   tune the weights in front of judges if asked.

### By 9:00 PM: Google Calendar
6. Setup (30 min):
   - Google Cloud project → enable the Calendar API → service account → download the JSON key
     (path in `GOOGLE_APPLICATION_CREDENTIALS`, file never committed).
   - In one team Google account, create a calendar per demo tech. Share each one with the service
     account's email ("Make changes to events"). Put each calendar id in that tech's `calendar_id`.
   - Add a few busy blocks so the free/busy logic visibly matters.
7. `free_slots`: `freebusy.query` for the next 48 h, business hours 8 AM to 6 PM, 2-hour slots.
8. `book`: `events.insert` with summary `"Electron: <issue>"`, a description with parts, quote
   and customer phone, and return the event id.
9. Any Google error → log it and fall back to the current stub. The demo must not break.
   Libraries: `google-api-python-client`, `google-auth` (add to `requirements.txt`).

### By 1:00 AM: dashboard (judges watch this during the live demo)
10. For the selected job, show:
    - the **offer chain**: who was offered, and declined, timed out or accepted
    - the **score breakdown** for the ranked techs
    - parts with confidence and links, and the quote
    Add read-only endpoints in `main.py` (for example `/api/jobs/{id}/offers`), and post in the
    group chat since `main.py` is shared.
11. Readable from across a table: large type, status colors, newest job on top.
12. Polish is your call, but nothing that can break the 2-second polling.

### Through the night: the story
13. **Peri evidence (today):** call the plumbing owner back for his name, role, business name, and a
    quote or screenshot. Fill the TODO in `README.md` and `CLAUDE.md`.
14. **Devpost:** one main track (Business and Enterprise), special tracks added, Discord usernames.
15. **Video under 2 minutes** by 5 AM, following the demo script in the README.
16. **Demo day:** phones charged, `python -m shared.seed --reset`, `core-demo` tag checked out.

## What you need from others
- **A:** Supabase keys, and confirmation that Photon assignments include the tech phones.
- **B:** consistent `trade` values (`plumbing`, `drain`, `water_heater`, `gas`, `sewer`, `electrical`).

## Paste into your coding agent
> I'm Person C on Electron. Read CLAUDE.md and docs/plans/person-c.md. Work only in `matching/`,
> `dashboard/` and `scripts/test_rank.py`. Start by adding the `free` and `workload` terms to
> `rank_techs` in `matching/rank.py`, keeping the WEIGHTS dict and readable reasons. Keep
> `python -m scripts.smoke_test` passing.
