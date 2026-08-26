"""Which MCP endpoint will actually talk to us, headless?

Architecture 0.1 says try hosted Cloud MCP before the OSS bridge. Grafana ships more than
one plausible surface, and the docs lag the product, so probe them all in one pass and let
the transcript decide rather than arguing from first principles.

Every await is bounded: adk-python #2615 is an indefinite hang against remote streamable
HTTP MCP servers, and an unbounded probe would just look like a network problem.
"""
import asyncio
import os

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

load_dotenv("../.env")

STACK = os.environ["GRAFANA_URL"].rstrip("/")
SA = os.environ["GRAFANA_SERVICE_ACCOUNT_TOKEN"]

CANDIDATES = [
    ("hosted + X-Grafana-URL", "https://mcp.grafana.com/mcp",
     {"Authorization": f"Bearer {SA}", "X-Grafana-URL": STACK}),
    ("hosted, bearer only", "https://mcp.grafana.com/mcp",
     {"Authorization": f"Bearer {SA}"}),
    ("hosted + X-Access-Token", "https://mcp.grafana.com/mcp",
     {"X-Access-Token": SA, "X-Grafana-URL": STACK}),
    ("stack /api/mcp", f"{STACK}/api/mcp",
     {"Authorization": f"Bearer {SA}"}),
    ("stack /mcp", f"{STACK}/mcp",
     {"Authorization": f"Bearer {SA}"}),
]


async def probe(label, url, headers):
    headers = {**headers, "Accept": "application/json, text/event-stream"}
    try:
        async with streamablehttp_client(url, headers=headers, timeout=20) as (r, w, _):
            async with ClientSession(r, w) as session:
                await asyncio.wait_for(session.initialize(), timeout=25)
                listed = await asyncio.wait_for(session.list_tools(), timeout=25)
                names = [t.name for t in listed.tools]
                print(f"\n  ✅ {label}\n     {url}\n     {len(names)} tools")
                return label, url, headers, names
    except Exception as exc:
        msg = str(exc).replace("\n", " ")[:150]
        print(f"  ❌ {label:26} {type(exc).__name__}: {msg}")
        return None


async def main():
    print(f"stack: {STACK}\nprobing {len(CANDIDATES)} MCP surfaces, 25s cap each\n")
    winners = []
    for label, url, headers in CANDIDATES:
        got = await probe(label, url, headers)
        if got:
            winners.append(got)

    print("\n" + "=" * 70)
    if not winners:
        print("NO HOSTED SURFACE ACCEPTED A STATIC TOKEN.")
        print("Fall back to the OSS bridge: ./start_grafana_mcp.sh, then")
        print("  python rung2_adk_agent.py --target bridge")
        return
    label, url, headers, names = winners[0]
    print(f"USE THIS: {label}\n  url={url}\n  headers={list(headers)}")
    print(f"\n{len(names)} tools exposed:")
    for n in sorted(names):
        print(f"  {n}")


if __name__ == "__main__":
    asyncio.run(main())
