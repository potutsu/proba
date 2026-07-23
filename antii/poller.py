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
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from paths import (
    ensure_dirs, append_log, log_error, LOGS
)
from antii_config import (
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

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

# Tag slugs/labels that mean sports — block these
_SPORTS_TAGS = {
    "sports","soccer","football","basketball","baseball","tennis","golf",
    "nba","nfl","nhl","mlb","mls","nascar","ufc","mma","boxing","rugby",
    "cricket","hockey","swimming","athletics","olympics","esports",
    "world-cup","fifa-world-cup","champions-league","premier-league",
    "nba-offseason","nba-free-agency",
}

# Tag slugs/labels that map to OR-relevant categories (focus mode)
_FOCUS_TAGS = {
    # politics
    "politics","political","election","elections","president","congress",
    "senate","democrat","republican","trump","biden","harris","white-house",
    "donald-trump","us-politics","government","2024-elections","2026-elections",
    # geopolitics
    "geopolitics","geopolitical","war","nato","military","iran","ukraine",
    "russia","china","taiwan","middle-east","north-korea","israel","gaza",
    "sanctions","nuclear",
    # economics / macro
    "economy","economics","economic-policy","fed","fed-rates","fomc",
    "jerome-powell","inflation","cpi","cpi-release","recession","gdp",
    "interest-rates","treasury","fiscal","tariff","trade","trade-war",
    "economic-indicators","unemployment","jobs",
    # crypto
    "crypto","cryptocurrency","bitcoin","ethereum","defi","nft","web3",
    "solana","btc","eth","crypto-prices",
    # tech (optional in focus)
    "ai","openai","artificial-intelligence","tech","technology",
}


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def _days_to_close(end_date: str) -> float:
    try:
        dt  = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return max((dt - now).total_seconds() / 86400.0, 0.0)
    except Exception:
        return 0.0


def _event_tag_slugs(event: dict) -> set:
    """Return set of lowercase tag slugs + labels for an event."""
    slugs = set()
    for t in (event.get("tags") or []):
        for key in ("slug", "label"):
            v = str(t.get(key, "") or "").lower().strip()
            if v:
                slugs.add(v)
    return slugs


def _is_sports(tag_slugs: set) -> bool:
    return bool(tag_slugs & _SPORTS_TAGS)


def _category_from_tags(tag_slugs: set, title: str) -> str:
    """Derive a single category string from tag slugs."""
    t = title.lower()
    if tag_slugs & {"bitcoin","ethereum","crypto","defi","nft","web3","solana","btc","eth","crypto-prices","cryptocurrency"}:
        return "crypto"
    if tag_slugs & {"fed","fed-rates","fomc","jerome-powell","economy","economics","economic-policy",
                    "inflation","cpi","cpi-release","recession","gdp","interest-rates","tariff","trade","trade-war"}:
        return "economics"
    if tag_slugs & {"war","nato","military","iran","ukraine","russia","china","taiwan",
                    "middle-east","north-korea","israel","gaza","sanctions","nuclear","geopolitics","geopolitical"}:
        return "geopolitics"
    if tag_slugs & {"politics","political","election","elections","president","congress","senate",
                    "democrat","republican","trump","biden","harris","white-house","donald-trump",
                    "us-politics","government"}:
        return "politics"
    if tag_slugs & {"ai","openai","artificial-intelligence","tech","technology"}:
        return "tech"
    if tag_slugs:
        return sorted(tag_slugs)[0]
    return "other"


def _category_ok(category: str, tag_slugs: set) -> bool:
    if MODE == "focus":
        # Must have at least one focus tag
        return bool(tag_slugs & _FOCUS_TAGS)
    # comprehensive — anything not sports
    return True


def fetch_all_markets() -> list:
    """
    Fetch active non-sports markets from Gamma /events.
    Returns flat list of market dicts enriched with _category, _tag_slugs, _event_title.
    """
    results = []
    offset  = 0
    limit   = 100

    while len(results) < POLL_LIMIT:
        try:
            resp = requests.get(
                GAMMA_EVENTS_URL,
                params={
                    "active":    "true",
                    "closed":    "false",
                    "order":     POLL_SORT_BY,
                    "ascending": "false",
                    "limit":     limit,
                    "offset":    offset,
                },
                headers={"User-Agent": "Mozilla/5.0 (compatible; AntiiPoller/1.0)"},
                timeout=20,
            )
            resp.raise_for_status()
            page = resp.json()

            if not isinstance(page, list) or not page:
                break

            for event in page:
                tag_slugs = _event_tag_slugs(event)

                if _is_sports(tag_slugs):
                    continue

                category = _category_from_tags(tag_slugs, event.get("title", ""))

                if not _category_ok(category, tag_slugs):
                    continue

                ev_title = event.get("title", "")
                for mkt in (event.get("markets") or []):
                    mkt["_category"]    = category
                    mkt["_tag_slugs"]   = list(tag_slugs)
                    mkt["_event_title"] = ev_title
                    results.append(mkt)

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

    return results


def market_to_tick(market: dict) -> dict:
    """
    Convert a Gamma market dict to a price_tick record.

    Key fields now included:
      lastTradePrice    — most recent trade price
      bestBid / bestAsk — current orderbook top
      oneDayPriceChange — Gamma-computed 24h price delta (this is our OR signal!)
      oneWeekPriceChange
    These come directly from Gamma — no CLOB needed.
    """
    # Price: prefer lastTradePrice, fall back to outcomePrices[0]
    last_trade = market.get("lastTradePrice")
    try:
        yes_price = float(last_trade) if last_trade is not None else None
    except (ValueError, TypeError):
        yes_price = None

    if yes_price is None:
        outcome_prices = market.get("outcomePrices", [])
        try:
            yes_price = float(outcome_prices[0]) if outcome_prices else None
        except (ValueError, TypeError):
            yes_price = None

    # Bid/ask
    try:
        best_bid = float(market.get("bestBid")) if market.get("bestBid") is not None else None
    except (ValueError, TypeError):
        best_bid = None
    try:
        best_ask = float(market.get("bestAsk")) if market.get("bestAsk") is not None else None
    except (ValueError, TypeError):
        best_ask = None

    # Price changes — Gamma gives these directly
    try:
        change_24h = float(market.get("oneDayPriceChange") or 0)
    except (ValueError, TypeError):
        change_24h = None
    try:
        change_1w = float(market.get("oneWeekPriceChange") or 0)
    except (ValueError, TypeError):
        change_1w = None

    # Token IDs
    clob_ids  = market.get("clobTokenIds", [])
    yes_token = str(clob_ids[0]) if len(clob_ids) > 0 else None
    no_token  = str(clob_ids[1]) if len(clob_ids) > 1 else None

    volume    = float(market.get("volumeNum", 0) or market.get("volume", 0) or 0)
    liquidity = float(market.get("liquidityNum", 0) or market.get("liquidity", 0) or 0)
    end_date  = market.get("endDateIso", market.get("endDate", ""))

    return {
        "ts":              _ts(),
        "market_id":       str(market.get("conditionId", market.get("id", ""))),
        "yes_token_id":    yes_token,
        "no_token_id":     no_token,
        "title":           market.get("question", market.get("title", "")),
        "event_title":     market.get("_event_title", ""),
        "category":        market.get("_category", "other"),
        "tag_slugs":       market.get("_tag_slugs", []),

        # Prices
        "yes_price":       yes_price,
        "best_bid":        best_bid,
        "best_ask":        best_ask,
        "spread":          round(best_ask - best_bid, 4) if best_bid and best_ask else None,

        # Gamma-native price changes — key OR signal inputs
        "change_24h":      change_24h,   # oneDayPriceChange
        "change_1w":       change_1w,    # oneWeekPriceChange

        # Volume / liquidity
        "volume":          volume,
        "volume_24h":      float(market.get("volume24hr", 0) or 0),
        "liquidity":       liquidity,

        # Lifecycle
        "days_to_close":   round(_days_to_close(end_date), 3),
        "end_date":        end_date,
        "active":          bool(market.get("active", True)),
        "closed":          bool(market.get("closed", False)),
        "resolved":        bool(market.get("resolved", False)),
        "resolution_price": market.get("resolutionPrice"),
        "mode":            MODE,
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

            skip_sports   = 0
            skip_category = 0
            skip_noprice  = 0

            for mkt in raw_markets:
                if not _RUNNING:
                    break

                tick = market_to_tick(mkt)

                # Skip markets with no YES price (no orderbook, ghost markets)
                if tick["yes_price"] is None:
                    skip_noprice += 1
                    skip_count += 1
                    continue

                append_log("tick", tick)
                tick_count += 1

            elapsed = round(time.time() - cycle_start, 1)
            print(
                f"[{_ts()}] [poller] cycle done — "
                f"fetched={len(raw_markets)} ticked={tick_count} "
                f"skipped={skip_count}(no_price={skip_noprice}) elapsed={elapsed}s",
                flush=True,
            )
            # Debug: show first 3 ticked markets on each cycle
            if tick_count == 0:
                print(f"[{_ts()}] [poller] WARNING: zero ticks — check category filter", flush=True)

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
