"""
auto_trader.py — automated paper trade decision engine
Proba | NoLaptopTrades

Decision rules (applied to all sources equally):
  1. Zone must be approved (FAVOURITE_STRONG, FAVOURITE_MILD, etc.)
  2. Confidence medium or high
  3. Volume tier not thin
  4. Days to close: within configured window
  5. Momentum not strongly against direction
  6. Spread not too wide (Polymarket CLOB only — > 6 cents = skip)
  7. No duplicate event
  8. Under max open positions cap

Each position tagged with:
  source:       "polymarket" | "futuur"
  strategy_id:  from config — lets you run two phones with different configs
"""

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from proba.paths import get_path, get_setting, log_error


APPROVED_ZONES      = {"FAVOURITE_STRONG", "FAVOURITE_MILD", "FAVOURITE_LIGHT",
                       "LONGSHOT_FADE", "LONGSHOT_FADE_MILD",
                       "OVERREACTION_FADE"}
APPROVED_CONFIDENCE = {"high", "medium"}
APPROVED_VOL_TIERS  = {"high", "medium", "low"}
MAX_SPREAD_CLOB     = 0.06   # 6 cents — above this market is too illiquid


def _load_open(source: Optional[str] = None) -> Tuple[int, set]:
    """Return (count, set_of_open_market_ids) optionally filtered by source."""
    path = get_path("paper_positions")
    if not path.exists():
        return 0, set()
    count, mids = 0, set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                pos = json.loads(line)
                if pos.get("status") == "open":
                    if source is None or pos.get("source") == source:
                        count += 1
                        mids.add(str(pos.get("market_id", "")))
            except Exception:
                pass
    return count, mids


def decide(scored: Dict, cfg: Dict) -> Tuple[bool, str]:
    """Apply all decision rules. Returns (approved, reason)."""
    auto_cfg  = cfg.get("auto_trader", {})
    max_open  = auto_cfg.get("max_open_positions", 10)
    min_days  = auto_cfg.get("min_days_to_close", 0.5)
    max_days  = auto_cfg.get("max_days_to_close", 5.0)
    min_vol   = auto_cfg.get("min_volume_usd", 1000)

    if scored.get("zone") not in APPROVED_ZONES:
        return False, f"zone={scored.get('zone')} not actionable"

    if scored.get("confidence") not in APPROVED_CONFIDENCE:
        return False, f"confidence={scored.get('confidence')} below threshold"

    if scored.get("volume_tier") not in APPROVED_VOL_TIERS:
        return False, f"volume_tier={scored.get('volume_tier')} too thin"

    # Hard volume floor — never trade zero-liquidity markets
    volume = float(scored.get("volume") or scored.get("liquidity") or 0)
    if volume < min_vol:
        return False, f"volume=${volume:,.0f} below min ${min_vol:,.0f}"

    days = scored.get("days_to_close", 0)
    if not (min_days <= days <= max_days):
        return False, f"days_to_close={days:.1f} outside [{min_days},{max_days}]"

    direction = scored.get("direction", "yes")
    momentum  = scored.get("price_momentum", "flat")
    if direction == "yes" and momentum == "falling":
        return False, "momentum falling against BUY_YES"
    if direction == "no" and momentum == "rising":
        return False, "momentum rising against BUY_NO"

    source = scored.get("source", "")
    spread = scored.get("spread")
    if source == "polymarket" and spread is not None and spread > MAX_SPREAD_CLOB:
        return False, f"spread={spread:.3f} too wide (>{MAX_SPREAD_CLOB})"

    open_count, open_mids = _load_open(source)
    mid = str(scored.get("market_id", ""))
    if mid in open_mids:
        return False, "already tracking this market"
    if open_count >= max_open:
        return False, f"at max open positions for {source} ({max_open})"

    return True, "all rules passed"


