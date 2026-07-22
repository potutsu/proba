"""
shadow.py — shadow price checkpoint writer
Anti-Insanity | Proba

Background worker. Every hour:
  1. Reads all open OR positions from paper_positions.jsonl
  2. For each position, checks if 24h/48h/72h checkpoint is due
  3. Fetches current YES price from Gamma
  4. Writes checkpoint record to checkpoint.jsonl
  5. If T+cooldown_minutes has passed, records simulated cooldown fill price

This is the core of the post-mortem comparison system:
  - Did the price revert at 24h? 48h? 72h?
  - Would a cooldown entry (T+90min) have given better fill?
  - What news arrived between signal and resolution?

Does NOT close positions — that's monitor.py's job.

Worker: background group, always-on, unlimited restarts.
"""

import json
import sys
import time
import signal as _signal
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

_HERE = Path(__file__).resolve().parent
# Support both standalone (~/proba/antii/) and nested (~/proba/proba/antii/) layouts
_ROOT = _HERE.parent if (_HERE.parent / 'antii').exists() else _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from antii.paths import (
    ensure_dirs, append_log, log_error,
    get_paper_positions_path, LOGS,
)
from antii.antii_config import (
    SHADOW_INTERVAL_SEC, SHADOW_CHECKPOINTS_H,
)

_RUNNING = True

def _handle_sig(sig, frame):
    global _RUNNING
    _RUNNING = False

_signal.signal(_signal.SIGTERM, _handle_sig)
_signal.signal(_signal.SIGINT,  _handle_sig)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Gamma price fetch (no CLOB dependency) ─────────────────────────

