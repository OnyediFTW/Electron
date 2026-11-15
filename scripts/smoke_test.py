"""End-to-end stub loop, no server and no Photon.

Customer texts -> offer to tech #1 -> customer adds a detail (same job, tech #1 gets it)
-> tech #1 says NO -> offer to tech #2 -> YES -> accepted -> customer texts again
(forwarded to tech #2, still one job) -> "new job ..." starts a second job.
Run: python -m scripts.smoke_test     (A runs this after every merge)
"""
import os
import time

os.environ["SUPABASE_URL"] = ""  # always run against the in-memory store
os.environ["PHOTON_API_KEY"] = ""  # never send real iMessages from the smoke test
os.environ["GEMINI_API_KEY"] = ""  # deterministic: keyword triage + rule-based router (no network)
os.environ["DEMO_CUSTOMER_PHONE"] = ""  # no seeded past job (B's demo memory) in the test data
os.environ["USE_GOOGLE_CALENDAR"] = "0"  # never write test bookings to the real Google calendars
os.environ.setdefault("OFFER_TIMEOUT_SECONDS", "600")

from messaging import dispatch  # noqa: E402
from shared import db  # noqa: E402
from shared.schema import JobStatus  # noqa: E402
from shared.seed import seed  # noqa: E402

CUSTOMER = "+16095550100"


def texts_to(phone: str) -> list[str]:
    return [m["text"] for m in db.list_messages() if m["phone"] == phone and m["direction"] == "out"]


seed()
dispatch.handle_inbound(CUSTOMER, "kitchen sink leaking underneath", "https://example.com/ptrap.jpg")
job = db.list_jobs()[0]
assert job.status == JobStatus.offered, job.status
first = db.get_tech(db.list_offers(job.id)[0].tech_id)

# A second text while the offer is out joins the same job and reaches the offered tech.
dispatch.handle_inbound(CUSTOMER, "also it's dripping fast")
assert len(db.list_jobs()) == 1, "follow-up must not create a second job"
assert "dripping fast" in db.get_job(job.id).raw_text
assert any("Update from the customer" in t for t in texts_to(first.phone)), texts_to(first.phone)
assert len(db.list_offers(job.id)) == 1, "follow-up must not re-offer the job"

# Offers take only YES or NO: "nope sorry" gets a reminder and the offer stays open; "No." declines.
dispatch.handle_inbound(first.phone, "nope sorry")
assert len(db.list_offers(job.id)) == 1 and db.list_offers(job.id)[0].response is None
assert texts_to(first.phone)[-1] == "Reply YES to take the job or NO to pass.", texts_to(first.phone)[-1]
dispatch.handle_inbound(first.phone, "No.")
offers = db.list_offers(job.id)
assert len(offers) == 2 and offers[0].response == "no", offers

second = db.get_tech(offers[1].tech_id)
dispatch.handle_inbound(second.phone, "yes!")
job = db.get_job(job.id)
# No scheduling yet (free_slots stub returns []), so YES leaves the job accepted, not booked.
assert job.status == JobStatus.accepted and job.assigned_tech_id == second.id, job

# After acceptance, the customer's texts go to the assigned tech, still one job.
dispatch.handle_inbound(CUSTOMER, "when can you come?")
assert len(db.list_jobs()) == 1
assert any("when can you come?" in t for t in texts_to(second.phone)), texts_to(second.phone)

# The customer was asked to approve the estimate; "ok" books the job and tells the tech.
assert any("Reply OK to confirm" in t for t in texts_to(CUSTOMER)), texts_to(CUSTOMER)
assert not any("+1 (609) 555-0100" in t or CUSTOMER in t for t in texts_to(second.phone)), "phone before booking"
dispatch.handle_inbound(CUSTOMER, "ok")
assert db.get_job(job.id).status == JobStatus.booked, db.get_job(job.id).status
booked_msg = next(t for t in texts_to(second.phone) if "Customer approved" in t)
assert "Customer: +1 (609) 555-0100" in booked_msg, booked_msg

# "new job" always starts a fresh job.
dispatch.handle_inbound(CUSTOMER, "new job: toilet keeps running")
assert len(db.list_jobs()) == 2, "'new job' should open a second job"
job2 = db.list_jobs()[-1]
assert job2.status == JobStatus.offered

# Workload: the tech already on a job is ranked below free techs and says so.
from matching import rank_techs  # noqa: E402
busy = next(r for r in rank_techs(job2) if r.tech_id == second.id)
assert "1 job today" in busy.reasons, busy.reasons  # fair share counts the job they took

# Restart: timers are lost; resume_offers re-arms them and times out overdue ones.
for t in list(dispatch._timers.values()):
    t.cancel()
