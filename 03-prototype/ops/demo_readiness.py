#!/usr/bin/env python
"""Is the farm in a state worth recording? Run this before every take.

The scenario cycles, so "does the demo look good" is a function of the clock:

    t+0     fault begins            t+170  SH042's review deadline
    t+180   bad node drained        t+240  cycle restarts, all shots reset

Between t+170 and t+240 the deadline has passed and the node is repaired, so the verdict
correctly reports a healthy farm — accurate, and useless on camera. We wasted a take
learning that. This tells you where in the cycle you are and whether to roll.
"""
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from second_unit import fleet                                   # noqa: E402
from second_unit.run import published_review_deadline           # noqa: E402
from second_unit import run as run_mod                          # noqa: E402

GOOD_FROM, GOOD_TO = 20, 150          # minutes into the cycle worth recording


def main() -> int:
    ok = True
    print("=== seeder ===")
    try:
        rows = fleet._prom_query("shot_review_deadline_seconds")
    except Exception as exc:  # noqa: BLE001
        print(f"  CANNOT QUERY PROMETHEUS: {type(exc).__name__}: {exc}")
        return 2
    if not rows:
        print("  NO shot_review_deadline_seconds — the seeder is not running with --loop")
        return 2

    deadline_ts = float(next(r for r in rows if r["metric"].get("shot") == "SH042")
                        ["value"][1])
    cycle_start = deadline_ts - 170 * 60
    minute = (time.time() - cycle_start) / 60.0
    phase = ("pre-fault" if minute < 0 else
             "fault building" if minute < GOOD_FROM else
             "DEGRADED — record now" if minute <= GOOD_TO else
             "past the review deadline" if minute < 180 else
             "repaired" if minute < 240 else "restarting")
    print(f"  cycle minute t{minute:+.0f}m   phase: {phase}")
    print(f"  review deadline: {datetime.fromtimestamp(deadline_ts).strftime('%H:%M')}"
          f"   cycle restarts at t+240m "
          f"({datetime.fromtimestamp(cycle_start + 240*60).strftime('%H:%M')})")
    if not (GOOD_FROM <= minute <= GOOD_TO):
        ok = False
        if minute < GOOD_FROM:
            wait = GOOD_FROM - minute
            print(f"  WAIT {wait:.0f} more minutes — the slip has not appeared yet")
        else:
            nxt = (240 - minute) + GOOD_FROM
            print(f"  TOO LATE in this cycle. Next good window in ~{nxt:.0f} minutes")

    print("\n=== the shots ===")
    dl = published_review_deadline("SH042")
    if run_mod.deadline_fallback_reason:
        print(f"  !! deadline is a PLACEHOLDER: {run_mod.deadline_fallback_reason}")
        ok = False
    shots = fleet.fleet_status(dl)
    if not shots:
        print("  no shot data:", fleet.last_error)
        return 2
    for s in shots:
        slip = f"{s.slip_hours:+.2f}h" if s.slip_hours is not None else "   —  "
        print(f"  {s.shot} {s.department:9} {s.status:9} left={s.frames_remaining:>5} "
              f"rate={s.rate_per_min:>5.2f}/min slip={slip}")
    sh042 = next((s for s in shots if s.shot == "SH042"), None)
    if not sh042 or sh042.status not in ("critical", "at_risk"):
        ok = False
        print("  SH042 is NOT slipping — there is no story on screen right now")

    print("\n=== the fault itself ===")
    ecc = fleet._prom_query(
        'sum by (node) (increase(render_node_gpu_ecc_errors_total[30m])) > 0')
    nodes = [r["metric"].get("node") for r in ecc]
    print(f"  nodes with ECC errors (30m): {nodes or 'NONE — the node is repaired'}")
    if not nodes:
        ok = False
    q = fleet._prom_query('render_queue_depth{queue="lighting"}')
    if q:
        print(f"  lighting queue depth: {float(q[0]['value'][1]):.0f}")

    print("\n" + ("READY — roll." if ok else "NOT READY — see above."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
