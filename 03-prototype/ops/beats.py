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
        actions=[("goto", "/"), ("wait", 1.2), ("scroll", 0)],
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
            "does not write Prom Q L.",
        caption="Three people need the same answer in three different languages. "
                "The producer doesn't write PromQL.",
        actions=[("goto", "/start"), ("wait", 1.5), ("click", "a[href*='persona=producer']")],
    ),
    dict(
        n=3, name="The verdict", source="browser",
        say="Shot forty two's lighting pass misses Friday's review by one point six hours. "
            "Capacity is down forty six percent. It puts Northwind, episode four, at risk. "
            "That sentence is what the whole system exists to produce.",
        caption="SH042's lighting pass misses Friday's review by 1.6 hours. Capacity is down "
                "46%. It puts Northwind S01E04 at risk. That sentence is what the whole "
                "system exists to produce.",
        actions=[("goto", "/investigation?persona=producer"), ("wait", 2.5),
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
        say="It also rules things out. The asset pipeline was throwing warnings the whole "
            "time. Loud, plausible, and irrelevant, because they started before the incident. "
            "An agent that pattern matches on lots of warnings gets this wrong.",
        caption="It also rules things out. The asset pipeline was throwing warnings the whole "
                "time: loud, plausible, and irrelevant, because they started before the "
                "incident. An agent that pattern-matches on 'lots of warnings' gets this wrong.",
        actions=[("tab", "pipeline"), ("wait", 1.5), ("scrollto", ".ruledout")],
    ),
    dict(
        n=6, name="The counterfactual", source="browser",
        say="The plan says drain render zero seven. The next question is whether that "
            "actually fixes it, so we compute it. Six nodes at eight point seven frames a "
            "minute lands twenty six minutes inside the review. That is arithmetic in "
            "Python, not the model's opinion.",
        caption="The plan says drain render-07. The next question is whether that fixes it, so "
                "we compute it: six nodes at 8.7 frames/min lands 26 minutes inside the "
                "review. Arithmetic in Python, not the model's opinion.",
        actions=[("tab", "writeback"), ("wait", 2.0), ("scrollto", "#wbZone")],
    ),
    dict(
        n=7, name="The gate", source="browser",
        say="Nothing is written until a human says yes. And the stage that proposes the write "
            "back holds no write tools at all. It cannot be talked into writing, because it "
            "has nothing to write with.",
        caption="Nothing is written until a human says yes. And the stage that proposes the "
                "write-back holds no write tools at all: it cannot be talked into writing, "
                "because it has nothing to write with.",
        actions=[("wait", 2.0), ("scrollto", ".approve")],
    ),
    dict(
        n=8, name="Verification", source="terminal",
        say="Here is the part I care about. The agent reported creating a dashboard. It did "
            "not. We check every write out of band, through Grafana's own A P I, not the "
            "tools the agent just used, and we print the disagreement. An observability "
            "agent that explains away its own mistakes is worse than no agent. It sends a "
            "technician to inspect a healthy machine.",
        caption="Here is the part I care about. The agent reported creating a dashboard. It "
                "didn't. We check every write out of band, through Grafana's API, not the "
                "tools the agent just used, and print the disagreement. An observability "
                "agent that explains away its own mistakes is worse than no agent: it sends "
                "a technician to inspect a healthy machine.",
        actions=[("hold",)],          # a separately captured terminal, see record_demo.py
    ),
    dict(
        n=9, name="The agent, observed", source="grafana",
        say="Which is itself a metric. Alongside A D K's own GenAI traces in Grafana's A I "
            "Observability. Model calls, tool calls, token cost per stage. Grafana is the "
            "agent's tool surface and its telemetry backend.",
        caption="Which is a metric. Alongside ADK's own GenAI traces in Grafana AI "
                "Observability: model calls, tool calls, token cost per stage. Grafana is the "
                "agent's tool surface AND its telemetry backend.",
        actions=[("hold",)],          # Grafana's own UI, captured separately
    ),
    dict(
        n=10, name="Close", source="browser",
        say="Gemini on Vertex, A D K, Cloud Run. Grafana Cloud M C P, seventy six tools, read "
            "and write. Second Unit.",
        caption="Gemini on Vertex AI, ADK, Cloud Run. Grafana Cloud MCP, 76 tools, read and "
                "write. Second Unit.",
        actions=[("goto", "/agent"), ("wait", 1.5), ("scroll", 0)],
    ),
]
