"""Engine tests: lifecycle, CPU deltas, tree building, orphans, pid reuse."""

from __future__ import annotations

import pytest

from process_ecosystem.engine import EcosystemState
from process_ecosystem.history import History
from process_ecosystem.model import ACTIVE, DYING, IDLE, NEW
from tests.conftest import engine_with, snap, system


def tick(eng: EcosystemState, saps, wall: float = 1.0, sys_=None):
    return eng.update(saps, sys_ if sys_ is not None else system(), 0.2, wall)


class TestBirth:
    def test_new_process_spawns_new(self):
        eng = engine_with([(1, 0)])
        proc = eng.process(1)
        assert proc is not None
        assert proc.lifecycle == NEW
        assert eng.alive_count() == 1

    def test_birth_event_in_diff_and_history(self):
        eng = engine_with([(1, 0)])
        assert eng.history.entries[-1].births == 1
        kinds = [ev.kind for ev in eng.history.entries[-1].events]
        assert kinds == ["birth"]

    def test_new_grace_ticks(self):
        eng = engine_with([(1, 0)])
        tick(eng, [snap(1, 0)], wall=1.0)
        assert eng.process(1).lifecycle == NEW
        tick(eng, [snap(1, 0)], wall=2.0)
        assert eng.process(1).lifecycle in (ACTIVE, IDLE)


class TestLifecycle:
    def test_active_when_cpu_above_threshold(self):
        eng = engine_with([(1, 0)])
        tick(eng, [snap(1, 0, utime=1000, stime=0)], wall=1.0)
        tick(eng, [snap(1, 0, utime=1100, stime=0)], wall=2.0)
        # 100 jiffies / (0.2s * 100 * 1) = 5% → ACTIVE
        assert eng.process(1).cpu_pct > 0.5
        assert eng.process(1).lifecycle == ACTIVE

    def test_idle_when_cpu_below_threshold(self):
        eng = engine_with([(1, 0)])
        tick(eng, [snap(1, 0, utime=1000, stime=0)], wall=1.0)
        tick(eng, [snap(1, 0, utime=1000, stime=0)], wall=2.0)
        # zero jiffies consumed in the window → 0.0% → IDLE
        assert eng.process(1).cpu_pct == 0.0
        assert eng.process(1).lifecycle == IDLE

    def test_first_sample_zero_cpu(self):
        eng = engine_with([(1, 0)])
        assert eng.process(1).cpu_pct == 0.0

    def test_zombie_stays_visible(self):
        eng = engine_with([(1, 0)])
        tick(eng, [snap(1, 0, state="Z")], wall=1.0)
        tick(eng, [snap(1, 0, state="Z")], wall=2.0)
        proc = eng.process(1)
        assert proc.state == "Z"
        assert proc.lifecycle != DYING


class TestDeath:
    def test_missing_process_goes_dying(self):
        eng = engine_with([(1, 0)])
        tick(eng, [], wall=1.0)
        assert eng.process(1).lifecycle == DYING

    def test_death_after_grace(self):
        eng = engine_with([(1, 0)])
        for w in (1.0, 2.0, 3.0, 4.0):
            tick(eng, [], wall=w)
        assert eng.process(1) is not None  # 4 missing ticks < 5
        tick(eng, [], wall=5.0)
        assert eng.process(1) is None
        assert eng.alive_count() == 0
    def test_death_event(self):
        eng = engine_with([(1, 0)])
        for w in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0):
            tick(eng, [], wall=w)
        kinds = [ev.kind for e in eng.history.entries for ev in e.events]
        assert "death" in kinds

    def test_reappear_during_dying_cancels_death(self):
        eng = engine_with([(1, 0)])
        tick(eng, [], wall=1.0)
        assert eng.process(1).lifecycle == DYING
        tick(eng, [snap(1, 0)], wall=2.0)
        proc = eng.process(1)
        assert proc.lifecycle in (ACTIVE, IDLE)  # back among the living
        assert proc.dying_since is None
        kinds = [ev.kind for ev in eng.history.entries[-1].events]
        assert "death" not in kinds


class TestPidReuse:
    def test_pid_reuse_evicts_and_respawns(self):
        eng = engine_with([(1, 0)])
        proc = eng.process(1)
        assert proc.starttime == 500
        tick(eng, [snap(1, 0, starttime=9999)], wall=1.0)
        new = eng.process(1)
        assert new.starttime == 9999
        kinds = [ev.kind for ev in eng.history.entries[-1].events]
        assert "death" in kinds
        assert "birth" in kinds


