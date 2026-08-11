"""Shared test fixtures and builders."""

from __future__ import annotations

import os

import pytest

from process_ecosystem.collector import ProcCollector, ProcReader
from process_ecosystem.engine import EcosystemState
from process_ecosystem.history import History
from process_ecosystem.model import ProcessSnapshot, SystemSample


def snap(
    pid: int,
    ppid: int = 1,
    comm: str = "proc",
    state: str = "S",
    utime: int = 100,
    stime: int = 50,
    threads: int = 1,
    starttime: int = 500,
    vsize: int = 10 << 20,
    rss: int = 4 << 20,
) -> ProcessSnapshot:
    return ProcessSnapshot(
        pid=pid,
        ppid=ppid,
        comm=comm,
        state=state,
        utime=utime,
        stime=stime,
        num_threads=threads,
        starttime=starttime,
        vsize=vsize,
        rss=rss,
    )


def system(
    busy: int = 500,
    total: int = 1000,
    mem_total: int = 16 << 30,
    mem_available: int = 8 << 30,
    load1: float = 0.5,
    btime: int = 1_700_000_000,
) -> SystemSample:
    return SystemSample(
        cpu_busy_ticks=busy,
        cpu_total_ticks=total,
        mem_total=mem_total,
        mem_available=mem_available,
        load1=load1,
        btime=btime,
    )


def engine_with(
    pairs: list[tuple[int, int]],
    history: History | None = None,
    clk_tck: int = 100,
    ncores: int = 1,
) -> EcosystemState:
    """(pid, ppid) pairs → a fresh engine populated by one update tick."""
    eng = EcosystemState(history, clk_tck=clk_tck, ncores=ncores)
    saps = [snap(pid, ppid) for pid, ppid in pairs]
    eng.update(saps, system(), 0.2, 0.0)
    return eng


@pytest.fixture
def fake_proc(tmp_path):
    """A minimal fake /proc tree; returns a ProcCollector wired to it."""

    def write(pid: int, name: str, text: str) -> None:
        d = tmp_path / str(pid)
        d.mkdir(exist_ok=True)
        (d / name).write_text(text)

    root = tmp_path
    (root / "stat").write_text(
        "cpu  1000 100 200 300 50 0 10 5 0 0\n"
        "cpu0 100 10 20 30 5 0 1 0 0 0\n"
        "intr 1\n"
        "ctxt 1\n"
        "btime 1700000000\n"
        "processes 1\n"
        "procs_running 2\n"
        "procs_blocked 0\n"
        "softirq 1 0 0 0 0 0 0 0 0 0 0\n"
    )
    (root / "meminfo").write_text(
        "MemTotal:       16777216 kB\n"
        "MemFree:          102400 kB\n"
        "MemAvailable:   8388608 kB\n"
        "Buffers:            100 kB\n"
    )
    (root / "loadavg").write_text("0.41 0.30 0.25 2/345 67890\n")

    def add_proc(pid: int, stat: str, statm: str | None = None, cmdline: str | None = None, status: str | None = None):
        write(pid, "stat", stat)
        if statm is not None:
            write(pid, "statm", statm)
        if cmdline is not None:
            write(pid, "cmdline", cmdline)
        if status is not None:
            write(pid, "status", status)

    reader = ProcReader(str(root), clk_tck=100, page_size=4096, ncores=8)
    collector = ProcCollector(reader)

    collector._root = root
    collector._add_proc = add_proc
    return collector


STAT_TPL = "1 (systemd) S 0 1 1 0 -1 4194560 1234 0 0 0 {utime} {stime} 0 0 20 0 1 0 {starttime} {vsize} {rss} 18446744073709551615 1 0 0 0 0 0 0 0 0 0 0 0 17 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"


def stat_line(pid=1, comm="systemd", state="S", ppid=0, utime=100, stime=50, starttime=500, vsize=180 << 20, rss=15360, **kw):
    return (
        f"{pid} ({comm}) {state} {ppid} 1 1 0 -1 4194560 1234 0 0 0 {utime} {stime} 0 0 20 0 1 0 "
        f"{starttime} {vsize} {rss} 18446744073709551615 1 0 0 0 0 0 0 0 0 0 0 0 17 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
    )
