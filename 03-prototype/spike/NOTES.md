# Day-1 Spike Log

Scaffolded 2026-08-22 (never run). **Resumed 2026-08-25.**
Operating deadline: **2026-09-07**, see `00-intel/00-verified-facts.md`.

## Rung 0, bootstrap + preflight   ✅ GREEN 2026-08-25
- [x] Python 3.11 installed, `3.11.16` via Homebrew (system Python is 3.9.6, too old)
- [x] gcloud CLI installed, **SDK 582.0.0**, tarball install to `~/google-cloud-sdk`
      (no sudo). ⚠️ Its installer runs under whatever `python` it finds and **crashes on
      3.9** (`TypeError: unsupported operand type(s) for |`). Fixed by exporting
      `CLOUDSDK_PYTHON=/opt/homebrew/opt/python@3.11/bin/python3.11`, now persisted in
      `~/.zshrc`. If gcloud ever breaks on this machine, check that variable first.
- [x] `application-default login` done 2026-08-26; **quota project set** to
      `second-unit-506700` (the "Cannot find a quota project" warning otherwise surfaces
      later as a confusing quota/API error at the first Gemini call).
      NB: `gcloud auth login` was NOT run, the CLI has ADC but no CLI account, so
      `gcloud services enable` fails. APIs were enabled through the Service Usage REST
      API using ADC instead. Run `gcloud auth login` if you want CLI-side commands.
- [x] APIs enabled on `second-unit-506700`: aiplatform, run, secretmanager,
      artifactregistry, cloudbuild, texttospeech, firestore
- [x] **Gemini on Vertex verified working**, a real generate_content round trip
- [ ] Grafana Cloud stack created, URL: ______________________  ← STILL NEEDED
- [x] Service account token captured (`glsa_...`, in `03-prototype/.env`), untested,
      testing it requires the stack URL
- [ ] `preflight.py` all green, blocked on the stack URL + a write-scoped push token
- google-adk version actually installed: **2.7.1**
- GEMINI_MODEL that actually worked: **`gemini-2.5-flash`** (and `gemini-2.5-pro`)

### 🔴 Second trap, the configured model does not exist on Vertex
`.env.example` shipped `GEMINI_MODEL=gemini-flash-latest`. On Vertex that 404s:
`Publisher model .../models/gemini-flash-latest not found`. The `-latest` aliases are an
AI-Studio/Gemini-API convention and do not resolve through Vertex publisher models.
Corrected to **`gemini-2.5-flash`**, with `GEMINI_MODEL_STRONG=gemini-2.5-pro` added for
the Impact Forecaster. Both verified with a live call.

### 🔴 Third trap, the Grafana push token had read scope
Both `remote_write` and the Loki push return
`401 authentication error: invalid scope requested`. The token decodes to
`stack-1808248-hm-read-metrics-remote-write`, read, not write. The Send-Metrics page's
"Generate now" does not guarantee write scope. Fix: create a Cloud Access Policy with
**`metrics:write` + `logs:write`** and mint a token from it. Endpoints and usernames are
confirmed correct (prom user `3540720`, loki user `1766035`).

### 🔴 Trap found in Rung 0, the one that would have cost a night
`google-adk` 2.7.1 requires **`mcp>=1.24,<2`**, but it declares that dependency only under
an **extra** (`google-adk[mcp]`). Our `requirements.txt` asked for a bare `mcp>=1.9.0`, so
pip happily resolved **mcp 2.1.1**, and `pip check` reported *no broken requirements*, 
while every ADK MCP import died with `No module named 'mcp.shared.session'` (the modules
were reorganized in mcp 2.x). This is almost certainly the same class of failure Grafana's
build session hits at 03:55.

Fixed by pinning `mcp>=1.24,<2` and installing `google-adk[mcp]`. **Now at mcp 1.29.1 and
`McpToolset` + `StreamableHTTPConnectionParams` import cleanly.** Do not relax that pin.
Confirmed accepted fields: `url, headers, timeout, sse_read_timeout, terminate_on_close,
httpx_client_factory`: so the explicit timeouts that guard adk-python #2615 do work.

