"""HTTP entry points for inbound texts. Owner: Person A."""
import json
import os

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from pydantic import BaseModel

from messaging import photon
from messaging.dispatch import handle_inbound
from shared.db import log_event

router = APIRouter()
_buffer = photon.MergeBuffer(handle_inbound)


class InboundText(BaseModel):
    phone: str
    text: str = ""
    photo_url: str | None = None


def _verify(body: bytes, headers) -> dict:
    """Standard Webhooks signature check when PHOTON_WEBHOOK_SECRET is set."""
    secret = os.getenv("PHOTON_WEBHOOK_SECRET")
    if not secret:
        return json.loads(body)
    from standardwebhooks.webhooks import Webhook
    try:
        return Webhook(secret).verify(body, dict(headers))
    except Exception as e:
        log_event(None, "error", f"webhook signature check failed: {e!r}")
        raise HTTPException(401, "bad signature") from None


@router.post("/webhook/photon")
async def photon_webhook(req: Request):
    event = _verify(await req.body(), req.headers)
    msg = photon.parse_event(event)
    if msg is None:
        return {"ok": True, "ignored": event.get("type")}
    if msg["space_id"] and msg["line_user_id"]:
        photon.save_contact(msg["phone"], msg["space_id"], msg["line_user_id"])
    photo_url = None
    if msg["attachment_id"]:
        base = os.getenv("PUBLIC_BASE_URL", str(req.base_url)).rstrip("/")
        photo_url = f"{base}/photos/{msg['attachment_id']}"
    _buffer.add(msg["phone"], msg["text"], photo_url)
    return {"ok": True}


@router.get("/photos/{attachment_id}")
def photo(attachment_id: str):
    """Public proxy so B's vision model can read a Photon attachment by URL."""
    if not attachment_id.startswith("pho_att_"):
        raise HTTPException(404)
    r = photon.fetch_attachment(attachment_id)
    data, ctype = r.content, r.headers.get("content-type", "image/jpeg").split(";")[0]
    if ctype in ("image/heic", "image/heif"):  # iPhone photos; browsers can't show HEIC
        try:
            import io

            from PIL import Image, ImageOps
            from pillow_heif import register_heif_opener
            register_heif_opener()
            img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
            img.thumbnail((1600, 1600))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            data, ctype = buf.getvalue(), "image/jpeg"
        except Exception as e:
            log_event(None, "error", f"HEIC to JPEG failed for {attachment_id}: {e!r}")
    return Response(data, media_type=ctype, headers={"Cache-Control": "public, max-age=3600"})


@router.post("/dev/inbound")
def dev_inbound(msg: InboundText, bg: BackgroundTasks):
    """Simulate a text without Photon (see README)."""
    bg.add_task(handle_inbound, msg.phone, msg.text, msg.photo_url)
    return {"ok": True}
