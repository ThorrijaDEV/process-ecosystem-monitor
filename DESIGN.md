# Process Ecosystem Monitor — Design

> Turn the Linux process tree into a living ecosystem: processes are born, consume
> resources, spawn children, compete, idle, and disappear — visibly.

Status: design draft (2026-08-11). Phase 2 of the laboratory workflow.

---

## 1. Concept

Not another `htop`. The default view is an **ecosystem**: a living visual field
where processes are nodes that enter with an entrance animation, glow with
activity, grow with memory, branch into children, and fade out when they die.

The data pipeline is strict and testable:

```text
Linux collector          (/proc scan, rootable for tests)
        ↓
Process model            (snapshot, identity, lifecycle)
        ↓
Ecosystem state          (diffing, tree, history ring)
        ↓
Visualization            (layout + frames, pure)
        ↓
Terminal UI              (curses, views, keys)
```

Rules:

- The collector knows nothing about rendering.
- The renderer is testable without touching `/proc` (pure functions on model state).
- The demo mode is a collector implementation, so demo and live use the same pipeline.

## 2. Data sources (Linux, standard, unprivileged)

| Source | Use | Readable unprivileged? |
|---|---|---|
| `/proc/[pid]/stat` | identity, ppid, state, utime/stime, starttime, threads | yes (all users) |
| `/proc/[pid]/statm` | RSS in pages (fast bulk) | yes |
| `/proc/[pid]/status` | inspector details (VmRSS, Threads, Uid, State) | yes |
| `/proc/[pid]/cmdline` | full command line (opt-in only, privacy) | yes (may be empty) |
| `/proc/[pid]/cgroup` | cgroup path (nice-to-have, not required) | yes |
| `/proc/[pid]/io` | I/O bytes (optional metrics) | own processes only |
| `/proc/[pid]/children` | NOT used — own-user only, absent for kernel threads | mostly no |
| `/proc/stat` | system CPU deltas, total ticks | yes |
| `/proc/meminfo` | system memory totals | yes |
| `/proc/loadavg` | load averages | yes |

Facts measured on this machine (uid 1000): `stat/statm/status/cmdline/cgroup/wchan`
readable even for root-owned PID 1; `io/environ/children` NOT readable for other
users. Full sweep of 582 pids (stat + statm) ≈ 5.5 ms best-of-5.

- No `ps` shell-outs. `/proc` is faster, cleaner, and testable with fixtures.
- No `sudo`, no system services, no `/proc` modification, no config changes.
- Default refresh: 5 Hz (0.2 s); range 1–10 Hz via `+`/`-`.

## 3. Process collection

`ProcCollector` walks `/proc` numeric directories once per tick:

- `read_stat()` — split on last `)` (comm may contain spaces/parens), then parse
  fields: state(3) ppid(4) utime(14) stime(15) num_threads(20) starttime(22)
  vsize(23) rss(24).
- `read_statm()` — `size resident shared text lib data dt` (pages).
- `read_cmdline()` — only when the UI explicitly asks (inspector toggle or
  `--show-cmdline`). Default UI shows `comm` only.
- Tolerates: missing pid (race), `PermissionError`, truncated files, garbage —
  each failure downgrades gracefully, never aborts the sweep.

Collector is rootable for tests: `ProcCollector(proc_root=tmpdir)` where the
fixture provides a fake proc tree (see Testing).

## 4. Process identity

Identity = `(pid, starttime)` per Linux convention. `starttime` (stat field 22,
ticks since boot) is the pid-reuse detector:

- If a pid reappears with a different starttime → it is a **new process**; all
  cached state (cpu baselines, lifecycle, history) for the old incarnation is
  dropped. (btop's `dead_procs` and bottom's start_time checks inspired this.)

## 5. Lifecycle model

States (driven by collector deltas, not wall-clock assumptions):

```text
NEW        first observation (entrance animation)
ACTIVE     observed, has recent activity
IDLE       running but no measurable CPU this window
DYING      seen before, missing from the latest sweep (fade-out window)
DEAD       fade-out complete → removed from ecosystem
```

