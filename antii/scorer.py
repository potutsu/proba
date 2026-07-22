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
sys.path.insert(0, str(_HERE.parent.parent))

from proba.antii.paths import (
    ensure_dirs, append_log, log_error,
    TailReader, load_seen, save_seen,
)
from proba.antii.antii_config import (
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

def _score_move(move_1h: Optional[float], move_2h: Optional[float]) -> int:
    """0-30 points for move magnitude."""
    best = max(
        abs(move_1h) if move_1h is not None else 0,
        abs(move_2h) if move_2h is not None else 0,
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


def _score_volume(vol_spike: Optional[float]) -> int:
    """0-20 points for volume spike ratio."""
    if vol_spike is None:
        return 5   # can't compute — partial credit
    if vol_spike >= 10: return 20
    if vol_spike >= 5:  return 15
    if vol_spike >= 3:  return 10
    if vol_spike >= 2:  return  5
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
    """
    move_1h     = enriched.get("move_1h")
    move_2h     = enriched.get("move_2h")
    gap         = enriched.get("base_rate_gap")
    vol_spike   = enriched.get("vol_spike_ratio")
    news_count  = enriched.get("news_count", 0)
    days        = float(enriched.get("days_to_close", 0))
    price_now   = float(enriched.get("price_now", 0))
    base_rate   = enriched.get("base_rate")

    s_move   = _score_move(move_1h, move_2h)
    s_gap    = _score_gap(gap)
    s_vol    = _score_volume(vol_spike)
    s_news   = _score_news(news_count)
    s_timing = _score_timing(days)
    total    = s_move + s_gap + s_vol + s_news + s_timing

    conf     = _conf_tier(total)
    priority = _priority(conf)

    # NO price is what we trade
    no_price = round(1.0 - price_now, 4)

    # Edge estimate: how much YES is overpriced vs base rate
    edge = None
    if base_rate is not None:
        edge = round(base_rate - price_now, 4)   # negative = YES overpriced vs base

    return {
        # Passthrough identity
        "signal_id":      enriched.get("signal_id"),
        "ts":             _ts(),
        "mode":           MODE,

        # Market
        "market_id":      enriched.get("market_id"),
        "yes_token_id":   enriched.get("yes_token_id"),
        "no_token_id":    enriched.get("no_token_id"),
        "title":          enriched.get("title"),
        "category":       enriched.get("category"),
        "days_to_close":  days,
        "end_date":       enriched.get("end_date"),

        # Signal data
        "price_now":      price_now,
        "price_1h_ago":   enriched.get("price_1h_ago"),
        "price_2h_ago":   enriched.get("price_2h_ago"),
        "price_24h_ago":  enriched.get("price_24h_ago"),
        "move_1h":        move_1h,
        "move_2h":        move_2h,
        "move_24h":       enriched.get("move_24h"),
        "volume":         enriched.get("volume"),
        "liquidity":      enriched.get("liquidity"),
        "vol_spike_ratio": vol_spike,
        "base_rate":      base_rate,
        "base_rate_gap":  gap,

        # Scoring
        "score_total":    total,
        "score_move":     s_move,
        "score_gap":      s_gap,
        "score_vol":      s_vol,
        "score_news":     s_news,
        "score_timing":   s_timing,
        "confidence":     conf,
        "priority":       priority,

        # Trade direction
        "direction":      "no",
        "no_price":       no_price,
        "edge":           edge,
        "zone":           "OVERREACTION_FADE",
        "strategy_type":  "overreaction",

        # Cooldown simulation config (used by trader + shadow)
        "cooldown_minutes": COOLDOWN_MINUTES,

        # News passthrough
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
