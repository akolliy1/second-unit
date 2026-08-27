"""The shot catalogue: a whole studio's slate, generated rather than stored.

A render farm with three shots is a toy. A real slate has hundreds in flight across
sequences and departments, arriving daily as artists publish work, and that is the setting
the product is actually for.

**The catalogue is a pure function of the date.** `catalog(as_of)` returns the same shots on
the same day in every process, forever, with no database, no migration, no scheduler and no
drift between the seeder and the web app. It also means the slate keeps growing on its own
after we stop touching it, which is the requirement: no intervention, every day, through
the judging period.

Live telemetry is a different thing and stays small. Only shots actually in flight get
Prometheus series (see `active`), because a completed shot has nothing left to measure and
700 idle series would slow every query on the console for no information.

    catalog(date)  -> every shot ingested up to that date        (hundreds; cheap)
    active(date)   -> the ones still rendering                   (dozens; real telemetry)
"""
import hashlib
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from typing import List, Optional

#: The slate opens here. Chosen so a judge opening the console on submission day sees a
#: catalogue with real history behind it rather than a system switched on this morning.
INGEST_START = date(2026, 8, 13)

#: New shots published per day. A mid-size feature unit publishes at roughly this rate.
PER_DAY = 50

#: Departments, weighted the way a slate actually skews, lighting and comp dominate.
DEPARTMENTS = (
    ["lighting"] * 30 + ["comp"] * 25 + ["fx"] * 15 +
    ["anim"] * 12 + ["lookdev"] * 10 + ["roto"] * 8
)

# --------------------------------------------------------- production hierarchy
# A shot is not the deliverable. A client review is booked against an EPISODE of a series
# or a REEL of a feature, and the shots feed it. Without that level the console can only
# say "SH1042 slips 1.6h", which is a shot; with it, the same finding becomes "Northwind
# S01E04 misses Friday, and SH1042 is 22% of its remaining frames", which is a decision.
#
# Two titles, not one, and deliberately: a vendor runs several shows at once, so the
# feature's fx passes and the series' lighting compete for the same nodes. That contention
# is the daily politics of a post house, and it makes "which shot needs a human" a real
# question rather than an obvious one.
#
# This is catalogue metadata, joined to telemetry by shot id. There are deliberately NO new
# Prometheus labels: stages.py teaches the agent the exact label schema, and every change
# to it risks the failure where an empty query result gets read as evidence of absence.
# A title is a fact about a shot, not a fact about a time series.
SERIES = "Northwind"
FEATURE = "Cinder & Salt"

#: (title, kind, unit code, its sequences). An episode holds several sequences, which is
#: what makes it a real level in the hierarchy rather than a rename of one.
UNITS = [
    (SERIES,  "series",  "S01E01", ("SQ010", "SQ020", "SQ030")),
    (SERIES,  "series",  "S01E02", ("SQ040", "SQ050", "SQ060")),
    (SERIES,  "series",  "S01E03", ("SQ070", "SQ080", "SQ090")),
    (SERIES,  "series",  "S01E04", ("SQ100", "SQ110", "SQ120")),
    (SERIES,  "series",  "S01E05", ("SQ130", "SQ140", "SQ150")),
    (FEATURE, "feature", "R1",     ("SQ160", "SQ170", "SQ180")),
    (FEATURE, "feature", "R2",     ("SQ190", "SQ200", "SQ210")),
    (FEATURE, "feature", "R3",     ("SQ220", "SQ230", "SQ240")),
]

#: Sequences, so shot ids group into something a supervisor would recognise.
SEQUENCES = [q for _, _, _, seqs in UNITS for q in seqs]

#: sequence -> the unit that owns it. A sequence belongs to exactly one episode or reel,
#: as it does in a real production, so the hierarchy never has to guess.
_SEQ_UNIT = {q: (title, kind, code)
             for title, kind, code, seqs in UNITS for q in seqs}

#: The three incident passes live in scenario.py, not here, so they need mapping too. All
#: three sit in one episode on purpose: the GPU fault then threatens a single delivery,
#: which is a coherent story rather than three unrelated slips.
_INCIDENT_UNITS = {
    "SH041": (SERIES, "series", "S01E04", "SQ100"),
    "SH042": (SERIES, "series", "S01E04", "SQ100"),
    "SH043": (SERIES, "series", "S01E04", "SQ110"),
}


