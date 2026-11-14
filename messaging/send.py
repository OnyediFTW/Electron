"""Outbound iMessage. Owner: Person A.

Sends through Photon when PHOTON_API_KEY + PHOTON_PROJECT_ID are set. Every
message is recorded in `messages` (for the live /phones view) and as an
"outbox" event. A failed send is logged instead of raised (FALLBACK), so the
loop and the dashboard keep working.
"""
from messaging import photon
from shared.db import log_event, log_message


def send_text(phone: str, text: str, *, job_id: str | None = None, photo_url: str | None = None) -> None:
    log_message(phone, "out", text, job_id=job_id)
    if photon.configured():
        try:
            photon.send(phone, text)
        except Exception as e:
            log_event(job_id, "outbox", f"SEND FAILED to {phone}: {e!r}\n{text}")
            return
    log_event(job_id, "outbox", f"to {phone}: {text}")
