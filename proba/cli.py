"""
cli.py — entry point, arg parsing, TUI launcher
Proba | NoLaptopTrades
"""

import argparse
import json
import sys
import os
from pathlib import Path
from datetime import datetime, timezone

# ── Path bootstrap ──────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
# ───────────────────────────────────────────────────────────────────────────

VERSION = "0.4.0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bar(width=70):
    return "─" * width

def _ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso[:16] if iso else "?"


# ---------------------------------------------------------------------------
# proba --test-api
# ---------------------------------------------------------------------------

def cmd_test_api():
    """Test API connectivity for all configured sources."""
    import requests

    print("\n" + _bar())
    print(" PROBA — API Connectivity Test")
    print(_bar())

    # ── Polymarket Gamma (public, no auth, no tag_id) ──
    print("\n[1] Polymarket Gamma API")
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/events",
            params={"active":"true","closed":"false","order":"volume24hr",
                    "ascending":"false","limit":5},
            headers={"User-Agent":"Mozilla/5.0 (compatible; Proba/0.4)"},
            timeout=15,
        )
        r.raise_for_status()
        events = r.json() if isinstance(r.json(), list) else []
        print(f"  OK — {len(events)} events returned")

        from proba.sources.polymarket import _is_sports_event
        sports = [e for e in events if _is_sports_event(e)]
        nonsports = [e for e in events if not _is_sports_event(e)]
        print(f"  Sports events: {len(sports)}  Non-sports: {len(nonsports)}")
        for ev in events[:3]:
            tag = "SPORT" if _is_sports_event(ev) else "skip"
            print(f"  [{tag}] {ev.get('title','')[:55]}")
            for m in ev.get("markets",[])[:1]:
                op = m.get("outcomePrices")
                ob = m.get("enableOrderBook")
                print(f"         outcomePrices={op}  orderBook={ob}")
    except Exception as e:
        print(f"  FAILED: {e}")

    # ── Polymarket CLOB ──
    print("\n[2] Polymarket CLOB (orderbook)")
    try:
        test_token = "21742633143463906290569050155826241533067272736897614950488156847949938836455"
        r2 = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": test_token},
            timeout=10
        )
        print(f"  /book status: {r2.status_code}")
        if r2.status_code == 200:
            book = r2.json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            print(f"  CLOB reachable — bids: {len(bids)}  asks: {len(asks)}")
            if bids and asks:
                print(f"  Best bid: {bids[0]['price']}  Best ask: {asks[0]['price']}")
                print(f"  Spread: {round(float(asks[0]['price'])-float(bids[0]['price']),4)}")
            else:
                print(f"  Book empty for test token — CLOB itself is working")
        else:
            print(f"  Status {r2.status_code} — body: {r2.text[:100]}")
    except requests.exceptions.ConnectionError:
        print("  UNREACHABLE — DNS failed. Gamma price fallback will be used.")
    except Exception as e:
        print(f"  {type(e).__name__}: {e}")

    # ── Futuur ──
    print("\n[3] Futuur API")
    try:
        import requests as req, time as _time
        from collections import OrderedDict
        from urllib.parse import urlencode
        from proba.sources.futuur import _keys, _has_keys, _auth_headers
        pub, priv = _keys()
        pub_display  = ("YES (" + pub[:8] + "...)") if pub else "MISSING"
        priv_display = "SET ({} chars)".format(len(priv)) if priv else "MISSING"
        print(f"  Keys: pub={pub_display}  priv={priv_display}")
        if not _has_keys():
            print("  Skipped — add keys to .env")
        else:
            params = {"currency_mode": "real_money", "limit": 1}
            headers = _auth_headers(params)
            r = req.get("https://api.futuur.com/events/",
                        params=params, headers=headers, timeout=15)
            print(f"  Status: {r.status_code}")
            if r.status_code == 200:
                data    = r.json()
                results = data.get("results",[]) if isinstance(data,dict) else []
                print(f"  OK — {len(results)} events")
            else:
                print(f"  {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")

    # ── pmxt ──
    print("\n[4] pmxt (overreaction data source)")
    try:
        from proba.sources.pmxt import pmxt_available
        if pmxt_available():
            print("  OK — pmxt installed and importable")
        else:
            print("  NOT installed — run: pip install pmxt")
            print("  (polymarket.py fallback will be used for price history)")
    except Exception as e:
        print(f"  {type(e).__name__}: {e}")

    # ── Wallet ──
    print("\n[5] Phase 1 wallet status")
    pk   = os.getenv("POLYMARKET_PRIVATE_KEY","")
    akey = os.getenv("POLYMARKET_API_KEY","")
    print(f"  POLYMARKET_PRIVATE_KEY: {'SET' if pk else 'not set — needed for Phase 1 only'}")
    print(f"  POLYMARKET_API_KEY:     {'SET' if akey else 'not set — needed for Phase 1 only'}")

    print("\n" + _bar())


