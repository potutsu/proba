"""
manager.py — Anti-Insanity TUI process manager
Proba | NoLaptopTrades

Run via:  antii   (alias set up by install)
          python -m proba.antii.manager

TUI hotkeys:
  [1-7]      switch log view to that worker
  [r]        restart currently viewed worker (pipeline only)
  [h]        halt / resume currently viewed worker (pipeline only)
  [Ctrl+A]   start all pipeline workers
  [Ctrl+P]   pause (halt) all pipeline workers
  [Ctrl+K]   kill all + quit (confirm required)
  [q]        quit manager + stop all workers
"""

import curses
import json
import os
import platform
import signal
import subprocess
import sys
import time
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

# ── Path bootstrap ────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
# Support both standalone (~/proba/antii/) and nested (~/proba/proba/antii/) layouts
_ROOT = _HERE.parent if (_HERE.parent / 'antii').exists() else _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from antii.antii_config import (
    SCRIPTS, STARTUP_DELAY_SEC, RESTART_COOLDOWN_SEC,
    MAX_RESTARTS, RESTART_RESET_SEC, ON_CRASH,
    REFRESH_SEC, LOG_LINES, LOG_VIEW_INIT,
    ALERT_BOT_TOKEN, ALERT_CHAT_ID, MODE,
)
from antii.paths import ensure_dirs, get_paper_positions_path, LOGS

import requests

PYTHON = sys.executable

GROUP_PIPELINE   = "pipeline"
GROUP_BACKGROUND = "background"

VERSION = "1.0.0"


# ── Globals ────────────────────────────────────────────────────────

state      = {}
state_lock = threading.Lock()

log_view_index = 0

manager_log      = deque(maxlen=50)
manager_log_lock = threading.Lock()

running = True


# ── Helpers ────────────────────────────────────────────────────────

def now_ts() -> float:
    return time.time()

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

def format_uptime(start_ts: float) -> str:
    if start_ts is None:
        return "—"
    sec = int(now_ts() - start_ts)
    if sec < 60:   return f"{sec}s"
    if sec < 3600: return f"{sec // 60}m {sec % 60}s"
    h = sec // 3600
    m = (sec % 3600) // 60
    return f"{h}h {m}m"

def mlog(msg: str):
    with manager_log_lock:
        manager_log.append(f"{now_iso()} {msg}")
    print(f"[manager] {msg}", flush=True)

def send_alert(msg: str):
    if not ALERT_BOT_TOKEN or not ALERT_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{ALERT_BOT_TOKEN}/sendMessage",
            json={"chat_id": ALERT_CHAT_ID, "text": f"🚨 ANTII: {msg}"},
            timeout=5,
        )
    except Exception:
        pass


# ── Stats reader ───────────────────────────────────────────────────

_stats_cache = {
    "signals":    0,
    "traded":     0,
    "open":       0,
    "resolved":   0,
    "correct":    0,
    "win_rate":   0.0,
    "checkpoints": 0,
}
_stats_lock = threading.Lock()

def _read_stats():
    stats = {
        "signals":     0,
        "traded":      0,
        "open":        0,
        "resolved":    0,
        "correct":     0,
        "win_rate":    0.0,
        "checkpoints": 0,
    }

    # Signals
    sig_log = LOGS.get("signal")
    if sig_log and sig_log.exists():
        try:
            stats["signals"] = sum(1 for _ in open(sig_log, "r", errors="replace"))
        except Exception:
            pass

    # Trades
    trade_log = LOGS.get("trade")
    if trade_log and trade_log.exists():
        try:
            for line in open(trade_log, "r", errors="replace"):
                try:
                    r = json.loads(line.strip())
                    if r.get("opened"):
                        stats["traded"] += 1
                except Exception:
                    pass
        except Exception:
            pass

    # Open + resolved positions
    pos_path = get_paper_positions_path()
    if pos_path.exists():
        try:
            for line in open(pos_path, "r", errors="replace"):
                try:
                    pos = json.loads(line.strip())
                    if pos.get("strategy_type") != "overreaction":
                        continue
                    if pos.get("status") == "open":
                        stats["open"] += 1
                    elif pos.get("status") == "closed":
                        stats["resolved"] += 1
                        if pos.get("correct"):
                            stats["correct"] += 1
                except Exception:
                    pass
        except Exception:
            pass

    if stats["resolved"] > 0:
        stats["win_rate"] = round(stats["correct"] / stats["resolved"] * 100, 1)

    # Checkpoints
    cp_log = LOGS.get("checkpoint")
    if cp_log and cp_log.exists():
        try:
            stats["checkpoints"] = sum(1 for _ in open(cp_log, "r", errors="replace"))
        except Exception:
            pass

    return stats


