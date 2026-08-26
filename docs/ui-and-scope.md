# UI, users, and scope — decided 2026-08-26

12 days to the operating deadline, solo. This document exists to stop us building a
platform when the thing being judged is **one link and a three-minute video**.

---

## 1. Is a UI required? Yes, and it is a disqualifying gate

> "URL to the **hosted, running project**" — judges will click and test it.
> "**Design** — delivers a complete product experience" — 25% of the score.

So a UI is not optional. But read what the gate actually demands: a stranger clicks a link
and something coherent happens. It does not demand a platform, an admin area, or user
accounts. Every hour spent on surface a judge will not click is an hour stolen from the
video, which is the artifact they actually experience.

**Verdict: ONE page. Not a multi-page app.**

---

## 2. RBAC: no. This is the most important "no" in this document

Deep RBAC would be actively harmful here, for four reasons:

1. **It breaks the gate.** The hosted URL must work for a logged-out stranger on a phone,
   with no Google session. Any login wall risks a judge bouncing — and "it worked on my
   machine with my account" is how submissions die. So there is **no auth at all** on the
   demo URL. With no auth, roles have nothing to attach to.
2. **The real permission boundary already exists and is not ours.** The Grafana service
   account token carries the actual authority (`dashboards:write`, `alert.rules:write`,
   `annotations:write`). That is enforced by Grafana, server-side, and we cannot weaken or
   fake it. App-level roles on top would be decoration over someone else's ACL.
3. **We have exactly one privileged action** — the write-back. It is already governed, and
   governed in the way that scores: a forced-function-calling approval gate. That is a
   boolean a human flips, not a role matrix.
4. **It is invisible to judging.** None of the four criteria reward an access-control
   model. Potential Impact rewards the *verdict*; Design rewards *finish*.

**What we build instead of RBAC:** a **persona toggle** — the same investigation, reframed.
Cheap to build, and it makes the entire thesis of the project visible in one click:

| Persona | Sees the same finding as |
|---|---|
| **Pipeline TD** | `render-07` ECC errors → 46% throughput loss on the lighting queue, PromQL and log lines cited |
| **VFX Supervisor** | SH042's lighting pass is 2.3h behind; one node is the cause; here is the fix and its risk |
| **Producer** | SH042 misses Friday's 14:00 review by ~2h. Three artists idle from 08:00. Approve the fix? |

That toggle *is* the translation layer the whole project claims to be. It is a demo beat,
not an auth system. It is read-only and needs no accounts.

---

## 3. The page, in detail

One route (`/`), one column, five zones top to bottom. Dark "studio console" palette.

```
┌──────────────────────────────────────────────────────────────────────┐
│  SECOND UNIT            [ TD | Supervisor | Producer ]   ● live      │  persona + data freshness
├──────────────────────────────────────────────────────────────────────┤
│  ⚠ SH042 · LIGHTING PASS                                             │
│  Misses Friday 14:00 review by 2h 18m   ·   capacity −46%            │  THE VERDICT. Biggest type
│  1,619 frames left · 5.5 fr/min (was 10.1) · ETA Fri 16:18           │  on the page.
├──────────────────────────────────────────────────────────────────────┤
│  ▸ Watchtower        3 tool calls   2 alerts, 41 error lines         │
│  ▾ Diagnostician     6 tool calls                                    │  streams live as the
│      render-07 uncorrectable ECC (Xid 48) since 02:14                │  pipeline runs. each stage
│      → frames failing exit 139 → lighting queue 62 (was 18)          │  expands to its evidence
│      ✗ ruled out: asset-pipeline cache misses (predate incident)     │  ← the decoy, dismissed
│  ▸ Impact Forecaster 2 tool calls                                    │
├──────────────────────────────────────────────────────────────────────┤
│  EVIDENCE   query_prometheus · topk(3, increase(...ecc...[30m]))     │  the actual queries,
│             query_loki_logs  · {service="render-node"} |= "uncorr…"  │  with deeplinks into
│             [open in Grafana ↗]                                      │  the real stack
├──────────────────────────────────────────────────────────────────────┤
│  PROPOSED WRITE-BACK — requires approval                             │
│   □ annotation on the render-farm timeline                           │  the governance beat.
│   □ incident dashboard "SH042 lighting slip"                         │  nothing runs until a
│   □ alert rule: ECC errors > 0 for 5m                                │  human clicks.
│                            [ Approve and write ]                     │
└──────────────────────────────────────────────────────────────────────┘
      [ Run investigation ]        [ Reset scenario ]
```

**Zones, and why each earns its place:**
1. **Verdict banner** — the one sentence a producer acts on, in the largest type on the
   page. If a judge reads nothing else, they read this.
2. **Stage stream** — proves the pipeline is deterministic and multi-step, live, with real
   tool-call counts. This is the "agent actually functioning" evidence the rules demand.
3. **Ruled-out line** — showing the decoy *dismissed* is worth more than showing the right
   answer found. It is the difference between reasoning and grep.
4. **Evidence panel** — every claim carries the query that produced it, plus a deeplink into
   the live stack. Makes runtime MCP usage trivial for a skeptical judge to verify.
5. **Approval gate** — the governance story, as a button.

**Explicitly NOT building:** login, user management, settings, run history, multi-shot
dashboards, dark/light toggle, mobile-specific layout (responsive is enough), notifications,
export. Each is a day we do not have.

### 3.1 The profile picker — APPROVED for Day H, 2026-08-26
A *persona-selection* entry screen, not authentication: no accounts, no credentials, nothing
to sign into. "Who's viewing?" — Pipeline TD / VFX Supervisor / Producer — then the console.

