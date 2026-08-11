"""Model tests: data classes, constants, lifecycle vocabulary."""

from __future__ import annotations

import pytest

from process_ecosystem.model import (
    ACTIVE,
    DEAD,
    DYING,
    DYING_TICKS,
    IDLE,
    IDLE_CPU_THRESHOLD,
    LIFECYCLE_ORDER,
    NEW,
    NEW_GRACE_TICKS,
    SPIKE_CPU_THRESHOLD,
    ZOMBIE,
    ProcessSnapshot,
    ProcessState,
    SystemSample,
    identity_from_snapshot,
)


class TestConstants:
    def test_lifecycle_order(self):
        assert LIFECYCLE_ORDER == (NEW, ACTIVE, IDLE, DYING, DEAD)

    def test_thresholds(self):
        assert NEW_GRACE_TICKS == 2
        assert DYING_TICKS == 5
        assert IDLE_CPU_THRESHOLD == 0.5
        assert SPIKE_CPU_THRESHOLD == 50.0
        assert ZOMBIE == "Z"


class TestSnapshot:
    def test_defaults(self):
        s = ProcessSnapshot(pid=1, ppid=0, comm="x", state="S", utime=1, stime=2, num_threads=1, starttime=3, vsize=4, rss=5)
        assert s.user is None

    def test_identity(self):
        s = ProcessSnapshot(pid=1, ppid=0, comm="x", state="S", utime=1, stime=2, num_threads=1, starttime=3, vsize=4, rss=5)
        assert identity_from_snapshot(s) == (1, 3)


class TestSystemSample:
    def test_mem_used(self):
        s = SystemSample(cpu_busy_ticks=1, cpu_total_ticks=2, mem_total=100, mem_available=60, load1=0.0, btime=0)
        assert s.mem_used == 40

    def test_defaults(self):
        s = SystemSample(cpu_busy_ticks=0, cpu_total_ticks=0, mem_total=0, mem_available=0, load1=0.0, btime=0)
        assert s.uptime_ticks == 0


class TestProcessState:
    def test_defaults(self):
        p = ProcessState(pid=1, starttime=2, comm="x")
        assert p.lifecycle == NEW
        assert p.state == "R"
        assert p.ppid == 0
        assert p.cpu_pct == 0.0
        assert p.rss == 0
        assert p.children == set()
        assert p.spike is False
        assert p.orphan is False

    def test_identity(self):
        p = ProcessState(pid=1, starttime=2, comm="x")
        assert p.identity() == (1, 2)

    def test_rings(self):
        p = ProcessState(pid=1, starttime=2, comm="x")
        assert p.rss_ring == []
        assert p.cpu_ring == []