def stats_loop():
    global _stats_cache
    while running:
        try:
            s = _read_stats()
            with _stats_lock:
                _stats_cache.update(s)
        except Exception:
            pass
        time.sleep(15)


# ── Log tail threads ───────────────────────────────────────────────

def tail_log(name: str, log_path: str):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    open(log_path, "a").close()
    buf = deque(maxlen=LOG_LINES * 3)
    pos = 0
    while running:
        try:
            size = os.path.getsize(log_path)
            if size < pos:
                pos = 0
            with open(log_path, "r", errors="replace") as f:
                f.seek(pos)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    stripped = line.strip()
                    if stripped:
                        buf.append(stripped)
                pos = f.tell()
            with state_lock:
                if name in state:
                    state[name]["log_buf"] = list(buf)[-LOG_LINES:]
        except Exception:
            pass
        time.sleep(0.5)


# ── Process management ─────────────────────────────────────────────

def init_state():
    for s in SCRIPTS:
        state[s["name"]] = {
            "proc":          None,
            "pid":           None,
            "status":        "STOPPED",
            "start_ts":      None,
            "restarts":      0,
            "last_crash_ts": None,
            "log_buf":       [],
            "path":          s["path"],
            "log":           s["log"],
            "key":           s["key"],
            "group":         s.get("group", GROUP_PIPELINE),
            "desc":          s.get("desc", ""),
        }


def start_script(name: str):
    with state_lock:
        s        = state[name]
        log_path = s["log"]
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    script_path = state[name]["path"]
    if not os.path.exists(script_path):
        mlog(f"ERROR: {name} not found at {script_path}")
        with state_lock:
            state[name]["status"] = "NOT FOUND"
        return
    try:
        log_file = open(log_path, "a")
        proc     = subprocess.Popen(
            [PYTHON, "-u", script_path],
            stdout=log_file,
            stderr=log_file,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )
        with state_lock:
            state[name]["proc"]     = proc
            state[name]["pid"]      = proc.pid
            state[name]["status"]   = "RUNNING"
            state[name]["start_ts"] = now_ts()
        mlog(f"started {name} (pid={proc.pid})")
    except Exception as e:
        mlog(f"ERROR starting {name}: {e}")
        with state_lock:
            state[name]["status"] = "ERROR"


def stop_script(name: str):
    with state_lock:
        proc = state[name].get("proc")
        state[name]["status"] = "STOPPING"
    if proc and proc.poll() is None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
        except Exception:
            pass
    with state_lock:
        state[name]["proc"]     = None
        state[name]["pid"]      = None
        state[name]["status"]   = "STOPPED"
        state[name]["start_ts"] = None
    mlog(f"stopped {name}")


def restart_script(name: str):
    mlog(f"restarting {name}")
    stop_script(name)
    time.sleep(1)
    start_script(name)


def halt_script(name: str):
    mlog(f"halting {name}")
    stop_script(name)
    with state_lock:
        state[name]["status"] = "HALTED"


def stop_all():
    for s in reversed(SCRIPTS):
        name = s["name"]
        with state_lock:
            status = state[name].get("status")
        if status not in ("STOPPED", "HALTED", "NOT FOUND", "FAILED"):
            stop_script(name)


# ── Monitor thread ─────────────────────────────────────────────────

