"""
paths.py — config loader, path resolver, log_error
Proba | NoLaptopTrades
"""

import json
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_FILENAME = "config.json"
APP_DIR = Path(__file__).resolve().parent.parent  # ~/proba/

PATH_MAP = {
    "paper_positions": "paper_positions.jsonl",
    "paper_results":   "paper_results.jsonl",
    "calibration":     "calibration.json",
    "discovery_log":   "discovery_log.jsonl",
    "error_log":       "proba.log",
}


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_config() -> dict:
    """Load config.json, resolve {base_dir} placeholder, return parsed dict."""
    config_path = APP_DIR / CONFIG_FILENAME

    if not config_path.exists():
        example = APP_DIR / "config.example.json"
        if example.exists():
            raise FileNotFoundError(
                f"config.json not found at {config_path}\n"
                f"Copy config.example.json → config.json and fill in your settings."
            )
        raise FileNotFoundError(f"config.json not found at {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = f.read()

    # Parse JSON first — never inject Windows paths (backslashes) into raw JSON
    # text, as C:\Users\... produces invalid \U, \p escape sequences.
    # If the user's config.json still has the literal placeholder "{base_dir}"
    # we temporarily swap it for a safe sentinel, parse, then resolve via pathlib.
    SENTINEL = "__PROBA_BASE_DIR__"
    raw_for_parse = raw.replace("{base_dir}", SENTINEL)

    try:
        cfg = json.loads(raw_for_parse)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"config.json is not valid JSON: {e}\n"
            f"If base_dir contains a Windows path, ensure backslashes are doubled "
            f"(C:\\\\Users\\\\...) or use forward slashes (C:/Users/...)."
        ) from e

    # Resolve base_dir: replace sentinel with APP_DIR, or resolve whatever the
    # user put there (handles ~, forward/back slashes, already-expanded paths).
    raw_base = cfg.get("base_dir", SENTINEL)
    if raw_base == SENTINEL:
        raw_base = str(APP_DIR)
    cfg["base_dir"] = str(Path(raw_base).expanduser().resolve())

    return cfg


def reload_config() -> dict:
    """Force a config reload (clears LRU cache)."""
    get_config.cache_clear()
    return get_config()


# ---------------------------------------------------------------------------
# Path resolver
# ---------------------------------------------------------------------------

def get_path(name: str) -> Path:
    """Return absolute Path for a named data file, creating parent dirs."""
    cfg = get_config()
    base = Path(cfg["base_dir"])
    base.mkdir(parents=True, exist_ok=True)

    if name not in PATH_MAP:
        raise KeyError(f"Unknown path name '{name}'. Valid names: {list(PATH_MAP)}")

    return base / PATH_MAP[name]


# ---------------------------------------------------------------------------
# Dot-notation config reader
# ---------------------------------------------------------------------------

def get_setting(key: str, default=None):
    """
    Read a config value using dot notation.
    Example: get_setting("scorer.min_edge", 0.10)
    """
    cfg = get_config()
    parts = key.split(".")
    node = cfg
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


# ---------------------------------------------------------------------------
# Error logger
# ---------------------------------------------------------------------------

def log_error(component: str, message: str) -> None:
    """Append a timestamped error line to proba.log."""
    try:
        log_path = get_path("error_log")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        line = f"[{ts}] [{component}] {message}\n"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        # Never crash on logging failure — just print
        print(f"[log_error] failed to write: [{component}] {message}")
