# Process Ecosystem Monitor

**Turn the Linux process tree into a living ecosystem.**

A real-time terminal visualizer for Linux that treats every process as a
creature in an ecosystem: processes are born (blinking green), live, get
busy (they spin), become idle, and die (they blink red and fade out).
Children that lose their parent become orphaned, tree branches collapse
when the ecosystem gets crowded, and a zombie lingers as a ✖.

Everything runs on **Python's standard library only** — no dependencies
to install. It reads `/proc` directly, like `htop`, but renders the
result as an animated ecosystem instead of a table.

---

## What it looks like

```
PROCESS ECOSYSTEM — demo seed 7                                  view:ecosystem

                                        ●
                         ┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬
                         │  │  │  │  │  │  │  │  │  │  │
                         ·  ◉  ●  ·  ·  ·  ·  ·  ·  ·  ·
                     ┬─┬──┬───┬                        │
                     │ │  ││ ││                        │
                     ◴ ◵  ●◉ ·●  ✖
                   ┬─┬─┬  ┬─┬
                   │ │ │  │ │
                   ◴ ● ●  ● ●
                     │
                     │
                     ●

CPU 14.8%  RAM 63.6%  procs 29  ↑0 ↓0  load 0.26  0.20s  ✖ subfra  q quit  ? help
```

`docs/screenshot-ecosystem.png` is a captured frame from the demo mode.

---

## Why

Process monitors usually show tables. A live process tree *is* a
structure — parents, children, workers, orphaned descendants, processes
that flash into existence and vanish. This project visualizes that
structure directly:

- node size = relative memory footprint (measured, from `/proc/[pid]/statm`)
- node brightness/dot-class = memory quartile
- spinner = a process using ≥ 10% of a core right now
- blinking green = brand-new process, blinking red = dying
- `✖` = zombie, `◌` = orphan
- `+N` = a crowded branch collapsed to save space (press `Enter` on it to expand)
- dedicated views: classic tree, birth/death **timeline**, and top-by-CPU

Every state is driven by measured data; nothing is faked in live mode.

---

## Features

- **Live `/proc` monitor** — systemd-style trees, per-process CPU/RSS deltas,
  kernel-thread subtrees hidden by default (`n` to toggle)
- **Deterministic demo mode** — a simulated ecosystem (compiler bursts,
  cron-job churn, browser tab churn, memory growth, a crash-and-respawn
  episode, a zombie) that exercises the exact same pipeline as live mode.
  Runs until you quit, or auto-exits after `--duration N` simulated
  seconds (countdown in the footer). Great for screenshots, demos, and tests.
- **4 views**: ecosystem (radar-like tree), classic tree, timeline
  (CPU/RAM/churn graphs + event log), top-by-CPU
- **Lifecycle engine**: NEW → ACTIVE/IDLE → DYING → DEAD with
  pid-reuse detection (`starttime` identity), orphan re-homing
- **History ring** (RAM-bounded, no telemetry) with optional JSON/CSV
  export on exit
- **ASCII fallback** (`--no-unicode`), small-terminal safe
- **Pure core**: all logic is a pipeline of testable functions —
  195 tests, zero I/O in engine/layout/render

---

## Installation

```bash
# nothing to install: stdlib only (Python 3.10+)
git clone https://github.com/ThorrijaDEV/process-ecosystem-monitor
cd process-ecosystem-monitor

# run against your own system
python3 -m process_ecosystem monitor

# or a deterministic demo ecosystem (default: seed 1)
python3 -m process_ecosystem demo

# run the test suite
python3 -m pytest tests/
```

Requires a real terminal (TUI). Running without a TTY exits with code 2
and a hint.

---

## Usage

```
process-ecosystem [command] [options]
  command: monitor (default) | demo
```

| Option | Meaning |
| --- | --- |
| `--rate HZ` | refresh rate, 0.5–10 (default 5) |
| `--budget N` | max visible nodes in the ecosystem view (default 40) |
| `--history MIN` | history ring length in minutes (default 10) |
| `--show-kernel` | include kernel threads (kthreadd subtree) |
| `--no-unicode` | ASCII glyphs |
| `--filter NAME` | start with a name filter |
| `--export PATH` | write history JSON on exit |
| `--export-csv PATH` | write history CSV on exit |
| `--seed N` | demo mode: simulation seed |
| `--duration SEC` | demo mode: auto-exit after SEC simulated seconds (default: run until quit); a `t=NN/NNs` countdown shows in the footer |
| `--speed X` | demo mode: simulation speed multiplier |

