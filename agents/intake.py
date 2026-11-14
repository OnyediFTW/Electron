"""intake(photo_url, text) -> Job.   Owner: Person B.

STUB: returns a hardcoded leaking-P-trap triage so A can wire the loop.
Replace with: vision LLM + strict JSON prompt, validated against Job.
If the photo is unclear, set job.follow_up_question instead of guessing.
"""
from typing import Optional

from shared.db import log_event
from shared.schema import Job, Part, Urgency


def intake(photo_url: Optional[str], text: str, *, customer_phone: str = "",
           job_id: Optional[str] = None) -> Job:
    job = Job(
        **({"id": job_id} if job_id else {}),
        customer_phone=customer_phone,
        photo_url=photo_url,
        raw_text=text,
        trade="plumbing",
        issue="leaking P-trap under kitchen sink",
        urgency=Urgency.high,
        parts=[
            Part(name="1-1/2 in P-trap kit", confidence=0.7),
            Part(name="1-1/2 in slip-joint washer", confidence=0.5),
        ],
    )
    log_event(job.id, "intake", f"STUB triage: {job.issue} ({job.urgency.value} urgency)")
    return job
