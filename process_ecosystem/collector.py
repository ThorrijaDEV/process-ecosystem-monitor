"""Linux /proc collector.

Pure parsing functions (fixture-testable) + a ProcCollector that walks
/proc once per tick. All reads are defensive: a vanished process, a
permission error or a malformed file degrades that one process, never the
sweep. Runs entirely unprivileged (see DESIGN.md §16).

The collector can be pointed at a fake /proc root for tests:
    ProcCollector(ProcReader(proc_root=tmpdir))
"""

from __future__ import annotations

import os

from .cpu import load_from_loadavg, parse_system_stat
from .model import ProcessSnapshot, SystemSample

MAX_CMDLINE = 256  # inspector cmdline truncation


# --- pure parsers -----------------------------------------------------------


def parse_stat(text: str) -> tuple[dict, int]:
    """Parse /proc/[pid]/stat.

    Returns (fields, consumed): fields is a dict with keys pid, comm, state,
    ppid, utime, stime, num_threads, starttime, vsize, rss_pages. The comm
    is everything between the first '(' (the kernel opens comm right after
    the pid; pids are digits-only) and the LAST ')' in the line — the real
    closing paren, because state and numeric fields never contain one.
    Spaces and parens inside comm are tolerated.
    """
    stripped = text.rstrip()
    close = stripped.rfind(")")
    if close < 0:
        raise ValueError("stat has no closing paren")
    openp = stripped.find("(", 0, close)
    if openp < 0:
        raise ValueError("stat has no opening paren")
    head = stripped[:openp]
    tail = stripped[close + 1 :].split()
    try:
        pid = int(head.split()[0])
    except (IndexError, ValueError):
        raise ValueError("stat has no pid") from None
    comm = stripped[openp + 1 : close]
    if len(tail) < 22:
        raise ValueError("stat too short")
    fields = {
        "pid": pid,
        "comm": comm,
        "state": tail[0],
        "ppid": int(tail[1]),
        "utime": int(tail[11]),
        "stime": int(tail[12]),
        "num_threads": int(tail[17]),
        "starttime": int(tail[19]),
        "vsize": int(tail[20]),
        "rss_pages": int(tail[21]),
    }
    return fields, pid


def parse_statm(text: str) -> tuple[int, int]:
    """(resident_pages, size_pages) from /proc/[pid]/statm."""
    parts = text.split()
    if len(parts) < 2:
        raise ValueError("statm too short")
    return int(parts[1]), int(parts[0])


def parse_meminfo(text: str) -> dict[str, int]:
    """kB values from /proc/meminfo → bytes."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if not rest:
            continue
        parts = rest.split()
        if not parts:
            continue
        try:
            out[key] = int(parts[0]) * 1024
        except ValueError:
            pass
    return out


def parse_status_value(text: str, key: str) -> str | None:
    """Value of one 'Key: value' line in /proc/[pid]/status, or None."""
    prefix = key + ":"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


# --- proc reader / collector -------------------------------------------------


class ProcReader:
    """Thin I/O wrapper around a proc root (real /proc or a fixture)."""

    def __init__(
        self,
        proc_root: str = "/proc",
        clk_tck: int = 100,
        page_size: int = 4096,
        ncores: int = 1,
    ):
        self.root = proc_root
        self.clk_tck = clk_tck
        self.page_size = page_size
        self.ncores = ncores

    def read(self, pid: int, name: str) -> str | None:
        try:
            with open(os.path.join(self.root, str(pid), name), "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            return None

    def read_system(self, name: str) -> str | None:
        try:
            with open(os.path.join(self.root, name), "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except (FileNotFoundError, PermissionError, OSError):
            return None

    def list_pids(self) -> list[int]:
        try:
            entries = os.listdir(self.root)
        except OSError:
            return []
        return sorted(int(e) for e in entries if e.isdigit())


class ProcCollector:
    """Collects one snapshot of the process ecosystem per tick."""

    def __init__(self, reader: ProcReader):
        self.reader = reader

    @property
    def finished(self) -> bool:
        """Live collection never finishes on its own (matches demo horizon)."""
        return False

    def read_process(self, pid: int) -> ProcessSnapshot | None:
        """One process; None when the pid vanished or is unreadable."""
        stat_text = self.reader.read(pid, "stat")
        if stat_text is None:
            return None
        try:
            fields, _ = parse_stat(stat_text)
        except ValueError:
            return None
        statm = self.reader.read(pid, "statm")
        rss_pages = fields["rss_pages"]
        vsize_bytes = fields["vsize"]  # stat reports vsize in bytes already
        if statm is not None:
            try:
                rss_pages, size_pages = parse_statm(statm)
                vsize_bytes = size_pages * self.reader.page_size
            except ValueError:
                rss_pages = fields["rss_pages"]
        return ProcessSnapshot(
            pid=fields["pid"],
            ppid=fields["ppid"],
            comm=fields["comm"],
            state=fields["state"],
            utime=fields["utime"],
            stime=fields["stime"],
            num_threads=fields["num_threads"],
            starttime=fields["starttime"],
            vsize=vsize_bytes,
            rss=rss_pages * self.reader.page_size,
        )

    def read_system(self) -> SystemSample | None:
        stat = self.reader.read_system("stat")
        meminfo = self.reader.read_system("meminfo")
        loadavg = self.reader.read_system("loadavg")
        if stat is None or meminfo is None or loadavg is None:
            return None
        cpus, btime = parse_system_stat(stat)
        agg = cpus.get(-1)
        mem = parse_meminfo(meminfo)
        total = mem.get("MemTotal", 0)
        available = mem.get("MemAvailable", total)
        return SystemSample(
            cpu_busy_ticks=0,
            cpu_total_ticks=0,
            mem_total=total,
            mem_available=available,
            load1=load_from_loadavg(loadavg),
            btime=btime,
        )

    def tick(self) -> tuple[list[ProcessSnapshot], SystemSample | None]:
        """Full sweep: list of process snapshots + system sample.

        The SystemSample carries the RAW /proc/stat aggregate row so the
        engine can compute deltas; we store it in the sample below.
        """
        snapshots: list[ProcessSnapshot] = []
        for pid in self.reader.list_pids():
            snap = self.read_process(pid)
            if snap is not None:
                snapshots.append(snap)
        sample = self.read_system()
        if sample is not None:
            stat_text = self.reader.read_system("stat")
            if stat_text:
                cpus, _ = parse_system_stat(stat_text)
                agg = cpus.get(-1)
                if agg:
                    sample.cpu_busy_ticks = sum(agg[:10]) - (agg[3] + agg[4])
                    sample.cpu_total_ticks = sum(agg[:10])
        return snapshots, sample

    # --- inspector helpers (lazy, on demand) --------------------------------

    def read_cmdline(self, pid: int) -> str | None:
        text = self.reader.read(pid, "cmdline")
        if text is None:
            return None
        return text.replace("\0", " ").strip()[:MAX_CMDLINE] or None

    def read_status(self, pid: int) -> dict[str, str]:
        """Selected /proc/[pid]/status fields for the inspector."""
        text = self.reader.read(pid, "status")
        out: dict[str, str] = {}
        if text is None:
            return out
        for key in ("Name", "State", "Threads", "VmRSS", "VmSize", "Uid", "Gid", "Tgid"):
            value = parse_status_value(text, key)
            if value is not None:
                out[key] = value
        return out
