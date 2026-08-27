#!/usr/bin/env python
"""Inventory and clean the artefacts our agent leaves in the Grafana stack.

Every approved write-back run adds an annotation. Across a day of development that becomes
a stack of near-duplicates, and a judge looking at the farm timeline should see ONE
incident marker, not eight versions of the same sentence with different wording.

Nothing here is repair work: Grafana writes are single-object atomic, so a run killed
mid-flight leaves either a complete object or none. This is housekeeping, and it exists so
that "clean the stack before recording" is a command rather than a memory.

    python stack_hygiene.py                  # inventory only, changes nothing
    python stack_hygiene.py --prune-annotations --keep 1
    python stack_hygiene.py --prune-alert-rules --title-contains ECC
"""
import argparse
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

TAG = "second-unit"


def session():
    base = os.environ["GRAFANA_URL"].rstrip("/")
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {os.environ['GRAFANA_SERVICE_ACCOUNT_TOKEN']}"
    return base, s


def inventory(base, s):
    anns = s.get(f"{base}/api/annotations", params={"limit": 200, "tags": TAG},
                 timeout=25)
    anns = anns.json() if anns.ok else []
    rules = s.get(f"{base}/api/v1/provisioning/alert-rules", timeout=25)
    rules = rules.json() if rules.ok else []
    dash = s.get(f"{base}/api/search", params={"type": "dash-db", "limit": 200},
                 timeout=25)
    dash = [d for d in (dash.json() if dash.ok else [])
            if str(d.get("uid", "")).startswith("second-unit-")]

    print(f"annotations tagged '{TAG}': {len(anns)}")
    for a in anns:
        # The list endpoint has returned rows without an id; do not assume the shape.
        print(f"  id={a.get('id', '?')!s:<5} t={a.get('time','?')}  "
              f"{str(a.get('text',''))[:80]}")
    print(f"\nalert rules: {len(rules)}")
    for r in rules:
        print(f"  uid={r.get('uid')}  {r.get('title')}")
    print(f"\nour provisioned dashboards: {len(dash)}")
    for d in dash:
        print(f"  {d.get('uid')}  {d.get('title')}")
    return anns, rules, dash


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prune-annotations", action="store_true",
                    help=f"delete '{TAG}' annotations, keeping the newest --keep")
    ap.add_argument("--keep", type=int, default=1)
    ap.add_argument("--prune-alert-rules", action="store_true")
    ap.add_argument("--title-contains", default="",
                    help="only prune alert rules whose title contains this")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    base, s = session()
    anns, rules, _ = inventory(base, s)

    if not (args.prune_annotations or args.prune_alert_rules):
        print("\n(inventory only, pass --prune-annotations / --prune-alert-rules to change "
              "anything)")
        return

    doomed_anns, doomed_rules = [], []
    if args.prune_annotations:
        # Newest first, keep the first `keep`. Rows without an id cannot be deleted.
        ordered = sorted([a for a in anns if a.get("id") is not None],
                         key=lambda a: a.get("time", 0), reverse=True)
        doomed_anns = ordered[args.keep:]
    if args.prune_alert_rules:
        doomed_rules = [r for r in rules
                        if args.title_contains.lower() in str(r.get("title", "")).lower()]

    print(f"\nWILL DELETE {len(doomed_anns)} annotation(s) and "
          f"{len(doomed_rules)} alert rule(s):")
    for a in doomed_anns:
        print(f"  annotation id={a['id']}  {str(a.get('text',''))[:70]}")
    for r in doomed_rules:
        print(f"  rule {r.get('uid')}  {r.get('title')}")
    if not doomed_anns and not doomed_rules:
        print("  (nothing matched)")
        return
    if not args.yes:
        if input("\ntype 'delete' to proceed: ").strip() != "delete":
            sys.exit("aborted")

    for a in doomed_anns:
        r = s.delete(f"{base}/api/annotations/{a['id']}", timeout=25)
        print(f"  annotation {a['id']}: HTTP {r.status_code}")
    for rule in doomed_rules:
        r = s.delete(f"{base}/api/v1/provisioning/alert-rules/{rule['uid']}", timeout=25)
        print(f"  rule {rule['uid']}: HTTP {r.status_code}")


if __name__ == "__main__":
    main()