dispatch._timers.clear()
pending = [o for o in db.list_offers(job2.id) if o.response is None][0]
db.store.update("offers", pending.id, {"sent_at": "2000-01-01T00:00:00+00:00"})
assert dispatch.resume_offers() == 1
time.sleep(0.5)
offers2 = db.list_offers(job2.id)
assert offers2[0].response == "timeout" and len(offers2) == 2, offers2

# Owner's view API reads the same data.
from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402

client = TestClient(app)
s = client.get("/api/owner/summary").json()
assert s["working"] == 1 and s["open"] == 1 and s["jobs_24h"] == 2, s
a = client.get("/api/owner/associates").json()
on_job = next(x for x in a["associates"] if x["name"] == second.name)
assert on_job["state"] == "on a job" and on_job["customers"][0]["customer"] == CUSTOMER, on_job
assert a["incoming"] and a["incoming"][0]["id"] == job2.id, a["incoming"]
assert all(x["description"] for x in a["associates"]), "every tech should have a description"

# Unreachable techs (never texted the Photon line) are skipped, not offered and waited on.
from messaging import photon  # noqa: E402

reachable = {second.phone}  # only the lower-ranked, busy tech can be texted
photon.configured = lambda: True
photon.get_contact = lambda p: {"space_id": "s", "line_user_id": "u"} if p in reachable else None
photon.send = lambda *a, **k: None
dispatch.handle_inbound("+16095550199", "new job: kitchen faucet dripping")
job3 = db.list_jobs()[-1]
offers3 = db.list_offers(job3.id)
assert len(offers3) == 1 and offers3[0].tech_id == second.id, offers3
assert any(e["message"].startswith("skipped") and first.name in e["message"] for e in db.list_events(job3.id))

# A new photo after the job is booked is a new problem: full triage, not a forward to the booked tech.
before = len(db.list_jobs())
dispatch._handle_customer_text(CUSTOMER, "the pipes under my sink are leaking",
                               "http://127.0.0.1:9/photo.jpg", db.get_job(job.id))
assert len(db.list_jobs()) == before + 1, "a photo after booking should open a new job"
assert db.list_jobs()[-1].status != JobStatus.new, "the new job should have been triaged"

# Start over (live phones page): the customer's next text opens a new job, even with one in progress.
dispatch.handle_inbound(CUSTOMER, "new job: shower drain is clogged")
assert db.list_jobs()[-1].status == JobStatus.offered
before = len(db.list_jobs())
dispatch.start_over()
dispatch.handle_inbound(CUSTOMER, "my bathroom faucet is dripping and won't stop", "https://example.com/faucet.jpg")
assert len(db.list_jobs()) == before + 1, "after Start over the next photo should open a new job"

# Customer rules: no photo and no open job -> no reply and no job; the text waits for the photo.
NEWBIE = "+16095550123"
dispatch.handle_inbound(NEWBIE, "hi")
dispatch.handle_inbound(NEWBIE, "my water heater is leaking from the bottom")
assert not texts_to(NEWBIE) and not db.store.select("jobs", customer_phone=NEWBIE), texts_to(NEWBIE)
dispatch.handle_inbound(NEWBIE, "", "https://example.com/heater.jpg")
newbie_job = db.list_jobs()[-1]
assert newbie_job.customer_phone == NEWBIE and "water heater is leaking" in newbie_job.raw_text, newbie_job
# Off-topic with an open job: no reply.
n = len(texts_to(NEWBIE))
dispatch.handle_inbound(NEWBIE, "thanks!")
assert len(texts_to(NEWBIE)) == n, texts_to(NEWBIE)[n:]

# Part photos for the tech come from live listings that have an image, capped at 3.
from agents import job_prep  # noqa: E402

fake = {"parts_plan": [{"live": {"image": f"https://x/{i}.jpg", "title": f"Part {i}", "price": 2.5, "store": "Ace"}}
                       for i in range(5)] + [{"live": None}]}
assert job_prep.part_photos(fake) == [(f"https://x/{i}.jpg", f"Part {i}: $2.50 at Ace") for i in range(3)]
assert "&prds=catalogid:42" in job_prep._short_link({"title": "PVC P-Trap 1-1/2 in", "product_id": "42"})

print(f"\nPASS: follow-ups joined the open job, {first.name} declined, {second.name} accepted, "
      f"'new job' opened a second job, workload ranking, restart resume, owner API, "
      f"unreachable techs skipped, photo after booking opens a new job, strict YES/NO, no reply without a photo or off-topic, contact info after booking. "
      f"{len(db.list_messages())} messages logged.")
