"""
monitor.py — position monitor with auto-resolution
Proba | NoLaptopTrades

Phase 0 strategy: hold every position to resolution.
Monitor checks Polymarket/Futuur for resolved markets,
auto-closes them, and updates calibration automatically.

You never need to run 'proba --resolve' manually in Phase 0.
Just check 'proba --stats' to see win rate progress.

Cycle (every 10 min):
  For each open position:
    1. Fetch market status from API
    2. If resolved → read winner → auto-close → update calibration
    3. If not resolved → log current price (for record only)
    4. Alert if approaching close date
"""

import os
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

from proba.paths import get_config, get_setting, log_error
from proba.paper_logger import list_open_positions, update_position_price
from proba.postmortem import process_resolved


# ---------------------------------------------------------------------------
# Fetch market status from Polymarket
# ---------------------------------------------------------------------------

def _fetch_polymarket_status(position: Dict) -> Tuple[Optional[float], bool, Optional[str]]:
    """
    Fetch current market status from Polymarket Gamma API.

    Returns (current_yes_price, is_resolved, resolution_outcome)
    resolution_outcome: 'yes' | 'no' | None

    resolutionPrice from Gamma: 1.0 = YES won, 0.0 = NO won
    """
    condition_id = position.get("market_id", "")
    if not condition_id:
        return None, False, None

    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"conditionId": condition_id},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        if r.status_code != 200:
            log_error("monitor", f"Gamma {r.status_code} for {condition_id[:16]}")
            return None, False, None

        data = r.json()
        markets = data if isinstance(data, list) else []
        if not markets:
            return None, False, None

        mkt = markets[0]
        closed   = mkt.get("closed", False)
        resolved = mkt.get("resolved", False)

        # Log raw fields — visible in proba.log for diagnosis
        log_error("monitor",
                  f"[CHECK] {condition_id[:20]}... "
                  f"closed={closed} resolved={resolved} "
                  f"resolutionPrice={mkt.get('resolutionPrice')} "
                  f"outcomePrices={str(mkt.get('outcomePrices',''))[:40]}")

        if closed or resolved:
            # Read the resolution price
            res_price = mkt.get("resolutionPrice")
            if res_price is not None:
                try:
                    rp = float(res_price)
                    outcome = "yes" if rp >= 0.99 else "no"
                    return None, True, outcome
                except (ValueError, TypeError):
                    pass

            # resolutionPrice not set yet — check outcomePrices
            op = mkt.get("outcomePrices", [])
            if isinstance(op, str):
                try:
                    import json as _j
                    op = _j.loads(op)
                except Exception:
                    op = []
            if op:
                try:
                    prices = [float(p) for p in op if p is not None]
                    if prices[0] >= 0.99:
                        return None, True, "yes"
                    elif len(prices) > 1 and prices[1] >= 0.99:
                        return None, True, "no"
                except Exception:
                    pass

            # Closed but outcome unreadable yet
            return None, True, None

        # Not resolved — get current YES price
        op = mkt.get("outcomePrices", [])
        if isinstance(op, str):
            try:
                import json as _j
                op = _j.loads(op)
            except Exception:
                op = []
        if op:
            try:
                return float(op[0]), False, None
            except (ValueError, TypeError):
                pass

    except Exception as e:
        log_error("monitor", f"Polymarket fetch failed {condition_id[:16]}: {e}")

    return None, False, None


# ---------------------------------------------------------------------------
# Fetch market status from Futuur
# ---------------------------------------------------------------------------

def _fetch_futuur_status(position: Dict) -> Tuple[Optional[float], bool, Optional[str]]:
    """
    Returns (current_yes_price, is_resolved, resolution_outcome)
    """
    try:
        from proba.sources.futuur import fetch_event_detail, _has_keys
        if not _has_keys():
            return None, False, None

        event_id = position.get("event_id", "")
        if not event_id:
            return None, False, None

        detail = fetch_event_detail(int(event_id))
        if not detail:
            return None, False, None

        if detail.get("resolved"):
            # Find which outcome resolved to long (won)
            for mkt in detail.get("markets", []):
                for oc in mkt.get("outcomes", []):
                    if float(oc.get("price", 0)) >= 0.99:
                        outcome = "yes" if oc.get("name","").lower() in ("yes","true") else "no"
                        return None, True, outcome
            return None, True, None  # resolved but can't determine outcome yet

        # Not resolved — get current price
        for mkt in detail.get("markets", []):
            for oc in mkt.get("outcomes", []):
                if oc.get("name", "").lower() in ("yes", "true"):
                    return float(oc.get("price", 0.5)), False, None

    except Exception as e:
        log_error("monitor", f"Futuur fetch failed: {e}")

    return None, False, None