Worth doing for a reason beyond IA: it states the project's thesis before the agent runs.
Three people need the same answer in three different languages, and putting that choice first
makes the translation layer the *premise* rather than a toggle discovered later. It is also a
strong opening shot for the 3-minute video.

Two non-negotiable constraints, both protecting the disqualifying gate:
- **`/` still lands on the console.** The picker is a first-visit overlay (remembered in
  `localStorage`) plus a dedicated `/start` route for deliberate linking. A judge must never
  meet an interstitial between themselves and the finding.
- **Always skippable** — a visible "just show me the incident", and `?persona=` bypasses it.

This is distinct from the RBAC proposal rejected in §2, and the distinction is the whole
point: choosing a viewing lens costs nothing and needs no accounts; enforcing *permissions*
would require auth, which breaks the gate and guards data we do not have.

---

## 4. Tech choice: no frontend build step

**FastAPI + one Jinja template + vanilla JS over Server-Sent Events.** Cloud Run.

Rationale: ADK emits events as the pipeline runs; SSE streams them to the page with about
twenty lines on each side. Adding React/Vite buys component ergonomics we do not need for
one page and costs an npm toolchain, a build step, and a deploy surface. The UI work here is
roughly **one focused day**, not three — but only if we hold this line.

---

## 5. Automation: three pieces, all necessary

The one that is easy to miss and would quietly fail us:

**5.1 The demo must be evergreen.** Judging happens days after submission, at an hour we do
not choose. A judge clicking at 03:00 on Sept 12 must find a *live* incident — not a farm
that finished rendering, and not a dead endpoint. So:
- `seed.py` runs as a **Cloud Run Job on Cloud Scheduler**, streaming continuously.
- It needs a **`--loop` mode**: replay the scenario on a cycle (healthy baseline → fault →
  cascade → resolve → repeat, ~3.5h) so there is always an incident in range and the data
  never runs out. ✅ **Built 2026-08-26.** `seed.py --live --loop`: fault at t+0, node
  drained and repaired at t+150 (queue drains over ~20m), scenario restarts at t+210.
  Verified end to end: lighting queue 18 → 63 → 17, and the ECC counter freezes on repair
  rather than decreasing, so `increase()` stays honest. The repair phase also gives the
  agent something to *confirm* when it re-runs after a write-back.
- The write token moves to **Secret Manager**; the job reads it at runtime.

**5.2 One-click reproducibility.** `Run investigation` triggers a fresh pipeline run against
current data. `Reset scenario` restarts the incident clock so a judge sees it from t+0.
Without these, the judge's experience depends on when they happen to arrive.

**5.3 Baseline dashboards.** Two hand-built dashboards so the stack looks like a working
studio's rather than an empty tenant — and so the agent's written-back dashboard has
something to sit beside. Provisioned as JSON via the Grafana API, checked into the repo.

---

## 6. Google Stitch, Antigravity, and the $100 credit — clearing up three things

**The $100 credit is GCP billing credit.** It pays for Vertex AI inference, Cloud Run,
Cloud Scheduler and Secret Manager on `second-unit-506700`. It does **not** grant or unlock
separate Google products. Antigravity, Stitch, Jules and Gemini Code Assist each have their
own access terms; none is "included" with the coupon. So the credit is not a reason to adopt
any of them.

**Google Stitch** (AI UI design → mockups/code): genuinely useful for generating a visual
language quickly. But for a **single page whose layout is already specified above**, it
solves a problem we do not have, and exporting its output into our FastAPI template is
friction. **Recommendation: skip.** If the page looks flat once it is real, timebox 45
minutes with Stitch on the verdict banner only — the one zone where visual weight matters.

**Antigravity / another agentic IDE:** the switching cost is the problem, not the tool. It
would not share this session's context, the repo conventions, or the three dependency traps
already logged — so it starts by rediscovering what we know. **Recommendation: no.** Not
because it is weak, but because mid-sprint tool adoption is how solo builds lose a day.

---

## 7. Where parallelism actually helps

The core pipeline must stay coherent — five stages with structured handoffs, written by one
mind. Splitting it produces integration debt we cannot afford. But three artifacts are
genuinely independent and can be built alongside it:

| Work | Independent? | Notes |
|---|---|---|
| **Baseline Grafana dashboards** (JSON + provisioning script) | ✅ fully | needs only the stack URL and the metric names, both fixed |
| **UI shell** (static page, layout above, mock data, SSE client) | ✅ mostly | contract is the event schema; wire to the real stream later |
| **Video script + README + judging traceability** | ✅ fully | needs the verdict text, which is already written |
| **The five-stage agent pipeline** | ❌ no | one mind, or it will not cohere |
| **Deploy (Agent Engine, Cloud Run, Secret Manager)** | ❌ no | depends on the pipeline existing |

---

## 8. Revised remaining schedule

| Day | Date | Work |
|---|---|---|
| C | Wed Aug 26 | Agent pipeline: Watchtower + Diagnostician against real seeded data |
| D | Thu Aug 27 | Impact Forecaster + approval gate + Remediator write-back |
| E | Fri Aug 28 | UI: the page above, wired to the live event stream |
| F | Sat Aug 29 | Grafana Agent Observability on our own agent · `--loop` mode for the seeder |
| G | Sun Aug 30 | Deploy: Agent Engine + Cloud Run + Scheduler + Secret Manager. Logged-out test. |
| H | Mon Aug 31 | Product finish: loading/error/empty states, the two baseline dashboards |
| I | Tue Sep 1 | The 3-minute video. Full day. |
| J | Wed Sep 2 | Submit. Editing after submission is free. |
| — | Sep 3–7 | Buffer. Do not spend it in advance. |
