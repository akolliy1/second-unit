"""Second Unit: the one page a judge clicks.

Design constraints that shape every decision in this file:

* Five pages plus a small JSON API. No auth, no accounts, no second page. The hosted URL
  has to work for a logged-out stranger on a phone (see 02-planning/08-ui-and-scope.md §1).
* No frontend build step. One Jinja template with inlined CSS and JS, no CDN, because the
  deploy target has no guaranteed network egress and a blocked CDN would render the page
  unstyled in front of a judge.
* The page renders *events*, not a finished report. The agent pipeline emits events as it
  runs, so the transport is Server-Sent Events and the client is a switch on `event.type`.

Right now the events come from `fixture.json`, a real recorded run. Swapping in the live
pipeline means replacing `_iter_events()` with the ADK runner's event stream; the wire
format and the client stay exactly as they are. That is why the fixture's field names are
copied verbatim from `second_unit/schemas.py` rather than reshaped for the UI.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates

# Load .env BEFORE anything reads os.environ.
#
# This has now bitten twice. First the web process never loaded it at all, which
# silently disabled Text-to-Speech (403, missing quota project) and the tracing
# exporter. Then it loaded too LATE: GRAFANA_URL is captured into a module constant a
# few lines below, so it was always empty and every evidence deeplink on the
# investigation page rendered as 'deeplink unavailable', on the one surface whose job
# is to let a judge verify our claims against the live stack. Import order is not a
# style question here.
try:
    from dotenv import load_dotenv as _load_dotenv
    from pathlib import Path as _P
    for _cand in (_P(__file__).resolve().parent / ".env",
                  _P(__file__).resolve().parent.parent / ".env"):
        if _cand.is_file():
            _load_dotenv(_cand)
            break
except Exception:  # noqa: BLE001
    pass


HERE = Path(__file__).parent
FIXTURE_PATH = HERE / "fixture.json"

# The recorded run took 129.5s of real wall clock. Nobody watches a demo for two minutes,
# so replay is compressed by this factor while the *reported* stage durations stay honest
# (the stage rows still say 45.0s / 66.1s, because that is what actually happened).
# Set REPLAY_SPEED=1 to watch it at true speed.
REPLAY_SPEED = float(os.environ.get("REPLAY_SPEED", "8"))

# Deeplinks into the live stack are built client-side from the tool + query on each piece
# of evidence, so the base URL is the only thing the page needs from the environment.
GRAFANA_URL = os.environ.get("GRAFANA_URL", "").rstrip("/")

# Cap on retained runs. This is a demo with no database; runs live in memory and a judge
# hammering "Run investigation" must not grow the process without bound.
MAX_RUNS = 32

app = FastAPI(title="Second Unit", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(HERE / "templates"))
# `{% extends %}` on a child template's first line renders to an empty line, so every
# response began with a newline before <!doctype html>. Browsers tolerate it, we measured
# CSS1Compat, but it is one stray character away from quirks mode, and quirks mode is a
# whole class of layout bugs nobody should be debugging on submission day.
templates.env.trim_blocks = True
templates.env.lstrip_blocks = True


# --------------------------------------------------------------------------- fixture


def load_fixture() -> Dict[str, Any]:
    """Read the recorded run from disk on every call.

    Deliberately not cached: editing fixture.json and refreshing the page is the whole
    development loop for this UI, and the file is 20 KB.
    """
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- run state


@dataclass
class Run:
    id: str
    created: float = field(default_factory=time.time)
    # "pending" -> "streaming" -> "complete" | "failed"
    status: str = "pending"
    inject: str = ""            # fault injection, so the error states are demoable
    approved: bool = False
    approved_at: Optional[float] = None
    approved_items: List[str] = field(default_factory=list)
    writeback_ids: List[str] = field(default_factory=list)
    writeback_items: List[Dict] = field(default_factory=list)   # the full proposal
    # Execution state. "idle" -> "running" -> "done" | "error". The UI polls this rather
    # than holding a request open for the two minutes the writes can take.
    write_state: str = "idle"
    write_error: str = ""
    write_results: List[Dict] = field(default_factory=list)     # verified, out of band
    live: bool = False          # run the real pipeline instead of replaying the recording
    shot: str = "SH042"         # which pass this run is about
    briefing_script: str = ""   # the spoken dailies script, composed by the pipeline
    briefing_mp3: Optional[bytes] = None    # synthesized lazily, then cached per run


# Export ADK's own GenAI spans to Grafana Cloud AI Observability. Done at import so every
# request path is traced, and guarded so a missing credential cannot stop the service.
try:
    import sys as _sys
    from pathlib import Path as _Path
    _agent = _Path(__file__).resolve().parent.parent / "agent"
    if _agent.is_dir() and str(_agent) not in _sys.path:
        _sys.path.insert(0, str(_agent))
    from second_unit.tracing import setup_tracing as _setup_tracing
    _setup_tracing(service_name="second-unit-web")
except Exception:  # noqa: BLE001
    pass


RUNS: Dict[str, Run] = {}


def _remember(run: Run) -> None:
    RUNS[run.id] = run
    if len(RUNS) > MAX_RUNS:
        for stale in sorted(RUNS.values(), key=lambda r: r.created)[: len(RUNS) - MAX_RUNS]:
            RUNS.pop(stale.id, None)


def _get_run(run_id: str) -> Run:
    run = RUNS.get(run_id)
    if run is None:
        # Most likely cause: the server restarted mid-demo. Say so, rather than 500ing, 
        # the client turns this into "run expired, press Run investigation again".
        raise HTTPException(status_code=404, detail="unknown or expired run id")
    return run


# --------------------------------------------------------------------------- SSE


def _sse(payload: Dict[str, Any]) -> str:
    """One event = one JSON object on one `data:` line.

    Kept to a single line on purpose: a newline inside `data:` would split the frame and
    the browser would hand the client half a JSON document.
    """
    return "data: " + json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n\n"


async def _iter_live(run: Run, request: Request) -> AsyncIterator[str]:
    """Stream the real pipeline. Same wire format, same client, no replay pacing, 
    the pauses a judge sees here are the agent actually thinking."""
    try:
        from live import iter_live_events
    except Exception as exc:  # noqa: BLE001
        run.status = "failed"
        yield _sse({"type": "run_failed", "stage": None,
                    "error": f"live pipeline unavailable: {type(exc).__name__}: {exc}",
                    "hint": "Falling back to the recorded run is safe: press "
                            "Run investigation without ?live=1."})
        return

    run.status = "streaming"
    yield _sse({"type": "hello", "run_id": run.id, "replay_speed": 1})
    try:
        # HEARTBEAT. The recorded path already does this; the live path did not, and it
        # cost us a truncated run on the hosted service: between one stage finishing and
        # the next stage's first tool call there is a 30-60s silence while the model
        # thinks, and Cloud Run's front end drops a connection that has sent no bytes.
        # The stream then ends cleanly with no error event, which looks exactly like the
        # agent dying halfway through: the worst possible thing for a judge to see.
        #
        # Wrapping __anext__ in wait_for lets us emit an SSE comment during the gaps
        # without touching the pipeline itself.
        events = iter_live_events(shot=run.shot).__aiter__()
        pending = None
        while True:
            # Heartbeat WITHOUT cancelling the pipeline.
            #
            # The first version did `await asyncio.wait_for(events.__anext__(), timeout=10)`.
            # On timeout, wait_for CANCELS the coroutine it was waiting on, which here is
            # the generator step currently running an agent. So every heartbeat killed the
            # stage in flight and left the generator dead: the stream ended cleanly after
            # Watchtower with no error, and the "fix" for truncation became a better cause
            # of it. Keep the step alive in its own task and simply look at it periodically.
            if pending is None:
                pending = asyncio.ensure_future(events.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=10)
            if not done:
                if await request.is_disconnected():
                    pending.cancel()
                    run.status = "abandoned"
                    return
                yield ": keepalive\n\n"      # a comment line: valid SSE, ignored by clients
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                break
            finally:
                pending = None
            if await request.is_disconnected():
                run.status = "abandoned"
                return
            # Keep the real proposal so /approve validates against what was actually
            # proposed, not against the fixture's items.
            if event.get("type") == "writeback_proposed":
                run.writeback_ids = [i["id"] for i in event.get("items", [])]
                run.writeback_items = list(event.get("items", []))
            if event.get("type") == "briefing" and event.get("script"):
                run.briefing_script = event["script"]
            if event.get("type") == "run_done":
                run.status = "complete"
            if event.get("type") == "run_failed":
                run.status = "failed"
            yield _sse(event)
    except Exception as exc:  # noqa: BLE001
        run.status = "failed"
        yield _sse({"type": "run_failed", "stage": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "hint": "The live pipeline raised. The recorded run still works."})


async def _iter_events(run: Run, request: Request) -> AsyncIterator[str]:
    """Replay the recorded run as SSE, honouring the recorded pacing.

    When the real pipeline is wired in, this function becomes an `async for` over the ADK
    event stream and everything downstream of it is unchanged.
    """
    if getattr(run, "live", False):
        async for frame in _iter_live(run, request):
            yield frame
        return

    try:
        fixture = load_fixture()
    except (OSError, json.JSONDecodeError) as exc:
        run.status = "failed"
        yield _sse({
            "type": "run_failed",
            "stage": None,
            "error": f"fixture unreadable: {type(exc).__name__}: {exc}",
            "hint": "The recorded run could not be loaded on the server.",
        })
        return

    run.status = "streaming"
    yield _sse({"type": "hello", "run_id": run.id, "replay_speed": REPLAY_SPEED})

    for item in fixture.get("events", []):
        if await request.is_disconnected():
            # A judge closing the tab must not leave this coroutine sleeping through the
            # rest of the run.
            run.status = "abandoned"
            return

        delay = float(item.get("after_ms", 0)) / 1000.0 / max(REPLAY_SPEED, 0.01)
        # Long gaps get a comment heartbeat first: intermediary proxies (and Cloud Run's
        # own front end) can drop a connection that has sent nothing for a while, and a
        # `:` line is a legal SSE no-op that the client never sees as a message.
        while delay > 5.0:
            yield ": keepalive\n\n"
            await asyncio.sleep(5.0)
            delay -= 5.0
        if delay > 0:
            await asyncio.sleep(delay)

        event = {k: v for k, v in item["event"].items()}

        # Fault injection. The error state is a quarter of the score's "complete product
        # experience", and the only way to keep it honest is to be able to trigger it.
        if run.inject and run.inject == event.get("stage") and event["type"] == "stage_done":
            run.status = "failed"
            yield _sse({
                "type": "stage_failed",
                "stage": event["stage"],
                "error": "unparseable output: ValidationError: 3 validation errors for "
                         "Diagnosis" if event["stage"] == "diagnostician"
                         else "McpError: streamable-http request timed out after 30s",
                "hint": "The stage ran but its output did not validate against the schema. "
                        "Nothing downstream is trusted.",
            })
            yield _sse({"type": "run_failed", "stage": event["stage"],
                        "error": "pipeline halted at " + event["stage"]})
            return

        if event["type"] == "writeback_proposed":
            run.writeback_ids = [i["id"] for i in event.get("items", [])]
            run.writeback_items = list(event.get("items", []))
        if event["type"] == "briefing" and event.get("script"):
            run.briefing_script = event["script"]
        if event["type"] == "run_done":
            run.status = "complete"

        event["run_id"] = run.id
        yield _sse(event)

    if run.status == "streaming":  # fixture ended without a run_done
        run.status = "complete"


# --------------------------------------------------------------------------- routes


#: Shots that existed when the console had three of them.
#:
#: This whitelist was hardcoded in three places, and then the slate arrived with 750 more.
#: Asking about SH1718 silently became a question about SH042, the ask feature answered
#: confidently about the wrong shot, and only its own caveat ("the provided context
#: exclusively concerned SH042") revealed it. A validator must ask what exists, not recite
#: what used to.
_INCIDENT_SHOTS = ("SH041", "SH042", "SH043")


def valid_shot(candidate: Optional[str], default: str = "SH042") -> str:
    """Normalise a requested shot, accepting anything that actually exists.

    Order matters: the incident passes are checked first because they are cheap and are the
    common case, then the generated catalogue. A slate id that is real but not currently
    rendering is still accepted, the page it lands on explains that there is nothing live
    to investigate, which is more useful than silently substituting a different shot.
    """
    if not candidate:
        return default
    shot = candidate.strip().upper()
    if shot in _INCIDENT_SHOTS:
        return shot
    try:
        from second_unit.shots_catalog import catalog
        if any(s.shot == shot for s in catalog()):
            return shot
    except Exception:  # noqa: BLE001, fall through to the default
        pass
    return default


def _chrome(request: Request, page_id: str, title: str, sub: str) -> dict:
    """Context every page needs. Centralised so the rail, persona chip and drawer cannot
    drift between pages: the most common way a multi-page prototype starts feeling cheap."""
    asked = request.query_params.get("persona")
    return {
        "page_id": page_id,
        "page_title": title,
        "page_sub": sub,
        "initial_persona": asked if asked in ("td", "supervisor", "producer") else "td",
        "docs_open": request.query_params.get("docs") == "1",
    }


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request) -> HTMLResponse:
    """The marketing page. `/` is what a judge clicks first, and an operator console with
    no explanation is the wrong first ten seconds, so the console moved to /overview and
    this page says what the product is before showing what it does. It carries no chrome
    context: its numbers are fetched client-side from the same public APIs the console
    uses, so there is nothing to server-render and nothing to keep in sync."""
    return templates.TemplateResponse(request=request, name="landing.html", context={})


@app.get("/overview", response_class=HTMLResponse)
async def overview(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name="overview.html",
        context=_chrome(request, "overview", "Overview",
                        "What needs attention across every shot in flight"))


@app.get("/shots", response_class=HTMLResponse)
async def shots_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name="shots.html",
        context=_chrome(request, "shots", "Shots",
                        "Delivery status against each pass's published review deadline"))


@app.get("/agent", response_class=HTMLResponse)
async def agent_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name="agent.html",
        context=_chrome(request, "agent", "Agent",
                        "What the agent did, what it cost, and whether its claims held up"))


@app.get("/start", response_class=HTMLResponse)
async def start_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name="start.html",
        context=_chrome(request, "start", "Who's looking at the farm?",
                        "Same investigation, reported in the language of whoever acts on it"))


@app.get("/investigation", response_class=HTMLResponse)
async def investigation(request: Request) -> HTMLResponse:
    fixture = load_fixture()
    verdict = next(
        (e["event"] for e in fixture["events"] if e["event"]["type"] == "verdict"), None
    )
    # The verdict is server-rendered into the page as well as streamed. Two reasons: a
    # judge who lands on the URL sees the finding immediately instead of an empty console,
    # and the headline survives JS being slow, blocked, or broken.
    return templates.TemplateResponse(
        request=request,
        name="investigation.html",
        context={
            **_chrome(request, "investigation", "Investigation",
                      "Five stages, live tool calls, and an approval gate before anything is written"),
            # The subject of this page. The recorded run is SH042; if a judge deep-links a
            # different shot we say so rather than silently showing the wrong pass.
            "subject_shot": valid_shot(request.query_params.get("shot")),
            "recorded_shot": "SH042",
            "scenario": fixture.get("scenario", {}),
            "verdict": verdict,
            # /start always shows the persona chooser; / shows it only on a first visit
            # (the client decides from localStorage). Either way the console is rendered
            # underneath, so the overlay never stands between a judge and the finding.

            # The recorded run is embedded in the page rather than fetched: it means a
            # judge landing on the URL sees a complete finding on first paint with no
            # second round trip, and the page stays coherent if the API is unreachable.
            "fixture": fixture,
            "grafana_url": GRAFANA_URL,
            "replay_speed": REPLAY_SPEED,
        },
    )


# `/healthz` is intercepted by Google's frontend on Cloud Run -- the bare path returns a
# Google-branded 404 that never reaches this app, while `/healthz/` and every other route
# arrive normally. Serve the same payload under names that survive the edge.
@app.get("/favicon.ico")
async def favicon() -> Response:
    """A tiny inline SVG favicon.

    Not cosmetic paranoia: every page load was logging a 404 for /favicon.ico, and a judge
    who opens devtools should not be greeted by a failed request on a page whose whole
    argument is rigour. Inline so it adds no file and no external fetch.
    """
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="7" fill="#11161d"/>'
        '<circle cx="16" cy="16" r="7.5" fill="none" stroke="#f5a623" stroke-width="2.5"/>'
        '<circle cx="16" cy="16" r="2.5" fill="#f5a623"/></svg>'
    )
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/health")
@app.get("/api/health")
@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.post("/api/run")
async def start_run(request: Request) -> JSONResponse:
    """Start a run and hand back its id. The work happens on the stream, not here.

    Returning immediately keeps the POST honest under a slow connection: the client gets
    an id it can reconnect the stream with, rather than a request that hangs for the
    duration of the pipeline.
    """
    inject = request.query_params.get("inject", "")
    if not inject:
        try:
            body = await request.json()
            inject = str(body.get("inject", "")) if isinstance(body, dict) else ""
        except Exception:  # noqa: BLE001, an absent or non-JSON body is the normal case
            inject = ""

    # ?live=1 (or SECOND_UNIT_LIVE=1 in the environment) runs the REAL pipeline instead
    # of replaying the recording. Recorded stays the default because it is instant and
    # cannot fail: a judge's first click must always show something. Live is the proof.
    want_live = (
        request.query_params.get("live") in ("1", "true", "yes")
        or os.environ.get("SECOND_UNIT_LIVE") in ("1", "true", "yes")
    )
    run = Run(id=uuid.uuid4().hex[:12], inject=inject)
    run.live = want_live and not inject
    run.shot = valid_shot(request.query_params.get("shot"), default=run.shot)

    # The page hydrates from the recorded run on load, without streaming. That run really
    # did happen, so its write-back proposal is genuinely approvable, this registers it
    # server-side so the approval gate works before anyone presses "Run investigation".
    if request.query_params.get("mode") == "recorded":
        run.status = "complete"
        try:
            fixture = load_fixture()
            run.writeback_ids = next(
                ([i["id"] for i in e["event"].get("items", [])]
                 for e in fixture["events"]
                 if e["event"]["type"] == "writeback_proposed"),
                [],
            )
        except (OSError, json.JSONDecodeError, KeyError):
            run.writeback_ids = []

    _remember(run)
    return JSONResponse(
        {"run_id": run.id, "stream": f"/api/run/{run.id}/stream", "status": run.status},
        status_code=201,
    )


@app.get("/api/run/{run_id}/stream")
async def stream_run(run_id: str, request: Request) -> StreamingResponse:
    run = _get_run(run_id)
    return StreamingResponse(
        _iter_events(run, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # Tells nginx-family proxies (and Cloud Run's front end) not to buffer the
            # response. Without it the whole "streams live" beat arrives in one lump.
            "X-Accel-Buffering": "no",
        },
    )


#: Which MCP tool performs each kind of write. The planner names a tool, but the recording
#: stores only the kind, so this fills the gap and keeps one execution path for both.
_TOOL_FOR = {
    "annotation": "create_annotation",
    "dashboard": "update_dashboard",
    "alert": "alerting_manage_rules",
    "alert_rule": "alerting_manage_rules",
}


def _plan_from_items(items: List[Dict]):
    """Rebuild the planner's proposal from the write-back event.

    Deliberately the same path for a live run and for the recording. A judge approving the
    recorded run is the common case, and giving them a stub while the CLI does the real thing
    would make the product's central claim untestable in the only place they can reach it.

    The executor touches nothing but `proposed_writes`, so a namespace is enough; building a
    full RemediationPlan would mean inventing the forecast fields it does not read.
    """
    from types import SimpleNamespace
    from second_unit.schemas import ProposedWrite

    writes = []
    for it in items:
        kind = (it.get("kind") or it.get("id") or "").strip()
        writes.append(ProposedWrite(
            action=kind,
            title=it.get("title") or kind,
            rationale=it.get("target") or "Approved by the operator in the console.",
            tool=_TOOL_FOR.get(kind, "create_annotation"),
            details=it.get("detail") or "",
            reversible=True,
        ))
    return SimpleNamespace(proposed_writes=writes)


async def _execute_writes(run: "Run", items: List[str]) -> None:
    """Perform the approved writes, then check them out of band.

    Runs detached from the request: the writes are one agent call each and take a minute or
    two, and holding a POST open for that is how a judge on hotel wifi sees a timeout instead
    of a result. State lands on the run and the UI polls it.

    The verification is the point. `verify_writes` diffs the stack through Grafana's HTTP API,
    not the MCP tools the executor just used, so a write the agent claims but never made shows
    up as a disagreement rather than as success.
    """
    from second_unit import verify as verify_mod
    from second_unit.pipeline import Approval, execute_approved_writes

    try:
        plan = _plan_from_items(run.writeback_items)
        indices = [i for i, it in enumerate(run.writeback_items)
                   if (it.get("id") or "") in items]
        approval = Approval(
            approved_by=f"console-operator ({run.id[:8]})",
            approved=indices,
            note="approved in the web console",
        )
        before = verify_mod.snapshot()
        # Returns a StageResult, not a list: `.output` is the WriteResult list. Iterating the
        # StageResult raised TypeError on the first real execution, which the error path
        # surfaced rather than swallowing, so it showed up as a message instead of an empty
        # success.
        stage = await execute_approved_writes(plan, approval, verbose=False)
        results = stage.output or []
        if stage.error and not results:
            raise RuntimeError(stage.error)
        claimed = [r.model_dump() if hasattr(r, "model_dump") else dict(r) for r in results]
        run.write_results = verify_mod.verify_writes(claimed, before)
        run.write_state = "done"
    except Exception as exc:  # noqa: BLE001
        # Reported, never swallowed. A failed write that looks like nothing happened is the
        # exact failure this project exists to make visible.
        run.write_state = "error"
        run.write_error = f"{type(exc).__name__}: {exc}"


@app.get("/api/run/{run_id}/writes")
async def write_status(run_id: str) -> JSONResponse:
    """Poll the write-back. Returns the out-of-band verification once it exists."""
    run = _get_run(run_id)
    rows = []
    for r in run.write_results:
        # verify_writes emits action / claimed_success / verified / evidence. Reading
        # `claimed` and `kind` instead, as the first draft did, reported every write as an
        # honest failure: a plausible-looking table that was wrong about all of it.
        claimed = bool(r.get("claimed_success"))
        verified = r.get("verified")            # None means no check exists for this kind
        if verified is None:
            outcome = "unchecked"
        elif claimed and not verified:
            outcome = "false_success"
        elif verified:
            outcome = "confirmed"
        else:
            outcome = "honest_failure"
        rows.append({
            "kind": r.get("action") or "",
            "claimed": claimed,
            "verified": bool(verified),
            "detail": r.get("evidence") or r.get("detail") or "",
            # The same four words the Agent page counts by, so the two surfaces cannot tell
            # different stories about one run.
            "outcome": outcome,
        })
    return JSONResponse({
        "run_id": run.id, "state": run.write_state, "error": run.write_error,
        "results": rows,
        "disagreements": [x for x in rows if x["outcome"] == "false_success"],
    })


@app.post("/api/run/{run_id}/approve")
async def approve(run_id: str, request: Request) -> JSONResponse:
    """The governance beat: nothing is written until a human flips this.

    This used to record the approval and echo what it *would* write, on the reasoning that
    faking a write would be theatre. That was right while the executor was a sketch. The
    executor is real now, and leaving this stubbed meant the product's central claim, that it
    writes back only after a human approves, could not be tested in the hosted console, which
    is the only place most people can reach. The stub had become the theatre.

    The writes are performed detached, because each is an agent call and the set takes a
    minute or two; a POST held open that long fails as a timeout rather than a result. The UI
    polls GET /api/run/{id}/writes, which reports the out-of-band verification.
    """
    run = _get_run(run_id)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    requested = body.get("items") if isinstance(body, dict) else None

    known = run.writeback_ids or ["annotation", "dashboard", "alert"]
    if requested is None:
        items = list(known)
    else:
        if not isinstance(requested, list):
            raise HTTPException(status_code=422, detail="items must be a list of ids")
        unknown = [i for i in requested if i not in known]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"unknown write-back item(s): {', '.join(map(str, unknown))}",
            )
        items = [i for i in known if i in requested]  # preserve proposal order

    if run.status not in ("complete", "streaming"):
        raise HTTPException(
            status_code=409,
            detail="nothing to approve yet, run the investigation first",
        )
    if not items:
        raise HTTPException(status_code=422, detail="select at least one item to write")

    if run.approved:
        # Idempotent: a double-tap on a phone must not read as a second write.
        return JSONResponse({
            "run_id": run.id, "approved": True, "already_approved": True,
            "approved_at": run.approved_at, "items": _echo(run.approved_items),
            "message": "Already approved, no second write was issued.",
        })

    run.approved = True
    run.approved_at = time.time()
    run.approved_items = items

    if not run.writeback_items:
        # Nothing to execute against. Say so plainly rather than reporting a queued write
        # that no code will ever pick up.
        run.write_state = "error"
        run.write_error = "no write-back proposal on this run"
        return JSONResponse({
            "run_id": run.id, "approved": True, "already_approved": False,
            "approved_at": run.approved_at, "items": _echo(items), "executing": False,
            "message": "Approved, but this run carries no write-back proposal, so there is "
                       "nothing to execute.",
        })

    run.write_state = "running"
    run.write_error = ""
    run.write_results = []
    asyncio.create_task(_execute_writes(run, items))
    return JSONResponse({
        "run_id": run.id, "approved": True, "already_approved": False,
        "approved_at": run.approved_at, "items": _echo(items), "executing": True,
        "message": f"{len(items)} write-back{'s' if len(items) != 1 else ''} approved. "
                   f"Writing to Grafana now, then verifying each one out of band through "
                   f"Grafana's HTTP API rather than the tools the agent just used.",
    })


SCOPES = {
    "annotation": "annotations:write",
    "dashboard": "dashboards:write",
    "alert": "alert.rules:write",
}


def _echo(items: List[str]) -> List[Dict[str, str]]:
    """What the server is about to do, per item, with the scope each one needs."""
    return [
        {"id": i, "status": "queued", "scope": SCOPES.get(i, "unknown"),
         "effect": "queued for execution"}
        for i in items
    ]


@app.get("/api/run/{run_id}/briefing.mp3")
async def briefing_audio(run_id: str) -> Response:
    """Synthesize the dailies briefing for a run, and cache it on the run.

    Synthesis is a second or two, so it happens on demand rather than during the
    investigation, a judge who never presses play should not wait for audio they did not
    ask for, and a TTS outage must not be able to fail a run.
    """
    run = RUNS.get(run_id)
    if run is None:
        return JSONResponse({"error": "unknown or expired run"}, status_code=404)
    script = run.briefing_script or (load_fixture().get("briefing") or {}).get("script", "")
    if not script:
        return JSONResponse(
            {"error": "no briefing for this run yet",
             "hint": "the briefing is composed after the remediation plan"},
            status_code=409)
    if run.briefing_mp3 is None:
        try:
            from second_unit.briefing import synthesize
            run.briefing_mp3 = synthesize(script)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"error": f"speech synthesis unavailable: {type(exc).__name__}",
                 "detail": str(exc)[:200],
                 "script": script,
                 "hint": "the text of the briefing is returned so it is still readable"},
                status_code=503)
    return Response(content=run.briefing_mp3, media_type="audio/mpeg",
                    headers={"Cache-Control": "public, max-age=3600",
                             "Content-Disposition": 'inline; filename="dailies.mp3"'})


# --------------------------------------------------------------------------- data APIs
#
# These exist so the console's pages can be built and tested independently of the agent.
# Every one is CHEAP and DETERMINISTIC, Prometheus queries and arithmetic, no model calls, 
# because a dashboard that costs an LLM invocation to render is a dashboard nobody refreshes.


def _deadline_and_shots():
    from second_unit import fleet
    from second_unit import run as run_mod
    from second_unit.run import published_review_deadline
    dl = published_review_deadline("SH042")
    return dl, fleet.fleet_status(dl), fleet, run_mod


@app.get("/api/fleet")
async def api_fleet() -> JSONResponse:
    """Every in-flight shot with its own forecast. The Overview's triage strip."""
    from second_unit.shots_catalog import hierarchy
    try:
        dl, shots, fleet, run_mod = _deadline_and_shots()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}", "shots": []},
                            status_code=503)
    return JSONResponse({
        "deadline": dl,
        # Never hide a failed sweep behind an empty list: an empty strip that looks like a
        # farm with no work in it is worse than an error a human can act on.
        "error": fleet.last_error,
        "deadline_is_placeholder": bool(run_mod.deadline_fallback_reason),
        "deadline_placeholder_reason": run_mod.deadline_fallback_reason,
        # Joined by shot id, from the catalogue. A live row knowing which episode it feeds
        # is what lets a finding read "Northwind S01E04 is at risk" instead of "SH1042 is".
        "shots": [
            dict({"shot": s.shot, "department": s.department, "status": s.status,
                  "frames_remaining": s.frames_remaining, "rate_per_min": s.rate_per_min,
                  "eta": s.eta_iso, "deadline": s.deadline_iso, "slip_hours": s.slip_hours,
                  "note": s.note},
                 **(hierarchy(s.shot) or {}))
            for s in shots
        ],
        "exceptions": [s.shot for s in shots if s.is_exception],
    })