# ---------------------------------------------------------------------------
# proba --log
# ---------------------------------------------------------------------------

def cmd_log(n: int = 20):
    from proba.paths import get_path

    path = get_path("discovery_log")
    if not path.exists():
        print("[proba] No discovery log yet. Run 'proba --scan' or 'proba' to start.")
        return

    with open(path, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]

    entries = []
    for line in lines[-n:]:
        try:
            entries.append(json.loads(line))
        except Exception:
            pass

    if not entries:
        print("[proba] Discovery log is empty.")
        return

    print("\n" + _bar())
    print(f" PROBA — Last {len(entries)} scored candidates")
    print(_bar())

    for e in reversed(entries):
        src_tag  = "[PM]" if e.get("source") == "polymarket" else "[FT]"
        strat    = e.get("strategy_type", "bias")
        strat_tag = "[OR]" if strat == "overreaction" else "[BS]"
        sig_icon = "▲" if e.get("signal") == "BUY_YES" else "▼"
        conf_dot = {"high": "●●●", "medium": "●●○", "low": "●○○"}.get(e.get("confidence",""), "○○○")

        print(f"\n  {sig_icon} {src_tag}{strat_tag} {e.get('title','')[:55]}")
        print(f"     Zone:     {e.get('zone','?'):22s}  Conf: {conf_dot}")
        print(f"     Fill:     {e.get('fill_price',0):.2%}  →  Est: {e.get('estimated_prob',0):.2%}  "
              f"Edge: {e.get('edge',0):+.2%}")
        if e.get("source") == "polymarket" and e.get("best_bid"):
            print(f"     Bid/Ask:  {e.get('best_bid',0):.3f} / {e.get('best_ask',0):.3f}  "
                  f"Spread: {e.get('spread',0):.3f}  Slippage: {e.get('slippage',0):.4f}")
        if strat == "overreaction":
            print(f"     Move 24h: {e.get('price_move_24h',0):+.2%}  "
                  f"Base rate: {e.get('base_rate',0):.2%}  "
                  f"Gap: {e.get('base_rate_gap',0):+.2%}")
        print(f"     Vol:      ${e.get('volume',0):,.0f}  "
              f"Days: {e.get('days_to_close',0):.1f}  "
              f"Momentum: {e.get('price_momentum','?')}")

    print("\n" + _bar())
    print(" 'proba --positions' to see auto-traded positions")
    print(_bar())


# ---------------------------------------------------------------------------
# proba --positions
# ---------------------------------------------------------------------------

def cmd_positions():
    from proba.paper_logger import list_open_positions

    positions = list_open_positions()
    if not positions:
        print("[proba] No open paper positions.")
        print("        Run 'proba --scan' or 'proba' to start the scanner.")
        return

    # Group by strategy type
    by_strat = {}
    for p in positions:
        st = p.get("strategy_type", "bias")
        by_strat.setdefault(st, []).append(p)

    print("\n" + _bar())
    print(f" PROBA — {len(positions)} Open Position(s)")
    print(_bar())

    strat_labels = {
        "bias":         ("Bias Strategy (favourite-longshot)", "[BS]"),
        "overreaction": ("Anti-Insanity / Overreaction Fade", "[OR]"),
    }

    for strat_key in ("bias", "overreaction"):
        group = by_strat.get(strat_key, [])
        if not group:
            continue
        label, tag = strat_labels[strat_key]
        print(f"\n  ── {label} ({len(group)}) ──────────────────────────────")
        for p in group:
            src      = "[PM]" if p.get("source") == "polymarket" else "[FT]"
            dir_icon = "▲ YES" if p.get("direction") == "yes" else "▼ NO "
            strat_id = p.get("strategy_id", "default")
            print(f"\n  {dir_icon} {src} {p['title'][:54]}")
            print(f"     ID:       {p['id'][:16]}...  strategy_id: {strat_id}")
            print(f"     Entry:    fill={p.get('fill_price', p.get('entry_price',0)):.2%}", end="")
            if p.get("best_bid"):
                print(f"  bid={p['best_bid']:.3f}  ask={p.get('best_ask',0):.3f}  "
                      f"spread={p.get('spread',0):.3f}", end="")
            print()
            print(f"     Edge:     {p.get('edge',0):+.2%}  Conf: {p.get('confidence','?')}  "
                  f"Zone: {p.get('zone','?')}")
            if strat_key == "overreaction":
                print(f"     Move 24h: {p.get('price_move_24h',0):+.2%}  "
                      f"Base rate: {p.get('base_rate',0):.2%}  "
                      f"Gap: {p.get('base_rate_gap',0):+.2%}")
            print(f"     Closes:   {_ts(p.get('close_date',''))}")
            if p.get("last_price"):
                shift = p["last_price"] - p.get("fill_price", p.get("entry_price", 0))
                print(f"     Now:      {p['last_price']:.2%}  "
                      f"({'↑' if shift > 0 else '↓'}{abs(shift):.2%} since entry)")

    # Any unknown strategy type
    for strat_key, group in by_strat.items():
        if strat_key not in strat_labels:
            print(f"\n  ── {strat_key} ({len(group)}) ──────────────────────────────")
            for p in group:
                print(f"     {p['title'][:60]}")

    print("\n" + _bar())


