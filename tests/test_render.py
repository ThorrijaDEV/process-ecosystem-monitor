"""Render tests: glyphs, colors, frame composition (pure functions)."""

from __future__ import annotations

import pytest

from process_ecosystem.model import ProcessState
from process_ecosystem.render import (
    A_BLINK,
    A_BOLD,
    A_DIM,
    A_NORMAL,
    COL_ACTIVE,
    COL_COLLAPSED,
    COL_DYING,
    COL_IDLE,
    COL_LINK,
    COL_NEW,
    COL_ORPHAN,
    COL_SPIKE,
    COL_ZOMBIE,
    Charset,
    Cell,
    compose_ecosystem,
    node_style,
)
from process_ecosystem.layout import compute_layout, Layout, LayoutNode, LinkSeg
from tests.conftest import engine_with, snap, system


class TestCharset:
    def test_unicode(self):
        c = Charset(True)
        assert c.node == ("·", "●", "◉", "⬤")
        assert c.zombie == "✖"
        assert c.orphan == "◌"
        assert c.v == "│"
        assert c.tee == "┬"

    def test_ascii(self):
        c = Charset(False)
        assert c.node == (".", "o", "O", "@")
        assert c.zombie == "x"
        assert c.orphan == "o"
        assert c.v == "|"
        assert c.tee == "+"
        assert c.spinner == ("-", "\\", "|", "/")


class TestNodeStyle:
    def test_new_blinks_even(self):
        proc = ProcessState(pid=1, starttime=1, comm="x", lifecycle="new")
        node = LayoutNode(pid=1, row=0, col=0, depth=0, glyph_class=1)
        ch, color, attr = node_style(proc, node, Charset(True), 0)
        assert color == COL_NEW
        assert attr & A_BOLD
        assert attr & A_BLINK
        assert ch == "●"

    def test_new_no_blink_odd_tick(self):
        proc = ProcessState(pid=1, starttime=1, comm="x", lifecycle="new")
        node = LayoutNode(pid=1, row=0, col=0, depth=0, glyph_class=1)
        _, _, attr = node_style(proc, node, Charset(True), 1)
        assert not (attr & A_BLINK)

    def test_dying_red(self):
        proc = ProcessState(pid=1, starttime=1, comm="x", lifecycle="dying")
        node = LayoutNode(pid=1, row=0, col=0, depth=0, glyph_class=0)
        _, color, attr = node_style(proc, node, Charset(True), 0)
        assert color == COL_DYING
        assert attr & A_BOLD

    def test_spike_spinner(self):
        proc = ProcessState(pid=7, starttime=1, comm="x", lifecycle="active", spike=True)
        node = LayoutNode(pid=7, row=0, col=0, depth=0, glyph_class=0)
        ch, color, _ = node_style(proc, node, Charset(True), 0)
        assert ch in ("◴", "◷", "◶", "◵")
        assert color == COL_SPIKE

    def test_high_cpu_spinner(self):
        proc = ProcessState(pid=7, starttime=1, comm="x", lifecycle="active", cpu_pct=55.0)
        node = LayoutNode(pid=7, row=0, col=0, depth=0, glyph_class=0)
        ch, color, _ = node_style(proc, node, Charset(True), 0)
        assert ch in ("◴", "◷", "◶", "◵")
        assert color == COL_ACTIVE

    def test_idle_dim(self):
        proc = ProcessState(pid=7, starttime=1, comm="x", lifecycle="idle")
        node = LayoutNode(pid=7, row=0, col=0, depth=0, glyph_class=0)
        _, color, attr = node_style(proc, node, Charset(True), 0)
        assert color == COL_IDLE
        assert attr & A_DIM

    def test_active_normal(self):
        proc = ProcessState(pid=7, starttime=1, comm="x", lifecycle="active", cpu_pct=1.0)
        node = LayoutNode(pid=7, row=0, col=0, depth=0, glyph_class=0)
        _, color, attr = node_style(proc, node, Charset(True), 0)
        assert color == COL_ACTIVE
        assert attr == A_NORMAL


