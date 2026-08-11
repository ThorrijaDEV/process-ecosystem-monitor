#!/usr/bin/env python3
"""Performance measurement for the Process Ecosystem Monitor.

Measures the three hot paths against realistic workload sizes:

    1. sweep   — one /proc sweep (collector) for N pids
    2. update  — one engine update (lifecycle + tree + history) for N pids
    3. paint   — one full layout + frame compose at WxH

Usage:
    python3 bench.py [--sweeps 20] [--ticks 200] [--pids 600]

Numbers are reported per-operation (best of the batch) plus an aggregate
frame estimate: sweep + update per tick, paint per frame.
"""

from __future__ import annotations

import argparse
import time

from process_ecosystem.collector import ProcCollector, ProcReader
from process_ecosystem.engine import EcosystemState
from process_ecosystem.history import History
from process_ecosystem.layout import compute_layout
from process_ecosystem.render import Charset, compose_ecosystem
from tests.conftest import snap, system


def build_pids(n: int, live: bool) -> list:
    """n snapshots shaped like a real system: ~26 roots, mixed depths."""
    saps = []
    roots = max(1, min(26, n // 20))
    for r in range(roots):
        saps.append(snap(r + 1, 0 if r == 0 else (r // 3 + 1), comm=f"root{r}", rss=(r + 1) << 20))
        if n > roots:
            saps.append(snap(r + 100, r + 1, comm=f"child{r}", rss=4 << 20))
    pid = 1000
    while len(saps) < n:
        parent = saps[(pid % max(1, len(saps) - 1))].pid
        saps.append(snap(pid, parent, comm=f"p{pid}", rss=(pid % 64) << 20, utime=pid % 500))
        pid += 1
    saps = saps[:n]
    return saps


def bench_sweep(sweeps: int) -> tuple[float, int]:
    collector = ProcCollector(ProcReader("/proc"))
    collector.tick()  # warm up
    best = float("inf")
    count = 0
    for _ in range(sweeps):
        t0 = time.perf_counter()
        snaps, _ = collector.tick()
        dt = time.perf_counter() - t0
        best = min(best, dt)
        count = len(snaps)
    return best, count


def bench_update(ticks: int, pids: int) -> tuple[float, int]:
    eng = EcosystemState(History(capacity=10000), clk_tck=100, ncores=8)
    saps = build_pids(pids, True)
    sys_ = system()
    best = float("inf")
    for i in range(ticks):
        # churn: a few pids vanish and reappear so the tree work is real
        if i % 3 == 0 and len(saps) > 10:
            saps2 = saps[:-3] + [snap(99999 + i, 1, comm="new")] + saps[len(saps) - 3 :]
        else:
            saps2 = saps
        t0 = time.perf_counter()
        eng.update(saps2, sys_, 0.2, i * 0.2)
        best = min(best, time.perf_counter() - t0)
    return best, len(saps)


def bench_paint(ticks: int, pids: int, w: int, h: int) -> tuple[float, int]:
    eng = EcosystemState(History(), clk_tck=100, ncores=8)
    saps = build_pids(pids, True)
    sys_ = system()
    eng.update(saps, sys_, 0.2, 0.0)
    chars = Charset(True)
    best = float("inf")
    count = 0
    for i in range(ticks):
        t0 = time.perf_counter()
        layout = compute_layout(eng, w, h, budget=40, charset=chars)
        frame = compose_ecosystem(layout, eng, i, chars, w, h)
        dt = time.perf_counter() - t0
        best = min(best, dt)
        count = len(layout.nodes)
    return best, count


def fmt(seconds: float) -> str:
    if seconds >= 0.001:
        return f"{seconds * 1000:7.2f} ms"
    return f"{seconds * 1e6:7.0f} us"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sweeps", type=int, default=20)
    ap.add_argument("--ticks", type=int, default=300)
    ap.add_argument("--pids", type=int, default=600)
    ap.add_argument("--width", type=int, default=120)
    ap.add_argument("--height", type=int, default=40)
    args = ap.parse_args()

    print(f"benchmark: {args.pids} pids, {args.width}x{args.height} canvas, "
          f"{args.sweeps} sweeps, {args.ticks} update/paint iterations")
    print()

    sweep_best, sweep_pids = bench_sweep(args.sweeps)
    print(f"  sweep  (real /proc, {sweep_pids} pids):  {fmt(sweep_best)}  "
          f"({sweep_pids / max(sweep_best, 1e-9):6.0f} pids/s)")

    update_best, update_pids = bench_update(args.ticks, args.pids)
    print(f"  update ({update_pids} pids):              {fmt(update_best)}  "
          f"({update_pids / max(update_best, 1e-9):6.0f} pids/s)")

    paint_best, paint_nodes = bench_paint(args.ticks, args.pids, args.width, args.height)
    print(f"  paint  ({paint_nodes} nodes, {args.width}x{args.height}):     {fmt(paint_best)}")

    print()
    per_tick = sweep_best + update_best
    print(f"  full tick (sweep + update):    {fmt(per_tick)}  → "
          f"max ~{1.0 / max(per_tick, 1e-9):5.0f} Hz (CPU bound, before display)")
    print(f"  full frame (tick + paint):     {fmt(per_tick + paint_best)}")
    print()
    print("  target from DESIGN.md: sweep < 10 ms, paint < 15 ms, 5 Hz nominal.")
    ok = per_tick + paint_best < 0.05
    print(f"  budget check: {'PASS' if ok else 'FAIL'} (frame must stay under 50 ms for 5 Hz + headroom)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