def _fetch_current_price(market_id: str) -> float | None:
    """
    Fetch current YES price from Gamma for a market.
    Returns float or None on error.
    """
    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"conditionId": market_id},
            headers={"User-Agent": "AntiiShadow/1.0"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        markets = data if isinstance(data, list) else [data]
        for mkt in markets:
            prices = mkt.get("outcomePrices", [])
            if prices:
                p = float(prices[0])
                # 0.5 is the Gamma default when no orderbook — treat as stale
                if p != 0.5:
                    return p
        return None
    except Exception as e:
        log_error("shadow", f"price fetch error for {market_id}: {e}")
        return None


# ── Position file helpers ──────────────────────────────────────────

def _load_open_or_positions() -> list:
    """Return list of open OR position dicts."""
    path = get_paper_positions_path()
    if not path.exists():
        return []
    positions = []
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
                        positions.append(pos)
                except Exception:
                    pass
    except Exception as e:
        log_error("shadow", f"load positions error: {e}")
    return positions


def _load_checkpoints_done() -> set:
    """
    Return set of (position_id, checkpoint_label) already written.
    Prevents duplicate checkpoint records.
    """
    done = set()
    path = LOGS.get("checkpoint")
    if not path or not path.exists():
        return done
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    pos_id = rec.get("position_id", "")
                    label  = rec.get("checkpoint", "")
                    if pos_id and label:
                        done.add(f"{pos_id}__{label}")
                except Exception:
                    pass
    except Exception as e:
        log_error("shadow", f"load checkpoints error: {e}")
    return done


def _update_position_field(pos_id: str, field: str, value):
    """
    Rewrite paper_positions.jsonl updating one field on one position.
    Uses full-file rewrite — positions file is small so this is safe.
    """
    path = get_paper_positions_path()
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        new_lines = []
        for line in lines:
            line = line.strip()
            if not line:
                new_lines.append(line)
                continue
            try:
                pos = json.loads(line)
                if pos.get("id") == pos_id:
                    pos[field] = value
                new_lines.append(json.dumps(pos))
            except Exception:
                new_lines.append(line)
        tmp = str(path) + ".tmp"
        Path(tmp).write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        import os
        os.replace(tmp, str(path))
    except Exception as e:
        log_error("shadow", f"update_position_field error pos={pos_id} field={field}: {e}")


# ── Checkpoint logic ───────────────────────────────────────────────

def _process_position(pos: dict, done: set, now: datetime):
    """
    Check if any checkpoints are due for this position. Write them.
    Also handle cooldown fill simulation.
    """
    pos_id   = pos.get("id", "")
    mid      = pos.get("market_id", "")
    open_ts  = pos.get("open_ts", "")
    title    = pos.get("title", "")[:45]

    try:
        open_dt = datetime.fromisoformat(open_ts)
    except Exception:
        return

    # ── Cooldown fill simulation ───────────────────────────────────
    # Record the price at T+cooldown_minutes as the "what if" entry
    cooldown_due_str  = pos.get("cooldown_entry_due", "")
    cooldown_fill     = pos.get("cooldown_fill_price")
    cooldown_key      = f"{pos_id}__cooldown"

    if cooldown_fill is None and cooldown_due_str and cooldown_key not in done:
        try:
            cooldown_dt = datetime.fromisoformat(cooldown_due_str)
            if now >= cooldown_dt:
                price = _fetch_current_price(mid)
                if price is not None:
                    _update_position_field(pos_id, "cooldown_fill_price", price)
                    append_log("checkpoint", {
                        "position_id":   pos_id,
                        "signal_id":     pos.get("signal_id"),
                        "market_id":     mid,
                        "title":         title,
                        "checkpoint":    "cooldown",
                        "ts":            _ts(),
                        "hours_elapsed": round((now - open_dt).total_seconds() / 3600, 1),
                        "price":         price,
                        "immediate_fill": pos.get("fill_price"),
                        "cooldown_fill": price,
                        "fill_delta":    round(price - (pos.get("fill_price") or 0), 4),
                        "note":          f"simulated T+{pos.get('cooldown_minutes', 90)}min entry price",
                    })
                    done.add(cooldown_key)
                    print(
                        f"[{_ts()}] [shadow] cooldown fill {pos_id[:8]} "
                        f"'{title}' price={price}",
                        flush=True,
                    )
        except Exception as e:
            log_error("shadow", f"cooldown fill error pos={pos_id}: {e}")

    # ── Time checkpoints (24h / 48h / 72h) ────────────────────────
    for hours in SHADOW_CHECKPOINTS_H:
        label = f"{hours}h"
        key   = f"{pos_id}__{label}"
        if key in done:
            continue

        due_dt = open_dt + timedelta(hours=hours)
        if now < due_dt:
            continue

        price = _fetch_current_price(mid)

        # Compute PnL at this checkpoint
        fill  = pos.get("fill_price", 0) or 0
        pnl   = None
        if price is not None and fill > 0:
            # We hold NO. NO price = 1 - YES price.
            no_price_now = round(1.0 - price, 4)
            pnl = round((no_price_now - fill) / fill * 100, 2)  # % ROI

        record = {
            "position_id":   pos_id,
            "signal_id":     pos.get("signal_id"),
            "market_id":     mid,
            "title":         title,
            "checkpoint":    label,
            "ts":            _ts(),
            "hours_elapsed": round((now - open_dt).total_seconds() / 3600, 1),

            # Prices
            "yes_price_at_checkpoint": price,
            "no_price_at_checkpoint":  round(1.0 - price, 4) if price is not None else None,
            "fill_price":              fill,
            "pnl_pct_if_exited_now":   pnl,

            # Anchor
            "entry_yes_price":         pos.get("price_now"),
            "base_rate":               pos.get("base_rate"),
            "move_1h_at_signal":       pos.get("move_1h"),
            "move_2h_at_signal":       pos.get("move_2h"),

            # News (for post-mortem — manually updated)
            "news_context_update":     "",

            # Resolution (blank until resolved)
            "resolved_outcome":        None,
            "correct":                 None,
        }

        append_log("checkpoint", record)
        done.add(key)

        pnl_str = f"{pnl:+.1f}%" if pnl is not None else "n/a"
        price_str = f"{price:.3f}" if price is not None else "fetch_fail"
        print(
            f"[{_ts()}] [shadow] checkpoint {label} {pos_id[:8]} "
            f"'{title}' YES={price_str} PnL={pnl_str}",
            flush=True,
        )


# ── Main loop ──────────────────────────────────────────────────────

def run():
    ensure_dirs()
    print(
        f"[{_ts()}] [shadow] starting — "
        f"interval={SHADOW_INTERVAL_SEC}s checkpoints={SHADOW_CHECKPOINTS_H}h",
        flush=True,
    )

    while _RUNNING:
        try:
            now       = _now()
            positions = _load_open_or_positions()
            done      = _load_checkpoints_done()

            if positions:
                print(
                    f"[{_ts()}] [shadow] checking {len(positions)} open OR positions",
                    flush=True,
                )
                for pos in positions:
                    if not _RUNNING:
                        break
                    _process_position(pos, done, now)
            else:
                print(f"[{_ts()}] [shadow] no open OR positions", flush=True)

        except Exception as e:
            log_error("shadow", f"cycle error: {e}")
            print(f"[{_ts()}] [shadow] ERROR: {e}", flush=True)

        for _ in range(SHADOW_INTERVAL_SEC):
            if not _RUNNING:
                break
            time.sleep(1)

    print(f"[{_ts()}] [shadow] stopped cleanly", flush=True)


if __name__ == "__main__":
    run()
