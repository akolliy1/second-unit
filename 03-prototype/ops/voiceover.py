#!/usr/bin/env python3
"""Synthesise the demo narration, then publish the timeline everything else obeys.

Audio is generated FIRST and measured, and the recorder takes its dwell times from those
measurements. Doing it in that order is what removes the usual problem with a scripted
demo video: if the picture is cut to a guess and the voice comes in longer, every beat
after it drifts, and the fix is a manual editing pass. Here the voice is the clock.

Three artefacts, written to ops/out/:
  beat-NN.mp3      one clip per beat
  voiceover.mp3    the whole narration, with the same padding the recorder will use
  manifest.json    per-beat start and duration, the recorder's script
  demo.srt         subtitles, split by sentence, timed from the real audio

The hackathon's 3:00 limit is enforced here rather than left to judgement: this refuses to
write a manifest that would produce a non-compliant video, because a script that reads
"about right" and a file that is 3:04 look identical until the upload is rejected.

Usage:
  spike/.venv/bin/python ops/voiceover.py            # synthesise and time everything
  spike/.venv/bin/python ops/voiceover.py --dry-run  # estimate only, no API calls
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "out"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "agent"))

from beats import BEATS, MAX_SECONDS          # noqa: E402

#: Breathing room after each beat. Also the recorder's cut point, so it is part of the
#: timeline rather than something added later by hand.
PAD_SECONDS = 0.35

#: Chirp3-HD's default pace is close to natural but a demo benefits from a fraction slower:
#: a judge is reading the screen at the same time.
SPEAKING_RATE = float(os.environ.get("DEMO_SPEAKING_RATE", "0.96"))


def _ffprobe_seconds(path: pathlib.Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _wrap(text: str, width: int = 42) -> str:
    """Two short lines read better than one long one at the bottom of a video."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur); cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "\n".join(lines[:3])


def _cues(caption: str, start: float, duration: float) -> list[tuple[float, float, str]]:
    """One cue per sentence, sharing the beat's real duration by word count.

    A single 25-second subtitle is technically compliant and genuinely unpleasant to read,
    so beats are split where the narration pauses anyway.
    """
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", caption.strip()) if p.strip()]
    if not parts:
        return []
    total_words = sum(len(p.split()) for p in parts)
    cues, t = [], start
    for p in parts:
        share = duration * (len(p.split()) / total_words)
        cues.append((t, t + share, _wrap(p)))
        t += share
    return cues


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="estimate durations from word count, make no API calls")
    ap.add_argument("--voice", default=None, help="override the TTS voice name")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)

    if not args.dry_run:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        from second_unit import briefing

    print(f"{'beat':>4}  {'name':24} {'source':9} {'words':>5} {'seconds':>8}")
    rows, t = [], 0.0
    for b in BEATS:
        words = len(b["say"].split())
        if args.dry_run:
            secs = words / 2.6
        else:
            mp3 = OUT / f"beat-{b['n']:02d}.mp3"
            audio = briefing.synthesize(b["say"], voice=args.voice,
                                        speaking_rate=SPEAKING_RATE)
            mp3.write_bytes(audio)
            secs = _ffprobe_seconds(mp3)
        rows.append(dict(n=b["n"], name=b["name"], source=b["source"],
                         start=round(t, 3), duration=round(secs, 3),
                         caption=b["caption"], say=b["say"], actions=b["actions"]))
        print(f"{b['n']:>4}  {b['name']:24} {b['source']:9} {words:>5} {secs:>8.2f}")
        t += secs + PAD_SECONDS

    total = t - PAD_SECONDS          # no trailing pad in the finished cut
    print(f"\n  total: {total:.1f}s   ceiling: {MAX_SECONDS:.0f}s")

    if total > MAX_SECONDS:
        over = total - MAX_SECONDS
        print(f"\n  REFUSING to write the manifest: {over:.1f}s over the hard 3:00 limit.")
        print(f"  Trim roughly {int(over * 2.6)} words from beats.py and run again.")
        print("  Longest beats:")
        for r in sorted(rows, key=lambda r: -r["duration"])[:3]:
            print(f"    {r['n']:2} {r['name']:24} {r['duration']:.1f}s")
        return 1

    if not args.dry_run:
        # One narration track, padded exactly as the manifest says, so audio and picture
        # cannot disagree about where a beat starts.
        silence = OUT / "_pad.mp3"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                        "-i", "anullsrc=r=24000:cl=mono", "-t", str(PAD_SECONDS),
                        str(silence)], check=True)

        # One narration track, padded exactly as the manifest says, so audio and picture
        # cannot disagree about where a beat starts. Relative names plus cwd=OUT, because
        # the concat demuxer resolves paths against its own file and absolute paths with
        # apostrophes in them are a quoting problem waiting to happen.
        entries = []
        for i, r in enumerate(rows):
            entries.append(f"file 'beat-{r['n']:02d}.mp3'")
            if i != len(rows) - 1:
                entries.append(f"file '{silence.name}'")
        listing = OUT / "_concat.txt"
        listing.write_text("\n".join(entries) + "\n")

        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                        "-i", listing.name, "-c:a", "libmp3lame", "-q:a", "2",
                        "voiceover.mp3"], check=True, cwd=OUT)
        got = _ffprobe_seconds(OUT / "voiceover.mp3")
        print(f"  voiceover.mp3: {got:.1f}s")
        if abs(got - total) > 1.0:
            print(f"  WARNING: track is {got:.1f}s but the manifest says {total:.1f}s.")

    (OUT / "manifest.json").write_text(json.dumps(
        dict(total_seconds=round(total, 3), pad_seconds=PAD_SECONDS,
             speaking_rate=SPEAKING_RATE, dry_run=args.dry_run, beats=rows), indent=2))

    srt, idx = [], 1
    for r in rows:
        for a, b_, text in _cues(r["caption"], r["start"], r["duration"]):
            srt.append(f"{idx}\n{_srt_time(a)} --> {_srt_time(b_)}\n{text}\n")
            idx += 1
    (OUT / "demo.srt").write_text("\n".join(srt))
    print(f"  manifest.json and demo.srt written ({idx - 1} subtitle cues)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
