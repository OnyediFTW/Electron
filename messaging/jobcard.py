"""Interactive job card for techs. Owner: Person A.

GET  /j/{offer_id}          phone page for one offer (with link-preview tags so iMessage shows a card)
POST /j/{offer_id}/accept   same as texting YES
POST /j/{offer_id}/pass     same as texting NO

The offer id is a random UUID, so the link is the access token. The customer's number is
only shown once this tech has accepted.
"""
from __future__ import annotations

from html import escape

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from matching.rank import miles
from messaging import dispatch
from shared import db

router = APIRouter()

URGENCY = {"high": ("Urgent", "#E4572E"), "medium": ("Soon", "#F2C14E"), "low": ("Whenever", "#8CC7A1")}
DONE = {"accepted": "You've got the job. The customer's number is below.",
        "passed": "Passed. We've offered it to the next plumber."}


def _e(s) -> str:
    return escape(str(s or ""), quote=True)


@router.get("/j/{offer_id}", response_class=HTMLResponse)
def job_card(offer_id: str, done: str | None = None):
    rows = db.store.select("offers", id=offer_id)
    if not rows:
        raise HTTPException(404, "This job card doesn't exist.")
    offer = rows[0]
    job, tech = db.get_job(offer["job_id"]), db.get_tech(offer["tech_id"])
    if not job or not tech:
        raise HTTPException(404, "This job card doesn't exist.")

    label, color = URGENCY.get(job.urgency.value if job.urgency else "", ("Not triaged", "#C3C9D0"))
    title = job.issue or "Problem not identified yet"
    quote = f"${job.quote.low:.0f}–{job.quote.high:.0f}" if job.quote else "Not available yet"
    dist = f"{miles((job.location.lat, job.location.lng), (tech.lat, tech.lng)):.1f} mi from you" if job.location else ""
    maps = f"https://maps.google.com/?q={job.location.lat},{job.location.lng}" if job.location else None
    response = offer.get("response")
    mine = response == "yes"
    status = (DONE.get(done) or {"yes": "You accepted this job.", "no": "You passed on this job.",
                                 "timeout": "This offer timed out and went to the next plumber."}.get(response, ""))
    open_now = response is None and job.status.value == "offered"

    parts = "".join(
        f'<li><span>{_e(p.name)} <em>{p.confidence:.0%} likely</em></span>'
        + (f'<a href="{_e(p.url)}">{"$%.2f" % p.price if p.price else "Find"}</a>' if p.url else "")
        + "</li>" for p in job.parts) or "<li><span>None identified</span></li>"
    desc = f"{label} · {quote}" + (f" · {dist}" if dist else "")
    og_image = f'<meta property="og:image" content="{_e(job.photo_url)}">' if job.photo_url else ""

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} · Electron</title>
<meta property="og:title" content="{_e(title)}">
<meta property="og:description" content="{_e(desc)}">
<meta property="og:site_name" content="Electron">
{og_image}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@700&family=IBM+Plex+Mono:wght@600&family=IBM+Plex+Sans:wght@400;600&display=swap" rel="stylesheet">
<style>
  :root {{ --ground:#F4F5F6; --ink:#17191C; --muted:#5A6068; --rule:#D5D9DE; --steel:#2B3036; --copper:#B45A2A; --ok:#1F7A45; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --ground:#15181C; --ink:#ECEEF0; --muted:#A3AAB3; --rule:#333941; --copper:#E08A5A; --ok:#6FD39A; }} }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--ground); color:var(--ink); font:16px/1.45 'IBM Plex Sans', system-ui, sans-serif; }}
  header {{ background:var(--steel); color:#F3F4F1; padding:14px 18px; font:700 22px/1 'Barlow Condensed', sans-serif; letter-spacing:.04em; text-transform:uppercase; }}
  main {{ max-width:560px; margin:0 auto; padding:16px; }}
  .tab {{ display:flex; justify-content:space-between; background:{color}; color:#17191C; font:600 12px 'IBM Plex Mono', monospace; letter-spacing:.1em; text-transform:uppercase; padding:6px 12px; border-radius:6px 6px 0 0; }}
  .card {{ background:#FFFFFF; border-radius:0 0 6px 6px; padding:16px; box-shadow:0 2px 10px rgba(0,0,0,.08); }}
  @media (prefers-color-scheme: dark) {{ .card {{ background:#22262B; }} }}
  h1 {{ margin:0 0 4px; font:700 30px/1.05 'Barlow Condensed', sans-serif; text-transform:uppercase; letter-spacing:.02em; }}
  .meta {{ font:600 13px 'IBM Plex Mono', monospace; color:var(--muted); }}
  img {{ display:block; width:100%; border-radius:6px; margin:14px 0 6px; }}
  h2 {{ margin:18px 0 6px; font:600 12px 'IBM Plex Mono', monospace; letter-spacing:.12em; text-transform:uppercase; color:var(--muted); }}
  p {{ margin:0; }}
  ul {{ list-style:none; margin:0; padding:0; }}
  li {{ display:flex; justify-content:space-between; gap:12px; padding:9px 0; border-bottom:1px solid var(--rule); }}
  li em {{ color:var(--muted); font-style:normal; font-size:13px; }}
  a {{ color:var(--copper); font-weight:600; }}
  .big {{ font:700 28px 'Barlow Condensed', sans-serif; color:var(--copper); }}
  .actions {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:20px; }}
  .actions form {{ margin:0; }}
  button, .btn {{ display:block; width:100%; min-height:52px; border-radius:6px; font:600 17px 'IBM Plex Sans', sans-serif; text-align:center; text-decoration:none; padding:14px; cursor:pointer; }}
  .yes {{ background:var(--ok); color:#fff; border:0; }}
  .no {{ background:transparent; color:var(--ink); border:2px solid var(--rule); }}
  .btn {{ background:transparent; border:2px solid var(--copper); color:var(--copper); margin-top:10px; }}
  .status {{ margin:14px 0 0; padding:12px; border-radius:6px; background:color-mix(in srgb, var(--ok) 14%, transparent); font-weight:600; }}
  button:focus-visible, a:focus-visible {{ outline:3px solid #FFB27A; outline-offset:2px; }}
</style></head>
<body><header>Electron · Job for {_e(tech.name.split()[0])}</header>
<main>
  <div class="tab"><span>{_e(label)}</span><span>{_e(dist)}</span></div>
  <div class="card">
    <h1>{_e(title)}</h1>
    <div class="meta">{_e((job.trade or "").replace("_", " "))}</div>
    {f'<img src="{_e(job.photo_url)}" alt="Photo the customer sent">' if job.photo_url else ""}
    {f'<h2>Photo shows</h2><p>{_e(job.photo_description)}</p>' if job.photo_description else ""}
    <h2>Customer said</h2><p>{_e(job.raw_text).replace(chr(10), "<br>")}</p>
    <h2>Likely parts</h2><ul>{parts}</ul>
    <h2>Quote</h2><p class="big">{_e(quote)}</p>
    {f'<a class="btn" href="{_e(maps)}">Directions</a>' if maps else ""}
    {f'<a class="btn" href="sms:{_e(job.customer_phone)}">Text the customer</a><a class="btn" href="tel:{_e(job.customer_phone)}">Call {_e(job.customer_phone)}</a>' if mine else ""}
    {f'<p class="status">{_e(status)}</p>' if status else ""}
    {f'''<div class="actions">
      <form method="post" action="/j/{_e(offer_id)}/accept"><button class="yes" type="submit">Accept job</button></form>
      <form method="post" action="/j/{_e(offer_id)}/pass"><button class="no" type="submit">Pass</button></form>
    </div>''' if open_now else ""}
  </div>
</main></body></html>"""
    return HTMLResponse(html)


@router.post("/j/{offer_id}/accept")
def accept(offer_id: str):
    result = dispatch.respond_to_offer(offer_id, accept=True)
    return RedirectResponse(f"/j/{offer_id}" + ("?done=accepted" if result == "accepted" else ""), status_code=303)


@router.post("/j/{offer_id}/pass")
def pass_offer(offer_id: str):
    result = dispatch.respond_to_offer(offer_id, accept=False)
    return RedirectResponse(f"/j/{offer_id}" + ("?done=passed" if result == "passed" else ""), status_code=303)
