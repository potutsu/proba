"""
detector.py — price move detector
Anti-Insanity | Proba

Tails price_tick.jsonl. For each market, maintains a rolling price
history (in memory). When a market's price moves >= threshold in
1h or 2h window, emits a signal to signal.jsonl.

Dedup: one signal per market per 4-hour window. Tracked by
market_id in .seen file + in-memory set. Signal IDs are
market_id + hour bucket so restarts don't re-emit stale signals.

Worker: runs as subprocess managed by antii manager.
"""

import json
import sys
import time
import uuid
import signal as _signal
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# Support both standalone (~/proba/antii/) and nested (~/proba/proba/antii/) layouts
_ROOT = _HERE.parent if (_HERE.parent / 'antii').exists() else _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from paths import (
    ensure_dirs, append_log, log_error,
    TailReader, load_seen, save_seen,
)
from antii_config import (
    DETECT_INTERVAL_SEC,
    TICKS_FOR_1H, TICKS_FOR_2H, TICKS_FOR_24H,
    SIGNAL_MIN_MOVE_1H, SIGNAL_MIN_MOVE_2H,
    SIGNAL_MIN_VOLUME, SIGNAL_MIN_LIQ,
    SIGNAL_MAX_YES_PRICE, MODE,
)
from base_rate import estimate_base_rate, price_gap

# ── Noise blocklist ────────────────────────────────────────────────
# Markets that move mechanically, not from emotional overreaction.
# OR signal is meaningless on these — price moves are predictable/scheduled.
_NOISE_PATTERNS = [
    "tweets",           # Elon/anyone tweet count buckets
    "tweet",
    "posts from",       # social media post count markets
    "post 1", "post 2", "post 3",
    "cs2 map",          # esports map-by-map
    "map 1", "map 2", "map 3",
    "set 1", "set 2", "set 3",   # tennis sets
    "quarter 1", "quarter 2",      # sports quarters
    "half 1", "half 2",
    "inning",
    "round 1", "round 2",
    "hole ",            # golf holes
    "lap ",             # racing laps
]

def _is_noise(title: str) -> bool:
    t = title.lower()
    return any(p in t for p in _NOISE_PATTERNS)

_RUNNING = True

def _handle_sig(sig, frame):
    global _RUNNING
    _RUNNING = False

_signal.signal(_signal.SIGTERM, _handle_sig)
_signal.signal(_signal.SIGINT,  _handle_sig)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def _hour_bucket() -> str:
    """4-hour bucket string for dedup window."""
    now = datetime.now(timezone.utc)
    bucket = (now.hour // 4) * 4
    return now.strftime(f"%Y-%m-%d-{bucket:02d}")


# ── Price history store ────────────────────────────────────────────
# market_id → deque of (ts_str, yes_price, volume) tuples
# Keep last TICKS_FOR_24H + buffer ticks per market
_MAX_TICKS = TICKS_FOR_24H + 10

_price_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=_MAX_TICKS))


def _ingest_tick(tick: dict):
    """Add a tick to in-memory price history."""
    mid   = tick.get("market_id")
    price = tick.get("yes_price")
    if not mid or price is None:
        return
    _price_history[mid].append((
        tick.get("ts", ""),
        float(price),
        float(tick.get("volume", 0)),
        float(tick.get("liquidity", 0)),
        tick,   # full tick stored for signal enrichment
    ))


