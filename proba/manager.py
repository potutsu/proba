"""
manager.py — Proba TUI
Proba | NoLaptopTrades

Layout (inspired by Alpha Sniper manager):

  ┌──────────────────────────────────────────────────────────┐
  │ PROBA  signals:5  positions:3  traded:12  cycle:14:23:01 │
  ├─────────────────────────┬────────────────────────────────┤
  │ [d]iscovery  ● RUNNING  │ [p]ositions  [s]ignals         │
  │ [m]onitor    ● RUNNING  │                                 │
  │                         │  (log panel — selected source) │
  │ Phase 0: ████░░ 3/50    │                                 │
  │ Win rate: 66.7%         │                                 │
  │                         │                                 │
  ├─────────────────────────┴────────────────────────────────┤
  │ [d]discovery [p]positions [s]signals [m]monitor [q]quit  │
  └──────────────────────────────────────────────────────────┘

Keys:
  d — view discovery log
  p — view open positions
  s — view last signals (scored candidates)
  m — view monitor log
  q — quit
"""

import curses
import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import List

from proba.paths import get_config, get_path, log_error
from proba import discovery as disc_module
from proba.paper_logger import list_open_positions
from proba.postmortem import get_calibration_summary
from proba.monitor import check_positions

MAX_LOG   = 300
REFRESH   = 0.5

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class State:
    def __init__(self):
        self.lock        = threading.Lock()
        self.running     = True
        self.view        = "d"   # d=discovery p=positions s=signals m=monitor

        self.disc_log    = deque(maxlen=MAX_LOG)
        self.mon_log     = deque(maxlen=MAX_LOG)
        self.signal_log  = deque(maxlen=MAX_LOG)

        self.positions   = []
        self.cal         = {}

        # Stats bar
        self.signal_count  = 0
        self.trade_count   = 0
        self.open_count    = 0
        self.alert_count   = 0
        self.cycle_ts      = ""
        self.cycle_num     = 0

        # Thread health
        self.disc_alive  = False
        self.mon_alive   = False

    def add(self, log: deque, msg: str):
        with self.lock:
            log.append(msg)

    def refresh_positions(self):
        try:
            pos = list_open_positions()
            cal = get_calibration_summary()
            with self.lock:
                self.positions  = pos
                self.open_count = len(pos)
                self.cal        = cal
        except Exception:
            pass


def _ts():
    return datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# Background threads
# ---------------------------------------------------------------------------

def _disc_status(state: State, msg: str):
    state.add(state.disc_log, msg)


def _disc_cycle_done(state: State, scored: List, logged: List):
    with state.lock:
        state.signal_count  = len(scored)
        state.trade_count  += len(logged)
        state.cycle_ts      = _ts()
        state.cycle_num    += 1

    state.refresh_positions()

    if scored:
        state.add(state.signal_log, f"── {_ts()}  cycle {state.cycle_num}: {len(scored)} signals ──")
        for s in scored[:15]:
            src  = "PM" if s.get("source") == "polymarket" else "FT"
            icon = "▲" if s.get("signal") == "BUY_YES" else "▼"
            conf = {"high":"HI","medium":"MD","low":"LO"}.get(s.get("confidence",""),"??")
            edge = s.get("edge", 0)
            fill = s.get("fill_price", s.get("market_price", 0))
            state.add(state.signal_log,
                      f"  {icon}[{src}][{conf}] {s.get('title','')[:42]}")
            state.add(state.signal_log,
                      f"     {s.get('zone','?'):<22}  fill={fill:.2%}  edge={edge:+.2%}")
        if logged:
            state.add(state.signal_log, f"  ✓ AUTO-TRADED {len(logged)} position(s)")
            for pos in logged:
                src = "PM" if pos.get("source")=="polymarket" else "FT"
                state.add(state.signal_log,
                          f"    [{src}] {pos.get('title','')[:50]}")
    else:
        state.add(state.signal_log, f"── {_ts()}  cycle {state.cycle_num}: no signals ──")


def _monitor_loop(state: State, cfg: dict):
    state.mon_alive = True
    interval = cfg.get("monitor", {}).get("check_interval_sec", 600)
    state.add(state.mon_log, f"[monitor] {_ts()}  ready  interval={interval}s")

    while state.running:
        try:
            state.refresh_positions()
            positions = state.positions
            if positions:
                state.add(state.mon_log,
                          f"[monitor] {_ts()}  checking {len(positions)} position(s)...")
                alerts = check_positions(positions, cfg)
                if alerts:
                    state.alert_count += len(alerts)
                    for a in alerts:
                        state.add(state.mon_log,
                                  f"[monitor] {_ts()}  ⚠ {a['alert_type']} — {a.get('title','')[:40]}")
                else:
                    state.add(state.mon_log,
                              f"[monitor] {_ts()}  all positions nominal")
            else:
                state.add(state.mon_log,
                          f"[monitor] {_ts()}  no open positions")
        except Exception as e:
            log_error("monitor", str(e))
            state.add(state.mon_log, f"[monitor] {_ts()}  error: {e}")

        for _ in range(interval):
            if not state.running:
                break
            time.sleep(1)

    state.mon_alive = False


