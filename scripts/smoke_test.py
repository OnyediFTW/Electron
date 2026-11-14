"""End-to-end stub loop, no server and no Photon.

Customer texts -> offer to tech #1 -> tech #1 says NO -> offer to tech #2 -> YES -> booked.
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

seed()
dispatch.handle_inbound("+16095550100", "kitchen sink leaking underneath", "https://example.com/ptrap.jpg")
job = db.list_jobs()[0]
assert job.status == JobStatus.offered, job.status

first = db.get_tech(db.list_offers(job.id)[0].tech_id)
dispatch.handle_inbound(first.phone, "nope sorry")
offers = db.list_offers(job.id)
assert len(offers) == 2 and offers[0].response == "no", offers

second = db.get_tech(offers[1].tech_id)
dispatch.handle_inbound(second.phone, "yes!")
job = db.get_job(job.id)
assert job.status == JobStatus.booked and job.assigned_tech_id == second.id, job

print(f"\nPASS: {first.name} declined, {second.name} booked. {len(db.list_events(job.id))} events logged.")
