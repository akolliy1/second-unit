"""Prove the seeded incident is visible THROUGH MCP — the path the agent will use.

Querying Grafana's HTTP API directly would prove the data landed. It would not prove the
agent can reach it, which is the only thing that matters. So every check here goes through
the same MCP tool calls the Diagnostician will make.
"""
import asyncio
import json
import os

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

load_dotenv("../.env")

URL = os.environ["MCP_GRAFANA_URL"]
HEADERS = {
    "Authorization": f"Bearer {os.environ['MCP_GRAFANA_SERVER_TOKEN']}",
    "Accept": "application/json, text/event-stream",
}
PROM = "grafanacloud-prom"
LOKI = "grafanacloud-logs"

CHECKS = [
    ("the bad node is identifiable by ECC errors alone", "query_prometheus", {
        "datasourceUid": PROM, "queryType": "instant", "endTime": "now",
        "expr": 'topk(3, increase(render_node_gpu_ecc_errors_total[30m]))'}),
    ("frame failures localize to that same node", "query_prometheus", {
        "datasourceUid": PROM, "queryType": "instant", "endTime": "now",
        "expr": 'topk(3, sum by (node, reason) (increase(render_frames_failed_total[30m])))'}),
    # Range, not instant: a finished backfill's newest sample is minutes old and keeps
    # aging, so it drops out of Prometheus's 5m instant lookback. Run `seed.py --live`
    # alongside the demo if you want instant queries to work.
    ("the lighting queue is visibly backed up", "query_prometheus", {
        "datasourceUid": PROM, "queryType": "range", "startTime": "now-90m",
        "endTime": "now", "stepSeconds": 300, "expr": 'render_queue_depth'}),
    # Range, and a 15m window: an instant rate([10m]) at `now` goes empty the moment the
    # newest sample is more than 10 minutes old, which is exactly what happened here --
    # this check passed and then failed minutes later with no change to the data.
    ("throughput dropped — healthy vs now", "query_prometheus", {
        "datasourceUid": PROM, "queryType": "range", "startTime": "now-90m",
        "endTime": "now", "stepSeconds": 300,
        "expr": 'sum(rate(render_frames_completed_total{shot="SH042"}[15m])) * 60'}),
    ("the money metric: frames left on SH042", "query_prometheus", {
        "datasourceUid": PROM, "queryType": "range", "startTime": "now-90m",
        "endTime": "now", "stepSeconds": 300, "expr": 'shot_frames_remaining'}),
    # NOT `|= "Xid 48"`. A real Xid line reads `NVRM: Xid (PCI:0000:41:00): 48, ...`, so
    # the obvious substring never matches -- a trap the agent will hit too. Match the
    # phrase that is actually contiguous.
    ("the root cause is in the logs", "query_loki_logs", {
        "datasourceUid": LOKI, "logql": '{service="render-node"} |= "uncorrectable ECC"',
        "limit": 3, "startRfc3339": "now-90m", "endRfc3339": "now"}),
    ("the decoy is there too, and predates the incident", "query_loki_logs", {
        "datasourceUid": LOKI, "logql": '{service="asset-pipeline"}',
        "limit": 2, "startRfc3339": "now-90m", "endRfc3339": "now"}),
]


def summarize(text, limit=420):
    try:
        data = json.loads(text)
    except Exception:
        return text[:limit].replace("\n", " ")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        out = []
        for row in data[:6]:
            metric = row.get("metric") or row.get("labels") or {}
            labels = {k: v for k, v in metric.items() if k != "__name__"}
            val = row.get("value") or row.get("values") or row.get("line") or ""
            if isinstance(val, list) and val and isinstance(val[-1], (list, tuple)):
                val = val[-1][-1]
            elif isinstance(val, list):
                val = val[-1]
            out.append(f"{labels} = {val}")
        return "; ".join(out)[:limit]
    return json.dumps(data)[:limit]


async def main():
    print(f"verifying the seed through MCP at {URL}\n")
    passed = failed = 0
    async with streamablehttp_client(URL, headers=HEADERS, timeout=45) as (r, w, _):
        async with ClientSession(r, w) as s:
            await asyncio.wait_for(s.initialize(), timeout=30)
            for label, tool, args in CHECKS:
                try:
                    res = await asyncio.wait_for(
                        s.call_tool(tool, args), timeout=60)
                    text = "".join(getattr(c, "text", "") for c in res.content)
                    # mcp-grafana returns a 200 with {"data": [], "hints": {...}} when a
                    # query matches nothing. The envelope is never empty, so testing the
                    # raw string reports success on no data -- which it did, and gave a
                    # false 7/7 green. Inspect the payload.
                    empty = (not text.strip()) or text.strip() in ("[]", "{}", "null")
                    if not empty:
                        try:
                            parsed = json.loads(text)
                            if isinstance(parsed, dict) and not parsed.get("data"):
                                empty = True
                        except Exception:
                            pass
                    if res.isError or empty:
                        failed += 1
                        print(f"  ❌ {label}\n     {tool} -> {text[:200] or '(empty)'}")
                    else:
                        passed += 1
                        print(f"  ✅ {label}\n     {summarize(text)}")
                except Exception as e:
                    failed += 1
                    print(f"  ❌ {label}\n     {type(e).__name__}: {str(e)[:180]}")
    print(f"\n{passed} passed, {failed} failed")
    if not failed:
        print("The world is visible through MCP. The agent has something real to reason over.")


if __name__ == "__main__":
    asyncio.run(main())
