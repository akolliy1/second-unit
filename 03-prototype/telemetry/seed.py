#!/usr/bin/env python
"""
Seed the render-farm incident into Grafana Cloud.

    # 90 minutes of history ending now, incident starting 40 minutes ago
    python seed.py --backfill 90 --incident-at 40

    # then keep it alive while you demo
    python seed.py --live

    # see what would be sent, push nothing
    python seed.py --backfill 20 --dry-run

Backfill is safe: Mimir rejects samples older than its reject_old_samples_max_age
(hours, not minutes), and we write each series strictly ascending. Re-running backfill
*after* a live run can produce out-of-order rejections for the overlap; those are counted
and reported rather than fatal, because a partial reseed is still a usable demo.
"""
import argparse
import os
import sys
import time

from dotenv import load_dotenv

from push import LokiWriter, PromWriter
from scenario import Farm

load_dotenv()


def batched(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def group_logs(logs, ts_ms):
    """[(labels, line)] -> [(labels, [(ts_ns, line)])], one stream per label set."""
    ts_ns = ts_ms * 1_000_000
    streams = {}
    for labels, line in logs:
        key = tuple(sorted(labels.items()))
        streams.setdefault(key, (labels, []))[1].append((ts_ns, line))
        ts_ns += 1000  # keep entries distinct and ordered within the stream
    return list(streams.values())


def run(args):
    dry = args.dry_run
    prom = loki = None
    if not dry:
        missing = [v for v in ("PROM_REMOTE_WRITE_URL", "PROM_USER", "LOKI_PUSH_URL",
                               "LOKI_USER", "GRAFANA_CLOUD_TOKEN")
                   if not os.environ.get(v)]
        if missing:
            sys.exit(f"missing in .env: {', '.join(missing)}\n"
                     f"see SETUP-NOW.md section A steps 3 and 4")
        prom, loki = PromWriter(), LokiWriter()

    farm = Farm(seed=args.seed)
    # Anchor the published review deadlines to when this cycle's incident begins.
    farm.cycle_start_ms = int(time.time() * 1000) - (
        args.incident_at * 60_000 if (args.backfill or args.warm) else 0)
    sent_samples = sent_lines = rejected = 0
    minute_ms = 60_000

    if args.backfill and not args.i_know_this_overlaps:
        # Backfilling over series that already hold data interleaves a fresh Farm's
        # low counter values with the previous run's high ones. Mimir accepts them
        # (its out-of-order window is generous), Prometheus reads the result as repeated
        # counter resets, and rate() then returns physically impossible numbers -- we
        # measured 742 frames/min on a 12-node farm this way. Use --warm to RESUME
        # instead; only re-backfill into a clean series.
        print("NOTE: --backfill writes over any existing samples in this window.\n"
              "      If you have seeded before, use `--warm N --live` to resume instead;\n"
              "      overlapping backfills corrupt rate() with false counter resets.\n"
              "      Pass --i-know-this-overlaps to proceed anyway.\n")
        if os.environ.get("SEED_NONINTERACTIVE"):
            sys.exit("refusing to backfill without --i-know-this-overlaps")
    if args.backfill:
        start_ms = int(time.time() * 1000) - args.backfill * minute_ms
        print(f"backfilling {args.backfill}m, incident begins at "
              f"t-{args.incident_at}m ({args.backfill - args.incident_at}m of healthy "
              f"baseline first)")
        for i in range(args.backfill):
            ts = start_ms + i * minute_ms
            # negative minute == before the incident
            minute = i - (args.backfill - args.incident_at)
            series, logs = farm.tick(minute, ts)
            sent_samples += len(series)
            sent_lines += len(logs)
            if dry:
                if i % 20 == 0:
                    print(f"  t{minute:+04d}m  {len(series):3d} samples  "
                          f"{len(logs):2d} log lines")
                continue
            try:
                for chunk in batched(series, 500):
                    prom.write(chunk)
                loki.write(group_logs(logs, ts))
            except RuntimeError as e:
                rejected += 1
                if rejected <= 3:
                    print(f"  ! t{minute:+04d}m rejected: {e}", file=sys.stderr)
            if i % 10 == 0:
                print(f"  t{minute:+04d}m ok", flush=True)
        print(f"backfill done: {sent_samples} samples, {sent_lines} lines, "
              f"{rejected} rejected batches")

    if args.warm:
        # Rebuild cumulative state without pushing. The Farm is deterministic given its
        # seed, so replaying N ticks silently reproduces exactly the counter values a
        # completed backfill left in Mimir. Without this, a fresh --live process starts
        # its counters at zero, Prometheus reads that as a counter reset, and
        # shot_frames_remaining jumps back up -- which would wreck every rate() and every
        # forecast the agent makes across the boundary.
        print(f"warming {args.warm} ticks to match the existing backfill (no pushes)")
        for i in range(args.warm):
            farm.tick(i - (args.warm - args.incident_at), 0)

    if args.live:
        minute = args.incident_at if (args.backfill or args.warm) else 0
        if args.start_minute is not None:
            # A NEGATIVE start gives a healthy lead-in before the fault, which the
            # forecaster needs: it measures the pre-incident baseline rate from history,
            # and without a healthy window there is nothing to compare the degraded rate
            # against. Starting live at -25 means the farm looks normal for 25 minutes and
            # then breaks, with no backfill and therefore no overlapping-counter
            # corruption.
            minute = args.start_minute
            farm.cycle_start_ms = int(time.time() * 1000) - minute * 60_000
        if args.loop:
            farm.cyclic = True
            print(f"loop mode: fault at t+0, repair at t+{farm.REPAIR_AT}m, "
                  f"restart every {farm.CYCLE_MINUTES}m")
        print(f"live mode, starting at t{minute:+d}m, ctrl-c to stop")
        while True:
            ts = int(time.time() * 1000)
            series, logs = farm.tick(minute, ts)
            if dry:
                print(f"  t{minute:+04d}m  {len(series)} samples  {len(logs)} lines")
            else:
                try:
                    for chunk in batched(series, 500):
                        prom.write(chunk)
                    loki.write(group_logs(logs, ts))
                    print(f"  t{minute:+04d}m ok  queue_lighting="
                          f"{farm.queue_depth('lighting', minute):.0f}  "
                          f"SH042_remaining={farm.remaining['SH042']}", flush=True)
                except RuntimeError as e:
                    print(f"  ! t{minute:+04d}m {e}", file=sys.stderr)
            minute += 1
            if args.loop and minute >= farm.CYCLE_MINUTES:
                # Restart the arc so a judge arriving at any hour finds a live incident.
                # A fresh Farm resets the cumulative counters, which Prometheus reads as a
                # counter reset -- correct and expected here, because it genuinely is a new
                # render job. rate() and increase() handle resets; instant gauges just
                # start over.
                print(f"  --- cycle complete, restarting the scenario ---", flush=True)
                farm = Farm(seed=args.seed)
                farm.cyclic = True
                farm.cycle_start_ms = int(time.time() * 1000)
                minute = 0
            time.sleep(60 / max(args.speed, 0.01))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backfill", type=int, default=0,
                   help="minutes of history to write, ending now")
    p.add_argument("--incident-at", type=int, default=40,
                   help="how many minutes ago the GPU fault began (default 40)")
    p.add_argument("--live", action="store_true",
                   help="keep emitting in real time after the backfill")
    p.add_argument("--speed", type=float, default=1.0,
                   help="live speed multiplier; 6 = one scenario-minute every 10s")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--i-know-this-overlaps", action="store_true",
                   help="proceed with --backfill even though it may overlap existing "
                        "samples and corrupt rate() with false counter resets")
    p.add_argument("--start-minute", type=int, default=None,
                   help="minute of the scenario to begin live streaming at; negative "
                        "values give a healthy lead-in before the fault (e.g. -25)")
    p.add_argument("--loop", action="store_true",
                   help="cycle the scenario forever: fault, cascade, repair, restart. Use "
                        "this for the hosted demo so a judge always finds a live incident.")
    p.add_argument("--warm", type=int, default=0,
                   help="silently replay N ticks first, to resume a backfill that ran in "
                        "an earlier process without resetting the counters")
    p.add_argument("--dry-run", action="store_true", help="print, push nothing")
    args = p.parse_args()
    if not args.backfill and not args.live:
        p.error("nothing to do: pass --backfill N and/or --live")
    if args.warm and args.backfill:
        p.error("--warm resumes a PREVIOUS backfill; do not combine it with --backfill")
    if args.warm and args.incident_at > args.warm:
        p.error("--incident-at must be <= --warm")
    if args.incident_at > args.backfill and args.backfill:
        p.error("--incident-at must be <= --backfill (need healthy baseline first)")
    try:
        run(args)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