def monitor_loop():
    while running:
        for s in SCRIPTS:
            name  = s["name"]
            group = s.get("group", GROUP_PIPELINE)
            with state_lock:
                proc     = state[name].get("proc")
                status   = state[name].get("status")
                restarts = state[name].get("restarts", 0)
                start_ts = state[name].get("start_ts")

            if status != "RUNNING" or proc is None:
                continue
            if proc.poll() is None:
                continue

            with state_lock:
                state[name]["status"]        = "CRASHED"
                state[name]["proc"]          = None
                state[name]["pid"]           = None
                state[name]["last_crash_ts"] = now_ts()

            mlog(f"CRASH detected: {name}")

            if start_ts and (now_ts() - start_ts) > RESTART_RESET_SEC:
                with state_lock:
                    state[name]["restarts"] = 0
                restarts = 0

            # Background — always restart
            if group == GROUP_BACKGROUND:
                mlog(f"background {name} crashed — restarting immediately")
                if ON_CRASH in ("restart+alert", "alert_only"):
                    send_alert(f"{name} crashed — restarting (always-on)")
                with state_lock:
                    state[name]["restarts"] = restarts + 1
                start_script(name)
                continue

            # Pipeline — respect MAX_RESTARTS
            if restarts >= MAX_RESTARTS:
                mlog(f"gave up restarting {name} after {MAX_RESTARTS} attempts")
                if ON_CRASH in ("restart+alert", "alert_only"):
                    send_alert(f"{name} crashed {MAX_RESTARTS}x — gave up")
                with state_lock:
                    state[name]["status"] = "FAILED"
                continue

            if ON_CRASH in ("restart+alert", "alert_only"):
                send_alert(f"{name} crashed — restarting")
            if ON_CRASH in ("restart+alert", "restart_only"):
                mlog(f"waiting {RESTART_COOLDOWN_SEC}s before restarting {name}")
                time.sleep(RESTART_COOLDOWN_SEC)
                with state_lock:
                    state[name]["restarts"] = restarts + 1
                start_script(name)

        time.sleep(2)


# ── TUI ────────────────────────────────────────────────────────────

