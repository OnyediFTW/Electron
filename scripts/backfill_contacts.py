"""Rebuild the contacts table (phone -> Photon space + line user) from Photon's
message history, so people don't have to text the line again after a reset.

Run: python -m scripts.backfill_contacts
"""
import httpx

from messaging import photon


def main() -> None:
    key, pid = photon._cfg()
    url = f"{photon.API}/projects/{pid}/messages"
    saved, cursor = {}, None
    for _ in range(10):  # up to 10 pages
        params = {"limit": 100, **({"cursor": cursor} if cursor else {})}
        r = httpx.get(url, headers={"Authorization": f"Bearer {key}"}, params=params, timeout=15)
        r.raise_for_status()
        body = r.json()
        items = body.get("messages") or body.get("data") or body.get("items") or []
        for m in items:
            if m.get("status") != "received":
                continue
            phone = (m.get("sentBy") or {}).get("handle")
            space = (m.get("space") or {}).get("id")
            line = (m.get("receivedBy") or {}).get("id")
            if phone and space and line and phone not in saved:  # newest first wins
                photon.save_contact(phone, space, line)
                saved[phone] = space
        cursor = body.get("nextCursor") or (body.get("pagination") or {}).get("nextCursor")
        if not cursor or not items:
            break
    print(f"saved {len(saved)} contacts: {', '.join(saved) or '(none)'}")


if __name__ == "__main__":
    main()
