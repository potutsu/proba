"""
scorer.py — OR signal scorer
Anti-Insanity | Proba

Tails news.jsonl (enriched signals). Scores each signal on:
  - move magnitude (1h, 2h)
  - base rate gap
  - volume spike ratio
  - news headline count (proxy for event significance)
  - days to close

Outputs score.jsonl with confidence tier, priority, and
all data needed by trader.py.

Worker: runs as subprocess managed by antii manager.
"""

import sys
import time
import signal as _signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
    SIGNAL_MIN_GAP, SIGNAL_MAX_YES_PRICE,
    COOLDOWN_MINUTES, MODE,
)

_RUNNING = True

def _handle_sig(sig, frame):
    global _RUNNING
    _RUNNING = False

_signal.signal(_signal.SIGTERM, _handle_sig)
_signal.signal(_signal.SIGINT,  _handle_sig)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ── Scoring logic ──────────────────────────────────────────────────

def _score_move(primary_move: float, move_24h: Optional[float]) -> int:
    """0-30 points for move magnitude. Uses best of primary_move and move_24h."""
    best = max(
        abs(primary_move) if primary_move else 0,
        abs(move_24h)     if move_24h is not None else 0,
    )
    if best >= 0.30: return 30
    if best >= 0.25: return 25
    if best >= 0.20: return 20
    if best >= 0.15: return 15
    if best >= 0.10: return 10
    if best >= 0.08: return  5
    return 0


def _score_gap(gap: Optional[float]) -> int:
    """0-30 points for base rate gap. None = 10 (unknown but possible)."""
    if gap is None:
        return 10   # no base rate match — partial credit
    if gap >= 0.25: return 30
    if gap >= 0.20: return 25
    if gap >= 0.15: return 20
    if gap >= 0.12: return 15
    if gap >= 0.10: return 10
    if gap >= 0.06: return  5
    return 0


def _score_volume(vol_spike: Optional[float], volume_24h: float = 0) -> int:
    """0-20 points for volume. Uses spike ratio if available, else raw 24h volume."""
    if vol_spike is not None:
        if vol_spike >= 10: return 20
        if vol_spike >= 5:  return 15
        if vol_spike >= 3:  return 10
        if vol_spike >= 2:  return  5
        return 0
    # Fallback: score on raw 24h volume
    if volume_24h >= 500_000: return 20
    if volume_24h >= 100_000: return 15
    if volume_24h >= 50_000:  return 10
    if volume_24h >= 10_000:  return  5
    return 0


def _score_spread(spread: Optional[float]) -> int:
    """0-10 points for bid/ask spread tightness. Tight spread = liquid = better."""
    if spread is None:
        return 0
    if spread <= 0.01: return 10
    if spread <= 0.02: return  8
    if spread <= 0.04: return  5
    if spread <= 0.08: return  2
    return 0


def _score_news(news_count: int) -> int:
    """0-10 points for news presence."""
    if news_count >= 4: return 10
    if news_count >= 2: return  7
    if news_count >= 1: return  4
    return 0


def _score_timing(days_to_close: float) -> int:
    """0-10 points for market timing. OR works on medium-term markets."""
    if 7  <= days_to_close <= 60:  return 10
    if 3  <= days_to_close < 7:   return  8
    if 60 < days_to_close <= 120: return  6
    if 1  <= days_to_close < 3:   return  4
    return 0


def _conf_tier(total_score: int) -> str:
    if total_score >= 70: return "high"
    if total_score >= 45: return "medium"
    return "low"


def _priority(conf: str) -> int:
    """Lower = higher priority."""
    return {"high": 1, "medium": 2, "low": 3}.get(conf, 3)


