"""Tests for the /proc parsers and collector (fixture-driven)."""

from __future__ import annotations

import pytest

from process_ecosystem.collector import (
    ProcCollector,
    ProcReader,
    parse_meminfo,
    parse_stat,
    parse_status_value,
    parse_statm,
)
from tests.conftest import stat_line


class TestParseStat:
    def test_normal(self):
        fields, pid = parse_stat(stat_line())
        assert fields == {
            "pid": 1,
            "comm": "systemd",
            "state": "S",
            "ppid": 0,
            "utime": 100,
            "stime": 50,
            "num_threads": 1,
            "starttime": 500,
            "vsize": 180 << 20,
            "rss_pages": 15360,
        }
        assert pid == 1

    def test_comm_with_spaces_and_parens(self):
        text = stat_line(pid=42, comm="gnome-shell --x (wayland)")
        fields, _ = parse_stat(text)
        assert fields["pid"] == 42
        assert fields["comm"] == "gnome-shell --x (wayland)"

    def test_comm_with_paren_char(self):
        text = "7 (foo(bar) x) S 0 1 1 0 -1 100 0 0 0 10 10 0 0 20 0 1 0 500 100 10 1 0 0 0 0 0 0 0 0 0 0 0 17 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
        fields, _ = parse_stat(text)
        assert fields["comm"] == "foo(bar) x"

    def test_no_closing_paren_raises(self):
        with pytest.raises(ValueError):
            parse_stat("1 (systemd")

    def test_too_short_raises(self):
        with pytest.raises(ValueError):
            parse_stat("1 (x) S 0 1")

    def test_no_pid_raises(self):
        with pytest.raises(ValueError):
            parse_stat("(x) S 0 1 1 0 -1 100 0 0 0 10 10 0 0 20 0 1 0 500 100 10")

    def test_negative_fields(self):
        fields, _ = parse_stat(stat_line(utime=-1, stime=-2))
        assert fields["utime"] == -1
        assert fields["stime"] == -2


class TestParseStatm:
    def test_resident_and_size(self):
        # statm layout: size resident ... → returns (resident, size)
        assert parse_statm("45000 15360 100 50 0 0 0\n") == (15360, 45000)

    def test_too_short_raises(self):
        with pytest.raises(ValueError):
            parse_statm("15360")


class TestParseMeminfo:
    def test_kb_to_bytes(self):
        out = parse_meminfo("MemTotal:       16777216 kB\nMemFree:         1024 kB\n")
        assert out["MemTotal"] == 16777216 * 1024
        assert out["MemFree"] == 1024 * 1024

    def test_missing_colon_skipped(self):
        assert parse_meminfo("not a key value line\n") == {}

    def test_non_numeric_value_skipped(self):
        out = parse_meminfo("MemTotal:  16777216 kB\nWeird:  n/a\n")
        assert "Weird" not in out
        assert out["MemTotal"] == 16777216 * 1024


class TestParseStatusValue:
    def test_found(self):
        text = "Name:\tsystemd\nState:\tS (sleeping)\nThreads:\t1\n"
        assert parse_status_value(text, "Name") == "systemd"
        assert parse_status_value(text, "Threads") == "1"

    def test_missing(self):
        assert parse_status_value("Name:\tsystemd\n", "State") is None

    def test_prefix_not_matched(self):
        text = "Name:\tsystemd\nName2:\tx\n"
        assert parse_status_value(text, "Name") == "systemd"


class TestProcReader:
    def test_read_missing_returns_none(self, tmp_path):
        r = ProcReader(str(tmp_path))
        assert r.read(999, "stat") is None

    def test_read_system_missing_returns_none(self, tmp_path):
        r = ProcReader(str(tmp_path))
        assert r.read_system("stat") is None

    def test_list_pids_only_digits(self, tmp_path):
        (tmp_path / "12").mkdir()
        (tmp_path / "abc").mkdir()
        (tmp_path / "1x").mkdir()
        r = ProcReader(str(tmp_path))
        assert r.list_pids() == [12]

    def test_list_pids_missing_root(self, tmp_path):
        r = ProcReader(str(tmp_path / "nope"))
        assert r.list_pids() == []


class TestProcCollector:
    def test_read_process(self, fake_proc):
        fake_proc._add_proc(
            1,
            stat_line(rss=15360, vsize=180 << 20),
            statm="45000 15360 100 50 0 0 0\n",
            cmdline="systemd\x00--user\x00",
            status="Name:\tsystemd\nState:\tS (sleeping)\n",
        )
        snap = fake_proc.read_process(1)
        assert snap.pid == 1
        assert snap.comm == "systemd"
        assert snap.state == "S"
        assert snap.utime == 100
        assert snap.starttime == 500
        assert snap.rss == 15360 * 4096
        assert snap.vsize == 45000 * 4096

    def test_read_process_missing(self, fake_proc):
        assert fake_proc.read_process(404) is None

    def test_read_process_bad_stat_returns_none(self, fake_proc):
        fake_proc._add_proc(1, "garbage without parens\n")
        assert fake_proc.read_process(1) is None

    def test_read_process_missing_statm_falls_back_to_stat(self, fake_proc):
        fake_proc._add_proc(1, stat_line(rss=15360, vsize=180 << 20))
        snap = fake_proc.read_process(1)
        assert snap.rss == 15360 * 4096
        assert snap.vsize == 180 << 20

    def test_read_cmdline_joins_nulls(self, fake_proc):
        fake_proc._add_proc(1, stat_line(), cmdline="foo\x00bar\x00baz\x00")
        assert fake_proc.read_cmdline(1) == "foo bar baz"

    def test_read_cmdline_empty_returns_none(self, fake_proc):
        fake_proc._add_proc(1, stat_line(), cmdline="")
        assert fake_proc.read_cmdline(1) is None

    def test_read_cmdline_truncated(self, fake_proc):
        fake_proc._add_proc(1, stat_line(), cmdline="a\x00" * 300)
        assert len(fake_proc.read_cmdline(1)) <= 256

    def test_read_status_fields(self, fake_proc):
        fake_proc._add_proc(
            1,
            stat_line(),
            status="Name:\tsystemd\nState:\tS (sleeping)\nThreads:\t1\nUid:\t0 0 0 0\n",
        )
        out = fake_proc.read_status(1)
        assert out["Name"] == "systemd"
        assert out["Threads"] == "1"
        assert "VmRSS" not in out

    def test_tick_sweep(self, fake_proc):
        fake_proc._add_proc(1, stat_line(pid=1, ppid=0))
        fake_proc._add_proc(100, stat_line(pid=100, ppid=1, comm="bash"))
        snaps, sample = fake_proc.tick()
        assert sorted(s.pid for s in snaps) == [1, 100]
        assert sample.mem_total == 16777216 * 1024
        assert sample.btime == 1700000000
        assert sample.load1 == 0.41
        assert sample.cpu_total_ticks == 1665  # 1000+100+200+300+50+10+5
        assert sample.cpu_busy_ticks == 1315  # total - idle(300+50)
        assert sample.cpu_busy_ticks == 1315  # total - idle(300+50)

    def test_tick_missing_system_files(self, tmp_path):
        reader = ProcReader(str(tmp_path))
        collector = ProcCollector(reader)
        snaps, sample = collector.tick()
        assert snaps == []
        assert sample is None

    def test_pid_order_deterministic(self, fake_proc):
        for pid in (50, 3, 200, 7):
            fake_proc._add_proc(pid, stat_line(pid=pid))
        pids = [s.pid for s in fake_proc.tick()[0]]
        assert pids == sorted(pids)