def auto_log_trade(scored: Dict, cfg: Dict) -> Optional[str]:
    """Log an approved paper trade. Returns position_id or None."""
    approved, reason = decide(scored, cfg)

    if not approved:
        log_error("auto_trader",
                  f"SKIP [{scored.get('source','?')}] "
                  f"'{scored.get('title','')[:40]}' — {reason}")
        return None

    position_id = str(uuid4())
    size_usd    = scored.get("position_size_usd",
                  get_setting("paper.default_position_size_usd", 40))
    phase       = get_setting("paper.phase", 0)
    strategy_id = cfg.get("strategy_id", "default")

    position = {
        "id":               position_id,
        "source":           scored.get("source", "unknown"),
        "strategy_id":      strategy_id,
        "strategy_type":    scored.get("strategy_type", "bias"),

        # Market identifiers
        "market_id":        str(scored.get("market_id", "")),
        "event_id":         str(scored.get("event_id", "")),
        "yes_token_id":     scored.get("yes_token_id"),
        "no_token_id":      scored.get("no_token_id"),
        "trade_token_id":   scored.get("trade_token_id"),
        "outcome_id":       scored.get("outcome_id"),

        # Market info
        "title":            scored.get("title", ""),
        "category":         scored.get("category", ""),

        # Trade direction
        "direction":        scored.get("direction", "yes"),
        "signal":           scored.get("signal", ""),
        "zone":             scored.get("zone", ""),

        # PRICE ACCURACY — this is what makes paper trading honest
        "gamma_price":      scored.get("gamma_price"),       # Gamma API price (may lag)
        "best_bid":         scored.get("best_bid"),          # Live CLOB bid at entry
        "best_ask":         scored.get("best_ask"),          # Live CLOB ask at entry
        "mid":              scored.get("mid"),               # Midpoint at entry
        "fill_price":       scored.get("fill_price"),        # Simulated fill (ask for buys)
        "spread":           scored.get("spread"),            # Bid-ask spread at entry
        "slippage":         scored.get("slippage"),          # Extra cost at position size
        "entry_price":      scored.get("fill_price",         # Canonical entry = fill price
                            scored.get("market_price", 0)),

        # Edge against fill price (not midpoint)
        "your_estimate":    scored.get("estimated_prob"),
        "estimated_prob":   scored.get("estimated_prob"),
        "edge":             scored.get("edge"),
        "confidence":       scored.get("confidence"),

        # Liquidity
        "volume":           scored.get("volume"),
        "volume_tier":      scored.get("volume_tier"),
        "price_momentum":   scored.get("price_momentum"),

        # Position sizing
        "position_size_usd": size_usd,

        # Lifecycle
        "entry_ts":         datetime.now(timezone.utc).isoformat(),
        "close_date":       scored.get("closes_at", ""),
        "status":           "open",
        "phase":            phase,
        "auto":             True,

        "reasoning": (
            f"AUTO [{scored.get('source','?').upper()}]: {scored.get('zone')} | "
            f"fill={scored.get('fill_price',0):.2%} (ask) | "
            f"spread={scored.get('spread',0):.3f} | "
            f"edge={scored.get('edge',0):+.2%} vs fill | "
            f"vol={scored.get('volume',0):,.0f} | "
            f"momentum={scored.get('price_momentum','?')}"
        ),
    }

    path = get_path("paper_positions")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(position) + "\n")

    log_error("auto_trader",
              f"TRADE [{position_id[:8]}] "
              f"[{scored.get('source','?').upper()}] "
              f"{scored.get('direction','?').upper()} "
              f"'{scored.get('title','')[:50]}' "
              f"@ fill={scored.get('fill_price',0):.2%} | "
              f"edge={scored.get('edge',0):+.2%} | "
              f"zone={scored.get('zone','?')}")

    return position_id


def process_batch(ranked_scored: List[Dict], cfg: Dict) -> List[Dict]:
    """Process ranked list, log approved trades. Returns logged positions."""
    logged = []
    for scored in ranked_scored:
        pos_id = auto_log_trade(scored, cfg)
        if pos_id:
            logged.append({**scored, "position_id": pos_id})
    return logged