def score_signal(enriched: dict) -> dict:
    """
    Score an enriched signal. Returns a score record ready for trader.py.

    Scoring inputs (all now available from first tick via Gamma fields):
      primary_move  — best available move (24h native > 1h > 2h)
      move_24h      — Gamma oneDayPriceChange
      base_rate_gap — market price vs historical base rate
      vol_spike     — volume ratio (sparse early on, partial credit)
      news_count    — headlines found
      days_to_close — market timing
      spread        — bid/ask spread (tighter = more liquid = better fill)
    """
    primary_move = enriched.get("primary_move") or enriched.get("move_24h") or enriched.get("move_1h") or 0
    move_24h     = enriched.get("move_24h")
    move_1h      = enriched.get("move_1h")
    move_2h      = enriched.get("move_2h")
    gap          = enriched.get("base_rate_gap")
    vol_spike    = enriched.get("vol_spike_ratio")
    news_count   = enriched.get("news_count", 0)
    days         = float(enriched.get("days_to_close", 0))
    price_now    = float(enriched.get("price_now", 0))
    base_rate    = enriched.get("base_rate")
    spread       = enriched.get("spread")
    volume_24h   = float(enriched.get("volume_24h", 0) or enriched.get("volume", 0) or 0)

    s_move   = _score_move(primary_move, move_24h)
    s_gap    = _score_gap(gap)
    s_vol    = _score_volume(vol_spike, volume_24h)
    s_news   = _score_news(news_count)
    s_timing = _score_timing(days)
    s_spread = _score_spread(spread)
    total    = s_move + s_gap + s_vol + s_news + s_timing + s_spread

    conf     = _conf_tier(total)
    priority = _priority(conf)

    no_price = round(1.0 - price_now, 4)
    edge     = round(base_rate - price_now, 4) if base_rate is not None else None

    return {
        "signal_id":      enriched.get("signal_id"),
        "ts":             _ts(),
        "mode":           MODE,

        "market_id":      enriched.get("market_id"),
        "yes_token_id":   enriched.get("yes_token_id"),
        "no_token_id":    enriched.get("no_token_id"),
        "title":          enriched.get("title"),
        "event_title":    enriched.get("event_title", ""),
        "category":       enriched.get("category"),
        "tag_slugs":      enriched.get("tag_slugs", []),
        "days_to_close":  days,
        "end_date":       enriched.get("end_date"),

        "price_now":      price_now,
        "price_1h_ago":   enriched.get("price_1h_ago"),
        "price_2h_ago":   enriched.get("price_2h_ago"),
        "price_24h_ago":  enriched.get("price_24h_ago"),
        "move_1h":        move_1h,
        "move_2h":        move_2h,
        "move_24h":       move_24h,
        "primary_move":   primary_move,

        "best_bid":       enriched.get("best_bid"),
        "best_ask":       enriched.get("best_ask"),
        "spread":         spread,
        "volume":         enriched.get("volume"),
        "volume_24h":     volume_24h,
        "liquidity":      enriched.get("liquidity"),
        "vol_spike_ratio": vol_spike,
        "base_rate":      base_rate,
        "base_rate_gap":  gap,

        "score_total":    total,
        "score_move":     s_move,
        "score_gap":      s_gap,
        "score_vol":      s_vol,
        "score_news":     s_news,
        "score_timing":   s_timing,
        "score_spread":   s_spread,
        "confidence":     conf,
        "priority":       priority,

        "direction":      "no",
        "no_price":       no_price,
        "edge":           edge,
        "zone":           "OVERREACTION_FADE",
        "strategy_type":  "overreaction",

        "cooldown_minutes": COOLDOWN_MINUTES,

        "news_headlines": enriched.get("news_headlines", []),
        "news_context":   enriched.get("news_context", ""),
        "news_source":    enriched.get("news_source", "none"),
        "news_count":     news_count,
    }


# ── Main loop ──────────────────────────────────────────────────────

def run():
    ensure_dirs()
    print(f"[{_ts()}] [scorer] starting", flush=True)

    reader = TailReader("scorer", "news")
    seen   = load_seen("scorer")

    scored_total = 0

    while _RUNNING:
        new_count = 0

        for enriched in reader.read_new():
            if not _RUNNING:
                break

            sid = enriched.get("signal_id", "")
            if not sid or sid in seen:
                continue

            try:
                scored = score_signal(enriched)
                append_log("score", scored)
                seen.add(sid)
                scored_total += 1
                new_count    += 1

                print(
                    f"[{_ts()}] [scorer] scored {sid} "
                    f"'{scored['title'][:40]}' "
                    f"total={scored['score_total']} conf={scored['confidence']} "
                    f"edge={scored['edge']} no_price={scored['no_price']}",
                    flush=True,
                )

            except Exception as e:
                log_error("scorer", f"score failed for {sid}: {e}")
                print(f"[{_ts()}] [scorer] ERROR {sid}: {e}", flush=True)

        reader.save()

        if new_count > 0:
            print(
                f"[{_ts()}] [scorer] cycle: new={new_count} total={scored_total}",
                flush=True,
            )

        for _ in range(30):
            if not _RUNNING:
                break
            time.sleep(1)

    save_seen("scorer", seen)
    reader.save()
    print(f"[{_ts()}] [scorer] stopped cleanly", flush=True)


if __name__ == "__main__":
    run()
