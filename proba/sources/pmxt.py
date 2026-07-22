"""
sources/pmxt.py — pmxt SDK adapter for price history + orderbook
Proba | NoLaptopTrades

Used by scorer_overreaction.py to detect 24h price moves.
Falls back to polymarket.py fetch_price_history() if pmxt is unavailable or fails.

pmxt released 2026-07-18. API may still shift — all calls are wrapped defensively.
"""

from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from proba.paths import log_error

# ---------------------------------------------------------------------------
# pmxt availability check
# ---------------------------------------------------------------------------

_PMXT_AVAILABLE = False

try:
    from pmxt import Exchange as _Exchange
    _PMXT_AVAILABLE = True
except ImportError:
    pass


def pmxt_available() -> bool:
    return _PMXT_AVAILABLE


# ---------------------------------------------------------------------------
# Price history — 24h delta
# ---------------------------------------------------------------------------

def fetch_price_history_24h(market_id: str, token_id: Optional[str] = None) -> Optional[Dict]:
    """
    Fetch 24h price history for a market. Returns:
        {
          "price_now":   float,
          "price_24h":   float,
          "move_24h":    float,   # price_now - price_24h (signed)
          "volume_24h":  float,
          "source":      "pmxt" | "polymarket_fallback",
        }
    Returns None if data is unavailable from all sources.
    """
    if _PMXT_AVAILABLE:
        result = _fetch_via_pmxt(market_id)
        if result:
            return result

    # Fallback: derive from polymarket.py weekly history
    if token_id:
        result = _fetch_via_polymarket_fallback(token_id)
        if result:
            return result

    log_error("pmxt", f"no price history available for market_id={market_id} token_id={token_id}")
    return None


def _fetch_via_pmxt(market_id: str) -> Optional[Dict]:
    try:
        pm      = _Exchange("polymarket")
        history = pm.fetchPriceHistory(market_id, interval="24h")

        # pmxt returns a list of {timestamp, price, volume} dicts
        # or possibly {"prices": [...], "volumes": [...]} — handle both shapes
        if isinstance(history, dict):
            prices  = history.get("prices", [])
            volumes = history.get("volumes", [])
        elif isinstance(history, list):
            prices  = history
            volumes = []
        else:
            return None

        if len(prices) < 2:
            return None

        # Last entry = now, first entry = 24h ago
        def _price_val(entry) -> Optional[float]:
            if isinstance(entry, dict):
                for key in ("price", "p", "value", "close"):
                    if key in entry:
                        return float(entry[key])
            if isinstance(entry, (int, float)):
                return float(entry)
            return None

        price_now  = _price_val(prices[-1])
        price_24h  = _price_val(prices[0])

        if price_now is None or price_24h is None:
            return None

        # Volume: sum of all buckets if list of dicts, else last value
        volume_24h = 0.0
        if volumes:
            try:
                volume_24h = sum(
                    float(v["volume"] if isinstance(v, dict) else v)
                    for v in volumes
                    if v is not None
                )
            except Exception:
                pass
        elif isinstance(prices[-1], dict) and "volume" in prices[-1]:
            try:
                volume_24h = sum(
                    float(p.get("volume", 0) or 0)
                    for p in prices
                )
            except Exception:
                pass

        return {
            "price_now":  round(price_now, 4),
            "price_24h":  round(price_24h, 4),
            "move_24h":   round(price_now - price_24h, 4),
            "volume_24h": round(volume_24h, 2),
            "source":     "pmxt",
        }

    except Exception as e:
        log_error("pmxt", f"pmxt fetch failed for {market_id}: {e}")
        return None


def _fetch_via_polymarket_fallback(token_id: str) -> Optional[Dict]:
    """
    Derive 24h move from polymarket.py's weekly history.
    Weekly history buckets are ~1h apart; take first and last points
    from the most recent 24 entries.
    """
    try:
        from proba.sources.polymarket import fetch_price_history

        history = fetch_price_history(token_id, interval="1w")
        if not history or len(history) < 2:
            return None

        # history is a list of {t: timestamp_ms, p: price} dicts
        now_ms    = datetime.now(timezone.utc).timestamp() * 1000
        cutoff_ms = now_ms - 86_400_000  # 24h ago

        recent = [h for h in history if float(h.get("t", 0)) >= cutoff_ms]
        if len(recent) < 2:
            # Not enough 24h data — use oldest available as proxy
            recent = history[-24:] if len(history) >= 24 else history

        price_now = float(recent[-1].get("p", 0))
        price_24h = float(recent[0].get("p", 0))

        # Volume not available from this endpoint — set to 0 so scorer
        # uses the market-level volume from Gamma instead
        return {
            "price_now":  round(price_now, 4),
            "price_24h":  round(price_24h, 4),
            "move_24h":   round(price_now - price_24h, 4),
            "volume_24h": 0.0,  # not available from weekly history
            "source":     "polymarket_fallback",
        }

    except Exception as e:
        log_error("pmxt", f"polymarket fallback failed for token_id={token_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Orderbook (pmxt path — optional, polymarket.py is primary for CLOB)
# ---------------------------------------------------------------------------

def fetch_orderbook_pmxt(market_id: str) -> Optional[Dict]:
    """
    Fetch orderbook via pmxt. Returns same shape as polymarket.py fetch_orderbook()
    so scorer_overreaction.py can use either interchangeably.
    """
    if not _PMXT_AVAILABLE:
        return None

    try:
        pm   = _Exchange("polymarket")
        book = pm.fetchOrderBook(market_id)

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        best_bid = float(bids[0][0]) if bids else None
        best_ask = float(asks[0][0]) if asks else None

        return {
            "bids":     bids,
            "asks":     asks,
            "best_bid": best_bid,
            "best_ask": best_ask,
        }

    except Exception as e:
        log_error("pmxt", f"orderbook fetch failed for {market_id}: {e}")
        return None
