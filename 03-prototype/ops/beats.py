"""The demo video's beat sheet, as data.

Two texts per beat, deliberately:

  `say`      what Cloud TTS is given. Shot ids and figures are spelled the way a person
             reads them aloud, because "SH042" is synthesised as something between "sh
             zero forty two" and nonsense, and a judge hearing gibberish in the first ten
             seconds stops listening.
  `caption`  what goes in the subtitle file, spelled the way it appears on screen. The
             rules require English or subtitles, and a caption that disagrees with the UI
             it sits under is worse than none.

`actions` is what the recorder does while that beat plays, in order. The dwell comes from
the audio's real measured length, never from a guess here, so narration and picture cannot
drift apart.

Order and content follow 04-submission/video-script.md. Change that file and this one
together, or the video stops matching the plan it was reviewed against.
"""

# Hard rule from the hackathon rules. Enforced in voiceover.py, not trusted to judgement.
MAX_SECONDS = 180.0

BEATS = [
    dict(
        n=0, name="What it is", source="browser",
        say="Second Unit is an autonomous pipeline SRE for a visual effects render farm. "
            "Every number on this page is read from the live system.",
        caption="Second Unit is an autonomous pipeline SRE for a VFX render farm. "
                "Every number on this page is read from the live system.",
        # Held until every stat has a real number. The strip counts up from placeholders as
        # its fetches land, so a fixed wait filmed the numbers mid-flight while the narration
        # said they were live. This runs before the capture starts, so the wait is free.
        actions=[("goto", "/"),
                 ("until",
                  "[...document.querySelectorAll('[data-stat]')].length >= 4 && "
                  "[...document.querySelectorAll('[data-stat]')].every("
                  "e => /[0-9]/.test(e.textContent) && !/[.\u2026]{2,}/.test(e.textContent))",
                  30),
                 ("wait", 1.5), ("scroll", 0)],
    ),
    dict(
        n=1, name="The problem", source="browser",
        say="A visual effects house lives and dies by the client review date. When the farm "
            "degrades, the telemetry is already in Grafana. Nobody who needs it can read it.",
        caption="A VFX house lives and dies by the client review date. When the farm "
                "degrades, the telemetry is already in Grafana. Nobody who needs it can read it.",
        actions=[("scrollto", "#why"), ("wait", 2.0)],
    ),
    dict(
        n=2, name="The premise", source="browser",
        say="Three people need the same answer in three different languages. The producer "
            "doesn't write Prom Q L.",
        caption="Three people need the same answer in three different languages. "
                "The producer doesn't write PromQL.",
        # The chooser is the subject of this beat, so it is held long enough to read before
        # the click. Previously it flashed past and the beat showed a half-loaded Overview.
        actions=[("goto", "/start"), ("wait", 3.5),
                 ("click", "a[href*='persona=producer']"), ("wait", 1.5)],
    ),
    dict(
        n=3, name="The verdict", lead=5.0, source="browser",
        say="Shot forty two misses Friday's two p.m. client review by two hours and eighteen "
            "minutes. Capacity is down forty six percent, and it puts Northwind, episode "
            "four, at risk. That sentence is what the system exists to produce.",
        caption="SH042 misses Friday's 14:00 client review by 2h 18m. Capacity is down 46%. "
                "It puts Northwind S01E04 at risk. That sentence is what the whole system "
                "exists to produce.",
        # A fixed 2.5s wait was both too long when the page was quick and too short when it
        # was not, and the whole sequence outran its lead, so this beat opened on the Overview
        # it had just left while the voice read the verdict. Waiting on the verdict itself
        # returns the moment it exists.
        actions=[("goto", "/investigation?persona=producer"),
                 ("until", "!!document.querySelector('[data-f=\"headline\"]') && "
                           "document.querySelector('[data-f=\"headline\"]').textContent.trim().length > 10",
                  20),
                 ("scrollto", ".verdict")],
    ),
    dict(
        n=4, name="It is real", source="browser",
        say="Same investigation, in an engineer's framing. Every claim carries the query "
            "that produced it. These are live calls against a real Grafana Cloud stack, "
            "through the M C P server.",
        caption="Same investigation, engineer's framing. Every claim carries the query that "
                "produced it: live MCP calls against a real Grafana Cloud stack.",
        actions=[("persona", "td"), ("wait", 1.0), ("tab", "evidence"), ("wait", 1.5),
                 ("scrollto", "#evZone")],
    ),
    dict(
        n=5, name="It discriminates", source="browser",
        # Rewritten to describe the hypotheses this run actually killed. The earlier text
        # described an asset pipeline from a different run, so the beat would have narrated
        # one thing while the screen showed another.
        say="It also rules things out. It asked whether the other five nodes were failing too, "
            "and whether the render software was at fault, and killed both. An agent that "
            "pattern matches on errors keeps all three.",
        caption="It also rules things out. It asked whether the other five nodes were failing "
                "too, and whether the render software was at fault, and killed both: the peers "
                "report zero failed frames and normal temperatures, and lighting frames on "
                "healthy nodes complete fine. An agent that pattern-matches on 'lots of "
                "errors' keeps all three.",
        actions=[("tab", "pipeline"), ("wait", 1.0),
                 # The hypothesis list is inside the Diagnostician's detail, collapsed by
                 # default. A bare click toggles, so it must be an idempotent expand, and the
                 # scroll target must be checked for visibility rather than existence.
                 ("expand", '.stage[data-stage="diagnostician"] .stage-head'),
                 ("wait", 1.2),
                 ("scrollto", '.hyp li[data-verdict="ruled_out"]')],
    ),
    dict(
        n=6, name="The counterfactual", source="browser",
        say="The plan says drain render zero seven. Does that fix it? Draining costs one node "
            "of six; the remaining five clear the backlog at nine point four frames a minute, "
            "and the shot lands sixteen minutes before the review. That's arithmetic in "
            "Python, not the model's opinion.",
        caption="The plan says drain render-07. The next question is whether that fixes it, so "
                "we compute it: draining costs 1 of 6 nodes, the remaining five clear the "
                "backlog at 9.4 fr/min, and SH042 lands ~16 min before the review instead of "
                "2h 18m after. Arithmetic in Python, not the model's opinion.",
        # The trade-off card carries exactly that sentence, so the narration and the screen
        # say the same thing at the same moment.
        actions=[("scrollto", '[data-f="risk"]'), ("wait", 1.5)],
    ),
    dict(
        n=7, name="The gate", source="browser",
        say="Nothing is written until a human says yes. And the stage that proposes the write "
            "back holds no write tools at all. It can't be talked into writing, because it "
            "has nothing to write with.",
        caption="Nothing is written until a human says yes. And the stage that proposes the "
                "write-back holds no write tools at all: it cannot be talked into writing, "
                "because it has nothing to write with.",
        actions=[("tab", "writeback"), ("wait", 1.5), ("scrollto", "#btnApprove")],
    ),
    dict(
        n=8, name="The briefing", lead=3.0, source="browser",
        # Asked for explicitly: the TTS dailies briefing was never in the beat sheet, so the
        # video omitted a feature the product leads with.
        say="The same finding, spoken. A forty eight second dailies standup for a producer "
            "who is walking into a meeting, or the transcript if they would rather read it.",
        caption="The same finding, spoken: a ~48-second dailies standup for a producer walking "
                "into a meeting, or the transcript if they'd rather read it.",
        actions=[("goto", "/investigation?persona=producer"),
                 ("until", "!!document.getElementById('btnBrief')", 25),
                 ("scrollto", "#briefing"),
                 ("click", "#briefing .transcript, #briefing a, #briefing button + *"),
                 ("wait", 1.2)],
    ),
    dict(
        n=9, name="Ask", source="browser",
        say="And you can ask it things. Every question is routed before it is answered, so an "
            "out of scope one comes back in seconds instead of after a minute of pointless "
            "querying.",
        caption="And you can ask it things. Every question is routed before it's answered, so "
                "an out-of-scope one comes back in seconds instead of after a minute of "
                "pointless querying.",
        actions=[("scrollto", "#ask"),
                 ("click", "#askChips button"),
                 ("wait", 2.0)],
    ),
    dict(
        n=10, name="Verification", lead=5.0, source="browser",
        # True again, and narrower than before. The live run on 2026-08-28 claimed an alert
        # rule that never appeared in the stack: false_success went from 0 to 1. The text has
        # now been wrong in both directions, which is the argument for checking rather than
        # for trusting either the agent or the script.
        say="Here's the part I care about. This run claimed an alert rule that isn't there. "
            "Every write is checked out of band, through Grafana's own A P I, not the tools "
            "the agent just used. An agent that explains away its own mistakes sends a "
            "technician to inspect a healthy machine.",
        caption="Here's the part I care about. This run claimed an alert rule. It wasn't "
                "there. Every write is checked out of band, through Grafana's API, not the "
                "tools the agent just used, and the check disagreed with the agent. An agent "
                "that explains away its own mistakes sends a technician to inspect a healthy "
                "machine.",
        actions=[("goto", "/agent?persona=td"),
                 ("until", "document.getElementById('claimZone')?.dataset.state === 'ready'", 40),
                 ("scrollto", "#claimSection"), ("wait", 1.5)],
    ),
    dict(
        n=11, name="The agent, observed", source="browser",
        # Was going to be Grafana's AI Observability UI, which needs an interactive login the
        # recorder does not have. Rather than narrate over a login page or claim something not
        # on screen, this shows our own agent telemetry in the same stack, which is true and
        # is the actual point: one backend for both roles.
        say="Which is itself a metric. Every stage's wall clock, tool calls and token cost "
            "land in the same Grafana stack the agent queries. Grafana is the agent's tool "
            "surface and its telemetry backend.",
        caption="Which is a metric. Every stage's wall clock, tool calls and token cost land "
                "in the same Grafana stack the agent queries, alongside ADK's own GenAI "
                "traces. Grafana is the agent's tool surface AND its telemetry backend.",
        # No navigation: the verification beat already put us on /agent, and reloading it
        # only bought a loading skeleton under this beat's opening line. Cheaper to not move
        # than to pay for moving.
        actions=[("tab", "latency"), ("wait", 1.2), ("scrollto", "#latZone")],
    ),
    dict(
        n=12, name="Close", lead=6.0, source="browser",
        say="Gemini on Vertex, A D K, Cloud Run. Grafana Cloud M C P, seventy six tools, read "
            "and write. Second Unit.",
        caption="Gemini on Vertex AI, ADK, Cloud Run. Grafana Cloud MCP, 76 tools, read and "
                "write. Second Unit.",
        # Was a third look at /agent, which beats 8 and 9 already own, while narrating the
        # stack. The landing page's "Built on" section names exactly these components and
        # runs the tool marquee, so the close shows what the close is about.
        actions=[("goto", "/"), ("wait", 1.5), ("scrollto", "#built"), ("wait", 1.0)],
    ),
]
