# Second Unit — Data Model

Three layers: the **telemetry** the agent queries, the **domain** it reasons about, and the
**contracts** that pass between stages. Get these right on paper and the build is mostly
typing.

---

## 1. Telemetry layer — what lives in Grafana Cloud

### 1.1 Metrics (Prometheus / Mimir via `remote_write`)

Naming follows Prometheus convention: `_total` counters, `_seconds` histograms, base units.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `render_frames_completed_total` | counter | show, sequence, shot, task, job_id, node, pool | frames finished |
| `render_frame_duration_seconds` | histogram | shot, task, pool | per-frame render time |
| `render_job_failures_total` | counter | show, shot, task, job_id, node, pool, **reason** | failures by cause |
| `render_job_frames_remaining` | gauge | job_id, shot, task | work left |
| `render_queue_depth` | gauge | pool, priority | queued jobs |
| `render_queue_wait_seconds` | gauge | pool | head-of-queue age |
| `render_node_gpu_temp_celsius` | gauge | node, pool, gpu_model | thermals |
| `render_node_gpu_memory_used_bytes` | gauge | node, pool | VRAM pressure |
| `render_node_up` | gauge | node, pool, driver_version | 1 online / 0 down |
| `license_features_available` | gauge | feature | free licence seats |
| `license_checkout_latency_seconds` | histogram | feature | contention signal |
| `asset_cache_hit_ratio` | gauge | site | cache health |

`reason` is a closed set: `oom`, `missing_texture`, `license_timeout`, `gpu_fault`,
`asset_cache_miss`, `unknown`. Closed label sets keep cardinality sane and make the
diagnosis grammar finite.

**Cardinality budget:** free tier allows 10k active series. Design target: 2 shows ×
6 sequences × ~8 shots × 3 tasks is already 288 series per shot-labelled metric. Keep
shot/task labels on the four job metrics only; node-level metrics carry node+pool and
nothing else. Stay under ~4k series to leave headroom.

### 1.2 Logs (Loki push API)
Stream labels — low cardinality only: `{job="renderfarm", service, pool, level}` where
`service` ∈ `render-worker | scheduler | license-server | asset-cache`.

Line format, logfmt so Loki pattern parsers work cleanly:
```
ts=2026-08-27T14:03:11Z level=error service=render-worker node=rn-b14 pool=gpu-b
job_id=J-8841 shot=SQ04_SH042 task=lighting frame=1180 err=oom
msg="CUDA out of memory allocating 6.2GiB for volumetric grid" driver=555.42.02
```
High-cardinality identifiers (`job_id`, `shot`, `frame`, `node`) live **in the line**, not
in stream labels. This is the standard Loki mistake and a Grafana judge will notice it.

### 1.3 Traces (Tempo)
Service `job-submitter`, spans: `submit_job` → `resolve_assets` → `checkout_license` →
`dispatch_to_pool`. Attributes: `shot`, `task`, `pool`, `job_id`. Gives latency attribution
and lets the agent show *where* submission time is going, not just that it is slow.

### 1.4 Alert rules (pre-provisioned, so Watchtower has something real to find)
| Rule | Condition | Severity |
|---|---|---|
| `RenderFailureSpike` | `rate(render_job_failures_total[10m])` above baseline | critical |
| `QueueBacklogGrowing` | `render_queue_depth` rising 20m | warning |
| `LicensePoolExhausted` | `license_features_available == 0` for 5m | critical |
| `NodeThermalThrottle` | `render_node_gpu_temp_celsius > 88` | warning |

---

## 2. The seeded scenario — and why it has a trap

**Scenario `driver-regression-cascade`:**

1. Nodes `rn-b01..rn-b16` in pool `gpu-b` were updated to driver `555.42.02`.
2. On heavy volumetric frames the new driver over-allocates VRAM → intermittent `oom`
   failures, **only on pool `gpu-b`, only on high-memory frames**.
3. The scheduler retries failed frames — back onto the same pool.
4. Each retry checks out a renderer licence. Retry storm drains
   `license_features_available` to 0.
5. **Every pool now stalls waiting on licences.** `LicensePoolExhausted` fires loudest.
6. `SQ04_SH042` lighting, due at Friday 14:00 client review, falls behind.

**The trap:** the loudest alert and the most obvious metric both point at the licence
server. The licence server is a *symptom*. The cause is a driver regression on one pool,
visible only by correlating `render_job_failures_total{reason="oom", pool="gpu-b"}` against
`driver_version` in the log lines.