def _compute_moves(mid: str) -> dict | None:
    """
    Compute price moves from tick history.
    Also uses Gamma's native change_24h field when available.
    Returns move dict or None if not enough data.
    """
    hist = _price_history.get(mid)
    if not hist or len(hist) < 1:
        return None

    hist_list  = list(hist)
    now_entry  = hist_list[-1]
    price_now  = now_entry[1]
    vol_now    = now_entry[2]
    liq_now    = now_entry[3]
    tick       = now_entry[4]

    # Use Gamma's native 24h change if available (most reliable)
    change_24h = tick.get("change_24h")

    # Compute from history for 1h and 2h
    def _price_n_ago(n):
        idx = len(hist_list) - 1 - n
        return hist_list[idx][1] if idx >= 0 else None

    price_1h  = _price_n_ago(TICKS_FOR_1H)
    price_2h  = _price_n_ago(TICKS_FOR_2H)
    price_24h = _price_n_ago(TICKS_FOR_24H)

    move_1h  = round(price_now - price_1h,  4) if price_1h  is not None else None
    move_2h  = round(price_now - price_2h,  4) if price_2h  is not None else None
    # Prefer Gamma native 24h change; fall back to computed
    move_24h = round(float(change_24h), 4) if change_24h is not None else (
               round(price_now - price_24h, 4) if price_24h is not None else None)

    # Volume spike ratio
    vol_24h_ago = _price_n_ago(TICKS_FOR_24H)
    vol_spike   = None
    if vol_24h_ago and vol_24h_ago > 0:
        vol_spike = round(vol_now / float(tick.get("volume", 1) or 1), 2)

    return {
        "price_now":       round(price_now, 4),
        "price_1h":        round(price_1h,  4) if price_1h  is not None else None,
        "price_2h":        round(price_2h,  4) if price_2h  is not None else None,
        "price_24h":       round(price_24h, 4) if price_24h is not None else None,
        "move_1h":         move_1h,
        "move_2h":         move_2h,
        "move_24h":        move_24h,
        "vol_now":         vol_now,
        "vol_spike_ratio": vol_spike,
        "best_bid":        tick.get("best_bid"),
        "best_ask":        tick.get("best_ask"),
        "spread":          tick.get("spread"),
        "volume_24h":      float(tick.get("volume_24h", 0) or 0),
    }


def _check_signal(mid: str, tick: dict, moves: dict, seen: set) -> dict | None:
    """
    Determine if a signal should be emitted for this market.

    Key change: move_24h from Gamma's oneDayPriceChange is always available
    from first tick — we don't need N ticks of history to detect OR.
    Signal fires if move_24h OR (move_1h/move_2h from accumulated history) passes threshold.
    """
    price_now  = moves["price_now"]
    move_1h    = moves["move_1h"]
    move_2h    = moves["move_2h"]
    move_24h   = moves["move_24h"]
    vol_now    = moves["vol_now"]
    liq        = float(tick.get("liquidity", 0) or 0)

    # ── Gate: noise filter ────────────────────────────────────────
    title = tick.get("title", "")
    if _is_noise(title):
        return None

    # ── Primary signal: Gamma native 24h change ────────────────────
    # This works from tick 1, no history needed
    primary_move = None
    if move_24h is not None and abs(move_24h) >= SIGNAL_MIN_MOVE_2H:
        primary_move = move_24h
    elif move_1h is not None and abs(move_1h) >= SIGNAL_MIN_MOVE_1H:
        primary_move = move_1h
    elif move_2h is not None and abs(move_2h) >= SIGNAL_MIN_MOVE_1H:
        primary_move = move_2h

    if primary_move is None:
        return None

    # ── Only fade upward moves (YES price pumped) ──────────────────
    if primary_move <= 0:
        return None

    # ── YES price must be in fadeable range ───────────────────────
    if price_now > SIGNAL_MAX_YES_PRICE:
        return None

    # ── Volume + liquidity gates ──────────────────────────────────
    vol_24h = moves.get("volume_24h", 0) or vol_now
    if vol_24h < SIGNAL_MIN_VOLUME and vol_now < SIGNAL_MIN_VOLUME:
        return None
    if liq < SIGNAL_MIN_LIQ:
        return None

    # ── Base rate ─────────────────────────────────────────────────
    base_rate = estimate_base_rate(title)
    gap       = price_gap(price_now, base_rate) if base_rate is not None else None

    # ── Dedup: one signal per market per 4-hour bucket ────────────
    dedup_key = f"{mid}__{_hour_bucket()}"
    if dedup_key in seen:
        return None

    signal_id = str(uuid.uuid4())[:16]

    return {
        "signal_id":       signal_id,
        "dedup_key":       dedup_key,
        "ts":              _ts(),
        "mode":            MODE,

        "market_id":       mid,
        "yes_token_id":    tick.get("yes_token_id"),
        "no_token_id":     tick.get("no_token_id"),
        "title":           title,
        "event_title":     tick.get("event_title", ""),
        "category":        tick.get("category", "unknown"),
        "tag_slugs":       tick.get("tag_slugs", []),
        "days_to_close":   tick.get("days_to_close", 0),
        "end_date":        tick.get("end_date", ""),

        "price_now":       moves["price_now"],
        "price_1h_ago":    moves["price_1h"],
        "price_2h_ago":    moves["price_2h"],
        "price_24h_ago":   moves["price_24h"],

        "move_1h":         move_1h,
        "move_2h":         move_2h,
        "move_24h":        move_24h,
        "primary_move":    round(primary_move, 4),

        "best_bid":        moves.get("best_bid"),
        "best_ask":        moves.get("best_ask"),
        "spread":          moves.get("spread"),

        "volume":          vol_now,
        "volume_24h":      vol_24h,
        "liquidity":       liq,
        "vol_spike_ratio": moves["vol_spike_ratio"],

        "base_rate":       base_rate,
        "base_rate_gap":   round(gap, 4) if gap is not None else None,

        "news_headlines":  [],
        "news_context":    "",
        "traded":          False,
        "skip_reason":     "",
    }


