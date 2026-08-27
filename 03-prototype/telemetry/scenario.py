"""
The render farm, and the one failure that ruins Friday.

This is the world the agent investigates. It has to satisfy three constraints at once:

1. **The chain must be genuinely discoverable.** Every link is visible in a metric or a
   log line, and the links are connected by labels (node -> shot -> queue -> department),
   so an agent that pivots on labels can walk it. Nothing is discoverable only by knowing
   the answer in advance.
2. **It must not be trivial.** There is a decoy: the asset pipeline throws cache-miss
   warnings through the whole window. They are loud, they look like a cause, and they are
   not. An agent that stops at "lots of warnings in the logs" gets the wrong answer.
3. **It must end in money.** `shot_frames_remaining` plus throughput is what turns a GPU
   fault into "SH042 lighting misses the Friday client review by 4 hours", which is the
   Potential Impact story.

The chain:
    t+0m   render-07 starts logging Xid 48 / uncorrectable ECC errors; GPU temp climbs
    t+5m   frames dispatched to render-07 fail (exit 139) and get retried
    t+8m   retries pile onto the remaining nodes; the lighting queue backs up
    t+12m  farm-wide completed-frame throughput drops ~35%
    ->     SH042 lighting pass ETA slips past the client review
"""
import math
import random

NODES = [f"render-{i:02d}" for i in range(1, 13)]
BAD_NODE = "render-07"

# Frame counts and node assignment are TUNED, not arbitrary. The fleet-triage sweep
# (second_unit/fleet.py) exposed the first version as narrative-breaking: SH043 had 2,600
# frames across two nodes, so it was ~9 hours late on a perfectly healthy farm -- worse
# than the shot the agent calls the crisis. A judge comparing the verdict ("SH042 is the
# problem") against the fleet strip ("SH043 is nine hours late") would have caught the
# contradiction immediately.
#
# The chronic-underresourcing story is real and interesting, but it is a capacity-planning
# problem, not an incident, and mixing the two muddies the one thing the demo must land.
# So the other two shots are sized to finish comfortably inside the review window, leaving
# SH042 as the unambiguous exception -- late for a REASON the agent can find.
SHOTS = [
    # shot,   department, queue,      frames left in the pass at window start
    ("SH041", "comp", "comp", 1400),        # 2 nodes @ ~4.8/min  -> ~2.4h, on track
    ("SH042", "lighting", "lighting", 1500),  # 7 nodes incl. the faulty one -> the story
    ("SH043", "fx", "fx", 950),             # 3 nodes @ ~2.1/min  -> ~2.5h, on track
]

# A farm assigns nodes to a pass; it does not smear every node across every shot.
# render-01..07 are on the lighting pass, and the bad one is among them -- losing it
# costs SH042 a seventh of its capacity, which is what makes the deadline slip real.
NODE_SHOT = {}
for _i, _n in enumerate(NODES):
    # render-01..07 -> SH042 (the faulty node is among them)
    # render-08..09 -> SH041   render-10..12 -> SH043
    NODE_SHOT[_n] = "SH042" if _i < 7 else ("SH041" if _i < 9 else "SH043")
assert NODE_SHOT[BAD_NODE] == "SH042"
DEPT_OF = {s: d for s, d, _, _ in SHOTS}
QUEUE_OF = {s: q for s, _, q, _ in SHOTS}

# Incident phase boundaries, in minutes from incident start.
ECC_START = 0
FAILURES_START = 5
QUEUE_BACKS_UP = 8
THROUGHPUT_DROPS = 12