# ---------------------------------------------------------------------------
# Route to correct source
# ---------------------------------------------------------------------------

def _fetch_status(position: Dict) -> Tuple[Optional[float], bool, Optional[str]]:
    source = position.get("source", "")
    if source == "polymarket":
        return _fetch_polymarket_status(position)
    elif source == "futuur":
        return _fetch_futuur_status(position)
    return None, False, None


# ---------------------------------------------------------------------------
# Days remaining
# ---------------------------------------------------------------------------

def _days_remaining(date_str: str) -> float:
    if not date_str:
        return 99.0
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return max((dt - datetime.now(timezone.utc)).total_seconds() / 86400.0, 0.0)
    except Exception:
        return 99.0


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _telegram(message: str) -> None:
    if not get_setting("telegram.enabled", False):
        return
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
    except Exception as e:
        log_error("monitor", f"Telegram failed: {e}")


# ---------------------------------------------------------------------------
# Main check — auto-resolves when market is done
# ---------------------------------------------------------------------------

def check_positions(
    positions: Optional[List[Dict]] = None,
    cfg: Optional[Dict] = None,
) -> List[Dict]:
    """
    Check all open positions. Auto-resolves any that are done.

    Phase 0 behaviour:
    - Market resolved + outcome known → auto-close, update calibration
    - Market resolved + outcome unclear → alert, wait next cycle
    - Not resolved, approaching close → alert
    - Not resolved → log current price

    Returns list of alert dicts for the TUI.
    """
    if positions is None:
        positions = list_open_positions()
    if not positions:
        return []

    if cfg is None:
        cfg = get_config()

    mon = cfg.get("monitor", {})
    tg  = cfg.get("telegram", {})

    close_alert_days = mon.get("alert_days_to_close", 1)
    alert_resolution = tg.get("alert_on_resolution", True)

    alerts = []

    for pos in positions:
        pos_id     = pos["id"]
        title      = pos.get("title", "")[:55]
        direction  = pos.get("direction", "yes")
        entry      = float(pos.get("fill_price") or pos.get("entry_price") or 0.5)
        close_date = pos.get("close_date") or pos.get("closes_at") or ""
        source     = pos.get("source", "?")

        current_price, is_resolved, outcome = _fetch_status(pos)

        # ── Auto-resolve ──────────────────────────────────────────────────
        if is_resolved:
            if outcome is not None:
                # We know the winner — auto-close
                try:
                    result = process_resolved(pos, outcome)
                    correct = result.get("correct", False)
                    icon    = "✓ CORRECT" if correct else "✗ WRONG"
                    profit_direction = "YES" if outcome == "yes" else "NO"

                    log_error("monitor",
                              f"AUTO-RESOLVED [{icon}] '{title}' "
                              f"direction={direction.upper()} outcome={outcome.upper()} "
                              f"entry={entry:.2%}")

                    alert = {
                        "position_id": pos_id,
                        "title":       title,
                        "alert_type":  "auto_resolved",
                        "outcome":     outcome,
                        "correct":     correct,
                        "source":      source,
                    }
                    alerts.append(alert)

                    if alert_resolution:
                        _telegram(
                            f"{'✅' if correct else '❌'} Proba auto-resolved\n"
                            f"{title}\n"
                            f"Dir: {direction.upper()} | Result: {outcome.upper()} | "
                            f"{icon}\n"
                            f"Entry: {entry:.2%}"
                        )

                except Exception as e:
                    log_error("monitor", f"auto-resolve failed for {pos_id[:8]}: {e}")

            else:
                # Resolved but outcome not readable yet — wait next cycle
                log_error("monitor",
                          f"RESOLVED but outcome unclear — '{title}' "
                          f"(will retry next cycle)")
                alerts.append({
                    "position_id": pos_id,
                    "title":       title,
                    "alert_type":  "resolved_pending",
                    "source":      source,
                    "note":        "Outcome not yet readable — retrying next cycle",
                })
            continue

        # ── Still open ────────────────────────────────────────────────────
        if current_price is not None:
            update_position_price(pos_id, current_price)

        # Approaching close alert
        days_left = _days_remaining(close_date)
        if 0 < days_left <= close_alert_days:
            alerts.append({
                "position_id":   pos_id,
                "title":         title,
                "alert_type":    "approaching_close",
                "days_remaining": round(days_left, 2),
                "current_price":  current_price,
                "source":         source,
            })
            _telegram(
                f"⏰ Proba: Closing in {days_left:.1f}d\n"
                f"{title}\nEntry: {entry:.2%}"
            )

    return alerts
