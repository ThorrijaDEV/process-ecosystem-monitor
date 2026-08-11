"""Curses terminal UI: views, interaction, painting.

Thin layer on top of the pure engine/layout/render modules: it only paints
cells and translates keys. The collector is injected, so demo and live runs
share everything.
"""

from __future__ import annotations

import curses
import signal
import sys
from dataclasses import dataclass, field

from .engine import EcosystemState
from .history import Event
from .layout import ROW_GAP, compute_layout
from .model import ACTIVE, DYING, IDLE, NEW, ProcessState
from .render import (
    A_BLINK,
    A_BOLD,
    A_DIM,
    A_NORMAL,
    A_REVERSE,
    COL_ACCENT,
    COL_ACTIVE,
    COL_COLLAPSED,
    COL_DEFAULT,
    COL_DYING,
    COL_IDLE,
    COL_LINK,
    COL_NEW,
    COL_ORPHAN,
    COL_SELECT,
    COL_SPIKE,
    COL_TEXT,
    COL_TITLE,
    COL_WARN,
    COL_ZOMBIE,
    Charset,
    Cell,
    compose_ecosystem,
    charset_for,
)

VIEW_ECOSYSTEM = "ecosystem"
VIEW_TREE = "tree"
VIEW_TIMELINE = "timeline"
VIEW_TOP = "top"
VIEWS = (VIEW_ECOSYSTEM, VIEW_TREE, VIEW_TIMELINE, VIEW_TOP)


@dataclass
class Options:
    rate: float = 0.2
    budget: int = 40
    show_kernel: bool = False
    history_minutes: int = 10
    unicode: bool | None = None
    filter_text: str = ""
    show_cmdline: bool = False
    seed: int | None = None          # demo seed (None → live mode)
    mode_label: str = "live"


_PAIR_MAP = {
    COL_DEFAULT: (curses.COLOR_WHITE, curses.COLOR_BLACK),
    COL_NEW: (curses.COLOR_GREEN, curses.COLOR_BLACK),
    COL_ACTIVE: (curses.COLOR_CYAN, curses.COLOR_BLACK),
    COL_IDLE: (curses.COLOR_WHITE, curses.COLOR_BLACK),
    COL_DYING: (curses.COLOR_RED, curses.COLOR_BLACK),
    COL_ZOMBIE: (curses.COLOR_RED, curses.COLOR_BLACK),
    COL_SPIKE: (curses.COLOR_YELLOW, curses.COLOR_BLACK),
    COL_LINK: (curses.COLOR_BLUE, curses.COLOR_BLACK),
    COL_ORPHAN: (curses.COLOR_MAGENTA, curses.COLOR_BLACK),
    COL_COLLAPSED: (curses.COLOR_WHITE, curses.COLOR_BLACK),
    COL_SELECT: (curses.COLOR_BLACK, curses.COLOR_CYAN),
    COL_TEXT: (curses.COLOR_WHITE, curses.COLOR_BLACK),
    COL_ACCENT: (curses.COLOR_CYAN, curses.COLOR_BLACK),
    COL_TITLE: (curses.COLOR_WHITE, curses.COLOR_BLACK),
    COL_WARN: (curses.COLOR_YELLOW, curses.COLOR_BLACK),
}

_ATTR_MAP = {
    A_BOLD: curses.A_BOLD,
    A_BLINK: curses.A_BLINK,
    A_DIM: curses.A_DIM,
    A_REVERSE: curses.A_REVERSE,
}


