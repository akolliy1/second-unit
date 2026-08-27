"""Rung 2: the actual Day-1 goal.

One Gemini-powered ADK agent that calls a Grafana Cloud MCP tool and prints the
result. No UI, no multi-agent, no seeded data. When this prints tool calls and a
grounded answer, the Grafana track is GO and you stop touching auth.

    python rung2_adk_agent.py --target cloud    # hosted mcp.grafana.com  <- TRY FIRST
    python rung2_adk_agent.py --target bridge   # local/OSS mcp-grafana fallback

Try `cloud` first (architecture amendment 0.1): Grafana's own build session connects an
ADK agent to Grafana Cloud MCP by one URL, so the hosted path is the sanctioned one and
makes the track-compliance argument unarguable. Fall back to `bridge` only if the hosted
endpoint demands an interactive browser flow a headless agent cannot complete.
"""
import argparse
import asyncio
import os

from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
from google.genai import types

load_dotenv()

APP = "second_unit_spike"
USER = "spike-user"
MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")

PROMPT = (
    "You are a post-production pipeline engineer for a VFX studio. "
    "Use the Grafana tools available to you to inspect the observability stack. "
    "Always call a tool before answering; never guess. "
    "Report what you actually found, and name the tools you used."
)

TASK = (
    "Inventory this Grafana stack for me: what datasources are configured, and what "
    "dashboards already exist? Then tell me, in one sentence, what kind of telemetry "
    "this stack is currently able to answer questions about."
)


def build_connection(target):
    """Version-tolerant construction. ADK is at 2.x and the params model has churned;
    fall back to the minimal signature rather than dying on an unknown field.

    Verified on 2026-08-25 with google-adk 2.7.1 / mcp 1.29.1: the full signature
    (url, headers, timeout, sse_read_timeout) is accepted, so the fallbacks below are
    belt-and-braces for a version bump, not the expected path."""
    if target == "cloud":
        url = os.getenv("GRAFANA_CLOUD_MCP_URL", "https://mcp.grafana.com/mcp")
        headers = {
            "Authorization": f"Bearer {os.environ['GRAFANA_SERVICE_ACCOUNT_TOKEN']}",
            "X-Grafana-URL": os.environ["GRAFANA_URL"],
            "Accept": "application/json, text/event-stream",
        }
    else:
        url = os.environ["MCP_GRAFANA_URL"]
        headers = {
            "Authorization": f"Bearer {os.environ['MCP_GRAFANA_SERVER_TOKEN']}",
            "Accept": "application/json, text/event-stream",
        }
    print(f"target={target}  url={url}")
    try:
        return StreamableHTTPConnectionParams(
            url=url, headers=headers, timeout=30, sse_read_timeout=120
        )
    except Exception:  # noqa: BLE001 - field names differ across ADK versions
        try:
            return StreamableHTTPConnectionParams(url=url, headers=headers, timeout=30)
        except Exception:  # noqa: BLE001
            print("!! could not set a timeout on the MCP connection, watch for hangs")
            return StreamableHTTPConnectionParams(url=url, headers=headers)


async def main(target):
    print(f"\n=== Rung 2: ADK + {MODEL} + Grafana MCP ===\n")

    toolset = McpToolset(connection_params=build_connection(target))

    agent = LlmAgent(
        model=MODEL,
        name="pipeline_scout",
        description="Inspects a studio's Grafana observability stack.",
        instruction=PROMPT,
        tools=[toolset],
    )

    session_service = InMemorySessionService()
    runner = Runner(app_name=APP, agent=agent, session_service=session_service)
    session = await session_service.create_session(app_name=APP, user_id=USER)

    message = types.Content(role="user", parts=[types.Part(text=TASK)])

    tool_calls = 0
    final = None
    async for event in runner.run_async(
        user_id=USER, session_id=session.id, new_message=message
    ):
        for part in (event.content.parts if event.content else []) or []:
            if getattr(part, "function_call", None):
                tool_calls += 1
                fc = part.function_call
                print(f"  -> tool call: {fc.name}({dict(fc.args or {})})")
            if getattr(part, "function_response", None):
                name = part.function_response.name
                print(f"  <- response from {name}")
        if event.is_final_response() and event.content and event.content.parts:
            final = "".join(p.text or "" for p in event.content.parts)

    print("\n--- agent answer ---")
    print(final or "(no final response)")

    print(f"\ntool calls made: {tool_calls}")
    if tool_calls and final:
        print(f"RUNG 2 GREEN via '{target}': the Grafana track is GO. Stop debugging auth.")
        if target == "cloud":
            print("Hosted Cloud MCP works headless: drop the bridge from the deployed")
            print("topology (architecture 0.1) and say so plainly in the README.")
        print("Next: seed the render-farm telemetry (03-prototype/telemetry/seed.py).")
    else:
        print(f"RUNG 2 RED via '{target}': no tool calls, or no output.")
        if target == "cloud":
            print("Expected if the hosted endpoint requires interactive OAuth 2.1.")
            print("Re-run with --target bridge; that result is the plan of record.")
        else:
            print("Check: did Rung 1 pass? If yes, this is ADK/model-side, not credentials.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=("cloud", "bridge"), default="cloud",
                    help="hosted mcp.grafana.com (default) or the local OSS bridge")
    asyncio.run(main(ap.parse_args().target))
