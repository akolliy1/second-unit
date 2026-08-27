"""Grafana Agent Observability: the agent watches itself.

This is the second, distinct use of the partner's product: Grafana is both the agent's tool
surface (via MCP) and the backend its own telemetry lands in. That gives the demo its best
shot, a Grafana dashboard of the agent that is diagnosing a Grafana dashboard, and it
answers "what did that investigation cost" with a number instead of a shrug.

Metrics emitted, per run, to the same Prometheus the farm writes to:

  second_unit_run_seconds{stage}              how long each stage took
  second_unit_tool_calls_total{stage}         MCP calls, by stage
  second_unit_tool_latency_ms{stage,tool}     per-tool latency
  second_unit_tokens_total{stage,kind}        prompt/candidate tokens, by stage
  second_unit_stage_failures_total{stage,reason}
  second_unit_write_claims_total{outcome}     confirmed | false_success | honest_failure

That last one is the metric this project earned. `outcome="false_success"` counts write-backs
the agent REPORTED as successful that independent verification could not find in the stack.
It is the number we would actually page on if this ran in a studio, and it exists because we
watched it happen (see verify.py). Nobody ships that metric because nobody admits they need
it.

Writes are best-effort and always swallowed: telemetry about a run must never be able to
break the run.
"""
import os
import time
from typing import Dict, Iterable, List, Optional

_PUSH_CACHE: Dict[str, object] = {}


def _writer():
    """Reuse the seeder's remote_write client; it is the same endpoint and credentials."""
    if "w" in _PUSH_CACHE:
        return _PUSH_CACHE["w"]
    import sys
    from pathlib import Path
    tel = Path(__file__).resolve().parent.parent.parent / "telemetry"
    if str(tel) not in sys.path:
        sys.path.insert(0, str(tel))
    from push import PromWriter          # noqa: E402
    _PUSH_CACHE["w"] = PromWriter()
    return _PUSH_CACHE["w"]


def _series(name: str, labels: Dict[str, str], value: float, ts_ms: int):
    return ({"__name__": name, "job": "second-unit-agent", **labels}, [(value, ts_ms)])


class RunObserver:
    """Accumulates one run's self-telemetry, then pushes it in a single batch.

    Batched on purpose: a push per tool call would add a network round trip to every step
    of an investigation, and the thing we are measuring is latency.
    """

    def __init__(self, run_id: str, model: str = ""):
        self.run_id = run_id
        self.model = model
        self.series: List = []
        self.started = time.time()
        self._counts: Dict[str, int] = {}

    # -- recording -----------------------------------------------------

    def stage(self, stage: str, seconds: float, tool_calls: int,
              ok: bool, error: Optional[str] = None,
              prompt_tokens: int = 0, candidate_tokens: int = 0):
        ts = int(time.time() * 1000)
        self.series.append(_series("second_unit_run_seconds", {"stage": stage},
                                   round(seconds, 2), ts))
        self._counts[stage] = self._counts.get(stage, 0) + tool_calls
        self.series.append(_series("second_unit_tool_calls_total", {"stage": stage},
                                   self._counts[stage], ts))
        if prompt_tokens:
            self.series.append(_series("second_unit_tokens_total",
                                       {"stage": stage, "kind": "prompt"},
                                       prompt_tokens, ts))
        if candidate_tokens:
            self.series.append(_series("second_unit_tokens_total",
                                       {"stage": stage, "kind": "candidate"},
                                       candidate_tokens, ts))
        if not ok:
            # Bucket the reason: unbounded label values would blow up cardinality, which is
            # the one way an observability addition can actually harm the host stack.
            reason = "unknown"
            if error:
                low = error.lower()
                reason = ("quota" if "resource_exhausted" in low or "429" in low
                          else "budget" if "exceeded the" in low and "budget" in low
                          else "unparseable" if "unparseable" in low
                          else "no_response" if "no final response" in low
                          else "other")
            self.series.append(_series("second_unit_stage_failures_total",
                                       {"stage": stage, "reason": reason}, 1, ts))

    def tools(self, stage: str, calls: Iterable):
        ts = int(time.time() * 1000)
        for rec in calls:
            ms = getattr(rec, "ms", 0) or 0
            if ms:
                self.series.append(_series("second_unit_tool_latency_ms",
                                           {"stage": stage, "tool": rec.name}, ms, ts))

    def write_claims(self, tally: Dict[str, int]):
        """The metric this project earned. See the module docstring."""
        ts = int(time.time() * 1000)
        for outcome, n in tally.items():
            self.series.append(_series("second_unit_write_claims_total",
                                       {"outcome": outcome}, n, ts))

    # -- flushing ------------------------------------------------------

    def flush(self, verbose: bool = True) -> bool:
        ts = int(time.time() * 1000)
        self.series.append(_series("second_unit_run_seconds", {"stage": "total"},
                                   round(time.time() - self.started, 2), ts))
        if not self.series:
            return False
        try:
            w = _writer()
            for i in range(0, len(self.series), 400):
                w.write(self.series[i:i + 400])
            if verbose:
                print(f"   observability: pushed {len(self.series)} series "
                      f"for run {self.run_id}")
            return True
        except Exception as exc:  # noqa: BLE001, telemetry must never break the run
            if verbose:
                print(f"   observability: push failed ({type(exc).__name__}), continuing")
            return False


def observer_enabled() -> bool:
    """Off by default in tests, on wherever the push credentials exist."""
    if os.environ.get("SECOND_UNIT_OBSERVE") in ("0", "false", "no"):
        return False
    return bool(os.environ.get("PROM_REMOTE_WRITE_URL"))
