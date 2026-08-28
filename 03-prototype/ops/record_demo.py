#!/usr/bin/env python3
"""Record the browser half of the demo, driven by the narration's real timings.

Frames come from Chrome itself, over CDP, not from the screen. Screen capture records a
RECTANGLE, so anything the operator puts on top of the browser lands in the take, and the
first attempt was ruined exactly that way. It also demands the machine be left untouched
for the whole timeline, which is not a reasonable thing to ask of the only computer in the
room. Chrome's own screencast sees the page and nothing else, so the recording is immune to
other windows and the machine stays usable while it runs.

Frames arrive only when something changes, so a constant frame rate is reconstructed at
assembly time by holding each frame until the next one arrives: smooth where there is
motion, free where the page is still.

The browser is stepped through the beat sheet on the schedule in ops/out/manifest.json.

Each beat's actions run during the PAD before that beat's audio starts, so the screen is
settled when the narration arrives instead of moving under it. That is what the pad is for.

Beats marked terminal or grafana are held, not skipped: the capture keeps rolling so the
timeline stays continuous, and assemble.py replaces those spans with footage captured for
them. A held span is left visibly on the last browser view rather than blacked out, so a
missing splice is obvious in review instead of looking intentional.

Usage:
  spike/.venv/bin/python ops/record_demo.py                     # record against the live URL
  spike/.venv/bin/python ops/record_demo.py --url http://localhost:8080
  spike/.venv/bin/python ops/record_demo.py --dry-run           # drive the browser, no capture
"""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "out"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9300
PROFILE = "/tmp/su-record-profile"          # persisted, so a Grafana login survives takes

#: 16:9 at a size that keeps the console's desktop layout, above its 1100px breakpoints.
WIN_W, WIN_H = 1600, 900

LIVE_URL = "https://second-unit-dzqjw5tifq-uc.a.run.app"


#: Selectors that matched nothing during a run. Collected rather than raised: aborting
#: mid-take would lose the whole recording over one beat, and the report at the end is
#: enough to fix the beat and go again.
MISSES: list[str] = []


