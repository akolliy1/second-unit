# Second Unit

**An autonomous post-production pipeline SRE.** A crew of Gemini agents reads a VFX render
farm's telemetry through the Grafana MCP server, works out which shot is going to miss its
client review and by how long, and — only after a human approves — writes the incident back
into Grafana.

> **SH042's lighting pass will miss the 15:35 client review by approximately 1.6 hours.**
> Capacity is down 46%. The lighting department is blocked; compositing is blocked downstream.

Built for the Agentic Cinema Hackathon, Grafana Labs track.

- **Live demo:** https://second-unit-dzqjw5tifq-uc.a.run.app
- **Video:** _(to be added)_

---

## The problem

A visual effects house lives and dies by the client review date. When something in the render
farm degrades, the telemetry is already in Grafana — and nobody who needs it can read it. The
VFX supervisor doesn't open dashboards; the producer doesn't write PromQL. So the failure mode
is social: a coordinator notices a queue looks slow, asks a pipeline TD, and four hours later
somebody says "we're going to miss Friday."

The data was never missing. What's missing is the translation layer between infrastructure
telemetry and production decisions.

## What it does

Five stages, deterministically sequenced, with a typed object at every boundary:

| Stage | Job | Model |
|---|---|---|
| **Watchtower** | what is abnormal right now — breadth, not diagnosis | Flash |
| **Diagnostician** | why, including what it ruled *out* and on what evidence | Pro |
| **Impact Forecaster** | the production consequence, in a producer's language | Pro |
| **Remediation Planner** | the real-world fix and the proposed write-back. **Holds no write tools.** | Flash |
| **Remediation Executor** | performs approved writes, one agent call per write | Flash |

Alongside them, a **fleet sweep** in plain Python answers "what else needs me?" across every
in-flight shot with no model involved, so the agent is spent only on the exception.

## Two design commitments

**1. The model decides what it finds. Code decides what happens next.**

Every guarantee we tried to express as an instruction held *most* of the time — which is worse
than failing, because it passes testing. Each one now lives in code:

