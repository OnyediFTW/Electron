"""Run intake -> find_parts -> quote on every labeled test photo. No Photon, no server.

    python -m scripts.test_agents                  # photos in test_photos/ (skips missing files)
    python -m scripts.test_agents --text-only      # also run labels whose photo is missing, text only
    python -m scripts.test_agents --offline        # blank all keys: exercises the FALLBACK paths
"""
import argparse
import json
import os
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--offline", action="store_true", help="ignore API keys, use fallbacks only")
ap.add_argument("--text-only", action="store_true", help="run labels with missing photos using text alone")
ap.add_argument("--no-parts", action="store_true", help="skip find_parts/quote (faster intake-only runs)")
args = ap.parse_args()

os.environ["SUPABASE_URL"] = ""  # never write test jobs to the real database
if args.offline:
    for k in ("ANTHROPIC_API_KEY", "EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "SERPAPI_KEY"):
        os.environ[k] = ""  # set before shared.db runs load_dotenv(), which won't override

from agents import find_parts, intake, quote  # noqa: E402

PHOTOS = Path(__file__).resolve().parent.parent / "test_photos"


def check(label: dict, job) -> bool:
    if label.get("expect_follow_up"):
        trade_ok = label["expected_trade"] is None or job.trade == label["expected_trade"]
        return bool(job.follow_up_question) and trade_ok
    urgency = job.urgency.value if job.urgency else None
    return (job.trade == label["expected_trade"] and urgency == label["expected_urgency"]
            and not job.follow_up_question)


def main() -> None:
    labels = json.loads((PHOTOS / "labels.json").read_text())
    rows, latencies = [], []
    for label in labels:
        path = PHOTOS / label["file"]
        if not path.exists() and not args.text_only:
            print(f"skip {label['file']} (no image; add it or pass --text-only)")
            continue
        photo = str(path) if path.exists() else None

        t0 = time.monotonic()
        job = intake(photo, label["text"], customer_phone="+10000000000")
        latencies.append(time.monotonic() - t0)

        parts_txt, quote_txt = "-", "-"
        if not args.no_parts and not job.follow_up_question:
            parts = find_parts(job)
            q = quote(job, parts)
            parts_txt = f"{sum(p.price is not None for p in parts)}/{len(parts)}"
            quote_txt = f"${q.low:.0f}-{q.high:.0f}"

        kw = label.get("issue_keywords") or []
        issue_ok = not kw or any(k in (job.issue or "").lower() for k in kw)
        rows.append({
            "file": label["file"] + ("" if photo else " (text)"),
            "exp": f"{label['expected_trade']}/{label['expected_urgency']}",
            "got": f"{job.trade}/{job.urgency.value if job.urgency else None}",
            "ok": check(label, job),
            "issue": (job.issue or "") + ("" if issue_ok else "  [issue?]"),
            "follow_up": job.follow_up_question or "",
            "secs": latencies[-1],
            "parts": parts_txt,
            "quote": quote_txt,
        })

    if not rows:
        print("nothing ran")
        return
    print()
    print(f"{'':2}{'file':28}{'expected':24}{'actual':24}{'secs':>6}  {'parts':6}{'quote':12}issue / follow-up")
    for r in rows:
        mark = "✓" if r["ok"] else "✗"
        extra = r["issue"] + (f"  -> asks: {r['follow_up']}" if r["follow_up"] else "")
        print(f"{mark} {r['file']:28}{r['exp']:24}{r['got']:24}{r['secs']:6.1f}  {r['parts']:6}{r['quote']:12}{extra}")
    correct = sum(r["ok"] for r in rows)
    print(f"\ntrade+urgency correct: {correct}/{len(rows)}   "
          f"avg intake {sum(latencies) / len(latencies):.1f}s, max {max(latencies):.1f}s")


if __name__ == "__main__":
    main()
