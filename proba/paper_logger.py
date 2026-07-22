"""
paper_logger.py — paper trade decisions + open position tracking
Proba | NoLaptopTrades
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from proba.paths import get_path, get_setting, log_error


# ---------------------------------------------------------------------------
# Internal I/O helpers
# ---------------------------------------------------------------------------

def _read_positions() -> List[Dict]:
    """Load all positions from paper_positions.jsonl."""
    path = get_path("paper_positions")
    if not path.exists():
        return []
    positions = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                positions.append(json.loads(line))
            except json.JSONDecodeError as e:
                log_error("paper_logger", f"Corrupt line in paper_positions.jsonl: {e}")
    return positions


def _write_positions(positions: List[Dict]) -> None:
    """Rewrite paper_positions.jsonl from list (used for updates)."""
    path = get_path("paper_positions")
    with open(path, "w", encoding="utf-8") as f:
        for pos in positions:
            f.write(json.dumps(pos) + "\n")


def _append_position(position: Dict) -> None:
    """Append a single position record."""
    path = get_path("paper_positions")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(position) + "\n")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def log_paper_entry(
    scored_market: Dict,
    your_estimate: float,
    direction: str,        # "yes" or "no"
    reasoning: str = "",
) -> str:
    """
    Record a new paper trade decision.
    Returns the generated position_id (UUID).
    """
    direction = direction.lower().strip()
    if direction not in ("yes", "no"):
        raise ValueError(f"direction must be 'yes' or 'no', got: {direction!r}")

    position_id  = str(uuid.uuid4())
    entry_price  = scored_market["market_price"]
    edge         = round(your_estimate - entry_price, 4)
    phase        = get_setting("paper.phase", 0)
    size_usd     = get_setting("paper.default_position_size_usd", 20)
    confidence   = scored_market.get("confidence") or "low"

    position = {
        "id":              position_id,
        "event_id":        scored_market["event_id"],
        "market_id":       scored_market["market_id"],
        "title":           scored_market["title"],
        "category":        scored_market.get("category", ""),
        "direction":       direction,
        "entry_price":     entry_price,
        "your_estimate":   round(your_estimate, 4),
        "edge":            edge,
        "confidence":      confidence,
        "position_size_usd": size_usd,
        "entry_ts":        datetime.now(timezone.utc).isoformat(),
        "close_date":      scored_market.get("close_date", ""),
        "status":          "open",
        "reasoning":       reasoning,
        "phase":           phase,
    }

    _append_position(position)
    return position_id


def list_open_positions() -> List[Dict]:
    """Return all positions with status='open'."""
    return [p for p in _read_positions() if p.get("status") == "open"]


def list_all_positions() -> List[Dict]:
    """Return all positions (open + closed)."""
    return _read_positions()


def get_position(position_id: str) -> Optional[Dict]:
    """Fetch a single position by ID."""
    for p in _read_positions():
        if p.get("id") == position_id:
            return p
    return None


def close_position(position_id: str, outcome: str, resolved_price: float) -> Dict:
    """
    Mark a position as closed with the resolved outcome and price.
    Returns the updated position dict.
    """
    positions = _read_positions()
    updated = None

    for i, pos in enumerate(positions):
        if pos.get("id") == position_id:
            positions[i] = {
                **pos,
                "status":         "closed",
                "resolved_outcome": outcome.lower(),
                "resolved_price": resolved_price,
                "close_ts":       datetime.now(timezone.utc).isoformat(),
            }
            updated = positions[i]
            break

    if updated is None:
        raise KeyError(f"Position {position_id} not found.")

    _write_positions(positions)
    return updated


def update_position_price(position_id: str, current_price: float) -> Optional[Dict]:
    """Update the last-seen price on an open position (for monitor)."""
    positions = _read_positions()
    updated = None

    for i, pos in enumerate(positions):
        if pos.get("id") == position_id and pos.get("status") == "open":
            positions[i] = {
                **pos,
                "last_price":    current_price,
                "last_check_ts": datetime.now(timezone.utc).isoformat(),
            }
            updated = positions[i]
            break

    if updated:
        _write_positions(positions)
    return updated
