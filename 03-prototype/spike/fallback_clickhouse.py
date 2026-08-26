"""INSURANCE ONLY. Do not run this unless Rung 1 and Rung 2 both failed.

The ClickHouse track's official MCP server authenticates with plain host/user/password
— no OAuth — so this is the fastest pivot if Grafana auth defeats you. Sign up for the
30-day ClickHouse Cloud trial, fill in the CLICKHOUSE_* vars in .env, run this, and if
it prints tables you have a viable track by Sunday morning.

There is NO official Docker image (checked 2026-08-22: `clickhouse/mcp-clickhouse` is not
on Docker Hub). It ships as the PyPI package `mcp-clickhouse`. Run the server first, in a
separate terminal:

  ./.venv/bin/pip install mcp-clickhouse
  set -a; . ./.env; set +a
  CLICKHOUSE_MCP_SERVER_TRANSPORT=http \
  CLICKHOUSE_MCP_BIND_HOST=127.0.0.1 \
  CLICKHOUSE_MCP_BIND_PORT=8001 \
  CLICKHOUSE_MCP_AUTH_DISABLED=true \
    ./.venv/bin/mcp-clickhouse

Verify those transport env var names against the repo README before trusting them —
https://github.com/ClickHouse/mcp-clickhouse — they were not confirmed first-hand.
`AUTH_DISABLED` is safe here only because we bind to localhost.
"""
import asyncio
import os

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

load_dotenv()

URL = os.getenv("CLICKHOUSE_MCP_URL", "http://localhost:8001/mcp")


async def main():
    print(f"\n=== FALLBACK: ClickHouse MCP -> {URL} ===\n")
    async with streamablehttp_client(URL, timeout=30) as (r, w, _):
        async with ClientSession(r, w) as session:
            await asyncio.wait_for(session.initialize(), timeout=30)
            listed = await asyncio.wait_for(session.list_tools(), timeout=30)
            print(f"tools: {[t.name for t in listed.tools]}")
            res = await asyncio.wait_for(session.call_tool("list_databases", {}), timeout=60)
            for block in res.content:
                print("  " + (getattr(block, "text", "") or "")[:800])
            print("\nFALLBACK GREEN — ClickHouse track is viable. Re-scope the concept.")


if __name__ == "__main__":
    asyncio.run(main())
