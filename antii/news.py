"""
news.py — news context fetcher for OR signals
Anti-Insanity | Proba

Tails signal.jsonl. For each new signal, fetches related news
headlines from Adjacent News API and Finnhub (fallback).
Enriches signal with news_headlines and news_context, writes
enriched record to news.jsonl for scorer to consume.

If neither token is set, writes signal through with empty news fields.
This is intentional — we still want to score and trade, just without
news context in the log.

.env keys:
  ADJACENT_NEWS_TOKEN=   (bearer token from adj.news)
  FINNHUB_TOKEN=         (from finnhub.io free tier)

Worker: runs as subprocess managed by antii manager.
"""

import json
import os
import sys
import time
import signal as _signal
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# Support both standalone (~/proba/antii/) and nested (~/proba/proba/antii/) layouts
_ROOT = _HERE.parent if (_HERE.parent / 'antii').exists() else _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from paths import (
    ensure_dirs, append_log, log_error,
    TailReader, load_seen, save_seen,
)
from antii_config import MODE

import requests

_RUNNING = True

def _handle_sig(sig, frame):
    global _RUNNING
    _RUNNING = False

_signal.signal(_signal.SIGTERM, _handle_sig)
_signal.signal(_signal.SIGINT,  _handle_sig)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ── Token loading ──────────────────────────────────────────────────

def _load_tokens():
    adj_token     = os.getenv("ADJACENT_NEWS_TOKEN", "").strip()
    finnhub_token = os.getenv("FINNHUB_TOKEN", "").strip()
    return adj_token, finnhub_token


# ── Adjacent News ──────────────────────────────────────────────────
# docs: https://docs.adj.news/
# Market + event endpoints: unauthenticated
# News correlation endpoint: requires bearer token

ADJ_BASE = "https://v2.api.adj.news/api/v1"