class TestCpuDelta:
    def test_cpu_pct_accumulates_over_ticks(self):
        eng = engine_with([(1, 0)], ncores=1)
        # cumulative counter grows by 20 jiffies per tick → 100% of one core
        tick(eng, [snap(1, 0, utime=1020, stime=0)], wall=1.0)
        tick(eng, [snap(1, 0, utime=1040, stime=0)], wall=2.0)
        assert eng.process(1).cpu_pct == pytest.approx(100.0, abs=1e-6)

    def test_cpu_ring_bounded(self):
        eng = engine_with([(1, 0)])
        for i in range(50):
            tick(eng, [snap(1, 0, utime=1000 + i, stime=0)], wall=i + 1.0)
        assert len(eng.process(1).cpu_ring) <= 30

    def test_rss_ring_bounded(self):
        eng = engine_with([(1, 0)])
        for i in range(50):
            tick(eng, [snap(1, 0, rss=4 << 20)], wall=i + 1.0)
        assert len(eng.process(1).rss_ring) <= 30


class TestSpike:
    def test_spike_detected(self):
        eng = engine_with([(1, 0)], ncores=1)
        tick(eng, [snap(1, 0, utime=1000, stime=0)], wall=1.0)
        tick(eng, [snap(1, 0, utime=2000, stime=0)], wall=2.0)
        proc = eng.process(1)
        assert proc.cpu_pct >= 50.0
        assert proc.spike is True
        # the spike event was recorded on the tick that crossed the threshold
        kinds = [ev.kind for ev in eng.history.entries[-2].events]
        assert "spike" in kinds

    def test_spike_clears_when_cpu_drops(self):
        eng = engine_with([(1, 0)], ncores=1)
        tick(eng, [snap(1, 0, utime=1000, stime=0)], wall=1.0)
        tick(eng, [snap(1, 0, utime=2000, stime=0)], wall=2.0)
        assert eng.process(1).spike
        tick(eng, [snap(1, 0, utime=2001, stime=0)], wall=3.0)
        assert eng.process(1).spike is False

    def test_counter_reset_stops_spike(self):
        eng = engine_with([(1, 0)], ncores=1)
        tick(eng, [snap(1, 0, utime=2000, stime=0)], wall=1.0)
        tick(eng, [snap(1, 0, utime=500, stime=0)], wall=2.0)
        assert eng.process(1).cpu_pct == 0.0
        assert eng.process(1).spike is False


class TestSystemMetrics:
    def test_ram_pct(self):
        eng = engine_with([(1, 0)])
        assert eng.system_ram == pytest.approx(50.0)  # 8G of 16G used

    def test_system_cpu_delta(self):
        eng = engine_with([(1, 0)])
        tick(eng, [snap(1, 0)], wall=1.0, sys_=system(busy=550, total=1500))
        assert eng.system_cpu == pytest.approx(10.0)  # 50 busy / 500 total

    def test_cpu_ignores_short_windows(self):
        eng = engine_with([(1, 0)])
        tick(eng, [snap(1, 0)], wall=1.0, sys_=system(busy=101, total=101, mem_total=1))
        # total delta 1 < ncores 1 → keep previous (0.0)

    def test_load1_and_btime(self):
        eng = engine_with([(1, 0)])
        assert eng.load1 == 0.5
        assert eng.btime == 1_700_000_000

    def test_no_system_sample(self):
        eng = engine_with([(1, 0)])
        tick(eng, [snap(1, 0)], wall=1.0, sys_=None)
        assert eng.system_cpu == 0.0


