"""
sources/polymarket.py — Polymarket API client
Proba | NoLaptopTrades

Gamma API:  https://gamma-api.polymarket.com  — public, no auth
CLOB API:   https://clob.polymarket.com       — public reads, auth for orders
Data API:   https://data-api.polymarket.com   — public

Sports discovery: fetch all active events by volume, filter client-side.
tag_id params are unreliable (return 422). Volume sort + keyword filter is robust.
"""

import os
import time
import hmac
import hashlib
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

from proba.paths import log_error

load_dotenv()

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL  = "https://clob.polymarket.com"
TIMEOUT   = 20

# Sports keywords for client-side filtering of event titles/tags
SPORTS_KEYWORDS = [
    "world cup", "fifa", "soccer", "football", "nfl", "nba", "mlb", "nhl",
    "ufc", "mma", "tennis", "wimbledon", "f1", "formula", "cricket", "rugby",
    "olympic", "championship", "league", "premier", "bundesliga", "serie a",
    "la liga", "champions league", "europa", "copa", "euros", "euro 2026",
    "match", "game", "win", "beat", "advance", "qualify", "knockout",
    "quarterfinal", "semifinal", "final", "playoff",
]

# Non-sports keywords to explicitly exclude (crypto price intervals etc.)
EXCLUDE_KEYWORDS = [
    "up or down", "price", "btc", "eth", "xrp", "sol", "doge",
    "will the fed", "interest rate", "inflation", "gdp",
    "ipo", "stock", "acquisition",
]


def _is_sports_event(event: dict) -> bool:
    """Client-side sports filter — check title, tags, slug."""
    title = (event.get("title") or event.get("question") or "").lower()
    slug  = (event.get("slug") or "").lower()
    tags  = [str(t.get("label","") or t.get("slug","") or t.get("name","")).lower()
             for t in (event.get("tags") or [])]
    text  = f"{title} {slug} {' '.join(tags)}"

    # Explicit exclude
    if any(kw in text for kw in EXCLUDE_KEYWORDS):
        return False

    # Must match at least one sports keyword
    return any(kw in text for kw in SPORTS_KEYWORDS)


