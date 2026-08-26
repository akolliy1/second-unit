#!/usr/bin/env python
"""Run the triage pipeline against the live stack.

    python -m second_unit.run              # Watchtower + Diagnostician
    python -m second_unit.run --stage watchtower
"""
import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

from typing import Optional  # noqa: E402

from .pipeline import (  # noqa: E402
    Approval,
    RunRecord,
    execute_approved_writes,
    run_stage,
)
from .schemas import (  # noqa: E402
    Diagnosis,
    ImpactForecast,
    RemediationPlan,
    WatchtowerReport,
)
from .stages import (  # noqa: E402
    diagnostician,
    impact_forecaster,
    remediation_planner,
    watchtower,
)

WATCHTOWER_TASK = (
    "Triage the render farm now. Something is reportedly wrong with tonight's deliveries. "
    "Establish what is abnormal and who the suspects are. Do not root-cause."
)


def diagnostician_task(report: WatchtowerReport) -> str:
    return (
        "Watchtower has triaged the farm. Its report follows as JSON. Diagnose the root "
        "cause, establish the order of events, and rule out at least one suspect that "
        "merely looks guilty.\n\n"
        f"{report.model_dump_json(indent=2)}"
    )


#: Hours from now to the client review. This is a PROPERTY OF THE SCENARIO, not a date.
#:
#: The first version picked the next calendar Friday and produced `slip: -45h` -- the pass
#: made its deadline by two days and there was nothing to forecast. The stakes have to be
#: tuned to the seeded farm: ~1,350 frames left, ~8.8 frames/min healthy, ~5.1 degraded.
#: That is 2.6h of work at full capacity and 4.4h at current capacity, so a review ~3h out
#: is exactly the interesting case -- comfortably made if the farm were healthy, missed by
#: roughly an hour and a half because it is not.
#:
#: This is also how a real schedule is set: a producer books the review off the healthy
#: estimate, which is precisely why a capacity loss turns into a missed delivery.
REVIEW_HOURS_FROM_NOW = 3.0


#: Fallback only. The real deadline is PUBLISHED BY THE SEEDER as
#: `shot_review_deadline_seconds`, and the forecaster is told to query it. This constant is
#: used only if that metric is missing.
#:
#: Two earlier designs failed and are worth remembering. A fixed "next Friday" gave a 45h
#: deadline and no jeopardy at all. Deriving it at query time from the healthy estimate was
#: circular -- it read the worst shot in the fleet, produced a 7h deadline, and turned the
#: shot we are investigating back into "on track". A delivery deadline is not something to
#: infer from throughput; it is a commitment made in advance. So it belongs in the world,
#: emitted as telemetry, where the agent can read it like any other fact.
REVIEW_HOURS_FROM_NOW = 3.0


def next_review_deadline(hours_from_now: float = REVIEW_HOURS_FROM_NOW) -> str:
    """Fallback deadline, if the seeder's published one cannot be read."""
    from datetime import datetime, timedelta
    target = datetime.now().astimezone() + timedelta(hours=hours_from_now)
    return target.replace(second=0, microsecond=0).isoformat(timespec="minutes")


def published_review_deadline(shot: str = "SH042") -> str:
    """Read the review deadline the production tracker publishes for `shot`.

    This is the honest shape: the agent discovers the commitment from telemetry rather
    than being handed a number by its own harness.
    """
    from datetime import datetime, timezone
    try:
        from . import fleet
        rows = fleet._prom_query(f'shot_review_deadline_seconds{{shot="{shot}"}}')
        if rows:
            ts = float(rows[0]["value"][1])
            return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(
                timespec="minutes")
    except Exception:  # noqa: BLE001
        pass
    return next_review_deadline()


def show(label, value, indent="   "):
    print(f"{indent}{label}: {value}")


# Set from the CLI before main() runs; a list so the closure sees the updated value.
args_review_hours = [REVIEW_HOURS_FROM_NOW]