## Rung 1, raw MCP (auth + network)   ✅ GREEN 2026-08-26
- [x] handshake ok, `mcp-grafana v1.2.0`, streamable-HTTP on `localhost:8000/mcp`,
      caller auth enforced with a bearer token
- Tool count exposed: **76** (the track blurb promises "60+", so we are above the line)
- Tool categories present: alerting (rules, routing, silences) · Prometheus (query,
      histogram, label/metric discovery) · Loki (query, patterns, stats, error-pattern
      search, label analysis) · Tempo (TraceQL search, metrics, attributes) · dashboards
      (search, get, update, panel queries, panel images) · annotations (get/create/update)
      · incidents (create, list, activity) · Sift investigations · OnCall · Pyroscope ·
      folders · datasources · snapshots · `grafana_api_request` escape hatch
- First successful tool call: `list_datasources()`, returned all 12 stack datasources
- Notes: ran the **native Go binary** (`go install github.com/grafana/mcp-grafana/cmd/
      mcp-grafana@latest`) instead of the container. Docker Desktop's daemon is usually
      not running on this machine and the binary removes that dependency entirely.
      `start_grafana_mcp.sh` prefers the binary and falls back to Docker.

## Rung 2, ADK + Gemini + MCP  ← THE GO/NO-GO   ✅ GREEN 2026-08-26
- Which target went green: **`bridge`** (hosted `cloud` refused, see Rung 3)
- [x] tool calls > 0, **2**
- [x] grounded final answer, and it was actually grounded: it correctly named the
      datasource mix and counted 14 existing dashboards, none of which it could have
      guessed
