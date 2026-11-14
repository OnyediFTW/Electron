# Person A plan: messaging and dispatch

**You own:** `messaging/` (webhook, router, dispatch loop, state machine) and the Supabase project.
**Branch:** `a/photon-dispatch`
**Your job in one line:** a real iMessage in, a real iMessage offer out, YES books it. You call B's and C's
functions and only care that they return the agreed shapes.

## Already done (on `main`)

- FastAPI app, `shared/` contract, in-memory DB fallback, seed techs
- Router (tech phone → tech flow, anyone else → customer flow)
- Customer flow, forgiving YES/NO parsing, timeout → next tech, booking on YES
- `advance()` is the only status writer; a lock prevents YES/timeout races
- `/dev/inbound` simulator and `python -m scripts.smoke_test`

What's left is mostly **Photon**: inbound webhook, outbound send, photos.

## Photon facts you need (checked against `https://api.photon.codes/openapi.json`)

- Base URL `https://api.photon.codes/v1`, header `Authorization: Bearer pho_sk_...` (project key).
  The project key only works under `/v1/projects/{projectId}/...` (id starts `pho_prj_`).
- **A shared iMessage line cannot text someone first.** Each person (demo customer phone and every
  tech phone) needs an **assignment**, and must **text the line once** before we can message them.
  After that first inbound text we can send to them any time.
  Different people may be assigned **different line numbers**, so there's no single "Photon number".
  - Create: `POST /v1/projects/{id}/platforms/imessage/assignments` with `{"handle": "+1609..."}`
- **Send:** `POST /v1/projects/{id}/messages` with an `idempotency-key` header (any unique string) and
  ```json
  {"spaceId": "pho_spc_...", "senderId": "pho_usr_...",
   "content": {"type": "text", "format": "plain", "text": "..."}}
  ```
  `spaceId` and `senderId` come from that person's inbound message: `space.id` and `receivedBy.id`
  (the line's user). **Save them per phone.** 202 means accepted, not delivered.
- **Inbound webhook:** register at `POST /v1/projects/{projectId}/webhooks/destinations` with a
  public URL. The event `type` is `message.received`, and the message is at `data.object`:
  - sender phone: `data.object.sentBy.handle`
  - `data.object.space.id`, `data.object.receivedBy.id`
  - `data.object.content` is ONE of `{"type":"text","text":...}` or `{"type":"attachment","attachmentId":...}`
- **A photo and its caption arrive as separate messages.** Buffer by sender for ~5 seconds and merge.
- **Photo bytes:** `GET /v1/projects/{id}/attachments/{attachmentId}/content` (needs the bearer key).
- Signatures follow the Standard Webhooks scheme (`webhook-id`, `webhook-timestamp`,
  `webhook-signature` headers). The `standardwebhooks` pip package verifies them.

## Plan

### By 4:30 PM: real text in, real text out
1. **Supabase** (10 min): create the project, run `supabase/schema.sql`, seed with
   `python -m shared.seed`, and send `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` to B and C privately.
2. **Photon project** (15 min): project key + project id into `.env`
   (`PHOTON_API_KEY`, `PHOTON_PROJECT_ID`; add both to `.env.example`). Create assignments for your
   demo customer phone and the two tech phones. Have each phone text its line "hi".
3. **Contacts table** (new, owned by you; post in the group chat): `contacts(phone pk, space_id,
   line_user_id, updated_at)`. Upsert on every inbound message. `send_text()` looks up the phone here.
4. **Tunnel:** `cloudflared tunnel --url http://localhost:8000` (or ngrok). Register the URL +
   `/webhook/photon` as a webhook destination.
5. **Rewrite `photon_webhook`** for the real payload: verify signature, ignore anything that isn't
   `message.received`, upsert contact, then hand off to `handle_inbound`.
6. **`send_text()`:** real Photon POST with a 5 s timeout. On any error, `log_event(..., "outbox",
   "SEND FAILED ...")` and keep going. Never raise.

**Done when:** you text the line from your phone, the dashboard shows the job, and the top tech's
phone gets the offer.

### By 6:00 PM: photos and polish
7. **Photos:** on an attachment, download the bytes, save to `photos/{attachmentId}.jpg`, and serve
   at `GET /photos/{id}` so `photo_url` is a public tunnel URL B's vision model can read.
   (Alternative: Supabase Storage public bucket.)
8. **Merge photo + text:** per-sender 5 s buffer, then one `handle_inbound(phone, text, photo_url)`.
9. **Open-job rule:** if a customer with a job still in progress texts again, attach the text to
   that job (log it) instead of creating a second job. This also handles B's
   `follow_up_question`: re-run `intake` with the original photo plus the answer.
10. **Send the photo to the tech** with the offer (upload with `POST /attachments`, then send
    `{"type":"attachment"}`). Skip it if it takes over 30 min; text alone is fine.

### By 9:00 PM: core loop live on phones
11. Swap in B's real `intake` and C's real `rank_techs` as they land. Run the smoke test after
    every merge.
12. Rehearse the demo flow 3 times in a row on real phones: P-trap photo → offer → YES → booked,
    then NO → next tech. Then `git tag core-demo && git push origin core-demo`.

### After 9 PM (branches only)
- Stretch: Photon voice call for Best Voice Agent (`content.type = "voice"`).
- Reset script for the demo: `python -m shared.seed --reset` plus re-run assignments if needed.

## What you need from others
- **B:** `intake()` must accept a URL that's only reachable through the tunnel. Tell B to download it
  and send base64 to the model, not to pass the URL through.
- **C:** each tech's `phone` in `techs` must exactly match the E.164 handle Photon sends (`+1...`).

## Risks
- Photon assignment or first-text opt-in not done on demo phones → nothing sends. Do this first and
  re-check before judging.
- The tunnel URL changes on restart → re-register the webhook. Keep one tunnel running all night.
- Fallback: `/dev/inbound` and the dashboard still show the full loop if Photon is down.

## Paste into your coding agent
> I'm Person A on Electron. Read CLAUDE.md and docs/plans/person-a.md. Work only in `messaging/`
> (and the new `contacts` table). Start with step 5: rewrite `photon_webhook` for Photon's
> `message.received` payload and implement `send_text()` against `POST /v1/projects/{id}/messages`.
> Keep the smoke test passing.
