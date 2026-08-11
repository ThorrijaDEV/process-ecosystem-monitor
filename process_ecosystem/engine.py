"""Ecosystem state engine.

Turns raw snapshots into a living model: per-process lifecycle, CPU deltas,
tree building, birth/death detection, orphan handling, pid-reuse identity,
bounded history. Pure logic — no I/O, fully testable with synthetic input.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .cpu import normalize_ticks, process_cpu_pct, system_cpu_pct
from .history import Event, History, TickEntry
from .model import (
    ACTIVE,
    DEAD,
    DYING,
    IDLE,
    IDLE_CPU_THRESHOLD,
    NEW,
    NEW_GRACE_TICKS,
    SPIKE_CPU_THRESHOLD,
    DYING_TICKS,
    ProcessSnapshot,
    ProcessState,
    SystemSample,
)

RING_LEN = 30
GRAVEYARD_MAX = 512  # remembered (pid → ppid) of evicted processes for orphan re-homing


@dataclass
class Diff:
    """What changed in one tick (for the UI and tests)."""

    births: list[tuple[int, str]] = field(default_factory=list)   # (pid, comm)
    deaths: list[tuple[int, str]] = field(default_factory=list)
    spikes: list[tuple[int, str]] = field(default_factory=list)
    orphans: list[tuple[int, str]] = field(default_factory=list)
    cpu_pct: float = 0.0
    ram_pct: float = 0.0
    alive: int = 0


class EcosystemState:
    def __init__(self, history: History | None = None, clk_tck: int = 100, ncores: int = 1):
        self.processes: dict[int, ProcessState] = {}
        self.history = history if history is not None else History()
        self.clk_tck = clk_tck
        self.ncores = ncores
        self.tick_index = 0
        self.btime: int = 0
        self.system_cpu = 0.0
        self.system_ram = 0.0
        self.load1 = 0.0
        self._prev_sys_busy: int | None = None
        self._prev_sys_total: int | None = None
        self._graveyard: dict[int, int] = {}  # pid → ppid of evicted processes

    # --- core update ---------------------------------------------------------

    def update(
        self,
        snapshots: list[ProcessSnapshot],
        system: SystemSample | None,
        interval_sec: float,
        wall_time: float,
    ) -> Diff:
        """Advance the ecosystem by one tick."""
        diff = Diff()
        seen: set[int] = set()

        # 1) observe everything that is alive
        for snap in snapshots:
            seen.add(snap.pid)
            proc = self.processes.get(snap.pid)
            if proc is None or proc.starttime != snap.starttime:
                # new process, or pid reuse (starttime changed) → fresh state
                if proc is not None:
                    self._evict(proc, diff)  # old incarnation died
                proc = self._spawn(snap, diff)
                self.processes[snap.pid] = proc
            else:
                self._observe(proc, snap)
            self._apply_cpu(proc, snap, interval_sec, diff)
            self._apply_memory(proc, snap)

        # 2) lifecycle for living processes; detect deaths
        for pid in list(self.processes):
            proc = self.processes[pid]
            if pid in seen:
                if proc.lifecycle == NEW:
                    if self.tick_index - proc.first_seen < NEW_GRACE_TICKS:
                        continue
                    proc.lifecycle = self._next_active_idle(proc)
                elif proc.lifecycle == DYING:
                    proc.lifecycle = self._next_active_idle(proc)  # reappeared
                else:
                    proc.lifecycle = self._next_active_idle(proc)
                continue
            if proc.lifecycle == DYING:
                if self.tick_index - (proc.dying_since or self.tick_index) + 1 >= DYING_TICKS:
                    self._evict(proc, diff)
                continue
            proc.lifecycle = DYING
            proc.dying_since = self.tick_index

        # 3) system metrics
        self._system_metrics(system, diff)

        # 4) tree + orphan re-homing
        self._build_tree(diff)

        # 5) history
        self._history_tick(wall_time, diff)

        self.tick_index += 1
        return diff

    # --- internals -----------------------------------------------------------

    def _spawn(self, snap: ProcessSnapshot, diff: Diff) -> ProcessState:
        proc = ProcessState(
            pid=snap.pid,
            starttime=snap.starttime,
            comm=snap.comm,
            lifecycle=NEW,
            state=snap.state,
            ppid=snap.ppid,
            utime=snap.utime,
            stime=snap.stime,
            first_seen=self.tick_index,
            last_seen=self.tick_index,
        )
        diff.births.append((proc.pid, proc.comm))
        return proc

    def _evict(self, proc: ProcessState, diff: Diff) -> None:
        """Remove a process (death or pid reuse); children become orphans."""
        for child_pid in proc.children:
            child = self.processes.get(child_pid)
            if child is not None:
                child.orphan = True
                diff.orphans.append((child_pid, child.comm))
        self._graveyard[proc.pid] = proc.ppid
        while len(self._graveyard) > GRAVEYARD_MAX:
            self._graveyard.pop(next(iter(self._graveyard)))
        diff.deaths.append((proc.pid, proc.comm))
        self.processes.pop(proc.pid, None)

    def _observe(self, proc: ProcessState, snap: ProcessSnapshot) -> None:
        proc.comm = snap.comm
        proc.state = snap.state
        proc.ppid = snap.ppid
        proc.utime = snap.utime
        proc.stime = snap.stime
        proc.num_threads = snap.num_threads
        proc.vsize = snap.vsize
        proc.last_seen = self.tick_index
        if proc.lifecycle == DYING:
            proc.lifecycle = ACTIVE
            proc.dying_since = None

    def _apply_cpu(self, proc: ProcessState, snap: ProcessSnapshot, interval_sec: float, diff: Diff) -> None:
        cur = normalize_ticks(snap.utime, snap.stime)
        pct = process_cpu_pct(proc.prev_cpu_ticks, cur, interval_sec, self.clk_tck, self.ncores)
        proc.prev_cpu_ticks = cur
        proc.cpu_pct = pct
        proc.cpu_ring.append(pct)
        if len(proc.cpu_ring) > RING_LEN:
            del proc.cpu_ring[0]
        if pct >= SPIKE_CPU_THRESHOLD and not proc.spike:
            proc.spike = True
            diff.spikes.append((proc.pid, proc.comm))
        elif pct < SPIKE_CPU_THRESHOLD:
            proc.spike = False

    def _apply_memory(self, proc: ProcessState, snap: ProcessSnapshot) -> None:
        proc.rss = snap.rss
        proc.rss_ring.append(snap.rss)
        if len(proc.rss_ring) > RING_LEN:
            del proc.rss_ring[0]

    @staticmethod
    def _next_active_idle(proc: ProcessState) -> str:
        if proc.state == "Z":
            return ACTIVE  # zombies stay visible, rendered specially
        return ACTIVE if proc.cpu_pct >= IDLE_CPU_THRESHOLD else IDLE

    def _system_metrics(self, system: SystemSample | None, diff: Diff) -> None:
        if system is None:
            return
        self.btime = system.btime
        self.load1 = system.load1
        self.mem_total = system.mem_total
        if system.mem_total:
            self.system_ram = (system.mem_total - system.mem_available) / system.mem_total * 100.0
        if self._prev_sys_total is not None and system.cpu_total_ticks >= self._prev_sys_total:
            busy = system.cpu_busy_ticks - (self._prev_sys_busy or 0)
            total = system.cpu_total_ticks - self._prev_sys_total
            # Windows shorter than ~1 jiffy per core carry no signal
            # (a partially-elapsed jiffy reads as 100% busy). Keep the
            # previous value instead of reporting noise.
            if total >= self.ncores:
                self.system_cpu = system_cpu_pct(busy, total)
        self._prev_sys_busy = system.cpu_busy_ticks
        self._prev_sys_total = system.cpu_total_ticks
        diff.cpu_pct = self.system_cpu
        diff.ram_pct = self.system_ram
        diff.alive = len(self.processes)

    def _build_tree(self, diff: Diff) -> None:
        living = set(self.processes)

        # flag orphans: living process whose parent is not in this tick
        for pid, proc in self.processes.items():
            ppid = proc.ppid
            if ppid != 0 and ppid not in living and not proc.orphan:
                proc.orphan = True
                diff.orphans.append((pid, proc.comm))
            if ppid != 0 and ppid in living:
                proc.orphan = False

        for proc in self.processes.values():
            proc.children.clear()

        for pid, proc in self.processes.items():
            parent = self.processes.get(proc.ppid)
            if parent is not None:
                parent.children.add(pid)
                continue
            if proc.orphan:
                ancestor = self._nearest_living_ancestor(proc)
                if ancestor is not None:
                    ancestor.children.add(pid)

    def _nearest_living_ancestor(self, proc: ProcessState) -> ProcessState | None:
        seen: set[int] = set()
        ppid = proc.ppid
        while ppid and ppid not in seen:
            seen.add(ppid)
            parent = self.processes.get(ppid)
            if parent is not None:
                return parent
            # the ancestor died: keep climbing through remembered evictions
            ppid = self._graveyard.get(ppid, 0)
        return None

    def _history_tick(self, wall_time: float, diff: Diff) -> None:
        events: list[Event] = []
        for pid, comm in diff.births:
            events.append(Event("birth", pid, comm, wall_time))
        for pid, comm in diff.deaths:
            events.append(Event("death", pid, comm, wall_time))
        for pid, comm in diff.spikes:
            events.append(Event("spike", pid, comm, wall_time))
        for pid, comm in diff.orphans:
            events.append(Event("orphan", pid, comm, wall_time))
        self.history.push(
            TickEntry(
                tick_time=wall_time,
                cpu_pct=self.system_cpu,
                ram_pct=self.system_ram,
                births=len(diff.births),
                deaths=len(diff.deaths),
                alive=len(self.processes),
                events=events,
            )
        )

    # --- accessors -----------------------------------------------------------

    def process(self, pid: int) -> ProcessState | None:
        return self.processes.get(pid)

    def children_of(self, pid: int) -> list[int]:
        proc = self.processes.get(pid)
        if proc is None:
            return []
        return sorted(proc.children)

    def roots(self) -> list[int]:
        """Pids with no living parent (kernel threads excluded)."""
        living = set(self.processes)
        return sorted(pid for pid, proc in self.processes.items() if proc.ppid not in living and proc.ppid != 2)

    def kernel_threads(self) -> list[int]:
        return sorted(pid for pid, proc in self.processes.items() if proc.ppid == 2)

    def alive_count(self) -> int:
        return len(self.processes)

    def age_seconds(self, proc: ProcessState, wall_time: float) -> float | None:
        if not self.btime or not proc.starttime:
            return None
        birth = self.btime + proc.starttime / self.clk_tck
        return max(0.0, wall_time - birth)

    def reset(self) -> None:
        self.processes.clear()
        self.history.clear()
        self.tick_index = 0
        self.btime = 0
        self._prev_sys_busy = None
        self._prev_sys_total = None
        self._graveyard.clear()