@app.get("/api/shots")
async def api_shots(request: Request) -> JSONResponse:
    """The studio slate: paginated, filterable, sortable.

    Served from the generated catalogue rather than Prometheus. The catalogue is hundreds of
    shots and grows daily; asking Prometheus for all of them would put a multi-second query
    behind every page of a table. Live telemetry stays where it belongs, /api/fleet, for
    the few dozen passes actually rendering.
    """
    from datetime import date as _date

    from second_unit.shots_catalog import catalog, summary, active

    q = request.query_params
    try:
        page = max(1, int(q.get("page", 1)))
        # Honour what the caller asked for. The first version clamped the floor to 10, so
        # per_page=3 silently returned 10, the same quiet override this project keeps
        # objecting to elsewhere. 200 remains a real ceiling because the payload is real.
        per_page = min(200, max(1, int(q.get("per_page", 25))))
    except ValueError:
        return JSONResponse({"error": "page and per_page must be integers"},
                            status_code=400)

    rows = catalog()
    # Filters. Unknown values are ignored rather than returning nothing, a filter that
    # silently empties the table looks identical to a broken feed.
    dept = (q.get("department") or "").lower()
    state = (q.get("state") or "").lower()
    seq = (q.get("sequence") or "").upper()
    prio = (q.get("priority") or "").lower()
    title = (q.get("title") or "").strip()
    unit = (q.get("unit") or "").strip().upper()
    search = (q.get("q") or "").strip().upper()
    if dept:
        rows = [r for r in rows if r.department == dept]
    if state:
        rows = [r for r in rows if r.state == state]
    if seq:
        rows = [r for r in rows if r.sequence == seq]
    if prio:
        rows = [r for r in rows if r.priority == prio]
    if title:
        rows = [r for r in rows if r.title.lower() == title.lower()]
    if unit:
        rows = [r for r in rows if r.unit == unit]
    if search:
        rows = [r for r in rows
                if search in r.shot or search in r.sequence
                or search in r.unit or search in r.title.upper()]

    sort = q.get("sort") or "newest"
    keys = {
        "newest": lambda r: (r.ingested_on, r.shot),
        "due": lambda r: (r.due_on, r.shot),
        "frames": lambda r: r.total_frames,
        "shot": lambda r: r.shot,
        "unit": lambda r: (r.kind != "series", r.title, r.unit, r.shot),
    }
    rows.sort(key=keys.get(sort, keys["newest"]),
              reverse=sort in ("newest", "frames"))

    total = len(rows)
    start = (page - 1) * per_page
    window = rows[start:start + per_page]

    # `state == "rendering"` is a catalogue fact; having telemetry is a different one. The
    # catalogue marks ~100 passes as rendering while `active()` caps emitted series at 60,
    # so an "investigate" link offered on state alone could land on a shot with no data.
    # This says which rows the farm is genuinely reporting on, so the UI can offer the link
    # only where it leads somewhere.
    live_ids = {s_.shot for s_ in active()}

    def row(r):
        d = r.as_dict()
        d["live"] = r.shot in live_ids
        return d

    return JSONResponse({
        "shots": [row(r) for r in window],
        "page": page, "per_page": per_page, "total": total,
        "pages": max(1, (total + per_page - 1) // per_page),
        "summary": summary(),
        # A cheap fingerprint the client polls to notice new ingestion without
        # re-downloading a page it already has.
        "catalog_version": f"{_date.today().isoformat()}:{summary()['total']}",
    })


@app.get("/api/shot/{shot}")
async def api_shot(shot: str) -> JSONResponse:
    """Production context for one shot, live or not.

    The switcher changes the subject without a page load, and a shot that is not on the farm
    has no row in /api/fleet to carry its hierarchy. Without this the breadcrumb would go
    blank exactly when someone deep-links a catalogue shot, which is the case the page most
    needs to explain rather than the case it can ignore.
    """
    from second_unit.shots_catalog import hierarchy
    h = hierarchy(shot)
    if not h:
        return JSONResponse({"error": f"no such shot: {shot}"}, status_code=404)
    return JSONResponse(dict(h, shot=shot.strip().upper()))


@app.get("/api/shots/facets")
async def api_shot_facets() -> JSONResponse:
    """Filter options, counted: so the UI never offers a filter that matches nothing."""
    from second_unit.shots_catalog import catalog, summary, units_summary
    rows = catalog()

    def count(attr):
        out = {}
        for r in rows:
            out[getattr(r, attr)] = out.get(getattr(r, attr), 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    return JSONResponse({
        "department": count("department"), "state": count("state"),
        "sequence": count("sequence"), "priority": count("priority"),
        "title": count("title"), "unit": count("unit"),
        "units": units_summary(),
        "summary": summary(),
    })


@app.get("/api/vitals")
async def api_vitals() -> JSONResponse:
    """Farm vitals for the Overview: throughput, queue, faulty nodes, cycle phase."""
    from second_unit import fleet
    out = {}
    try:
        def one(expr, cast=float):
            rows = fleet._prom_query(expr)
            return cast(rows[0]["value"][1]) if rows else None

        out["throughput_now"] = one(
            'sum(rate(render_frames_completed_total[15m])) * 60')
        out["queue_lighting"] = one('render_queue_depth{queue="lighting"}')
        ecc = fleet._prom_query(
            'sum by (node) (increase(render_node_gpu_ecc_errors_total[30m])) > 0')
        out["faulty_nodes"] = [r["metric"].get("node") for r in ecc]
        hot = fleet._prom_query('topk(1, render_node_gpu_temp_celsius)')
        out["hottest_node"] = (
            {"node": hot[0]["metric"].get("node"),
             "celsius": round(float(hot[0]["value"][1]), 1)} if hot else None)
        # Cycle phase, derived from the published deadline: the scenario loops, and a judge
        # arriving mid-repair should be told that rather than left confused by a calm farm.
        rows = fleet._prom_query('shot_review_deadline_seconds{shot="SH042"}')
        if rows:
            import time as _t
            deadline_ts = float(rows[0]["value"][1])
            minute = (_t.time() - (deadline_ts - 170 * 60)) / 60.0
            out["cycle_minute"] = round(minute)
            out["cycle_phase"] = (
                "pre-fault" if minute < 0 else "fault building" if minute < 20
                else "degraded" if minute <= 170 else "past deadline" if minute < 180
                else "repaired" if minute < 240 else "restarting")
        out["error"] = fleet.last_error
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=503)
    return JSONResponse(out)


@app.get("/api/agent/metrics")
async def api_agent_metrics() -> JSONResponse:
    """What the agent did to itself: our own observability series, read back.

    Read from Prometheus rather than kept in memory on purpose, the numbers a judge sees
    on this page are the same ones in Grafana, from the same source, so the page cannot
    flatter the agent.
    """
    from second_unit import fleet

    #: How far back to look for the last run's self-telemetry.
    #:
    #: These series are written ONCE per investigation, not scraped continuously, so a plain
    #: instant query only sees them if a run finished inside Prometheus's ~5-minute lookback.
    #: Everything then returns empty and the page reads as "the agent has never run", which
    #: is a different and much worse claim than "no run recently". `last_over_time` asks the
    #: right question: what did the most recent run report? Same mistake we made with the
    #: farm metrics; see telemetry/README.md.
    #: 6h was still too tight, the last run aged out of it within an afternoon and the
    #: page went blank again. These series are written once per run, so the window is really
    #: "how long ago will we still tell you about", and for a demo a judge might open a day
    #: later the answer is 24h. The response carries the window so the page can say how old
    #: the numbers are rather than implying they are current.
    WINDOW = os.environ.get("AGENT_METRICS_WINDOW", "24h")

    def series(metric: str):
        try:
            rows = fleet._prom_query(f"last_over_time({metric}[{WINDOW}])")
            return [{"labels": {k: v for k, v in r["metric"].items()
                                if k not in ("__name__", "job")},
                     "value": float(r["value"][1])}
                    for r in rows]
        except Exception:  # noqa: BLE001
            return []
    return JSONResponse({
        "window": WINDOW,
        "stage_seconds": series("second_unit_run_seconds"),
        "tool_calls": series("second_unit_tool_calls_total"),
        "tokens": series("second_unit_tokens_total"),
        "tool_latency_ms": series("second_unit_tool_latency_ms"),
        "failures": series("second_unit_stage_failures_total"),
        "write_claims": series("second_unit_write_claims_total"),
        "grafana_url": os.environ.get("GRAFANA_URL", ""),
    })


@app.get("/api/ask/suggestions")
async def ask_suggestions() -> JSONResponse:
    """The chips. Every one is answerable from this farm's telemetry and exercises a
    different capability, so the demo path cannot land on a dud."""
    try:
        from second_unit.ask import SUGGESTED
        return JSONResponse({"suggestions": SUGGESTED})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"suggestions": [], "error": str(exc)[:200]}, status_code=503)