# --------------------------------------------------------------------------- CDP
class Chrome:
    def __init__(self, url: str, headless: bool = True):
        # Headless by default now that frames come from the page rather than the screen:
        # a window that is never composited cannot be covered, and the operator keeps their
        # desktop. The cost is the loss of the URL bar, which the overlay puts back.
        # A take that crashes leaves Chrome holding the debug port, and the next launch then
        # attaches to a dying endpoint and fails with "socket is already closed". Clearing it
        # first turns a confusing failure into a non-event.
        subprocess.run(["pkill", "-f", f"remote-debugging-port={PORT}"],
                       capture_output=True)
        time.sleep(1.5)
        flags = [CHROME, f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
                 "--no-first-run", "--no-default-browser-check", "--hide-crash-restore-bubble",
                 "--disable-session-crashed-bubble", "--disable-popup-blocking",
                 "--remote-allow-origins=*", f"--window-position=0,0",
                 f"--window-size={WIN_W},{WIN_H}"]
        if headless:
            flags += ["--headless=new", "--disable-gpu"]
        self.proc = subprocess.Popen(flags + [url],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.frames: list = []          # (monotonic seconds, jpeg bytes)
        self._replies: dict = {}
        self._lock = threading.Lock()
        self._stop = False
        self.ws = None
        for _ in range(40):
            time.sleep(0.5)
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
                page = [t for t in tabs if t["type"] == "page"]
                if page:
                    import websocket
                    self.ws = websocket.create_connection(page[0]["webSocketDebuggerUrl"],
                                                          timeout=60)
                    break
            except Exception:                      # noqa: BLE001, Chrome is still starting
                continue
        if not self.ws:
            raise RuntimeError("Chrome did not expose a debuggable page")
        self._id = 0
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self.cmd("Page.enable")
        self.cmd("Runtime.enable")
        self.cmd("Page.addScriptToEvaluateOnNewDocument", source=OVERLAY_JS)

    def _read_loop(self):
        """Screencast frames arrive unsolicited, so a cmd() that read the socket inline
        would throw them away while waiting for its own reply. One reader, replies filed by
        id, frames appended as they land."""
        while not self._stop:
            try:
                msg = json.loads(self.ws.recv())
            except Exception:                    # noqa: BLE001, socket closed at shutdown
                return
            if "id" in msg:
                with self._lock:
                    self._replies[msg["id"]] = msg
            elif msg.get("method") == "Page.screencastFrame":
                p = msg["params"]
                self.frames.append((time.monotonic(), base64.b64decode(p["data"])))
                try:
                    self._id += 1
                    self.ws.send(json.dumps({"id": self._id,
                                             "method": "Page.screencastFrameAck",
                                             "params": {"sessionId": p["sessionId"]}}))
                except Exception:                # noqa: BLE001
                    return

    def cmd(self, method, timeout=60.0, **params):
        self._id += 1
        mine = self._id
        self.ws.send(json.dumps({"id": mine, "method": method, "params": params}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                msg = self._replies.pop(mine, None)
            if msg is not None:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            time.sleep(0.005)
        raise TimeoutError(f"{method} did not reply in {timeout}s")

    def ev(self, expr):
        r = self.cmd("Runtime.evaluate", expression=expr, returnByValue=True,
                     awaitPromise=True)
        return r.get("result", {}).get("value")

    def bounds(self):
        wid = self.cmd("Browser.getWindowForTarget")["windowId"]
        return self.cmd("Browser.getWindowBounds", windowId=wid)["bounds"]

    def close(self):
        self._stop = True
        try:
            self.ws.close()
        finally:
            self.proc.terminate()


# ----------------------------------------------------------------------- actions
#: A pointer and a highlight, injected into every document. Without them the video cuts
#: between views with no indication of what was touched: the narration says "the write-back
#: tab" and the picture simply changes, so a viewer has to guess which of nine things on
#: screen was meant. Drawn in the page rather than composited afterwards, so the highlight
#: tracks the real element even when the layout reflows.
OVERLAY_JS = r"""
(function () {
  if (window.__spot) return;
  var mk = function (css) {
    var d = document.createElement('div');
    d.style.cssText = css;
    (document.body || document.documentElement).appendChild(d);
    return d;
  };
  var ring = mk('position:fixed;z-index:2147483646;pointer-events:none;border-radius:10px;' +
    'border:2px solid #f5a623;box-shadow:0 0 0 4px rgba(245,166,35,.22);opacity:0;' +
    'transition:left .45s cubic-bezier(.4,0,.2,1),top .45s cubic-bezier(.4,0,.2,1),' +
    'width .45s,height .45s,opacity .3s;');
  var cur = mk('position:fixed;z-index:2147483647;pointer-events:none;left:-80px;top:-80px;' +
    'width:26px;height:26px;filter:drop-shadow(0 2px 4px rgba(0,0,0,.55));' +
    'transition:left .55s cubic-bezier(.4,0,.2,1),top .55s cubic-bezier(.4,0,.2,1);');
  cur.innerHTML = '<svg viewBox="0 0 24 24" width="26" height="26">' +
    '<path d="M4 2 L4 19 L8.6 14.6 L11.6 21.4 L14.6 20.1 L11.7 13.6 L18 13.6 Z" ' +
    'fill="#ffffff" stroke="#0b0d10" stroke-width="1.5" stroke-linejoin="round"/></svg>';
  window.__spot = function (sel, pulse) {
    var e = document.querySelector(sel);
    if (!e) return false;
    var r = e.getBoundingClientRect();
    if (!r.width && !r.height) return false;
    cur.style.left = (r.left + Math.min(r.width * 0.5, 70)) + 'px';
    cur.style.top = (r.top + r.height / 2) + 'px';
    ring.style.left = (r.left - 5) + 'px';
    ring.style.top = (r.top - 5) + 'px';
    ring.style.width = (r.width + 10) + 'px';
    ring.style.height = (r.height + 10) + 'px';
    ring.style.opacity = '1';
    clearTimeout(window.__ringTimer);
    /* Held long enough to be read, then faded: a ring that never leaves stops meaning
       "this one" and starts being part of the furniture. */
    window.__ringTimer = setTimeout(function () { ring.style.opacity = '0'; },
                                    pulse === false ? 1200 : 3200);
    return true;
  };
})();
"""


def spot(ch: "Chrome", sel: str, settle: float = 0.85) -> None:
    """Move the pointer to an element and ring it, before anything happens to it.

    The pause is the point: a click that lands the same frame the pointer arrives reads as a
    jump cut. This gives the eye time to follow.
    """
    ch.ev(f"window.__spot && window.__spot({sel!r})")
    time.sleep(settle)


def act(ch: Chrome, base: str, action) -> None:
    """One beat-sheet action. Unknown verbs raise: a silently ignored action would mean
    recording a beat that shows the wrong thing, and finding out on playback."""
    verb = action[0]
    if verb == "goto":
        ch.cmd("Page.navigate", url=base + action[1])
        time.sleep(2.2)
    elif verb == "wait":
        time.sleep(float(action[1]))
    elif verb == "click":
        spot(ch, action[1])
        found = ch.ev(f"""(function(){{const e=document.querySelector({action[1]!r});
                 if(e) {{e.click(); return true;}} return false;}})()""")
        if not found:
            MISSES.append(action[1])
        time.sleep(1.2)
    elif verb == "tab":
        sel = f'[role="tab"][data-tab="{action[1]}"]'
        spot(ch, sel)
        found = ch.ev(f"""(function(){{const e=document.querySelector({sel!r});
                 if(e) {{e.click(); return true;}} return false;}})()""")
        if not found:
            MISSES.append(sel)
        time.sleep(0.8)
    elif verb == "persona":
        ch.ev(f"try{{localStorage.setItem('second-unit:persona','{action[1]}')}}catch(e){{}}")
        cur = ch.ev("""(function(){const u=new URL(location.href);
                     u.searchParams.delete('persona');
                     return u.pathname + (u.search || '');}())""")
        sep = "&" if "?" in str(cur) else "?"
        ch.cmd("Page.navigate", url=f"{base}{cur}{sep}persona={action[1]}")
        time.sleep(2.2)
        got = ch.ev("document.documentElement.dataset.persona || "
                    "document.body.dataset.persona || ''")
        if got != action[1]:
            MISSES.append(f"persona={action[1]} (page reports {got!r})")
    elif verb == "scrollto":
        state = ch.ev(f"""(function(){{const e=document.querySelector({action[1]!r});
                 if(!e) {{window.scrollTo({{top:0,behavior:'smooth'}}); return 'absent';}}
                 /* An element inside a collapsed panel is in the DOM and invisible, which
                    is not the same as being on screen. */
                 if(!e.offsetParent && getComputedStyle(e).position !== 'fixed') return 'hidden';
                 e.scrollIntoView({{behavior:'smooth', block:'center'}});
                 return 'ok';}})()""")
        if state != "ok":
            MISSES.append(f"{action[1]} ({state})")
        time.sleep(1.0)
        if state == "ok":
            # Rung after the scroll, not before: the element is somewhere else until it lands.
            ch.ev(f"window.__spot && window.__spot({action[1]!r})")
            time.sleep(0.4)

    elif verb == "expand":
        spot(ch, action[1])
        # Idempotent on purpose. A bare click TOGGLES, so re-running a beat, or a panel that
        # was already open, would close the thing the beat exists to show.
        state = ch.ev(f"""(function(){{
                 const h=document.querySelector({action[1]!r});
                 if(!h) return 'absent';
                 if(h.getAttribute('aria-expanded') === 'true') return 'already';
                 h.click(); return 'opened';}})()""")
        if state == "absent":
            MISSES.append(f"{action[1]} (absent)")
        time.sleep(1.0)

    elif verb == "until":
        # Wait for the page to be TRUE, not merely loaded. The landing strip counts up from
        # placeholders as three sequenced fetches land, and /api/agent/metrics runs six
        # blocking Prometheus queries, so filming on a fixed delay caught "61 / 4 / 15 / ..."
        # under a narration promising live numbers.
        expr, limit = action[1], float(action[2]) if len(action) > 2 else 20.0
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if ch.ev(expr):
                break
            time.sleep(0.4)
        else:
            MISSES.append(f"until({expr[:48]}...) timed out after {limit:.0f}s")
    elif verb == "scroll":
        ch.ev(f"window.scrollTo({{top:{int(action[1])}, behavior:'smooth'}})")
        time.sleep(0.8)
    elif verb == "hold":
        pass
    else:
        raise ValueError(f"unknown beat action: {action!r}")


def write_video(ch: "Chrome", total: float) -> int:
    """Turn the captured frames into browser.mp4 at a constant 30fps.

    Chrome sends a frame when the page changes, so the stream is uneven by design: dozens a
    second during an animation, none at all while a verdict sits on screen. Each frame is
    given a duration equal to the gap until the next one, which reconstructs real time
    without duplicating a single file. The concat demuxer needs the last entry repeated for
    its duration to be honoured, which is a quirk, not a mistake.
    """
    frames = ch.frames
    if not frames:
        print("\n  no frames arrived from the page. Screencast did not start.")
        return 1
    fdir = OUT / "frames"
    if fdir.exists():
        shutil.rmtree(fdir)
    fdir.mkdir(parents=True)

    base = ch.t0
    stamped = [(max(0.0, ts - base), data) for ts, data in frames]
    entries = []
    for i, (ts, data) in enumerate(stamped):
        name = f"f-{i:06d}.jpg"
        (fdir / name).write_bytes(data)
        end = stamped[i + 1][0] if i + 1 < len(stamped) else total
        dur = max(1 / 60, end - ts)
        entries.append(f"file '{name}'\nduration {dur:.4f}")
    # The concat demuxer ignores the final entry's duration unless the file is repeated.
    entries.append(f"file 'f-{len(stamped) - 1:06d}.jpg'")
    (fdir / "list.txt").write_text("\n".join(entries) + "\n")

    span = stamped[-1][0] - stamped[0][0]
    print(f"\n  {len(frames)} frames over {span:.1f}s "
          f"({len(frames)/max(span,0.01):.1f} fps average, uneven by design)")

    out = OUT / "browser.mp4"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", "list.txt", "-fps_mode", "cfr", "-r", "30",
         "-vf", "scale=1920:1080:flags=lanczos",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", str(out)],
        cwd=fdir, capture_output=True, text=True)
    if proc.returncode != 0:
        # Printed, because "returned non-zero exit status 8" told me nothing and the real
        # message was one line away the whole time.
        print(f"  ffmpeg failed ({proc.returncode}):\n    "
              + "\n    ".join((proc.stderr or "").strip().split("\n")[:6]))
        return 1
    got = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(out)],
        capture_output=True, text=True, check=True).stdout.strip())
    print(f"  browser.mp4: {got:.1f}s, {out.stat().st_size / 1e6:.1f} MB "
          f"(timeline wants {total:.1f}s)")
    if got < total - 1.5:
        print(f"  SHORT by {total - got:.1f}s: the last beats would be missing.")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=LIVE_URL)
    ap.add_argument("--dry-run", action="store_true", help="drive the browser, capture nothing")
    ap.add_argument("--headful", action="store_true",
                    help="show the browser window; frames still come from the page, so this "
                         "only affects whether you can watch it happen")
    args = ap.parse_args()

    manifest = json.loads((OUT / "manifest.json").read_text())
    if manifest.get("dry_run"):
        print("  manifest was written by a --dry-run voiceover: no real audio, no real timings.")
        print("  Run ops/voiceover.py for real first.")
        return 1
    # Timings come from the manifest, because only measured audio can supply them. ACTIONS
    # come from beats.py, because they are not timings and freezing them into the manifest
    # meant a fixed selector did not reach the recorder until the audio was re-synthesised.
    # That cost one full take: the manifest still held two selectors I had already fixed.
    sys.path.insert(0, str(HERE))
    from beats import BEATS
    live_actions = {b["n"]: b["actions"] for b in BEATS}

    beats = manifest["beats"]
    for b in beats:
        if b["n"] in live_actions:
            b["actions"] = live_actions[b["n"]]
    missing_from_beats = [b["n"] for b in beats if b["n"] not in live_actions]
    if missing_from_beats:
        print(f"  manifest has beats {missing_from_beats} that beats.py no longer defines. "
              f"Re-run ops/voiceover.py so the two agree.")
        return 1

    pad = manifest["pad_seconds"]
    total = manifest["total_seconds"]

    if not args.dry_run and not shutil.which("ffmpeg"):
        print("  ffmpeg not on PATH")
        return 1

    print(f"  url:    {args.url}")
    print(f"  beats:  {len(beats)}   timeline: {total:.1f}s")

    ch = Chrome(args.url, headless=not args.headful)
    try:
        # 1440x810 CSS at 1.333x, so frames land at exactly 1920x1080. Capturing at 1920 CSS
        # would be the same pixels with everything a third smaller, which is the difference
        # between a judge reading a PromQL expression on a laptop and squinting at it.
        ch.cmd("Emulation.setDeviceMetricsOverride", width=1440, height=810,
               deviceScaleFactor=1.3333, mobile=False)
        print(f"  capture: 1440x810 CSS at 1.333x -> 1920x1080 frames, from the page itself")

        # Beat 0's actions run before the capture starts: there is no pad in front of it,
        # and its narration begins on the first frame.
        ch.ev(OVERLAY_JS)          # the first document predates the injection
        for a in beats[0]["actions"]:
            act(ch, args.url, a)
        time.sleep(1.0)

        if not args.dry_run:
            ch.cmd("Page.startScreencast", format="jpeg", quality=88,
                   maxWidth=1920, maxHeight=1080, everyNthFrame=1)
        t0 = time.time()
        ch.t0 = time.monotonic()

        print(f"\n  {'beat':>4}  {'at':>7}  {'name':24} action window")
        for i, beat in enumerate(beats):
            # Act during the pad ahead of this beat's audio, so the screen is settled by the
            # time the narration for it starts.
            # Start this beat's setup its full lead ahead of the narration, not merely one
            # pad. Anything slower than the pad was running INTO the beat it was preparing.
            prepare_at = max(0.0, beat["start"] - pad - float(beat.get("lead", 0.0)))
            while time.time() - t0 < prepare_at:
                time.sleep(0.02)
            held = beat["source"] != "browser"
            print(f"  {beat['n']:>4}  {beat['start']:>7.1f}  {beat['name']:24}"
                  f"{' HELD, spliced later' if held else ''}")
            if i > 0 and not held:
                for a in beat["actions"]:
                    act(ch, args.url, a)
            # Nothing else to do inside the beat: the browser holds this view while the
            # narration plays over it.
        while time.time() - t0 < total + 0.5:
            time.sleep(0.05)

        if not args.dry_run:
            ch.cmd("Page.stopScreencast")
            time.sleep(0.4)
            rc = write_video(ch, total)
            if rc:
                return rc
    finally:
        ch.close()

    if MISSES:
        print(f"\n  {len(MISSES)} selector(s) matched nothing, so those beats recorded the "
              f"wrong view:")
        for m in dict.fromkeys(MISSES):
            print(f"    {m}")
        print("  Fix them in ops/beats.py and record again.")
        return 2
    print("  every selector resolved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
