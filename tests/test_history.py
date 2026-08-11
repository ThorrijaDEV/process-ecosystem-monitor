"""Tests for the history ring, events, and export formats."""

from __future__ import annotations

import json

import pytest

from process_ecosystem.history import Event, History, TickEntry


def entry(t: float = 0.0, events: list[Event] | None = None) -> TickEntry:
    return TickEntry(tick_time=t, cpu_pct=10.0, ram_pct=50.0, births=1, deaths=0, alive=5, events=events or [])


class TestHistoryRing:
    def test_push_and_len(self):
        h = History(capacity=16)
        h.push(entry())
        h.push(entry())
        assert len(h) == 2

    def test_capacity_bounded(self):
        h = History(capacity=20)
        for i in range(50):
            h.push(entry(t=i))
        assert len(h) == 20
        assert [e.tick_time for e in h.entries] == list(range(30, 50))

    def test_capacity_floor(self):
        h = History(capacity=1)
        assert h.capacity >= 16

    def test_recent(self):
        h = History(capacity=16)
        for i in range(10):
            h.push(entry(t=i))
        assert [e.tick_time for e in h.recent(3)] == [7, 8, 9]
        assert [e.tick_time for e in h.recent(99)] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]

    def test_clear(self):
        h = History(capacity=16)
        h.push(entry())
        h.clear()
        assert len(h) == 0


class TestExport:
    def test_to_json_round_trip(self):
        h = History(capacity=16)
        h.push(entry(t=1.5, events=[Event("birth", 42, "bash", 1.5)]))
        h.push(entry(t=2.0))
        data = json.loads(h.to_json())
        assert len(data) == 2
        assert data[0]["t"] == 1.5
        assert data[0]["births"] == 1
        assert data[0]["events"][0] == {"kind": "birth", "pid": 42, "comm": "bash", "t": 1.5}
        assert data[1]["events"] == []

    def test_to_csv_header_and_rows(self):
        h = History(capacity=16)
        h.push(entry(t=1.5, events=[Event("birth", 42, "bash", 1.5)]))
        csv_text = h.to_csv()
        lines = csv_text.splitlines()
        assert lines[0] == "t,cpu_pct,ram_pct,births,deaths,alive"
        assert lines[1].startswith("1.5,")
        assert "1.5" in lines[1]

    def test_empty_export(self):
        h = History(capacity=16)
        assert json.loads(h.to_json()) == []
        assert h.to_csv().splitlines() == ["t,cpu_pct,ram_pct,births,deaths,alive"]


class TestEvent:
    def test_fields(self):
        ev = Event("death", 7, "cc1", 123.0, detail="parent")
        assert ev.kind == "death"
        assert ev.pid == 7
        assert ev.detail == "parent"

    def test_tick_entry_defaults(self):
        e = TickEntry(tick_time=1.0, cpu_pct=0.0, ram_pct=0.0)
        assert e.births == 0
        assert e.deaths == 0
        assert e.alive == 0
        assert e.events == []
