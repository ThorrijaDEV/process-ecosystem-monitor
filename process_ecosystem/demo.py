"""Deterministic demo collector.

Simulates a small synthetic Linux box (services, a workspace, a browser
with tab churn, a compiler burst, cron jobs, memory growth, a crash-and-
respawn episode) through the SAME collector protocol as the real /proc
collector, so demo and live runs share the whole engine/UI pipeline.

A given seed always produces the identical event sequence (testable).
"""

from __future__ import annotations

import math
import random

from .model import ProcessSnapshot, SystemSample

SIM_INTERVAL = 0.2  # one sim tick = 0.2 s (5 Hz)
BTIME = 1_700_000_000


class DemoProcess:
    __slots__ = (
        "pid", "comm", "ppid", "state", "starttime", "utime", "stime",
        "rss", "vsize", "threads", "cpu_target", "rss_growth", "die_vt",
        "zombie",
    )

    def __init__(self, pid, comm, ppid, starttime, rss, cpu=0.2, state="S", zombie=False):
        self.pid = pid
        self.comm = comm
        self.ppid = ppid
        self.state = state
        self.starttime = starttime
        self.utime = 0
        self.stime = 0
        self.rss = rss
        self.vsize = rss * 3
        self.threads = 1
        self.cpu_target = cpu
        self.rss_growth = 0.0   # bytes per second
        self.die_vt = None      # virtual time of scheduled death
        self.zombie = zombie