async def main(stages):
    record = RunRecord()

    # Self-observability. Best-effort by construction: if the push endpoint is missing or
    # failing, the run proceeds and simply is not measured.
    from .observe import RunObserver, observer_enabled
    import uuid
    obs = (RunObserver(uuid.uuid4().hex[:8], model=os.getenv("GEMINI_MODEL", ""))
           if observer_enabled() else None)

    def watch(res):
        """Record one stage. Kept tiny so it can be called from every branch."""
        if obs and res is not None:
            obs.stage(res.stage, res.seconds, len(res.tool_calls), res.ok, res.error)
            obs.tools(res.stage, res.tool_calls)

    if "watchtower" in stages:
        print("\n=== WATCHTOWER — what is on fire ===")
        r = await run_stage(watchtower(), WATCHTOWER_TASK, WatchtowerReport)
        record.stages.append(r); watch(r)
        if not r.ok:
            print(f"  FAILED: {r.error}\n  raw: {r.raw_text[:400]}")
            return record
        out = r.output
        print(f"  {len(r.tool_calls)} tool calls, {r.seconds:.1f}s")
        show("summary", out.summary)
        show("firing alerts", out.firing_alerts or "none")
        show("error signatures", f"{len(out.error_signatures)}")
        for sig in out.error_signatures[:5]:
            print(f"      · {sig[:110]}")
        show("suspects", ", ".join(out.suspect_entities) or "none")
        show("evidence items", len(out.evidence))

    if "diagnostician" in stages:
        wt = record.stage("watchtower")
        if not wt or not wt.ok:
            print("\ndiagnostician skipped: no valid watchtower report")
            return record
        print("\n=== DIAGNOSTICIAN — why ===")
        r = await run_stage(diagnostician(), diagnostician_task(wt.output), Diagnosis)
        record.stages.append(r); watch(r)
        if not r.ok:
            print(f"  FAILED: {r.error}\n  raw: {r.raw_text[:400]}")
            return record
        out = r.output
        print(f"  {len(r.tool_calls)} tool calls, {r.seconds:.1f}s")
        show("ROOT CAUSE", f"{out.root_cause}  [confidence: {out.confidence.value}]")
        print("   causal chain:")
        for i, link in enumerate(out.causal_chain, 1):
            print(f"      {i}. {link}")
        print("   hypotheses:")
        for h in out.hypotheses:
            mark = {"confirmed": "✓", "ruled_out": "✗"}.get(h.verdict, "?")
            print(f"      {mark} [{h.verdict}/{h.confidence.value}] {h.statement[:95]}")
        show("affected shots", ", ".join(out.affected_shots) or "none")
        show("evidence items", len(out.evidence))
        for e in out.evidence[:3]:
            print(f"      · {e.claim[:80]}")
            print(f"        {e.tool}: {e.query[:100]}")

    if "forecaster" in stages:
        dx = record.stage("diagnostician")
        if not dx or not dx.ok:
            print("\nforecaster skipped: no valid diagnosis")
            return record
        deadline = published_review_deadline("SH042")
        print(f"\n=== IMPACT FORECASTER — what it costs (review: {deadline}) ===")
        task = (
            "Forecast the production impact of this diagnosis. The client review deadline "
            f"is {deadline}. Measure the frames remaining and both the current and the "
            "pre-incident baseline completion rate, then call forecast_delivery.\n\n"
            f"{dx.output.model_dump_json(indent=2)}"
        )
        r = await run_stage(impact_forecaster(), task, ImpactForecast)
        record.stages.append(r); watch(r)
        if not r.ok:
            print(f"  FAILED: {r.error}\n  raw: {r.raw_text[:400]}")
            return record
        out = r.output
        print(f"  {len(r.tool_calls)} tool calls, {r.seconds:.1f}s")
        print(f"\n   >>> {out.verdict}\n")
        show("shot", f"{out.shot} / {out.department}")
        show("frames left", out.frames_remaining)
        show("rate", f"{out.current_rate_per_min:.2f}/min now vs "
                     f"{out.baseline_rate_per_min:.2f}/min healthy "
                     f"(−{out.capacity_loss_pct:.0f}% capacity)")
        show("ETA vs deadline", f"{out.eta_iso}  vs  {out.deadline_iso}")
        show("slip", f"{out.slip_hours:+.2f}h  "
                     f"({'MISSES' if not out.makes_deadline else 'makes'} the review)")
        show("crew impact", out.crew_impact)

    if "planner" in stages:
        fc = record.stage("impact_forecaster")
        dx = record.stage("diagnostician")
        if not fc or not fc.ok:
            print("\nplanner skipped: no valid forecast")
            return record
        print("\n=== REMEDIATION PLANNER — proposes, cannot write ===")
        task = (
            "Propose the fix and the write-back for a human to approve.\n\n"
            f"DIAGNOSIS:\n{dx.output.model_dump_json(indent=2)}\n\n"
            f"IMPACT:\n{fc.output.model_dump_json(indent=2)}"
        )
        r = await run_stage(remediation_planner(), task, RemediationPlan)
        record.stages.append(r); watch(r)
        if not r.ok:
            print(f"  FAILED: {r.error}\n  raw: {r.raw_text[:400]}")
            return record
        out = r.output
        print(f"  {len(r.tool_calls)} tool calls, {r.seconds:.1f}s")
        show("REAL FIX", out.fix_recommendation)
        show("risk if ignored", out.risk_if_ignored)
        print("   proposed writes — NOTHING HAS HAPPENED YET:")
        for i, w in enumerate(out.proposed_writes):
            print(f"      [{i}] {w.action}: {w.title}")
            print(f"          tool={w.tool}  reversible={w.reversible}")
            print(f"          {w.rationale}")

    if "execute" in stages:
        pl = record.stage("remediation_planner")
        if not pl or not pl.ok:
            print("\nexecute skipped: no valid plan")
            return record
        approval = Approval(
            approved_by=os.getenv("SECOND_UNIT_OPERATOR", "cli-operator"),
            approved=list(range(len(pl.output.proposed_writes))),
            note="approved via CLI --execute",
        )
        print(f"\n=== REMEDIATION EXECUTOR — {len(approval.approved)} approved write(s) ===")
        from .verify import snapshot
        before = snapshot()          # must be taken BEFORE anything mutates
        r = await execute_approved_writes(pl.output, approval)
        record.stages.append(r); watch(r)
        claimed = []
        if r.ok:
            for wr in (r.output or []):
                mark = "✓" if wr.succeeded else "✗"
                print(f"   {mark} {wr.action}: {wr.detail[:150]}")
                claimed.append(wr.model_dump())
        else:
            print(f"  FAILED: {r.error}\n  raw: {r.raw_text[:400]}")

        # Never take the executor's word for it. See second_unit/verify.py.
        if claimed:
            from .verify import print_report, verify_writes
            tally = print_report(verify_writes(claimed, before))
            if obs:
                obs.write_claims(tally)

    print(f"\n--- {record.total_tool_calls} MCP tool calls across "
          f"{len(record.stages)} stages ---")
    if obs:
        obs.flush()
    return record


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", action="append",
                    choices=("watchtower", "diagnostician", "forecaster",
                             "planner", "execute"),
                    help="run only these stages (repeatable)")
    ap.add_argument("--review-in-hours", type=float, default=REVIEW_HOURS_FROM_NOW,
                    help="hours from now to the client review (default %(default)s). "
                         "See REVIEW_HOURS_FROM_NOW for why this is scenario-derived.")
    ap.add_argument("--execute", action="store_true",
                    help="APPROVE AND PERFORM the proposed write-back. Without this the "
                         "pipeline stops at the proposal, which is the default on purpose.")
    args = ap.parse_args()
    stages = args.stage or ["watchtower", "diagnostician", "forecaster", "planner"]
    args_review_hours[0] = args.review_in_hours
    if args.execute and "execute" not in stages:
        stages.append("execute")
    try:
        asyncio.run(main(stages))
    except KeyboardInterrupt:
        sys.exit(130)
