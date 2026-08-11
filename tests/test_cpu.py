"""Tests for CPU accounting math (pure functions in cpu.py)."""

from __future__ import annotations

import pytest

from process_ecosystem.cpu import (
    load_from_loadavg,
    normalize_ticks,
    parse_system_stat,
    process_cpu_pct,
    system_busy_delta,
    system_cpu_pct,
)


class TestParseSystemStat:
    def test_parses_aggregate_and_cores(self):
        text = "cpu  1000 200 300 4000 100 0 10 5 0 0\ncpu0 100 20 30 400 10 0 1 0 0 0\nbtime 1700000000\n"
        cpus, btime = parse_system_stat(text)
        assert cpus[-1] == [1000, 200, 300, 4000, 100, 0, 10, 5, 0, 0]
        assert cpus[0] == [100, 20, 30, 400, 10, 0, 1, 0, 0, 0]
        assert btime == 1700000000

    def test_aggregate_key_does_not_collide_with_core_zero(self):
        cpus, _ = parse_system_stat("cpu  1 2 3 4 5 6 7 8 9 10\ncpu0 9 9 9 9 9 9 9 9 9 9\n")
        assert cpus[-1] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        assert cpus[0] == [9] * 10

    def test_aggregate(self):
        cpus, btime = parse_system_stat("cpu  1000 200 300 4000 100 0 10 5 0 0\n")
        assert cpus[-1] == [1000, 200, 300, 4000, 100, 0, 10, 5, 0, 0]

    def test_missing_btime_defaults_zero(self):
        cpus, btime = parse_system_stat("cpu  1 0 0 0 0 0 0 0 0 0\n")
        assert btime == 0

    def test_empty_text(self):
        cpus, btime = parse_system_stat("")
        assert cpus == {}
        assert btime == 0

    def test_ignores_non_cpu_lines(self):
        cpus, _ = parse_system_stat("intr 123\nctxt 456\ncpu  1 0 0 0 0 0 0 0 0 0\n")
        assert -1 in cpus

    def test_garbage_lines_ignored(self):
        cpus, _ = parse_system_stat("cpu  1 0 0 0 0 0 0 0 0 0\ncpuX 1\ncpuabc\n")
        assert len(cpus) == 1


class TestSystemBusyDelta:
    def test_basic_delta(self):
        prev = [100, 20, 30, 400, 10, 0, 0, 0, 0, 0]
        cur = [110, 25, 35, 450, 20, 0, 0, 0, 0, 0]
        busy, total = system_busy_delta(prev, cur)
        assert total == 80
        assert busy == 20  # deltas of non-idle fields only

    def test_idle_includes_iowait(self):
        prev = [100, 0, 0, 400, 10, 0, 0, 0, 0, 0]
        cur = [110, 0, 0, 410, 20, 0, 0, 0, 0, 0]
        _, total = system_busy_delta(prev, cur)
        assert total == 30

    def test_none_inputs(self):
        assert system_busy_delta(None, [1, 2, 3, 4, 5, 0, 0, 0, 0, 0]) == (0, 0)
        assert system_busy_delta([1, 2, 3, 4, 5, 0, 0, 0, 0, 0], None) == (0, 0)
        assert system_busy_delta(None, None) == (0, 0)

    def test_short_rows(self):
        assert system_busy_delta([1, 2, 3], [4, 5, 6]) == (0, 0)

    def test_counter_reset_yields_zero(self):
        prev = [200, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        cur = [10, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        assert system_busy_delta(prev, cur) == (0, 0)


class TestSystemCpuPct:
    def test_percent(self):
        assert system_cpu_pct(50, 100) == 50.0

    def test_full_busy(self):
        assert system_cpu_pct(100, 100) == 100.0

    def test_zero_total(self):
        assert system_cpu_pct(5, 0) == 0.0

    def test_clamped(self):
        assert system_cpu_pct(120, 100) == 100.0


class TestProcessCpuPct:
    def test_normalization_per_core(self):
        # 100 jiffies over a 1 s window on 1 core = 100%
        assert process_cpu_pct(0, 100, 1.0, 100, 1) == 100.0
        # same on 4 cores = 25%
        assert process_cpu_pct(0, 100, 1.0, 100, 4) == 25.0

    def test_math_formula(self):
        # delta 50 jiffies, 0.5 s window, 100 Hz, 2 cores
        assert abs(process_cpu_pct(100, 150, 0.5, 100, 2) - 50.0) < 1e-9

    def test_first_sample_zero(self):
        assert process_cpu_pct(None, 100, 0.2, 100, 1) == 0.0

    def test_counter_reset_returns_zero(self):
        assert process_cpu_pct(200, 100, 0.2, 100, 1) == 0.0

    def test_bad_args(self):
        assert process_cpu_pct(0, 100, 0.0, 100, 1) == 0.0
        assert process_cpu_pct(0, 100, 0.2, 0, 1) == 0.0
        assert process_cpu_pct(0, 100, 0.2, 100, 0) == 0.0


class TestMisc:
    def test_normalize_ticks_sums(self):
        assert normalize_ticks(100, 50) == 150

    def test_normalize_ticks_negative_clamped(self):
        assert normalize_ticks(-5, 50) == 50
        assert normalize_ticks(-5, -5) == 0

    def test_loadavg(self):
        assert load_from_loadavg("0.41 0.30 0.25 2/345 67890\n") == 0.41

    def test_loadavg_garbage(self):
        assert load_from_loadavg("garbage") == 0.0
        assert load_from_loadavg("") == 0.0
