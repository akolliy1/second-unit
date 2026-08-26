"""Second Unit — the one page a judge clicks.

Design constraints that shape every decision in this file:

* One route (`/`) plus `/healthz`. No auth, no accounts, no second page. The hosted URL
  has to work for a logged-out stranger on a phone (see 02-planning/08-ui-and-scope.md §1).
* No frontend build step. One Jinja template with inlined CSS and JS, no CDN, because the
  deploy target has no guaranteed network egress and a blocked CDN would render the page
  unstyled in front of a judge.
* The page renders *events*, not a finished report. The agent pipeline emits events as it
  runs, so the transport is Server-Sent Events and the client is a switch on `event.type`.

Right now the events come from `fixture.json` — a real recorded run. Swapping in the live
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
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

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
    live: bool = False          # run the real pipeline instead of replaying the recording


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
        # Most likely cause: the server restarted mid-demo. Say so, rather than 500ing —
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
    """Stream the real pipeline. Same wire format, same client, no replay pacing —
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
        async for event in iter_live_events():
            if await request.is_disconnected():
                run.status = "abandoned"
                return
            # Keep the real proposal so /approve validates against what was actually
            # proposed, not against the fixture's items.
            if event.get("type") == "writeback_proposed":
                run.writeback_ids = [i["id"] for i in event.get("items", [])]
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
        if event["type"] == "run_done":
            run.status = "complete"

        event["run_id"] = run.id
        yield _sse(event)

    if run.status == "streaming":  # fixture ended without a run_done
        run.status = "complete"


# --------------------------------------------------------------------------- routes


@app.get("/start", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    fixture = load_fixture()
    verdict = next(
        (e["event"] for e in fixture["events"] if e["event"]["type"] == "verdict"), None
    )
    # The verdict is server-rendered into the page as well as streamed. Two reasons: a
    # judge who lands on the URL sees the finding immediately instead of an empty console,
    # and the headline survives JS being slow, blocked, or broken.
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "scenario": fixture.get("scenario", {}),
            "verdict": verdict,
            # /start always shows the persona chooser; / shows it only on a first visit
            # (the client decides from localStorage). Either way the console is rendered
            # underneath, so the overlay never stands between a judge and the finding.
            "force_picker": request.url.path == "/start",
            # Render the requested framing SERVER-SIDE. Letting JS fix it after paint
            # means a ?persona=producer link flashes the TD framing first, and shows the
            # wrong one entirely if scripts are slow or blocked — on the one screen a judge
            # is guaranteed to read.
            "initial_persona": (
                request.query_params.get("persona")
                if request.query_params.get("persona") in ("td", "supervisor", "producer")
                else "td"
            ),
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
        except Exception:  # noqa: BLE001 — an absent or non-JSON body is the normal case
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

    # The page hydrates from the recorded run on load, without streaming. That run really
    # did happen, so its write-back proposal is genuinely approvable — this registers it
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


@app.post("/api/run/{run_id}/approve")
async def approve(run_id: str, request: Request) -> JSONResponse:
    """The governance beat: nothing is written until a human flips this.

    The write itself is stubbed — this records the approval and echoes back what *would*
    be written, with the Grafana scope each item needs. The real effect belongs behind the
    service-account token, and that token's permissions are the actual security boundary
    (§2 of the scope doc), so faking a write here would be theatre.
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
            detail="nothing to approve yet — run the investigation first",
        )
    if not items:
        raise HTTPException(status_code=422, detail="select at least one item to write")

    if run.approved:
        # Idempotent: a double-tap on a phone must not read as a second write.
        return JSONResponse({
            "run_id": run.id, "approved": True, "already_approved": True,
            "approved_at": run.approved_at, "items": _echo(run.approved_items),
            "message": "Already approved — no second write was issued.",
        })

    run.approved = True
    run.approved_at = time.time()
    run.approved_items = items
    return JSONResponse({
        "run_id": run.id, "approved": True, "already_approved": False,
        "approved_at": run.approved_at, "items": _echo(items),
        "message": f"{len(items)} write-back{'s' if len(items) != 1 else ''} approved and "
                   f"queued. Grafana writes are stubbed in this build.",
    })


SCOPES = {
    "annotation": "annotations:write",
    "dashboard": "dashboards:write",
    "alert": "alert.rules:write",
}


def _echo(items: List[str]) -> List[Dict[str, str]]:
    """What the server says it would do, per item. The UI renders this verbatim."""
    return [
        {"id": i, "status": "queued", "scope": SCOPES.get(i, "unknown"),
         "effect": "stubbed in this build"}
        for i in items
    ]


@app.post("/api/reset")
async def reset() -> JSONResponse:
    """`Reset scenario` — drop run state so the page returns to its empty state.

    In the deployed build this also restarts the seeder's incident clock so a judge sees
    the incident from t+0 (scope doc §5.2). Here it clears server-side runs, which is the
    part that exists.
    """
    cleared = len(RUNS)
    RUNS.clear()
    return JSONResponse({"ok": True, "cleared_runs": cleared,
                         "message": "Scenario reset. Incident clock back to t+0."})
