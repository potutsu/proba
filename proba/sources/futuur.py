"""
sources/futuur.py — Futuur API client
Proba | NoLaptopTrades

Base URL: https://api.futuur.com
Auth:     HMAC-SHA512 per https://docs.futuur.com/authentication

List endpoint (discovery — no markets/prices):
  GET /events/  → {count, next, previous, results[{id,title,category,
                    closes_at,resolved,pending_resolution,tags,currency_mode}]}

Detail endpoint (prices — has markets[].outcomes[].price):
  GET /events/{id}/  → {id,title,category,closes_at,resolved,markets[{
                          id,question,outcomes[{id,name,price}]}]}

Pipeline: list → filter by timing/resolved → detail per event → scorer
"""

import hashlib
import hmac
import os
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

from proba.paths import log_error

load_dotenv()

BASE_URL = "https://api.futuur.com"
TIMEOUT  = 20

CATEGORY_IDS = {
    "sports":        7,
    "crypto":        5,
    "politics":      4,
    "technology":    3,
    "science":       6,
    "economics":     2,
    "finance":       2,
    "entertainment": 1,
}

CATEGORY_NAMES = {v: k for k, v in CATEGORY_IDS.items()}


# ---------------------------------------------------------------------------
# Auth — matches docs exactly
# ---------------------------------------------------------------------------

def _keys() -> tuple[str, str]:
    pub  = (os.getenv("FUTUUR_PUBLIC_KEY",  "") or "").strip()
    priv = (os.getenv("FUTUUR_PRIVATE_KEY", "") or "").strip()
    return pub, priv


def _has_keys() -> bool:
    pub, priv = _keys()
    return bool(pub and priv)


def _auth_headers(request_params: dict) -> dict:
    """
    From https://docs.futuur.com/authentication:
    1. Collect all query params + Key + Timestamp
    2. Sort alphabetically by key name
    3. URL-encode
    4. HMAC-SHA512(private_key, encoded) → hexdigest
    5. Headers: Key, Timestamp, HMAC
    """
    pub, priv = _keys()
    if not pub or not priv:
        raise EnvironmentError("FUTUUR_PUBLIC_KEY and FUTUUR_PRIVATE_KEY must be set in .env")
    ts       = int(datetime.now(timezone.utc).timestamp())
    to_sign  = {**request_params, "Key": pub, "Timestamp": ts}
    sorted_p = OrderedDict(sorted(to_sign.items()))
    encoded  = urlencode(sorted_p).encode("utf-8")
    sig      = hmac.new(priv.encode("utf-8"), encoded, hashlib.sha512).hexdigest()
    return {"Key": pub, "Timestamp": str(ts), "HMAC": sig}


def _get(path: str, params: dict = None) -> dict | list:
    """Authenticated GET. Returns parsed JSON or {} on failure."""
    params = params or {}
    url    = f"{BASE_URL}{path}"
    try:
        headers = _auth_headers(params)
    except EnvironmentError as e:
        log_error("futuur", str(e))
        return {}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        if r.status_code != 200:
            # Log full body for diagnosis
            log_error("futuur",
                      f"{r.status_code} GET {path} "
                      f"params={list(params.keys())} "
                      f"body={r.text[:400]}")
            return {}
        return r.json()
    except requests.RequestException as e:
        log_error("futuur", f"Request failed GET {path}: {e}")
        return {}


# ---------------------------------------------------------------------------
# Category resolution
# ---------------------------------------------------------------------------

def _resolve_categories(cfg: dict) -> List[int]:
    names = cfg.get("discovery", {}).get("categories", ["sports"])
    ids, seen = [], set()
    for name in names:
        cid = CATEGORY_IDS.get(name.lower())
        if cid and cid not in seen:
            ids.append(cid)
            seen.add(cid)
    return ids or [CATEGORY_IDS["sports"]]


# ---------------------------------------------------------------------------
# List events — discovery only, no prices
# ---------------------------------------------------------------------------

def fetch_markets(cfg: dict) -> List[Dict]:
    """
    GET /events/ — discovery list.
    Tries real_money first, falls back to play_money if 404.
    """
    if not _has_keys():
        log_error("futuur", "Skipping — no API keys in .env")
        return []

    disc          = cfg.get("discovery", {})
    limit         = disc.get("max_results", 50)
    currency_mode = disc.get("currency_mode", "real_money")
    cat_ids       = _resolve_categories(cfg)
    all_results   = []

    for cat_id in cat_ids:
        # Try configured currency mode first, fall back to play_money
        for mode in [currency_mode, "play_money"] if currency_mode == "real_money" else [currency_mode]:
            params = {
                "currency_mode": mode,
                "limit":         limit,
                "categories":    cat_id,
                "ordering":      "-created_at",
            }
            data = _get("/events/", params=params)
            if data:
                results = data.get("results", []) if isinstance(data, dict) else []
                if results:
                    if mode != currency_mode:
                        log_error("futuur", f"real_money unavailable — using play_money instead")
                    cat_name = CATEGORY_NAMES.get(cat_id, str(cat_id))
                    for ev in results:
                        ev["_category_name"] = cat_name
                        ev["_source"]        = "futuur"
                        ev["_currency_mode"] = mode
                    all_results.extend(results)
                    break  # got data, don't try fallback

    # Deduplicate by id
    seen, deduped = set(), []
    for ev in all_results:
        eid = ev.get("id")
        if eid not in seen:
            seen.add(eid)
            deduped.append(ev)
    return deduped


# ---------------------------------------------------------------------------
# Retrieve event detail — has markets[].outcomes[].price
# ---------------------------------------------------------------------------

def fetch_event_detail(event_id: int) -> Dict:
    """
    GET /events/{id}/ — full event with markets and outcome prices.

    Real response:
    {
      "id": 1023, "title": "...", "category": 4,
      "closes_at": "2026-06-30T23:59:59Z",
      "resolved": false, "pending_resolution": false,
      "markets": [
        {
          "id": 501, "question": "Will X happen?",
          "outcomes": [
            {"id": 1, "name": "Yes", "price": 0.62},
            {"id": 2, "name": "No",  "price": 0.38}
          ]
        }
      ]
    }
    """
    if not _has_keys():
        return {}
    # Detail endpoint: no extra params, sign only Key + Timestamp
    data = _get(f"/events/{event_id}/")
    if not data:
        return {}
    return data


def fetch_price_history(event_id: int, time_interval: str = "week") -> List[Dict]:
    """GET /events/{id}/price_history/"""
    if not _has_keys():
        return []
    params = {"currency_mode": "real_money", "time_interval": time_interval}
    data   = _get(f"/events/{event_id}/price_history/", params=params)
    if isinstance(data, dict):
        return data.get("history", [])
    return []


def get_my_info() -> Dict:
    """
    GET /me/  — account info. Signs with empty params (just Key + Timestamp).
    From docs tip: 'If your GET request has no additional parameters
    (e.g., GET /me/), you still sign just Key and Timestamp.'
    """
    return _get("/me/") or {}
