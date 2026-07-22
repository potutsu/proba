"""
discovery.py — parallel multi-source discovery pipeline
Proba | NoLaptopTrades
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from proba.paths import get_config, get_path, log_error
from proba.filter import filter_polymarket_markets, filter_futuur_markets
from proba.scorer import score_polymarket_market, score_futuur_market, rank_scored, _classify
from proba.auto_trader import process_batch


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_stop_event   = threading.Event()
_cycle_count  = 0
_last_scored: List[Dict] = []
_last_logged: List[Dict] = []
_lock = threading.Lock()


def get_last_scored() -> List[Dict]:
    with _lock:
        return list(_last_scored)

def get_last_logged() -> List[Dict]:
    with _lock:
        return list(_last_logged)

def stop():
    _stop_event.set()

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# Polymarket pipeline
# ---------------------------------------------------------------------------

def _run_polymarket(cfg: Dict, status: Callable) -> List[Dict]:
    """
    Two-stage pipeline:
    1. Gamma fetch + filter (fast, no CLOB)
    2. Pre-score with Gamma prices → find bias-zone candidates
    3. CLOB fetch only for top 20
    4. Final score with live bid/ask
    """
    from proba.sources.polymarket import (
        fetch_sports_markets, fetch_orderbook,
        fetch_price_history, get_yes_token_id, get_gamma_prices,
        _is_quality_market,
    )

    position_size = cfg.get("paper", {}).get("default_position_size_usd", 40.0)

    status(f"[PM] {_ts()}  fetching markets (Gamma)...")
    raw = fetch_sports_markets(cfg)
    status(f"[PM] {_ts()}  {len(raw)} raw markets")
    if not raw:
        return []

    filtered = filter_polymarket_markets(raw, cfg)
    status(f"[PM] {_ts()}  {len(filtered)} after filter")
    if not filtered:
        return []

    # ── Stage 1: pre-score with Gamma prices only ─────────────────────────
    skip_no_prices  = 0
    skip_no_signal  = 0
    skip_no_token   = 0
    zone_counts: Dict[str, int] = {}
    candidates = []

    for mkt in filtered:
        # Quality gate — skip props, totals, exact scores
        if not _is_quality_market(mkt):
            skip_no_signal += 1
            continue

        # Use get_gamma_prices which handles JSON-string format correctly
        yes_p, no_p = get_gamma_prices(mkt)
        if yes_p is None:
            skip_no_prices += 1
            continue

        clob_ids = mkt.get("clobTokenIds", [])
        op_raw   = mkt.get("outcomePrices", [])

        # Parse all outcome prices (may be JSON string)
        try:
            import json as _json
            if isinstance(op_raw, str):
                all_prices = [float(p) for p in _json.loads(op_raw) if p is not None]
            elif isinstance(op_raw, list):
                all_prices = [float(p) for p in op_raw if p is not None]
            else:
                skip_no_prices += 1
                continue
        except Exception:
            skip_no_prices += 1
            continue

        if not all_prices:
            skip_no_prices += 1
            continue

        # Check every outcome price for a bias zone signal
        best_priority = 99
        best_entry = None

        for idx, price in enumerate(all_prices):
            signal, zone, direction, priority = _classify(price)
            if signal == "NO_SIGNAL":
                continue
            token_id = str(clob_ids[idx]) if idx < len(clob_ids) and clob_ids[idx] else None
            if not token_id:
                skip_no_token += 1
                continue
            if priority < best_priority:
                best_priority = priority
                best_entry = (priority, mkt, price, token_id, zone)

        if best_entry is None:
            skip_no_signal += 1
            continue

        _, mkt, price, token_id, zone = best_entry
        zone_counts[zone] = zone_counts.get(zone, 0) + 1
        candidates.append((best_priority, mkt, price, token_id))

    # Log detailed pre-score summary
    log_error("discovery",
              f"[PM] pre-score: {len(filtered)} filtered → "
              f"{len(candidates)} in bias zones | "
              f"skip: no_prices={skip_no_prices} no_signal={skip_no_signal} no_token={skip_no_token} | "
              f"zones={zone_counts}")
    status(f"[PM] {_ts()}  pre-score: {len(candidates)} in bias zones "
           f"(no_prices={skip_no_prices} no_signal={skip_no_signal}) "
           f"zones={zone_counts}")

    if not candidates:
        # Log sample of what prices look like to diagnose further
        sample = filtered[:5]
        for mkt in sample:
            op = mkt.get("outcomePrices", [])
            q  = mkt.get("question", "?")[:50]
            log_error("discovery", f"[PM] sample market: '{q}' outcomePrices={op[:4]}")
        return []

    # Sort by priority, take top 20
    candidates.sort(key=lambda x: x[0])
    top = candidates[:20]
    status(f"[PM] {_ts()}  fetching CLOB for top {len(top)}...")

    # ── Stage 2: CLOB fetch for top candidates ────────────────────────────
    scored = []
    for _, mkt, gamma_price, token_id in top:
        try:
            book    = fetch_orderbook(token_id)
            history = fetch_price_history(token_id, interval="1w")
            s = score_polymarket_market(mkt, history, book, cfg, position_size)
            if s:
                scored.append(s)
        except Exception as e:
            log_error("discovery", f"[PM] score failed {mkt.get('conditionId','?')}: {e}")

    status(f"[PM] {_ts()}  {len(scored)} signals")
    return scored


# ---------------------------------------------------------------------------
# Overreaction pipeline
# ---------------------------------------------------------------------------

def _run_overreaction(cfg: Dict, status: Callable) -> List[Dict]:
    """
    Anti-insanity pipeline: find markets that spiked >15% in 24h on hype,
    compare to historical base rate, fade the overreaction by buying NO.
    """
    from proba.sources.polymarket import fetch_sports_markets, get_yes_token_id
    from proba.sources.pmxt import fetch_price_history_24h
    from proba.scorer_overreaction import score_overreaction
    from proba.filter import filter_polymarket_markets

    or_cfg = cfg.get("overreaction", {})
    if not or_cfg.get("enabled", True):
        return []

    position_size = cfg.get("paper", {}).get("default_position_size_usd", 40.0)

    status(f"[OR] {_ts()}  fetching markets for overreaction scan...")
    raw = fetch_sports_markets(cfg)
    if not raw:
        return []

    # Reuse the same quality filter — still want liquid, non-junk markets
    filtered = filter_polymarket_markets(raw, cfg)
    status(f"[OR] {_ts()}  {len(filtered)} markets to scan for 24h moves")
    if not filtered:
        return []

    scored = []
    skipped_no_history = 0
    skipped_no_signal  = 0

    for mkt in filtered:
        try:
            market_id = str(mkt.get("conditionId", mkt.get("id", "")))
            token_id  = get_yes_token_id(mkt)

            history = fetch_price_history_24h(market_id, token_id=token_id)
            if not history:
                skipped_no_history += 1
                continue

            s = score_overreaction(mkt, history, cfg, position_size)
            if s:
                scored.append(s)
            else:
                skipped_no_signal += 1

        except Exception as e:
            log_error("discovery", f"[OR] score failed {mkt.get('conditionId','?')}: {e}")

    log_error("discovery",
              f"[OR] scan: {len(filtered)} markets → {len(scored)} signals | "
              f"no_history={skipped_no_history} no_signal={skipped_no_signal}")
    status(f"[OR] {_ts()}  {len(scored)} overreaction signals "
           f"(no_history={skipped_no_history} no_signal={skipped_no_signal})")
    return scored


# ---------------------------------------------------------------------------
# Futuur pipeline
# ---------------------------------------------------------------------------

def _run_futuur(cfg: Dict, status: Callable) -> List[Dict]:
    from proba.sources.futuur import fetch_markets, fetch_event_detail, fetch_price_history, _has_keys

    if not _has_keys():
        status(f"[FT] {_ts()}  no API keys — skipping")
        return []

    status(f"[FT] {_ts()}  fetching markets...")
    raw = fetch_markets(cfg)
    if not raw:
        status(f"[FT] {_ts()}  0 markets (check keys or try play_money in config)")
        return []

    status(f"[FT] {_ts()}  {len(raw)} raw markets")
    filtered = filter_futuur_markets(raw, cfg)
    status(f"[FT] {_ts()}  {len(filtered)} after filter")
    if not filtered:
        return []

    scored = []
    for ev in filtered:
        try:
            detail = fetch_event_detail(ev["id"])
            if not detail:
                continue
            detail["_category_name"] = ev.get("_category_name", "sports")
            detail["_source"] = "futuur"

            # Price-level filter
            f_cfg     = cfg.get("filter", {})
            near_zero = f_cfg.get("skip_price_near_zero", 0.03)
            near_one  = f_cfg.get("skip_price_near_one",  0.97)
            prices = [float(oc["price"]) for m in detail.get("markets",[])
                      for oc in m.get("outcomes",[]) if oc.get("price") is not None]
            if not prices:
                continue
            if all(p < near_zero or p > near_one for p in prices):
                continue

            history = fetch_price_history(ev["id"], time_interval="week")
            s = score_futuur_market(detail, history, cfg)
            if s:
                scored.append(s)
        except Exception as e:
            log_error("discovery", f"[FT] score failed {ev.get('id','?')}: {e}")

    status(f"[FT] {_ts()}  {len(scored)} signals")
    return scored


# ---------------------------------------------------------------------------
# Combined single cycle
# ---------------------------------------------------------------------------

def run_once(
    on_status: Optional[Callable[[str], None]] = None,
    dry_run: bool = False,
) -> Tuple[List[Dict], List[Dict]]:
    cfg = get_config()

    def status(msg: str):
        if on_status:
            on_status(msg)

    sources = cfg.get("discovery", {}).get("sources", ["polymarket", "futuur"])
    all_scored: List[Dict] = []

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        if "polymarket" in sources:
            futures[pool.submit(_run_polymarket, cfg, status)] = "polymarket"
        if "futuur" in sources:
            futures[pool.submit(_run_futuur, cfg, status)] = "futuur"
        if "overreaction" in sources or cfg.get("overreaction", {}).get("enabled", False):
            futures[pool.submit(_run_overreaction, cfg, status)] = "overreaction"

        for future in as_completed(futures, timeout=120):
            src = futures[future]
            try:
                result = future.result(timeout=90)
                all_scored.extend(result)
            except Exception as e:
                status(f"[{src.upper()}] error: {e}")
                log_error("discovery", f"{src} error: {e}")

    ranked = rank_scored(all_scored)

    if ranked:
        log_path = get_path("discovery_log")
        ts_now   = datetime.now(timezone.utc).isoformat()
        with open(log_path, "a", encoding="utf-8") as f:
            for s in ranked:
                f.write(json.dumps({**s, "log_ts": ts_now}) + "\n")

    logged: List[Dict] = []
    if not dry_run and ranked:
        logged = process_batch(ranked, cfg)
        n  = len(logged)
        pm = sum(1 for p in logged if p.get("source") == "polymarket" and p.get("strategy_type") != "overreaction")
        ft = sum(1 for p in logged if p.get("source") == "futuur")
        or_ = sum(1 for p in logged if p.get("strategy_type") == "overreaction")
        if n:
            status(f"[discovery] {_ts()}  AUTO-TRADED {n} (PM:{pm} FT:{ft} OR:{or_})")
        else:
            status(f"[discovery] {_ts()}  {len(ranked)} signals — none met trade criteria")
    elif dry_run:
        pm  = sum(1 for s in ranked if s.get("source") == "polymarket" and s.get("strategy_type") != "overreaction")
        ft  = sum(1 for s in ranked if s.get("source") == "futuur")
        or_ = sum(1 for s in ranked if s.get("strategy_type") == "overreaction")
        status(f"[discovery] {_ts()}  DRY RUN — {len(ranked)} signals (PM:{pm} FT:{ft} OR:{or_})")

    return ranked, logged


# ---------------------------------------------------------------------------
# Continuous loop
# ---------------------------------------------------------------------------

def loop(
    on_status: Optional[Callable[[str], None]] = None,
    on_cycle_done: Optional[Callable[[List[Dict], List[Dict]], None]] = None,
) -> None:
    global _cycle_count, _last_scored, _last_logged
    _stop_event.clear()

    cfg      = get_config()
    disc     = cfg.get("discovery", {})
    interval = disc.get("loop_interval_sec", 300)
    sources  = disc.get("sources", ["polymarket", "futuur"])
    cats     = disc.get("categories", ["sports"])

    msg = (f"[discovery] ready — sources: {', '.join(sources)}  "
           f"categories: {', '.join(cats)}  interval: {interval}s")
    if on_status: on_status(msg)
    else:         print(msg)

    while not _stop_event.is_set():
        _cycle_count += 1
        msg = f"[discovery] {_ts()}  cycle {_cycle_count} — scanning..."
        if on_status: on_status(msg)
        else:         print(msg)

        try:
            scored, logged = run_once(on_status=on_status)
            with _lock:
                _last_scored = scored
                _last_logged = logged
            if on_cycle_done:
                on_cycle_done(scored, logged)
            msg = (f"[discovery] {_ts()}  cycle {_cycle_count} done — "
                   f"{len(scored)} signals | {len(logged)} traded")
            if on_status: on_status(msg)
            else:         print(msg)
        except Exception as e:
            log_error("discovery", str(e))
            msg = f"[discovery] {_ts()}  cycle {_cycle_count} ERROR: {e}"
            if on_status: on_status(msg)
            else:         print(msg)

        _stop_event.wait(timeout=interval)

    msg = f"[discovery] {_ts()}  stopped."
    if on_status: on_status(msg)
    else:         print(msg)
