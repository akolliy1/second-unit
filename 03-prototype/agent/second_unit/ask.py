"""Ask: a scoped question box, not a chatbot.

The objection to free-form chat was never latency or difficulty, it was that an
open-ended box invites a question the system cannot answer, produces a confident
non-answer, and that becomes the lasting impression of the whole product. Two things fix
that, and neither is a longer prompt:

1. **A router runs first.** One fast model call, no tools, ~1s: can this be answered from
   this farm's telemetry? If not, say so immediately and suggest what *is* answerable. An
   out-of-scope question never reaches a tool, so it never costs a minute to be told no.
2. **The answering agent has five tools, not seventy-six.** Narrow scope is what keeps it
   from wandering, and wandering is how you get a slow wrong answer.

The answer streams its tool calls into the same renderer the pipeline uses, so the wait is
legible rather than dead air: which is the same reason the stage stream exists.
"""
import os
from typing import Optional

from google.adk.agents import LlmAgent

from .grafana import ASK_TOOLS, grafana_toolset
from .schemas import AskAnswer, AskRoute
from .stages import FARM_CONTEXT, FLASH
from .tools import forecast_after_remediation, forecast_delivery, idle_cost

#: Questions the console offers as chips. Every one is answerable from the farm's telemetry,
#: and each exercises a different capability, so the demo path cannot land on a dud.
SUGGESTED = [
    "Does SH041 make its review?",
    "What if we drain render-07 right now?",
    "Which department is worst affected?",
    "What is the delay costing in idle artist time?",
    "Is the asset pipeline actually a problem?",
]


def router() -> LlmAgent:
    """Cheap, tool-less triage of the question itself."""
    return LlmAgent(
        model=FLASH,
        name="ask_router",
        description="Decides whether a question can be answered from this farm's telemetry.",
        instruction="""
You gate a question box on an observability console for ONE synthetic VFX render farm. Decide
only whether the question can be answered from that farm's telemetry. Do not answer it.

IN SCOPE, the farm's own state and consequences:
  render nodes (12, one faulty), GPU temperature and ECC errors, frames completed/failed,
  queue depth by department, per-shot frames remaining and completion rate, review deadlines
  and whether a shot makes them, what a fix would change, crew/idle-time consequences,
  the asset pipeline's logs, the render scheduler's logs.

OUT OF SCOPE, anything else. Weather, general knowledge, other systems, other companies,
code questions, the model itself, anything about real production infrastructure, and anything
needing data this farm does not emit (costs in currency other than idle time, artist names,
client identities, storage capacity, licence servers).

Be decisive. If a question is partly answerable, call it in scope and let the answering stage
state the limits. If it is not, `reason` must say plainly what this system can and cannot
see, one sentence, no apology, no speculation, and `suggestion` must offer the nearest
question that IS answerable here.
""",
        output_schema=AskRoute,
        output_key="ask_route",
    )


def answerer() -> LlmAgent:
    """Answers in scope, with citations, using five tools."""
    return LlmAgent(
        model=FLASH,
        name="ask_answerer",
        description="Answers one scoped question about the render farm, with evidence.",
        instruction=FARM_CONTEXT + """
YOUR JOB: answer ONE question about this farm, briefly, and show your working.

Rules:
- Lead with the answer. A producer asked a question; do not narrate your method first.
- Measure before you answer. Two or three tool calls is usually enough; you have a small
  budget and a wandering search is a slow wrong answer.
- For anything about deadlines or "would the fix work", call `forecast_delivery` or
  `forecast_after_remediation` rather than doing arithmetic yourself. Quote what they return.
  If they refuse your input, re-measure: do not talk past them.
- Every claim carries the tool and the exact query in `evidence`.
- `caveat` is not optional decoration. If the window is short, a series is missing, or you
  assumed something, say it. An answer that hides its own weakness is worse than one that
  admits it.
- If, having looked, the data genuinely cannot answer it, say that in `answer` with
  confidence "low". Do not construct a plausible number.
- NEVER ask the operator a question back. You are given the shot in question, its published
  review deadline and the current fleet state; everything else you can measure. "Which pass
  did you mean?" is not an answer, if something is genuinely ambiguous, pick the most
  likely reading, answer it, and name the assumption in `caveat`.
""",
        tools=[grafana_toolset(tool_filter=ASK_TOOLS),
               forecast_delivery, forecast_after_remediation, idle_cost],
        output_schema=AskAnswer,
        output_key="ask_answer",
    )


