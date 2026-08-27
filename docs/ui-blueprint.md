# UI blueprint, from one page to a product surface
Decided 2026-08-26. Supersedes §3 of `08-ui-and-scope.md`, which specified a single page.

## Why this changes

§3 was the right call for a submission that had nothing built. It is the wrong call now.
The judges spend their time *in the interface*, and a single scrolling page, however well
finished, reads as a demo. The system underneath already has more surface than one page can
express honestly: a fleet sweep across every shot, a five-stage investigation, an approval
gate, a spoken briefing, and the agent's own telemetry. Flattening all of that into one
column is not restraint, it is under-representation.

So: a real information architecture, built to the standards of an operator console, with the
prototype's honesty intact: every screen shows something that actually works.

**The discipline that does not change:** no auth, nothing that blocks a logged-out judge,
and every page must be useful on first paint.

---

## 1. The model behind the IA

An operator console answers four questions, in this order:

1. **What needs me?**, across everything, right now.  → **Overview**
2. **Why, and what should I do?**, one thing, in depth. → **Investigation**
3. **Where does my work stand?**, the delivery view.    → **Shots**
4. **Do I trust the thing that told me?**, the agent.   → **Agent**

Those are the four pages. Anything that does not answer one of them does not get a page.
The fifth surface is not a page: it is the **documentation drawer**, because the answer to
"what am I looking at?" should never cost a navigation.

## 2. Pages

### `/start`. Role
A standalone entry page, not a modal. Three cards: Pipeline TD, VFX Supervisor, Producer,
each stating plainly what that person sees and what they can act on. Beneath them, a live
one-line farm summary so the page is informative even before a choice is made.

Why a page rather than the modal we shipped first: a modal says "dismiss me to get to the
real thing". A page says "this choice is part of the product". It is also the honest place
to explain that this is a viewing lens, not a login, which is a distinction the modal had
to make in six words and can now make properly.

Sets the persona (localStorage), then routes to Overview. `?persona=` still deep-links past
it, and `/` never requires it.

### `/`. Overview  *(the landing)*
The producer's question, answered above the fold.

- **Fleet triage strip**, every in-flight shot: status, frames left, rate, ETA vs review,
  slip. Computed in Python with no model involved (`fleet.py`), so it is always consistent
  and cannot hallucinate a shot. Exceptions sort to the top.
- **Active incident card**, the current verdict in the reader's persona, with a single
  primary action: *Investigate*.
- **Farm vitals**, the four numbers that matter: throughput vs baseline, lighting queue
  depth, nodes with faults, the scenario's cycle phase.
- **Agent health**, one line: last run, tool calls, and how many of its write claims
  survived verification. Links to `/agent`.

### `/investigation`. Investigation
What is currently the whole product, given its own page and room to breathe: the five-stage
stream with live tool calls, the evidence panel with the exact PromQL/LogQL and Grafana
deeplinks, the ruled-out hypotheses, the counterfactual, the approval gate, and the dailies
briefing. Deep-linkable per run (`/investigation?run=<id>`).

### `/shots`. Delivery
The supervisor's view. Every shot as a row: department, frames remaining, completion rate,
ETA against its published review deadline, and the burn-down. `/shots/{shot}` drills into
one pass. This is where the *published* review deadline is made visible as a fact of the
production rather than a number in a verdict.

### `/agent`. Agent
The page almost nobody builds, and the one a Grafana judge will recognise. Our agent, observed:

- per-stage duration, tool-call counts and latencies, token usage by stage
- failure reasons, bucketed (quota, budget exceeded, unparseable, no response)
- **write-claim verification**: confirmed / honest failure / unchecked / **false success**
- a link straight into Grafana Cloud AI Observability for the same run's GenAI spans

It exists because the agent's self-report is a claim, and a console that presents claims
without a way to check them is the thing we are arguing against.

## 3. Chrome

**Left rail**, the four pages, icon + label, collapsible to icons under 1100px, hidden
behind a menu button under 720px. Current page is unambiguous. No nested nav: four items do
not need a tree.

**Top bar**, product mark; the persona as a chip that links back to `/start` ("Viewing as
Producer"), which resolves the two-controls-doing-one-job problem the modal created; farm
freshness dot; and the docs toggle.

**Right documentation drawer**, the pattern Google Cloud console uses, for the same reason:
an operator console is full of terms that need one sentence of explanation, and making
people leave to get it is hostile. Contextual per page, keyboard-dismissable, remembers
open/closed. Content is written per page and explains *what you are looking at and why it is
trustworthy*, where each number came from, what a status means, what the agent may and may
not do.

## 4. What we are deliberately still not building

No accounts, no settings, no run history browser, no multi-farm switcher, no theming, no
notifications, no export. Each is a plausible fifth page and none of them answers one of the
four questions.

## 5. Build order

1. Shared layout: tokens, rail, top bar, drawer mechanics  ← the skeleton everything hangs on
2. Move the existing page to `/investigation` unchanged, so nothing that works breaks
3. `/` Overview with the fleet strip
4. `/start` as a page
5. `/shots`
6. `/agent`
7. Drawer content per page

Steps 5 and 6 are independent of each other and of the skeleton once it exists, so they can
be built in parallel against a fixed contract.
