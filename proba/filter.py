"""
filter.py — quality gate for both Polymarket and Futuur markets
Proba | NoLaptopTrades

Polymarket market fields: question, conditionId, outcomePrices, volume24hr,
    liquidity, endDate, active, closed, enableOrderBook
Futuur market fields: id, title, closes_at, resolved, markets[].outcomes[].price, volume
"""

import json
from datetime import datetime, timezone
from typing import Dict, List, Set, Tuple

from proba.paths import get_path, log_error


# ---------------------------------------------------------------------------
# Shared timing helper
# ---------------------------------------------------------------------------

def _days_remaining(date_str: str) -> float:
    if not date_str:
        return -1.0
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return (dt - datetime.now(timezone.utc)).total_seconds() / 86400.0
    except Exception:
        return -1.0


def _load_tracked_ids(source: str) -> Set[str]:
    """Load condition_ids or event_ids already in open paper positions."""
    try:
        path = get_path("paper_positions")
        if not path.exists():
            return set()
        ids = set()
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    pos = json.loads(line)
                    if pos.get("status") == "open" and pos.get("source") == source:
                        ids.add(str(pos.get("market_id", "")))
                except Exception:
                    pass
        return ids
    except Exception as e:
        log_error("filter", f"Could not load tracked ids: {e}")
        return set()


# ---------------------------------------------------------------------------
# Polymarket filter
# ---------------------------------------------------------------------------

def filter_polymarket_markets(markets: List[Dict], cfg: Dict) -> List[Dict]:
    """
    Filter Polymarket Gamma market objects.
    NOTE: require_binary is intentionally ignored for Polymarket —
    multi-outcome markets (World Cup Winner etc.) are handled by the scorer
    which evaluates each outcome individually.
    """
    f     = cfg.get("filter", {})
    d     = cfg.get("discovery", {})
    min_liq   = f.get("min_liquidity_usd", 500)
    near_zero = f.get("skip_price_near_zero", 0.03)
    near_one  = f.get("skip_price_near_one",  0.97)
    max_days  = d.get("max_days_to_close", 7)
    min_days  = d.get("min_days_to_close", 0.5)

    tracked = _load_tracked_ids("polymarket")
    passed  = []
    killed  = {k: 0 for k in ("inactive","decided","timing","liquidity","duplicate","no_book")}

    for m in markets:
        cid = str(m.get("conditionId", m.get("id", "")))

        if not m.get("active") or m.get("closed"):
            killed["inactive"] += 1
            continue

        if not m.get("enableOrderBook", False):
            killed["no_book"] += 1
            continue

        if cid in tracked:
            killed["duplicate"] += 1
            continue

        days = _days_remaining(m.get("endDate", ""))
        if not (min_days <= days <= max_days):
            killed["timing"] += 1
            continue

        liq = float(m.get("liquidity", 0) or 0)
        vol = float(m.get("volume24hr", m.get("volume", 0)) or 0)
        if liq < min_liq and vol < (min_liq * 2):
            killed["liquidity"] += 1
            continue

        # Skip markets where ALL outcomes are decided (near 0 or 1)
        op = m.get("outcomePrices", [])
        if op:
            try:
                prices = [float(p) for p in op if p is not None]
                if prices and all(p < near_zero or p > near_one for p in prices):
                    killed["decided"] += 1
                    continue
            except Exception:
                pass

        passed.append(m)

    n_in = len(markets)
    if n_in > 0:
        log_error("filter", (
            f"[PM] {n_in}→{len(passed)} passed "
            f"[inactive:{killed['inactive']} no_book:{killed['no_book']} "
            f"timing:{killed['timing']} liq:{killed['liquidity']} "
            f"decided:{killed['decided']} dup:{killed['duplicate']}]"
        ))
    return passed


# ---------------------------------------------------------------------------
# Futuur filter (unchanged logic, correct field names)
# ---------------------------------------------------------------------------

def filter_futuur_markets(markets: List[Dict], cfg: Dict) -> List[Dict]:
    """
    Filter Futuur list-level event objects.
    At list level: id, title, category(int), closes_at, resolved,
                   pending_resolution, tags(str[]), currency_mode
    No markets/prices yet — those come from fetch_event_detail().
    We filter on timing and status only at this stage.
    """
    f    = cfg.get("filter", {})
    d    = cfg.get("discovery", {})
    max_days  = d.get("max_days_to_close", 7)
    min_days  = d.get("min_days_to_close", 0.5)

    tracked = _load_tracked_ids("futuur")
    passed  = []
    killed  = {k: 0 for k in ("resolved","timing","duplicate","currency")}

    # Match configured currency mode
    currency_mode = d.get("currency_mode", "real_money")

    for ev in markets:
        eid = str(ev.get("id", ""))

        # Currency mode filter
        ev_currency = ev.get("currency_mode", "")
        if ev_currency and ev_currency != currency_mode:
            killed["currency"] += 1
            continue

        # Already resolved or pending
        if ev.get("resolved") or ev.get("pending_resolution"):
            killed["resolved"] += 1
            continue

        # Duplicate guard
        if eid in tracked:
            killed["duplicate"] += 1
            continue

        # Timing — closes_at is correct field name per real API docs
        days = _days_remaining(ev.get("closes_at", ""))
        if not (min_days <= days <= max_days):
            killed["timing"] += 1
            continue

        passed.append(ev)

    n_in = len(markets)
    if n_in > 0:
        log_error("filter", (
            f"[FT] {n_in}→{len(passed)} passed "
            f"[resolved:{killed['resolved']} timing:{killed['timing']} "
            f"dup:{killed['duplicate']} currency:{killed['currency']}]"
        ))
    return passed


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------

def filter_markets(markets: List[Dict], cfg: Dict) -> List[Dict]:
    """Route to correct filter based on _source field."""
    pm  = [m for m in markets if m.get("_source") == "polymarket"]
    ft  = [m for m in markets if m.get("_source") != "polymarket"]
    return filter_polymarket_markets(pm, cfg) + filter_futuur_markets(ft, cfg)