class App:
    def __init__(self, collector, engine: EcosystemState, opts: Options, interval_sec: float = 0.2, clk_tck: int = 100, ncores: int = 1, initial_wall_time: float = 0.0):
        self.collector = collector
        self.engine = engine
        self.opts = opts
        self.interval = interval_sec
        self.clk_tck = clk_tck
        self.ncores = ncores
        self.wall_time = initial_wall_time
        self.chars = charset_for(self._unicode_ok())
        self.view = VIEW_ECOSYSTEM
        self.paused = False
        self.quit = False
        self.tick_index = 0
        self.selected_pid: int | None = None
        self.inspector_open = False
        self.show_cmdline = False
        self.help_open = False
        self.pan_x = 0
        self.pan_y = 0
        self.budget = opts.budget
        self.show_kernel = opts.show_kernel
        self.filter_text = opts.filter_text
        self.rate = opts.rate
        self.last_diff = None
        self.messages: list[tuple[str, int]] = []
        self._collapsed: set[int] = set()
        self._prev_cells: dict[tuple[int, int], tuple[str, int, int]] = {}
        self._layout = None
        self._user_cache: dict[int, tuple[int, str | None]] = {}
        self._cmdline_cache: dict[int, tuple[int, str | None]] = {}
        signal.signal(signal.SIGINT, self._on_sigint)

    def _unicode_ok(self) -> bool:
        if self.opts.unicode is not None:
            return self.opts.unicode
        try:
            import locale

            return "UTF-8" in (locale.nl_langinfo(locale.CODESET) or "").upper()
        except Exception:
            return False

    def _on_sigint(self, *args):
        self.quit = True

    # --- main loop -------------------------------------------------------------

    def run(self, screen) -> None:
        self.screen = screen
        try:
            screen.timeout(100)
            screen.keypad(True)
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            self._init_pairs()
            while not self.quit:
                if not self.paused:
                    self._tick()
                self._handle_keys()
                self._paint(screen)
        except KeyboardInterrupt:
            self.quit = True
        finally:
            try:
                screen.keypad(False)
                curses.curs_set(1)
            except curses.error:
                pass

    def _init_pairs(self) -> None:
        curses.start_color()
        for color_id, (fg, bg) in _PAIR_MAP.items():
            try:
                curses.init_pair(color_id + 1, fg, bg)
            except curses.error:
                pass

    def _tick(self) -> None:
        snapshots, system = self.collector.tick()
        self.wall_time += self.interval
        diff = self.engine.update(snapshots, system, self.interval, self.wall_time)
        self.last_diff = diff
        self.tick_index += 1
        for text in self._events_from_diff(diff):
            self.messages.append((text, self.tick_index))
        self.messages = [m for m in self.messages if self.tick_index - m[1] < int(2.0 / self.interval)]
        if self.selected_pid is not None and self.engine.process(self.selected_pid) is None:
            self.selected_pid = None
        if self.collector.finished:
            self.quit = True

    def _events_from_diff(self, diff) -> list[str]:
        plus, cross, ring = ("✚", "✖", "◌") if self.chars.v == "│" else ("+", "x", "o")
        out = []
        for pid, comm in diff.births[:2]:
            out.append(f"{plus} {comm} ({pid})")
        for pid, comm in diff.deaths[:2]:
            out.append(f"{cross} {comm} ({pid})")
        for pid, comm in diff.orphans[:1]:
            out.append(f"{ring} orphan {comm} ({pid})")
        return out

    # --- keys -------------------------------------------------------------------

    def _handle_keys(self) -> None:
        scr = self.screen
        while True:
            key = scr.getch()
            if key == -1:
                return
            if key == curses.KEY_RESIZE:
                self._reset_diff()
                continue
            self._dispatch(key)

    def _dispatch(self, key: int) -> None:
        ch = chr(key) if 0 < key < 256 else ""
        if ch == "q":
            self.quit = True
        elif ch == "p":
            self.paused = not self.paused
        elif ch == "\t" or (ch in "1234"):
            self.view = VIEWS[(VIEWS.index(self.view) + 1) % len(VIEWS)] if ch == "\t" else VIEWS[int(ch) - 1]
            self._reset_diff()
        elif ch in ("+", "="):
            self.rate = max(0.1, round(self.rate - 0.05, 2))
        elif ch == "-":
            self.rate = min(2.0, round(self.rate + 0.05, 2))
        elif ch == "m":
            self.budget = min(200, self.budget + 10)
        elif ch == "M":
            self.budget = max(5, self.budget - 10)
        elif ch == "n":
            self.show_kernel = not self.show_kernel
        elif ch == "c":
            self.show_cmdline = not self.show_cmdline
        elif ch == "?":
            self.help_open = not self.help_open
        elif ch == "/":
            self._prompt_filter()
        elif ch == "\x1b":
            self.filter_text = ""
        elif ch == " ":
            self._toggle_collapse()
        elif key == curses.KEY_ENTER or ch == "\n":
            self.inspector_open = not self.inspector_open
        elif key == curses.KEY_UP or ch == "k":
            self._move_selection(0, -1)
        elif key == curses.KEY_DOWN or ch == "j":
            self._move_selection(0, 1)
        elif key == curses.KEY_LEFT or ch == "h":
            self._move_selection(-1, 0)
        elif key == curses.KEY_RIGHT or ch == "l":
            self._move_selection(1, 0)
        elif key == curses.KEY_PPAGE:
            self.pan_y = max(0, self.pan_y - 5)
        elif key == curses.KEY_NPAGE:
            self.pan_y += 5

    def _prompt_filter(self) -> None:
        scr = self.screen
        self._paint(scr)
        curses.echo()
        try:
            scr.move(self._canvas_bottom(), 0)
            scr.addstr("filter: ")
            text = scr.getstr().decode("utf-8", "replace")
            self.filter_text = text.strip()
        except Exception:
            pass
        finally:
            curses.noecho()

    def _toggle_collapse(self) -> None:
        if self.selected_pid is None:
            return
        if self.selected_pid in self._collapsed:
            self._collapsed.discard(self.selected_pid)
        else:
            self._collapsed.add(self.selected_pid)
        self._reset_diff()

    # --- selection ---------------------------------------------------------------

    def _visible_pids(self) -> list[int]:
        if self._layout is None:
            return []
        return [n.pid for n in self._layout.nodes]

    def _move_selection(self, dc: int, dr: int) -> None:
        nodes = self._layout.nodes if self._layout else []
        if not nodes:
            return
        if self.selected_pid is None:
            self.selected_pid = nodes[0].pid
            return
        cur = None
        for n in nodes:
            if n.pid == self.selected_pid:
                cur = n
                break
        if cur is None:
            self.selected_pid = nodes[0].pid
            return
        # nearest node strictly in the requested direction
        best, best_d = None, None
        for n in nodes:
            dcol, drow = n.col - cur.col, n.row - cur.row
            if dc == 0 and drow == 0:
                continue
            if dc != 0 and dcol == 0:
                continue
            if (dc < 0 and dcol >= 0) or (dc > 0 and dcol <= 0):
                continue
            if (dr < 0 and drow >= 0) or (dr > 0 and drow <= 0):
                continue
            if dr != 0 and dc == 0 and abs(dcol) > abs(drow) * 3:
                continue
            d = abs(dcol) + abs(drow)
            if best_d is None or d < best_d:
                best, best_d = n, d
        if best is not None:
            self.selected_pid = best.pid

    # --- painting ----------------------------------------------------------------

    def _canvas_top(self) -> int:
        return 1

    def _canvas_bottom(self) -> int:
        h, _ = self.screen.getmaxyx()
        return h - 2

    def _paint(self, screen) -> None:
        h, w = screen.getmaxyx()
        if h < 6 or w < 24:
            self._fill(screen, "terminal too small")
            return
        if self.help_open:
            self._paint_help(screen, h, w)
            return
        self._paint_header(screen, w)
        self._build_layout(w, h)
        if self.view == VIEW_ECOSYSTEM:
            frame = compose_ecosystem(self._layout, self.engine, self.tick_index, self.chars, w, h - 2)
            self._paint_frame(screen, frame, self._canvas_top(), 0)
        elif self.view == VIEW_TREE:
            self._paint_tree(screen, h, w)
        elif self.view == VIEW_TIMELINE:
            self._paint_timeline(screen, h, w)
        elif self.view == VIEW_TOP:
            self._paint_top(screen, h, w)
        if self.inspector_open and self.selected_pid is not None:
            self._paint_inspector(screen, h, w)
        self._paint_footer(screen, h, w)
        screen.refresh()

    def _build_layout(self, w: int, h: int) -> None:
        self._layout = compute_layout(
            self.engine,
            w,
            h - 2,
            budget=self.budget,
            show_kernel=self.show_kernel,
            pan_x=self.pan_x,
            pan_y=self.pan_y,
            charset=self.chars,
        )

    def _paint_header(self, screen, w: int) -> None:
        try:
            screen.move(0, 0)
            screen.clrtoeol()
        except curses.error:
            pass
        title = f"PROCESS ECOSYSTEM — {self.opts.mode_label}"
        if self.paused:
            title += "  [paused]"
        self._put(screen, 0, 0, title[: w - 1], COL_TITLE, A_BOLD)
        view = f"view:{self.view}"
        self._put(screen, 0, max(0, w - len(view) - 1), view, COL_ACCENT)

    def _paint_footer(self, screen, h: int, w: int) -> None:
        eng = self.engine
        diff = self.last_diff
        births = len(diff.births) if diff else 0
        deaths = len(diff.deaths) if diff else 0
        left = (
            f"CPU {eng.system_cpu:4.1f}%  RAM {eng.system_ram:4.1f}%  "
            f"procs {eng.alive_count()}  ↑{births} ↓{deaths}  load {eng.load1:4.2f}  "
            f"{self.rate:0.2f}s"
        )
        duration = getattr(self.collector, "duration", None)
        if duration is not None:
            t = getattr(self.collector, "sim_time", 0.0)
            left += f"  t={min(t, duration):.0f}/{duration:.0f}s"
        hint = "q quit  ? help"
        hint_col = max(0, w - len(hint))
        space = hint_col - 1
        msg = "  ".join(t for t, _ in self.messages[-3:])
        if msg:
            self._put(screen, h - 1, 0, (left + "  " + msg)[:space], COL_TEXT)
        else:
            self._put(screen, h - 1, 0, left[:space], COL_TEXT)
        self._put(screen, h - 1, hint_col, hint, COL_ACCENT)

    def _paint_frame(self, screen, frame, base_row: int, base_col: int) -> None:
        current: dict[tuple[int, int], tuple[str, int, int]] = {}
        for cell in frame.cells:
            key = (cell.row + base_row, cell.col + base_col)
            current[key] = (cell.ch, cell.color, cell.attr)
            prev = self._prev_cells.get(key)
            if prev != (cell.ch, cell.color, cell.attr):
                self._draw_cell(screen, cell.row + base_row, cell.col + base_col, cell.ch, cell.color, cell.attr)
        # erase cells that vanished from the frame (moved/dead nodes, cleared links)
        for key in self._prev_cells:
            if key not in current and key[0] >= base_row:
                self._draw_cell(screen, key[0], key[1], " ", COL_DEFAULT, A_NORMAL)
        sel_key = None
        if self.selected_pid is not None and self._layout is not None:
            for n in self._layout.nodes:
                if n.pid == self.selected_pid:
                    key = (n.row + base_row, n.col + base_col)
                    cell = current.get(key)
                    if cell:
                        self._draw_cell(screen, key[0], key[1], cell[0], COL_SELECT, A_REVERSE)
                        sel_key = key
                    break
        prev_sel = getattr(self, "_sel_key", None)
        if prev_sel is not None and prev_sel != sel_key:
            self._prev_cells.pop(prev_sel, None)
        self._sel_key = sel_key
        self._prev_cells = current

    def _clear_canvas(self, screen) -> None:
        h, w = screen.getmaxyx()
        top, bottom = self._canvas_top(), min(self._canvas_bottom(), h - 1)
        for r in range(top, bottom):
            try:
                screen.addstr(r, 0, " " * w)
            except curses.error:
                pass

    def _draw_cell(self, screen, row: int, col: int, ch: str, color: int, attr: int) -> None:
        try:
            screen.addstr(row, col, ch, self._pair(color, attr))
        except curses.error:
            pass

    def _pair(self, color: int, attr: int) -> int:
        flags = 0
        for a, flag in _ATTR_MAP.items():
            if attr & a:
                flags |= flag
        return curses.color_pair(color + 1) | flags

    def _put(self, screen, row: int, col: int, text: str, color: int = COL_TEXT, attr: int = A_NORMAL) -> None:
        try:
            screen.addstr(row, col, text[: self.screen.getmaxyx()[1] - col], self._pair(color, attr))
        except curses.error:
            pass

    def _fill(self, screen, text: str) -> None:
        try:
            screen.erase()
            screen.addstr(0, 0, text[: max(1, self.screen.getmaxyx()[1] - 1)])
            screen.refresh()
        except curses.error:
            pass

    def _reset_diff(self) -> None:
        self._prev_cells = {}

    # --- tree view ----------------------------------------------------------------

    def _tree_rows(self) -> list[tuple[int, int]]:
        """(pid, depth) rows, honoring collapsed nodes, filter, kernel filter."""
        rows: list[tuple[int, int]] = []
        seen: set[int] = set()

        def walk(pid: int, depth: int, chain: list[bool]) -> None:
            if pid in seen:
                return
            seen.add(pid)
            proc = self.engine.process(pid)
            if proc is None:
                return
            if not self.show_kernel and proc.ppid == 2:
                return
            if self.filter_text and self.filter_text.lower() not in proc.comm.lower():
                return
            rows.append((pid, depth))
            if pid in self._collapsed:
                return
            for child in self.engine.children_of(pid):
                walk(child, depth + 1, chain + [True])

        for root in self.engine.roots():
            if not self.show_kernel and root == 2:
                continue
            walk(root, 0, [])
        return rows

    def _paint_tree(self, screen, h: int, w: int) -> None:
        rows = self._tree_rows()
        self._clear_canvas(screen)
        top = self._canvas_top()
        bottom = self._canvas_bottom()
        height = bottom - top
        if self.selected_pid is None and rows:
            self.selected_pid = rows[0][0]
        sel_idx = next((i for i, (pid, _) in enumerate(rows) if pid == self.selected_pid), 0)
        if sel_idx >= height:
            start = sel_idx - height + 1
        elif sel_idx < self.pan_y:
            start = sel_idx
        else:
            start = self.pan_y
        for i in range(height):
            idx = start + i
            if idx >= len(rows):
                break
            pid, depth = rows[idx]
            proc = self.engine.process(pid)
            if proc is None:
                continue
            prefix = "  " * depth
            line = f"{prefix}{proc.comm:<14} {proc.cpu_pct:5.1f}% {proc.rss // (1024*1024):4d}M {proc.state} {self._age(proc)}"
            color, attr = self._row_style(proc)
            if pid == self.selected_pid:
                color, attr = COL_SELECT, A_REVERSE
            self._put(screen, top + i, 0, line[: w - 1], color, attr)

    def _row_style(self, proc: ProcessState) -> tuple[int, int]:
        if proc.lifecycle == NEW:
            return (COL_NEW, A_BOLD)
        if proc.lifecycle == DYING:
            return (COL_DYING, A_BOLD)
        if proc.state == "Z":
            return (COL_ZOMBIE, A_BOLD)
        if proc.spike or proc.cpu_pct >= 10:
            return (COL_SPIKE, A_BOLD)
        if proc.lifecycle == IDLE:
            return (COL_IDLE, A_DIM)
        return (COL_ACCENT, A_NORMAL)

    # --- timeline view --------------------------------------------------------------

    def _paint_timeline(self, screen, h: int, w: int) -> None:
        entries = self.engine.history.recent(w - 4)
        self._clear_canvas(screen)
        top = self._canvas_top()
        if not entries:
            self._put(screen, top, 0, "collecting history…", COL_ACCENT)
            return
        ramp = " ▁▂▃▄▅▆▇█"
        max_bd = max(1, max((e.births + e.deaths for e in entries)))
        self._put(screen, top, 0, "CPU%   ", COL_ACCENT)
        line = "".join(ramp[min(8, int(e.cpu_pct / 100 * 8))] for e in entries)
        self._put(screen, top, 7, line, COL_SPIKE)
        self._put(screen, top + 1, 0, "RAM%   ", COL_ACCENT)
        line = "".join(ramp[min(8, int(e.ram_pct / 100 * 8))] for e in entries)
        self._put(screen, top + 1, 7, line, COL_DYING)
        self._put(screen, top + 2, 0, "churn  ", COL_ACCENT)
        line = "".join(ramp[min(8, int((e.births + e.deaths) / max_bd * 8))] for e in entries)
        self._put(screen, top + 2, 7, line, COL_NEW)
        self._put(screen, top + 4, 0, "events", COL_ACCENT)
        events: list[Event] = []
        for e in reversed(entries):
            events.extend(reversed(e.events))
        for i, ev in enumerate(events[: h - top - 6]):
            t = f"{ev.tick_time:6.1f}"
            self._put(screen, top + 5 + i, 0, f"{t}  {ev.kind:<7} {ev.comm} ({ev.pid})", COL_TEXT)

    # --- top view ---------------------------------------------------------------------

    def _paint_top(self, screen, h: int, w: int) -> None:
        procs = sorted(self.engine.processes.values(), key=lambda p: (-p.cpu_pct, -p.rss))
        self._clear_canvas(screen)
        top = self._canvas_top()
        height = self._canvas_bottom() - top
        start = self.pan_y
        for i in range(height):
            idx = start + i
            if idx >= len(procs):
                break
            proc = procs[idx]
            line = f"{proc.pid:<7} {proc.comm:<14} {proc.cpu_pct:6.1f}% {proc.rss // (1024*1024):5d}M {proc.state} {proc.num_threads:3d}t {self._age(proc)}"
            color, attr = self._row_style(proc)
            if proc.pid == self.selected_pid:
                color, attr = COL_SELECT, A_REVERSE
            self._put(screen, top + i, 0, line[: w - 1], color, attr)

    # --- inspector ----------------------------------------------------------------------

    def _paint_inspector(self, screen, h: int, w: int) -> None:
        pid = self.selected_pid
        proc = self.engine.process(pid)
        if proc is None:
            return
        eng = self.engine
        rows: list[tuple[str, str]] = [
            ("PID", str(proc.pid)),
            ("Name", proc.comm),
            ("Parent", "0 (kernel)" if proc.ppid == 0 else (str(proc.ppid) if eng.process(proc.ppid) else f"{proc.ppid} (gone)")),
            ("State", f"{proc.state}  [{proc.lifecycle}]"),
            ("CPU", f"{proc.cpu_pct:.1f}% of one core (delta/{self.interval:0.2f}s window)"),
            ("RAM", f"{proc.rss / 1024 ** 2:.1f} MB  ({self._pct_ram(proc.rss):.2f}%)"),
            ("VSZ", f"{proc.vsize / 1024 ** 2:.1f} MB"),
            ("Threads", str(proc.num_threads)),
            ("Age", self._age(proc)),
            ("Children", str(len(proc.children))),
            ("User", self._user(pid) or "n/a"),
        ]
        if self.show_cmdline:
            cmd = self._cmdline(pid)
            rows.append(("Cmd", cmd or "n/a"))
        else:
            rows.append(("Cmd", "hidden (press c)"))
        panel_h = len(rows) + 2
        top = max(self._canvas_top(), h - panel_h - 2)
        self._fill_box(screen, top, 0, w - 1, h - 2)
        self._put(screen, top, 1, f" inspector — {proc.comm} ", COL_TITLE, A_BOLD)
        for i, (k, v) in enumerate(rows):
            self._put(screen, top + 1 + i, 1, f"{k:<9} {v}", COL_TEXT)

    def _fill_box(self, screen, top: int, left: int, right: int, bottom: int) -> None:
        for r in range(top, bottom + 1):
            try:
                screen.addstr(r, left, " " * (right - left + 1))
            except curses.error:
                pass

    def _pct_ram(self, rss: int) -> float:
        total = getattr(self.engine, "mem_total", 0)
        if not total:
            return 0.0
        return rss / total * 100.0

    def _user(self, pid: int) -> str | None:
        proc = self.engine.process(pid)
        if proc is None:
            return None
        cached = self._user_cache.get(pid)
        if cached and cached[0] == proc.starttime:
            return cached[1]
        user = None
        reader = getattr(self.collector, "read_status", None)
        if reader is not None:
            status = reader(pid)
            uid = status.get("Uid")
            if uid:
                import pwd

                try:
                    user = pwd.getpwuid(int(uid.split()[0])).pw_name
                except (KeyError, ValueError):
                    user = uid.split()[0]
        self._user_cache[pid] = (proc.starttime, user)
        return user

    def _cmdline(self, pid: int) -> str | None:
        proc = self.engine.process(pid)
        if proc is None:
            return None
        cached = self._cmdline_cache.get(pid)
        if cached and cached[0] == proc.starttime:
            return cached[1]
        text = None
        reader = getattr(self.collector, "read_cmdline", None)
        if reader is not None:
            text = reader(pid)
        self._cmdline_cache[pid] = (proc.starttime, text)
        return text

    def _age(self, proc: ProcessState) -> str:
        seconds = self.engine.age_seconds(proc, self.wall_time)
        if seconds is None:
            return "-"
        return humanize_duration(seconds)

    # --- help ------------------------------------------------------------------------

    def _paint_help(self, screen, h: int, w: int) -> None:
        self._fill(screen, "")
        lines = [
            "PROCESS ECOSYSTEM — keys",
            "",
            "q          quit",
            "p          pause / resume",
            "Tab, 1-4   switch view (ecosystem/tree/timeline/top)",
            "arrows     navigate / pan",
            "Enter      toggle inspector",
            "c          show command line in inspector",
            "+ / -      refresh rate",
            "m / M      ecosystem detail budget",
            "n          show kernel threads",
            "/          filter by name   (Esc clears)",
            "space      collapse/expand in tree view",
            "PgUp/PgDn  scroll tree/top",
            "?          this help",
        ]
        for i, line in enumerate(lines):
            color = COL_ACCENT if i == 0 else COL_TEXT
            self._put(screen, 1 + i, 2, line, color)


def humanize_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