class DemoCollector:
    """Collector-protocol simulator: tick() → (snapshots, system sample)."""

    def __init__(self, seed: int = 1, interval: float = SIM_INTERVAL, clk_tck: int = 100, ncores: int = 8):
        self.seed = seed
        self.interval = interval
        self.clk_tck = clk_tck
        self.ncores = ncores
        self.rng = random.Random(seed)
        self.sim_time = 0.0
        self.tick_count = 0
        self._cum_total = 0
        self._cum_busy = 0
        self.processes: dict[int, DemoProcess] = {}
        self._next_pid = 1000
        self._schedule: list[tuple[float, callable]] = []
        self._last_utime_stime: dict[int, tuple[int, int]] = {}
        self._build_population()

    # --- population & script --------------------------------------------------

    def _build_population(self):
        rng = self.rng

        def svc(pid, comm, ppid, rss_mb, cpu=0.2):
            self.processes[pid] = DemoProcess(pid, comm, ppid, 200 + pid * 13, rss_mb * 1024 * 1024, cpu)

        svc(1, "systemd", 0, 60, 0.3)
        svc(2, "kthreadd", 0, 0, 0)
        svc(3, "kworker/0:0", 2, 1, 0)
        svc(4, "kworker/0:1", 2, 1, 0)
        svc(10, "dbus-daemon", 1, 12, 0.4)
        svc(11, "sshd", 1, 8, 0.2)
        svc(12, "pipewire", 1, 30, 0.7)
        svc(13, "NetworkManager", 1, 25, 0.5)
        svc(20, "chronyd", 1, 5, 0.1)
        svc(21, "cron", 1, 4, 0.1)
        svc(100, "systemd --user", 1, 20, 0.2)
        svc(101, "gnome-shell", 100, 420, 2.5)
        svc(102, "firefox", 100, 980, 3.0)
        svc(110, "renderer", 102, 210, 8.0)
        svc(111, "gpu", 102, 150, 4.0)
        svc(112, "tab", 102, 85, 1.5)
        svc(120, "code", 100, 500, 1.0)
        svc(121, "pty-host", 120, 35, 0.8)
        svc(130, "bash", 100, 12, 0.2)
        svc(200, "postgres", 1, 720, 0.8)
        svc(210, "worker", 200, 60, 1.5)
        svc(211, "worker", 200, 55, 1.2)
        svc(250, "snapd", 1, 90, 0.5)
        svc(251, "bluetoothd", 1, 18, 0.2)
        svc(252, "cupsd", 1, 25, 0.2)

        # scripted events ------------------------------------------------------
        def spawn(vt, comm, parent_pid, rss_mb, cpu, life=None):
            pid_holder: list[int | None] = [None]

            def action():
                pid = self._next_pid
                self._next_pid += 1
                parent_pid_resolved = parent_pid[0] if isinstance(parent_pid, list) else parent_pid
                parent = self.processes.get(parent_pid_resolved)
                if parent is None:
                    return
                proc = DemoProcess(pid, comm, parent_pid_resolved, int(vt * self.clk_tck), int(rss_mb * 1024 * 1024), cpu)
                if life is not None:
                    proc.die_vt = vt + life
                self.processes[pid] = proc
                pid_holder[0] = pid

            self._schedule.append((vt, action))
            return pid_holder

        def kill(vt, pid):
            self._schedule.append((vt, lambda: self.processes.pop(pid, None)))

        def set_cpu(vt, pid, target):
            self._schedule.append((vt, lambda: self._set_cpu_target(pid, target)))

        def set_growth(vt, pid, rate):
            self._schedule.append((vt, lambda: self._set_rss_growth(pid, rate)))

        # memory growth
        for vt, pid, rate in ((0, 120, 0.8), (0, 200, 1.2), (20, 102, 0.6), (30, 101, 0.4)):
            set_growth(vt, pid, rate * 1024 * 1024)

        # compile burst: make + cc1 workers with CPU spikes
        make = spawn(10.0, "make", 130, 18, 5.0, life=8.0)
        for i, (vt, cpu) in enumerate(((10.5, 70), (10.8, 90), (11.2, 55), (11.6, 85))):
            spawn(vt, "cc1", make, 60 + i * 20, cpu, life=17.8 - vt)
        set_cpu(10.0, 130, 15.0)

        # cron job churn: repeated short-lived jobs
        t = 5.0
        while t < 55.0:
            spawn(t, "job", 21, 6, rng.uniform(4, 18), life=rng.uniform(2, 4.5))
            t += rng.uniform(5, 9)

        # tab churn under the browser
        t = 15.0
        while t < 50.0:
            spawn(t, "subframe", 112, rng.uniform(20, 60), rng.uniform(2, 12), life=rng.uniform(3, 7))
            t += rng.uniform(4, 8)

        # random blips under gnome-shell and code
        t = 8.0
        while t < 55.0:
            parent = 101 if rng.random() < 0.5 else 120
            spawn(t, rng.choice(["gjs", "task", "helper", "loader"]), parent, rng.uniform(15, 70), rng.uniform(1, 9), life=rng.uniform(2, 6))
            t += rng.uniform(3, 7)

        # cpu spike wave on the browser
        for vt, pid, target in ((22, 110, 65), (24, 110, 30), (26, 102, 45), (28, 101, 20)):
            set_cpu(vt, pid, target)

        # firefox crash episode: browser + children die → orphans → respawn
        for vt, pid in ((58.0, 102), (58.2, 110), (58.4, 111), (58.6, 112)):
            kill(vt, pid)

        def respawn_firefox():
            pid = self._next_pid
            self._next_pid += 1
            proc = DemoProcess(pid, "firefox", 100, int(62.0 * self.clk_tck), int(940 * 1024 * 1024), 3.0)
            self.processes[pid] = proc
            self._firefox_new = pid

        self._schedule.append((62.0, respawn_firefox))

        def respawn_renderer():
            parent = getattr(self, "_firefox_new", None)
            if parent is None:
                return
            pid = self._next_pid
            self._next_pid += 1
            proc = DemoProcess(pid, "renderer", parent, int(63.0 * self.clk_tck), int(190 * 1024 * 1024), 7.0)
            self.processes[pid] = proc

        self._schedule.append((63.0, respawn_renderer))

        # a zombie that lingers after its parent is gone
        def spawn_zombie():
            pid = self._next_pid
            self._next_pid += 1
            proc = DemoProcess(pid, "defunct", 21, int(25.0 * self.clk_tck), 0, 0.0, state="Z", zombie=True)
            self.processes[pid] = proc

        self._schedule.append((25.0, spawn_zombie))

        self._schedule.sort(key=lambda item: item[0])

    def _set_cpu_target(self, pid, target):
        proc = self.processes.get(pid)
        if proc is not None:
            proc.cpu_target = target

    def _set_rss_growth(self, pid, rate):
        proc = self.processes.get(pid)
        if proc is not None:
            proc.rss_growth = rate

    # --- tick -----------------------------------------------------------------

    def tick(self) -> tuple[list[ProcessSnapshot], SystemSample]:
        vt = self.sim_time
        while self._schedule and self._schedule[0][0] <= vt:
            _, action = self._schedule.pop(0)
            action()
        # scheduled deaths
        for pid in list(self.processes):
            proc = self.processes[pid]
            if proc.die_vt is not None and vt >= proc.die_vt:
                self.processes.pop(pid, None)

        # advance cpu ticks + memory
        delta_ticks_total = self.clk_tck * self.ncores * self.interval
        for proc in self.processes.values():
            if proc.zombie:
                proc.state = "Z"
                continue
            frac = proc.cpu_target / 100.0
            delta = delta_ticks_total * frac
            proc.utime += int(delta * 0.85)
            proc.stime += int(delta * 0.15)
            if proc.rss_growth:
                proc.rss += int(proc.rss_growth * self.interval)
                proc.vsize = proc.rss * 3
            proc.state = "R" if proc.cpu_target >= 1.0 else "S"

        snapshots = [self._snapshot(p) for p in sorted(self.processes.values(), key=lambda p: p.pid)]
        sample = self._system_sample()
        self.sim_time += self.interval
        self.tick_count += 1
        return snapshots, sample

    def _snapshot(self, proc: DemoProcess) -> ProcessSnapshot:
        return ProcessSnapshot(
            pid=proc.pid,
            ppid=proc.ppid,
            comm=proc.comm,
            state=proc.state,
            utime=proc.utime,
            stime=proc.stime,
            num_threads=proc.threads,
            starttime=proc.starttime,
            vsize=proc.vsize,
            rss=proc.rss,
        )

    def _system_sample(self) -> SystemSample:
        vt = self.sim_time
        base = 12.0 + 8.0 * math.sin(vt / 6.0) + self.rng.uniform(-2, 2)
        if 10.0 <= vt <= 18.0:
            base += 55.0
        cpu = max(2.0, min(98.0, base))
        delta_total = int(self.clk_tck * self.ncores * self.interval)
        self._cum_total += delta_total
        self._cum_busy += int(delta_total * cpu / 100.0)
        total_mem = 16 * 1024 ** 3
        used = int((8.2 + 0.05 * vt) * 1024 ** 3)
        return SystemSample(
            cpu_busy_ticks=self._cum_busy,
            cpu_total_ticks=self._cum_total,
            mem_total=total_mem,
            mem_available=total_mem - used,
            load1=round(cpu / 100.0 * 2.0, 2) + self.rng.uniform(-0.1, 0.1),
            btime=BTIME,
        )

    def run(self, ticks: int):
        """Advance N ticks, returning (snapshots, samples) history for tests."""
        out = []
        for _ in range(ticks):
            out.append(self.tick())
        return out
