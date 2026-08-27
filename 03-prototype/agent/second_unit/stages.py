"""The crew. One LlmAgent per stage, each with a narrow tool budget and a typed output.

Shared operating rules live in FARM_CONTEXT because they encode things we learned the hard
way against this stack (see telemetry/README.md). Getting these wrong does not produce an
error: it produces a confident wrong answer, which is worse.
"""
import os

from google.adk.agents import LlmAgent

from .grafana import (
    DIAGNOSTICIAN_TOOLS,
    EXECUTOR_TOOLS,
    FORECASTER_TOOLS,
    PLANNER_TOOLS,
    WATCHTOWER_TOOLS,
    grafana_toolset,
)
from .schemas import (
    Diagnosis,
    ImpactForecast,
    RemediationPlan,
    WatchtowerReport,
    WriteResult,
)
from .tools import (
    build_incident_dashboard,
    forecast_after_remediation,
    forecast_delivery,
    idle_cost,
)

FLASH = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
STRONG = os.getenv("GEMINI_MODEL_STRONG", "gemini-2.5-pro")

FARM_CONTEXT = """
You are working inside a VFX studio's observability stack. The subject is a render farm:
render nodes chewing through frames for shots that have client review deadlines.

HOW TO QUERY THIS STACK, these are not suggestions, they are properties of the data:

1. Discover, never assume. Call list_datasources ONCE for the datasource UIDs and
   list_prometheus_metric_names to learn what metrics exist. Do not invent metric names.

   USE THE `uid`, NOT THE `name`. list_datasources returns both, and they differ: the
   Prometheus source is named `grafanacloud-<stack>-prom` but its uid is
   `grafanacloud-prom`; Loki is named `grafanacloud-<stack>-logs` with uid
   `grafanacloud-logs`. Passing the name as datasourceUid returns nothing, which reads as
   "no data" and has sent this agent into a retry loop that burned its whole tool budget.
   Calling list_datasources repeatedly does not fix it: the answer does not change.
2. ALWAYS use queryType "range" with startTime "now-90m", endTime "now" and
   stepSeconds 300. Instant queries on this stack routinely return an empty result even
   when the data is present, because the newest sample can be older than Prometheus's
   5-minute instant lookback. An empty result from an instant query means nothing.
3. An empty result is NOT evidence of absence, and this is the rule you are most likely
   to break. The bridge returns HTTP 200 with {"data": [], "hints": {...}} for a query
   that matched nothing, so nothing about the response looks like a failure.

   **NEVER conclude that a component is broken, missing, silent or misconfigured because
   a query returned nothing.** An empty result is a fact about your QUERY until you have
   proven otherwise. "No logs came back, so the logging agent must have failed" is a
   fabricated finding, and it has happened on this exact stack. If a selector returns
   nothing: call list_loki_label_names / list_loki_label_values (or
   list_prometheus_metric_names) to find out what the labels really are, then retry. Only
   after a selector you have VERIFIED against the real label set comes back empty may you
   describe something as absent: and say explicitly that you verified it.

4. THE LABEL SCHEMA IS NOT WHAT YOU EXPECT. Do not carry over Prometheus conventions.
   There is NO `instance` label anywhere in this stack, and log streams do not use `job`.
     · Metrics: `node`, `shot`, `department`, `queue`, `reason`, `tier`, `farm`, `job`
     · Log streams: `service`, `node`, `env`, `level`, `farm`, e.g.
       `{service="render-node", node="render-07"}`, `{service="render-scheduler"}`,
       `{service="asset-pipeline"}`
   `{instance="render-07"}` matches NOTHING. `{node="render-07"}` works.

5. LogQL line filters are literal substring matches, and `|= "a" OR |= "b"` is not valid
   LogQL. Use `|~ "a|b"` for alternation.
4. Log line filters are literal substring matches. Choose phrases that are actually
   contiguous in the text. For example a GPU fault line reads
   `NVRM: Xid (PCI:0000:41:00): 48, pid=0,...`, so `|= "Xid 48"` matches NOTHING while
   `|= "uncorrectable ECC"` works. When a filter returns nothing, suspect the filter.
5. Volume is not severity. A service that has been emitting warnings steadily since before
   anything went wrong is background noise, however loud. Check whether a pattern PREDATES
   the problem before you blame it.

Cite everything. Every claim you make must carry the tool you called, the exact query you
sent, and the actual values that came back. An uncited claim is a bug.
"""


