# Day-1 Spike — prove the Grafana track is buildable, in one day

**Goal:** one Gemini-powered ADK agent, running locally, that calls a Grafana Cloud MCP
tool and returns a grounded answer. No UI. No multi-agent. No seeded data. Nothing else
until this is green.

**Why a ladder:** each rung isolates one failure domain, so a red result tells you *which
layer* is broken instead of leaving you bisecting a five-layer stack at 2am.

| Rung | File | Proves | If it fails |
|---|---|---|---|
| 0 | `bootstrap.sh`, `preflight.py` | machine, deps, GCP auth, Gemini reachable | environment problem — fix before writing code |
| 1 | `rung1_mcp_raw.py` | Grafana token + network + MCP server (no LLM) | credentials or the bridge |
| 2 | `rung2_adk_agent.py` | **ADK + Gemini + MCP end to end ← the go/no-go** | ADK/model side, since Rung 1 already passed |
| 3 | `rung3_cloud_mcp.py` | whether hosted `mcp.grafana.com` accepts headless auth | a *finding* to document, not a blocker |
| — | `fallback_clickhouse.py` | ClickHouse pivot is viable | only run if 1 and 2 both failed |

## Known blockers on this machine (found 2026-08-22)
- **System Python is 3.9.6** — `google-adk` (now 2.6.x) requires **3.10+**. `bootstrap.sh`
  installs Python 3.11 via Homebrew and builds a venv.
- **No gcloud CLI** — needed for Application Default Credentials and, later, Agent Engine
  deploy. `bootstrap.sh` installs it, then asks you to reopen your terminal.
- Docker and Go are already present. Docker is how we run `grafana/mcp-grafana`.

## Run order

```bash
./bootstrap.sh                      # installs py3.11 + gcloud, makes .venv, copies .env
# fill in .env
gcloud auth application-default login
gcloud config set project <your-project-id>
gcloud services enable aiplatform.googleapis.com

./.venv/bin/python preflight.py     # must be all green

./start_grafana_mcp.sh              # terminal 2 — leave running
./.venv/bin/python rung1_mcp_raw.py
./.venv/bin/python rung2_adk_agent.py
./.venv/bin/python rung3_cloud_mcp.py
```

Record every result in `NOTES.md` as you go. The Rung 1 tool dump becomes your
Technological Implementation evidence in the final README, so don't throw it away.

## The auth design, and why it's shaped this way
`mcp.grafana.com/mcp` uses an **OAuth 2.1 browser flow**, which a headless agent on Agent
Engine cannot complete unattended. So the spike bridges through Grafana's **official
open-source `mcp-grafana` server**, which takes a static **service account token**
(`GRAFANA_URL` + `GRAFANA_SERVICE_ACCOUNT_TOKEN`) and re-exposes the same tools over
streamable HTTP with a bearer token of your choosing.

That is still your Grafana Cloud stack, still Grafana's own MCP server, still runtime MCP
tool calls. But it is *arguably* not "the Grafana Cloud MCP server" under a strict reading
of the track rule — which is exactly why Rung 3 exists and why the **forum question goes
out today**, not on Sept 8.

## Timeouts are load-bearing
`adk-python` issue #2615 is an indefinite hang against remote streamable-HTTP MCP servers.
Every client here sets an explicit `timeout`, and every await is wrapped in
`asyncio.wait_for`. Do not remove these to "clean up" the code later.

## Secrets
`.env` is gitignored. Your Grafana service account token grants access to your stack —
before the repo goes public, run `git log -p | grep -i glsa_` and confirm nothing leaked.
In production the token moves to **Secret Manager**, which is also a scored resource in
the hackathon guide.
