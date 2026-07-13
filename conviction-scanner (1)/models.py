"""
Internal trade format. Every data source (Helius today, CSV/others later)
gets adapted into a list of these. Nothing downstream should know or care
where a Trade came from.
"""

from dataclasses import dataclass


@dataclass
class Trade:
    wallet: str
    token_mint: str
    direction: str      # 'buy' or 'sell'
    token_amount: float
    usd_value: float
    timestamp: int       # unix seconds
    tx_sig: str
