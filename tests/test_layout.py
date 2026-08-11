"""Layout tests: deterministic placement, pruning, panning, kernels."""

from __future__ import annotations

import pytest

from process_ecosystem.layout import Layout, compute_layout, rss_classes
from process_ecosystem.render import Charset
from tests.conftest import engine_with, snap, system

CHARS = Charset(True)


def lay(eng, width=80, height=24, **kw):
    return compute_layout(eng, width, height, charset=CHARS, **kw)


class TestBasicPlacement:
    def test_empty_engine_empty_layout(self):
        eng = engine_with([])
        out = lay(eng)
        assert out.nodes == []
        assert out.links == []

    def test_single_root_at_top_middle(self):
        eng = engine_with([(1, 0)])
        out = lay(eng)
        assert len(out.nodes) == 1
        n = out.nodes[0]
        assert n.pid == 1
        assert n.row == 1
        assert n.col == 40

    def test_children_fan_below_root(self):
        eng = engine_with([(1, 0), (2, 1), (3, 1), (4, 1)])
        out = lay(eng)
        kids = sorted(n.col for n in out.nodes if n.pid in (2, 3, 4))
        assert len(set(kids)) == 3
        child = next(n for n in out.nodes if n.pid == 2)
        assert child.row == 4  # ROW_GAP=3

    def test_links_present(self):
        eng = engine_with([(1, 0), (2, 1), (3, 1)])
        out = lay(eng)
        assert out.links
        assert all(0 <= s.row < 24 and 0 <= s.col < 80 for s in out.links)

    def test_roots_placed_evenly(self):
        eng = engine_with([(1, 0), (7, 0)])
        out = lay(eng)
        cols = sorted(n.col for n in out.nodes if n.depth == 0)
        assert cols[0] < cols[1]

    def test_too_small_canvas(self):
        eng = engine_with([(1, 0)])
        assert lay(eng, width=3, height=2).nodes == []

    def test_deep_tree_rows_spaced(self):
        eng = engine_with([(1, 0), (2, 1), (3, 2), (4, 3)])
        out = lay(eng)
        depth_row = {n.depth: n.row for n in out.nodes}
        assert depth_row[3] - depth_row[2] == 3


class TestGlyphClasses:
    def test_rss_classes_thresholds(self):
        p50, p90, p99 = rss_classes(list(range(100, 1600, 100)))  # 15 values
        assert p50 == 800   # vals[7]
        assert p90 == 1400  # vals[13]
        assert p99 == 1500  # vals[14]

    def test_rss_classes_thresholds_monotonic(self):
        p50, p90, p99 = rss_classes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        assert p50 <= p90 <= p99

    def test_rss_classes_empty(self):
        assert rss_classes([]) == (1.0, 1.0, 1.0)

    def test_rss_classes_ignores_zero(self):
        assert rss_classes([0, 0, 10]) == (10.0, 10.0, 10.0)

    def test_node_glyph_class_by_rss(self):
        eng = engine_with([])
        eng.update(
            [snap(1, 0, rss=10), snap(2, 1, rss=1000), snap(3, 1, rss=1000000)],
            system(),
            0.2,
            0.0,
        )
        out = lay(eng)
        classes = {n.pid: n.glyph_class for n in out.nodes}
        assert classes[1] < classes[2] < classes[3]


class TestBudgetPruning:
    def test_budget_caps_visible_nodes(self):
        eng = engine_with([])
        saps = [snap(i, i // 2 if i > 1 else 0, comm=f"p{i}") for i in range(1, 30)]
        eng.update(saps, system(), 0.2, 0.0)
        out = lay(eng, budget=10)
        assert len(out.nodes) <= 10

    def test_collapsed_counter(self):
        eng = engine_with([])
        saps = [snap(i, i // 2 if i > 1 else 0, comm=f"p{i}") for i in range(1, 30)]
        eng.update(saps, system(), 0.2, 0.0)
        out = lay(eng, budget=8)
        assert out.collapsed_count >= 1
        collapsed = [n for n in out.nodes if n.collapsed]
        assert collapsed

    def test_no_budget_overflow(self):
        eng = engine_with([])
        saps = [snap(i, i // 2 if i > 1 else 0) for i in range(1, 50)]
        eng.update(saps, system(), 0.2, 0.0)
        out = lay(eng, budget=20)
        assert len(out.nodes) <= 20


class TestKernel:
    def test_kernel_subtree_hidden_by_default(self):
        eng = engine_with([(1, 0), (2, 0), (3, 2), (4, 2), (5, 3)])
        out = lay(eng)
        assert all(n.pid != 2 and n.pid != 3 and n.pid != 5 for n in out.nodes)
        assert any(n.pid == 1 for n in out.nodes)

    def test_kernel_shown_when_requested(self):
        eng = engine_with([(1, 0), (2, 0), (3, 2)])
        out = lay(eng, show_kernel=True)
        assert any(n.pid == 2 for n in out.nodes)
        assert any(n.pid == 3 for n in out.nodes)

    def test_kernel_child_of_non_kernel_visible(self):
        # pid 3's parent is 2 (kernel) → hidden; pid 7 parent 1 → visible
        eng = engine_with([(1, 0), (2, 0), (3, 2), (7, 1)])
        out = lay(eng)
        pids = {n.pid for n in out.nodes}
        assert 3 not in pids
        assert 7 in pids


class TestPanClip:
    def test_pan_moves_nodes(self):
        eng = engine_with([(1, 0)])
        out = lay(eng, pan_x=10)
        assert out.nodes[0].col == 30

    def test_clipped_counted(self):
        eng = engine_with([(1, 0), (2, 1), (3, 1)])
        # pan down beyond the tree
        out = lay(eng, pan_y=10)
        assert out.clipped >= 1
        assert all(0 <= n.row < 24 for n in out.nodes)

    def test_pan_clamp_negative(self):
        eng = engine_with([(1, 0)])
        out = lay(eng, pan_x=-5)
        assert out.nodes[0].col == 40


class TestNodeHelpers:
    def test_node_by_pid(self):
        eng = engine_with([(1, 0), (2, 1)])
        out = lay(eng)
        assert out.node_by_pid(2).pid == 2
        assert out.node_by_pid(999) is None

    def test_nearest(self):
        eng = engine_with([(1, 0), (2, 1), (3, 1)])
        out = lay(eng)
        near = out.nearest(1, 40)
        assert near.pid == 1

    def test_nearest_empty(self):
        out = Layout()
        assert out.nearest(0, 0) is None

    def test_zombie_and_orphan_flags(self):
        eng = engine_with([(1, 0), (2, 1,), (3, 2)])
        eng.process(2).state = "Z"
        eng.process(3).orphan = True
        out = lay(eng)
        by_pid = {n.pid: n for n in out.nodes}
        assert by_pid[2].zombie is True
        assert by_pid[3].orphan is True
