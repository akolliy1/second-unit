"""The MCP connection, in one place.

Hosted `mcp.grafana.com` cannot be used from an unattended process: its authorization
server offers only `authorization_code` and `refresh_token`, so there is no grant a
headless agent can complete. We therefore bridge through Grafana's official open-source
`mcp-grafana` server, authenticated to our Grafana Cloud stack with a service account
token. Evidence for that decision: `03-prototype/spike/NOTES.md`, Rung 3.

Timeouts are load-bearing. adk-python #2615 is an indefinite hang against remote
streamable-HTTP MCP servers; every connection here sets an explicit bound.
"""
import os

from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams


def grafana_toolset(tool_filter=None) -> McpToolset:
    """One toolset per stage, optionally narrowed.

    Narrowing matters: the bridge exposes 76 tools, and handing all 76 to every stage
    invites the model to wander (and inflates every prompt). Each stage gets the tools its
    job needs.
    """
    params = StreamableHTTPConnectionParams(
        url=os.environ["MCP_GRAFANA_URL"],
        headers={
            "Authorization": f"Bearer {os.environ['MCP_GRAFANA_SERVER_TOKEN']}",
            "Accept": "application/json, text/event-stream",
        },
        timeout=30,
        sse_read_timeout=180,
    )
    return McpToolset(connection_params=params, tool_filter=tool_filter)


# Stage tool budgets. Names verified against the live bridge (mcp-grafana v1.2.0).
WATCHTOWER_TOOLS = [
    "list_datasources",
    "list_alert_groups",
    "list_prometheus_metric_names",
    "query_prometheus",
    "query_loki_logs",
    # `find_error_pattern_logs` REMOVED 2026-08-26. It is a Sift *investigation* tool whose
    # signature is (name, labels, start, end) -- no `datasourceUid`, unlike every other
    # tool here. The model applied the pattern it had learned from its siblings, got back
    # `unknown argument "datasourceUid"`, and retried 11 times: the error names the wrong
    # argument but not the right one, so there is nothing in it to break the loop. Caught
    # on the deployed service by the tool-call budget, which is the only reason it did not
    # keep going. query_loki_logs does this job and has worked on every run.
    # Same fix as the Diagnostician, for the same reason: without label discovery it
    # guesses stream selectors, gets nothing, and reports zero error signatures on a farm
    # that is visibly full of them. Honest, but useless -- and the honesty only came after
    # the anti-fabrication rule stopped it inventing a cause instead.
    "list_loki_label_names",
    "list_loki_label_values",
]

DIAGNOSTICIAN_TOOLS = [
    "list_datasources",
    "list_prometheus_metric_names",
    "list_prometheus_label_values",
    "query_prometheus",
    "query_loki_logs",
    "query_loki_patterns",
    # Label DISCOVERY for Loki, added 2026-08-26 after a real failure: the agent guessed
    # Prometheus-style `instance`/`job` labels on log streams, got zero results, and then
    # invented a "the logging agent has failed" finding to explain the silence. It needs
    # to be able to ask what the labels actually are.
    "list_loki_label_names",
    "list_loki_label_values",
    "analyze_loki_labels",
    "check_datasources_health",
]


FORECASTER_TOOLS = [
    "list_datasources",
    "list_prometheus_metric_names",
    "query_prometheus",
]

# Read-only. The planner proposes writes; it must not be able to perform them.
PLANNER_TOOLS = [
    "list_datasources",
    "query_prometheus",
    "search_dashboards",
    "get_dashboard_summary",
]

# Handed out ONLY after a human approves. See stages.remediation_executor.
EXECUTOR_TOOLS = [
    "create_annotation",
    "update_dashboard",
    "create_folder",
    "alerting_manage_rules",
    "generate_deeplink",
    "search_dashboards",
    "list_datasources",
    # Added 2026-08-26 after a real failure: creating an alert rule requires a
    # folder_uid, and the executor had create_folder but no way to LIST folders. It
    # reported, correctly, that "the available tools do not provide a mechanism to list
    # existing folder UIDs" and gave up. Same class of gap as the missing Loki label
    # tools: we withheld a capability and its discovery path together.
    "search_folders",
    # Needed so the executor can read back what it just wrote instead of assuming.
    "get_dashboard_by_uid",
    "get_dashboard_summary",
]


# The ask feature's tool budget. Narrow on purpose: a question box with 76 tools behind it
# is an invitation to wander, and the failure mode of wandering is a slow wrong answer.
ASK_TOOLS = [
    "list_datasources",
    "list_prometheus_metric_names",
    "query_prometheus",
    "query_loki_logs",
    "list_loki_label_values",
]
