# Baseline dashboards

Two hand-built dashboards for the Vancouver render farm, checked in as JSON and provisioned
to the Grafana Cloud stack over its HTTP API.

They exist for two reasons. A stack with nothing in it does not read as a studio's
observability, and the agent's investigation is less convincing if its written-back dashboard
is the only thing in the tenant. These are what a real facility would already have had open
before anyone noticed a problem, so when the agent writes its own dashboard, it lands
*beside* the daily drivers rather than into a vacuum.

Every panel queries a metric or stream that is actually in the stack, and every query in both
files has been run through the API and confirmed to return data. `provision.py --verify` does
that check on demand, so it can be re-run whenever the seed data changes.

## `farm-overview.json`, "Render Farm, Vancouver"

The pipeline TD's daily driver. `uid: second-unit-farm-vancouver`, 11 panels.

Top row is the at-a-glance state of the farm: throughput in frames/min, frame failure rate as
a percentage, farm-wide uncorrectable ECC errors over the last 15 minutes, and the depth of
the most backed-up queue. Below that, throughput split by department and failure rate split
by reason (`gpu_fault` vs `transient`, the distinction between hardware and noise), then
per-node GPU temperature and per-node utilization across all 12 nodes, then queue depth per
queue and ECC errors per node. The last panel is the live `render-scheduler` log, filtered to
errors and warnings.

The per-node panels are the point: a single node running hot, or a single node accumulating
ECC errors while its eleven peers sit flat, is the shape of the incident.

## `delivery-status.json`, "Delivery Status, Current Shots"

The producer's view. `uid: second-unit-delivery-status`, 9 panels.

Top row carries one number per shot: frames remaining, current completion rate, and projected
ETA. ETA is `frames_remaining / completion_rate`, so it is derived from the same data rather
than asserted, and it turns red past four hours. Below, the same three quantities over time, 
the burndown per shot, the rate per shot, and the ETA curve, which bends upward as a delivery
date moves away from you. Alongside them, average frame duration by department, which is the
mechanism by which a hardware fault reaches a schedule. Bottom row: failed frames per shot
(work a shot has to redo) and queue depth (the early warning that shows up before a burndown
flattens).

## Provisioning

Credentials are read from `../.env`, `GRAFANA_URL` and `GRAFANA_SERVICE_ACCOUNT_TOKEN`, which
needs `dashboards:write`. The token is never printed and never written to a file.

```bash
cd 03-prototype/dashboards

# push both dashboards
../spike/.venv/bin/python provision.py

# push both, then read each one back and run every panel query against the stack
../spike/.venv/bin/python provision.py --verify
```

`--verify` prints one line per panel with the series and datapoint count it got back, and exits
non-zero if any panel's query returns nothing. That exit code is the whole point: a panel
querying a metric that does not exist is worse than no panel.

Both dashboards carry a stable `uid` and are POSTed to `/api/dashboards/db` with
`overwrite: true`, so the script is safe to re-run, it updates in place instead of
duplicating. Grafana only cuts a new version when the JSON has actually changed.

## Notes for anyone editing these

Two traps from `../telemetry/README.md`, both of which these files are written around:

**Prefer range queries.** Every Prometheus target here sets `"range": true` and
`"instant": false`. A finished backfill's newest sample keeps aging, and once it is more than
five minutes old an instant query returns `{"data": []}` while the data is plainly there. The
same applies to the verifier, which uses `query_range` throughout.

**`|= "Xid 48"` matches nothing.** The real line reads
`NVRM: Xid (PCI:0000:41:00): 48, pid=0, GPU has fallen off the bus. uncorrectable ECC error
encountered on GPU 0`: the number is not adjacent to the word. Match `"uncorrectable ECC"`.
The logs panel here avoids the problem entirely by filtering on the `level` stream label
rather than on message text.

Shot and department are 1:1 in the seeded data (SH041=comp, SH042=lighting, SH043=fx), so
"by shot" and "by department" produce the same three series. `gpu_fault` failures only ever
appear on `render-07`; the other eleven nodes emit `transient` only.
