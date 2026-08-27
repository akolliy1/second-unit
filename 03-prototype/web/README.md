# Second Unit, web UI

One page. The verdict a producer acts on, the pipeline that produced it, the queries
behind every claim, and an approval gate in front of the only privileged action.

## Run it

```bash
cd 03-prototype/web
../spike/.venv/bin/python -m uvicorn app:app --port 8080 --reload
# then open http://localhost:8080
```

Dependencies (`fastapi`, `uvicorn`, `jinja2`) are already installed in
`03-prototype/spike/.venv`. For a fresh environment: `pip install -r requirements.txt`.

## Routes

| Route | What it does |
|---|---|
| `GET /` | the page, one Jinja template, inlined CSS and JS, zero external requests |
| `GET /healthz` | `{"ok": true}` |
| `POST /api/run` | starts a run, returns `{run_id, stream}` |
| `GET /api/run/{id}/stream` | Server-Sent Events; one JSON object per event |
| `POST /api/run/{id}/approve` | records approval of the write-back and echoes what would be written |
| `POST /api/reset` | `Reset scenario`, drops run state, returns the page to its empty state |

## Environment

| Variable | Default | Effect |
|---|---|---|
| `GRAFANA_URL` | *(unset)* | base for the "open in Grafana" deeplinks. Unset is handled: the evidence panel says deeplinks are off rather than rendering dead links. |
| `REPLAY_SPEED` | `8` | the recorded run took 129.5s; replay is compressed by this factor. `1` plays it at true speed. |

## Where the data comes from

`fixture.json` is a **recorded run**, so the whole UI is developable and demoable with no
agent and no Grafana stack. The Watchtower and Diagnostician payloads are the real output
of the 2026-08-26 run (9 tool calls / 45.0s and 6 tool calls / 66.1s). `impact_forecaster`
is not built yet: its payload is invented, but every number in it is a real measured one
(1,619 frames, 10.1 → 5.5 fr/min, 2h 18m past the Friday 14:00 review).

Field names match `agent/second_unit/schemas.py` **exactly**. Wiring the live pipeline in
means replacing `_iter_events()` in `app.py` with the ADK event stream, the wire format
and the entire client stay as they are.

## Demoing the states

Loading / error / empty are designed states, not accidents, and each is reachable:

- `/?demo=empty`, nothing investigated yet
- `/?demo=loading`, every zone mid-flight
- `/?demo=error`, a stage whose output failed schema validation, pipeline halted
- `/?inject=diagnostician` then press **Run investigation**, the same failure over the
  real SSE transport, not faked client-side
- `/?persona=producer`, deep-link a particular framing

Also handled: stream dropped mid-run, unknown/expired run id after a server restart,
approval with nothing selected, double-approval (idempotent), and `GRAFANA_URL` unset.

## The persona toggle

`TD | Supervisor | Producer` reframes the same finding, client-side, no reload. It changes
the headline, the recommended action, the trade-off, and how much machinery is on screen
(a producer does not get PromQL by default; a TD does). It is a demo beat and a translation
layer, deliberately **not** an auth boundary. There is no login on this page, by design:
the hosted URL must work for a logged-out stranger on a phone.

The real permission boundary is the Grafana service-account token, enforced server-side by
Grafana. The approval gate is the one thing standing between the agent and using it.
