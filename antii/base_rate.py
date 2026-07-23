"""
base_rate.py — OR base rate lookup (antii subsystem)
Anti-Insanity | Proba

Broader than proba/base_rate.py — covers all non-sports categories.
Pattern matches market titles against known historical base rates.
First match wins — ordered most specific → most general.

Returns None if no match — scorer handles None gracefully.
"""

from typing import Optional

_BASE_RATES = [
    # ── Nobel ──────────────────────────────────────────────────────
    (["nobel", "peace"],                    0.02),
    (["nobel", "prize"],                    0.02),

    # ── Impeachment / Removal ──────────────────────────────────────
    (["impeach", "convict"],                0.04),
    (["impeach"],                           0.06),
    (["removed from office"],               0.04),

    # ── Resignation / Death ────────────────────────────────────────
    (["resign"],                            0.07),
    (["die", "office"],                     0.03),
    (["death", "office"],                   0.03),

    # ── Nuclear / WMD ──────────────────────────────────────────────
    (["nuclear", "war"],                    0.02),
    (["nuclear", "attack"],                 0.02),
    (["nuclear", "weapon"],                 0.03),
    (["nuclear"],                           0.04),

    # ── World War / Conflict ───────────────────────────────────────
    (["world war"],                         0.02),
    (["ww3"],                               0.02),
    (["invasion"],                          0.10),
    (["ceasefire"],                         0.35),
    (["peace deal"],                        0.15),
    (["peace agreement"],                   0.15),

    # ── Assassination ──────────────────────────────────────────────
    (["assassin"],                          0.02),
    (["assassination"],                     0.02),

    # ── Coup / Regime change ───────────────────────────────────────
    (["coup"],                              0.05),
    (["regime change"],                     0.04),
    (["overthrow"],                         0.04),
    (["revolution"],                        0.05),

    # ── Sanctions ──────────────────────────────────────────────────
    (["sanction", "lift"],                  0.20),
    (["sanction", "remove"],                0.20),
    (["sanction"],                          0.30),

    # ── Government shutdown / Default ─────────────────────────────
    (["debt ceiling", "default"],           0.06),
    (["government shutdown"],               0.20),
    (["debt default"],                      0.05),

    # ── Fed / Rates ────────────────────────────────────────────────
    (["fed", "emergency cut"],              0.05),
    (["fed", "cut", "50"],                  0.15),
    (["fed", "cut", "75"],                  0.05),
    (["fed", "cut"],                        0.32),
    (["fed", "hike"],                       0.22),
    (["fed", "pause"],                      0.40),
    (["rate cut"],                          0.32),
    (["rate hike"],                         0.22),
    (["interest rate"],                     0.35),

    # ── Recession / Crash / Crisis ─────────────────────────────────
    (["recession"],                         0.25),
    (["depression"],                        0.05),
    (["financial crisis"],                  0.08),
    (["market crash"],                      0.10),
    (["crash"],                             0.12),
    (["collapse"],                          0.08),
    (["hyperinflation"],                    0.04),
    (["deflation"],                         0.10),
    (["stagflation"],                       0.15),

    # ── Inflation / CPI ────────────────────────────────────────────
    (["cpi", "above"],                      0.35),
    (["cpi", "below"],                      0.35),
    (["inflation", "above"],                0.35),
    (["inflation", "below"],                0.35),

    # ── Crypto price targets ───────────────────────────────────────
    (["bitcoin", "1000000"],                0.03),
    (["bitcoin", "500000"],                 0.05),
    (["bitcoin", "250000"],                 0.10),
    (["bitcoin", "200000"],                 0.12),
    (["bitcoin", "150000"],                 0.18),
    (["bitcoin", "100000"],                 0.28),
    (["bitcoin", "50000"],                  0.45),
    (["bitcoin", "zero"],                   0.02),
    (["bitcoin", "crash"],                  0.12),
    (["eth", "10000"],                      0.08),
    (["eth", "5000"],                       0.20),
    (["crypto", "ban"],                     0.07),
    (["crypto", "regulation"],              0.45),
    (["crypto", "crash"],                   0.12),
    (["stablecoin", "depeg"],               0.06),
    (["defi", "hack"],                      0.08),

    # ── AI / Tech ──────────────────────────────────────────────────
    (["ai", "ban"],                         0.05),
    (["ai", "shutdown"],                    0.04),
    (["ai", "regulation"],                  0.40),
    (["agi"],                               0.08),
    (["antitrust", "break"],                0.08),
    (["breakup"],                           0.08),
    (["ipo"],                               0.30),

    # ── Election / Political ───────────────────────────────────────
    (["electoral college", "tie"],          0.03),
    (["third party", "win"],                0.04),
    (["landslide"],                         0.15),
    (["recount"],                           0.10),
    (["fraud"],                             0.08),
    (["concede"],                           0.40),
    (["veto"],                              0.25),
    (["executive order"],                   0.45),
    (["filibuster"],                        0.30),
    (["pardon"],                            0.20),
    (["pardon trump"],                      0.15),
    (["supreme court"],                     0.40),

    # ── Health / Pandemic ──────────────────────────────────────────
    (["pandemic"],                          0.05),
    (["lockdown"],                          0.08),
    (["emergency", "declaration"],          0.20),
    (["outbreak"],                          0.12),
    (["vaccine"],                           0.50),

    # ── Natural disasters / Climate ────────────────────────────────
    (["category 5"],                        0.08),
    (["category 4"],                        0.15),
    (["earthquake", "major"],               0.05),
    (["climate", "emergency"],              0.10),
    (["net zero"],                          0.25),

    # ── Space ──────────────────────────────────────────────────────
    (["moon landing"],                      0.15),
    (["mars"],                              0.08),
    (["launch", "fail"],                    0.15),

    # ── Airspace / closure ────────────────────────────────────────
    (["airspace", "closure"],      0.12),
    (["airspace", "close"],        0.12),
    (["airspace", "closed"],       0.12),
    (["airspace"],                  0.15),
    (["no-fly"],                    0.10),
    (["border", "close"],          0.15),
    (["port", "close"],            0.20),

    # ── Strike / attack ───────────────────────────────────────────
    (["strike", "iran"],           0.12),
    (["attack", "iran"],           0.10),
    (["strike", "israel"],         0.12),
    (["iran", "israel"],           0.15),
    (["missile", "strike"],        0.10),

    # ── Geopolitical specific ──────────────────────────────────────
    (["taiwan", "invasion"],                0.04),
    (["taiwan", "war"],                     0.04),
    (["china", "invade"],                   0.04),
    (["north korea"],                       0.12),
    (["iran", "nuclear"],                   0.08),
    (["israel", "ceasefire"],               0.30),
    (["ukraine", "peace"],                  0.15),
    (["nato"],                              0.25),

    # ── Trade / Tariffs ────────────────────────────────────────────
    (["tariff"],                            0.40),
    (["trade war"],                         0.20),
    (["trade deal"],                        0.25),
    (["trade agreement"],                   0.30),
]


def estimate_base_rate(title: str) -> Optional[float]:
    """
    Pattern match a market title against known base rates.
    Returns float in [0, 1] or None if no match.
    First match wins — table is ordered most specific first.
    """
    t = title.lower()
    for keywords, rate in _BASE_RATES:
        if all(kw in t for kw in keywords):
            return rate
    return None


def price_gap(market_price: float, base_rate: float) -> float:
    """
    How far the YES market price is above base rate.
    Positive = market is pricing YES higher than historical base (hype).
    Fade signal = positive gap.
    """
    return round(market_price - base_rate, 4)
