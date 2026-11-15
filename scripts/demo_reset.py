"""Reset the demo between runs WITHOUT breaking texting. Owner: Person A.

Clears jobs, offers, messages and events (the owner board, the live phones page and every
trace start empty). Keeps techs and Photon contacts, so nobody has to re-text the line.
Then re-seeds B's demo past job so the "Welcome back" memory still shows.

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
kept = {"techs": len(db.list_techs()), "contacts": len(db.store.select("contacts"))}
print(f"[demo_reset] cleared {before} from {db.BACKEND}; kept {kept}")
print("[demo_reset] reload the owner board and /phones (or press Start over) before the next run")
