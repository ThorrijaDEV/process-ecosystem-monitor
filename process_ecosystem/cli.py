"""Command-line entry point for the Process Ecosystem Monitor.

Commands:
    process-ecosystem [monitor]   live /proc monitor (needs a tty)
    process-ecosystem demo        deterministic simulated ecosystem
    process-ecosystem --version

All modes share the same engine and UI pipeline. `--headless N` runs N ticks
without a terminal (CI / tests) and prints a summary.
"""

from __future__ import annotations

import argparse
import curses
import os
import sys
import time

from . import __version__
from .collector import ProcCollector, ProcReader
from .demo import BTIME, DemoCollector
from .engine import EcosystemState
from .history import History
from .model import ProcessSnapshot
from .ui import App, Options


def _sysconf(name: str, default: int) -> int:
    try:
        return os.sysconf(name)
    except (ValueError, OSError, AttributeError):
        return default


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="process-ecosystem",
        description="Turn the Linux process tree into a living ecosystem.",
    )
    p.add_argument("--version", action="version", version=f"process-ecosystem {__version__}")
    p.add_argument("command", nargs="?", choices=("monitor", "demo"), default="monitor",
                   help="mode to run (default: monitor)")
    p.add_argument("--rate", type=float, default=5.0, metavar="HZ", help="refresh rate in Hz (default 5, range 0.5-10)")
    p.add_argument("--budget", type=int, default=40, metavar="N", help="ecosystem detail budget: max visible nodes (default 40)")
    p.add_argument("--history", type=int, default=10, metavar="MIN", help="history ring length in minutes (default 10)")
    p.add_argument("--show-kernel", action="store_true", help="show kernel threads (kthreadd subtree)")
    p.add_argument("--no-unicode", dest="unicode", action="store_false", default=None, help="force ASCII glyphs")
    p.add_argument("--filter", metavar="NAME", default="", help="initial name filter")
    p.add_argument("--export", metavar="PATH", help="write history JSON to PATH on exit")
    p.add_argument("--export-csv", metavar="PATH", help="write history CSV to PATH on exit")
    p.add_argument("--proc-root", default="/proc", metavar="DIR", help=argparse.SUPPRESS)
    p.add_argument("--seed", type=int, default=1, help="demo: simulation seed (default 1)")
    p.add_argument("--duration", type=float, default=None, metavar="SEC",
                   help="demo: exit after SEC simulated seconds (default: run until quit)")
    p.add_argument("--speed", type=float, default=1.0, help="demo: simulation speed multiplier (default 1)")
    p.add_argument("--headless", type=int, default=0, metavar="TICKS", help=argparse.SUPPRESS)
    return p


def _make_options(args) -> Options:
    return Options(
        rate=1.0 / max(0.1, min(10.0, args.rate)),
        budget=args.budget,
        show_kernel=args.show_kernel,
        history_minutes=args.history,
        unicode=args.unicode,
        filter_text=args.filter,
        mode_label="live",
    )


def _export(engine: EcosystemState, args) -> None:
    if args.export:
        with open(args.export, "w", encoding="utf-8") as fh:
            fh.write(engine.history.to_json())
    if args.export_csv:
        with open(args.export_csv, "w", encoding="utf-8") as fh:
            fh.write(engine.history.to_csv())


def _headless(engine: EcosystemState, collector, opts: Options, interval: float, ticks: int, wall_time0: float) -> int:
    app = App(collector, engine, opts, interval_sec=interval, initial_wall_time=wall_time0)
    ran = 0
    while ran < ticks and not app.quit and not collector.finished:
        app._tick()
        ran += 1
    print(f"ticks {ran}  alive {engine.alive_count()}  "
          f"cpu {engine.system_cpu:5.1f}%  ram {engine.system_ram:5.1f}%  "
          f"history {len(engine.history)}  load {engine.load1:4.2f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    opts = _make_options(args)

    rate = max(0.1, min(10.0, args.rate))
    interval = 1.0 / rate
    clk_tck = _sysconf("SC_CLK_TCK", 100)
    ncores = max(1, os.cpu_count() or 1)
    page_size = _sysconf("SC_PAGE_SIZE", 4096)

    if args.command == "demo":
        speed = max(0.1, args.speed)
        eff_interval = interval / speed
        seed = args.seed
        opts.mode_label = f"demo seed {seed}"
        collector = DemoCollector(seed=seed, interval=eff_interval, clk_tck=clk_tck,
                                  ncores=ncores, duration=args.duration)
        wall0 = BTIME + 3660.0
    else:
        collector = ProcCollector(ProcReader(args.proc_root, clk_tck, page_size, ncores))
        wall0 = time.time()

    engine = EcosystemState(History(capacity=int(args.history * 60 / interval)), clk_tck=clk_tck, ncores=ncores)

    if args.headless:
        rc = _headless(engine, collector, opts, interval, args.headless, wall0)
        _export(engine, args)
        return rc

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("process-ecosystem needs a terminal (try: process-ecosystem demo --headless 20)", file=sys.stderr)
        return 2

    app = App(collector, engine, opts, interval_sec=interval, clk_tck=clk_tck, ncores=ncores, initial_wall_time=wall0)
    try:
        screen = curses.initscr()
        try:
            app.run(screen)
        finally:
            curses.endwin()
    except curses.error:
        curses.endwin()
        raise
    _export(engine, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
