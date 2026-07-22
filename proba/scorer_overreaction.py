"""
scorer_overreaction.py — Anti-Insanity (Overreaction Fading) scorer
Proba | NoLaptopTrades

Strategy: find markets that moved >15% in 24h driven by emotion/viral moment,
not by actual probability change. Fade the hype — buy NO on the overpriced YES side.

Signal conditions (all must pass):
  1. abs(price_now - price_24h_ago) >= min_move  (default 0.15)
  2. Market price vs base_rate gap >= min_gap     (default 0.10)
  3. YES price <= max_yes_price                   (default 0.40 — we only fade if extreme)
  4. volume >= min_volume_spike                   (confirms real momentum, not noise)

Direction: always BUY_NO — we're fading the pumped YES side.

Output dict shape is identical to scorer.py outputs so rank_scored(),
auto_trader.py, and postmortem.py consume it without changes.
"""

from datetime import datetime, timezone
from typing import Dict, Optional

from proba.paths import log_error
from proba.base_rate import estimate_base_rate, price_gap


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _get_or_cfg(cfg: Dict) -> Dict:
    """Extract overreaction sub-config with safe defaults."""
    return cfg.get("overreaction", {})


def _thresholds(cfg: Dict):
    or_cfg = _get_or_cfg(cfg)
    return (
        float(or_cfg.get("min_price_move_24h",    0.15)),
        float(or_cfg.get("min_volume_spike",      10_000)),
        float(or_cfg.get("min_gap_vs_base_rate",  0.10)),
        float(or_cfg.get("max_yes_price",         0.40)),
    )


# ---------------------------------------------------------------------------
# Edge and confidence
# ---------------------------------------------------------------------------

def _compute_edge(market_price: float, base_rate: float) -> float:
    """
    Edge of buying NO = (1 - market_price) vs true_no_prob = (1 - base_rate).
    Simplified: edge = base_rate - market_price (how overpriced YES is).
    A positive value means YES is overpriced → buying NO has positive EV.
    """
    return round(base_rate - market_price, 4)


def _conf_tier(gap: float, move: float) -> str:
    """Tier based on how large the gap and move are."""
    if gap >= 0.20 and move >= 0.25:
        return "high"
    if gap >= 0.12 or move >= 0.18:
        return "medium"
    return "low"


def _priority(conf: str, gap: float) -> int:
    """Lower = higher priority (matches scorer.py convention)."""
    if conf == "high":   return 2
    if conf == "medium": return 3
    return 4


def _vol_tier(volume: float) -> str:
    if volume >= 100_000: return "high"
    if volume >= 20_000:  return "medium"
    if volume >= 5_000:   return "low"
    return "thin"


# ---------------------------------------------------------------------------
# Main scorer
# ---------------------------------------------------------------------------

