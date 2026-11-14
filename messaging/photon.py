"""Photon API client + inbound event parsing. Owner: Person A.

Facts (from https://api.photon.codes/openapi.json):
- Send: POST /v1/projects/{id}/messages {spaceId, senderId, content}, idempotency-key header.
  spaceId/senderId come from the person's inbound message (space.id, receivedBy.id),
  so we save them per phone in the `contacts` table.
- A shared line can only message people who were assigned and texted it first.
- One message = one content item: a photo and its caption arrive as two messages.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import httpx

from shared import db

API = "https://api.photon.codes/v1"
MERGE_SECONDS = float(os.getenv("PHOTON_MERGE_SECONDS", "4"))


def _cfg() -> tuple[str, str]:
    return os.getenv("PHOTON_API_KEY", ""), os.getenv("PHOTON_PROJECT_ID", "")


def configured() -> bool:
    key, pid = _cfg()
    return bool(key and pid)


def _headers() -> dict:
    return {"Authorization": f"Bearer {_cfg()[0]}"}


# ---------- contacts: phone -> (space_id, line_user_id) ----------

def save_contact(phone: str, space_id: str, line_user_id: str) -> None:
    row = {"space_id": space_id, "line_user_id": line_user_id,
           "updated_at": datetime.now(timezone.utc).isoformat()}
    if db.store.select("contacts", id=phone):
        db.store.update("contacts", phone, row)
    else:
        db.store.insert("contacts", {"id": phone, **row})


def get_contact(phone: str) -> Optional[dict]:
    rows = db.store.select("contacts", id=phone)
    return rows[0] if rows else None


# ---------- outbound ----------

def send(phone: str, text: str) -> None:
    """Raises on failure; messaging.send.send_text catches and logs."""
    c = get_contact(phone)
    if not c:
        raise RuntimeError(f"{phone} hasn't texted the line yet (no Photon space)")
    r = httpx.post(
        f"{API}/projects/{_cfg()[1]}/messages",
        headers={**_headers(), "idempotency-key": str(uuid4())},
        json={"spaceId": c["space_id"], "senderId": c["line_user_id"],
              "content": {"type": "text", "format": "plain", "text": text}},
        timeout=5,
    )
    r.raise_for_status()


_lines: dict = {"at": 0.0, "map": {}}


def line_numbers() -> dict[str, str]:
    """{person's phone: the Photon number they text}, cached 5 minutes. {} on any error."""
    import time
    if not configured():
        return {}
    if time.time() - _lines["at"] < 300:
        return _lines["map"]
    try:
        r = httpx.get(f"{API}/projects/{_cfg()[1]}/platforms/imessage/assignments",
                      headers=_headers(), timeout=5)
        r.raise_for_status()
        body = r.json()
        items = body.get("assignments") or body.get("data") or []
        _lines["map"] = {a["handle"]: a.get("assignedPhoneNumber") for a in items if a.get("handle")}
    except Exception:
        pass
    _lines["at"] = time.time()
    return _lines["map"]


def fetch_attachment(attachment_id: str) -> httpx.Response:
    r = httpx.get(f"{API}/projects/{_cfg()[1]}/attachments/{attachment_id}/content",
                  headers=_headers(), timeout=15, follow_redirects=True)
    r.raise_for_status()
    return r


# ---------- inbound ----------

def parse_event(event: dict) -> Optional[dict]:
    """message.received -> {phone, space_id, line_user_id, text, attachment_id}; else None."""
    if event.get("type") != "message.received":
        return None
    msg = (event.get("data") or {}).get("object") or {}
    sender = msg.get("sentBy") or {}
    content = msg.get("content") or {}
    phone = sender.get("handle")
    if not phone:
        return None
    return {
        "phone": phone,
        "space_id": (msg.get("space") or {}).get("id"),
        "line_user_id": (msg.get("receivedBy") or {}).get("id"),
        "text": content.get("text", "") if content.get("type") == "text" else "",
        "attachment_id": content.get("attachmentId") if content.get("type") == "attachment" else None,
    }


class MergeBuffer:
    """Photo and caption arrive as separate messages: collect per sender, flush after a pause."""

    def __init__(self, on_flush, delay: float = MERGE_SECONDS) -> None:
        self.on_flush, self.delay = on_flush, delay
        self.pending: dict[str, dict] = {}
        self.lock = threading.Lock()

    def add(self, phone: str, text: str, photo_url: Optional[str]) -> None:
        with self.lock:
            p = self.pending.get(phone)
            if p:
                p["timer"].cancel()
            else:
                p = self.pending[phone] = {"texts": [], "photo_url": None}
            if text:
                p["texts"].append(text)
            p["photo_url"] = p["photo_url"] or photo_url
            p["timer"] = threading.Timer(self.delay, self._flush, args=(phone,))
            p["timer"].daemon = True
            p["timer"].start()

    def _flush(self, phone: str) -> None:
        with self.lock:
            p = self.pending.pop(phone, None)
        if p:
            self.on_flush(phone, " ".join(p["texts"]), p["photo_url"])
