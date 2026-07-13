"""
Helius adapter.

Converts Helius's "enhanced transactions" for a given token mint into our
internal Trade format. This is the only file that should know anything
about Helius's response shape. If you ever add another source (Solscan CSV,
Birdeye, etc.), it gets its own adapter file that also returns list[Trade]
and everything else in the app stays unchanged.

Docs: https://docs.helius.dev/solana-apis/enhanced-transactions-api
"""

import time
import requests
from models import Trade

HELIUS_BASE = "https://api.helius.xyz/v0"

# Native SOL and the major stablecoins, used to figure out which leg of a
# swap represents "USD value" when the token itself isn't a stable/SOL.
SOL_MINT = "So11111111111111111111111111111111111111"
STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


class HeliusError(RuntimeError):
    pass


def _sol_price_usd(api_key: str) -> float:
    """
    Rough SOL/USD price for converting SOL-denominated swap legs to USD.
    Uses DexScreener's SOL/USDC pair as a free, keyless source so we don't
    need a second paid API just for this.
    """
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
    return 150.0  # crude fallback if the lookup fails; report will still run


def fetch_trades(
    token_mint: str,
    api_key: str,
    max_transactions: int = 1000,
    progress_callback=None,
) -> list[Trade]:
    """
    Pull SWAP transactions involving token_mint from Helius and convert
    each into a Trade (or two, for edge cases -- see note below).

    Note on direction: for a SWAP transaction, the wallet is "buying"
    token_mint if token_mint appears in tokenOutputs for that wallet's
    transfer, and "selling" if it appears in tokenInputs.
    """
    sol_price = _sol_price_usd(api_key)
    trades: list[Trade] = []
    before = None
    fetched = 0

    while fetched < max_transactions:
        params = {
            "api-key": api_key,
            "type": "SWAP",
            "limit": 100,
        }
        if before:
            params["before"] = before

        url = f"{HELIUS_BASE}/addresses/{token_mint}/transactions"
        resp = requests.get(url, params=params, timeout=30)

        if resp.status_code != 200:
            raise HeliusError(
                f"Helius returned {resp.status_code}: {resp.text[:300]}"
            )

        batch = resp.json()
        if not batch:
            break

        for tx in batch:
            trade = _parse_swap(tx, token_mint, sol_price)
            if trade:
                trades.append(trade)

        fetched += len(batch)
        before = batch[-1].get("signature")

        if progress_callback:
            progress_callback(fetched)

        if len(batch) < 100:
            break  # last page

        time.sleep(0.15)  # stay polite to the API

    return trades


def _parse_swap(tx: dict, token_mint: str, sol_price: float) -> Trade | None:
    """
    Parse a single Helius enhanced-transaction SWAP event into a Trade.

    Helius's "events.swap" block (when present) already resolves the
    wallet, tokenInputs, tokenOutputs, nativeInput/nativeOutput. This is
    the fast path. If events.swap is missing (some routers aren't
    decoded), we skip the transaction rather than guess -- silently wrong
    direction is worse than a missing data point.
    """
    swap = (tx.get("events") or {}).get("swap")
    if not swap:
        return None

    wallet = tx.get("feePayer") or swap.get("tokenOutputs", [{}])[0].get("userAccount")
    if not wallet:
        return None

    token_in = next(
        (t for t in swap.get("tokenInputs", []) if t.get("mint") == token_mint), None
    )
    token_out = next(
        (t for t in swap.get("tokenOutputs", []) if t.get("mint") == token_mint), None
    )

    if token_out and not token_in:
        direction = "buy"
        amount = float(token_out.get("tokenAmount", 0))
        usd_value = _counter_leg_usd(swap, sol_price, exclude_mint=token_mint)
    elif token_in and not token_out:
        direction = "sell"
        amount = float(token_in.get("tokenAmount", 0))
        usd_value = _counter_leg_usd(swap, sol_price, exclude_mint=token_mint)
    else:
        # both or neither -- not a clean buy/sell of this mint, skip
        return None

    if amount <= 0:
        return None

    return Trade(
        wallet=wallet,
        token_mint=token_mint,
        direction=direction,
        token_amount=amount,
        usd_value=usd_value,
        timestamp=int(tx.get("timestamp", 0)),
        tx_sig=tx.get("signature", ""),
    )


def _counter_leg_usd(swap: dict, sol_price: float, exclude_mint: str) -> float:
    """
    Estimate the USD value of a swap from whichever leg isn't our target
    token: native SOL, a stablecoin, or (fallback) just 0.
    """
    native_in = float(swap.get("nativeInput", {}).get("amount", 0) or 0) / 1e9
    native_out = float(swap.get("nativeOutput", {}).get("amount", 0) or 0) / 1e9
    if native_in:
        return native_in * sol_price
    if native_out:
        return native_out * sol_price

    for leg in swap.get("tokenInputs", []) + swap.get("tokenOutputs", []):
        if leg.get("mint") in STABLE_MINTS and leg.get("mint") != exclude_mint:
            return float(leg.get("tokenAmount", 0))

    return 0.0
