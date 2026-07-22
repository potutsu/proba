"""
eula.py — terms display, first-run acceptance
Proba | NoLaptopTrades
"""

import json
from pathlib import Path

from proba.paths import get_path, log_error

EULA_VERSION = "1.0"

EULA_TEXT = """\
╔══════════════════════════════════════════════════════════════╗
║            PROBA — Terms of Use & Disclaimer                 ║
╚══════════════════════════════════════════════════════════════╝

Version: {version}

1. PAPER TRADING ONLY (Phase 0)
   Proba is currently in Phase 0: paper trading mode only.
   No real money is placed. All positions are simulated for
   research and calibration purposes.

2. NOT FINANCIAL ADVICE
   Proba is a research and learning tool. Nothing produced by
   this software constitutes financial, investment, or betting
   advice. All decisions are yours alone.

3. PREDICTION MARKET RISK
   Prediction markets carry risk. Past calibration results do
   not guarantee future accuracy. You can lose money in Phase 1+.

4. LOCAL-ONLY & TRANSPARENT
   Proba runs locally on your machine. No data is sent to
   NoLaptopTrades servers unless you explicitly enable sharing.

5. RESEARCH USE
   This tool is intended for research into market efficiency
   and probability calibration. Use responsibly and in
   accordance with Futuur's terms of service.

By typing AGREE you confirm you have read and understood these terms.
"""


def _eula_acceptance_path() -> Path:
    return get_path("error_log").parent / ".eula_accepted"


def is_accepted() -> bool:
    """Return True if the current EULA version has been accepted."""
    path = _eula_acceptance_path()
    if not path.exists():
        return False
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data.get("version") == EULA_VERSION
    except Exception:
        return False


def show_and_prompt() -> bool:
    """
    Display the EULA and prompt for acceptance.
    Returns True if accepted, False if declined.
    """
    print(EULA_TEXT.format(version=EULA_VERSION))
    try:
        response = input("Type AGREE to accept, or press Enter to exit: ").strip().upper()
    except (EOFError, KeyboardInterrupt):
        return False

    if response == "AGREE":
        _record_acceptance()
        return True
    return False


def _record_acceptance() -> None:
    path = _eula_acceptance_path()
    from datetime import datetime, timezone
    with open(path, "w") as f:
        json.dump({
            "version":     EULA_VERSION,
            "accepted_at": datetime.now(timezone.utc).isoformat(),
        }, f)


def ensure_accepted() -> None:
    """Call at startup. Prompts for EULA if not yet accepted. Exits if declined."""
    if is_accepted():
        return
    accepted = show_and_prompt()
    if not accepted:
        print("\nEULA not accepted. Exiting.")
        raise SystemExit(1)
