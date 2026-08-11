"""CPU accounting.

Documented approximations (see DESIGN.md §6):

- Per-process CPU% is the delta of (utime+stime) jiffies over the wall-clock
  sampling interval, normalized per core: a single-core-bound process shows
  ~100/ncores.
- The first observation of a process has no delta and shows 0.
- System CPU% is the delta of busy ticks over total ticks from /proc/stat,
  with idle = idle+iowait and guest folded into user (kernel convention).
"""

from __future__ import annotations


def parse_system_stat(text: str) -> tuple[dict[int, list[int]], int]:
    """Parse /proc/stat; return (cpus, btime).

    cpus maps -1 (aggregate 'cpu' row) and n (per-core rows) to the 10
    field values in kernel order: user nice system idle iowait irq softirq
    steal guest guest_nice. -1 is used so per-core row 0 cannot collide
    with the aggregate. btime is the boot-time epoch (0 when absent).
    """
    cpus: dict[int, list[int]] = {}
    btime = 0
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "cpu":
            cpus[-1] = [int(v) for v in parts[1:11]]
        elif parts[0].startswith("cpu") and parts[0][3:].isdigit():
            cpus[int(parts[0][3:])] = [int(v) for v in parts[1:11]]
        elif parts[0] == "btime" and len(parts) >= 2:
            btime = int(parts[1])
    return cpus, btime


def system_busy_delta(prev: list[int] | None, cur: list[int] | None) -> tuple[int, int]:
    """Return (busy_delta, total_delta) between two /proc/stat 'cpu' rows.

    Idle includes iowait; guest/guest_nice are folded into user/nice by the
    kernel already (kernel convention) and ignored here.
    """
    if prev is None or cur is None:
        return (0, 0)
    if len(cur) < 4 or len(prev) < 4:
        return (0, 0)
    total_cur = sum(cur[:10])
    total_prev = sum(prev[:10])
    idle_cur = cur[3] + cur[4]          # idle + iowait
    idle_prev = prev[3] + prev[4]
    total = max(0, total_cur - total_prev)
    busy = max(0, (total_cur - idle_cur) - (total_prev - idle_prev))
    return (busy, total)


def system_cpu_pct(busy_delta: int, total_delta: int) -> float:
    """System-wide CPU% (0..100*ncores scale is possible; clamped to 100)."""
    if total_delta <= 0:
        return 0.0
    return min(100.0, busy_delta / total_delta * 100.0)


def process_cpu_pct(
    prev_ticks: int | None,
    cur_ticks: int,
    interval_sec: float,
    clk_tck: int,
    ncores: int,
) -> float:
    """Per-process CPU% normalized per core.

    prev_ticks is the (utime+stime) baseline from the previous observation.
    A counter reset (cur < prev) yields a fresh baseline → 0 for this window.
    Returns 0 when no baseline exists yet (first sample).
    """
    if prev_ticks is None or interval_sec <= 0 or clk_tck <= 0 or ncores <= 0:
        return 0.0
    delta = cur_ticks - prev_ticks
    if delta < 0:
        return 0.0
    return delta / (interval_sec * clk_tck * ncores) * 100.0


def normalize_ticks(utime: int, stime: int) -> int:
    return max(0, utime) + max(0, stime)


def load_from_loadavg(text: str) -> float:
    """First field of /proc/loadavg; 0.0 on garbage."""
    try:
        return float(text.split()[0])
    except (IndexError, ValueError):
        return 0.0