- Tools the model chose on its own: `list_datasources`, `search_dashboards`
      (nothing was forced, the instruction only said "always call a tool before
      answering")
- Model: **`gemini-2.5-flash`** on Vertex
- Notes / errors: two benign warnings on the way through, 
      `mTLS was requested but AsyncAuthorizedSession channel is not mTLS` and ADK's
      `[EXPERIMENTAL] PLUGGABLE_AUTH` notice. Neither affects the run; do not chase them.

## Rung 3, hosted Cloud MCP (compliance probe)   ✅ ANSWERED 2026-08-26
Run `probe_mcp.py` to reproduce. Five surfaces probed; all five refused a static token.

- Static bearer token accepted?  **NO, definitively.**
- Exact errors:
  - `https://mcp.grafana.com/mcp` + `Authorization: Bearer glsa_...` (with and without
    `X-Grafana-URL`) → **401** `{"error":"invalid_token","error_description":"could not
    determine backend for token"}`
  - same with `X-Access-Token` → **401** `missing or invalid Bearer token`
  - `<stack>/api/mcp` → **404 Not found**;  `<stack>/mcp` → **404** (serves the Grafana UI)
- The `WWW-Authenticate` header advertises a per-stack resource,
  `https://mcp.grafana.com/mcp/gurl/<base64url(stack_url)>`. Posting `initialize` there
  directly still returns the same 401, the path is for OAuth resource discovery, not a
  bypass.
- **The decisive evidence.** `GET /.well-known/oauth-authorization-server/mcp/gurl/<b64>`
  returns:
  ```
  grant_types_supported: ["authorization_code", "refresh_token"]
  token_endpoint:        https://mcp.grafana.com/mcp/oauth/token
  registration_endpoint: https://mcp.grafana.com/mcp/oauth/register
  scopes_supported:      ["grafana:read", "grafana:query", "grafana:write"]
  ```
  **There is no `client_credentials` grant.** Every supported grant requires a user agent
  that can complete a redirect. A headless agent on Agent Engine cannot obtain a token,
  and no header combination changes that: it is a property of the authorization server.

**Conclusion: the original architecture was right and the optimistic reading of the build
session was wrong.** Grafana's session works because it runs locally, where a browser is
available to complete the flow. We bridge through Grafana's official OSS `mcp-grafana`
server against the same Cloud stack. This is now an evidenced engineering decision with
reproducible error strings, not a workaround, which is a better story for the judges than
the one we would have told if the hosted path had simply worked.
- [ ] Forum question posted, link: ______________________
- [ ] Organiser answer received: ______________________

## DECISION (2026-08-26, four days late but taken)
- [x] **GO: Grafana track.** Locked. Stop touching auth. Next = seed telemetry.
- [ ] ~~NO-GO: pivot to ClickHouse~~, not needed. `fallback_clickhouse.py` stays in the
      repo unused.

**Decision: GO.**
**Reason:** the full stack is proven end to end, ADK 2.7.1 + `gemini-2.5-flash` on Vertex
+ 76 Grafana MCP tools over streamable HTTP, with the model selecting tools unprompted and
answering from what they returned. Nothing on the critical path is unproven any more except
the write-scoped push token, which is a credential, not a risk.

## Telemetry seeded   ✅ 2026-08-26
Write-scoped push token (`hackathon-write-policy`, realm `stack-1808248`) works:
remote_write **200**, Loki **204**, one token for both. Seeded 90 minutes, 6,390 samples,
511 lines, 0 rejected, and `telemetry/verify_seed.py` confirms **7/7 checks pass reading it
back through MCP**. `seed.py --warm 90 --incident-at 40 --live` is running to keep it fresh.

Two access policies on one realm is the correct setup, not a workaround: policies are
independent, each token binds to exactly one, and a leaked write token can then be revoked
without breaking reads. The write scopes added to the original read policy should be
reverted.

Gotchas recorded in `telemetry/README.md`: backfills age out of the 5-minute instant-query
lookback (a check passed, then failed, with unchanged data); `|= "Xid 48"` never matches a
real Xid line; and `--warm` prevents counter resets when resuming.

## Tools worth building the real agent on
From the Rung 1 dump. This list is the Technological Implementation evidence, every one of
these should appear in a real triage run, and the README should point at where each is called.

**Watchtower** (what is on fire)
 1. `list_alert_groups` / `get_alert_group`, firing alerts
 2. `find_error_pattern_logs`, error signatures in Loki without knowing the query yet
 3. `list_datasources`, datasource discovery, so nothing is hardcoded

**Diagnostician** (why)
 4. `list_prometheus_metric_names` / `list_prometheus_label_values`, discover the farm's
    own metric and label space rather than assuming it
 5. `query_prometheus`, the actual PromQL over node health, throughput, queue depth
 6. `query_loki_logs`, pull the `Xid 48` / exit-139 evidence
 7. `query_loki_patterns`, cluster the log noise, which is how the decoy gets dismissed
 8. `analyze_loki_labels`, narrow to the failing node
 9. `check_datasources_health`, rule out "the datasource is broken" before blaming the farm

**Impact Forecaster**
10. `query_prometheus` again, over `shot_frames_remaining` and completion rate, the ETA

**Remediator** (all behind the approval gate)
11. `create_annotation`, mark the incident on the timeline
12. `update_dashboard`, the focused incident dashboard
13. `alerting_manage_rules`, the rule that catches this earlier next time
14. `create_incident` + `add_activity_to_incident`, if Grafana IRM is on the free tier
15. `generate_deeplink`, hand the crew a URL straight to the evidence

That is 15 distinct tools across 6 subsystems, comfortably past the "10+" target.

## Telemetry seeded   ✅ 2026-08-26
Write-scoped push token (`hackathon-write-policy`, realm `stack-1808248`) works:
remote_write **200**, Loki **204**, one token for both. Seeded 90 minutes, 6,390 samples,
511 lines, 0 rejected, and `telemetry/verify_seed.py` confirms **7/7 checks pass reading it
back through MCP**. `seed.py --warm 90 --incident-at 40 --live` is running to keep it fresh.

Two access policies on one realm is the correct setup, not a workaround: policies are
independent, each token binds to exactly one, and a leaked write token can then be revoked
without breaking reads. The write scopes added to the original read policy should be
reverted.

Gotchas recorded in `telemetry/README.md`: backfills age out of the 5-minute instant-query
lookback (a check passed, then failed, with unchanged data); `|= "Xid 48"` never matches a
real Xid line; and `--warm` prevents counter resets when resuming.

## Tools worth building the real agent on
From the Rung 1 dump, list the 10+ tools you intend to exercise. This list goes
straight into the README as your Technological Implementation evidence.
1.
2.
3.