# ---------------------------------------------------------------------------
# proba --stats
# ---------------------------------------------------------------------------

def cmd_stats():
    from proba.postmortem import get_calibration_summary, phase0_ready_for_phase1

    cal   = get_calibration_summary()
    ready, reason = phase0_ready_for_phase1()

    print("\n" + _bar())
    print(f" PROBA — Calibration Stats  v{VERSION}")
    print(_bar())

    progress = cal["phase0_progress"]
    target   = cal["phase0_target"]
    filled   = int(40 * progress / max(target, 1))
    print(f"\n  Phase 0: [{'█' * filled}{'░' * (40-filled)}] {progress}/{target}")
    print(f"  Overall: {cal['win_rate']:.1%}  ({cal['correct']}/{cal['total']})")

    # By strategy — new in v0.4.0
    bstrat = cal.get("by_strategy", {})
    if any(bstrat.get(t, {}).get("total", 0) > 0 for t in ("bias", "overreaction")):
        print(f"\n  By strategy:")
        labels = {"bias": "Bias (fav-longshot)", "overreaction": "Anti-insanity (fade)"}
        for key in ("bias", "overreaction"):
            d = bstrat.get(key, {})
            if d.get("total", 0) > 0:
                bar_f = int(20 * d["win_rate"])
                print(f"    {labels.get(key, key):22s}  [{'█'*bar_f}{'░'*(20-bar_f)}]  "
                      f"{d['win_rate']:.1%}  ({d['correct']}/{d['total']})")

    bc = cal.get("by_confidence", {})
    if any(bc.get(t, {}).get("total", 0) > 0 for t in ("high","medium","low")):
        print(f"\n  By confidence:")
        for tier in ("high","medium","low"):
            d = bc.get(tier, {})
            if d.get("total",0) > 0:
                bar_f = int(20 * d["win_rate"])
                print(f"    {tier:6s}  [{'█'*bar_f}{'░'*(20-bar_f)}]  "
                      f"{d['win_rate']:.1%}  ({d['correct']}/{d['total']})")

    bzone = cal.get("by_zone", {})
    if bzone:
        print(f"\n  By zone:")
        for zone, d in sorted(bzone.items()):
            if d.get("total",0) > 0:
                print(f"    {zone:22s}  {d['win_rate']:.1%}  ({d['correct']}/{d['total']})")

    bcat = cal.get("by_category", {})
    if bcat:
        print(f"\n  By category:")
        for cat, d in sorted(bcat.items()):
            if d.get("total",0) > 0:
                print(f"    {cat:12s}  {d['win_rate']:.1%}  ({d['correct']}/{d['total']})")

    print(f"\n  Phase 1 gate: {'✓ READY' if ready else '✗ NOT YET'}")
    print(f"  {reason}")
    print("\n" + _bar())


# ---------------------------------------------------------------------------
# proba --resolve
# ---------------------------------------------------------------------------

