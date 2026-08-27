"""Function tools: the parts the model must NOT do itself.

The Impact Forecaster's entire value is a number a producer will plan around. So the
arithmetic does not happen in the model. `forecast_delivery` is ordinary Python: the agent
gathers the inputs from Grafana and calls this to get the answer, which means the ETA is
reproducible, auditable, and cannot be a plausible-sounding hallucination.

This is the concrete form of the architecture rule "the model decides what it finds, code
decides what happens next", and it is also what makes the forecast defensible to a judge
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

    # A rate the farm cannot physically produce is refused outright.
    #
    # The inverted-baseline check below catches "current >= baseline". It does NOT catch
    # both numbers being nonsense: a counter reset produced baseline 1456.8 and current
    # 1248.7 frames/min on a twelve-node farm, the inversion test passed because 1456 > 1248,
    # and a confident forecast was built on both. The farm's ceiling is about 190/min.
    CEILING = 200.0
    for label, value in (("current", current_rate_frames_per_min),
                         ("baseline", baseline_rate_frames_per_min)):
        if value > CEILING:
            return {
                "error": "implausible_rate",
                "detail": (f"{label} rate of {value:.0f} frames/min exceeds anything this "
                           f"farm can produce (~{CEILING:.0f}/min across twelve nodes). "
                           f"Almost certainly a counter reset inside the rate window."),
                "makes_deadline": None,
                "recommendation": ("Re-measure over a window that excludes the reset. Do not "
                                   "forecast from this, and do not invent a mechanism to "
                                   "explain it, no such capacity exists."),
            }

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
            "error": "current rate is zero or negative, the pass is stalled, not slow",
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
    current_rate_frames_per_min: float = 0.0,
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
        baseline_rate_frames_per_min: The pass's HEALTHY rate, all nodes working. This must
            be the pre-incident rate, NOT the rate you can measure right now.
        nodes_on_the_pass: How many render nodes are assigned to this pass.
        nodes_to_drain: How many are being taken out of rotation (usually 1).
        review_deadline_iso: The client review time, ISO 8601.
        current_rate_frames_per_min: The rate measured NOW, while degraded. Pass it and the
            function can catch the mistake below; omit it and it cannot.

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

    # The mistake this catches, observed for real: asked "what if we drain render-07?", the
    # agent had no healthy baseline to hand, substituted the CURRENT degraded rate, and the
    # function dutifully reported that draining a dead node would make delivery slightly
    # WORSE. That is arithmetically consistent and physically impossible, and it is exactly
    # the class of answer this project exists to refuse.
    if (current_rate_frames_per_min > 0
            and baseline_rate_frames_per_min <= current_rate_frames_per_min * 1.05):
        return {
            "error": "baseline_is_not_a_baseline",
            "detail": (
                f"a healthy baseline of {baseline_rate_frames_per_min:.1f}/min is not "
                f"credibly above the degraded rate of {current_rate_frames_per_min:.1f}/min. "
                f"You have almost certainly passed the CURRENT rate as the baseline."),
            "recommendation": (
                "Measure the healthy rate over a window BEFORE the fault began, or use the "
                "pre-incident baseline supplied in your context. Do not model a fix against "
                "the broken farm's own throughput, the result is that removing a dead node "
                "appears to reduce capacity, which is impossible."),
            "makes_deadline": None,
        }

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


def build_incident_dashboard(
    shot: str,
    faulty_node: str,
    department: str = "lighting",
    prometheus_uid: str = "grafanacloud-prom",
) -> dict:
    """Build a valid Grafana dashboard for an incident. Returns JSON ready for update_dashboard.

    Call this and pass the result straight through as the `dashboard` argument. Do not write
    dashboard JSON yourself.

    Why this exists: `update_dashboard` declares NO required fields, so there is no schema to
    follow, and the agent has to invent Grafana's panel/gridPos/targets structure from
    nothing. It failed on every attempt, four separate runs, while reporting success. The
    model should decide WHAT to show; the shape of a Grafana dashboard is not a judgement
    call, it is a spec, and specs belong in code.

    Args:
        shot: The affected shot, e.g. SH042.
        faulty_node: The node at fault, e.g. render-07.
        department: The affected department, used in the queue panel.
        prometheus_uid: Datasource UID; the stack default is usually right.

    Returns:
        A dict to pass as `dashboard`. Pair it with overwrite=true and a folderUid.
    """
    def panel(pid, title, expr, x, y, w=12, h=8, unit=""):
        return {
            "id": pid, "type": "timeseries", "title": title,
            "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "datasource": {"type": "prometheus", "uid": prometheus_uid},
            "fieldConfig": {"defaults": {"unit": unit, "custom": {"lineWidth": 2,
                            "fillOpacity": 8}}, "overrides": []},
            "targets": [{"refId": "A", "expr": expr,
                         "datasource": {"type": "prometheus", "uid": prometheus_uid}}],
        }

    return {
        # No uid: Grafana assigns one on create. Passing a uid we invented risks colliding
        # with an existing dashboard and silently overwriting someone else's work.
        "title": f"Incident · {shot} · {faulty_node}",
        "tags": ["second-unit", "incident", shot.lower()],
        "timezone": "browser",
        "schemaVersion": 39,
        "refresh": "30s",
        "time": {"from": "now-3h", "to": "now"},
        "panels": [
            panel(1, f"{faulty_node}, GPU ECC errors (the cause)",
                  f'increase(render_node_gpu_ecc_errors_total{{node="{faulty_node}"}}[5m])',
                  0, 0),
            panel(2, f"{faulty_node}, GPU temperature vs peers",
                  "render_node_gpu_temp_celsius", 12, 0, unit="celsius"),
            panel(3, f"{department} queue depth",
                  f'render_queue_depth{{queue="{department}"}}', 0, 8),
            panel(4, f"{shot}, frames remaining vs completion rate",
                  f'shot_frames_remaining{{shot="{shot}"}}', 12, 8),
            panel(5, "Frame failures by node and reason",
                  "sum by (node, reason) (increase(render_frames_failed_total[10m]))",
                  0, 16, w=24),
        ],
    }


def idle_cost(artists_idle: int, slip_hours: float, hourly_rate_usd: float = 85.0) -> dict:
    """Convert a schedule slip into the number a producer actually argues about.

    Rates vary wildly by studio and region, so this returns the assumption alongside the
    figure: an unlabelled cost estimate is worse than none.
    """
    if slip_hours <= 0:
        return {"cost_usd": 0.0, "note": "no slip, no idle cost"}
    cost = artists_idle * slip_hours * hourly_rate_usd
    return {
        "cost_usd": round(cost, 2),
        "artists_idle": artists_idle,
        "slip_hours": round(slip_hours, 2),
        "assumed_hourly_rate_usd": hourly_rate_usd,
        "note": "assumed rate, state it whenever you quote the figure",
    }