Rules:

- `NEW` lasts a fixed short grace (e.g. 2 s of ticks) unless it dies.
- `ACTIVE`/`IDLE` decided per tick: CPU over an activity threshold → ACTIVE,
  else IDLE. Idle nodes render dimmed.
- A process missing from a sweep enters `DYING` (1–2 s fade) instead of
  vanishing instantly; if it reappears (pid still alive, e.g. a missed read),
  it returns to `ACTIVE` and the death is cancelled.
- `DYING` → `DEAD` removes the node; a birth/death event is recorded in history.
- CPU spike events are tracked per process (threshold-crossing), shown as a
  flash glyph.

## 6. CPU calculation

Documented approximation, not a claim of precision.

- System: from `/proc/stat` totals — `busy = total − idle − iowait`
  (guest folded into user per kernel convention; iowait folded into idle).
  `cpu_pct = Δbusy / Δtotal * 100` over the sampling window.
- Per process: `Δ(utime + stime)` in jiffies (CLK_TCK from `os.sysconf`),
  divided by the **wall-clock sampling interval** × cores, × 100:

  `pct = Δcpu_ticks / (interval_sec × CLK_TCK × ncores) × 100`

  → a single-core-bound process shows ≈ `100/ncores` (per-core normalization,
  the htop `PERCENT_NORM_CPU` convention). `ncores` read from
  `os.cpu_count()`/`_SC_NPROCESSORS_ONLN`.
- First observation of a process has no delta → CPU shown as 0 until the next
  tick (documented in README; matches btop/bottom behavior).
- Processes that disappear between samples contribute their final delta to the
  last frame they were alive in.
- Counter resets / pid reuse: handled by identity rule (Section 4) — baseline
  reset on starttime change.
- Refreshing only when paused: deltas frozen, UI shows a pause state.

## 7. Memory

- Bulk: RSS from `/proc/[pid]/statm` resident pages × `os.sysconf(PAGESIZE)`
  (cheapest per-sweep source; same as htop).
- System: `/proc/meminfo` `MemTotal`/`MemAvailable`.
- Node size in the ecosystem derives from RSS (log scale), clamped to glyph
  classes (small/medium/large/huge). Inspector shows RSS, VSZ (stat field 23),
  %RAM = RSS/MemTotal, and per-process RSS history sparkline.
- Memory changes over time: per-process RSS history ring (bounded) for the
  inspector; ecosystem-wide RAM trend in the status/footer area.

## 8. Tree construction

- Build adjacency from `ppid` each tick (`pid → children`, sorted by child pid
  for stability).
- Missing parent (dead, outside namespace, or ppid=0): process becomes a root.
- Kernel threads (ppid=2, kthreadd) are **hidden by default** (`N` toggles
  "show kernel noise").
- Reparenting: if the parent dies while children live, the orphan subtree is
  re-attached to the parent's parent (nearest living ancestor) — orphan markers
  shown when this happens (event in history).
- Cycles are impossible by construction (ppid of a live process is a live or
  dead pid; dead pids resolve to nothing → root), but the builder still
  guards against visiting a pid twice.

## 9. Ecosystem layout (primary view)

Terminal-native, glyph-based, deterministic (testable golden frames):

- **Layered forest**: depth = distance from root; each layer is a row band.
- Children placed in order around/under their parent; a child node is drawn in
  the horizontal span of its parent's subtree.