This is worth the effort for three reasons: the diagnosis is genuinely non-trivial, a naive
single-shot agent gets it wrong, and the demo has a narrative beat — *"the obvious answer is
wrong, and here's how it knows."* Judges remember that.

Second scenario if time allows: `asset-cache-cold-site` (a site cache flush causing
cross-region asset pulls). Third: `quiet-day` — nothing wrong, so the agent must correctly
say "nothing is wrong," which is the failure mode most demos never test.

---

## 3. Domain layer — what the agent reasons about

```python
Show(id, title, delivery_calendar)
Sequence(id, show_id, code)             # SQ04
Shot(id, sequence_id, code)             # SQ04_SH042
Task(id, shot_id, kind)                 # lighting | comp | fx | dmp
RenderJob(id, task_id, pool, priority, frames_total, submitted_at)
RenderNode(hostname, pool, gpu_model, driver_version)
Review(id, show_id, starts_at, shot_ids) # Friday 14:00 client review
```

The `Review` entity is the whole point. It is what turns an infrastructure metric into a
production consequence, and it is the thing no observability tool models. Seed it as static
config; a real deployment reads it from the production tracker.

---

## 4. Contract layer — typed handoffs between stages

Pydantic models. **These are the API of the pipeline.** Every stage boundary is validated;
a stage that cannot produce a valid contract fails loudly rather than passing prose along.

```python
class EvidenceRef(BaseModel):
    """Provenance for one claim. Nothing is stated without one of these."""
    tool: str                  # "query_prometheus"
    query: str                 # the exact PromQL / LogQL
    at: datetime
    summary: str               # what came back, one line

class IncidentSeed(BaseModel):
    firing_alerts: list[str]
    window: tuple[datetime, datetime]
    suspected_pools: list[str]
    evidence: list[EvidenceRef]

class Evidence(BaseModel):
    metrics: list[MetricFinding]
    logs: list[LogFinding]
    traces: list[TraceFinding]
    unavailable: list[str]     # what we could NOT get, named explicitly

class Diagnosis(BaseModel):
    root_cause: str
    mechanism: str             # the causal chain, in order
    confidence: Literal["high", "medium", "low"]
    ruled_out: list[RuledOut]  # claim + the evidence that killed it
    evidence: list[EvidenceRef]

class Forecast(BaseModel):
    """Produced by pure Python. The model never computes these numbers."""
    shot: str
    task: str
    frames_remaining: int
    throughput_fps_per_hour: float
    eta: datetime | None
    review_at: datetime
    slip: timedelta | None
    status: Literal["on_track", "at_risk", "will_miss", "insufficient_data"]
    idle_artists_from: datetime | None

class RemediationStep(BaseModel):
    op: Literal["create_dashboard", "create_alert_rule", "create_annotation"]
    rationale: str
    payload: dict              # validated against the op's schema before apply

class RemediationPlan(BaseModel):
    plan_id: str
    steps: list[RemediationStep]     # allowlisted ops ONLY
    manual_actions: list[str]        # things a human must do (drain pool gpu-b)
    expires_at: datetime

class Verdict(BaseModel):
    """What the operator actually reads."""
    headline: str              # "SQ04_SH042 misses Friday's review by ~9h"
    diagnosis: Diagnosis
    forecasts: list[Forecast]
    plan: RemediationPlan
    briefing_audio_uri: str | None
```

### Two contract rules that carry the quality
1. **`ruled_out` is required, not optional.** Forcing the Diagnostician to state what it
   eliminated and why is what stops it from taking the licence-server bait. A diagnosis with
   an empty `ruled_out` on a multi-signal incident is treated as low confidence.
2. **`unavailable` is required.** A system that silently drops a failed query and answers
   confidently is worse than one that says "Tempo was unreachable, confidence reduced."
   Name the gap.

---

## 5. Persistence

| Store | Holds | Why |
|---|---|---|
| Firestore `runs/{run_id}` | full `Verdict`, timings, token cost | judge sees the last run instantly on cold open |
| Firestore `plans/{plan_id}` | plan + approval state | makes approve single-use and idempotent |
| GCS `briefings/{run_id}.mp3` | audio | signed URL from the UI |

Retention: nothing needs deleting inside the contest window. Do not build lifecycle rules.