def cmd_resolve():
    from proba.paper_logger import list_open_positions
    from proba.postmortem import process_resolved

    positions = list_open_positions()
    if not positions:
        print("[proba] No open positions to resolve.")
        return

    print(f"\n  Open positions:\n")
    for i, p in enumerate(positions, 1):
        src   = "[PM]" if p.get("source") == "polymarket" else "[FT]"
        strat = "[OR]" if p.get("strategy_type") == "overreaction" else "[BS]"
        print(f"  [{i}] {src}{strat} {p['title'][:55]}")
        print(f"       {p.get('signal','?')} | entry {p.get('fill_price',p.get('entry_price',0)):.2%} | "
              f"closes {_ts(p.get('close_date',''))}")

    try:
        raw = input("\n  Select number (Enter to cancel): ").strip()
        if not raw:
            return
        idx = int(raw) - 1
        if not (0 <= idx < len(positions)):
            print("[proba] Invalid.")
            return
        pos     = positions[idx]
        outcome = input(f"  Resolved outcome [yes/no]: ").strip().lower()
        if outcome not in ("yes", "no"):
            print("[proba] Must be yes or no.")
            return
    except (EOFError, KeyboardInterrupt, ValueError):
        print("\n[proba] Cancelled.")
        return

    result = process_resolved(pos, outcome)
    icon   = "✓ CORRECT" if result["correct"] else "✗ WRONG"
    print(f"\n  {icon} — was {result['direction'].upper()}, resolved {result['resolved_outcome'].upper()}")
    print(f"  Edge was {result.get('edge_was',0):+.2%}  Confidence: {result['confidence']}")
    print(f"  Run 'proba --stats' to see updated calibration.")


# ---------------------------------------------------------------------------
# proba --check  (position checker — separate from dry-run)
# ---------------------------------------------------------------------------

def cmd_check():
    """Manually check all open positions against Polymarket API right now."""
    from proba.paper_logger import list_open_positions
    from proba.monitor import _fetch_polymarket_status
    from proba.postmortem import process_resolved

    positions = list_open_positions()
    if not positions:
        print("[proba] No open positions.")
        return

    print(f"\n{_bar()}")
    print(f" PROBA — Checking {len(positions)} positions against Polymarket now...")
    print(_bar())

    auto_resolved = 0
    still_open    = 0
    errors        = 0

    for pos in positions:
        title = pos.get("title","")[:55]
        cid   = pos.get("market_id","")
        close = pos.get("close_date","")[:16]
        strat = "[OR]" if pos.get("strategy_type") == "overreaction" else "[BS]"

        print(f"\n  {strat} {title}")
        print(f"  closes={close}  id={cid[:16]}...")

        current_price, is_resolved, outcome = _fetch_polymarket_status(pos)

        if is_resolved and outcome:
            try:
                result = process_resolved(pos, outcome)
                correct = result.get("correct", False)
                icon    = "✓ CORRECT" if correct else "✗ WRONG"
                print(f"  → RESOLVED: outcome={outcome.upper()}  {icon}")
                auto_resolved += 1
            except Exception as e:
                print(f"  → RESOLVE ERROR: {e}")
                errors += 1
        elif is_resolved and not outcome:
            print(f"  → RESOLVED on Polymarket but outcome not readable yet (retry later)")
            errors += 1
        elif current_price is not None:
            entry = pos.get("fill_price", pos.get("entry_price", 0))
            shift = current_price - entry
            print(f"  → OPEN  current={current_price:.2%}  entry={entry:.2%}  shift={shift:+.2%}")
            still_open += 1
        else:
            print(f"  → Could not fetch (API error or market not found)")
            errors += 1

    print(f"\n{_bar()}")
    print(f" Auto-resolved: {auto_resolved}  Still open: {still_open}  Errors: {errors}")
    print(_bar())

    if auto_resolved > 0:
        print(f"\n  Run 'proba --stats' to see updated calibration.")


# ---------------------------------------------------------------------------
# proba --dry-run  (pipeline preview — no trades written)
# ---------------------------------------------------------------------------

