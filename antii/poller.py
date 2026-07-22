"""
poller.py — Gamma all-category tick collector
Anti-Insanity | Proba

Polls Gamma API every 15 min, writes one tick per active market
to price_tick.jsonl. Does NOT filter — logs everything except sports
(or everything in comprehensive mode). Detector does the filtering.

Worker: runs as subprocess managed by antii manager.
"""

import json
import sys
import time
import signal
from datetime import datetime, timezone
from pathlib import Path

# ── Path bootstrap ────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
# Support both standalone (~/proba/antii/) and nested (~/proba/proba/antii/) layouts
_ROOT = _HERE.parent if (_HERE.parent / 'antii').exists() else _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from antii.paths import (
    ensure_dirs, append_log, log_error, LOGS
)
from antii.antii_config import (
    MODE, FOCUS_CATEGORIES, BLOCKED_CATEGORIES,
    POLL_INTERVAL_SEC, POLL_LIMIT, POLL_SORT_BY,
)

import requests

_RUNNING = True

def _handle_sig(sig, frame):
    global _RUNNING
    _RUNNING = False

signal.signal(signal.SIGTERM, _handle_sig)
signal.signal(signal.SIGINT,  _handle_sig)


# ── Gamma fetch ────────────────────────────────────────────────────

GAMMA_URL = "https://gamma-api.polymarket.com/markets"

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def _days_to_close(end_date: str) -> float:
    try:
        dt  = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return max((dt - now).total_seconds() / 86400.0, 0.0)
    except Exception:
        return 0.0


def _category_ok(market: dict) -> bool:
    """Return True if this market's category passes mode filter."""
    raw = (
        market.get("category", "") or
        market.get("_category_name", "") or
        ""
    ).lower().strip()

    # Always block sports
    if any(b in raw for b in BLOCKED_CATEGORIES):
        return False

    if MODE == "focus":
        return any(f in raw for f in FOCUS_CATEGORIES)

    # comprehensive — everything not blocked
    return True


def fetch_all_markets() -> list:
    """
    Fetch all active non-sports markets from Gamma.
    Returns list of raw market dicts.
    """
    markets = []
    offset  = 0
    limit   = min(POLL_LIMIT, 100)   # Gamma max per page = 100

    while len(markets) < POLL_LIMIT:
        try:
            resp = requests.get(
                GAMMA_URL,
                params={
                    "active":     "true",
                    "closed":     "false",
                    "order":      POLL_SORT_BY,
                    "ascending":  "false",
                    "limit":      limit,
                    "offset":     offset,
                },
                headers={"User-Agent": "Mozilla/5.0 (compatible; AntiiPoller/1.0)"},
                timeout=20,
            )
            resp.raise_for_status()
            page = resp.json()

            if not isinstance(page, list) or not page:
                break

            markets.extend(page)
            if len(page) < limit:
                break
            offset += limit

        except requests.exceptions.Timeout:
            log_error("poller", "Gamma fetch timeout", {"offset": offset})
            break
        except requests.exceptions.HTTPError as e:
            log_error("poller", f"Gamma HTTP error: {e}", {"offset": offset})
            break
        except Exception as e:
            log_error("poller", f"Gamma fetch error: {e}", {"offset": offset})
            break

    return markets


def market_to_tick(market: dict) -> dict:
    """Convert a raw Gamma market dict to a price_tick record."""
    # YES price from outcomePrices[0]
    outcome_prices = market.get("outcomePrices", [])
    try:
        yes_price = float(outcome_prices[0]) if outcome_prices else None
    except (ValueError, TypeError):
        yes_price = None

    # Token IDs
    clob_ids  = market.get("clobTokenIds", [])
    yes_token = str(clob_ids[0]) if len(clob_ids) > 0 else None
    no_token  = str(clob_ids[1]) if len(clob_ids) > 1 else None

    volume    = float(market.get("volume", 0) or 0)
    liquidity = float(market.get("liquidity", 0) or market.get("liquidityNum", 0) or 0)
    end_date  = market.get("endDate", market.get("endDateIso", ""))

    return {
        "ts":           _ts(),
        "market_id":    str(market.get("conditionId", market.get("id", ""))),
        "yes_token_id": yes_token,
        "no_token_id":  no_token,
        "title":        market.get("question", market.get("title", "")),
        "category":     (market.get("category") or market.get("_category_name") or "unknown").lower(),
        "yes_price":    yes_price,
        "volume":       volume,
        "liquidity":    liquidity,
        "days_to_close": round(_days_to_close(end_date), 3),
        "end_date":     end_date,
        "active":       bool(market.get("active", True)),
        "closed":       bool(market.get("closed", False)),
        "resolved":     bool(market.get("resolved", False)),
        "resolution_price": market.get("resolutionPrice"),
        "mode":         MODE,
    }


# ── Main loop ──────────────────────────────────────────────────────

def run():
    ensure_dirs()
    print(f"[{_ts()}] [poller] starting — mode={MODE} interval={POLL_INTERVAL_SEC}s", flush=True)

    while _RUNNING:
        cycle_start = time.time()

        try:
            raw_markets = fetch_all_markets()
            tick_count  = 0
            skip_count  = 0

            for mkt in raw_markets:
                if not _RUNNING:
                    break

                if not _category_ok(mkt):
                    skip_count += 1
                    continue

                tick = market_to_tick(mkt)

                # Skip markets with no YES price (no orderbook, ghost markets)
                if tick["yes_price"] is None:
                    skip_count += 1
                    continue

                append_log("tick", tick)
                tick_count += 1

            elapsed = round(time.time() - cycle_start, 1)
            print(
                f"[{_ts()}] [poller] cycle done — "
                f"fetched={len(raw_markets)} ticked={tick_count} "
                f"skipped={skip_count} elapsed={elapsed}s",
                flush=True,
            )

        except Exception as e:
            log_error("poller", f"cycle error: {e}")
            print(f"[{_ts()}] [poller] ERROR: {e}", flush=True)

        # Sleep until next poll, checking _RUNNING every second
        sleep_remaining = POLL_INTERVAL_SEC - (time.time() - cycle_start)
        for _ in range(max(1, int(sleep_remaining))):
            if not _RUNNING:
                break
            time.sleep(1)

    print(f"[{_ts()}] [poller] stopped cleanly", flush=True)


if __name__ == "__main__":
    run()
