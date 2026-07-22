"""
paths.py — antii path resolver
Anti-Insanity (OR) subsystem | Proba

All paths resolve relative to the proba project root.
Log dir: <project_root>/logs/antii/
Offset dir: <project_root>/logs/antii/.offsets/
"""

import os
import json
from pathlib import Path
from datetime import datetime, timezone

# ── Project root ────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent   # antii/ directory
# Support both layouts:
#   standalone: ~/proba/antii/paths.py  → root = ~/proba/
#   nested:     ~/proba/proba/antii/paths.py → root = ~/proba/
_PROBA_ROOT = _HERE.parent if (_HERE.parent / 'antii').exists() else _HERE.parent.parent

# ── Log directory ────────────────────────────────────────────────────────────
LOG_DIR     = _PROBA_ROOT / "logs" / "antii"
OFFSET_DIR  = LOG_DIR / ".offsets"
SEEN_DIR    = LOG_DIR / ".seen"

# ── Log files (one per component) ────────────────────────────────────────────
LOGS = {
    "tick":       LOG_DIR / "price_tick.jsonl",
    "signal":     LOG_DIR / "signal.jsonl",
    "news":       LOG_DIR / "news.jsonl",
    "score":      LOG_DIR / "score.jsonl",
    "trade":      LOG_DIR / "trade.jsonl",
    "checkpoint": LOG_DIR / "checkpoint.jsonl",
    "error":      LOG_DIR / "error.jsonl",
    "monitor":    LOG_DIR / "monitor.jsonl",
}

# ── Shared paper positions (same file as proba BS, different strategy_type) ──
def get_paper_positions_path() -> Path:
    """Reuse proba's paper_positions.jsonl so --positions shows everything."""
    try:
        from proba.paths import get_path
        return get_path("paper_positions")
    except Exception:
        return _PROBA_ROOT / "paper_positions.jsonl"

# ── Config ───────────────────────────────────────────────────────────────────
def get_config_path() -> Path:
    return _PROBA_ROOT / "config.json"

def load_config() -> dict:
    p = get_config_path()
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def get_antii_config() -> dict:
    """Return antii sub-config with safe defaults."""
    cfg = load_config()
    return cfg.get("antii", {})

# ── Setup ─────────────────────────────────────────────────────────────────────
def ensure_dirs():
    """Create all required directories on first run."""
    for d in (LOG_DIR, OFFSET_DIR, SEEN_DIR):
        d.mkdir(parents=True, exist_ok=True)
    for log_path in LOGS.values():
        if not log_path.exists():
            log_path.touch()

# ── Offset helpers (byte-position tracking for tail readers) ─────────────────
def read_offset(reader_name: str, log_key: str) -> int:
    """Read last known byte offset for a reader+log combo."""
    p = OFFSET_DIR / f"{reader_name}__{log_key}.offset"
    try:
        return int(p.read_text().strip())
    except Exception:
        return 0

def write_offset(reader_name: str, log_key: str, offset: int):
    """Persist byte offset atomically."""
    p   = OFFSET_DIR / f"{reader_name}__{log_key}.offset"
    tmp = str(p) + ".tmp"
    Path(tmp).write_text(str(offset))
    os.replace(tmp, str(p))

# ── Seen-ID helpers (dedup for signal/score consumers) ───────────────────────
def load_seen(reader_name: str) -> set:
    """Load set of already-processed IDs for a reader."""
    p = SEEN_DIR / f"{reader_name}.seen"
    try:
        return set(p.read_text().splitlines())
    except Exception:
        return set()

def save_seen(reader_name: str, seen: set):
    """Persist seen IDs atomically."""
    p   = SEEN_DIR / f"{reader_name}.seen"
    tmp = str(p) + ".tmp"
    Path(tmp).write_text("\n".join(seen))
    os.replace(tmp, str(p))

# ── Logging helpers ───────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def log_error(component: str, msg: str, context: dict = None):
    """Append to error.jsonl. Never raises."""
    try:
        ensure_dirs()
        record = {
            "ts":        _now(),
            "component": component,
            "msg":       msg,
            "context":   context or {},
        }
        with open(LOGS["error"], "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass

def append_log(log_key: str, record: dict):
    """Append a JSON record to a log file. Never raises."""
    try:
        ensure_dirs()
        path = LOGS.get(log_key)
        if not path:
            return
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        log_error("paths", f"append_log failed for {log_key}: {e}")

# ── Tail reader (used by all downstream workers) ──────────────────────────────
class TailReader:
    """
    Reads new lines from a JSONL log file, tracking byte offset.
    Safe to use across restarts — resumes from last offset.

    Usage:
        reader = TailReader("detector", "tick")
        for record in reader.read_new():
            process(record)
        # call reader.save() on clean shutdown
    """

    def __init__(self, reader_name: str, log_key: str):
        self.reader_name = reader_name
        self.log_key     = log_key
        self.log_path    = LOGS[log_key]
        self.offset      = read_offset(reader_name, log_key)

    def read_new(self):
        """Yield new JSON records since last offset. Updates offset in memory."""
        try:
            size = os.path.getsize(self.log_path)
            if size < self.offset:
                # File was rotated or truncated — reset
                self.offset = 0

            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self.offset)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    self.offset = f.tell()
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            pass
        except Exception as e:
            log_error(self.reader_name, f"TailReader error on {self.log_key}: {e}")

    def save(self):
        """Persist current offset to disk."""
        write_offset(self.reader_name, self.log_key, self.offset)