class TestCompose:
    def test_node_cell_emitted(self):
        eng = engine_with([(1, 0)])
        lay = compute_layout(eng, 80, 24, charset=Charset(True))
        frame = compose_ecosystem(lay, eng, 0, Charset(True), 80, 24)
        assert any(cell.ch in ("·", "●", "◉", "⬤") for cell in frame.cells)

    def test_link_cells_colored(self):
        eng = engine_with([(1, 0), (2, 1), (3, 1)])
        lay = compute_layout(eng, 80, 24, charset=Charset(True))
        frame = compose_ecosystem(lay, eng, 0, Charset(True), 80, 24)
        link_cells = [c for c in frame.cells if c.color == COL_LINK]
        assert link_cells

    def test_zombie_glyph(self):
        eng = engine_with([(1, 0), (2, 1)])
        eng.process(2).state = "Z"
        lay = compute_layout(eng, 80, 24, charset=Charset(True))
        frame = compose_ecosystem(lay, eng, 0, Charset(True), 80, 24)
        zombie_cells = [c for c in frame.cells if c.ch == "✖"]
        assert len(zombie_cells) == 1
        assert zombie_cells[0].color == COL_ZOMBIE

    def test_orphan_glyph(self):
        eng = engine_with([(1, 0), (2, 1)])
        eng.process(2).orphan = True
        lay = compute_layout(eng, 80, 24, charset=Charset(True))
        frame = compose_ecosystem(lay, eng, 0, Charset(True), 80, 24)
        orphan_cells = [c for c in frame.cells if c.ch == "◌"]
        assert len(orphan_cells) == 1
        assert orphan_cells[0].color == COL_ORPHAN

    def test_collapsed_label(self):
        eng = engine_with([(1, 0), (2, 1), (3, 2), (4, 3)])
        lay = compute_layout(eng, 80, 24, budget=2, charset=Charset(True))
        frame = compose_ecosystem(lay, eng, 0, Charset(True), 80, 24)
        plus_cells = [c for c in frame.cells if c.ch == "+"]
        assert plus_cells
        assert all(c.color == COL_COLLAPSED for c in plus_cells)

    def test_ascii_frame(self):
        eng = engine_with([(1, 0), (2, 1)])
        lay = compute_layout(eng, 80, 24, charset=Charset(False))
        frame = compose_ecosystem(lay, eng, 0, Charset(False), 80, 24)
        chars = {c.ch for c in frame.cells}
        assert chars <= set(".oO@x|+-") | set("0123456789")

    def test_cells_sorted_deterministic(self):
        eng = engine_with([(1, 0), (2, 1), (3, 1)])
        lay = compute_layout(eng, 80, 24, charset=Charset(True))
        f1 = compose_ecosystem(lay, eng, 0, Charset(True), 80, 24)
        f2 = compose_ecosystem(lay, eng, 0, Charset(True), 80, 24)
        assert [(c.row, c.col, c.ch) for c in f1.cells] == [(c.row, c.col, c.ch) for c in f2.cells]

    def test_cells_bounds(self):
        eng = engine_with([(1, 0), (2, 1), (3, 1)])
        lay = compute_layout(eng, 30, 10, charset=Charset(True))
        frame = compose_ecosystem(lay, eng, 0, Charset(True), 30, 10)
        for c in frame.cells:
            assert 0 <= c.row < 10
            assert 0 <= c.col < 30

    def test_missing_proc_skipped(self):
        lay = Layout(nodes=[LayoutNode(pid=999, row=1, col=1, depth=0, glyph_class=0)])
        eng = engine_with([(1, 0)])
        frame = compose_ecosystem(lay, eng, 0, Charset(True), 80, 24)
        assert frame.cells == []

    def test_cell_dataclass(self):
        c = Cell(1, 2, "x")
        assert (c.row, c.col, c.ch) == (1, 2, "x")