def route_task(question: str) -> str:
    return f"Question from the console:\n\n{question}"


def farm_context(shot: str = "SH042") -> str:
    """Facts the answerer would otherwise have to ask the user for.

    Without this, "what if we drain render-07?" came back as "which render pass, and what
    is its review deadline?", a technically reasonable question and a useless answer. The
    console knows which shot is selected and the deadline is published as telemetry, so
    handing both over is not a shortcut, it is the difference between an assistant and a
    form. It still measures everything else itself.
    """
    lines = [f"The operator is looking at {shot}."]
    try:
        from . import fleet
        from .run import published_review_deadline
        dl = published_review_deadline(shot)
        lines.append(f"{shot}'s published client review deadline is {dl}.")
        # The HEALTHY baseline, DERIVED FROM CAPACITY rather than from history.
        #
        # Three attempts got here. Using the current degraded rate as the baseline made
        # draining a dead node look harmful. Taking the peak of the last 3 hours returned
        # the degraded rate, because the scenario's healthy lead-in had aged out. Widening
        # to 12 hours returned 980 frames/min, a counter-reset artefact from the seeder's
        # cycle restarts, the same class of nonsense `forecast_delivery` already refuses.
        #
        # History is the wrong source in an environment with counter resets. Capacity is
        # not: the pass has a known number of nodes, we can see which ones are faulty, and
        # a faulty node contributes ~nothing. So the healthy rate is simply what the
        # working nodes are achieving, scaled back up to the full pool. One instant query,
        # no windows, nothing to age out.
        try:
            nodes_on_pass = 7 if shot == "SH042" else (2 if shot == "SH041" else 3)
            ecc = fleet._prom_query(
                "sum by (node) (increase(render_node_gpu_ecc_errors_total[30m])) > 0")
            faulty = len(ecc)
            healthy_now = max(1, nodes_on_pass - faulty)
            now_rate = next((s2.rate_per_min for s2 in fleet.fleet_status(dl)
                             if s2.shot == shot), 0.0)
            if now_rate > 0 and faulty:
                baseline = now_rate * nodes_on_pass / healthy_now
                lines.append(
                    f"{shot}'s HEALTHY baseline is about {baseline:.1f} frames/min. That is "
                    f"DERIVED, not measured from history: {healthy_now} of "
                    f"{nodes_on_pass} nodes on this pass are working and achieving "
                    f"{now_rate:.1f}/min between them, so the full pool would do "
                    f"{baseline:.1f}. Use this as the baseline when modelling a fix, and "
                    f"pass {now_rate:.1f} as the current rate.")
            elif now_rate > 0:
                lines.append(
                    f"No node on {shot} is currently faulty, so its current "
                    f"{now_rate:.1f} frames/min IS the healthy rate. There is no capacity "
                    f"to recover by draining anything.")
        except Exception:  # noqa: BLE001
            pass
        lines.append("The farm has 12 render nodes; 7 are on the lighting pass.")
    except Exception:  # noqa: BLE001, context is a help, not a prerequisite
        lines.append("(live fleet state unavailable, measure what you need yourself)")
    return "\n".join(lines)


def answer_task(question: str, shot: str = "SH042",
                context: Optional[str] = None) -> str:
    ctx = f"\n\nWhat the last investigation found:\n{context}" if context else ""
    return (
        f"Answer this question about the farm:\n\n{question}\n\n"
        f"Context you already have, do NOT ask the operator for any of it:\n"
        f"{farm_context(shot)}{ctx}"
    )