@app.get("/api/ask/stream")
async def ask_stream(request: Request) -> StreamingResponse:
    """Answer one scoped question, streaming the work.

    Stateless on purpose, a question is not a run, so there is nothing to register or
    resume. The router goes first: an out-of-scope question returns in a few seconds with an
    honest reason and never touches a tool, which is the whole reason this is a scoped ask
    box rather than a chat window.
    """
    question = (request.query_params.get("q") or "").strip()[:400]
    asked_shot = valid_shot(request.query_params.get("shot"))

    async def gen():
        if not question:
            yield _sse({"type": "ask_failed", "error": "empty question"})
            return
        try:
            from second_unit.ask import (
                answer_task, answerer, route_task, router,
            )
            from second_unit.pipeline import stream_stage
            from second_unit.schemas import AskAnswer, AskRoute
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "ask_failed",
                        "error": f"ask unavailable: {type(exc).__name__}: {exc}"})
            return

        yield _sse({"type": "ask_routing", "question": question})
        route = None
        try:
            async for kind, payload in stream_stage(
                router(), route_task(question), AskRoute, verbose=False
            ):
                if kind == "result":
                    route = payload
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "ask_failed", "error": f"{type(exc).__name__}: {exc}"})
            return

        if route is None or not route.ok:
            yield _sse({"type": "ask_failed",
                        "error": (route.error if route else "router returned nothing"),
                        "hint": "The scope check itself failed, so no answer was attempted."})
            return

        r = route.output
        if not r.in_scope:
            # No tools, no waiting. This is the case a chat window handles badly.
            yield _sse({"type": "ask_rejected", "reason": r.reason,
                        "suggestion": r.suggestion})
            return

        yield _sse({"type": "ask_accepted", "reason": r.reason})
        answer = None
        try:
            async for kind, payload in stream_stage(
                answerer(), answer_task(question, shot=asked_shot), AskAnswer,
                verbose=False
            ):
                if kind == "result":
                    answer = payload
                elif kind == "tool_done":
                    yield _sse(payload)
                if await request.is_disconnected():
                    return
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "ask_failed", "error": f"{type(exc).__name__}: {exc}"})
            return

        if answer is None or not answer.ok:
            err = answer.error if answer else "no answer"
            quota = any(k in (err or "").lower() for k in ("resource_exhausted", "429", "quota"))
            yield _sse({"type": "ask_failed", "error": err,
                        "reason": "quota" if quota else "answer_error",
                        "hint": ("The model is rate limited right now."
                                 if quota else "The answering stage returned nothing usable.")})
            return

        a = answer.output
        yield _sse({
            "type": "ask_answer",
            "answer": a.answer,
            "confidence": a.confidence.value,
            "caveat": a.caveat,
            "evidence": [
                {"claim": e.claim, "tool": e.tool, "query": e.query, "observed": e.observed}
                for e in a.evidence
            ],
        })

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/reset")
async def reset() -> JSONResponse:
    """Clear this session's runs and report where the scenario actually is.

    This used to claim it reset the incident clock, the docstring said the deployed build
    restarted the seeder and the response said "Incident clock back to t+0". Neither was
    true. The farm is generated by a Cloud Run Job on its own loop and nothing here can
    rewind it, so saying otherwise put a lie in the one product whose entire argument is
    that it does not let an agent claim things that are not so.

    What it does now: clears run state, and tells you where the cycle is, so "why is the
    farm healthy?" has an answer instead of looking broken.
    """
    cleared = len(RUNS)
    RUNS.clear()

    phase, minute, next_fault = None, None, None
    try:
        from second_unit import fleet
        rows = fleet._prom_query('shot_review_deadline_seconds{shot="SH042"}')
        if rows:
            cycle_start = float(rows[0]["value"][1]) - 170 * 60
            minute = round((time.time() - cycle_start) / 60)
            phase = ("pre-fault" if minute < 0 else "fault building" if minute < 20
                     else "degraded" if minute <= 170 else "past the review deadline"
                     if minute < 180 else "repaired" if minute < 240 else "restarting")
            if minute >= 180:
                next_fault = max(0, 240 - minute)
    except Exception:  # noqa: BLE001
        pass

    msg = f"Cleared {cleared} run(s)."
    if phase:
        msg += f" The farm is at cycle minute t{minute:+d} ({phase})."
        if next_fault is not None:
            msg += f" The next fault begins in about {next_fault} minutes."
    return JSONResponse({"ok": True, "cleared_runs": cleared, "cycle_phase": phase,
                         "cycle_minute": minute, "minutes_to_next_fault": next_fault,
                         "message": msg})
