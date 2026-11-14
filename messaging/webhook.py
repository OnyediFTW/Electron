"""HTTP entry points for inbound texts. Owner: Person A."""
from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel

from messaging.dispatch import handle_inbound

router = APIRouter()


class InboundText(BaseModel):
    phone: str
    text: str = ""
    photo_url: str | None = None


@router.post("/webhook/photon")
async def photon_webhook(req: Request, bg: BackgroundTasks):
    payload = await req.json()
    # TODO(A): map Photon's real payload shape. Guessing common field names for now.
    attachments = payload.get("attachments") or []
    msg = InboundText(
        phone=payload.get("from") or payload.get("sender") or payload.get("phone", ""),
        text=payload.get("text") or payload.get("body", ""),
        photo_url=attachments[0] if attachments else payload.get("photo_url"),
    )
    bg.add_task(handle_inbound, msg.phone, msg.text, msg.photo_url)
    return {"ok": True}


@router.post("/dev/inbound")
def dev_inbound(msg: InboundText, bg: BackgroundTasks):
    """Simulate a text without Photon (see README)."""
    bg.add_task(handle_inbound, msg.phone, msg.text, msg.photo_url)
    return {"ok": True}