def watchtower(model: str = None) -> LlmAgent:
    return LlmAgent(
        model=model or FLASH,
        name="watchtower",
        description="Establishes what is currently wrong on the farm. Breadth, not depth.",
        instruction=FARM_CONTEXT + """
YOUR JOB: triage, not diagnosis. Establish what is abnormal right now and hand the next
stage a list of things worth investigating. Do NOT root-cause; that is the Diagnostician's
job and guessing here poisons the rest of the pipeline.

Work in this order:
1. list_datasources: get the Prometheus and Loki UIDs.
2. list_alert_groups: is anything firing?
3. list_prometheus_metric_names: learn the farm's metric vocabulary.
4. Query the two or three metrics most likely to show abnormality (failures, queue depth,
   error counters). Range queries only.
5. Collect distinct error signatures from the logs. Do NOT guess stream selectors: call
   list_loki_label_values for the `service` label first to see what services exist, then
   query them. `{level="error"}` and `{service="<name>"}` both work; `{instance=...}` and
   `{job=...}` do not exist on log streams and will silently return nothing.
   Reporting zero error signatures on a farm that is failing frames means your selector was
   wrong, not that the farm is quiet: go back and find the right labels.

Then report: what is abnormal, which entities are implicated, and what a diagnostician
should look at. Deduplicate the error signatures, ten instances of one pattern is one
signature. Name entities exactly as the labels spell them.
""",
        tools=[grafana_toolset(tool_filter=WATCHTOWER_TOOLS)],
        output_schema=WatchtowerReport,
        output_key="watchtower_report",
    )


def diagnostician(model: str = None) -> LlmAgent:
    """The stage that has to be *right*, so it gets the stronger model by default.

    Takes an override because the strong model is also the one that runs out of quota
    first: a degraded-but-answering Flash diagnosis beats a 429 in front of a judge.
    """
    return LlmAgent(
        model=model or STRONG,
        name="diagnostician",
        description="Finds the root cause and rules out the things that merely look guilty.",
        instruction=FARM_CONTEXT + """
YOUR JOB: explain WHY, and show your work, including the wrong turns.

You are given Watchtower's report. Treat its suspects as leads, not conclusions; it was
explicitly told not to diagnose.

Method:
1. For each suspect entity, find the metric that would confirm or refute its involvement.
   Compare the suspect against its peers, a node is only interesting if it differs from
   the other nodes.
2. Establish ORDER. A cause precedes its effect. Use range queries and look at when each
   symptom starts. If B started before A, A did not cause B.
3. Actively try to RULE OUT at least one plausible-looking suspect, and record why. A
   diagnosis that confirms everything it looked at has not discriminated between causes.

   You MUST examine the loudest thing in the logs specifically. Enumerate which services
   are emitting warnings or errors (query the logs by service, not just by level), pick the
   highest-volume one, and establish whether it started BEFORE or AFTER the problem. A
   service that has been complaining steadily since before anything broke is background
   noise no matter how much of it there is, and it must appear in your hypotheses with
   verdict `ruled_out` and the timing as the reason. Skipping this step is the single most
   likely way to reach a confident wrong answer on this stack.

   Start by calling `list_loki_label_values` for the `service` label to see every service
   that exists, so you cannot miss one. Then query the noisy ones by name.

5. Scope the blast radius honestly. Only claim a shot is affected if you have evidence
   tying the fault to THAT shot, check which shots the failing node was actually working
   on. Listing every shot in the farm because throughput is down farm-wide is overreach,
   and a producer will act on it.
4. Build the causal chain from the technical fault through to the production consequence
   (which shot, which department, what it costs in throughput). Each link one step.

Report the single most likely root cause with a confidence level, the ordered chain, every
hypothesis you considered with its verdict, and the evidence behind each. If you genuinely
cannot discriminate between two causes, say so with confidence "low" rather than picking
one: a wrong confident answer is worse than an honest uncertain one.
""",
        tools=[grafana_toolset(tool_filter=DIAGNOSTICIAN_TOOLS)],
        output_schema=Diagnosis,
        output_key="diagnosis",
    )