- Links: box-drawing chars (`─ │ ╭ ╮ ╰ ╯`), dim color, drawn before nodes.
- Node glyph classes by RSS (log scale): `· ● ◉ ⬤` (with `o O @` ASCII fallback
  when the locale/TERM can't do unicode). Color = CPU activity (dim→bright),
  state color overlays: NEW = green flash + `A_BLINK` for the grace period,
  DYING = red fade (brightness steps), IDLE = dimmed, ACTIVE = full.
- CPU activity animation: per-tick spinner glyphs `◴◷◶◵` (or `-\|/`) for
  processes above the activity threshold — cheap, alive-looking, no motion
  tweening (research: nobody tweens node motion in a TUI; color/glyph fades
  are the norm).
- **Tree pruning for screen width** (research-backed, no tool force-layouts 300
  nodes): show all roots + a bounded detail budget (default max ~40 visible
  nodes). Nodes over budget collapse into `+N` markers; the node with the
  highest subtree weight (CPU+memory) keeps expanding. Keys `m`/`M` adjust the
  budget; `f`/`F` change collapse aggressiveness. The whole canvas is
  scrollable/pan-able with arrows (offset state; pan preserves tree positions
  for stability).
- Node positions are stable across ticks (fixed slot by pid within parent
  span) — no jitter while processes are unchanged.

## 10. Views

Four views over the same state (`Tab` / `1 2 3 4` to switch):

1. **Ecosystem** — the defining view (Section 9).
2. **Tree** — classic indented hierarchy, polished: box-drawing, state glyphs,
   per-row CPU/RSS bars, collapse/expand nodes, scroll, selectable.
3. **Timeline** — births/deaths per window + CPU/RAM trend sparklines from the
   history ring; last N minutes.
4. **Top** — compact sorted resource view (CPU then RAM), 1 line per process,
   bar sparklines.

## 11. History (lightweight, local, bounded)

- In-memory ring buffer, `--history N` minutes (default 10), always bounded;
  resizeable; nothing written to disk, so "delete" is trivial (it's RAM).
- Records per tick: total CPU, RAM%, births, deaths, and recent birth/death
  event details (pid, comm, parent) for the Timeline view.
- Optional `export` subcommand writes the in-memory history as JSON/CSV on
  demand (user-initiated only) — for analysis, not telemetry.
- No telemetry, no network access, no auto-persisted databases.

## 12. Interaction

| Key | Action |
|---|---|
| `q` | quit (clean terminal restore) |
| `p` | pause/resume (freezes sampling; deltas reset on resume) |
| `Tab`/`1..4` | switch view |
| `↑/↓`/`←/→` | navigate / pan ecosystem |
| `Enter` | inspect selected process (panel) |
| `c` | toggle command line in inspector (privacy gate) |
| `n` | toggle kernel/system noise |
| `+`/`-` | refresh rate 1–10 Hz |
| `m`/`M` | ecosystem detail budget |
| `/` | filter by name (simple substring) |
| `?` | help overlay |

Inspector shows only reliable fields: PID, comm, parent pid, state, CPU%,
RSS, %RAM, threads, age (now − starttime), children count, user (from status
Uid, first real uid), and a small RSS/CPU sparkline. Command line hidden by
default.

## 13. Demo mode

`process-ecosystem demo [--seed N] [--duration S] [--speed F]`

- A `DemoCollector` implementing the same collector protocol, emitting a
  deterministic scripted population: stable long-lived services, spawning
  workers, CPU spikes on schedule, memory growth, short-lived children,
  periodic deaths, orphan episodes.
- Seeded PRNG → identical event sequence for a given seed (testable
  determinism, reproducible README screenshots).
- Reuses the exact same engine/visualization/TUI code as live mode — no fake
  rendering path.

## 14. Performance targets

- Collector: < 10 ms per full sweep (measured ~5.5 ms for 582 pids).
- Rendering: full redraw budget < 15 ms at 80×24; partial-cell differential
  where cheap; measure and document real numbers.
- Self CPU: target < 2% of one core while idle in the foreground.
- History ring bounded → no unbounded growth.
- Benchmark harness: `./bench.py` (or pytest benchmark tests) timing sweep,
  tick, and render with the real collector and a 582-pid fixture.

## 15. Privacy

- No uploads, no telemetry, no network I/O.
- Default UI shows `comm` only; command lines require an explicit keypress and
  are never logged, exported, or screenshot (README examples use demo data).
- README screenshots and portfolio images generated exclusively from demo mode.
- Document exactly what data the tool reads and displays.

## 16. Permissions

- Runs as a normal user. Other users' `stat/statm/status/cmdline` are readable
  on Linux; `io/environ/children` are not → those features degrade silently
  (inspector shows "n/a").
- No root anywhere. If something turns out to need elevation, STOP and ask.

## 17. Terminal compatibility

- `curses` with `curses.setupterm` fallback; ASCII glyph table when locale
  is not UTF-8 (checked via `locale.nl_langinfo(CODESET)`).
- Clean restore on: normal quit, Ctrl+C (signal handler restores terminal),
  exceptions (try/finally around the curses session), SIGWINCH resize
  (curses re-queries size each loop).
- Tested under: xterm-256color PTY, tmux, small (40×12) and large terminals,
  and a no-tty environment (CLI prints error and exits non-zero).

## 18. Dependencies

- Runtime: **Python 3.10+ stdlib only** (argparse, curses, os, locale, time,
  dataclasses, math, json). Zero pip dependencies → zero install risk.
- Dev/test: pytest (like previous projects). pyte (optional) for TUI
  integration tests, as in ble-radar.

## 19. Testing strategy

Synthetic everywhere — tests never depend on live processes:

- `tests/fixtures.py` — builder for fake `/proc` trees (stat/statm/status/
  cmdline/meminfo/stat files with scripted content, malformed variants,
  permission-denied variants, pid-reuse variants).
- Collector tests: parsing, malformed files, missing processes, permission
  failures, race (pid vanishes mid-read).
- Model/lifecycle tests: NEW→ACTIVE→IDLE→DYING→DEAD, reappear-cancels-death,
  starttime change = new identity.
- CPU tests: known tick deltas → expected %, multi-core normalization,
  counter-reset/pid-reuse baseline reset, first-sample-zero.
- Tree tests: forest building, orphans re-attached, kernel-thread filter,
  cycle guard, stability of ordering.
- Layout tests: golden frames for scripted populations (like ble-radar's
  golden frame tests).
- History tests: ring bounds, event recording, export format.
- Demo tests: same seed → identical event sequence; duration/exit codes.
- CLI tests: `--version`, `--help`, invalid flags, `demo --duration 1` runs
  and exits 0, no-tty behavior, `export` content.
- TUI smoke tests (optional, pyte): render frames from engine state, keys
  mapped, resize handling.

Target: ≥ 90 tests, all green, hardware-independent.

## 20. Project layout

```text
process-ecosystem-monitor/
├── process_ecosystem/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── collector.py      # ProcCollector + ProcReader (rootable)
│   ├── model.py          # ProcessSnapshot, states, identity
│   ├── cpu.py            # CPU delta math
│   ├── engine.py         # EcosystemState: diff, tree, history
│   ├── history.py        # bounded ring
│   ├── layout.py         # pure ecosystem layout
│   ├── render.py         # frame model + glyph tables (pure)
│   ├── demo.py           # DemoCollector (deterministic)
│   ├── ui.py             # curses app + views + inspector
│   └── version.py
├── tests/
│   ├── fixtures.py
│   └── test_*.py
├── bench.py              # overhead measurement
├── DESIGN.md
├── README.md             # after it works (Phase 20)
└── .gitignore
```

## 21. Roadmap within this build

1. collector + model + cpu + engine (pure, tested)
2. layout + render (pure, golden frames)
3. demo collector (deterministic)
4. curses UI + views + inspector + keys
5. CLI + signal/resize hygiene
6. bench + perf review
7. README + privacy review
8. full local validation → publication proposal (STOP for approval)

## 22. Differentiation (vs htop/btop)

- **Lifecycle as first-class**: entrance animation, fade-out death, activity
  glow, spike flashes — not a list of rows.
- **Ecosystem metaphor**: memory-sized nodes, branches, pan-able living field.
- **Birth/death timeline** as a view (nobody else has it).
- **Honest CPU**: documented per-core normalization + first-sample-zero.
- **Zero dependencies** and rootless, fixture-testable data layer.
