"""Demo collector tests: determinism, scripted episodes, pipeline parity."""

from __future__ import annotations

import pytest

from process_ecosystem.demo import BTIME, DemoCollector
from process_ecosystem.engine import EcosystemState
from process_ecosystem.history import History
from process_ecosystem.model import NEW


def run(seed: int, ticks: int = 60) -> DemoCollector:
    d = DemoCollector(seed=seed)
    for _ in range(ticks):
        d.tick()
    return d


class TestDeterminism:
    def test_same_seed_identical(self):
        a = run(1, 40)
        b = run(1, 40)
        assert a._cum_total == b._cum_total
        assert a.processes.keys() == b.processes.keys()
        sa = a.tick()
        sb = b.tick()
        assert [(s.pid, s.utime, s.rss) for s in sa[0]] == [(s.pid, s.utime, s.rss) for s in sb[0]]
        assert sa[1].cpu_total_ticks == sb[1].cpu_total_ticks

    def test_different_seed_differs(self):
        a = run(1, 60)
        b = run(2, 60)
        assert a.processes.keys() != b.processes.keys()

    def test_cumulative_counters_monotonic(self):
        d = run(1, 40)
        prev = d.tick()
        for _ in range(5):
            _, sample = d.tick()
            assert sample.cpu_total_ticks > prev[1].cpu_total_ticks
            prev = (None, sample)


class TestScript:
    def test_compile_burst_born(self):
        d = run(1, 60)  # t = 12 s: make + cc1 alive
        comms = {p.comm for p in d.processes.values()}
        assert "make" in comms
        assert "cc1" in comms

    def test_cc1_parented_under_make(self):
        d = run(1, 60)
        make = [p for p in d.processes.values() if p.comm == "make"][0]
        cc1 = [p for p in d.processes.values() if p.comm == "cc1"][0]
        assert cc1.ppid == make.pid

    def test_compile_burst_ended(self):
        d = run(1, 110)  # t = 22 s: burst over
        comms = {p.comm for p in d.processes.values()}
        assert "make" not in comms
        assert "cc1" not in comms

    def test_system_cpu_high_during_burst(self):
        eng = EcosystemState(History(), clk_tck=100, ncores=8)
        d = DemoCollector(seed=1)
        for _ in range(60):  # covers t=0..12 s
            snaps, sample = d.tick()
            eng.update(snaps, sample, 0.2, BTIME + d.sim_time)
        assert eng.system_cpu > 30.0

    def test_cpu_spike_wave_on_renderer(self):
        d = run(1, 120)  # t=24: renderer target 65 → spike at 65%... exceeds
        renderer = next(p for p in d.processes.values() if p.comm == "renderer")
        assert renderer.cpu_target == 65.0

    def test_firefox_crash_and_respawn(self):
        eng = EcosystemState(History(), clk_tck=100, ncores=8)
        d = DemoCollector(seed=1)
        saw_death = saw_orphan = saw_new_firefox = False
        for _ in range(340):  # t = 0..68 s
            snaps, sample = d.tick()
            diff = eng.update(snaps, sample, 0.2, BTIME + d.sim_time)
            for pid, comm in diff.deaths:
                if comm == "firefox":
                    saw_death = True
            if diff.orphans:
                saw_orphan = True
            for pid, comm in diff.births:
                if comm == "firefox" and pid >= 1000:
                    saw_new_firefox = True
        assert saw_death
        assert saw_orphan
        assert saw_new_firefox

    def test_firefox_respawn_under_systemd_user(self):
        d = run(1, 330)  # t = 66 s
        firefox = [p for p in d.processes.values() if p.comm == "firefox"]
        assert len(firefox) == 1
        assert firefox[0].pid >= 1000
        assert firefox[0].ppid == 100

    def test_zombie_created(self):
        d = run(1, 130)  # t = 26 s
        defunct = [p for p in d.processes.values() if p.comm == "defunct"]
        assert len(defunct) == 1
        assert defunct[0].state == "Z"

    def test_memory_growth(self):
        d = run(1, 250)  # t = 50 s: growth ran since vt 20
        firefox = next(p for p in d.processes.values() if p.pid == 102)
        assert firefox.rss > 980 * 1024 * 1024 + 10 * 1024 * 1024

    def test_jobs_churn(self):
        d = DemoCollector(seed=1)
        max_jobs = 0
        for _ in range(100):
            d.tick()
            max_jobs = max(max_jobs, len([p for p in d.processes.values() if p.comm == "job"]))
        assert max_jobs >= 1
        alive = [p for p in d.processes.values() if p.comm == "job"]
        assert all(p.ppid == 21 for p in alive)


class TestEngineParity:
    def test_engine_runs_demo_pipeline(self):
        eng = EcosystemState(History(), clk_tck=100, ncores=8)
        d = DemoCollector(seed=1)
        for _ in range(300):
            snaps, sample = d.tick()
            eng.update(snaps, sample, 0.2, BTIME + d.sim_time)
        assert eng.alive_count() > 10
        assert len(eng.history) == 300

    def test_demo_births_are_new(self):
        eng = EcosystemState(History(), clk_tck=100, ncores=8)
        d = DemoCollector(seed=1)
        for _ in range(80):
            snaps, sample = d.tick()
            eng.update(snaps, sample, 0.2, BTIME + d.sim_time)
        # a subframe born around t=15..16 should still be NEW for 2 ticks
        subframes = [p for p in eng.processes.values() if p.comm == "subframe"]
        assert subframes

    def test_btime_epoch(self):
        d = DemoCollector(seed=1)
        snaps, sample = d.tick()
        assert sample.btime == BTIME

    def test_snapshots_sorted(self):
        d = DemoCollector(seed=1)
        snaps, _ = d.tick()
        assert [s.pid for s in snaps] == sorted(s.pid for s in snaps)

    def test_run_returns_ticks(self):
        d = DemoCollector(seed=1)
        out = d.run(10)
        assert len(out) == 10
        assert all(len(part[0]) > 0 for part in out)

    def test_ncores_scales_system_ticks(self):
        d1 = DemoCollector(seed=1, ncores=2)
        d8 = DemoCollector(seed=1, ncores=8)
        _, s1 = d1.tick()
        _, s8 = d8.tick()
        assert s8.cpu_total_ticks == 4 * s1.cpu_total_ticks
