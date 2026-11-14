"""The ONLY place job status changes. Owner: Person A.

new -> triaged -> offered -> accepted -> booked   (any -> failed)
"""
import threading

from shared import db
from shared.schema import TRANSITIONS, JobStatus

_lock = threading.Lock()


class IllegalTransition(Exception):
    pass


def advance(job_id: str, to: JobStatus, why: str = "") -> None:
    with _lock:
        job = db.get_job(job_id)
        if job is None:
            raise IllegalTransition(f"no job {job_id}")
        if to not in TRANSITIONS[job.status]:
            raise IllegalTransition(f"{job.status.value} -> {to.value} not allowed")
        db._set_status(job_id, to.value)
    db.log_event(job_id, "state", f"{job.status.value} -> {to.value}" + (f" ({why})" if why else ""))
