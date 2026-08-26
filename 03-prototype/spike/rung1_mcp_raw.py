"""Rung 1: raw MCP over streamable HTTP. No ADK, no Gemini, no LLM.

Purpose: prove that auth + network + the Grafana MCP server work, in isolation.
If this fails, the problem is credentials or the bridge — not your agent code.
If this passes and Rung 2 fails, the problem is ADK. That separation is the whole
point of running these separately.

Prints every tool the server exposes, which is also your menu for designing the
real agent later.
"""
import asyncio
import json
import os

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

load_dotenv()

URL = os.environ["MCP_GRAFANA_URL"]
HEADERS = {"Authorization": f"Bearer {os.environ['MCP_GRAFANA_SERVER_TOKEN']}"}

# Read-only tools, in order of preference, that should need no arguments.
PREFERRED = ["list_datasources", "list_teams", "list_alert_rules", "list_incidents"]


def zero_arg_tools(tools):
    out = []
    for t in tools:
        schema = t.inputSchema or {}
        if not schema.get("required"):
            out.append(t.name)
    return out


async def main():
    print(f"\n=== Rung 1: raw MCP -> {URL} ===\n")

    # Timeouts are non-negotiable: adk-python#2615 is an indefinite hang against
    # remote streamable-HTTP MCP servers. Same class of bug bites the raw client.
    async with streamablehttp_client(URL, headers=HEADERS, timeout=30) as (read, write, _):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=30)
            print("handshake: ok")

            listed = await asyncio.wait_for(session.list_tools(), timeout=30)
            tools = listed.tools
            print(f"tools exposed: {len(tools)}\n")

            by_prefix = {}
            for t in tools:
                by_prefix.setdefault(t.name.split("_")[0], []).append(t.name)
            for prefix in sorted(by_prefix):
                print(f"  {prefix:<14} {', '.join(sorted(by_prefix[prefix]))}")

            candidates = zero_arg_tools(tools)
            names = {t.name for t in tools}
            target = next((p for p in PREFERRED if p in names), None) or (
                candidates[0] if candidates else None
            )
            if not target:
                print("\n!! No zero-argument tool to probe. Pick one manually and pass args.")
                return

            print(f"\ncalling: {target}()")
            result = await asyncio.wait_for(session.call_tool(target, {}), timeout=60)

            if result.isError:
                print(f"!! tool returned an error: {result.content}")
                return

            for block in result.content:
                text = getattr(block, "text", None)
                if text is None:
                    print(f"  <{type(block).__name__}>")
                    continue
                try:
                    print("  " + json.dumps(json.loads(text), indent=2)[:1500])
                except (ValueError, TypeError):
                    print("  " + text[:1500])

            print(f"\nRUNG 1 GREEN — {len(tools)} tools reachable, one call returned data.")
            print("Record the tool count in NOTES.md. Now run rung2_adk_agent.py")


if __name__ == "__main__":
    asyncio.run(main())
