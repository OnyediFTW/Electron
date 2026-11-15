"""Reset the demo between runs WITHOUT breaking texting. Owner: Person A.

Clears jobs, offers, messages and events (the owner board, the live phones page and every
trace start empty), plus the "Electron job:" bookings on techs' Google Calendars. Keeps techs and
Photon contacts, so nobody has to re-text the line.
Then re-seeds B's demo past job so the technician's offer still shows the repeat-customer history.

Run:  python -m scripts.demo_reset --yes
Then reload the board and the /phones page (or press Start over there).
"""
import sys

from shared import db
from shared.seed import seed

if "--yes" not in sys.argv:
    sys.exit("This clears every job, offer, message and event in the "
             f"{db.BACKEND} database (techs and phone connections are kept).\n"
             "Run again with --yes to do it:  python -m scripts.demo_reset --yes")

before = {t: len(db.store.select(t)) for t in ("jobs", "offers", "messages", "events")}
for table in ("events", "offers", "messages", "jobs"):  # children before parents
    db.store.delete_all(table)
seed()  # refreshes techs in place and re-adds the demo customer's past job


def _clear_calendar_bookings() -> int:
    """Delete the "Electron job:" events this app booked on techs' real Google Calendars, so the next
    run starts with open slots. Other events are left alone. Skipped when Google isn't set up."""
    from matching import calendar as cal
    n = 0
    for t in db.list_techs():
        cal_id = cal._calendar_id(t.id)
        if not cal_id:
            continue
        try:
            svc = cal._svc()
            items = svc.events().list(calendarId=cal_id, q="Electron job").execute().get("items", [])
            for ev in items:
                if (ev.get("summary") or "").startswith("Electron job:"):
                    svc.events().delete(calendarId=cal_id, eventId=ev["id"]).execute()
                    n += 1
        except Exception as e:
            print(f"[demo_reset] couldn't clear {t.name}'s calendar ({type(e).__name__}); skipping")
    return n


print(f"[demo_reset] removed {_clear_calendar_bookings()} Electron booking(s) from tech calendars")
kept = {"techs": len(db.list_techs()), "contacts": len(db.store.select("contacts"))}
print(f"[demo_reset] cleared {before} from {db.BACKEND}; kept {kept}")
print("[demo_reset] reload the owner board and /phones (or press Start over) before the next run")
