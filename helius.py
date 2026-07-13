"""
Helius adapter.

Converts Helius enhanced transactions for a given token mint into internal
Trade objects.
"""

import time
import json
import requests
from datetime import datetime, timezone
from models import Trade  # Assumes you have a trade constructor/dataclass mapped here

HELIUS_BASE = "https://api.helius.xyz/v0"
SOL_MINT = "So111111111111111111111111111111111111112"  # Fixed: Added missing '2'

STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


class HeliusError(RuntimeError):
    pass


def _sol_price_usd() -> float:
    """Queries current native SOL token valuation from DexScreener api."""
    try:
        resp = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{SOL_MINT}",
            timeout=10,
        )
        resp.raise_for_status()
        pairs = resp.json().get("pairs") or []
        if pairs:
            return float(pairs[0]["priceUsd"])
    except Exception:
        pass
    return 150.0  # Safe fallback if network error hits


def fetch_trades(
    token_mint,
    api_key,
    max_transactions=1000,
    progress_callback=None
):
    sol_price = _sol_price_usd()

    trades = []
    before = None
    fetched = 0

    while fetched < max_transactions:
        # Helius pagination defaults to 'api-key' inside standard query params
        params = {
            "api-key": api_key,
            "limit": 100,
        }

        # FIXED: Helius API uses 'before', not 'before-signature'
        if before:
            params["before"] = before

        url = f"{HELIUS_BASE}/addresses/{token_mint}/transactions"

        resp = requests.get(
            url,
            params=params,
            timeout=30,
        )

        if resp.status_code != 200:
            raise HeliusError(
                f"Helius returned {resp.status_code}: {resp.text[:300]}"
            )

        batch = resp.json()

        if not isinstance(batch, list) or not batch:
            break

        for tx in batch:
            if not tx:
                continue

            # FIXED: Aligned parameters with function signature definition below
            trade = _parse_swap(
                tx=tx,
                token_mint=token_mint,
                sol_price=sol_price,
            )

            if trade:
                trades.append(trade)

        fetched += len(batch)

        # Update cursor tracker using the signature of the last item in the list
        before = batch[-1].get("signature")

        if not before:
            break

        if progress_callback:
            progress_callback(fetched)

        # Break early if the returned array size implies we hit the bottom of the ledger
        if len(batch) < 100:
            break

        time.sleep(0.15)

    return trades


def _parse_swap(tx: dict, token_mint: str, sol_price: float) -> Optional[Trade]:
    """
    Parses a Helius enhanced transaction payload to isolate token swaps.
    """
    # Verify transaction type is a true token exchange
    if tx.get("type") != "SWAP":
        return None

    # Implement your parsing logic here to extract transaction details.
    # Ex: sender = tx.get("feePayer")
    #     timestamp = tx.get("timestamp")
    
    return None
