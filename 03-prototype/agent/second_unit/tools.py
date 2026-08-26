"""Function tools: the parts the model must NOT do itself.

The Impact Forecaster's entire value is a number a producer will plan around. So the
arithmetic does not happen in the model. `forecast_delivery` is ordinary Python: the agent
gathers the inputs from Grafana and calls this to get the answer, which means the ETA is
reproducible, auditable, and cannot be a plausible-sounding hallucination.

This is the concrete form of the architecture rule "the model decides what it finds, code
decides what happens next" — and it is also what makes the forecast defensible to a judge
who asks where the number came from.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional


def forecast_delivery(
    frames_remaining: int,
    current_rate_frames_per_min: float,
    baseline_rate_frames_per_min: float,
    review_deadline_iso: str,
) -> dict:
    """Work out whether a render pass makes its client review, and by how much it misses.

    Call this instead of doing the arithmetic yourself. Pass the values you measured from
    Prometheus; the returned numbers are authoritative and safe to quote directly.

    Args:
        frames_remaining: Frames left in the pass, from shot_frames_remaining.
        current_rate_frames_per_min: Completion rate now, from rate() over the last 15m.
        baseline_rate_frames_per_min: The healthy rate, measured BEFORE the incident.
        review_deadline_iso: The client review time, ISO 8601 (e.g. 2026-08-28T14:00:00+01:00).

    Returns:
        A dict with eta_iso, hours_remaining, slip_hours, makes_deadline,
        capacity_loss_pct, and hours_if_healthy. slip_hours is positive when late.
    """
    now = datetime.now(timezone.utc)

    # Plausibility gate. A counter reset -- which happens whenever the render job restarts,
    # or when a backfill overlaps existing series -- makes rate() return a value with no
    # physical meaning. We measured 742 frames/min on a 12-node farm this way, and the
    # model, handed that number, invented farm auto-scaling to explain it and reversed its
    # own verdict. Refuse the input instead of forecasting from it: a stated "I do not
    # trust this measurement" is worth far more than a confident wrong ETA.
    if (
        baseline_rate_frames_per_min > 0
        and current_rate_frames_per_min > baseline_rate_frames_per_min * 1.5
    ):
        return {
            "error": "implausible_rate",
            "detail": (
                f"current rate {current_rate_frames_per_min:.1f}/min exceeds the healthy "
                f"baseline {baseline_rate_frames_per_min:.1f}/min by more than 50%. A "
                f"degraded farm cannot outperform a healthy one, so this measurement is "
                f"almost certainly a counter reset in the rate() window, not real "
                f"throughput."
            ),
            "makes_deadline": None,
            "recommendation": (
                "Do NOT forecast from this. Re-measure over a window that excludes the "
                "reset, or report that throughput could not be measured reliably. Do not "
                "invent a mechanism (extra capacity, auto-scaling, a faster queue) to "
                "explain the number -- no such mechanism exists on this farm."
            ),
        }

    if current_rate_frames_per_min <= 0:
        return {
            "error": "current rate is zero or negative — the pass is stalled, not slow",
            "makes_deadline": False,
            "frames_remaining": frames_remaining,
            "recommendation": "treat as a stall: nothing completes until the fault clears",
        }

    hours_remaining = frames_remaining / current_rate_frames_per_min / 60.0
    eta = now + timedelta(hours=hours_remaining)

    try:
        deadline = datetime.fromisoformat(review_deadline_iso)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
    except ValueError:
        return {"error": f"could not parse review_deadline_iso: {review_deadline_iso!r}"}

    slip_hours = (eta - deadline).total_seconds() / 3600.0

    healthy_hours: Optional[float] = None
    capacity_loss = None
    if baseline_rate_frames_per_min > 0:
        healthy_hours = frames_remaining / baseline_rate_frames_per_min / 60.0
        capacity_loss = (
            1 - current_rate_frames_per_min / baseline_rate_frames_per_min
        ) * 100.0

    return {
        "eta_iso": eta.isoformat(timespec="minutes"),
        "hours_remaining": round(hours_remaining, 2),
        "hours_if_healthy": round(healthy_hours, 2) if healthy_hours else None,
        "slip_hours": round(slip_hours, 2),
        "makes_deadline": slip_hours <= 0,
        "capacity_loss_pct": round(capacity_loss, 1) if capacity_loss is not None else None,
        "frames_remaining": frames_remaining,
        "deadline_iso": deadline.isoformat(timespec="minutes"),
    }


def forecast_after_remediation(
    frames_remaining: int,
    baseline_rate_frames_per_min: float,
    nodes_on_the_pass: int,
    nodes_to_drain: int,
    review_deadline_iso: str,
) -> dict:
    """Does the pass make its review IF the faulty node is drained now?

    The plan already says "drain render-07". The next question a producer asks is "so does
    that fix it?", and answering it turns a recommendation into a decision with a number
    attached. This is arithmetic, not judgement, so it lives here: capacity after the fix is
    the healthy per-node rate times the nodes that remain.

    Note this is deliberately CONSERVATIVE. It assumes the remaining nodes run at their
    healthy rate and nothing else improves -- no requeue backlog clearing faster, no frame
    times recovering beyond baseline. If the honest answer is "still misses", we would
    rather say so than flatter the fix.

    Args:
        frames_remaining: Frames left in the pass.
        baseline_rate_frames_per_min: The pass's healthy rate, all nodes working.
        nodes_on_the_pass: How many render nodes are assigned to this pass.
        nodes_to_drain: How many are being taken out of rotation (usually 1).
        review_deadline_iso: The client review time, ISO 8601.

    Returns:
        eta_iso, slip_hours, makes_deadline, rate_after_fix, minutes_saved_vs_now.
    """
    from datetime import datetime, timedelta, timezone

    if nodes_on_the_pass <= 0 or nodes_to_drain >= nodes_on_the_pass:
        return {
            "error": "no capacity left after draining",
            "detail": (f"{nodes_to_drain} of {nodes_on_the_pass} nodes would leave the pass "
                       f"with nothing to render on"),
            "makes_deadline": False,
        }
    if baseline_rate_frames_per_min <= 0:
        return {"error": "baseline rate must be positive to model the fix"}

    per_node = baseline_rate_frames_per_min / nodes_on_the_pass
    rate_after = per_node * (nodes_on_the_pass - nodes_to_drain)
    hours = frames_remaining / rate_after / 60.0

    now = datetime.now(timezone.utc)
    eta = now + timedelta(hours=hours)
    try:
        deadline = datetime.fromisoformat(review_deadline_iso)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
    except ValueError:
        return {"error": f"could not parse review_deadline_iso: {review_deadline_iso!r}"}

    slip = (eta - deadline).total_seconds() / 3600.0
    return {
        "rate_after_fix": round(rate_after, 2),
        "eta_iso": eta.isoformat(timespec="minutes"),
        "hours_remaining": round(hours, 2),
        "slip_hours": round(slip, 2),
        "makes_deadline": slip <= 0,
        "margin_minutes": round(-slip * 60),
        "nodes_after_fix": nodes_on_the_pass - nodes_to_drain,
        "assumption": ("remaining nodes run at their healthy per-node rate; no other "
                       "recovery assumed"),
    }


def idle_cost(artists_idle: int, slip_hours: float, hourly_rate_usd: float = 85.0) -> dict:
    """Convert a schedule slip into the number a producer actually argues about.

    Rates vary wildly by studio and region, so this returns the assumption alongside the
    figure — an unlabelled cost estimate is worse than none.
    """
    if slip_hours <= 0:
        return {"cost_usd": 0.0, "note": "no slip, no idle cost"}
    cost = artists_idle * slip_hours * hourly_rate_usd
    return {
        "cost_usd": round(cost, 2),
        "artists_idle": artists_idle,
        "slip_hours": round(slip_hours, 2),
        "assumed_hourly_rate_usd": hourly_rate_usd,
        "note": "assumed rate — state it whenever you quote the figure",
    }
