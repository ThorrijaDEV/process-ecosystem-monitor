"""Process model: raw snapshots, lifecycle states, per-process state.

Pure data structures and pure transition logic — no I/O, fully testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- lifecycle states -------------------------------------------------------

NEW = "new"            # first observation(s) — entrance animation
ACTIVE = "active"      # alive and busy (measurable CPU this window)
IDLE = "idle"          # alive but quiet
DYING = "dying"        # missing from sweeps — fade-out window
DEAD = "dead"          # fade-out complete, removed from the ecosystem

LIFECYCLE_ORDER = (NEW, ACTIVE, IDLE, DYING, DEAD)

# windows, in ticks
NEW_GRACE_TICKS = 2    # how many ticks a process stays NEW
DYING_TICKS = 5        # how many missing ticks before removal (~1 s at 5 Hz)

# CPU below this % of one core counts as idle
IDLE_CPU_THRESHOLD = 0.5
# CPU above this % of one core triggers the activity spinner
SPIKE_CPU_THRESHOLD = 50.0

ZOMBIE = "Z"


@dataclass
class ProcessSnapshot:
    """One raw observation of one process from the collector (or demo).

    All fields plain numbers/strings — no computed state, no CPU math.
    """

    pid: int
    ppid: int
    comm: str
    state: str                  # kernel process state char (R/S/D/Z/...)
    utime: int                  # jiffies
    stime: int                  # jiffies
    num_threads: int
    starttime: int              # ticks since boot (identity component)
    vsize: int                  # virtual bytes
    rss: int                    # resident bytes
    user: str | None = None     # effective uid name, when readable


@dataclass
class SystemSample:
    """System-wide counters for one tick."""

    cpu_busy_ticks: int         # /proc/stat delta accumulator (see cpu.py)
    cpu_total_ticks: int
    mem_total: int              # bytes
    mem_available: int          # bytes
    load1: float
    btime: int                  # boot time epoch seconds
    uptime_ticks: int = 0       # /proc/stat "btime" companion (unused), kept for fixtures

    @property
    def mem_used(self) -> int:
        return self.mem_total - self.mem_available


@dataclass
class ProcessState:
    """Engine-side, per-process living state."""

    pid: int
    starttime: int
    comm: str
    lifecycle: str = NEW
    state: str = "R"            # last kernel state char
    ppid: int = 0
    prev_ppid: int | None = None
    utime: int = 0
    stime: int = 0
    prev_cpu_ticks: int | None = None   # baseline for delta (None → no delta yet)
    cpu_pct: float = 0.0
    rss: int = 0
    vsize: int = 0
    num_threads: int = 0
    user: str | None = None
    first_seen: int = 0         # tick index of first observation
    last_seen: int = 0          # tick index of last observation
    dying_since: int | None = None
    spike: bool = False         # CPU spike in the last window
    orphan: bool = False        # parent died underneath it
    children: set[int] = field(default_factory=set)
    rss_ring: list[int] = field(default_factory=list)   # bounded history
    cpu_ring: list[float] = field(default_factory=list)  # bounded history

    def identity(self) -> tuple[int, int]:
        return (self.pid, self.starttime)


def identity_from_snapshot(s: ProcessSnapshot) -> tuple[int, int]:
    return (s.pid, s.starttime)