### Keys

| Key | Action |
| --- | --- |
| `q` / `Ctrl-C` | quit |
| `p` / `Space` | pause/resume |
| `Tab`, `1`–`4` | switch view (ecosystem / tree / timeline / top) |
| arrows | move selection |
| `Enter` | inspect process (CPU, RAM, parent, children, …) |
| `c` | toggle command line in the inspector |
| `n` | toggle kernel threads |
| `+` / `-` | more / fewer visible nodes |
| `m` / `M` | pan view |
| `/` | name filter |
| `Esc` | close inspector/help |
| `?` | help |

---

## How it works

```
/proc ──► collector ──► model ──► engine (lifecycle, tree, history)
  │                                 │
  └── demo (same protocol) ◄────────┘
                                   │
                         layout (pure) ──► render (pure) ──► curses UI
```

- **Identity** — a process is `(pid, starttime)`. When a pid is reused,
  the old creature dies and a new one is born (no ghost updates).
- **CPU** — delta of `utime+stime` jiffies over the sampling window,
  normalized per core. First observation reads 0. System CPU comes from
  `/proc/stat` deltas with the kernel's idle=iowait convention; windows
  shorter than one jiffy per core are ignored (they'd read as 100% noise).
- **Lifecycle** — new processes stay NEW for 2 ticks, missing processes
  fade over 5 ticks (`DYING`), then die. Reappearing mid-fade cancels the
  death. Zombies stay visible.
- **Memory** — RSS from `/proc/[pid]/statm` pages, byte-accurate.
- **Tree** — built from ppid each tick. Orphans (parent vanished) are
  re-homed under the nearest living ancestor; the ancestor chain survives
  parent eviction via a small bounded graveyard.
- **History** — in-memory ring, only exported when you ask.
- **No root, no services, no dependencies** — it reads only world-readable
  procfs files and runs as your user.

See `DESIGN.md` for the full architecture, measurements, and the
limitations discussion (CPU-percent approximations, RSSI-style caveats
for inferred relationships, etc.).

---

## Performance

Measured on this machine (28 cores, ~580 processes, 120×40 canvas):

```
sweep  (real /proc, 579 pids):   7.60 ms   (76k pids/s)
update (600 pids):               0.39 ms
paint  (40 nodes, 120x40):       0.35 ms
full frame:                      8.34 ms   (target < 50 ms for 5 Hz)
```

`python3 bench.py` reproduces these numbers.

---

## Tests

195 tests, zero external dependencies:

```bash
python3 -m pytest tests/
```

Covers the /proc parsers (malformed input, weird comm names), CPU math,
the full lifecycle (birth → active/idle → dying → death, pid reuse,
reappear-cancels-death), orphan re-homing across multiple generations,
layout golden frames, budget pruning, glyph rendering, demo
determinism, CLI behavior, and history export round-trips.

---

## Inspiration

Studied (no code copied): `htop` (C, GPL-2.0-or-later) for /proc parsing
patterns and the 22-field stat layout; `btop` (C++, Apache-2.0) for
pid-reuse handling and dead-process bookkeeping; `bottom`/`btm`
(Rust, MIT) for starttime-based identity; `pscircle` for the idea of
drawing the process tree as a living structure. This project is an
independent, clean-room implementation that combines those ideas into an
ecosystem metaphor with a lifecycle model, orphan re-homing, a timeline
view, and a deterministic demo — none of which any of the above provide
as a feature set.

---

## Limitations (honest)

- CPU% is a sampled estimate (per-core normalized delta), not a kernel
  accounting value; short windows are noisy and are averaged out.
- RSS shows resident memory, not shared-memory attribution (same
  convention as htop).
- Tree relationships are inferred from ppid at sweep time; after pid
  reuse the displayed ancestry may briefly reflect the new incarnation.
- The demo ecosystem's memory numbers are simulated; live mode is real.

---

## License

MIT — see `LICENSE`.
