"""
monitor.py — OR position monitor
Anti-Insanity | Proba

Background worker. Every 10 min:
  - Reads open OR positions from paper_positions.jsonl
  - Checks Gamma for resolution (resolved=true + resolutionPrice)
  - On resolution: closes position, records outcome, updates calibration
  - Writes resolution record to monitor.jsonl

Background group — always-on, unlimited restarts.
"""

import json
import os
import sys
import time
import signal as _signal
from datetime import datetime, timezone
from pathlib import Path

import requests

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

from proba.antii.paths import (
    ensure_dirs, append_log, log_error,
    get_paper_positions_path, LOGS,
)
from proba.antii.antii_config import MONITOR_INTERVAL_SEC

_RUNNING = True

def _handle_sig(sig, frame):
    global _RUNNING
    _RUNNING = False

_signal.signal(_signal.SIGTERM, _handle_sig)
_signal.signal(_signal.SIGINT,  _handle_sig)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ── Gamma resolution check ─────────────────────────────────────────

def _check_resolution(market_id: str) -> tuple[bool, str | None, float | None]:
    """
    Check if market is resolved on Polymarket.
    Returns (is_resolved, outcome, resolution_price).
    outcome: "yes" | "no" | None
    """
    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"conditionId": market_id},
            headers={"User-Agent": "AntiiMonitor/1.0"},
            timeout=15,
        )
        resp.raise_for_status()
        data    = resp.json()
        markets = data if isinstance(data, list) else [data]

        for mkt in markets:
            if not mkt.get("resolved", False):
                return False, None, None

            res_price = mkt.get("resolutionPrice")
            if res_price is None:
                # Try outcomes
                outcomes = mkt.get("outcomes", [])
                if outcomes:
                    res_price = outcomes[0].get("resolutionPrice")

            if res_price is not None:
                try:
                    p = float(res_price)
                    outcome = "yes" if p >= 0.5 else "no"
                    return True, outcome, p
                except (ValueError, TypeError):
                    pass

            # Resolved but price unreadable
            return True, None, None

        return False, None, None

    except Exception as e:
        log_error("monitor", f"resolution check error for {market_id}: {e}")
        return False, None, None


# ── Position file rewrite ──────────────────────────────────────────

def _close_position(pos_id: str, outcome: str, res_price: float):
    """
    Rewrite paper_positions.jsonl marking position as closed.
    """
    path = get_paper_positions_path()
    if not path.exists():
        return

    now = datetime.now(timezone.utc).isoformat()

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
                    # Direction is NO, so we win if outcome is NO
                    direction = pos.get("direction", "no")
                    correct   = (outcome == direction)
                    pos["status"]           = "closed"
                    pos["resolved_outcome"] = outcome
                    pos["resolution_price"] = res_price
                    pos["correct"]          = correct
                    pos["close_ts"]         = now
                new_lines.append(json.dumps(pos))
            except Exception:
                new_lines.append(line)

        tmp = str(path) + ".tmp"
        Path(tmp).write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        os.replace(tmp, str(path))

    except Exception as e:
        log_error("monitor", f"close_position error pos={pos_id}: {e}")


def _update_calibration(pos: dict, outcome: str):
    """
    Update proba's calibration tracker with this OR result.
    Reuses postmortem.py if available, otherwise no-ops gracefully.
    """
    try:
        from proba.postmortem import process_resolved
        result = process_resolved(pos, outcome)
        return result
    except Exception as e:
        log_error("monitor", f"calibration update error: {e}")
        return None


def _load_open_or_positions() -> list:
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
        log_error("monitor", f"load positions error: {e}")
    return positions


# ── Main loop ──────────────────────────────────────────────────────

def run():
    ensure_dirs()
    print(
        f"[{_ts()}] [monitor] starting — interval={MONITOR_INTERVAL_SEC}s",
        flush=True,
    )

    resolved_total = 0

    while _RUNNING:
        try:
            positions = _load_open_or_positions()

            if not positions:
                print(f"[{_ts()}] [monitor] no open OR positions", flush=True)
            else:
                print(
                    f"[{_ts()}] [monitor] checking {len(positions)} positions",
                    flush=True,
                )

            for pos in positions:
                if not _RUNNING:
                    break

                pos_id = pos.get("id", "")
                mid    = pos.get("market_id", "")
                title  = pos.get("title", "")[:45]

                is_resolved, outcome, res_price = _check_resolution(mid)

                if not is_resolved:
                    continue

                if outcome is None:
                    print(
                        f"[{_ts()}] [monitor] {pos_id[:8]} resolved "
                        f"but outcome unreadable — retry next cycle",
                        flush=True,
                    )
                    continue

                # Close position
                _close_position(pos_id, outcome, res_price)

                # Update calibration
                cal_result = _update_calibration(pos, outcome)

                direction = pos.get("direction", "no")
                correct   = (outcome == direction)
                icon      = "✓ CORRECT" if correct else "✗ WRONG"

                # Write monitor log
                record = {
                    "ts":               _ts(),
                    "position_id":      pos_id,
                    "signal_id":        pos.get("signal_id"),
                    "market_id":        mid,
                    "title":            title,
                    "outcome":          outcome,
                    "direction":        direction,
                    "correct":          correct,
                    "resolution_price": res_price,
                    "fill_price":       pos.get("fill_price"),
                    "confidence":       pos.get("confidence"),
                    "score_total":      pos.get("score_total"),
                    "base_rate":        pos.get("base_rate"),
                    "base_rate_gap":    pos.get("base_rate_gap"),
                    "move_1h":          pos.get("move_1h"),
                    "move_2h":          pos.get("move_2h"),
                    "news_count":       len(pos.get("news_headlines", [])),
                    "cooldown_fill":    pos.get("cooldown_fill_price"),
                    "shadow_24h":       pos.get("shadow_price_24h"),
                    "shadow_48h":       pos.get("shadow_price_48h"),
                    "shadow_72h":       pos.get("shadow_price_72h"),
                    "open_ts":          pos.get("open_ts"),
                    "close_ts":         datetime.now(timezone.utc).isoformat(),
                }
                append_log("monitor", record)
                resolved_total += 1

                print(
                    f"[{_ts()}] [monitor] RESOLVED {pos_id[:8]} "
                    f"'{title}' outcome={outcome.upper()} {icon} "
                    f"fill={pos.get('fill_price', 0):.3f} "
                    f"res_price={res_price}",
                    flush=True,
                )

        except Exception as e:
            log_error("monitor", f"cycle error: {e}")
            print(f"[{_ts()}] [monitor] ERROR: {e}", flush=True)

        for _ in range(MONITOR_INTERVAL_SEC):
            if not _RUNNING:
                break
            time.sleep(1)

    print(f"[{_ts()}] [monitor] stopped cleanly total_resolved={resolved_total}", flush=True)


if __name__ == "__main__":
    run()