def draw(stdscr):
    global log_view_index, running

    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()

    curses.init_pair(1, curses.COLOR_GREEN,  -1)
    curses.init_pair(2, curses.COLOR_RED,    -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_CYAN,   -1)
    curses.init_pair(5, curses.COLOR_WHITE,  -1)
    curses.init_pair(6, -1,                  -1)

    GREEN  = curses.color_pair(1) | curses.A_BOLD
    RED    = curses.color_pair(2) | curses.A_BOLD
    YELLOW = curses.color_pair(3) | curses.A_BOLD
    CYAN   = curses.color_pair(4) | curses.A_BOLD
    NORMAL = curses.color_pair(5)
    DIM    = curses.color_pair(6)

    script_names = [s["name"] for s in SCRIPTS]
    if LOG_VIEW_INIT in script_names:
        log_view_index = script_names.index(LOG_VIEW_INIT)

    key_to_index = {s["key"]: i for i, s in enumerate(SCRIPTS)}
    kill_confirm    = False
    kill_confirm_ts = 0.0

    def status_color(status):
        if status == "RUNNING":                               return GREEN,  "●"
        if status == "HALTED":                                return YELLOW, "⏸"
        if status in ("CRASHED", "FAILED", "ERROR",
                      "NOT FOUND"):                           return RED,    "✖"
        if status in ("STOPPING", "STOPPED"):                 return YELLOW, "○"
        return NORMAL, "?"

    def safe_add(row, col, text, attr=None):
        h, w = stdscr.getmaxyx()
        # Hard clamp — never write to last 2 rows (footer + buffer)
        if row < 0 or row >= h - 2 or col < 0:
            return
        text = text[:max(0, w - col - 1)].replace("\x00", "")
        try:
            if attr is not None:
                stdscr.addstr(row, col, text, attr)
            else:
                stdscr.addstr(row, col, text)
        except curses.error:
            pass

    def render_script_row(s, row):
        name     = s["name"]
        key_ch   = s["key"]
        group    = s.get("group", GROUP_PIPELINE)
        info     = {}
        with state_lock:
            info = dict(state.get(name, {}))
        status   = info.get("status", "UNKNOWN")
        start_ts = info.get("start_ts")
        restarts = info.get("restarts", 0)
        uptime   = format_uptime(start_ts) if start_ts else "—"
        scol, sym = status_color(status)
        is_active = (name == script_names[log_view_index % len(script_names)])
        row_attr  = CYAN if is_active else NORMAL
        h, w      = stdscr.getmaxyx()

        if row >= h - 3:
            return row

        portrait = w < 72

        if portrait:
            safe_add(row, 0,  f"[{key_ch}] {name:<12} ", row_attr)
            safe_add(row, 17, f"{sym} {status[:7]:<7} ",  scol)
            safe_add(row, 26, uptime[:8],                  NORMAL)
        else:
            safe_add(row, 1,  f"[{key_ch}]",              row_attr)
            safe_add(row, 5,  name[:16],                   row_attr)
            safe_add(row, 22, f"{sym} {status}"[:13],     scol)
            safe_add(row, 36, uptime[:10],                 NORMAL)
            if restarts > 0:
                safe_add(row, 48, f"R:{restarts}", YELLOW)
            if group == GROUP_BACKGROUND:
                safe_add(row, max(52, w - 12), "⚙always-on", DIM)

        return row + 1

    while running:
        with state_lock:
            snap = {n: dict(v) for n, v in state.items()}
        with _stats_lock:
            sc = dict(_stats_cache)

        h, w = stdscr.getmaxyx()
        ts   = datetime.now().strftime("%m-%d %H:%M:%S")
        view_name = script_names[log_view_index % len(script_names)]

        try:
            stdscr.erase()

            if h < 14 or w < 30:
                safe_add(0, 0, "Terminal too small", YELLOW)
                stdscr.refresh()
                time.sleep(REFRESH_SEC)
                continue

            row = 0

            # ── Header ─────────────────────────────────────────
            safe_add(row, 0, "═" * w, CYAN); row += 1
            safe_add(
                row, 0,
                f" ANTII — Anti-Insanity OR  [{MODE.upper()}]  {ts} ".center(w)[:w],
                CYAN,
            ); row += 1
            safe_add(row, 0, "═" * w, CYAN); row += 1

            # ── Pipeline workers ───────────────────────────────
            pipeline = [s for s in SCRIPTS if s.get("group") == GROUP_PIPELINE]
            for s in pipeline:
                row = render_script_row(s, row)

            # ── Background workers ─────────────────────────────
            if row < h - 3:
                safe_add(row, 0, "┄" * w, DIM); row += 1
            background = [s for s in SCRIPTS if s.get("group") == GROUP_BACKGROUND]
            for s in background:
                row = render_script_row(s, row)

            # ── Stats ──────────────────────────────────────────
            if row < h - 4:
                safe_add(row, 0, "─" * w, DIM); row += 1
            if row < h - 3:
                wr_str = f"{sc['win_rate']:.1f}%" if sc["resolved"] > 0 else "—"
                line1  = (
                    f" Signals:{sc['signals']}  "
                    f"Traded:{sc['traded']}  "
                    f"Open:{sc['open']}  "
                    f"Resolved:{sc['resolved']}  "
                    f"WinRate:{wr_str}  "
                    f"Checkpoints:{sc['checkpoints']}"
                )
                safe_add(row, 0, line1[:w], CYAN); row += 1

            # ── Log panel ──────────────────────────────────────
            if row < h - 3:
                safe_add(row, 0, "─" * w, DIM); row += 1
            if row < h - 3:
                desc = ""
                for s in SCRIPTS:
                    if s["name"] == view_name:
                        desc = s.get("desc", "")
                        break
                safe_add(row, 0, f" LOGS [{view_name}] — {desc}"[:w], CYAN); row += 1

            with state_lock:
                log_buf = list(state.get(view_name, {}).get("log_buf", []))
            if not log_buf:
                with manager_log_lock:
                    log_buf = list(manager_log)[-LOG_LINES:]

            max_log = h - row - 3
            for line in log_buf[-max(1, max_log):]:
                if row < h - 3:
                    safe_add(row, 0, (" " + line)[:w - 1], NORMAL); row += 1

            # ── Kill confirm overlay ───────────────────────────
            if kill_confirm:
                elapsed = now_ts() - kill_confirm_ts
                if elapsed > 5:
                    kill_confirm = False
                else:
                    msg = f" KILL ALL? Ctrl+K again to confirm ({int(5-elapsed)}s) "
                    safe_add(h - 3, max(0, (w - len(msg)) // 2), msg[:w], RED)

            # ── Footer ─────────────────────────────────────────
            footer = "[#]view [r]restart [h]halt [C-A]start all [C-P]pause all [C-K]kill [q]quit"
            try:
                stdscr.addstr(h - 1, 0, footer.center(w)[:w - 1], CYAN)
            except curses.error:
                pass

            stdscr.refresh()

        except curses.error:
            try:
                stdscr.clear()
                curses.flushinp()
            except Exception:
                pass

        # ── Input ──────────────────────────────────────────────
        try:
            key = stdscr.getch()
            curses.flushinp()
        except Exception:
            key = -1

        if key == -1:
            time.sleep(REFRESH_SEC)
            continue

        if key == ord("q"):
            running = False
            break

        elif key == 1:   # Ctrl+A — start all pipeline
            def _start_all():
                for s in SCRIPTS:
                    if s.get("group") == GROUP_PIPELINE:
                        with state_lock:
                            st = state[s["name"]].get("status")
                        if st not in ("RUNNING",):
                            start_script(s["name"])
                            time.sleep(STARTUP_DELAY_SEC)
            threading.Thread(target=_start_all, daemon=True).start()
            mlog("Ctrl+A — starting all pipeline workers")

        elif key == 16:  # Ctrl+P — pause all pipeline
            def _pause_all():
                for s in SCRIPTS:
                    if s.get("group") == GROUP_PIPELINE:
                        with state_lock:
                            st = state[s["name"]].get("status")
                        if st == "RUNNING":
                            halt_script(s["name"])
            threading.Thread(target=_pause_all, daemon=True).start()
            mlog("Ctrl+P — halting all pipeline workers")

        elif key == 11:  # Ctrl+K — kill all confirm
            if kill_confirm and (now_ts() - kill_confirm_ts) < 5:
                running = False
                break
            else:
                kill_confirm    = True
                kill_confirm_ts = now_ts()

        elif key == ord("r"):
            name  = script_names[log_view_index % len(script_names)]
            group = next((s.get("group") for s in SCRIPTS if s["name"] == name), GROUP_PIPELINE)
            if group == GROUP_BACKGROUND:
                mlog(f"[TUI] {name} is always-on — use Ctrl+K to stop")
            else:
                threading.Thread(target=restart_script, args=(name,), daemon=True).start()

        elif key == ord("h"):
            name  = script_names[log_view_index % len(script_names)]
            group = next((s.get("group") for s in SCRIPTS if s["name"] == name), GROUP_PIPELINE)
            if group == GROUP_BACKGROUND:
                mlog(f"[TUI] {name} is always-on — cannot halt")
            else:
                with state_lock:
                    cur = state[name].get("status")
                if cur == "HALTED":
                    threading.Thread(target=start_script, args=(name,), daemon=True).start()
                    mlog(f"resuming {name}")
                elif cur == "RUNNING":
                    threading.Thread(target=halt_script, args=(name,), daemon=True).start()

        else:
            char = chr(key) if 0 <= key < 256 else ""
            if char in key_to_index:
                log_view_index = key_to_index[char]

        time.sleep(REFRESH_SEC)


# ── Entry point ────────────────────────────────────────────────────

def main():
    global running

    ensure_dirs()

    print(f"ANTII v{VERSION} — Anti-Insanity OR Manager  [mode={MODE}]")
    print("Starting background workers, pipeline workers start manually in TUI.")
    print()

    init_state()

    # Start log tail threads for all workers
    for s in SCRIPTS:
        threading.Thread(
            target=tail_log,
            args=(s["name"], s["log"]),
            daemon=True,
        ).start()

    # Stats thread
    threading.Thread(target=stats_loop,   daemon=True).start()

    # Monitor thread (crash detection + auto-restart)
    threading.Thread(target=monitor_loop, daemon=True).start()

    # Auto-start background workers (shadow + monitor)
    for s in SCRIPTS:
        if s.get("group") == GROUP_BACKGROUND:
            threading.Thread(
                target=start_script,
                args=(s["name"],),
                daemon=True,
            ).start()
            mlog(f"auto-started background: {s['name']}")
            time.sleep(0.3)

    # Launch TUI
    try:
        curses.wrapper(draw)
    except KeyboardInterrupt:
        pass
    finally:
        running = False
        print("\nShutting down antii workers...")
        stop_all()
        print("Done.")


if __name__ == "__main__":
    main()