class TestTree:
    def test_children_of(self):
        eng = engine_with([(1, 0), (2, 1), (3, 1), (4, 2)])
        assert eng.children_of(1) == [2, 3]
        assert eng.children_of(2) == [4]
        assert eng.children_of(4) == []

    def test_roots(self):
        eng = engine_with([(1, 0), (2, 1)])
        assert eng.roots() == [1]

    def test_root_with_missing_parent_is_root(self):
        eng = engine_with([(1, 0), (7, 99)])
        assert eng.roots() == [1, 7]

    def test_kernel_threads(self):
        eng = engine_with([(1, 0), (2, 0), (3, 2), (4, 2)])
        assert eng.kernel_threads() == [3, 4]

    def test_orphan_when_parent_fully_removed(self):
        eng = engine_with([(1, 0), (2, 1), (3, 2)])
        for w in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0):
            tick(eng, [snap(1, 0), snap(3, 2)], wall=w)
        # pid 2 missed 5+ sweeps → evicted → pid 3 becomes an orphan
        assert eng.process(2) is None
        assert eng.process(3).orphan is True
        kinds = [ev.kind for e in eng.history.entries for ev in e.events]
        assert "orphan" in kinds

    def test_orphan_rehomed_to_nearest_ancestor(self):
        eng = engine_with([(1, 0), (2, 1), (3, 2), (4, 3)])
        for w in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0):
            tick(eng, [snap(1, 0), snap(4, 3)], wall=w)
        # pids 2 and 3 fully removed → 4 re-homes under 1
        assert eng.process(4).orphan is True
        assert 4 in eng.children_of(1)

    def test_orphan_rehomed_direct_parent_alive(self):
        eng = engine_with([(1, 0), (2, 1), (3, 2)])
        tick(eng, [snap(1, 0), snap(2, 1), snap(3, 2)], wall=1.0)
        assert eng.process(3).orphan is False
        assert 3 in eng.children_of(2)

    def test_orphan_rehome_across_two_generations(self):
        # 5 → 4 → 3 → 2 → 1; 4 and 3 die together; 5 re-homes to 2
        eng = engine_with([(1, 0), (2, 1), (3, 2), (4, 3), (5, 4)])
        for w in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0):
            tick(eng, [snap(1, 0), snap(2, 1), snap(5, 4)], wall=w)
        assert eng.process(5).orphan is True
        assert 5 in eng.children_of(2)

    def test_graveyard_bounded(self):
        eng = engine_with([(1, 0)])
        for i in range(700):
            saps = [snap(1, 0)]
            for j in range(1, 5):
                saps.append(snap(1000 + i * 4 + j, 1, comm=f"p{j}"))
            saps.append(snap(3000 + i, 1000 + i * 4, comm="leaf"))
            tick(eng, saps, wall=i + 1.0)
            saps2 = [snap(1, 0)]
            for j in range(1, 5):
                saps2.append(snap(1000 + i * 4 + j, 1, comm=f"p{j}"))
            tick(eng, saps2, wall=i + 1.5)
        assert len(eng._graveyard) <= 512

    def test_dying_parent_keeps_children_until_evicted(self):
        eng = engine_with([(1, 0), (2, 1)])
        tick(eng, [snap(1, 0)], wall=1.0)
        # pid 2 is missing → DYING but still the parent of nothing here
        assert eng.children_of(1) == [2]
        for w in (2.0, 3.0, 4.0, 5.0, 6.0):
            tick(eng, [snap(1, 0)], wall=w)
        assert eng.children_of(1) == []


class TestAge:
    def test_age_seconds(self):
        eng = engine_with([(1, 0)])
        # starttime 500 ticks / clk 100 = 5 s after boot (btime)
        assert eng.age_seconds(eng.process(1), 1_700_000_010.0) == pytest.approx(5.0)

    def test_age_without_btime(self):
        eng = engine_with([(1, 0)])
        eng.btime = 0
        assert eng.age_seconds(eng.process(1), 1.0) is None

    def test_age_never_negative(self):
        eng = engine_with([(1, 0)])
        assert eng.age_seconds(eng.process(1), 1_700_000_000.0) == 0.0


class TestResetAndHistory:
    def test_reset_clears_everything(self):
        eng = engine_with([(1, 0), (2, 1)])
        eng.update([snap(3, 1)], system(), 0.2, 1.0)
        eng.reset()
        assert eng.alive_count() == 0
        assert len(eng.history) == 0
        assert eng.tick_index == 0
        assert eng.system_cpu == 0.0

    def test_history_tick_per_update(self):
        eng = engine_with([(1, 0)])
        tick(eng, [snap(1, 0)], wall=1.0)
        assert len(eng.history) == 2
        assert eng.history.entries[-1].alive == 1

    def test_tick_index_advances(self):
        eng = engine_with([(1, 0)])
        assert eng.tick_index == 1
        tick(eng, [snap(1, 0)], wall=1.0)
        assert eng.tick_index == 2

    def test_identity_method(self):
        eng = engine_with([(1, 0)])
        assert eng.process(1).identity() == (1, 500)

    def test_comm_update_reflected(self):
        eng = engine_with([(1, 0)])
        tick(eng, [snap(1, 0, comm="renamed")], wall=1.0)
        assert eng.process(1).comm == "renamed"
