"""Ecosystem layout: pure, deterministic placement of the visible tree.

Turns the engine's tree into positioned nodes and link segments for a given
canvas. No I/O, no curses — golden-frame testable. Pruning and panning live
here; rendering and input live elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LayoutNode:
    pid: int
    row: int
    col: int
    depth: int
    glyph_class: int          # 0..3 by RSS
    collapsed: int = 0        # hidden descendant count (>0 → "+N")
    orphan: bool = False
    zombie: bool = False
    collapsed_folded: bool = False  # this node itself was collapsed into parent

    def position(self) -> tuple[int, int]:
        return (self.row, self.col)


@dataclass
class LinkSeg:
    row: int
    col: int
    ch: str


@dataclass
class Layout:
    nodes: list[LayoutNode] = field(default_factory=list)
    links: list[LinkSeg] = field(default_factory=list)
    clipped: int = 0          # nodes hidden by canvas bounds (panning)
    collapsed_count: int = 0  # total hidden-by-collapse nodes

    def node_by_pid(self, pid: int) -> LayoutNode | None:
        for n in self.nodes:
            if n.pid == pid:
                return n
        return None

    def nearest(self, row: int, col: int) -> LayoutNode | None:
        """Nearest node by Manhattan distance (for arrow-key selection)."""
        best, best_d = None, None
        for n in self.nodes:
            d = abs(n.row - row) + abs(n.col - col)
            if best_d is None or d < best_d:
                best, best_d = n, d
        return best


ROW_GAP = 3  # vertical distance between tree levels


def rss_classes(rss_values: list[int]) -> tuple[float, float, float]:
    """RSS thresholds (p50, p90, p99) over positive values → glyph classes."""
    vals = sorted(v for v in rss_values if v > 0)
    if not vals:
        return (1.0, 1.0, 1.0)
    n = len(vals)
    p = lambda q: vals[min(n - 1, int(n * q))]

    def clamp(pct):
        v = p(pct)
        return v if v > 0 else 1.0

    return (clamp(0.5), clamp(0.9), clamp(0.99))


def _class_for(rss: int, p50: float, p90: float, p99: float) -> int:
    if rss <= 0:
        return 0
    if rss >= p99:
        return 3
    if rss >= p90:
        return 2
    if rss >= p50:
        return 1
    return 0


def compute_layout(
    engine,
    width: int,
    height: int,
    budget: int = 40,
    show_kernel: bool = False,
    pan_x: int = 0,
    pan_y: int = 0,
    charset: object = None,
) -> Layout:
    """Lay out the visible ecosystem on a canvas.

    engine: EcosystemState (or anything exposing processes/children_of/roots).
    charset: glyph table with .v .h .tee .corner_left .corner_right attrs.
    """
    out = Layout()
    if width < 4 or height < 3:
        return out
    chars = charset

    alive = [p for p in engine.processes.values()]
    p50, p90, p99 = rss_classes([p.rss for p in alive])

    def weight(pid: int) -> float:
        proc = engine.process(pid)
        if proc is None:
            return 0.0
        rss_norm = proc.rss / p99 if p99 else 0.0
        return proc.cpu_pct + rss_norm * 50.0

    subtree_weight: dict[int, float] = {}

    def sub_weight(pid: int) -> float:
        if pid in subtree_weight:
            return subtree_weight[pid]
        total = weight(pid)
        for c in engine.children_of(pid):
            total += sub_weight(c)
        subtree_weight[pid] = total
        return total

    # root forest (kernel subtrees hidden unless requested)
    def keep(pid: int) -> bool:
        if show_kernel:
            return True
        return not _is_kernel_subtree(engine, pid)

    roots = [r for r in engine.roots() if keep(r)]
    # kernel roots whose parent is kthreadd (pid 2) are already excluded by roots()

    for r in roots:
        sub_weight(r)

    # order roots by subtree weight desc, children likewise; deterministic
    def ordered_children(pid: int) -> list[int]:
        return sorted(engine.children_of(pid), key=lambda c: (-subtree_weight.get(c, 0.0), c))

    # budget pruning: depth-first, collapse subtrees that overflow the budget
    visible: list[LayoutNode] = []
    count = [0]

    def walk(pid: int, depth: int, parent_col: int | None, sibling_index: int, sibling_count: int, collapsed_into: bool):
        if count[0] >= budget:
            return
        proc = engine.process(pid)
        if proc is None:
            return
        count[0] += 1
        node = LayoutNode(
            pid=pid,
            row=0,
            col=0,
            depth=depth,
            glyph_class=_class_for(proc.rss, p50, p90, p99),
            orphan=proc.orphan,
            zombie=proc.state == "Z",
            collapsed_folded=collapsed_into,
        )
        visible.append(node)
        kids = ordered_children(pid)
        if not kids:
            return
        if count[0] + len(kids) > budget:
            node.collapsed = _count_subtree(engine, pid) - 1
            return
        for i, k in enumerate(kids):
            walk(k, depth + 1, None, i, len(kids), False)

    for r in roots:
        walk(r, 0, None, 0, len(roots), False)

    if not visible:
        return out

    # --- placement -----------------------------------------------------------
    # depth → row band
    max_depth = max(n.depth for n in visible)
    max_row = 1 + max_depth * ROW_GAP
    # rows may exceed the canvas: allow panning by shrinking row indices first
    row_for = {d: 1 + d * ROW_GAP for d in range(max_depth + 1)}

    by_pid = {n.pid: n for n in visible}

    def place_root(r: LayoutNode, i: int, n: int):
        r.row = row_for[0]
        r.col = clamp_col(round((i + 1) * width / (n + 1)))

    def clamp_col(c: int) -> int:
        return max(0, min(width - 1, c))

    roots_vis = [n for n in visible if n.depth == 0]
    for i, r in enumerate(roots_vis):
        place_root(r, i, len(roots_vis))

    # children fan out from their parent's column
    def place_children(parent: LayoutNode):
        kids = [by_pid[pid] for pid in ordered_children(parent.pid) if pid in by_pid]
        if not kids:
            return
        fan = 2 if parent.depth >= 2 else 3
        for i, k in enumerate(kids):
            k.row = row_for[k.depth]
            k.col = clamp_col(parent.col + round((i - (len(kids) - 1) / 2) * fan))

    for node in visible:
        if node.depth > 0:
            continue
        place_children(node)
    for depth in range(1, max_depth + 1):
        for node in [n for n in visible if n.depth == depth]:
            place_children(node)

    out.collapsed_count = sum(1 for n in visible if n.collapsed)

    # --- links ---------------------------------------------------------------
    links: dict[tuple[int, int], str] = {}
    for parent in visible:
        kids = [by_pid[pid] for pid in ordered_children(parent.pid) if pid in by_pid]
        if not kids:
            continue
        pr, pc = parent.row, parent.col
        rail = pr + 1
        if rail >= parent.row + ROW_GAP:
            # vertical stub from parent down to the rail
            for rr in range(pr + 1, rail):
                _put(links, rr, pc, chars.v)
        if len(kids) == 1 and kids[0].col == pc:
            for rr in range(rail, kids[0].row):
                _put(links, rr, pc, chars.v)
            continue
        lo, hi = min(k.col for k in kids), max(k.col for k in kids)
        if lo != hi:
            for cc in range(lo, hi + 1):
                _put(links, rail, cc, chars.h)
        for k in kids:
            for cc in (k.col,):
                if rail < k.row:
                    _put(links, rail, cc, chars.tee)
                    for rr in range(rail + 1, k.row):
                        _put(links, rr, cc, chars.v)
                else:
                    _put(links, k.row - 1, cc, chars.tee)

    # --- pan + clip ----------------------------------------------------------
    pan_y = max(0, pan_y)
    pan_x = max(0, pan_x)
    kept_nodes: list[LayoutNode] = []
    for n in visible:
        nr, nc = n.row - pan_y, n.col - pan_x
        if 0 <= nr < height and 0 <= nc < width:
            n.row, n.col = nr, nc
            kept_nodes.append(n)
        else:
            out.clipped += 1
    out.nodes = kept_nodes
    for (lr, lc), ch in sorted(links.items()):
        lr2, lc2 = lr - pan_y, lc - pan_x
        if 0 <= lr2 < height and 0 <= lc2 < width:
            out.links.append(LinkSeg(lr2, lc2, ch))
    return out


def _put(grid: dict[tuple[int, int], str], row: int, col: int, ch: str) -> None:
    grid[(row, col)] = ch


def _count_subtree(engine, pid: int) -> int:
    total = 1
    for c in engine.children_of(pid):
        total += _count_subtree(engine, c)
    return total


def _is_kernel_subtree(engine, pid: int) -> bool:
    if pid == 2:  # kthreadd itself
        return True
    proc = engine.process(pid)
    if proc is None:
        return True
    if proc.ppid == 2:  # kernel thread
        return True
    if proc.ppid == 0:
        return False
    return _is_kernel_subtree(engine, proc.ppid)
