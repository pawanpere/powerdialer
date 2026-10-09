"""Make a PXL Kraft trial invite link for a prospect, as the platform owner.

Run from the platform repo so its settings and database are used:
    cd ~/pxlkraft-ppap && uv run python ~/.claude/skills/process-calls/make_trial_link.py \
        --email dale@shop.com --company "Harlan Precision" [--limit 2] [--dry-run]

Prints JSON: {"company": ..., "limit": 2, "links": [{"email": ..., "link": ...}]}.
Creating the link sends nothing; the link goes into the email draft.
The links use the platform's app URL, or PXL_PUBLIC_URL when that is set
(e.g. a tunnel or the deployed address), since the invite token works on any
address that serves this platform.
--dry-run makes the link inside a transaction and rolls it back (for testing).
"""
import argparse
import json
import os
import sys

from pxlkraft.core import trials
from pxlkraft.core.db import sessions
from pxlkraft.core.models import User
from pxlkraft.core.owner import is_owner
from pxlkraft.core.settings import settings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True, action="append")
    ap.add_argument("--company", default="")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    session = sessions()()
    try:
        owner = next((u for u in session.query(User).filter(User.role == "admin") if is_owner(session, u)), None)
        if owner is None:
            sys.exit(json.dumps({"error": "no platform owner found (PXL_OWNER_EMAILS)"}))
        out = trials.create(session, owner, emails=a.email, company_name=a.company, limit=a.limit)
        base = (os.environ.get("PXL_PUBLIC_URL") or settings().app_url).rstrip("/")
        for item in out.get("links", []):
            item["link"] = base + "/invite/" + item["link"].rsplit("/invite/", 1)[-1]
        out["app_url"] = base
        out["reachable_by_prospects"] = not base.startswith(("http://localhost", "http://127.0.0.1"))
        if a.dry_run:
            session.rollback()
            out["dry_run"] = True
        else:
            session.commit()
        print(json.dumps(out, indent=1))
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
