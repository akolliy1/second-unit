"""Stage boundaries.

Every handoff between stages is one of these objects, never free text. Two reasons, and
the second is the one that matters for judging:

1. A downstream stage that receives prose has to re-parse an upstream stage's opinions,
   and errors compound silently.
2. "Deterministic, multi-step" is a claim we have to be able to demonstrate. Typed
   boundaries are the demonstration: the model decides what goes *in* the fields, code
   decides what happens next.

Kept deliberately shallow — two levels of nesting. Gemini's structured output degrades on
deeply nested schemas, and a flat shape is also easier to render in the UI.
"""
from enum import Enum
from typing import List

from pydantic import BaseModel, Field


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class Evidence(BaseModel):
    """A single citeable fact. No claim reaches the operator without one of these."""

    claim: str = Field(description="One sentence, stated plainly, that this evidence supports.")
    tool: str = Field(description="The MCP tool that produced it, e.g. query_prometheus.")
    query: str = Field(description="The exact PromQL/LogQL sent, or the tool arguments.")
    observed: str = Field(description="What actually came back — real numbers, real log text.")


class WatchtowerReport(BaseModel):
    """Stage 1: what is on fire. Breadth, not depth. No root-causing here."""

    summary: str = Field(description="Two sentences on the current state of the farm.")
    firing_alerts: List[str] = Field(default_factory=list)
    error_signatures: List[str] = Field(
        default_factory=list,
        description="Distinct error patterns seen in logs, deduplicated.")
    suspect_entities: List[str] = Field(
        default_factory=list,
        description="Nodes, queues, shots or services worth investigating. Names only.")
    evidence: List[Evidence] = Field(default_factory=list)


class Hypothesis(BaseModel):
    statement: str
    verdict: str = Field(description="Exactly one of: confirmed, ruled_out, unresolved")
    confidence: Confidence
    reasoning: str = Field(description="Why the evidence supports this verdict.")


class Diagnosis(BaseModel):
    """Stage 2: why. This stage is expected to rule things OUT as well as in."""

    root_cause: str = Field(description="One sentence. The single most likely cause.")
    confidence: Confidence
    causal_chain: List[str] = Field(
        description="Ordered links from cause to production consequence, one step each.")
    hypotheses: List[Hypothesis] = Field(
        default_factory=list,
        description="Everything considered, including what was ruled out and why.")
    affected_shots: List[str] = Field(default_factory=list)
    evidence: List[Evidence] = Field(default_factory=list)


class ImpactForecast(BaseModel):
    """Stage 3: what it costs. The one sentence a producer acts on."""

    verdict: str = Field(
        description="One sentence in PRODUCTION language, not infrastructure language. "
                    "Name the shot, the deadline, and the slip. No metric names.")
    shot: str
    department: str
    frames_remaining: int
    current_rate_per_min: float
    baseline_rate_per_min: float
    capacity_loss_pct: float
    eta_iso: str
    deadline_iso: str
    slip_hours: float = Field(description="Positive means late. From forecast_delivery.")
    makes_deadline: bool
    crew_impact: str = Field(description="Who is idle or blocked, and from when.")
    evidence: List[Evidence] = Field(default_factory=list)


class ProposedWrite(BaseModel):
    """One mutating action the agent WANTS to take. Nothing here has happened yet."""

    action: str = Field(description="One of: annotation, dashboard, alert_rule, incident")
    title: str = Field(description="Short label for the operator's approval checkbox.")
    rationale: str = Field(description="Why this specific write helps, in one sentence.")
    tool: str = Field(description="The MCP tool that would perform it.")
    details: str = Field(description="What exactly would be created or changed.")
    reversible: bool = Field(description="Can a human undo this in one step?")


class RemediationPlan(BaseModel):
    """Stage 4: the proposal. Produced by an agent that HAS NO WRITE TOOLS."""

    summary: str
    proposed_writes: List[ProposedWrite]
    fix_recommendation: str = Field(
        description="The real-world fix for the crew — not a Grafana change.")
    risk_if_ignored: str
    # The counterfactual. Advice without a number is an opinion; advice with an ETA is a
    # decision the producer can actually make.
    fix_outcome: str = Field(
        default="",
        description="One sentence: if the fix is applied NOW, does the pass make its "
                    "review, and by what margin? Quote forecast_after_remediation.")
    fix_eta_iso: str = Field(default="", description="ETA after the fix, from the tool.")
    fix_makes_deadline: bool = Field(
        default=False, description="From forecast_after_remediation, not your judgement.")
    fix_margin_minutes: int = Field(
        default=0, description="Minutes inside (positive) or outside (negative) the review.")


class WriteResult(BaseModel):
    """Stage 5: what actually happened, after a human approved it."""

    action: str
    succeeded: bool
    detail: str = Field(description="The created object's URL/uid, or the error.")
