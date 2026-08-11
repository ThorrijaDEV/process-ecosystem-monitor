"""Rendering: glyph tables, color mapping, ecosystem frame composition.

Pure functions — a frame is a list of cells; the curses layer only paints.
Deterministic for a given engine state and tick (golden-frame testable).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import DYING, IDLE, NEW

# --- color ids (mapped to curses pairs in ui.py) ----------------------------

COL_DEFAULT = 0
COL_NEW = 1
COL_ACTIVE = 2
COL_IDLE = 3
COL_DYING = 4
COL_ZOMBIE = 5
COL_SPIKE = 6
COL_LINK = 7
COL_ORPHAN = 8
COL_COLLAPSED = 9
COL_SELECT = 10
COL_TEXT = 11
COL_ACCENT = 12
COL_TITLE = 13
COL_OK = 14
COL_WARN = 15

# --- attributes --------------------------------------------------------------

A_NORMAL = 0
A_BOLD = 1
A_BLINK = 2
A_REVERSE = 4
A_DIM = 8

SPINNER_CPU = 10.0  # % of one core → the node spins


@dataclass
class Cell:
    row: int
    col: int
    ch: str
    color: int = COL_DEFAULT
    attr: int = A_NORMAL


@dataclass
class Frame:
    cells: list[Cell] = field(default_factory=list)
    width: int = 0
    height: int = 0


# --- glyph tables --------------------------------------------------------------

class Charset:
    def __init__(self, unicode_: bool):
        u = unicode_
        self.node = ("·", "●", "◉", "⬤") if u else (".", "o", "O", "@")
        self.spinner = ("◴", "◷", "◶", "◵") if u else ("-", "\\", "|", "/")
        self.zombie = "✖" if u else "x"
        self.orphan = "◌" if u else "o"
        self.v = "│" if u else "|"
        self.h = "─" if u else "-"
        self.tee = "┬" if u else "+"
        self.leaf = "·" if u else "."


def charset_for(unicode_ok: bool) -> Charset:
    return Charset(unicode_ok)


# --- ecosystem frame -----------------------------------------------------------

def compose_ecosystem(layout, engine, tick: int, chars: Charset, width: int, height: int) -> Frame:
    """Compose the ecosystem view frame from a Layout."""
    frame = Frame(width=width, height=height)
    cells: dict[tuple[int, int], Cell] = {}

    def put(row: int, col: int, ch: str, color: int = COL_DEFAULT, attr: int = A_NORMAL) -> None:
        if 0 <= row < height and 0 <= col < width:
            cells[(row, col)] = Cell(row, col, ch, color, attr)

    for seg in layout.links:
        put(seg.row, seg.col, seg.ch, COL_LINK)

    for node in layout.nodes:
        proc = engine.process(node.pid)
        if proc is None:
            continue
        r, c = node.row, node.col
        if node.collapsed:
            label = f"+{node.collapsed}"
            for i, ch in enumerate(label):
                put(r, c + i, ch, COL_COLLAPSED, A_BOLD)
            continue
        if node.zombie:
            put(r, c, chars.zombie, COL_ZOMBIE)
            continue
        if node.orphan:
            put(r, c, chars.orphan, COL_ORPHAN)
            continue
        ch, color, attr = node_style(proc, node, chars, tick)
        put(r, c, ch, color, attr)

    frame.cells = [cells[k] for k in sorted(cells)]
    return frame


def node_style(proc, node, chars: Charset, tick: int) -> tuple[str, int, int]:
    """(char, color, attrs) for one visible node."""
    glyph = chars.node[node.glyph_class]
    if proc.lifecycle == NEW:
        return (glyph, COL_NEW, A_BOLD | (A_BLINK if tick % 2 == 0 else 0))
    if proc.lifecycle == DYING:
        return (glyph, COL_DYING, A_BOLD)
    if proc.spike:
        return (chars.spinner[(proc.pid + tick) % 4], COL_SPIKE, A_BOLD)
    if proc.cpu_pct >= SPINNER_CPU:
        return (chars.spinner[(proc.pid + tick) % 4], COL_ACTIVE, A_BOLD)
    if proc.lifecycle == IDLE:
        return (glyph, COL_IDLE, A_DIM)
    return (glyph, COL_ACTIVE, A_NORMAL)