def _is_sports_market(market: dict, event_title: str = "") -> bool:
    """Check if a market (inside an event) is sports-related."""
    q    = (market.get("question") or market.get("title") or "").lower()
    text = f"{q} {event_title.lower()}"
    if any(kw in text for kw in EXCLUDE_KEYWORDS):
        return False
    # Either the market question mentions sports, or inherits from event
    return any(kw in text for kw in SPORTS_KEYWORDS) or bool(event_title)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _gamma_get(path: str, params: dict = None) -> dict | list:
    url = f"{GAMMA_URL}{path}"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Proba/0.3)"}
    try:
        r = requests.get(url, params=params or {}, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        log_error("polymarket", f"Gamma {r.status_code} GET {path}: {r.text[:200]}")
        raise
    except requests.RequestException as e:
        log_error("polymarket", f"Gamma request failed GET {path}: {e}")
        raise


def _clob_get(path: str, params: dict = None) -> dict | list:
    """CLOB public read — falls back gracefully if DNS fails."""
    url = f"{CLOB_URL}{path}"
    try:
        r = requests.get(url, params=params or {}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError as e:
        # DNS or connection failure — CLOB may be blocked on this network
        log_error("polymarket", f"CLOB unreachable (DNS/connection): {e}")
        return {}
    except requests.HTTPError as e:
        log_error("polymarket", f"CLOB {r.status_code} GET {path}: {r.text[:200]}")
        return {}
    except requests.RequestException as e:
        log_error("polymarket", f"CLOB request failed GET {path}: {e}")
        return {}


# ---------------------------------------------------------------------------
# Gamma API — market discovery
# ---------------------------------------------------------------------------

def fetch_sports_markets(cfg: dict) -> List[Dict]:
    """
    Fetch active sports markets from Gamma API.

    Strategy: fetch all active events by 24hr volume (no tag_id — unreliable),
    filter client-side for sports keywords. This is robust across Polymarket
    API changes and doesn't depend on numeric tag IDs.
    """
    disc  = cfg.get("discovery", {})
    limit = min(disc.get("max_results", 100), 100)

    # Fetch top events by 24hr volume — sports events dominate when active
    params = {
        "active":    "true",
        "closed":    "false",
        "order":     "volume24hr",
        "ascending": "false",
        "limit":     limit,
    }

    try:
        data = _gamma_get("/events", params=params)
    except Exception as e:
        log_error("polymarket", f"fetch_sports_markets failed: {e}")
        return []

    events = data if isinstance(data, list) else []
    if not events:
        log_error("polymarket", "fetch_sports_markets: empty response from /events")
        return []

    # Flatten events → markets, filtering for sports
    markets: List[Dict] = []
    for ev in events:
        ev_title = ev.get("title", "")

        # Check if the event itself is sports-related
        ev_is_sports = _is_sports_event(ev)

        for mkt in ev.get("markets", []):
            if not mkt.get("active") or mkt.get("closed"):
                continue
            if not mkt.get("enableOrderBook", False):
                continue

            # Skip if no prices available
            op = mkt.get("outcomePrices")
            if not op:
                continue

            # Sports filter
            if not ev_is_sports and not _is_sports_market(mkt, ev_title):
                continue

            # Quality filter — skip exact score, handicap, prop markets
            if not _is_quality_market(mkt):
                continue

            # Attach event metadata
            mkt["_event_id"]    = ev.get("id", "")
            mkt["_event_title"] = ev_title
            mkt["_source"]      = "polymarket"
            markets.append(mkt)

    log_error("polymarket",
              f"fetch_sports_markets: {len(events)} events → {len(markets)} sports markets")
    return markets


def fetch_market_by_slug(slug: str) -> Dict:
    """Fetch a specific market by slug."""
    try:
        data = _gamma_get("/events", params={"slug": slug})
        events = data if isinstance(data, list) else []
        return events[0] if events else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# CLOB API — orderbook (public reads, may be DNS-blocked on some networks)
# ---------------------------------------------------------------------------

def fetch_orderbook(token_id: str) -> Dict:
    """
    GET /book?token_id={id}
    Returns {bids:[{price,size}], asks:[{price,size}]} or {} if CLOB unreachable.
    Falls back to Gamma prices if empty.
    """
    if not token_id:
        return {}
    return _clob_get("/book", params={"token_id": token_id})


def fetch_best_prices(token_id: str) -> Tuple[Optional[float], Optional[float]]:
    """Returns (best_bid, best_ask) or (None, None) if unreachable."""
    book = fetch_orderbook(token_id)
    if not book:
        return None, None
    try:
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        return (float(bids[0]["price"]) if bids else None,
                float(asks[0]["price"]) if asks else None)
    except Exception:
        return None, None


def estimate_fill_price(token_id: str, side: str, amount_usd: float) -> Optional[float]:
    """
    Walk the orderbook to estimate fill price at given size.
    Returns None if orderbook unreachable (caller uses Gamma price as fallback).
    """
    book = fetch_orderbook(token_id)
    if not book:
        return None
    orders = book.get("asks", []) if side == "BUY" else book.get("bids", [])
    if not orders:
        return None

    remaining, total_cost, total_shares = amount_usd, 0.0, 0.0
    for level in orders:
        price = float(level["price"])
        size  = float(level["size"])
        cost  = size * price
        if remaining <= 0:
            break
        if cost <= remaining:
            total_cost   += cost
            total_shares += size
            remaining    -= cost
        else:
            shares = remaining / price
            total_cost   += remaining
            total_shares += shares
            remaining     = 0

    return round(total_cost / total_shares, 4) if total_shares > 0 else None


def fetch_price_history(token_id: str, interval: str = "1w") -> List[Dict]:
    """GET /prices-history — returns [{t, p}] or [] if unreachable."""
    if not token_id:
        return []
    data = _clob_get("/prices-history", params={
        "market":   token_id,
        "interval": interval,
        "fidelity": 60,
    })
    if isinstance(data, dict):
        return data.get("history", [])
    return []


# ---------------------------------------------------------------------------
# Market structure helpers
# ---------------------------------------------------------------------------

def get_yes_token_id(market: dict) -> Optional[str]:
    """
    Extract YES outcome token ID.
    Polymarket Gamma uses clobTokenIds: [yes_id, no_id]
    tokens[] field exists but token_id may be blank — clobTokenIds is reliable.
    """
    # Primary: clobTokenIds[0] = YES
    clob = market.get("clobTokenIds", [])
    if clob and clob[0]:
        return str(clob[0])
    # Fallback: tokens array
    for t in market.get("tokens", []):
        if str(t.get("outcome", "")).upper() == "YES":
            tid = t.get("token_id") or t.get("tokenId") or t.get("id")
            if tid:
                return str(tid)
    return None


def get_no_token_id(market: dict) -> Optional[str]:
    """Extract NO outcome token ID. clobTokenIds[1] = NO."""
    clob = market.get("clobTokenIds", [])
    if len(clob) > 1 and clob[1]:
        return str(clob[1])
    for t in market.get("tokens", []):
        if str(t.get("outcome", "")).upper() == "NO":
            tid = t.get("token_id") or t.get("tokenId") or t.get("id")
            if tid:
                return str(tid)
    return None


def get_gamma_prices(market: dict) -> Tuple[Optional[float], Optional[float]]:
    """
    Extract YES/NO prices from outcomePrices field.

    Polymarket returns outcomePrices as a JSON-encoded STRING, not a list:
      '["0.65", "0.35"]'   ← string containing JSON array
    We must json.loads() it first, then parse floats.
    """
    import json as _json
    op = market.get("outcomePrices")
    if op is None:
        return None, None

    # Case 1: already a list (parsed correctly)
    if isinstance(op, list):
        prices = op
    # Case 2: string containing JSON array — most common from Gamma API
    elif isinstance(op, str):
        try:
            prices = _json.loads(op)
        except Exception:
            return None, None
    else:
        return None, None

    try:
        if len(prices) >= 2:
            return float(prices[0]), float(prices[1])
        if len(prices) == 1:
            p = float(prices[0])
            return p, round(1.0 - p, 4)
    except Exception:
        pass
    return None, None


# Markets to SKIP — bias strategy only applies to clean binary outcome markets
# NOT props, novelty bets, tweet counts, announcer words, box office, etc.
SKIP_MARKET_KEYWORDS = [
    # Sports props / totals
    "exact score", "exact result", "correct score",
    "handicap", "asian handicap",
    "o/u", "over/under", "total goals",
    "over 0.5", "over 1.5", "over 2.5", "over 3.5",
    "1st half", "2nd half", "half time", "halftime", "ht/ft",
    "1+ goals", "2+ goals", "1+ assists", "2+ assists",
    "goals + assists", "anytime goalscorer", "first goalscorer",
    "last goalscorer", "player to score", "to score",
    "both teams to score", "clean sheet",
    "method of victory", "round betting", "corner",
    "yellow card", "red card",

    # Announcer / broadcast novelty
    "announcers say", "announcer say", "commentator",
    "say \"", "say '", "times during",

    # Social media counts
    "tweets from", "tweet from", "post <", "post >",
    "retweet", "likes", "followers",

    # Box office / entertainment
    "box office", "opening weekend", "opening night",
    "academy award", "oscar", "grammy", "emmy",

    # Geopolitical novelty (not clean binary outcomes)
    "airspace closure", "military action against",
    "ceasefire continues", "ceasefire holds",

    # Generic novelty patterns
    "how many", "how much", "number of",
    "times will", "times does",
]

# Markets that MUST contain one of these to be considered
# (positive allowlist for clean outcome markets)
REQUIRE_OUTCOME_KEYWORDS = [
    "win", "wins", "beat", "beats", "advance", "advances",
    "qualify", "qualifies", "champion", "championship",
    "title", "final", "semifinal", "playoff",
    "above", "below", "higher", "lower",
    "reach", "hit", "exceed",
    "elected", "win election", "become",
    "draw", "end in", "result in",       # match draw markets
    "cut rates", "raise rates", "hike",  # fed decisions
    "hold rates", "pause",
    "above $", "below $", "over $",      # price markets
]


def _is_quality_market(market: dict) -> bool:
    """
    Only keep clean binary outcome markets where the bias strategy applies.
    Two-stage: blocklist check + allowlist check.
    """
    q = (market.get("question") or market.get("groupItemTitle") or "").lower()

    # Stage 1: block anything that matches a skip keyword
    if any(kw in q for kw in SKIP_MARKET_KEYWORDS):
        return False

    # Stage 2: must contain at least one outcome keyword
    # (filters out vague markets that aren't clearly about a winner/outcome)
    if not any(kw in q for kw in REQUIRE_OUTCOME_KEYWORDS):
        return False

    return True


def days_to_close(market: dict) -> float:
    end = market.get("endDate") or market.get("end_date_iso", "")
    if not end:
        return 99.0
    try:
        dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return max((dt - datetime.now(timezone.utc)).total_seconds() / 86400.0, 0.0)
    except Exception:
        return 99.0
