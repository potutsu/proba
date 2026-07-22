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
sys.path.insert(0, str(_HERE.parent.parent))

from proba.antii.paths import (
    ensure_dirs, append_log, log_error,
    TailReader, load_seen, save_seen,
)
from proba.antii.antii_config import (
    DETECT_INTERVAL_SEC,
    TICKS_FOR_1H, TICKS_FOR_2H, TICKS_FOR_24H,
    SIGNAL_MIN_MOVE_1H, SIGNAL_MIN_MOVE_2H,
    SIGNAL_MIN_VOLUME, SIGNAL_MIN_LIQ,
    SIGNAL_MAX_YES_PRICE, MODE,
)
from proba.antii.base_rate import estimate_base_rate, price_gap

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


def _compute_moves(mid: str):
    """
    Return (move_1h, move_2h, move_24h, price_now, price_1h, price_2h,
            price_24h, vol_now, vol_spike_ratio) or None if not enough data.
    """
    hist = _price_history.get(mid)
    if not hist or len(hist) < 2:
        return None

    hist_list = list(hist)  # oldest → newest
    now_entry = hist_list[-1]
    price_now = now_entry[1]
    vol_now   = now_entry[2]

    def _price_n_ago(n):
        """Price n ticks ago, or None if not enough history."""
        idx = len(hist_list) - 1 - n
        if idx < 0:
            return None
        return hist_list[idx][1]

    def _vol_n_ago(n):
        idx = len(hist_list) - 1 - n
        if idx < 0:
            return None
        return hist_list[idx][2]

    price_1h  = _price_n_ago(TICKS_FOR_1H)
    price_2h  = _price_n_ago(TICKS_FOR_2H)
    price_24h = _price_n_ago(TICKS_FOR_24H)
    vol_24h_ago = _vol_n_ago(TICKS_FOR_24H)

    move_1h  = round(price_now - price_1h,  4) if price_1h  is not None else None
    move_2h  = round(price_now - price_2h,  4) if price_2h  is not None else None
    move_24h = round(price_now - price_24h, 4) if price_24h is not None else None

    # Volume spike: ratio of current to 24h-ago volume
    vol_spike = None
    if vol_24h_ago and vol_24h_ago > 0:
        vol_spike = round(vol_now / vol_24h_ago, 2)

    return {
        "price_now":   round(price_now, 4),
        "price_1h":    round(price_1h,  4) if price_1h  is not None else None,
        "price_2h":    round(price_2h,  4) if price_2h  is not None else None,
        "price_24h":   round(price_24h, 4) if price_24h is not None else None,
        "move_1h":     move_1h,
        "move_2h":     move_2h,
        "move_24h":    move_24h,
        "vol_now":     vol_now,
        "vol_spike_ratio": vol_spike,
    }


def _check_signal(mid: str, tick: dict, moves: dict, seen: set) -> dict | None:
    """
    Given a market's current tick and move data, determine if a
    signal should be emitted. Returns signal dict or None.
    """
    price_now = moves["price_now"]
    move_1h   = moves["move_1h"]
    move_2h   = moves["move_2h"]
    vol_now   = moves["vol_now"]
    liq       = float(tick.get("liquidity", 0))

    # ── Gate: upward move only (fading pumps, not crashes) ────────
    best_move = max(
        abs(move_1h)  if move_1h  is not None else 0,
        abs(move_2h)  if move_2h  is not None else 0,
    )
    if best_move == 0:
        return None

    # Only fade upward moves (YES price pumped)
    primary_move = move_1h if move_1h is not None else move_2h
    if primary_move <= 0:
        return None

    # ── Gate: move threshold ───────────────────────────────────────
    passes_1h = move_1h is not None and abs(move_1h) >= SIGNAL_MIN_MOVE_1H
    passes_2h = move_2h is not None and abs(move_2h) >= SIGNAL_MIN_MOVE_2H
    if not passes_1h and not passes_2h:
        return None

    # ── Gate: volume + liquidity ───────────────────────────────────
    if vol_now < SIGNAL_MIN_VOLUME:
        return None
    if liq < SIGNAL_MIN_LIQ:
        return None

    # ── Gate: YES price in fadeable range ─────────────────────────
    if price_now > SIGNAL_MAX_YES_PRICE:
        return None

    # ── Base rate ─────────────────────────────────────────────────
    title     = tick.get("title", "")
    base_rate = estimate_base_rate(title)
    gap       = price_gap(price_now, base_rate) if base_rate is not None else None

    # ── Dedup: one signal per market per 4-hour bucket ────────────
    dedup_key = f"{mid}__{_hour_bucket()}"
    if dedup_key in seen:
        return None

    # ── Build signal ──────────────────────────────────────────────
    signal_id = str(uuid.uuid4())[:16]

    return {
        # Identity
        "signal_id":      signal_id,
        "dedup_key":      dedup_key,
        "ts":             _ts(),
        "mode":           MODE,

        # Market
        "market_id":      mid,
        "yes_token_id":   tick.get("yes_token_id"),
        "no_token_id":    tick.get("no_token_id"),
        "title":          title,
        "category":       tick.get("category", "unknown"),
        "days_to_close":  tick.get("days_to_close", 0),
        "end_date":       tick.get("end_date", ""),

        # Prices
        "price_now":      moves["price_now"],
        "price_1h_ago":   moves["price_1h"],
        "price_2h_ago":   moves["price_2h"],
        "price_24h_ago":  moves["price_24h"],

        # Moves
        "move_1h":        moves["move_1h"],
        "move_2h":        moves["move_2h"],
        "move_24h":       moves["move_24h"],

        # Volume
        "volume":         vol_now,
        "liquidity":      liq,
        "vol_spike_ratio": moves["vol_spike_ratio"],

        # Base rate
        "base_rate":      base_rate,
        "base_rate_gap":  round(gap, 4) if gap is not None else None,

        # News context — filled by news.py
        "news_headlines": [],
        "news_context":   "",

        # Trade flags — filled by trader.py
        "traded":         False,
        "skip_reason":    "",
    }


# ── Main loop ──────────────────────────────────────────────────────

def run():
    ensure_dirs()
    print(f"[{_ts()}] [detector] starting — interval={DETECT_INTERVAL_SEC}s", flush=True)

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