class Farm:
    """Cumulative state for the counters. Counters must never go backwards."""

    #: Minutes from incident start at which the fault is repaired in --loop mode.
    #: Deliberately AFTER the SH042 review deadline: if the node were swapped out before
    #: the review, the shot would recover and there would be no missed delivery to
    #: forecast. The repair is the epilogue, not the rescue.
    REPAIR_AT = 180
    #: Full cycle length. After this the scenario restarts at a healthy baseline.
    CYCLE_MINUTES = 240

    #: Minutes from cycle start to each shot's client review. These are COMMITMENTS made
    #: in advance, published as telemetry, not inferred from throughput.
    #:
    #: Tuned so the arithmetic tells the truth: SH042 is 1,500 frames across 7 lighting
    #: nodes at ~1.46 frames/min each = ~10.2/min healthy = ~147 min, which comfortably
    #: beats its 170-minute review. Under the fault it runs at ~5.5/min = ~273 min and
    #: misses by about 1.7 hours. The other two shots make their reviews with room spare,
    #: so SH042 is the unambiguous exception.
    REVIEW_AT = {"SH042": 170, "SH041": 195, "SH043": 195}

    def __init__(self, seed=42, frames_done_at_start=None):
        self.rng = random.Random(seed)
        self.cyclic = False   # set by seed.py --loop
        #: Wall-clock ms at which this cycle began; set by seed.py so the published review
        #: deadlines move with the scenario instead of drifting into the past.
        self.cycle_start_ms = None
        self.ecc = {n: 0 for n in NODES}
        self.completed = {(n, s): 0 for n in NODES for s, _, _, _ in SHOTS}
        self.failed = {(n, s): 0 for n in NODES for s, _, _, _ in SHOTS}
        self.remaining = {s: total for s, _, _, total in SHOTS}
        if frames_done_at_start:
            for s, done in frames_done_at_start.items():
                self.remaining[s] = max(0, self.remaining[s] - done)

    # -- phase helpers -------------------------------------------------

    def node_healthy(self, node, minute):
        if node != BAD_NODE:
            return True
        return minute < ECC_START

    def failure_rate(self, node, minute):
        """Fraction of this node's dispatched frames that fail."""
        if node != BAD_NODE or minute < FAILURES_START or self.repaired(minute):
            return 0.002  # background flakiness, every farm has it
        ramp = min(1.0, (minute - FAILURES_START) / 6.0)
        return 0.002 + 0.85 * ramp

    def repaired(self, minute):
        """In --loop mode the node is swapped out, so the farm visibly recovers.

        A judge arriving at an arbitrary hour should be able to see the whole arc, not
        just a permanently broken farm -- and a recovery gives the agent something to
        confirm when it re-runs after the write-back.
        """
        return self.cyclic and minute >= self.REPAIR_AT

    def farm_throughput_scale(self, minute):
        """Farm-wide slowdown once retries start crowding out real work."""
        if minute < THROUGHPUT_DROPS or self.repaired(minute):
            return 1.0
        ramp = min(1.0, (minute - THROUGHPUT_DROPS) / 8.0)
        return 1.0 - 0.35 * ramp

    def queue_depth(self, queue, minute):
        base = {"lighting": 18, "comp": 9, "fx": 12}[queue]
        jitter = self.rng.uniform(-2, 2)
        if queue != "lighting" or minute < QUEUE_BACKS_UP:
            return max(0, base + jitter)
        if self.repaired(minute):
            # Drains over ~20 minutes once the bad node is out of rotation.
            drain = max(0.0, 1 - (minute - self.REPAIR_AT) / 20.0)
            return base + jitter + 46 * drain
        # Retries land back on the lighting queue and it climbs, then plateaus high.
        climb = 46 * (1 - math.exp(-(minute - QUEUE_BACKS_UP) / 9.0))
        return base + climb + jitter

    def gpu_temp(self, node, minute):
        base = 63 + self.rng.uniform(-2.5, 2.5)
        if node != BAD_NODE or minute < ECC_START or self.repaired(minute):
            return base
        climb = 22 * (1 - math.exp(-minute / 7.0))
        return base + climb

    def utilization(self, node, minute):
        if node == BAD_NODE and minute >= FAILURES_START:
            # It looks busy. It is busy failing.
            return min(0.99, 0.93 + self.rng.uniform(-0.03, 0.05))
        if minute >= QUEUE_BACKS_UP:
            return min(0.99, 0.88 + self.rng.uniform(-0.05, 0.08))
        return 0.71 + self.rng.uniform(-0.09, 0.09)

    def frame_duration(self, dept, minute):
        base = {"lighting": 41.0, "comp": 12.5, "fx": 28.0}[dept]
        if dept == "lighting" and minute >= QUEUE_BACKS_UP:
            base *= 1.0 + 0.55 * min(1.0, (minute - QUEUE_BACKS_UP) / 10.0)
        return base * self.rng.uniform(0.94, 1.06)

    def cache_hit_ratio(self, tier, minute):
        """The decoy. Dips, recovers, means nothing. Present before the incident too."""
        base = {"hot": 0.94, "cold": 0.62}[tier]
        wobble = 0.08 * math.sin(minute / 4.0) + self.rng.uniform(-0.03, 0.03)
        return max(0.05, min(0.999, base + wobble))

    # -- one tick ------------------------------------------------------

    def catalog_series(self, ts_ms):
        """Telemetry for the wider slate — the passes rendering today beyond the incident.

        Without this the catalogue was a table you could read and nothing else: no series
        meant no forecast, no investigation, and a "Shots" page whose rows led nowhere. A
        slate you cannot ask questions about is a brochure.

        These shots are deliberately UNEVENTFUL. They burn down at their department's normal
        rate, so the farm looks like a working studio rather than a farm where everything is
        on fire, and the one real incident still stands out. An investigation of one of them
        should honestly conclude "on track" — that is a useful answer, not a failure.
        """
        import sys
        from pathlib import Path
        # Two layouts: the repo (../agent/second_unit) and the container, where the package
        # is copied next to this file. Try the import first so the container path, which is
        # already importable, needs no games.
        here = Path(__file__).resolve().parent
        for cand in (here, here.parent / "agent"):
            if cand.is_dir() and str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
        try:
            from second_unit.shots_catalog import active
        except Exception:  # noqa: BLE001 — the incident must not depend on the catalogue
            return []

        out = []
        now_s = ts_ms / 1000
        for sh in active():
            # Progress is a fraction of the pass's OWN duration, not a fixed frames/min.
            #
            # The first version burned frames at a department rate (anim 11/min), so a shot
            # the catalogue considered two days into a three-day pass reported itself
            # finished after 86 minutes. The console then showed "DONE · pass complete" for a
            # shot the slate listed as rendering — two parts of the same product disagreeing
            # about the same shot, which is worse than either being wrong alone.
            #
            # Deriving progress from days_to_render makes the telemetry and the catalogue the
            # same statement expressed twice.
            span_s = max(1.0, sh.days_to_render * 24 * 3600)
            frac = (now_s - self._catalog_epoch(sh)) / span_s
            frac = max(0.0, min(1.0, frac))
            done = int(sh.total_frames * frac)
            labels = {"shot": sh.shot, "department": sh.department,
                      "farm": "vancouver", "job": "production-tracker"}
            out.append(({"__name__": "shot_frames_remaining", **labels},
                        [(sh.total_frames - done, ts_ms)]))
            out.append(({"__name__": "render_frames_completed_total",
                         "node": "pool", "shot": sh.shot,
                         "department": sh.department, "farm": "vancouver",
                         "job": "render-scheduler"}, [(done, ts_ms)]))
            # Each pass publishes its OWN review deadline.
            #
            # Without this the console judged every shot against the incident pass's
            # deadline, which is hours away — so a roto pass legitimately due in three days
            # was marked CRITICAL for being slow against a review it has nothing to do with.
            # Fifteen shots on fire for no reason is not a dashboard, it is noise.
            from datetime import datetime as _dt, time as _t
            due = _dt.combine(_dt.fromisoformat(sh.due_on).date(), _t(hour=18))
            out.append(({"__name__": "shot_review_deadline_seconds",
                         "shot": sh.shot, "department": sh.department,
                         "farm": "vancouver", "job": "production-tracker"},
                        [(round(due.timestamp()), ts_ms)]))
        return out

    @staticmethod
    def _catalog_epoch(sh) -> float:
        """When this pass started rendering, as a unix timestamp.

        Midnight, not 08:00. With an 08:00 start, every shot ingested *today* had an epoch
        in the future for the first eight hours of the day: progress clamped to zero, the
        counter never moved, and the console reported them as "no completions in window" —
        its phrase for a stalled pass. Fifteen healthy shots looked stalled every morning,
        which is precisely the false alarm that teaches an operator to ignore a screen.
        """
        from datetime import datetime, time as _time
        d = datetime.fromisoformat(sh.ingested_on)
        return datetime.combine(d.date(), _time(hour=0)).timestamp()

    def tick(self, minute, ts_ms):
        """Advance one minute. Returns (prom_series, loki_streams)."""
        series = []
        logs = []
        scale = self.farm_throughput_scale(minute)

        for node in NODES:
            if node == BAD_NODE and minute >= ECC_START and not self.repaired(minute):
                new_ecc = self.rng.randint(3, 11)
                self.ecc[node] += new_ecc
            series.append(
                ({"__name__": "render_node_gpu_ecc_errors_total", "node": node,
                  "farm": "vancouver", "job": "render-node"}, [(self.ecc[node], ts_ms)])
            )
            series.append(
                ({"__name__": "render_node_gpu_temp_celsius", "node": node,
                  "farm": "vancouver", "job": "render-node"},
                 [(round(self.gpu_temp(node, minute), 2), ts_ms)])
            )
            series.append(
                ({"__name__": "render_node_utilization_ratio", "node": node,
                  "farm": "vancouver", "job": "render-node"},
                 [(round(self.utilization(node, minute), 4), ts_ms)])
            )

            # Frames this node worked this minute. Throughput is derived from the
            # frame duration, not invented -- otherwise the farm burns through the
            # whole shot before the incident lands and there is no deadline to miss.
            for shot, dept, queue, _ in SHOTS:
                if NODE_SHOT[node] != shot:
                    continue
                per_min = 60.0 / self.frame_duration(dept, minute)
                dispatched = max(0, int(round(self.rng.gauss(per_min, per_min * 0.18)
                                              * scale)))
                if self.remaining[shot] <= 0:
                    dispatched = 0
                fr = self.failure_rate(node, minute)
                failed = sum(1 for _ in range(dispatched) if self.rng.random() < fr)
                done = dispatched - failed
                self.completed[(node, shot)] += done
                self.failed[(node, shot)] += failed
                self.remaining[shot] = max(0, self.remaining[shot] - done)

                labels = {"node": node, "shot": shot, "department": dept,
                          "farm": "vancouver", "job": "render-scheduler"}
                series.append(({"__name__": "render_frames_completed_total", **labels},
                               [(self.completed[(node, shot)], ts_ms)]))
                series.append(({"__name__": "render_frames_failed_total",
                                "reason": "gpu_fault" if node == BAD_NODE and
                                minute >= FAILURES_START else "transient", **labels},
                               [(self.failed[(node, shot)], ts_ms)]))

                if failed and node == BAD_NODE and minute >= FAILURES_START:
                    for _ in range(min(failed, 3)):
                        frame = self.rng.randint(1000, 2399)
                        logs.append((
                            {"service": "render-scheduler", "env": "prod",
                             "farm": "vancouver", "level": "error"},
                            f'level=error service=render-scheduler shot={shot} '
                            f'department={dept} frame={frame} node={node} '
                            f'exit_code=139 msg="frame failed, requeueing" retry=1/3'
                        ))

        for queue in ("lighting", "comp", "fx"):
            series.append(({"__name__": "render_queue_depth", "queue": queue,
                            "farm": "vancouver", "job": "render-scheduler"},
                           [(round(self.queue_depth(queue, minute), 1), ts_ms)]))

        # The production tracker publishes each shot's review deadline as a unix
        # timestamp. The agent queries this like any other fact about the world.
        if self.cycle_start_ms:
            for shot, dept, _, _ in SHOTS:
                minutes = self.REVIEW_AT.get(shot)
                if minutes is None:
                    continue
                deadline_s = self.cycle_start_ms / 1000.0 + minutes * 60
                series.append(({"__name__": "shot_review_deadline_seconds",
                                "shot": shot, "department": dept, "farm": "vancouver",
                                "job": "production-tracker"},
                               [(round(deadline_s), ts_ms)]))

        for shot, dept, _, _ in SHOTS:
            series.append(({"__name__": "shot_frames_remaining", "shot": shot,
                            "department": dept, "farm": "vancouver",
                            "job": "production-tracker"},
                           [(self.remaining[shot], ts_ms)]))
            series.append(({"__name__": "render_frame_duration_seconds", "shot": shot,
                            "department": dept, "farm": "vancouver",
                            "job": "render-scheduler"},
                           [(round(self.frame_duration(dept, minute), 2), ts_ms)]))

        for tier in ("hot", "cold"):
            series.append(({"__name__": "asset_cache_hit_ratio", "tier": tier,
                            "farm": "vancouver", "job": "asset-pipeline"},
                           [(round(self.cache_hit_ratio(tier, minute), 4), ts_ms)]))

        # The rest of the slate, so every active shot has telemetry and can be forecast,
        # investigated and asked about — not just the three in the incident.
        series.extend(self.catalog_series(ts_ms))

        logs.extend(self._logs(minute))
        return series, logs

    def _logs(self, minute):
        out = []

        # The real cause, only on the bad node, only after t+0, and not once repaired.
        if minute >= ECC_START and not self.repaired(minute):
            pci = "0000:41:00"
            out.append((
                {"service": "render-node", "node": BAD_NODE, "env": "prod",
                 "farm": "vancouver", "level": "error"},
                f'NVRM: Xid (PCI:{pci}): 48, pid=0, GPU has fallen off the bus. '
                f'uncorrectable ECC error encountered on GPU 0'
            ))
            if self.rng.random() < 0.6:
                out.append((
                    {"service": "render-node", "node": BAD_NODE, "env": "prod",
                     "farm": "vancouver", "level": "error"},
                    'CUDA error: uncorrectable ECC error encountered '
                    '(cudaErrorECCUncorrectable) at kernel launch'
                ))

        if minute == self.REPAIR_AT and self.cyclic:
            out.append((
                {"service": "render-scheduler", "env": "prod", "farm": "vancouver",
                 "level": "info"},
                f'level=info service=render-scheduler node={BAD_NODE} '
                f'msg="node drained and removed from rotation" reason=gpu_ecc_failure'
            ))
        if minute >= QUEUE_BACKS_UP and not self.repaired(minute):
            depth = int(self.queue_depth("lighting", minute))
            out.append((
                {"service": "render-scheduler", "env": "prod", "farm": "vancouver",
                 "level": "warn"},
                f'level=warn service=render-scheduler queue=lighting depth={depth} '
                f'msg="queue depth above threshold" threshold=30'
            ))

        # The decoy: loud, constant, harmless. Present before the incident starts.
        for _ in range(self.rng.randint(2, 5)):
            shot = self.rng.choice(["SH041", "SH042", "SH043"])
            ms = self.rng.randint(300, 1400)
            out.append((
                {"service": "asset-pipeline", "env": "prod", "farm": "vancouver",
                 "level": "warn"},
                f'level=warn service=asset-pipeline msg="texture cache miss" '
                f'asset=/assets/{shot.lower()}/tex/diffuse_{self.rng.randint(1,40):03d}.exr '
                f'fetch_ms={ms} tier=cold'
            ))

        # Healthy-farm chatter, so the logs are not 100% signal.
        node = self.rng.choice(NODES)
        out.append((
            {"service": "render-node", "node": node, "env": "prod",
             "farm": "vancouver", "level": "info"},
            f'level=info service=render-node node={node} msg="heartbeat" '
            f'slots_free={self.rng.randint(0,4)}'
        ))
        return out