def cmd_dry_run():
    """Run the full discovery pipeline, show all signals, write nothing."""
    print("[proba] DRY RUN — full pipeline, no trades written.\n")

    from proba import discovery
    scored, _ = discovery.run_once(on_status=print, dry_run=True)

    if not scored:
        print("\n[proba] No signals found this cycle.")
        return

    bias_scored = [s for s in scored if s.get("strategy_type", "bias") == "bias"]
    or_scored   = [s for s in scored if s.get("strategy_type") == "overreaction"]

    print(f"\n{_bar()}")
    print(f" {len(scored)} Signals Found (dry run — nothing written)")
    print(f" Bias: {len(bias_scored)}  Anti-insanity: {len(or_scored)}")
    print(_bar())

    if bias_scored:
        print(f"\n  ── Bias Strategy (favourite-longshot) ──────────────────")
        for s in bias_scored:
            src  = "[PM]" if s.get("source") == "polymarket" else "[FT]"
            icon = "▲" if s.get("signal") == "BUY_YES" else "▼"
            print(f"\n  {icon} {src} {s['title'][:60]}")
            print(f"     {s['zone']:22s}  edge={s['edge']:+.2%}  conf={s['confidence']}")
            print(f"     fill={s.get('fill_price',0):.3f}  "
                  f"spread={s.get('spread') or 0:.3f}  "
                  f"days={s.get('days_to_close',0):.1f}  "
                  f"vol=${s.get('volume',0):,.0f}")

    if or_scored:
        print(f"\n  ── Anti-Insanity / Overreaction Fade ──────────────────")
        for s in or_scored:
            print(f"\n  ▼ [OR] {s['title'][:60]}")
            print(f"     {s['zone']:22s}  edge={s['edge']:+.2%}  conf={s['confidence']}")
            print(f"     YES now={s.get('gamma_price',0):.2%}  "
                  f"24h ago={s.get('price_24h_ago',0):.2%}  "
                  f"move={s.get('price_move_24h',0):+.2%}")
            print(f"     base_rate={s.get('base_rate',0):.2%}  "
                  f"gap={s.get('base_rate_gap',0):+.2%}  "
                  f"days={s.get('days_to_close',0):.1f}  "
                  f"vol=${s.get('volume',0):,.0f}")
            print(f"     data_source={s.get('history_source','?')}")

    print(f"\n{_bar()}")
    print(" Run 'proba --scan' to run with auto-trading ON")
    print(_bar())


# ---------------------------------------------------------------------------
# proba --scan
# ---------------------------------------------------------------------------

def cmd_scan():
    print("[proba] Running one discovery cycle (auto-trade ON)...\n")

    from proba import discovery
    scored, logged = discovery.run_once(on_status=print)

    bias_scored = [s for s in scored if s.get("strategy_type", "bias") == "bias"]
    or_scored   = [s for s in scored if s.get("strategy_type") == "overreaction"]

    print(f"\n{_bar()}")
    print(f" Cycle complete: {len(scored)} signals "
          f"(bias:{len(bias_scored)} or:{len(or_scored)}) | {len(logged)} auto-traded")
    print(_bar())

    if logged:
        print(f"\n  New paper positions:")
        for pos in logged:
            src   = "[PM]" if pos.get("source") == "polymarket" else "[FT]"
            strat = "[OR]" if pos.get("strategy_type") == "overreaction" else "[BS]"
            icon  = "▲" if pos.get("direction") == "yes" else "▼"
            print(f"  {icon} {src}{strat} {pos['title'][:54]}")
            print(f"     {pos['zone']:22s}  "
                  f"fill={pos.get('fill_price',pos.get('market_price',0)):.2%}  "
                  f"edge={pos.get('edge',0):+.2%}")

    print(f"\n  'proba --positions' to see all open positions.")
    print(_bar())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="proba",
        description="Proba — automated prediction market paper trader (v{})".format(VERSION),
    )
    parser.add_argument("--test-api",  action="store_true",
                        help="Test API connectivity (Polymarket + Futuur + pmxt)")
    parser.add_argument("--log",       action="store_true",
                        help="Show recent scored candidates from discovery log")
    parser.add_argument("--log-n",     type=int, default=20, metavar="N",
                        help="Number of log entries to show (default 20)")
    parser.add_argument("--positions", action="store_true",
                        help="Show open paper positions grouped by strategy")
    parser.add_argument("--stats",     action="store_true",
                        help="Show calibration stats by strategy, confidence, zone")
    parser.add_argument("--resolve",   action="store_true",
                        help="Manually mark a position as resolved")
    parser.add_argument("--scan",      action="store_true",
                        help="Run one discovery cycle with auto-trading ON")
    parser.add_argument("--check",     action="store_true",
                        help="Manually check and resolve open positions against Polymarket now")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Discovery only — score everything, no trades written")
    parser.add_argument("--version",   action="store_true",
                        help="Show version")

    args = parser.parse_args()

    if args.version:
        print(f"proba {VERSION}")
        return

    from proba.eula import ensure_accepted
    try:
        ensure_accepted()
    except SystemExit:
        sys.exit(1)

    if args.test_api:
        cmd_test_api()
    elif args.check:
        cmd_check()
    elif args.dry_run:
        cmd_dry_run()
    elif args.log:
        cmd_log(n=args.log_n)
    elif args.positions:
        cmd_positions()
    elif args.stats:
        cmd_stats()
    elif args.resolve:
        cmd_resolve()
    elif args.scan:
        cmd_scan()
    else:
        from proba.manager import run_tui
        run_tui()


if __name__ == "__main__":
    main()
