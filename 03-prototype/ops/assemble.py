#!/usr/bin/env python3
"""Cut the captured footage to the narration and write demo.mp4.

The manifest is the single source of truth for where every beat starts and how long it
lasts, so this slices rather than negotiates: each beat becomes exactly its own audio's
length, taken from browser.mp4 unless a replacement clip exists for it.

Replacements live in ops/out/ as seg-NN.mp4, one per beat that browser capture cannot tell
the truth about: beat 8 is a terminal, beat 9 is Grafana's own UI. If one is absent the
browser footage is used and the fact is printed, because a video that quietly shows the
wrong thing for twenty seconds is the failure mode this whole project is about.

Subtitles are written as a sidecar rather than burned in: YouTube takes the .srt directly,
and burned captions cannot be turned off by a judge who finds them in the way.

Usage:
  spike/.venv/bin/python ops/assemble.py
  spike/.venv/bin/python ops/assemble.py --burn-subs     # also write demo-subbed.mp4
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "out"


def ffprobe_seconds(path: pathlib.Path) -> float:
    return float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True).stdout.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--burn-subs", action="store_true")
    args = ap.parse_args()

    manifest = json.loads((OUT / "manifest.json").read_text())
    beats, total = manifest["beats"], manifest["total_seconds"]
    browser = OUT / "browser.mp4"
    voice = OUT / "voiceover.mp3"
    for f in (browser, voice):
        if not f.exists():
            print(f"  missing {f.name}. Run ops/record_demo.py and ops/voiceover.py first.")
            return 1

    # Footage older than the manifest cannot contain what the manifest describes. The
    # recorder crashed once and this assembled hour-old frames against fresh timings without
    # a word, which is the exact shape of bug this project keeps finding: a confident result
    # about the wrong input.
    # BOTH inputs count. The recorder takes timings from the manifest and actions from
    # beats.py, so comparing against the manifest alone missed exactly the case that bit:
    # beat 10 was repointed in beats.py, the manifest was untouched, and hour-old footage
    # looked current. The guard has to cover every input the footage depends on.
    man_age = max((OUT / "manifest.json").stat().st_mtime,
                  (HERE / "beats.py").stat().st_mtime)
    if browser.stat().st_mtime < man_age - 5:
        import datetime as _dt
        fmt = lambda t: _dt.datetime.fromtimestamp(t).strftime("%H:%M:%S")
        print(f"  REFUSING: browser.mp4 ({fmt(browser.stat().st_mtime)}) is older than "
              f"manifest.json ({fmt(man_age)}).")
        print("  The beat sheet or narration changed after that footage was captured, so it "
              "cannot contain those beats. Run ops/record_demo.py again.")
        return 1

    have = ffprobe_seconds(browser)
    print(f"  browser.mp4 {have:.1f}s   timeline {total:.1f}s   voiceover "
          f"{ffprobe_seconds(voice):.1f}s")
    if have < total - 1.0:
        print(f"  capture is {total - have:.1f}s short of the timeline: the last beat(s) "
              f"would be missing. Record again.")
        return 1

    # Cutting every beat separately cost 3.6s of drift across eleven cuts: -ss before -i
    # snaps to a keyframe, and the error accumulates until the picture runs ahead of the
    # voice. browser.mp4 is ALREADY one continuous timeline aligned to the same t0 as the
    # narration, so consecutive browser beats need no cut at all. Only a spliced beat forces
    # a boundary, and the browser spans around it are taken in one piece each with an
    # accurate seek (-ss after -i).
    spans, substituted, missing = [], [], []
    run_start = None
    for b in beats:
        seg = OUT / f"seg-{b['n']:02d}.mp4"
        if seg.exists():
            if run_start is not None:
                spans.append(("browser", run_start, b["start"] - run_start))
                run_start = None
            spans.append(("clip", seg, b["duration"]))
            substituted.append(b["n"])
        else:
            if b["source"] != "browser":
                missing.append(b["n"])
            if run_start is None:
                run_start = b["start"]
    if run_start is not None:
        spans.append(("browser", run_start, total - run_start))

    print(f"  {len(spans)} span(s): "
          + ", ".join(f"{k}{'' if k == 'clip' else f' {a:.1f}s+{d:.1f}s'}"
                      for k, a, d in spans))

    parts = []
    for i, (kind, a, dur) in enumerate(spans):
        cut = OUT / f"cut-{i:02d}.mp4"
        if kind == "browser":
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", str(browser),
                 "-ss", f"{a:.3f}", "-t", f"{dur:.3f}",          # after -i: frame accurate
                 "-an", "-vf", "fps=30", "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", "20", "-pix_fmt", "yuv420p", str(cut)], check=True)
        else:
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", str(a), "-t", f"{dur:.3f}",
                 "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,"
                        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30",
                 "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                 "-pix_fmt", "yuv420p", str(cut)], check=True)
        got = ffprobe_seconds(cut)
        if abs(got - dur) > 0.25:
            print(f"    span {i} wanted {dur:.2f}s, got {got:.2f}s")
        parts.append(cut)

    listing = OUT / "_video_concat.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts))
    silent = OUT / "_silent.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", listing.name, "-c", "copy", silent.name], check=True, cwd=OUT)

    demo = OUT / "demo.mp4"
    vid_len = ffprobe_seconds(silent)
    aud_len = ffprobe_seconds(voice)
    if abs(vid_len - aud_len) > 0.35:
        # -shortest would quietly truncate to whichever stream is shorter, which is how the
        # first attempt lost 3.6s without complaining. A gap this size means the picture and
        # the voice disagree about where beats are, so it gets said out loud.
        print(f"\n  picture {vid_len:.2f}s vs voice {aud_len:.2f}s: "
              f"{abs(vid_len - aud_len):.2f}s apart, so the two will drift.")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(silent), "-i", str(voice),
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                    str(demo)], check=True)

    got = ffprobe_seconds(demo)
    print(f"\n  demo.mp4  {got:.1f}s  {demo.stat().st_size / 1e6:.1f} MB")
    if substituted:
        print(f"  spliced clips for beats: {substituted}")
    if missing:
        print(f"\n  NOTE: beats {missing} are not browser scenes but have no seg-NN.mp4, so "
              f"they currently show whatever the browser was holding.")
        for n in missing:
            b = next(x for x in beats if x["n"] == n)
            print(f"    beat {n} ({b['source']}): {b['name']}, {b['duration']:.1f}s")
    if got > 180.5:
        print(f"\n  OVER the 3:00 limit by {got - 180:.1f}s. This will be rejected.")
        return 1
    print(f"  within the 3:00 limit with {180 - got:.0f}s to spare")
    print(f"  subtitles: {(OUT / 'demo.srt')}")

    if args.burn_subs:
        subbed = OUT / "demo-subbed.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", "demo.mp4",
                        "-vf", "subtitles=demo.srt:force_style='FontSize=18,"
                               "PrimaryColour=&Hffffff&,OutlineColour=&H80000000&,BorderStyle=3'",
                        "-c:a", "copy", "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "20", subbed.name], check=True, cwd=OUT)
        print(f"  demo-subbed.mp4  {ffprobe_seconds(subbed):.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
