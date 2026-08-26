"""Fleet-wide triage, in Python, with no model involved.

The console showed one verdict for one shot, which quietly assumed there is only ever one
thing on fire. A producer's first question is "what else needs me?", and answering it with
an agent per shot would triple the cost of every run to tell us that two of three shots are
fine.

So the sweep is deterministic: one Prometheus query per series, arithmetic in Python, every
shot every time. The agent is then spent where it earns its keep — the deep investigation of
the shot that is actually slipping. That is also how an ops team works: cheap check across
everything, expensive attention on the exception.

Because this never asks a model anything, the strip cannot hallucinate a shot, invent an
ETA, or disagree with itself between runs.
"""
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

#: Slip thresholds, in hours, for the status label a producer scans.
AT_RISK_HOURS = 0.0        # ETA past the deadline at all -> at risk
CRITICAL_HOURS = 1.0       # more than an hour late -> critical


@dataclass
class ShotStatus:
    shot: str
    department: str
    frames_remaining: int
    rate_per_min: float
    eta_iso: Optional[str]
    deadline_iso: str
    slip_hours: Optional[float]
    status: str                        # critical | at_risk | on_track | done | unknown
    note: str = ""

    @property
    def is_exception(self) -> bool:
        return self.status in ("critical", "at_risk")


#: Grafana's datasource proxy, authenticated with the service account token.
#:
#: The first version queried Grafana Cloud's Prometheus endpoint directly with the push
#: credentials -- which cannot read. `hackathon-write-policy` holds metrics:write and
#: logs:write only, so every query 401'd. Going through the stack's own datasource proxy
#: uses the service account token we already know has datasources:query, adds no new
#: credential, and needs no read policy at all.
PROM_DS_UID = os.environ.get("PROM_DATASOURCE_UID", "grafanacloud-prom")


def _prom_query(expr: str) -> List[dict]:
    """Instant query via the datasource proxy. Raises on failure -- callers report it."""
    base = os.environ["GRAFANA_URL"].rstrip("/")
    token = os.environ["GRAFANA_SERVICE_ACCOUNT_TOKEN"]
    r = requests.get(
        f"{base}/api/datasources/proxy/uid/{PROM_DS_UID}/api/v1/query",
        params={"query": expr},
        headers={"Authorization": f"Bearer {token}"},
        timeout=25,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"prometheus said: {str(payload)[:200]}")
    return payload.get("data", {}).get("result", [])


#: Set by fleet_status when the sweep could not run, so callers can SAY SO.
last_error: Optional[str] = None


def fleet_status(deadline_iso: str, *, window: str = "15m") -> List[ShotStatus]:
    """Every in-flight shot, with its own forecast.

    Does not raise -- a broken sweep must not take the console down -- but it does not fail
    silently either. `fleet.last_error` carries the reason, and the caller is expected to
    surface "triage unavailable: <reason>" rather than render an empty strip that looks like
    a farm with no work in it. An empty result that reads as good news is the exact failure
    mode this whole project keeps tripping over.
    """
    global last_error
    last_error = None
    try:
        remaining = _prom_query("shot_frames_remaining")
        rates = _prom_query(
            f"sum by (shot) (rate(render_frames_completed_total[{window}])) * 60")
    except Exception as exc:  # noqa: BLE001
        last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
        return []

    if not remaining:
        last_error = ("shot_frames_remaining returned no series — the seeder may not be "
                      "running, or the metric name has changed")
        return []

    rate_by_shot: Dict[str, float] = {
        m["metric"].get("shot", ""): float(m["value"][1]) for m in rates
    }
    now = datetime.now(timezone.utc)
    try:
        deadline = datetime.fromisoformat(deadline_iso)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
    except ValueError:
        deadline = now

    out: List[ShotStatus] = []
    for series in remaining:
        labels = series["metric"]
        shot = labels.get("shot", "?")
        dept = labels.get("department", "?")
        left = int(float(series["value"][1]))
        rate = rate_by_shot.get(shot, 0.0)

        if left <= 0:
            out.append(ShotStatus(shot, dept, 0, rate, None, deadline.isoformat(
                timespec="minutes"), None, "done", "pass complete"))
            continue
        if rate <= 0:
            out.append(ShotStatus(shot, dept, left, 0.0, None, deadline.isoformat(
                timespec="minutes"), None, "unknown",
                "no completions in the last %s — stalled or no data" % window))
            continue

        hours = left / rate / 60.0
        eta = now + timedelta(hours=hours)
        slip = (eta - deadline).total_seconds() / 3600.0
        status = ("critical" if slip > CRITICAL_HOURS
                  else "at_risk" if slip > AT_RISK_HOURS
                  else "on_track")
        out.append(ShotStatus(
            shot, dept, left, round(rate, 2), eta.isoformat(timespec="minutes"),
            deadline.isoformat(timespec="minutes"), round(slip, 2), status,
        ))

    # Worst first: a producer reads top-down and should not have to scan.
    order = {"critical": 0, "at_risk": 1, "unknown": 2, "on_track": 3, "done": 4}
    out.sort(key=lambda s: (order.get(s.status, 9), -(s.slip_hours or 0)))
    return out


def strip_event(shots: List[ShotStatus]) -> dict:
    """The UI event for the triage strip."""
    return {
        "type": "fleet_status",
        "shots": [
            {
                "shot": s.shot, "department": s.department, "status": s.status,
                "frames_remaining": s.frames_remaining, "rate_per_min": s.rate_per_min,
                "eta": s.eta_iso, "slip_hours": s.slip_hours, "note": s.note,
            }
            for s in shots
        ],
        "exceptions": [s.shot for s in shots if s.is_exception],
    }
