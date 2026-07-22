"""
scorer.py — automated probability scorer
Proba | NoLaptopTrades

Strategy: Favourite-Longshot Bias exploitation.
Works on both binary (Yes/No) AND multi-outcome markets (World Cup winner etc.)
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from proba.paths import log_error

# ---------------------------------------------------------------------------
# Bias model
# ---------------------------------------------------------------------------

BIAS_TABLE = [
    (0.00, 0.05, -0.00),
    (0.05, 0.15, -0.08),
    (0.15, 0.25, -0.05),
    (0.25, 0.40, -0.02),
    (0.40, 0.50, -0.01),
    (0.50, 0.65,  0.05),
    (0.65, 0.80,  0.10),
    (0.80, 0.90,  0.07),
    (0.90, 1.00,  0.02),
]

SIGNAL_ZONES = {
    (0.65, 0.80): ("BUY_YES", "FAVOURITE_STRONG",   "yes", 1),
    (0.50, 0.65): ("BUY_YES", "FAVOURITE_MILD",     "yes", 2),
    (0.80, 0.90): ("BUY_YES", "FAVOURITE_LIGHT",    "yes", 3),
    (0.15, 0.25): ("BUY_NO",  "LONGSHOT_FADE_MILD", "no",  4),
    (0.05, 0.15): ("BUY_NO",  "LONGSHOT_FADE",      "no",  5),
}


def _bias_adj(price: float) -> float:
    for low, high, adj in BIAS_TABLE:
        if low <= price < high:
            return adj
    return 0.0


def _est_true_prob(price: float) -> float:
    return min(0.99, max(0.01, price + _bias_adj(price)))


def _classify(price: float) -> Tuple[str, str, str, int]:
    for (low, high), (sig, zone, direction, pri) in SIGNAL_ZONES.items():
        if low <= price < high:
            return sig, zone, direction, pri
    return "NO_SIGNAL", "OUT_OF_ZONE", "skip", 99


# ---------------------------------------------------------------------------
# Config helpers (version-aware)
# ---------------------------------------------------------------------------

def _get_thresholds(cfg: Dict) -> Tuple[float, float, float]:
    """Returns (min_edge, conf_high, conf_medium) — handles stale configs."""
    meta_ver = cfg.get("_meta", {}).get("version", "0.1.0")
    if meta_ver < "0.2.0":
        return 0.04, 0.08, 0.05
    min_edge    = cfg.get("scorer", {}).get("min_edge",          0.04)
    conf_high   = cfg.get("scorer", {}).get("confidence_high",   0.08)
    conf_medium = cfg.get("scorer", {}).get("confidence_medium",  0.05)
    if min_edge > 0.5 or conf_high > 0.5:
        return 0.04, 0.08, 0.05
    return min_edge, conf_high, conf_medium


def _conf_tier(edge: float, conf_high: float, conf_medium: float) -> str:
    abs_e = abs(edge)
    if abs_e >= conf_high:   return "high"
    if abs_e >= conf_medium: return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Momentum helpers
# ---------------------------------------------------------------------------

def _momentum_from_clob_history(history: List[Dict], direction: str) -> str:
    prices = []
    for pt in history:
        try:
            prices.append(float(pt.get("p", pt.get("price", 0.5))))
        except Exception:
            pass
    if len(prices) < 3:
        return "flat"
    third = max(1, len(prices) // 3)
    delta = sum(prices[-third:]) / third - sum(prices[:third]) / third
    if direction == "no":
        delta = -delta
    if delta > 0.03:  return "rising"
    if delta < -0.03: return "falling"
    return "flat"


def _momentum_from_futuur_history(history: List[Dict], direction: str) -> str:
    prices = []
    for pt in history:
        for oc in pt.get("outcomes", []):
            if oc.get("name", "").lower() in ("yes", "true"):
                try:
                    prices.append(float(oc["price"]))
                except Exception:
                    pass
                break
    if len(prices) < 3:
        return "flat"
    third = max(1, len(prices) // 3)
    delta = sum(prices[-third:]) / third - sum(prices[:third]) / third
    if direction == "no":
        delta = -delta
    if delta > 0.03:  return "rising"
    if delta < -0.03: return "falling"
    return "flat"


# ---------------------------------------------------------------------------
# Volume tier
# ---------------------------------------------------------------------------

def _vol_tier(volume: float, source: str) -> str:
    if source == "polymarket":
        if volume >= 50000: return "high"
        if volume >= 5000:  return "medium"
        if volume >= 1000:  return "low"
        return "thin"
    else:
        if volume >= 2000:  return "high"
        if volume >= 500:   return "medium"
        if volume >= 100:   return "low"
        return "thin"


# ---------------------------------------------------------------------------
# Score: Polymarket
# Handles both binary (2 outcomes) and multi-outcome markets
# ---------------------------------------------------------------------------

def score_polymarket_market(
    market: Dict,
    history: List[Dict],
    orderbook: Dict,
    cfg: Dict,
    position_size_usd: float = 40.0,
) -> Optional[Dict]:
    """
    Score a Polymarket market.
    Evaluates ALL outcome prices — takes the best bias signal found.
    Works on binary Yes/No AND multi-outcome (World Cup winner etc.)
    """
    from proba.sources.polymarket import (
        get_yes_token_id, get_no_token_id, days_to_close
    )

    min_edge, conf_high, conf_medium = _get_thresholds(cfg)

    # outcomePrices may be a JSON-encoded string: '["0.65","0.35"]'
    import json as _json
    op = market.get("outcomePrices", [])
    if not op:
        return None
    try:
        if isinstance(op, str):
            all_prices = [float(p) for p in _json.loads(op) if p is not None]
        elif isinstance(op, list):
            all_prices = [float(p) for p in op if p is not None]
        else:
            return None
    except Exception:
        return None
    if not all_prices:
        return None

    clob_ids = market.get("clobTokenIds", [])

    # Evaluate every outcome, keep best signal
    best = None
    for idx, price in enumerate(all_prices):
        signal, zone, direction, priority = _classify(price)
        if signal == "NO_SIGNAL":
            continue

        # trade_price: for BUY_YES = price, for BUY_NO = 1 - price
        trade_price = price if direction == "yes" else round(1.0 - price, 4)
        edge_pre    = round(_est_true_prob(trade_price) - trade_price, 4)
        if abs(edge_pre) < min_edge:
            continue

        token_id = str(clob_ids[idx]) if idx < len(clob_ids) and clob_ids[idx] else None

        if best is None or priority < best["priority"]:
            best = {
                "idx": idx, "price": price, "trade_price": trade_price,
                "signal": signal, "zone": zone, "direction": direction,
                "priority": priority, "token_id": token_id,
            }

    if not best:
        return None

    # CLOB orderbook for fill price
    bids = (orderbook or {}).get("bids", [])
    asks = (orderbook or {}).get("asks", [])

    if bids and asks:
        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        spread   = round(best_ask - best_bid, 4)
        if spread > 0.08:
            return None
        mid        = round((best_bid + best_ask) / 2, 4)
        fill_price = best_ask if best["direction"] == "yes" else round(1.0 - best_bid, 4)
    else:
        # Gamma fallback — assume 2-cent spread
        t = best["trade_price"]
        best_bid   = round(t - 0.01, 4)
        best_ask   = round(t + 0.01, 4)
        spread     = 0.02
        mid        = t
        fill_price = round(t + 0.01, 4)

    # Final edge vs fill price
    est_prob = _est_true_prob(fill_price)
    edge     = round(est_prob - fill_price, 4)
    if abs(edge) < min_edge:
        return None

    confidence = _conf_tier(edge, conf_high, conf_medium)
    volume     = float(market.get("volume24hr", market.get("volume", 0)) or 0)
    liq        = float(market.get("liquidity", 0) or 0)
    vol_tier   = _vol_tier(max(volume, liq), "polymarket")
    momentum   = _momentum_from_clob_history(history, best["direction"])
    days       = days_to_close(market)

    priority = best["priority"]
    if momentum == "falling" and best["direction"] == "yes": priority += 1
    if momentum == "rising"  and best["direction"] == "no":  priority += 1
    if vol_tier == "thin": priority += 2

    yes_tid = get_yes_token_id(market)
    no_tid  = get_no_token_id(market)

    return {
        "source":          "polymarket",
        "market_id":       str(market.get("conditionId", market.get("id", ""))),
        "yes_token_id":    yes_tid,
        "no_token_id":     no_tid,
        "trade_token_id":  best["token_id"] or yes_tid,
        "event_id":        str(market.get("_event_id", "")),
        "title":           market.get("question", market.get("_event_title", "")),
        "category":        "sports",
        "signal":          best["signal"],
        "zone":            best["zone"],
        "direction":       best["direction"],
        "priority":        priority,
        "gamma_price":     round(best["price"], 4),
        "best_bid":        best_bid,
        "best_ask":        best_ask,
        "mid":             mid,
        "fill_price":      fill_price,
        "spread":          spread,
        "slippage":        0.0,
        "market_price":    fill_price,
        "estimated_prob":  round(est_prob, 4),
        "edge":            edge,
        "confidence":      confidence,
        "your_estimate":   None,
        "volume":          volume,
        "liquidity":       liq,
        "volume_tier":     vol_tier,
        "days_to_close":   round(days, 2),
        "closes_at":       market.get("endDate", ""),
        "price_momentum":  momentum,
        "score_ts":        datetime.now(timezone.utc).isoformat(),
        "position_size_usd": position_size_usd,
    }


# ---------------------------------------------------------------------------
# Score: Futuur (AMM)
# ---------------------------------------------------------------------------

def score_futuur_market(
    event: Dict,
    history: List[Dict],
    cfg: Dict,
) -> Optional[Dict]:
    """Score a Futuur event (AMM — no orderbook)."""
    min_edge, conf_high, conf_medium = _get_thresholds(cfg)

    markets = event.get("markets", [])
    primary = next((m for m in markets if len(m.get("outcomes", [])) == 2), None)
    if not primary:
        return None

    outcomes = primary.get("outcomes", [])
    yes_oc = next((o for o in outcomes if o.get("name","").lower() in ("yes","true")), None)
    if not yes_oc:
        yes_oc = max(outcomes, key=lambda o: float(o.get("price", 0)))
    no_oc = next((o for o in outcomes if o.get("id") != yes_oc.get("id")), None)

    yes_price = float(yes_oc.get("price", 0.5))
    no_price  = float(no_oc.get("price", 0.5)) if no_oc else round(1 - yes_price, 4)

    signal, zone, direction, priority = _classify(yes_price)
    if signal == "NO_SIGNAL":
        return None

    trade_price = yes_price if direction == "yes" else no_price
    est_prob    = _est_true_prob(trade_price)
    edge        = round(est_prob - trade_price, 4)
    if abs(edge) < min_edge:
        return None

    confidence = _conf_tier(edge, conf_high, conf_medium)
    volume     = float(event.get("volume", 0) or 0)
    vol_tier   = _vol_tier(volume, "futuur")
    momentum   = _momentum_from_futuur_history(history, direction)

    try:
        dt   = datetime.fromisoformat(event.get("closes_at","").replace("Z","+00:00"))
        days = max((dt - datetime.now(timezone.utc)).total_seconds() / 86400.0, 0.0)
    except Exception:
        days = 0.0

    if momentum == "falling" and direction == "yes": priority += 1
    if vol_tier == "thin": priority += 2

    return {
        "source":          "futuur",
        "market_id":       str(event.get("id", "")),
        "event_id":        str(event.get("id", "")),
        "outcome_id":      str(yes_oc["id"] if direction=="yes" else (no_oc["id"] if no_oc else "")),
        "yes_token_id":    None,
        "no_token_id":     None,
        "trade_token_id":  None,
        "title":           event.get("title", ""),
        "category":        event.get("_category_name", "sports"),
        "signal":          signal,
        "zone":            zone,
        "direction":       direction,
        "priority":        priority,
        "gamma_price":     None,
        "best_bid":        None,
        "best_ask":        None,
        "mid":             trade_price,
        "fill_price":      trade_price,
        "spread":          0.0,
        "slippage":        0.0,
        "market_price":    trade_price,
        "estimated_prob":  round(est_prob, 4),
        "edge":            edge,
        "confidence":      confidence,
        "your_estimate":   None,
        "volume":          volume,
        "liquidity":       volume,
        "volume_tier":     vol_tier,
        "days_to_close":   round(days, 2),
        "closes_at":       event.get("closes_at", ""),
        "price_momentum":  momentum,
        "score_ts":        datetime.now(timezone.utc).isoformat(),
        "position_size_usd": cfg.get("paper", {}).get("default_position_size_usd", 40),
    }


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def rank_scored(scored: List[Dict]) -> List[Dict]:
    return sorted(scored, key=lambda s: (
        s["priority"],
        -abs(s.get("edge", 0)),
        -s.get("volume", 0),
    ))
