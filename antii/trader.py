"""
trader.py — OR paper trader
Anti-Insanity | Proba

Tails score.jsonl. For each scored signal:
  - Checks position cap (MAX_OPEN_OR)
  - Checks dedup (not already in a position on same market)
  - Opens paper position in paper_positions.jsonl
  - Logs trade decision to trade.jsonl (including skips with reason)
  - Records entry_timing: "immediate"
  - Records cooldown_entry_due_ts for shadow.py to simulate T+90min fill

Worker: runs as subprocess managed by antii manager.
"""

import json
import sys
import time
import uuid
import signal as _signal
from datetime import datetime, timezone, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# Support both standalone (~/proba/antii/) and nested (~/proba/proba/antii/) layouts
_ROOT = _HERE.parent if (_HERE.parent / 'antii').exists() else _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from antii.paths import (
    ensure_dirs, append_log, log_error,
    TailReader, load_seen, save_seen,
    get_paper_positions_path, LOGS,
)
from antii.antii_config import (
    POSITION_SIZE_USD, MAX_OPEN_OR, COOLDOWN_MINUTES, MODE,
)

_RUNNING = True

def _handle_sig(sig, frame):
    global _RUNNING
    _RUNNING = False

_signal.signal(_signal.SIGTERM, _handle_sig)
_signal.signal(_signal.SIGINT,  _handle_sig)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Open position reader ────────────────────────────────────────────

def _load_or_positions() -> tuple[int, set]:
    """
    Count open OR positions and collect open market IDs.
    Returns (count, set_of_market_ids).
    """
    path = get_paper_positions_path()
    if not path.exists():
        return 0, set()

    count = 0
    mids  = set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    pos = json.loads(line)
                    if (
                        pos.get("status") == "open"
                        and pos.get("strategy_type") == "overreaction"
                    ):
                        count += 1
                        mids.add(str(pos.get("market_id", "")))
                except Exception:
                    pass
    except Exception as e:
        log_error("trader", f"load_or_positions error: {e}")

    return count, mids


# ── Position writer ────────────────────────────────────────────────

def _open_position(scored: dict) -> dict:
    """
    Write a new paper position to paper_positions.jsonl.
    Returns the position dict.
    """
    now    = datetime.now(timezone.utc)
    pos_id = str(uuid.uuid4())

    cooldown_due = (now + timedelta(minutes=COOLDOWN_MINUTES)).isoformat()

    position = {
        # Identity
        "id":              pos_id,
        "signal_id":       scored.get("signal_id"),
        "source":          "polymarket",
        "strategy_type":   "overreaction",
        "mode":            MODE,

        # Market
        "market_id":       scored.get("market_id"),
        "yes_token_id":    scored.get("yes_token_id"),
        "no_token_id":     scored.get("no_token_id"),
        "title":           scored.get("title"),
        "category":        scored.get("category"),

        # Trade
        "direction":       "no",
        "signal":          "BUY_NO",
        "zone":            "OVERREACTION_FADE",
        "entry_timing":    "immediate",
        "fill_price":      scored.get("no_price"),
        "market_price":    scored.get("no_price"),
        "position_size_usd": POSITION_SIZE_USD,

        # Scoring passthrough
        "confidence":      scored.get("confidence"),
        "priority":        scored.get("priority"),
        "score_total":     scored.get("score_total"),
        "edge":            scored.get("edge"),
        "base_rate":       scored.get("base_rate"),
        "base_rate_gap":   scored.get("base_rate_gap"),

        # Price data
        "price_now":       scored.get("price_now"),
        "price_1h_ago":    scored.get("price_1h_ago"),
        "price_2h_ago":    scored.get("price_2h_ago"),
        "price_24h_ago":   scored.get("price_24h_ago"),
        "move_1h":         scored.get("move_1h"),
        "move_2h":         scored.get("move_2h"),
        "move_24h":        scored.get("move_24h"),
        "vol_spike_ratio": scored.get("vol_spike_ratio"),

        # News
        "news_headlines":  scored.get("news_headlines", []),
        "news_context":    scored.get("news_context", ""),
        "news_source":     scored.get("news_source", "none"),

        # Shadow comparison
        "cooldown_minutes":    COOLDOWN_MINUTES,
        "cooldown_entry_due":  cooldown_due,
        "cooldown_fill_price": None,   # filled by shadow.py at T+cooldown
        "shadow_price_24h":    None,   # filled by shadow.py
        "shadow_price_48h":    None,
        "shadow_price_72h":    None,

        # Lifecycle
        "status":          "open",
        "open_ts":         now.isoformat(),
        "close_date":      scored.get("end_date", ""),
        "days_to_close":   scored.get("days_to_close"),
        "resolved_outcome": None,
        "correct":         None,
        "close_ts":        None,
    }

    path = get_paper_positions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(position) + "\n")

    return position


