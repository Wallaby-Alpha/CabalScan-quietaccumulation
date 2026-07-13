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


def fetch_trades(token_mint, api_key, max_transactions=1000, progress_callback=None):
    sol_price = _sol_price_usd(api_key)
    trades = []
    before = None
    fetched = 0

    while fetched < max_transactions:
        params = {"api-key": api_key, "limit": 100}   # <- drop server-side type=SWAP
        if before:
            params["before-signature"] = before        # <- fixed param name

        url = f"{HELIUS_BASE}/addresses/{token_mint}/transactions"
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            raise HeliusError(f"Helius returned {resp.status_code}: {resp.text[:300]}")

        batch = resp.json()

        import json

        print(json.dumps(batch, indent=2)[:10000])
        raise Exception("STOP")

        if not isinstance(batch, list) or not batch:
            break

        for tx in batch:
            if not tx:
                continue
            trade = _parse_swap(tx, token_mint, sol_price)
            if trade:
                trades.append(trade)

        fetched += len(batch)
        before = (batch[-1] or {}).get("signature")
        if not before:
            break
        if progress_callback:
            progress_callback(fetched)
        if len(batch) < 100:
            break
        time.sleep(0.15)

    return trades

def _parse_swap(tx: dict, token_mint: str, sol_price: float) -> Trade | None:
    wallet = tx.get("feePayer")
    if not wallet:
        return None

    # -------------------------------------------------------
    # Fast path: Helius decoded the swap
    # -------------------------------------------------------
    swap = (tx.get("events") or {}).get("swap")
    if swap:
        token_inputs = [t for t in (swap.get("tokenInputs") or []) if t]
        token_outputs = [t for t in (swap.get("tokenOutputs") or []) if t]

        token_in = next((t for t in token_inputs if t.get("mint") == token_mint), None)
        token_out = next((t for t in token_outputs if t.get("mint") == token_mint), None)

        if token_out and not token_in:
            return Trade(
                wallet=wallet,
                token_mint=token_mint,
                direction="buy",
                token_amount=float(token_out.get("tokenAmount", 0)),
                usd_value=_counter_leg_usd(swap, sol_price, token_mint),
                timestamp=int(tx.get("timestamp", 0)),
                tx_sig=tx.get("signature", ""),
            )

        if token_in and not token_out:
            return Trade(
                wallet=wallet,
                token_mint=token_mint,
                direction="sell",
                token_amount=float(token_in.get("tokenAmount", 0)),
                usd_value=_counter_leg_usd(swap, sol_price, token_mint),
                timestamp=int(tx.get("timestamp", 0)),
                tx_sig=tx.get("signature", ""),
            )

    # -------------------------------------------------------
    # Fallback: use token balance changes
    # -------------------------------------------------------

    changes = tx.get("accountData", [])

    before = None
    after = None

    for acct in changes:
        for bal in acct.get("tokenBalanceChanges", []) or []:
            if bal.get("mint") != token_mint:
                continue

            if before is None:
                before = float(bal.get("rawTokenAmount", {}).get("tokenAmount", 0))

            after = float(bal.get("rawTokenAmount", {}).get("tokenAmount", 0))

    if before is None or after is None:
        return None

    delta = after - before

    if delta == 0:
        return None

    direction = "buy" if delta > 0 else "sell"

    return Trade(
        wallet=wallet,
        token_mint=token_mint,
        direction=direction,
        token_amount=abs(delta),
        usd_value=0,
        timestamp=int(tx.get("timestamp", 0)),
        tx_sig=tx.get("signature", ""),
    )


def _counter_leg_usd(swap: dict, sol_price: float, exclude_mint: str) -> float:
    """
    Estimate the USD value of a swap from whichever leg isn't our target
    token: native SOL, a stablecoin, or (fallback) just 0.
    """
    native_in = float((swap.get("nativeInput") or {}).get("amount", 0) or 0) / 1e9
    native_out = float((swap.get("nativeOutput") or {}).get("amount", 0) or 0) / 1e9
    if native_in:
        return native_in * sol_price
    if native_out:
        return native_out * sol_price

    token_legs = [t for t in (swap.get("tokenInputs") or []) if t] + \
                 [t for t in (swap.get("tokenOutputs") or []) if t]
    for leg in token_legs:
        if leg.get("mint") in STABLE_MINTS and leg.get("mint") != exclude_mint:
            return float(leg.get("tokenAmount", 0) or 0)

    return 0.0
