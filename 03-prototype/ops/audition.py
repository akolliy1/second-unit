#!/usr/bin/env python3
"""Render one beat across several voices so a human can pick by ear.

There is no way to measure "sounds like a machine". Duration and file size prove nothing
about it, so this produces comparable samples and hands the decision to the person who can
actually hear them. Beat 3 is the sample on purpose: it is the beat the video turns on, and
it contains a shot id and three figures, so it also exposes pronunciation problems.

Usage:
  spike/.venv/bin/python ops/audition.py
  open ops/out/audition          # then listen and pick
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "out" / "audition"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "agent"))

#: Studio voices first: Google builds them for long-form narration and they are the least
#: synthetic of the families available. News voices are next, being read-aloud voices by
#: design. Chirp3-HD is kept in the list only as the incumbent, for comparison.
CANDIDATES = [
    ("en-GB-Studio-B",             "GB male, narration"),
    ("en-GB-Studio-C",             "GB female, narration"),
    ("en-US-Studio-Q",             "US male, narration"),
    ("en-US-Studio-O",             "US female, narration"),
    ("en-GB-News-L",               "GB male, newsreader"),
    ("en-GB-News-H",               "GB female, newsreader"),
    ("en-GB-Chirp3-HD-Achernar",   "the current one, for comparison"),
]

RATES = {"en-GB-Studio-B": 0.95, "en-GB-Studio-C": 0.95,
         "en-US-Studio-Q": 0.95, "en-US-Studio-O": 0.95}


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from second_unit import briefing
    from beats import BEATS

    sample = next(b for b in BEATS if b["n"] == 3)["say"]
    OUT.mkdir(parents=True, exist_ok=True)
    print(f'  sample (beat 3):\n    "{sample}"\n')
    print(f"  {'file':40} {'seconds':>8}  note")
    for voice, note in CANDIDATES:
        path = OUT / f"{voice}.mp3"
        try:
            audio = briefing.synthesize(sample, voice=voice,
                                        speaking_rate=RATES.get(voice, 1.0))
        except Exception as exc:                       # noqa: BLE001
            # A voice the project or region does not permit is a fact worth printing, not a
            # reason to abandon the other candidates.
            print(f"  {voice:40} {'--':>8}  unavailable: {type(exc).__name__}")
            continue
        path.write_bytes(audio)
        secs = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True).stdout.strip())
        print(f"  {path.name:40} {secs:>8.2f}  {note}")
    print(f"\n  {OUT}")
    print("  Listen, then tell me the filename you want and I will regenerate all 11 beats.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
