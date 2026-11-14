"""Outbound iMessage. Owner: Person A.

Sends through Photon when PHOTON_API_KEY + PHOTON_PROJECT_ID are set. Every
message is recorded in `messages` (for the live /phones view) and as an
"outbox" event. A failed send is logged instead of raised (FALLBACK), so the
loop and the dashboard keep working.

With photo_url, the actual image is sent first (uploaded to Photon as an
attachment), then the text. If the image can't be sent, the text carries a
link to it instead, so the photo is never lost.
"""
from messaging import photon
from shared.db import log_event, log_message


def send_text(phone: str, text: str, *, job_id: str | None = None, photo_url: str | None = None) -> None:
    if photo_url and photon.configured():
        try:
            photon.send_photo(phone, photo_url)
            log_event(job_id, "outbox", f"photo sent to {phone}")
        except Exception as e:
            log_event(job_id, "outbox", f"PHOTO FAILED to {phone}, sending a link instead: {e!r}")
            text = f"{text}\nPhoto: {photo_url}"
            photo_url = None  # the live view shows what was actually delivered: the link

    log_message(phone, "out", text, photo_url=photo_url, job_id=job_id)
    if photon.configured():
        try:
            photon.send(phone, text)
        except Exception as e:
            log_event(job_id, "outbox", f"SEND FAILED to {phone}: {e!r}\n{text}")
            return
    log_event(job_id, "outbox", f"to {phone}: {text}")