def _fetch_adjacent(signal: dict, token: str) -> list:
    """
    Fetch news headlines from Adjacent for a signal's market.
    Returns list of {headline, url, published_at, source} dicts.
    """
    if not token:
        return []

    market_id = signal.get("market_id", "")
    title     = signal.get("title", "")

    headlines = []

    # Try market-specific news first
    try:
        resp = requests.get(
            f"{ADJ_BASE}/news",
            params={
                "market_id": market_id,
                "limit":     5,
                "sort":      "relevance",
            },
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent":    "AntiiNews/1.0",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            items = data if isinstance(data, list) else data.get("results", data.get("articles", []))
            for item in items[:5]:
                headlines.append({
                    "headline":     item.get("title", item.get("headline", "")),
                    "url":          item.get("url", item.get("link", "")),
                    "published_at": item.get("published_at", item.get("publishedAt", "")),
                    "source":       item.get("source", "adjacent"),
                    "relevance":    item.get("relevance_score", None),
                })
        elif resp.status_code == 404:
            # Market not in Adjacent — fall through to keyword search
            pass
        elif resp.status_code == 401:
            log_error("news", "Adjacent: 401 unauthorized — check ADJACENT_NEWS_TOKEN")
    except Exception as e:
        log_error("news", f"Adjacent market news error: {e}", {"market_id": market_id})

    # If no results, try semantic search on title
    if not headlines and title:
        try:
            resp = requests.get(
                f"{ADJ_BASE}/markets",
                params={
                    "search": title[:80],
                    "limit":  3,
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent":    "AntiiNews/1.0",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                data  = resp.json()
                items = data if isinstance(data, list) else data.get("results", [])
                for item in items[:3]:
                    for art in item.get("articles", item.get("news", []))[:2]:
                        headlines.append({
                            "headline":     art.get("title", ""),
                            "url":          art.get("url", ""),
                            "published_at": art.get("published_at", ""),
                            "source":       "adjacent_search",
                            "relevance":    None,
                        })
        except Exception as e:
            log_error("news", f"Adjacent search error: {e}", {"title": title[:40]})

    return headlines


# ── Finnhub fallback ───────────────────────────────────────────────
# Free tier: 60 calls/min. We use company/general news endpoint.

FINNHUB_BASE = "https://finnhub.io/api/v1"

def _title_to_keywords(title: str) -> str:
    """Extract 2-3 key terms from title for Finnhub general news search."""
    # Strip common prediction market boilerplate
    noise = [
        "will ", "by ", "in ", "before ", "does ", "is ", "are ",
        "when ", "what ", "who ", "how ", "the ", "a ", "an ",
        "2024", "2025", "2026", "2027",
    ]
    t = title.lower()
    for n in noise:
        t = t.replace(n, " ")
    words = [w for w in t.split() if len(w) > 3][:4]
    return " ".join(words)


def _fetch_finnhub(signal: dict, token: str) -> list:
    """
    Fetch general news from Finnhub related to signal title.
    Returns list of {headline, url, published_at, source} dicts.
    """
    if not token:
        return []

    keywords = _title_to_keywords(signal.get("title", ""))
    if not keywords:
        return []

    try:
        resp = requests.get(
            f"{FINNHUB_BASE}/news",
            params={
                "category": "general",
                "token":    token,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return []

        articles = resp.json()
        if not isinstance(articles, list):
            return []

        # Filter by keyword relevance (simple substring match)
        kw_list  = keywords.lower().split()
        relevant = []
        for art in articles:
            headline = (art.get("headline", "") or "").lower()
            summary  = (art.get("summary",  "") or "").lower()
            score    = sum(1 for kw in kw_list if kw in headline or kw in summary)
            if score >= 2:
                relevant.append({
                    "headline":     art.get("headline", ""),
                    "url":          art.get("url", ""),
                    "published_at": str(art.get("datetime", "")),
                    "source":       f"finnhub/{art.get('source', '')}",
                    "relevance":    score,
                })

        relevant.sort(key=lambda x: x["relevance"], reverse=True)
        return relevant[:5]

    except Exception as e:
        log_error("news", f"Finnhub error: {e}", {"keywords": keywords})
        return []


# ── Enrichment ─────────────────────────────────────────────────────

def _enrich(signal: dict, adj_token: str, finnhub_token: str) -> dict:
    """
    Fetch news for signal, return enriched copy.
    Always returns a dict — never raises.
    """
    headlines = []

    # Adjacent first
    try:
        adj = _fetch_adjacent(signal, adj_token)
        headlines.extend(adj)
    except Exception as e:
        log_error("news", f"Adjacent fetch failed: {e}")

    # Finnhub if Adjacent found nothing
    if not headlines:
        try:
            fh = _fetch_finnhub(signal, finnhub_token)
            headlines.extend(fh)
        except Exception as e:
            log_error("news", f"Finnhub fetch failed: {e}")

    enriched = dict(signal)
    enriched["news_headlines"] = headlines
    enriched["news_context"]   = ""   # blank — filled manually post-mortem
    enriched["news_fetch_ts"]  = _ts()
    enriched["news_source"]    = (
        "adjacent" if any(h["source"].startswith("adj") for h in headlines) else
        "finnhub"  if headlines else
        "none"
    )
    enriched["news_count"] = len(headlines)
    return enriched


# ── Main loop ──────────────────────────────────────────────────────

def run():
    ensure_dirs()

    adj_token, finnhub_token = _load_tokens()
    adj_ok     = bool(adj_token)
    finnhub_ok = bool(finnhub_token)

    print(
        f"[{_ts()}] [news] starting — "
        f"adjacent={'OK' if adj_ok else 'NO TOKEN'} "
        f"finnhub={'OK' if finnhub_ok else 'NO TOKEN'}",
        flush=True,
    )
    if not adj_ok and not finnhub_ok:
        print(
            f"[{_ts()}] [news] WARNING: no news tokens set — "
            f"signals will pass through with empty news_headlines. "
            f"Set ADJACENT_NEWS_TOKEN and/or FINNHUB_TOKEN in .env",
            flush=True,
        )

    reader = TailReader("news", "signal")
    seen   = load_seen("news")

    processed = 0

    while _RUNNING:
        new_count = 0

        for signal in reader.read_new():
            if not _RUNNING:
                break

            sid = signal.get("signal_id", "")
            if not sid:
                continue

            # Dedup — news.jsonl should have exactly one record per signal_id
            if sid in seen:
                continue

            try:
                enriched = _enrich(signal, adj_token, finnhub_token)
                append_log("news", enriched)
                seen.add(sid)
                save_seen("news", seen)   # persist immediately — crash-safe
                processed += 1
                new_count += 1

                n = enriched.get("news_count", 0)
                src = enriched.get("news_source", "none")
                print(
                    f"[{_ts()}] [news] enriched {sid} "
                    f"'{signal.get('title','')[:40]}' "
                    f"headlines={n} source={src}",
                    flush=True,
                )

            except Exception as e:
                log_error("news", f"enrich failed for {sid}: {e}")
                print(f"[{_ts()}] [news] ERROR {sid}: {e}", flush=True)

        reader.save()

        if new_count > 0:
            print(
                f"[{_ts()}] [news] cycle: new={new_count} total_processed={processed}",
                flush=True,
            )

        for _ in range(30):   # check every 30s
            if not _RUNNING:
                break
            time.sleep(1)

    save_seen("news", seen)
    reader.save()
    print(f"[{_ts()}] [news] stopped cleanly", flush=True)


if __name__ == "__main__":
    run()
