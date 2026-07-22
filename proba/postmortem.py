"""
postmortem.py — resolution handler, calibration tracker
Proba | NoLaptopTrades
"""

import json
from datetime import datetime, timezone
from typing import Dict, Optional

from proba.paths import get_path, log_error
from proba.paper_logger import close_position


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _days_held(entry_ts: str) -> float:
    try:
        entry_dt = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - entry_dt).total_seconds() / 86400.0
    except Exception:
        return 0.0


def _load_calibration() -> Dict:
    path = get_path("calibration")
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "total": 0,
        "correct": 0,
        "win_rate": 0.0,
        "by_confidence": {
            "high":   {"total": 0, "correct": 0, "win_rate": 0.0},
            "medium": {"total": 0, "correct": 0, "win_rate": 0.0},
            "low":    {"total": 0, "correct": 0, "win_rate": 0.0},
        },
        "by_category": {},
        "by_strategy": {
            "bias":          {"total": 0, "correct": 0, "win_rate": 0.0},
            "overreaction":  {"total": 0, "correct": 0, "win_rate": 0.0},
        },
        "phase0_progress": 0,
        "phase0_target": 50,
        "updated_at": "",
    }


def _save_calibration(cal: Dict) -> None:
    path = get_path("calibration")
    cal["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cal, f, indent=2)


def _win_rate(d: Dict) -> float:
    if d["total"] == 0:
        return 0.0
    return round(d["correct"] / d["total"], 4)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def process_resolved(position: Dict, resolved_outcome: str) -> Dict:
    """
    Close a position and write result to paper_results.jsonl.
    resolved_outcome: 'yes' or 'no' (the actual market outcome).
    Returns the result record.
    """
    resolved_outcome = resolved_outcome.lower().strip()
    direction        = position.get("direction", "").lower()
    correct          = direction == resolved_outcome

    # Close the position record
    try:
        close_position(position["id"], resolved_outcome, resolved_price=float(resolved_outcome == "yes"))
    except Exception as e:
        log_error("postmortem", f"close_position failed for {position['id']}: {e}")

    result = {
        "id":               position["id"],
        "event_id":         position.get("event_id", ""),
        "title":            position.get("title", ""),
        "category":         position.get("category", ""),
        "direction":        direction,
        "entry_price":      position.get("entry_price", 0.0),
        "your_estimate":    position.get("your_estimate", 0.0),
        "resolved_outcome": resolved_outcome,
        "correct":          correct,
        "edge_was":         position.get("edge", 0.0),
        "confidence":       position.get("confidence", "low"),
        "zone":             position.get("zone", "UNKNOWN"),
        "signal":           position.get("signal", ""),
        "days_held":        round(_days_held(position.get("entry_ts", "")), 2),
        "phase":            position.get("phase", 0),
        "resolved_ts":      datetime.now(timezone.utc).isoformat(),
    }

    # Write to paper_results.jsonl
    results_path = get_path("paper_results")
    with open(results_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(result) + "\n")

    # Update calibration stats
    update_calibration(result)

    return result


def update_calibration(result: Dict) -> None:
    """Update calibration.json with a resolved result."""
    cal = _load_calibration()

    correct    = bool(result.get("correct", False))
    confidence = result.get("confidence", "low")
    category   = result.get("category", "unknown")

    # Overall
    cal["total"]   += 1
    cal["correct"] += int(correct)
    cal["win_rate"] = _win_rate(cal)

    # By confidence
    if confidence not in cal["by_confidence"]:
        cal["by_confidence"][confidence] = {"total": 0, "correct": 0, "win_rate": 0.0}
    bc = cal["by_confidence"][confidence]
    bc["total"]    += 1
    bc["correct"]  += int(correct)
    bc["win_rate"]  = _win_rate(bc)

    # By category
    if category not in cal["by_category"]:
        cal["by_category"][category] = {"total": 0, "correct": 0, "win_rate": 0.0}
    bcat = cal["by_category"][category]
    bcat["total"]   += 1
    bcat["correct"] += int(correct)
    bcat["win_rate"] = _win_rate(bcat)

    # By strategy type (bias vs overreaction)
    strategy = result.get("strategy_type", "bias")
    if "by_strategy" not in cal:
        cal["by_strategy"] = {}
    if strategy not in cal["by_strategy"]:
        cal["by_strategy"][strategy] = {"total": 0, "correct": 0, "win_rate": 0.0}
    bstrat = cal["by_strategy"][strategy]
    bstrat["total"]   += 1
    bstrat["correct"] += int(correct)
    bstrat["win_rate"] = _win_rate(bstrat)

    # By zone (favourite-longshot bias zone)
    zone = result.get("zone", "UNKNOWN")
    if "by_zone" not in cal:
        cal["by_zone"] = {}
    if zone not in cal["by_zone"]:
        cal["by_zone"][zone] = {"total": 0, "correct": 0, "win_rate": 0.0}
    bzone = cal["by_zone"][zone]
    bzone["total"]   += 1
    bzone["correct"] += int(correct)
    bzone["win_rate"] = _win_rate(bzone)

    # Phase 0 progress
    from proba.paths import get_setting
    phase0_target = get_setting("paper.target_trades_phase0", 50)
    cal["phase0_target"]   = phase0_target
    cal["phase0_progress"] = cal["total"]

    _save_calibration(cal)


def get_calibration_summary() -> Dict:
    """Return current calibration stats."""
    return _load_calibration()


def phase0_ready_for_phase1() -> tuple[bool, str]:
    """
    Check if Phase 0 thresholds are met to advance to Phase 1.
    Returns (ready: bool, reason: str).
    """
    cal = get_calibration_summary()
    target   = cal.get("phase0_target", 50)
    progress = cal.get("phase0_progress", 0)

    if progress < target:
        return False, f"Need {target - progress} more resolved trades ({progress}/{target})"

    overall_wr = cal.get("win_rate", 0.0)
    high_conf  = cal.get("by_confidence", {}).get("high", {})
    high_wr    = high_conf.get("win_rate", 0.0) if high_conf.get("total", 0) >= 5 else None

    if overall_wr < 0.60:
        return False, f"Overall win rate {overall_wr:.1%} < 60% target"

    if high_wr is not None and high_wr < 0.75:
        return False, f"High-confidence win rate {high_wr:.1%} < 75% target"

    return True, f"✓ Phase 0 complete — overall {overall_wr:.1%}, high-conf {high_wr:.1%}"