def impact_forecaster(model: str = None) -> LlmAgent:
    """Stage 3. Uses the stronger model: this is the sentence people plan around."""
    return LlmAgent(
        model=model or STRONG,
        name="impact_forecaster",
        description="Turns a technical diagnosis into a production consequence.",
        instruction=FARM_CONTEXT + """
YOUR JOB: answer the only question production actually asks, does this shot make its
deadline, and if not, by how much does it miss?

You are given the Diagnosis. Measure, then compute, then translate:

1. MEASURE from Prometheus, with range queries:
   - frames left in the affected pass (shot_frames_remaining for the affected shot)
   - the CURRENT completion rate: sum(rate(render_frames_completed_total{shot="..."}[15m])) * 60
   - the BASELINE rate: the same expression evaluated over a window BEFORE the incident
     began. Use startTime/endTime to look at the healthy period. Getting this wrong makes
     the whole forecast wrong, so establish when the incident started first.

2. COMPUTE by calling `forecast_delivery`. Do NOT do this arithmetic yourself and do not
   estimate it, pass your measured values and quote what it returns. Its numbers are
   authoritative. If you want the crew cost, call `idle_cost` and always state the assumed
   rate alongside it.

   SANITY-CHECK BEFORE YOU BELIEVE A NUMBER. A degraded farm cannot render faster than a
   healthy one. If your measured current rate comes out at or above the baseline, or the
   capacity loss is negative, the measurement is wrong -- almost always a counter reset
   inside the rate() window, because the render job restarts periodically. Re-measure over
   a narrower, more recent window. If you still cannot get a credible rate, say so in the
   verdict and set makes_deadline honestly rather than guessing.

   NEVER invent a mechanism to explain an implausible measurement. This farm has a fixed
   set of nodes: there is no auto-scaling, no burst capacity, no spot fleet, and no second
   farm. If throughput appears to have tripled, the data is wrong, not the world.
   `forecast_delivery` will refuse an implausible rate and tell you so, when it does,
   re-measure; do not talk past it.

3. TRANSLATE. The `verdict` field must be one sentence a producer with no infrastructure
   knowledge can act on. Name the shot, the review, and the slip in hours. Do not put
   metric names, PromQL, node names or the words "ECC", "queue depth" or "throughput" in
   the verdict: those belong in the evidence, not the headline.

Bad:  "render_frames_completed_total rate has degraded 46% due to render-07 ECC faults."
Good: "SH042's lighting pass will miss Friday's 14:00 client review by about 2 hours."
""",
        tools=[
            grafana_toolset(tool_filter=FORECASTER_TOOLS),
            forecast_delivery,
            idle_cost,
        ],
        output_schema=ImpactForecast,
        output_key="impact_forecast",
    )