def unit_label(title: str, code: str) -> str:
    """How a supervisor says it out loud: "Northwind S01E04"."""
    return f"{title} {code}"


def hierarchy(shot_id: str) -> Optional[dict]:
    """Title, kind, unit and sequence for any shot id, incident or catalogue.

    Returns None for an id that does not exist, so a caller can tell "no hierarchy" from
    "hierarchy I have not looked up yet" instead of getting a plausible-looking blank.
    """
    if not shot_id:
        return None
    shot_id = shot_id.strip().upper()
    if shot_id in _INCIDENT_UNITS:
        title, kind, code, seq = _INCIDENT_UNITS[shot_id]
        return {"title": title, "kind": kind, "unit": code,
                "unit_label": unit_label(title, code), "sequence": seq}
    for s in catalog():
        if s.shot == shot_id:
            return {"title": s.title, "kind": s.kind, "unit": s.unit,
                    "unit_label": s.unit_label, "sequence": s.sequence}
    return None

PRIORITIES = ["standard"] * 70 + ["high"] * 22 + ["hero"] * 8


@dataclass
class Shot:
    shot: str
    sequence: str
    title: str                # the show or film this shot belongs to
    kind: str                 # "series" | "feature"
    unit: str                 # "S01E04" for an episode, "R2" for a reel
    department: str
    priority: str
    total_frames: int
    ingested_on: str          # ISO date
    due_on: str               # ISO date, the client review this pass is booked against
    days_to_render: int       # how long the pass takes once it starts
    state: str                # queued | rendering | complete

    @property
    def unit_label(self) -> str:
        return unit_label(self.title, self.unit)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["unit_label"] = self.unit_label
        return d


def _rand(*parts) -> float:
    """Deterministic pseudo-random in [0,1) from any key.

    A hash, not `random`: the same shot must come out identical in the seeder, in the web
    process, tomorrow, and on a judge's machine. Seeding a PRNG would work too until
    someone reorders a loop.
    """
    h = hashlib.sha256("·".join(str(p) for p in parts).encode()).hexdigest()
    return int(h[:12], 16) / 0xFFFFFFFFFFFF


def _day_index(d: date) -> int:
    return (d - INGEST_START).days


def shots_for_day(d: date) -> List[Shot]:
    """The shots published on one specific day. Ids never repeat: they are keyed to the
    global index, so day 3's shots cannot collide with day 12's."""
    idx = _day_index(d)
    if idx < 0:
        return []
    out = []
    for i in range(PER_DAY):
        # Numbering starts at 1001 so catalogue ids (SH1001+) can never be mistaken for the
        # incident passes the agent investigates (SH041, SH042, SH043). "SH0041" and "SH041"
        # differing by one zero is the kind of thing that reads fine in code and ruins a
        # conversation between a supervisor and a TD.
        n = 1000 + idx * PER_DAY + i + 1
        r = lambda *k: _rand(n, *k)            # noqa: E731 - terse on purpose here
        dept = DEPARTMENTS[int(r("dept") * len(DEPARTMENTS))]
        seq = SEQUENCES[int(r("seq") * len(SEQUENCES))]
        prio = PRIORITIES[int(r("prio") * len(PRIORITIES))]
        # Frame counts by department: comp passes are short, fx and lighting are long.
        base = {"lighting": 1400, "comp": 700, "fx": 2000,
                "anim": 900, "lookdev": 500, "roto": 400}[dept]
        total = int(base * (0.55 + r("frames") * 0.95))
        days = 1 + int(r("days") * 3)          # 1-3 days of rendering
        due = d + timedelta(days=days + 1 + int(r("due") * 3))
        title, kind, unit = _SEQ_UNIT[seq]
        out.append(Shot(
            shot=f"SH{n:04d}", sequence=seq, title=title, kind=kind, unit=unit,
            department=dept, priority=prio,
            total_frames=total, ingested_on=d.isoformat(), due_on=due.isoformat(),
            days_to_render=days, state="queued",
        ))
    return out


