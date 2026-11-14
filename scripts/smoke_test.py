"""End-to-end stub loop, no server and no Photon.

Customer texts -> offer to tech #1 -> customer adds a detail (same job, tech #1 gets it)
-> tech #1 says NO -> offer to tech #2 -> YES -> accepted -> customer texts again
(forwarded to tech #2, still one job) -> "new job ..." starts a second job.
Run: python -m scripts.smoke_test     (A runs this after every merge)
"""
import os

os.environ["SUPABASE_URL"] = ""  # always run against the in-memory store
os.environ["PHOTON_API_KEY"] = ""  # never send real iMessages from the smoke test
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

dispatch.handle_inbound(first.phone, "nope sorry")
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

# "new job" always starts a fresh job.
dispatch.handle_inbound(CUSTOMER, "new job: toilet keeps running")
assert len(db.list_jobs()) == 2, "'new job' should open a second job"

print(f"\nPASS: follow-ups joined the open job, {first.name} declined, {second.name} accepted, "
      f"'new job' opened a second job. {len(db.list_messages())} messages logged.")
