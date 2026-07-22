"""
antii_config.py — Anti-Insanity worker registry + manager constants
Proba | NoLaptopTrades

Edit this file to configure mode, thresholds, and worker settings.
Changes take effect on next worker restart ([r] in TUI).
"""

import os
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# ─────────────────────────────────────────────────────────────────
# MODE — one per Termux, never mixed
# "focus"         → politics, geopolitics, economics, crypto only
# "comprehensive" → everything except sports
# ─────────────────────────────────────────────────────────────────
MODE = "focus"

# ─────────────────────────────────────────────────────────────────
# CATEGORIES
# ─────────────────────────────────────────────────────────────────
FOCUS_CATEGORIES = {
    "politics", "political", "geopolitics", "geopolitical",
    "economics", "economy", "macro", "finance", "financial",
    "crypto", "cryptocurrency",
}

BLOCKED_CATEGORIES = {
    "sports", "sport",
}

# ─────────────────────────────────────────────────────────────────
# SIGNAL THRESHOLDS
# ─────────────────────────────────────────────────────────────────

# Minimum absolute price move to flag as overreaction candidate
SIGNAL_MIN_MOVE_1H  = 0.08   # focus: 0.08 / comprehensive: 0.06
SIGNAL_MIN_MOVE_2H  = 0.12   # focus: 0.12 / comprehensive: 0.08

# Minimum volume on the market to consider signal real
SIGNAL_MIN_VOLUME   = 5_000  # USD

# Minimum liquidity
SIGNAL_MIN_LIQ      = 2_000  # USD

# Maximum YES price to fade (above this it's not extreme enough)
SIGNAL_MAX_YES_PRICE = 0.45

# Minimum base rate gap (market price - base rate)
SIGNAL_MIN_GAP      = 0.10   # focus: 0.10 / comprehensive: 0.06

# ─────────────────────────────────────────────────────────────────
# POLLER
# ─────────────────────────────────────────────────────────────────
POLL_INTERVAL_SEC   = 900    # 15 minutes
POLL_LIMIT          = 500    # markets per Gamma fetch
POLL_SORT_BY        = "volume24hr"   # sort descending — most active first

# ─────────────────────────────────────────────────────────────────
# DETECTOR
# ─────────────────────────────────────────────────────────────────
DETECT_INTERVAL_SEC = 60     # how often detector scans new ticks
TICKS_FOR_1H        = 4      # 4 ticks × 15 min = 1h lookback
TICKS_FOR_2H        = 8      # 8 ticks × 15 min = 2h lookback
TICKS_FOR_24H       = 96     # 96 ticks × 15 min = 24h lookback

# ─────────────────────────────────────────────────────────────────
# TRADER
# ─────────────────────────────────────────────────────────────────
POSITION_SIZE_USD       = 40.0
MAX_OPEN_OR             = 10
COOLDOWN_MINUTES        = 90   # simulated cooldown window for shadow comparison

# ─────────────────────────────────────────────────────────────────
# SHADOW (checkpoint writer)
# ─────────────────────────────────────────────────────────────────
SHADOW_INTERVAL_SEC     = 3600          # check every hour
SHADOW_CHECKPOINTS_H    = [24, 48, 72]  # hours after entry to record shadow price

# ─────────────────────────────────────────────────────────────────
# MONITOR
# ─────────────────────────────────────────────────────────────────
MONITOR_INTERVAL_SEC    = 600   # 10 minutes

# ─────────────────────────────────────────────────────────────────
# MANAGER
# ─────────────────────────────────────────────────────────────────
STARTUP_DELAY_SEC       = 1.5
RESTART_COOLDOWN_SEC    = 15
MAX_RESTARTS            = 5
RESTART_RESET_SEC       = 300
ON_CRASH                = "restart+alert"   # restart+alert | restart_only | alert_only | nothing
REFRESH_SEC             = 1.0
LOG_LINES               = 40
LOG_VIEW_INIT           = "poller"

# ─────────────────────────────────────────────────────────────────
# ALERTS (Telegram — optional)
# ─────────────────────────────────────────────────────────────────
ALERT_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALERT_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ─────────────────────────────────────────────────────────────────
# SCRIPTS REGISTRY
# Each entry: name, path, log, key (TUI hotkey), group
# group: "pipeline" | "background"
# background = always-on, unlimited restarts (like Alpha Sniper)
# pipeline   = manual start from TUI
# ─────────────────────────────────────────────────────────────────

_W    = str(_HERE)
_ROOT = _HERE.parent if (_HERE.parent / 'antii').exists() else _HERE.parent.parent

SCRIPTS = [
    {
        "name":  "poller",
        "path":  os.path.join(_W, "poller.py"),
        "log":   str(_ROOT / "logs" / "antii" / "poller.log"),
        "key":   "1",
        "group": "pipeline",
        "desc":  "Gamma tick collector — writes price_tick.jsonl every 15 min",
    },
    {
        "name":  "detector",
        "path":  os.path.join(_W, "detector.py"),
        "log":   str(_ROOT / "logs" / "antii" / "detector.log"),
        "key":   "2",
        "group": "pipeline",
        "desc":  "Price move detector — reads ticks, emits signals",
    },
    {
        "name":  "news",
        "path":  os.path.join(_W, "news.py"),
        "log":   str(_ROOT / "logs" / "antii" / "news.log"),
        "key":   "3",
        "group": "pipeline",
        "desc":  "News fetcher — Adjacent + Finnhub per signal",
    },
    {
        "name":  "scorer",
        "path":  os.path.join(_W, "scorer.py"),
        "log":   str(_ROOT / "logs" / "antii" / "scorer.log"),
        "key":   "4",
        "group": "pipeline",
        "desc":  "OR scorer — base rate + gap + confidence",
    },
    {
        "name":  "trader",
        "path":  os.path.join(_W, "trader.py"),
        "log":   str(_ROOT / "logs" / "antii" / "trader.log"),
        "key":   "5",
        "group": "pipeline",
        "desc":  "Paper trader — opens positions, logs to trade.jsonl",
    },
    {
        "name":  "shadow",
        "path":  os.path.join(_W, "shadow.py"),
        "log":   str(_ROOT / "logs" / "antii" / "shadow.log"),
        "key":   "6",
        "group": "background",
        "desc":  "Shadow logger — 24h/48h/72h price checkpoints",
    },
    {
        "name":  "monitor",
        "path":  os.path.join(_W, "monitor.py"),
        "log":   str(_ROOT / "logs" / "antii" / "monitor.log"),
        "key":   "7",
        "group": "background",
        "desc":  "Position monitor — auto-resolves on Polymarket resolution",
    },
]
