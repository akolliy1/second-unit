#!/usr/bin/env python3
"""Do the subtitles say what the narration says?

They are written from two different strings on purpose: the spoken text spells figures and
acronyms out so the voice reads them correctly, the caption uses the notation that appears
on screen. That freedom is exactly how they drift, and trimming a line of narration without
trimming its caption leaves the subtitle asserting something nobody said.

The comparison therefore has to speak both dialects, or it cries wolf on every beat with a
number in it and stops being read.
"""
from __future__ import annotations

import difflib
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from beats import BEATS  # noqa: E402

WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "sixteen": "16", "eighteen": "18",
    "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70",
}
PHRASES = [
    (r"\ba p i\b", "api"), (r"\bm c p\b", "mcp"), (r"\ba d k\b", "adk"),
    (r"\bprom q l\b", "promql"),
    (r"\bshot forty two\b", "sh042"), (r"\bsh042\b", "sh042"),
    (r"\brender zero seven\b", "render07"), (r"\brender-07\b", "render07"),
    (r"\btwo hours and eighteen minutes\b", "2h18m"), (r"\b2h 18m\b", "2h18m"),
    (r"\btwo p\.? ?m\.?\b", "1400"), (r"\b14:00\b", "1400"),
    (r"\bforty six percent\b", "46pc"), (r"\b46%\b", "46pc"),
    (r"\bnine point four frames a minute\b", "94frmin"), (r"\b9\.4 fr/min\b", "94frmin"),
    (r"\bone node of six\b", "1of6"), (r"\b1 of 6 nodes\b", "1of6"),
    (r"\bsixteen minutes\b", "16min"), (r"\b16 min\b", "16min"),
    (r"\bforty eight second\b", "48s"), (r"\b~?48-second\b", "48s"),
    (r"\bseventy six\b", "76"),
    (r"\bnorthwind,? episode four\b", "s01e04"), (r"\bnorthwind s01e04\b", "s01e04"),
    (r"\bthe shot\b", "sh042"),
]


def norm(t: str) -> list[str]:
    t = t.lower()
    for pat, rep in PHRASES:
        t = re.sub(pat, rep, t)
    out = []
    for w in re.sub(r"[^a-z0-9 ]", " ", t).split():
        out.append(WORDS.get(w, w))
    return out


def main() -> int:
    print(f"  {'beat':>4} {'name':22} {'match':>6}  status")
    worst = []
    for b in BEATS:
        r = difflib.SequenceMatcher(None, norm(b["say"]), norm(b["caption"])).ratio()
        ok = r >= 0.85
        print(f"  {b['n']:>4} {b['name']:22} {r:>6.2f}  {'ok' if ok else 'DIVERGED'}")
        if not ok:
            worst.append((b["n"], b["name"], r))
    if worst:
        print("\n  captions that assert something the narration does not:")
        for n, name, r in worst:
            print(f"    beat {n} ({name}), {r:.2f}")
            print(f"      spoken : {next(x for x in BEATS if x['n'] == n)['say'][:110]}")
            print(f"      caption: {next(x for x in BEATS if x['n'] == n)['caption'][:110]}")
        return 1
    print("\n  every caption tracks its narration")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