# ── Main loop ──────────────────────────────────────────────────────

def run():
    ensure_dirs()

    # ── Offset reset flag ─────────────────────────────────────────
    # Pass --reset to reprocess all ticks from the beginning.
    # Use after changing thresholds or noise filters.
    reset = "--reset" in sys.argv
    if reset:
        from paths import OFFSET_DIR, SEEN_DIR
        offset_file = OFFSET_DIR / "detector__tick.offset"
        seen_file   = SEEN_DIR / "detector.seen"
        if offset_file.exists():
            offset_file.unlink()
            print(f"[{_ts()}] [detector] reset: cleared tick offset", flush=True)
        if seen_file.exists():
            seen_file.unlink()
            print(f"[{_ts()}] [detector] reset: cleared seen signals", flush=True)

    print(f"[{_ts()}] [detector] starting — interval={DETECT_INTERVAL_SEC}s reset={reset}", flush=True)

    reader = TailReader("detector", "tick")
    seen   = load_seen("detector")

    signals_emitted = 0
    ticks_ingested  = 0

    while _RUNNING:
        # Ingest new ticks
        new_ticks = 0
        for tick in reader.read_new():
            _ingest_tick(tick)
            new_ticks += 1
            ticks_ingested += 1
        reader.save()

        # Check for signals across all markets with history
        new_signals = 0
        for mid, hist in list(_price_history.items()):
            if not _RUNNING:
                break
            if len(hist) < 2:
                continue

            latest_tick = hist[-1][4]   # full tick stored at index 4
            moves = _compute_moves(mid)
            if moves is None:
                continue

            sig = _check_signal(mid, latest_tick, moves, seen)
            if sig:
                append_log("signal", sig)
                seen.add(sig["dedup_key"])
                new_signals   += 1
                signals_emitted += 1
                print(
                    f"[{_ts()}] [detector] SIGNAL {sig['signal_id']} "
                    f"'{sig['title'][:45]}' "
                    f"move_1h={sig['move_1h']} move_2h={sig['move_2h']} "
                    f"price={sig['price_now']}",
                    flush=True,
                )

        if new_ticks > 0 or new_signals > 0:
            print(
                f"[{_ts()}] [detector] "
                f"new_ticks={new_ticks} new_signals={new_signals} "
                f"total_markets={len(_price_history)} "
                f"total_signals={signals_emitted}",
                flush=True,
            )

        for _ in range(DETECT_INTERVAL_SEC):
            if not _RUNNING:
                break
            time.sleep(1)

    save_seen("detector", seen)
    reader.save()
    print(f"[{_ts()}] [detector] stopped cleanly", flush=True)


if __name__ == "__main__":
    run()
