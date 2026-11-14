"""Outbound iMessage. Owner: Person A.

STUB/FALLBACK: when Photon isn't configured, messages are printed and logged
as events so the loop (and the dashboard) still works.
TODO(A): wire the Photon send call here.
"""
import os

from shared.db import log_event


def send_text(phone: str, text: str, *, job_id: str | None = None, photo_url: str | None = None) -> None:
    if os.getenv("PHOTON_API_KEY"):
        pass  # TODO(A): Photon send; on exception fall through to the log below
    log_event(job_id, "outbox", f"to {phone}: {text}")