| Guarantee | Where it lives |
|---|---|
| Nothing is written without human approval | the planning stage has **no mutating tools**; the executor is constructed only from an `Approval` record — [`pipeline.py:102`](03-prototype/agent/second_unit/pipeline.py) |
| The delivery forecast is arithmetic, not rhetoric | [`tools.py:16`](03-prototype/agent/second_unit/tools.py) — and it **refuses** implausible input rather than forecasting from it |
| A retry loop cannot hammer the observability stack | per-stage tool-call budgets, [`pipeline.py:216`](03-prototype/agent/second_unit/pipeline.py) (ADK's own default is 500 calls) |
| "No data" is a question, not a finding | label-discovery tools plus an explicit rule in the shared prompt, [`stages.py`](03-prototype/agent/second_unit/stages.py) |

**2. The agent's report is a claim; the system checks it.**

After an approved write-back, [`verify.py`](03-prototype/agent/second_unit/verify.py) diffs the
stack **out of band** — through Grafana's HTTP API, not the MCP tools the agent just used — and
prints the disagreement. It distinguishes four outcomes: `confirmed`, `honest_failure`,
`unchecked`, and `false_success` — *reported successful but not present in the stack*. That last
one is not hypothetical: it is why the module exists.

That outcome is also a metric, so it is graphable next to everything else:

```
second_unit_write_claims_total{outcome="false_success"}
```

## Where each service is actually called

The hackathon rules ask for runtime use of Google Cloud and the partner's product — imported
and called, not named in a README. Exact call sites:

**Grafana Cloud MCP (runtime, every fact the agent states)**
- [`agent/second_unit/grafana.py:24`](03-prototype/agent/second_unit/grafana.py) — `StreamableHTTPConnectionParams` → `McpToolset`, with explicit timeouts
- Same file — per-stage tool budgets: which of the bridge's **76 tools** each stage may use
- A single triage run exercises `list_datasources`, `list_alert_groups`,
  `list_prometheus_metric_names`, `list_prometheus_label_values`, `query_prometheus`,
  `list_loki_label_names`, `list_loki_label_values`, `query_loki_logs`, `query_loki_patterns`,
  `search_folders`, `search_dashboards`, `get_dashboard_by_uid`, `create_annotation`,
  `update_dashboard`, `alerting_manage_rules` — read *and* write
- [`agent/second_unit/observe.py`](03-prototype/agent/second_unit/observe.py) — **Grafana Agent
  Observability**: the agent's own token counts, latencies, tool calls and failure reasons are
  pushed to the same stack it investigates

**Google Cloud**
- [`agent/second_unit/stages.py:28`](03-prototype/agent/second_unit/stages.py) — Gemini on
  **Vertex AI** (`gemini-2.5-flash` / `gemini-2.5-pro`), five ADK `LlmAgent`s with
  `output_schema` on every stage boundary
- [`agent/second_unit/pipeline.py`](03-prototype/agent/second_unit/pipeline.py) — ADK `Runner`,
  streaming events, plus quota-resilient retry with model downgrade
- [`deploy/`](03-prototype/deploy) — **Cloud Run** service (web + MCP bridge in one container),
  **Cloud Run Job** for the telemetry seeder, **Cloud Scheduler** restart trigger,
  **Secret Manager** for every credential, **Cloud Build** for images, **Artifact Registry**

## On the MCP connection, precisely

Grafana's hosted Cloud MCP endpoint cannot be used by an unattended process, and we can show
why rather than assert it. Its authorization-server metadata advertises:

```
grant_types_supported: ["authorization_code", "refresh_token"]
```

No `client_credentials`. Every supported grant needs a user agent that can complete a redirect,
so a headless deployment cannot obtain a token — a property of the authorization server, not a
header we got wrong. All five surfaces we probed refused a static service account token
([`spike/probe_mcp.py`](03-prototype/spike/probe_mcp.py) reproduces it).

So Second Unit bridges through Grafana's **official open-source `mcp-grafana` server** against
the same Grafana Cloud stack. In the deployed container that bridge runs beside the app on
localhost, which removes a public hop and a shared secret rather than adding one.

## The world the agent investigates

A studio's render farm isn't something you can borrow, so
[`telemetry/`](03-prototype/telemetry) generates one: 12 nodes, three shots in flight, and one
GPU that starts throwing uncorrectable ECC errors.

```
t+0m    render-07 logs Xid 48 / uncorrectable ECC; GPU temp climbs 62°C → 85°C
t+5m    frames dispatched to it fail with exit 139 and are requeued
t+8m    retries crowd the lighting queue; depth 18 → 63
t+12m   farm-wide throughput falls; SH042 slips past its review
```

Two things make it a real test rather than a script. There is a **decoy** — the asset pipeline
emits loud texture-cache-miss warnings throughout, starting *before* the incident, so an agent
that pattern-matches on "lots of warnings" reaches the wrong answer. And the review deadline is
**published as telemetry** (`shot_review_deadline_seconds`), so the agent discovers the
commitment rather than being handed it.

## Running it

```bash
cd 03-prototype
cp .env.example .env          # fill in: GCP project, Grafana stack URL + service account token
python -m venv .venv && ./.venv/bin/pip install -r spike/requirements.txt

gcloud auth application-default login
gcloud auth application-default set-quota-project <your-project>

# terminal 1 — the MCP bridge against your Grafana Cloud stack
./spike/start_grafana_mcp.sh

# terminal 2 — seed the farm, then keep it live
./.venv/bin/python telemetry/seed.py --backfill 60 --incident-at 35
./.venv/bin/python telemetry/seed.py --warm 60 --incident-at 35 --live --loop

# terminal 3 — run the pipeline
cd agent && ../.venv/bin/python -m second_unit.run            # stops at the proposal
cd agent && ../.venv/bin/python -m second_unit.run --execute  # performs approved writes

# the console
cd web && ../spike/.venv/bin/python -m uvicorn app:app --port 8080
```

`spike/` is the de-risk ladder that proved the stack end to end, and
[`spike/NOTES.md`](03-prototype/spike/NOTES.md) records what actually broke on the way —
including a dependency trap that reports itself as healthy (`google-adk` needs `mcp>=1.24,<2`
but declares it only as an extra, so a bare `mcp` install resolves to 2.x, `pip check` reports
no conflict, and every MCP import dies).

## Layout

```
03-prototype/
  agent/second_unit/   the five stages, schemas, tools, verification, observability
  telemetry/           the render farm: scenario, remote_write/Loki push, seeder
  web/                 FastAPI console — one page, SSE, no build step
  dashboards/          two baseline Grafana dashboards, provisioned as JSON
  deploy/              Cloud Run service + job, Cloud Build, Secret Manager wiring
  ops/                 stack hygiene for artefacts the agent leaves behind
  spike/               the de-risk ladder and its notes
docs/                  architecture, data model, agent design
```

## License

MIT — see [LICENSE](LICENSE).