# ---------------------------------------------------------------------------
# TUI
# ---------------------------------------------------------------------------

def _safe(stdscr, row, col, text, attr=0):
    try:
        h, w = stdscr.getmaxyx()
        if 0 <= row < h - 1 and 0 <= col < w - 1:
            stdscr.addstr(row, col, str(text)[:max(0, w - col - 1)], attr)
    except curses.error:
        pass


def _draw(stdscr, state: State):
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN,    -1)  # title / header
    curses.init_pair(2, curses.COLOR_GREEN,   -1)  # good / buy
    curses.init_pair(3, curses.COLOR_YELLOW,  -1)  # warn / sell
    curses.init_pair(4, curses.COLOR_RED,     -1)  # error / alert
    curses.init_pair(5, curses.COLOR_WHITE,   -1)  # normal
    curses.init_pair(6, curses.COLOR_MAGENTA, -1)  # accent / stats

    TITLE  = curses.color_pair(1) | curses.A_BOLD
    GOOD   = curses.color_pair(2)
    WARN   = curses.color_pair(3)
    ERR    = curses.color_pair(4)
    NORM   = curses.color_pair(5)
    ACC    = curses.color_pair(6)
    DIM    = curses.color_pair(5) | curses.A_DIM
    BOLD   = curses.color_pair(5) | curses.A_BOLD

    while state.running:
        key = stdscr.getch()
        if key == ord('q'):
            state.running = False
            break
        elif key == ord('d'):
            state.view = "d"
        elif key == ord('p'):
            state.view = "p"
        elif key == ord('s'):
            state.view = "s"
        elif key == ord('m'):
            state.view = "m"

        h, w = stdscr.getmaxyx()
        stdscr.erase()

        if h < 8 or w < 40:
            _safe(stdscr, 0, 0, "Terminal too small", WARN)
            stdscr.refresh()
            time.sleep(REFRESH)
            continue

        # ── LEFT PANEL width ──────────────────────────────────────────────
        LEFT_W = min(26, w // 3)
        RIGHT_START = LEFT_W + 1
        RIGHT_W = w - RIGHT_START

        with state.lock:
            sc   = state.signal_count
            tc   = state.trade_count
            oc   = state.open_count
            ac   = state.alert_count
            cts  = state.cycle_ts
            cn   = state.cycle_num
            view = state.view
            cal  = dict(state.cal)
            pos  = list(state.positions)
            disc_alive = state.disc_alive
            mon_alive  = state.mon_alive

        # ── Title bar ─────────────────────────────────────────────────────
        title = f" PROBA  sig:{sc}  pos:{oc}  traded:{tc}  alerts:{ac}  {cts} "
        _safe(stdscr, 0, 0, title[:w-1], TITLE)
        _safe(stdscr, 1, 0, "─" * (w-1), NORM)

        # ── Left panel ────────────────────────────────────────────────────
        row = 2

        # Thread status
        d_sym = "●" if disc_alive else "○"
        m_sym = "●" if mon_alive  else "○"
        d_col = GOOD if disc_alive else WARN
        m_col = GOOD if mon_alive  else WARN

        _safe(stdscr, row, 0, f" [d]iscovery ", BOLD if view=="d" else NORM)
        _safe(stdscr, row, 13, f"{d_sym} {'ON' if disc_alive else 'OFF'}", d_col)
        row += 1

        _safe(stdscr, row, 0, f" [m]onitor   ", BOLD if view=="m" else NORM)
        _safe(stdscr, row, 13, f"{m_sym} {'ON' if mon_alive else 'OFF'}", m_col)
        row += 1
        row += 1

        # View selector
        _safe(stdscr, row, 0, " View:", DIM)
        row += 1
        for vkey, vlabel in [("d","Discovery"),("s","Signals"),("p","Positions"),("m","Monitor")]:
            attr = GOOD if view == vkey else DIM
            _safe(stdscr, row, 1, f"[{vkey}] {vlabel}", attr)
            row += 1
        row += 1

        # Calibration
        _safe(stdscr, row, 0, "─" * (LEFT_W-1), DIM)
        row += 1
        prog   = cal.get("phase0_progress", 0)
        target = cal.get("phase0_target", 50)
        wr     = cal.get("win_rate", 0.0)
        filled = int((LEFT_W - 4) * prog / max(target, 1))
        bar_w  = LEFT_W - 4
        _safe(stdscr, row, 0, f" Phase 0: {prog}/{target}", NORM)
        row += 1
        _safe(stdscr, row, 1,
              "[" + "█" * filled + "░" * (bar_w - filled) + "]", ACC)
        row += 1
        _safe(stdscr, row, 0, f" Win rate: {wr:.1%}", GOOD if wr >= 0.6 else NORM)
        row += 1
        row += 1

        # Open positions summary (left panel)
        _safe(stdscr, row, 0, "─" * (LEFT_W-1), DIM)
        row += 1
        _safe(stdscr, row, 0, f" Open positions: {len(pos)}", NORM)
        row += 1
        for p in pos[:min(3, h - row - 4)]:
            src  = "PM" if p.get("source")=="polymarket" else "FT"
            icon = "▲" if p.get("direction")=="yes" else "▼"
            fill = p.get("fill_price", p.get("entry_price", 0))
            _safe(stdscr, row, 1,
                  f"{icon}[{src}] {p.get('title','')[:LEFT_W-7]}",
                  GOOD if icon=="▲" else WARN)
            row += 1
            if row >= h - 3:
                break

        if len(pos) > 3:
            _safe(stdscr, row, 1, f"... +{len(pos)-3} more", DIM)

        # ── Vertical divider ──────────────────────────────────────────────
        for r in range(2, h - 1):
            _safe(stdscr, r, LEFT_W, "│", NORM)

        # ── Right panel header ────────────────────────────────────────────
        view_labels = {
            "d": "DISCOVERY LOG",
            "s": "LAST SIGNALS",
            "p": "OPEN POSITIONS",
            "m": "MONITOR LOG",
        }
        header = f" {view_labels.get(view, '')} "
        _safe(stdscr, 2, RIGHT_START, header + "─" * max(0, RIGHT_W - len(header) - 1), TITLE)

        # ── Right panel content ───────────────────────────────────────────
        content_rows = h - 4  # rows 3 .. h-2

        if view == "d":
            with state.lock:
                lines = list(state.disc_log)
            lines = lines[-(content_rows):]
            for i, line in enumerate(lines):
                row = 3 + i
                if row >= h - 1:
                    break
                lo = line.lower()
                if any(x in lo for x in ["error","failed","timeout"]):
                    attr = ERR
                elif "auto-traded" in lo or "signal" in lo:
                    attr = GOOD
                elif "scanning" in lo or "fetching" in lo:
                    attr = DIM
                else:
                    attr = NORM
                _safe(stdscr, row, RIGHT_START, line, attr)

        elif view == "s":
            with state.lock:
                lines = list(state.signal_log)
            lines = lines[-(content_rows):]
            for i, line in enumerate(lines):
                row = 3 + i
                if row >= h - 1:
                    break
                if "▲" in line:
                    attr = GOOD
                elif "▼" in line:
                    attr = WARN
                elif "✓" in line:
                    attr = GOOD | curses.A_BOLD
                elif "──" in line:
                    attr = TITLE
                else:
                    attr = NORM
                _safe(stdscr, row, RIGHT_START, line, attr)

        elif view == "p":
            # Full positions detail
            row = 3
            if not pos:
                _safe(stdscr, row, RIGHT_START, "  No open positions.", DIM)
            else:
                for p in pos:
                    if row >= h - 1:
                        break
                    src  = "PM" if p.get("source")=="polymarket" else "FT"
                    icon = "▲" if p.get("direction")=="yes" else "▼"
                    fill = p.get("fill_price", p.get("entry_price", 0))
                    edge = p.get("edge", 0)
                    conf = p.get("confidence","?")[0].upper()
                    zone = p.get("zone","?")
                    closes = (p.get("close_date","") or "")[:10]
                    title  = p.get("title","")[:RIGHT_W - 14]
                    attr   = GOOD if icon=="▲" else WARN
                    _safe(stdscr, row, RIGHT_START,
                          f" {icon}[{src}][{conf}] {title}", attr)
                    row += 1
                    if row < h - 1:
                        _safe(stdscr, row, RIGHT_START,
                              f"   fill={fill:.2%}  edge={edge:+.2%}  {zone}  {closes}", NORM)
                        row += 1
                    if row < h - 1:
                        _safe(stdscr, row, RIGHT_START, "", NORM)
                        row += 1

        elif view == "m":
            with state.lock:
                lines = list(state.mon_log)
            lines = lines[-(content_rows):]
            for i, line in enumerate(lines):
                row = 3 + i
                if row >= h - 1:
                    break
                attr = ERR if "⚠" in line or "error" in line.lower() else NORM
                _safe(stdscr, row, RIGHT_START, line, attr)

        # ── Footer ────────────────────────────────────────────────────────
        footer = " [d]discovery  [s]signals  [p]positions  [m]monitor  [q]quit "
        _safe(stdscr, h - 1, 0, footer[:w-1], TITLE)

        stdscr.refresh()
        time.sleep(REFRESH)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_tui():
    cfg   = get_config()
    state = State()
    state.refresh_positions()

    # Discovery thread
    def on_status(msg):
        state.disc_alive = True
        state.add(state.disc_log, msg)

    def on_cycle(scored, logged):
        _disc_cycle_done(state, scored, logged)

    disc_thread = threading.Thread(
        target=disc_module.loop,
        args=(on_status, on_cycle),
        daemon=True, name="discovery",
    )

    mon_thread = threading.Thread(
        target=_monitor_loop,
        args=(state, cfg),
        daemon=True, name="monitor",
    )

    disc_thread.start()
    mon_thread.start()

    try:
        curses.wrapper(lambda s: _draw(s, state))
    except KeyboardInterrupt:
        pass
    finally:
        state.running = False
        disc_module.stop()
