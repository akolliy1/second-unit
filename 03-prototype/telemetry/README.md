# The world the agent investigates

`seed.py` writes a VFX render farm into Grafana Cloud: 12 nodes, three shots in flight, and
one GPU that starts throwing uncorrectable ECC errors 40 minutes before the demo starts.

```bash
../.venv/bin/pip install -r requirements.txt      # requests, cramjam, python-dotenv
../.venv/bin/python seed.py --backfill 20 --dry-run   # no creds needed, prints volumes
../.venv/bin/python seed.py --backfill 90 --incident-at 40   # real history
../.venv/bin/python seed.py --live                # keep it moving during the demo
```

## Why it is shaped this way
An investigation demo is only as good as the thing being investigated. Three constraints:

**The chain has to be walkable.** Every link is in a metric or a log, and consecutive links
share labels (`node` → `shot` → `department` → `queue`), so an agent that pivots on labels
can get from a GPU fault to a missed delivery without being told the answer.

| t | what appears | where |
|---|---|---|
| +0m | `Xid 48` / uncorrectable ECC on `render-07`, GPU temp climbing | Loki + `render_node_gpu_ecc_errors_total` |
| +5m | frames on `render-07` fail with exit 139 and requeue | `render_frames_failed_total{reason="gpu_fault"}` |
| +8m | retries crowd the lighting queue; it climbs past its threshold | `render_queue_depth{queue="lighting"}` |
| +12m | farm-wide throughput falls; frame duration inflates | `render_frames_completed_total`, `render_frame_duration_seconds` |

**It must not be trivial.** The asset pipeline emits texture-cache-miss warnings the whole
time, loud, plausible, and irrelevant. They start *before* the incident does, so an agent
that pattern-matches on "lots of warnings" reaches the wrong conclusion. That decoy is the
difference between a demo that proves reasoning and one that proves grep.

**It has to end in money.** Measured over a 150-minute run: healthy throughput ~10.1
frames/min, degraded ~5.5, a **46% loss**. With `shot_frames_remaining{shot="SH042"}` at
~1,600, the lighting pass goes from a 2.7h ETA to 5.0h: **it slips 2.3 hours past the client
review.** That sentence is the Potential Impact score, and it is derived, not asserted.

## Verified in the stack, 2026-08-26
Seeded 90 minutes (`--backfill 90 --incident-at 40`): **6,390 samples, 511 log lines, 0
rejected**. `verify_seed.py` then re-reads it **through MCP**, the same path the agent
uses, and all 7 checks pass: the bad node is identifiable from ECC alone, failures localize
to it, the lighting queue is backed up, throughput has dropped, `shot_frames_remaining` is
queryable, the root-cause log line is there, and so is the decoy.

Proving it via Grafana's HTTP API would only prove the data landed. Going through MCP proves
the agent can reach it, which is the only claim worth making.

## Three traps this cost us, so you don't pay twice

**1. A finished backfill goes stale, and instant queries silently return nothing.**
Seeding 90 minutes takes wall-clock minutes, so the newest sample is already several
minutes old, and it keeps aging. Prometheus's instant lookback is 5 minutes, so
`render_queue_depth` at `now` returns `{"data": []}` while the data is plainly there. Worse,
`sum(rate(...[10m]))` *passed and then failed* minutes later with no change to the data.
Fix: keep `seed.py --live` running during any demo, and prefer range queries in checks.

**2. `|= "Xid 48"` never matches.** A real Xid line reads
`NVRM: Xid (PCI:0000:41:00): 48, pid=0,...`, the number is not adjacent to the word. The
obvious filter finds nothing and looks like missing data. Match `"uncorrectable ECC"` or
`|~ "Xid.*48"`. Keep this: **the agent will hit the same trap**, and watching it recover is
a better demo than watching it guess right first time.

**3. `--warm` exists because restarting live seeding resets the counters.** A fresh process
starts its cumulative counters at zero; Prometheus reads that as a counter reset and
`shot_frames_remaining` jumps back to full, wrecking every rate and every forecast across
the boundary. `--warm 90` silently replays 90 deterministic ticks to rebuild state, then
streams on from there. Verified: it resumed at `SH042_remaining=1619`, the exact value the
scenario predicts for t+40m.

```bash
# resume a backfill that ran in an earlier process, without a counter reset
python seed.py --warm 90 --incident-at 40 --live
```

## Notes
- `push.py` hand-rolls the Prometheus remote_write protobuf (~60 lines, no build step) and
  snappy-compresses it with `cramjam`. Labels are sorted before encoding. Mimir rejects
  unsorted label sets.
- Backfill is fine within Mimir's old-sample window (hours). Re-running a backfill *after* a
  live run can produce out-of-order rejections on the overlap; those are counted and printed,
  not fatal, because a partial reseed still demos.
- Counters are cumulative and never decrease, which is what makes `rate()` meaningful.
