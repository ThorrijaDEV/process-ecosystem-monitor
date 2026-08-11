"""Bounded in-memory history ring.

Records one summary entry per tick plus birth/death/spike events. Never
written to disk automatically; `export` subcommand dumps it on demand.
RAM-only, bounded by capacity — nothing grows unbounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Event:
    kind: str                 # "birth" | "death" | "spike" | "orphan"
    pid: int
    comm: str
    tick_time: float
    detail: str = ""          # e.g. parent comm for births


@dataclass
class TickEntry:
    tick_time: float
    cpu_pct: float
    ram_pct: float
    births: int = 0
    deaths: int = 0
    alive: int = 0
    events: list[Event] = field(default_factory=list)


class History:
    def __init__(self, capacity: int = 3000):
        self.capacity = max(16, capacity)
        self.entries: list[TickEntry] = []

    def push(self, entry: TickEntry) -> None:
        self.entries.append(entry)
        if len(self.entries) > self.capacity:
            del self.entries[: len(self.entries) - self.capacity]

    def clear(self) -> None:
        self.entries.clear()

    def __len__(self) -> int:
        return len(self.entries)

    def recent(self, n: int) -> list[TickEntry]:
        return self.entries[-n:]

    # --- export (user-initiated only) --------------------------------------

    def to_json(self) -> str:
        import json

        out = []
        for e in self.entries:
            out.append(
                {
                    "t": round(e.tick_time, 3),
                    "cpu": round(e.cpu_pct, 2),
                    "ram": round(e.ram_pct, 2),
                    "births": e.births,
                    "deaths": e.deaths,
                    "alive": e.alive,
                    "events": [
                        {"kind": ev.kind, "pid": ev.pid, "comm": ev.comm, "t": round(ev.tick_time, 3)}
                        for ev in e.events
                    ],
                }
            )
        return json.dumps(out, indent=1)

    def to_csv(self) -> str:
        import csv
        import io

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["t", "cpu_pct", "ram_pct", "births", "deaths", "alive"])
        for e in self.entries:
            w.writerow([round(e.tick_time, 3), round(e.cpu_pct, 2), round(e.ram_pct, 2), e.births, e.deaths, e.alive])
        return buf.getvalue()