def score_overreaction(
    market: Dict,
    history_24h: Dict,
    cfg: Dict,
    position_size: Optional[float] = None,
) -> Optional[Dict]:
    """
    Score a market for overreaction signal.

    Args:
        market:      Gamma market dict (same shape as used in _run_polymarket).
        history_24h: Output of pmxt.fetch_price_history_24h() — contains
                     price_now, price_24h, move_24h, volume_24h.
        cfg:         Full proba config dict.

    Returns:
        Signal dict (same shape as scorer.py outputs) or None if no signal.
    """
    if not _get_or_cfg(cfg).get("enabled", True):
        return None

    min_move, min_vol_spike, min_gap, max_yes_price = _thresholds(cfg)

    # ── Validate 24h data ─────────────────────────────────────────────────
    if not history_24h:
        return None

    price_now  = history_24h.get("price_now")
    price_24h  = history_24h.get("price_24h")
    move_24h   = history_24h.get("move_24h")
    vol_24h    = history_24h.get("volume_24h", 0.0)

    if price_now is None or price_24h is None or move_24h is None:
        return None

    # ── Gate 1: price moved enough ────────────────────────────────────────
    if abs(move_24h) < min_move:
        return None

    # ── Gate 2: YES price must be in "extreme" zone to fade ───────────────
    # We only fade moves upward (hype pump). If price fell, skip.
    if move_24h <= 0:
        return None  # price fell — not a hype pump, skip

    if price_now > max_yes_price:
        return None  # >40% is plausible, not clearly extreme

    # ── Gate 3: base rate gap ─────────────────────────────────────────────
    title     = market.get("question", market.get("title", ""))
    base_rate = estimate_base_rate(title)
    gap       = price_gap(price_now, base_rate)

    if gap is None or gap < min_gap:
        # No known base rate or gap too small — skip
        return None

    # ── Gate 4: volume ────────────────────────────────────────────────────
    # Use vol_24h from pmxt if available; fall back to market-level volume
    market_volume = float(market.get("volume", 0) or market.get("volumeNum", 0) or 0)
    effective_vol = vol_24h if vol_24h > 0 else market_volume

    if effective_vol < min_vol_spike:
        return None

    # ── Build signal ──────────────────────────────────────────────────────
    edge       = _compute_edge(price_now, base_rate)
    confidence = _conf_tier(gap, abs(move_24h))
    priority   = _priority(confidence, gap)
    vol_tier   = _vol_tier(effective_vol)

    # NO price is what we'd buy
    no_price   = round(1.0 - price_now, 4)
    fill_price = no_price  # no CLOB fetch in this scorer — use mid as fill estimate

    # Days to close
    try:
        closes_raw = market.get("endDate", market.get("closes_at", ""))
        dt   = datetime.fromisoformat(closes_raw.replace("Z", "+00:00"))
        days = max((dt - datetime.now(timezone.utc)).total_seconds() / 86400.0, 0.0)
    except Exception:
        days = 0.0

    # Token IDs
    clob_ids = market.get("clobTokenIds", [])
    yes_token = str(clob_ids[0]) if len(clob_ids) > 0 else None
    no_token  = str(clob_ids[1]) if len(clob_ids) > 1 else None

    if position_size is None:
        position_size = float(cfg.get("paper", {}).get("default_position_size_usd", 40))

    return {
        # Identification
        "source":          "polymarket",
        "market_id":       str(market.get("conditionId", market.get("id", ""))),
        "event_id":        str(market.get("conditionId", market.get("id", ""))),
        "outcome_id":      no_token or "",
        "yes_token_id":    yes_token,
        "no_token_id":     no_token,
        "trade_token_id":  no_token,  # we're always buying NO

        # Market info
        "title":           title,
        "category":        market.get("_category_name", market.get("category", "politics")),

        # Signal
        "signal":          "BUY_NO",
        "zone":            "OVERREACTION_FADE",
        "direction":       "no",
        "priority":        priority,
        "strategy_type":   "overreaction",

        # Prices
        "gamma_price":     price_now,   # YES price (the one we're fading)
        "best_bid":        None,        # no CLOB fetch at this stage
        "best_ask":        None,
        "mid":             no_price,
        "fill_price":      fill_price,
        "spread":          0.0,
        "slippage":        0.0,
        "market_price":    no_price,

        # Edge
        "estimated_prob":  round(1.0 - base_rate, 4),  # true NO probability
        "edge":            edge,
        "confidence":      confidence,
        "your_estimate":   round(1.0 - base_rate, 4),

        # Overreaction-specific diagnostics
        "price_24h_ago":   round(price_24h, 4),
        "price_move_24h":  round(move_24h, 4),
        "base_rate":       base_rate,
        "base_rate_gap":   gap,
        "history_source":  history_24h.get("source", "unknown"),

        # Liquidity
        "volume":          effective_vol,
        "liquidity":       effective_vol,
        "volume_tier":     vol_tier,
        "price_momentum":  "rising",   # by construction — price pumped up

        # Lifecycle
        "days_to_close":   round(days, 2),
        "closes_at":       market.get("endDate", market.get("closes_at", "")),
        "score_ts":        datetime.now(timezone.utc).isoformat(),
        "position_size_usd": position_size,
    }
