"""Independent verification of the write-back.

Why this file exists. The executor reported "Annotation created with ID 1" (true), and
also made four `update_dashboard` calls that created no dashboard at all. Reading its own
report, you would conclude three writes landed. One did.

An agent's self-report is a claim, not a result. So after every approved write-back we
check the stack **out of band**, through Grafana's HTTP API rather than the MCP tools the
agent just used, and report what is actually there. Same principle as the rest of the
system: the model decides what to do, code decides what is true.
"""
import os
from typing import Dict, List

import requests


def _session():
    base = os.environ["GRAFANA_URL"].rstrip("/")
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {os.environ['GRAFANA_SERVICE_ACCOUNT_TOKEN']}"
    return base, s


def _get(base, s, path, **params):
    try:
        r = s.get(f"{base}{path}", params=params or None, timeout=25)
        return r.json() if r.ok else None
    except Exception:  # noqa: BLE001
        return None


def snapshot(tag: str = "second-unit") -> Dict[str, set]:
    """Record what exists BEFORE the write-back, so verification can diff.

    The first version of this module matched dashboards by name, and "confirmed" two
    writes that never happened because Grafana Cloud ships a built-in dashboard called
    "Incident Insights" and the heuristic matched the word "incident". A verifier with a
    false-positive mode is worse than no verifier -- it launders the agent's claims.

    A before/after diff has no such mode: either a new object id appeared or it did not.
    """
    base, s = _session()
    dash = _get(base, s, "/api/search", type="dash-db", limit=200) or []
    anns = _get(base, s, "/api/annotations", limit=100, tags=tag) or []
    rules = _get(base, s, "/api/v1/provisioning/alert-rules") or []
    return {
        "dashboard_uids": {d.get("uid") for d in dash},
        "annotation_ids": {a.get("id") for a in anns},
        "rule_uids": {r.get("uid") or r.get("title") for r in rules},
    }


def verify_writes(claimed: List[Dict], before: Dict[str, set],
                  *, tag: str = "second-unit") -> List[Dict]:
    """Diff the stack against `before` and attribute new objects to the claims.

    `claimed` is the executor's own WriteResult list (as dicts). `verified` is True only
    when a NEW object of the right kind appeared since the snapshot.
    """
    base, s = _session()
    out = []

    dash = _get(base, s, "/api/search", type="dash-db", limit=200) or []
    anns = _get(base, s, "/api/annotations", limit=100, tags=tag) or []
    rules = _get(base, s, "/api/v1/provisioning/alert-rules") or []

    new_dash = [d for d in dash if d.get("uid") not in before["dashboard_uids"]]
    new_anns = [a for a in anns if a.get("id") not in before["annotation_ids"]]
    new_rules = [r for r in rules
                 if (r.get("uid") or r.get("title")) not in before["rule_uids"]]
    # Each new object may only be credited to one claim.
    pools = {"annotation": list(new_anns), "dashboard": list(new_dash),
             "alert": list(new_rules)}

    for c in claimed:
        action = (c.get("action") or "").lower()
        row = {"action": action, "claimed_success": bool(c.get("succeeded")),
               "detail": c.get("detail", ""), "verified": None, "evidence": ""}

        key = ("annotation" if "annotation" in action
               else "dashboard" if "dashboard" in action
               else "alert" if "alert" in action else None)

        if key is None:
            row["evidence"] = "no independent check implemented for this action type"
        elif pools[key]:
            obj = pools[key].pop(0)          # credit one new object to this claim
            row["verified"] = True
            ident = obj.get("uid") or obj.get("id") or obj.get("title")
            label = obj.get("title") or obj.get("text", "")[:60] or ""
            row["evidence"] = f"NEW {key}: {label} ({ident})".strip()
        else:
            row["verified"] = False
            row["evidence"] = f"no new {key} appeared in the stack since the snapshot"

        out.append(row)
    return out


def print_report(rows: List[Dict]) -> Dict[str, int]:
    """Human-readable, and blunt about disagreement."""
    tally = {"confirmed": 0, "false_success": 0, "honest_failure": 0, "unchecked": 0}
    print("\n   --- independent verification (Grafana HTTP API, not the agent) ---")
    for r in rows:
        if r["verified"] is None:
            mark, key = "?", "unchecked"
        elif r["verified"] and r["claimed_success"]:
            mark, key = "✓", "confirmed"
        elif r["claimed_success"] and not r["verified"]:
            mark, key = "‼", "false_success"
        else:
            mark, key = "✗", "honest_failure"
        tally[key] += 1
        print(f"   {mark} {r['action']:12} claimed={r['claimed_success']!s:5} "
              f"verified={r['verified']!s:5}  {r['evidence'][:90]}")
    if tally["false_success"]:
        print(f"   ‼ {tally['false_success']} write(s) were REPORTED as successful but "
              f"are not present in the stack.")
    return tally


def stack_targets() -> Dict[str, object]:
    """The concrete identifiers the executor needs, resolved in Python.

    The executor kept failing on missing parameters -- `folder_uid` for an alert rule, then
    `dashboardUid` for an annotation -- because the plan it receives describes intent in
    prose ("mark the incident on the farm timeline") and it has to guess the identifiers.
    That is a free-text handoff, which is precisely what the rest of this pipeline refuses
    to do. So we look the identifiers up and hand them over as facts.
    """
    base, s = _session()
    folders = _get(base, s, "/api/folders") or []
    dash = _get(base, s, "/api/search", type="dash-db", limit=200) or []
    ours = [d for d in dash if str(d.get("uid", "")).startswith("second-unit-")]
    return {
        "folder_uid": (folders[0]["uid"] if folders else None),
        "folder_title": (folders[0]["title"] if folders else None),
        "dashboards": [{"uid": d["uid"], "title": d["title"]} for d in ours],
    }