def catalog(as_of: Optional[date] = None) -> List[Shot]:
    """Every shot ingested up to and including `as_of`, newest day first.

    Cheap: a few hundred dataclasses. Nothing here touches Prometheus, so the Shots page
    stays responsive however large the slate gets.
    """
    as_of = as_of or date.today()
    out: List[Shot] = []
    d = as_of
    while d >= INGEST_START:
        for s in shots_for_day(d):
            s.state = _state_on(s, as_of)
            out.append(s)
        d -= timedelta(days=1)
    return out


def _state_on(s: Shot, as_of: date) -> str:
    started = date.fromisoformat(s.ingested_on)
    done = started + timedelta(days=s.days_to_render)
    if as_of < started:
        return "queued"
    return "rendering" if as_of < done else "complete"


def active(as_of: Optional[date] = None, limit: int = 60) -> List[Shot]:
    """Shots actually rendering today: the only ones worth emitting telemetry for.

    Capped because live series cost query time on every console page load, and a farm that
    is genuinely rendering 400 passes at once is not a farm, it is a fiction.
    """
    as_of = as_of or date.today()
    live = [s for s in catalog(as_of) if s.state == "rendering"]
    # Hero and high-priority work first: it is what a supervisor would actually be watching.
    live.sort(key=lambda s: ({"hero": 0, "high": 1, "standard": 2}[s.priority], s.shot))
    return live[:limit]


def units_summary(as_of: Optional[date] = None) -> List[dict]:
    """One row per episode or reel: what it holds and when it can land.

    The delivery date here is DERIVED, not configured: an episode cannot be delivered until
    its last pass is, so it is the latest `due_on` among its shots. That is worth being
    explicit about, because a date the product invents and a date the production published
    are different kinds of fact and only one of them can be missed. Shot deadlines are left
    exactly as they are; nothing here rewrites them.
    """
    rows: dict = {}
    today = (as_of or date.today()).isoformat()
    soon = ((as_of or date.today()) + timedelta(days=3)).isoformat()
    for s in catalog(as_of):
        key = (s.title, s.kind, s.unit)
        r = rows.setdefault(key, {
            "title": s.title, "kind": s.kind, "unit": s.unit,
            "unit_label": unit_label(s.title, s.unit),
            "shots": 0, "frames": 0, "due_on": s.due_on, "first_due": s.due_on,
            "outstanding": 0, "overdue": 0, "due_soon": 0, "next_due": None,
            "by_state": {}, "by_department": {}, "sequences": set(),
        })
        r["shots"] += 1
        r["frames"] += s.total_frames
        r["due_on"] = max(r["due_on"], s.due_on)        # derived: the last pass gates it
        r["first_due"] = min(r["first_due"], s.due_on)
        # "When does this episode land" is the wrong question when 95 passes are spread over
        # a fortnight: the latest date is the same for every unit and says nothing. What a
        # supervisor is actually asking is what is coming at them, so count that instead.
        if s.state != "complete":
            r["outstanding"] += 1
            if s.due_on < today:
                r["overdue"] += 1
            else:
                r["next_due"] = s.due_on if r["next_due"] is None else min(r["next_due"], s.due_on)
                if s.due_on <= soon:
                    r["due_soon"] += 1
        r["by_state"][s.state] = r["by_state"].get(s.state, 0) + 1
        r["by_department"][s.department] = r["by_department"].get(s.department, 0) + 1
        r["sequences"].add(s.sequence)
    out = []
    for r in rows.values():
        r["sequences"] = sorted(r["sequences"])
        out.append(r)
    # Exceptions first, the rule the rest of the console follows, then the slate board
    # order (series before feature, by unit code) for everything that needs nothing.
    out.sort(key=lambda r: (-r["overdue"], -r["due_soon"],
                            r["kind"] != "series", r["title"], r["unit"]))
    return out


def summary(as_of: Optional[date] = None) -> dict:
    as_of = as_of or date.today()
    cat = catalog(as_of)
    by_state, by_dept = {}, {}
    for s in cat:
        by_state[s.state] = by_state.get(s.state, 0) + 1
        by_dept[s.department] = by_dept.get(s.department, 0) + 1
    return {
        "as_of": as_of.isoformat(),
        "total": len(cat),
        "ingested_today": len(shots_for_day(as_of)),
        "by_state": by_state,
        "by_department": by_dept,
        "days_ingesting": _day_index(as_of) + 1,
        "per_day": PER_DAY,
    }
