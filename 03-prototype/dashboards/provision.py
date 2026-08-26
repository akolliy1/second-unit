#!/usr/bin/env python3
"""Push the baseline Second Unit dashboards into Grafana Cloud.

Idempotent: each dashboard JSON carries a stable `uid`, and we POST with
`overwrite: true`, so re-running updates the existing dashboard in place
rather than creating a second copy of it.

    ../spike/.venv/bin/python provision.py            # push both
    ../spike/.venv/bin/python provision.py --verify   # push, then read back
                                                      # and run every panel query

Credentials come from ../.env (GRAFANA_URL, GRAFANA_SERVICE_ACCOUNT_TOKEN).
The token needs dashboards:write. It is never written to stdout or to disk.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
DASHBOARDS = ["farm-overview.json", "delivery-status.json"]
PROM_UID = "grafanacloud-prom"
LOKI_UID = "grafanacloud-logs"

load_dotenv(HERE.parent / ".env")
BASE = os.environ["GRAFANA_URL"].rstrip("/")
TOKEN = os.environ["GRAFANA_SERVICE_ACCOUNT_TOKEN"]


def request(path: str, method: str = "GET", body: dict | None = None,
            params: dict | None = None, timeout: int = 90):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/json")
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, payload, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:600]
        try:
            return exc.code, json.loads(detail)
        except ValueError:
            return exc.code, {"error": detail}


# --------------------------------------------------------------------------- push

def push(filename: str) -> tuple[bool, str]:
    spec = json.loads((HERE / filename).read_text())
    # version 0 + overwrite lets Grafana take our copy as authoritative without
    # having to fetch and match the currently stored version number first.
    spec["version"] = 0
    status, resp = request("/api/dashboards/db", "POST", {
        "dashboard": spec,
        "overwrite": True,
        "message": f"Second Unit baseline: provisioned from {filename}",
    })
    if status != 200:
        print(f"  FAIL  {filename}: HTTP {status} {resp}")
        return False, ""
    url = BASE + resp["url"]
    print(f"  200   {filename}  uid={resp['uid']}  v{resp['version']}")
    print(f"        {url}")
    return True, url


# ------------------------------------------------------------------------- verify

def prom_range(expr: str, minutes: int = 90, step: int = 60):
    now = int(time.time())
    return request(f"/api/datasources/proxy/uid/{PROM_UID}/api/v1/query_range",
                   params={"query": expr, "start": now - minutes * 60,
                           "end": now, "step": step})


def loki_range(expr: str, minutes: int = 90, limit: int = 20):
    now_ns = int(time.time() * 1e9)
    return request(f"/api/datasources/proxy/uid/{LOKI_UID}/loki/api/v1/query_range",
                   params={"query": expr, "start": now_ns - minutes * 60 * 10**9,
                           "end": now_ns, "limit": limit, "direction": "backward"})


def datapoints(resp: dict) -> tuple[int, int]:
    """(series, datapoints) for either a Prometheus or a Loki range response."""
    if not isinstance(resp, dict) or resp.get("status") != "success":
        return 0, 0
    result = resp.get("data", {}).get("result", [])
    points = 0
    for series in result:
        points += len(series.get("values", []) or ([series["value"]] if "value" in series else []))
    return len(result), points


def verify(filename: str) -> list[str]:
    """Read the dashboard back, then run every panel query. Returns failures."""
    spec = json.loads((HERE / filename).read_text())
    uid = spec["uid"]
    failures: list[str] = []

    status, resp = request(f"/api/dashboards/uid/{uid}")
    if status != 200:
        failures.append(f"{filename}: GET /api/dashboards/uid/{uid} -> HTTP {status}")
        print(f"  FAIL  read-back HTTP {status}")
        return failures
    stored = resp["dashboard"]
    print(f"  200   read back {uid!r}: {stored['title']!r}, "
          f"{len(stored.get('panels', []))} panels, v{stored['version']}")

    for panel in stored.get("panels", []):
        for tgt in panel.get("targets", []):
            expr = tgt.get("expr")
            if not expr:
                continue
            is_loki = (tgt.get("datasource") or {}).get("type") == "loki"
            _, resp = (loki_range(expr) if is_loki else prom_range(expr))
            series, points = datapoints(resp)
            label = f"[{panel['id']:>2}] {panel['title']}"
            if points == 0:
                failures.append(f"{filename}: panel {panel['id']!r} "
                                f"({panel['title']}) returned no data for: {expr}")
                print(f"  EMPTY {label}")
            else:
                kind = "lines" if is_loki else "pts"
                print(f"  ok    {label}  ({series} series, {points} {kind})")
    return failures


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verify", action="store_true",
                    help="after pushing, read each dashboard back and run every panel query")
    args = ap.parse_args()

    print(f"Grafana: {BASE}")
    urls, ok = [], True
    for filename in DASHBOARDS:
        pushed, url = push(filename)
        ok &= pushed
        if url:
            urls.append(url)

    failures: list[str] = []
    if args.verify and ok:
        for filename in DASHBOARDS:
            print(f"\nverifying {filename}")
            failures += verify(filename)

    print("\n" + "=" * 72)
    for url in urls:
        print(url)
    if failures:
        print(f"\n{len(failures)} problem(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nall dashboards provisioned" + (" and every panel returns data" if args.verify else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
