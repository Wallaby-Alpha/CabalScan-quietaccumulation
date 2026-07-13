"""
Helius adapter.

Converts Helius enhanced transactions for a given token mint into internal
Trade objects.
"""

import time
import json
import requests
from models import Trade


HELIUS_BASE = "https://api.helius.xyz/v0"

SOL_MINT = "So11111111111111111111111111111111111111"

STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


class HeliusError(RuntimeError):
    pass


def _sol_price_usd(api_key: str) -> float:
    try:
        resp = requests.get(
            "https://api.dexscreener.com/latest/dex/tokens/" + SOL_MINT,
            timeout=10,
        )
        resp.raise_for_status()

        pairs = resp.json().get("pairs") or []

        if pairs:
            return float(pairs[0]["priceUsd"])

    except Exception:
        pass

    return 150.0


def fetch_trades(
    token_mint,
    api_key,
    max_transactions=1000,
    progress_callback=None
):
    sol_price = _sol_price_usd(api_key)

    trades = []
    before = None
    fetched = 0

    while fetched < max_transactions:

        params = {
            "api-key": api_key,
            "limit": 100,
        }

        if before:
            params["before-signature"] = before

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

        # DEBUG: print response once
        print(json.dumps(batch, indent=2)[:10000])
        raise Exception("STOP")

        if not isinstance(batch, list) or not batch:
            break

        for tx in batch:
            if not tx:
                continue

            trade = _parse_swap(
                tx,
                token_mint,
                sol_price,
            )

            if trade:
                trades.append(trade)

        fetched += len(batch)

        before = batch[-1].get("signature")

        if not before:
            break

        if progress_callback:
            progress_callback(fetched)

        if len(batch) < 100:
            break

        time.sleep(0.15)

    return trades


def _parse_swap(tx, token_mint, sol_price):

    # TEMPORARY PLACEHOLDER
    # We will replace this after seeing the Helius JSON output.

    return None