def _log_trade(scored: dict, opened: bool, reason: str, position: dict = None):
    """Write trade decision to trade.jsonl."""
    record = {
        "ts":         _ts(),
        "signal_id":  scored.get("signal_id"),
        "market_id":  scored.get("market_id"),
        "title":      scored.get("title", "")[:60],
        "opened":     opened,
        "reason":     reason,
        "confidence": scored.get("confidence"),
        "score":      scored.get("score_total"),
        "no_price":   scored.get("no_price"),
        "edge":       scored.get("edge"),
        "position_id": position.get("id") if position else None,
    }
    append_log("trade", record)


# ── Main loop ──────────────────────────────────────────────────────

def run():
    ensure_dirs()
    print(
        f"[{_ts()}] [trader] starting — "
        f"max_open={MAX_OPEN_OR} size=${POSITION_SIZE_USD} cooldown={COOLDOWN_MINUTES}min",
        flush=True,
    )

    reader = TailReader("trader", "score")
    seen   = load_seen("trader")

    opened_total = 0
    skipped_total = 0

    while _RUNNING:
        new_count = 0

        for scored in reader.read_new():
            if not _RUNNING:
                break

            sid = scored.get("signal_id", "")
            if not sid or sid in seen:
                continue

            seen.add(sid)
            new_count += 1

            market_id = str(scored.get("market_id", ""))
            title     = scored.get("title", "")[:55]
            conf      = scored.get("confidence", "low")
            score     = scored.get("score_total", 0)
            no_price  = scored.get("no_price")

            # ── Gate: low confidence skip ──────────────────────
            if conf == "low":
                reason = f"confidence=low score={score}"
                _log_trade(scored, False, reason)
                skipped_total += 1
                print(f"[{_ts()}] [trader] SKIP {sid} — {reason}", flush=True)
                continue

            # ── Gate: no trade price ───────────────────────────
            if no_price is None or no_price <= 0:
                reason = "no_price unavailable"
                _log_trade(scored, False, reason)
                skipped_total += 1
                print(f"[{_ts()}] [trader] SKIP {sid} — {reason}", flush=True)
                continue

            # ── Gate: position cap ─────────────────────────────
            open_count, open_mids = _load_or_positions()
            if open_count >= MAX_OPEN_OR:
                reason = f"at max open OR positions ({MAX_OPEN_OR})"
                _log_trade(scored, False, reason)
                skipped_total += 1
                print(f"[{_ts()}] [trader] SKIP {sid} — {reason}", flush=True)
                continue

            # ── Gate: already in this market ───────────────────
            if market_id in open_mids:
                reason = "already have open position on this market"
                _log_trade(scored, False, reason)
                skipped_total += 1
                print(f"[{_ts()}] [trader] SKIP {sid} — {reason}", flush=True)
                continue

            # ── Open position ──────────────────────────────────
            try:
                position = _open_position(scored)
                _log_trade(scored, True, "opened", position)
                opened_total += 1
                print(
                    f"[{_ts()}] [trader] OPENED {position['id'][:8]} "
                    f"'{title}' "
                    f"NO@{no_price:.3f} conf={conf} score={score} "
                    f"cooldown_due={position['cooldown_entry_due'][:16]}",
                    flush=True,
                )
            except Exception as e:
                log_error("trader", f"open_position failed for {sid}: {e}")
                print(f"[{_ts()}] [trader] ERROR opening {sid}: {e}", flush=True)

        reader.save()

        if new_count > 0:
            print(
                f"[{_ts()}] [trader] cycle: new_scored={new_count} "
                f"opened={opened_total} skipped={skipped_total}",
                flush=True,
            )

        for _ in range(30):
            if not _RUNNING:
                break
            time.sleep(1)

    save_seen("trader", seen)
    reader.save()
    print(f"[{_ts()}] [trader] stopped cleanly", flush=True)


if __name__ == "__main__":
    run()