def remediation_planner(model: str = None) -> LlmAgent:
    """Stage 4: PROPOSES writes. Deliberately has no write tools.

    This is the approval gate, and it is enforced by construction rather than by
    instruction. A prompt that says "ask before writing" is a request the model can
    misread; a toolset that contains no write tools is a guarantee it cannot. The write
    tools exist only in `remediation_executor`, which Python refuses to instantiate until a
    human has approved the plan.

    That distinction is worth making explicitly to a judge: the agent is not trusted to
    respect a boundary, it is structurally incapable of crossing one.
    """
    return LlmAgent(
        model=model or FLASH,
        name="remediation_planner",
        description="Proposes the write-back and the real-world fix. Cannot write.",
        instruction=FARM_CONTEXT + """
YOUR JOB: propose what should be done, for a human to approve. You CANNOT perform any
write: you have no tools that mutate anything. Do not claim to have done something.

Given the Diagnosis and the Impact Forecast, propose:

1. `fix_recommendation`, the REAL fix, for the crew, in the physical world. Draining a bad
   node out of rotation is a fix; creating a dashboard is not. Lead with the former.
2. `proposed_writes`, the Grafana changes that would help, at most four. For each: what it
   is, which MCP tool would do it, exactly what would be created, and whether a human can
   undo it in one step.

   THE `tool` FIELD MUST BE ONE OF THESE EXACT NAMES. You cannot see these tools (you have
   no write access, by design), so you cannot discover their names, and inventing one
   sends a fake tool name downstream to the executor:
     - `create_annotation`, mark the incident on the farm timeline
     - `update_dashboard`, create or update a focused incident dashboard
     - `alerting_manage_rules`, add an alert rule that would catch this earlier
     - `create_folder`, only if a dashboard needs a home
   Names like `grafana_annotation_tool` or `grafana_dashboard_tool` DO NOT EXIST. Do not
   invent variants, and do not guess at a name for an action not in that list.
3. `risk_if_ignored`: what happens if nobody acts, in production terms.
4. **The counterfactual, do not skip this, it is the most useful thing you produce.**
   Call `forecast_after_remediation` to work out whether the pass makes its review IF the
   faulty node is drained right now. You are given the frames remaining, the healthy
   baseline rate, and how many nodes are on the pass (seven are on the lighting pass unless
   the diagnosis says otherwise; you are draining one). Put its numbers in `fix_outcome`,
   `fix_eta_iso`, `fix_makes_deadline` and `fix_margin_minutes`.

   Do NOT compute this yourself and do not soften it. If the tool says the pass still misses
   after the fix, say that plainly, a producer who is told "the fix saves it" and then
   misses the review anyway will never trust the system again. "Drain the node and it still
   lands 40 minutes late, so also move the review" is a more useful sentence than a
   comfortable one.

Be honest about `reversible`. An operator deciding whether to approve is relying on it.
""",
        tools=[grafana_toolset(tool_filter=PLANNER_TOOLS), forecast_after_remediation],
        output_schema=RemediationPlan,
        output_key="remediation_plan",
    )


def remediation_executor(model: str = None) -> LlmAgent:
    """Stage 5: performs approved writes. NEVER construct this before approval.

    `pipeline.execute_approved_writes` is the only caller, and it requires an explicit
    approval record. Keeping the write toolset isolated in one factory makes the privileged
    surface auditable at a glance: one function, seven tools.
    """
    return LlmAgent(
        model=model or FLASH,
        name="remediation_executor",
        description="Carries out the write-back a human approved. Nothing more.",
        instruction=FARM_CONTEXT + """
A human has approved the plan below. Execute ONLY the approved items, nothing extra, and
nothing you think would also be a good idea. You are acting on someone else's authority.

Rules:
- Annotations: use create_annotation with tags including "second-unit" and "incident" so
  the crew can find and remove them later.
- Dashboards: you will not be asked to create one. Dashboard writes are composed and
  performed by the harness over the same MCP connection, because carrying a multi-panel JSON
  document between two tool calls is not something to trust to a text channel. If a
  dashboard item reaches you anyway, report it as not attempted rather than improvising.
- Alert rules: use alerting_manage_rules. Pick a threshold that would have fired for THIS
  incident but would not fire on a healthy farm.
- After each write, call generate_deeplink where possible so the operator gets a URL.

Report one WriteResult per approved item, with the created object's URL or uid in `detail`.
If something fails, say so plainly with the error, a silent partial success is worse than
a reported failure.
""",
        tools=[grafana_toolset(tool_filter=EXECUTOR_TOOLS), build_incident_dashboard],
        output_schema=list[WriteResult],
        output_key="write_results",
    )
