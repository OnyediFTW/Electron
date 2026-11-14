"""intake(photo_url, text) -> Job.   Owner: Person B.

STUB: the vision model isn't connected yet, so this does NOT guess. It keeps the
customer's words and leaves issue, urgency and parts unknown (None / []).
Replace with: vision LLM + strict JSON prompt, validated against Job.
If the photo is unclear, set job.follow_up_question instead of guessing.
"""
from typing import Optional

from shared.db import log_event
from shared.schema import Job


def intake(photo_url: Optional[str], text: str, *, customer_phone: str = "",
           job_id: Optional[str] = None) -> Job:
    job = Job(
        **({"id": job_id} if job_id else {}),
        customer_phone=customer_phone,
        photo_url=photo_url,
        raw_text=text,
        trade="plumbing",  # Electron only dispatches plumbers for now
        issue=None,
        urgency=None,
        parts=[],
    )
    log_event(job.id, "intake", "STUB: can't identify the problem yet (vision model not connected)")
    return job
