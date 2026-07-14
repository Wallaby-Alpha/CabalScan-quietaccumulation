"""
Helius adapter.

Converts Helius's "enhanced transactions" for a given token mint into our
internal Trade format. This is the only file that should know anything
about Helius's response shape. If you ever add another source (Solscan CSV,
Birdeye, etc.), it gets its own adapter file that also returns list[Trade]
and everything else in the app stays unchanged.

Fetch strategy: instead of the deprecated GET /v0/addresses/{address}/transactions
endpoint (unreliable pagination, and server-side type filtering only scans a
shallow window before giving up), we do it in two steps:

  1. getSignaturesForAddress (standard Solana JSON-RPC) -- returns the
     complete, correctly-paginated list of signatures for the mint.
  2. POST /v0/transactions -- Helius's enhanced parser, batched up to 100
     signatures at a time, returns the same events.swap structure we parse
     below.

Docs:
  https://docs.helius.dev/solana-apis/enhanced-transactions-api
  https://docs.solana.com/api/http#getsignaturesforaddress
"""

import time

import requests

from models import Trade

HELIUS_RPC_URL = "https://mainnet.helius-rpc.com"
HELIUS_PARSE_URL = "https://api.helius.xyz/v0/transactions"

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

    signatures = _get_signatures(token_mint, api_key, max_transactions)
    if not signatures:
        return []

    trades: list[Trade] = []
    fetched = 0

    for i in range(0, len(signatures), 100):
        batch_sigs = signatures[i : i + 100]

        resp = requests.post(
            HELIUS_PARSE_URL,
            params={"api-key": api_key},
            json={"transactions": batch_sigs},
            timeout=30,
        )
        if resp.status_code != 200:
            raise HeliusError(
                f"Helius returned {resp.status_code}: {resp.text[:300]}"
            )

        batch = resp.json()
        if not isinstance(batch, list):
            # Unexpected shape (e.g. an error object) -- surface it rather
            # than silently treating it as zero trades.
            raise HeliusError(f"Unexpected response parsing transactions: {batch}")

        for tx in batch:
            if not tx:
                continue
            trade = _parse_swap(tx, token_mint, sol_price)
            if trade:
                trades.append(trade)

        fetched += len(batch_sigs)
        if progress_callback:
            progress_callback(fetched)

        time.sleep(0.15)  # stay polite to the API

    return trades


def _get_signatures(token_mint: str, api_key: str, max_transactions: int) -> list[str]:
    """
    Full, correctly-paginated signature list for the mint via standard
    Solana JSON-RPC (getSignaturesForAddress). Skips failed transactions.
    """
    sigs: list[str] = []
    before = None

    while len(sigs) < max_transactions:
        opts = {"limit": 1000}
        if before:
            opts["before"] = before

        resp = requests.post(
            HELIUS_RPC_URL,
            params={"api-key": api_key},
            json={
                "jsonrpc": "2.0",
                "id": "cabalscan-sigs",
                "method": "getSignaturesForAddress",
                "params": [token_mint, opts],
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise HeliusError(
                f"Helius RPC returned {resp.status_code}: {resp.text[:300]}"
            )

        payload = resp.json()
        if "error" in payload:
            raise HeliusError(f"Helius RPC error: {payload['error']}")

        result = payload.get("result") or []
        if not result:
            break

        sigs.extend(r["signature"] for r in result if not r.get("err"))
        before = result[-1]["signature"]

        if len(result) < 1000:
            break  # last page

        time.sleep(0.1)

    return sigs[:max_transactions]


def _parse_swap(tx: dict, token_mint: str, sol_price: float) -> Trade | None:
    """
    Parse a single Helius enhanced transaction into a Trade.

    Fast path: Helius's "events.swap" block (when present) already
    resolves the wallet, tokenInputs, tokenOutputs, nativeInput/nativeOutput.

    Fallback: many swaps -- especially on newer/smaller programs like
    PumpSwap -- don't get an events.swap block built, and aren't even
    reliably tagged type == "SWAP" by Helius's classifier. Rather than
    trust that tag, reconstruct the buy/sell from the raw tokenTransfers +
    nativeTransfers arrays (present on essentially every parsed
    transaction regardless of program) whenever there's a net token move
    for the fee payer paired with a real SOL/stablecoin counter-leg. That
    counter-leg requirement is what keeps plain transfers/airdrops from
    being miscounted as trades.
    """
    swap = (tx.get("events") or {}).get("swap")
    if swap:
        token_inputs = [t for t in (swap.get("tokenInputs") or []) if t]
        token_outputs = [t for t in (swap.get("tokenOutputs") or []) if t]

        wallet = tx.get("feePayer")
        if not wallet and token_outputs:
            wallet = token_outputs[0].get("userAccount")
        if not wallet and token_inputs:
            wallet = token_inputs[0].get("userAccount")
        if not wallet:
            return None

        token_in = next((t for t in token_inputs if t.get("mint") == token_mint), None)
        token_out = next((t for t in token_outputs if t.get("mint") == token_mint), None)

        if token_out and not token_in:
            direction = "buy"
            amount = float(token_out.get("tokenAmount", 0))
            usd_value = _counter_leg_usd(swap, sol_price, exclude_mint=token_mint)
        elif token_in and not token_out:
            direction = "sell"
            amount = float(token_in.get("tokenAmount", 0))
            usd_value = _counter_leg_usd(swap, sol_price, exclude_mint=token_mint)
        else:
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

    # Generic fallback, tried regardless of Helius's `type` tag. Programs
    # like PumpSwap often aren't classified as "SWAP" by Helius at all (they
    # come through as UNKNOWN or something else), so gating on `type` was
    # silently dropping entire DEXes. _parse_transfers only returns a Trade
    # when it finds a real SOL/stablecoin counter-leg, which is what
    # distinguishes an actual trade from a plain wallet-to-wallet transfer.
    return _parse_transfers(tx, token_mint, sol_price)


def _parse_transfers(tx: dict, token_mint: str, sol_price: float) -> Trade | None:
    """
    Reconstruct a buy/sell of token_mint from a transaction's raw
    tokenTransfers + nativeTransfers, for swaps Helius tagged but didn't
    build an events.swap block for.
    """
    wallet = tx.get("feePayer")
    if not wallet:
        return None

    token_transfers = [t for t in (tx.get("tokenTransfers") or []) if t]
    native_transfers = [n for n in (tx.get("nativeTransfers") or []) if n]

    amount_in = sum(
        float(t.get("tokenAmount", 0) or 0)
        for t in token_transfers
        if t.get("toUserAccount") == wallet and t.get("mint") == token_mint
    )
    amount_out = sum(
        float(t.get("tokenAmount", 0) or 0)
        for t in token_transfers
        if t.get("fromUserAccount") == wallet and t.get("mint") == token_mint
    )

    net = amount_in - amount_out
    if net > 0:
        direction = "buy"
        amount = net
    elif net < 0:
        direction = "sell"
        amount = -net
    else:
        return None

    sol_out = sum(
        float(n.get("amount", 0) or 0)
        for n in native_transfers
        if n.get("fromUserAccount") == wallet
    ) / 1e9
    sol_in = sum(
        float(n.get("amount", 0) or 0)
        for n in native_transfers
        if n.get("toUserAccount") == wallet
    ) / 1e9

    if direction == "buy" and sol_out:
        usd_value = sol_out * sol_price
    elif direction == "sell" and sol_in:
        usd_value = sol_in * sol_price
    elif direction == "buy":
        usd_value = sum(
            float(t.get("tokenAmount", 0) or 0)
            for t in token_transfers
            if t.get("fromUserAccount") == wallet and t.get("mint") in STABLE_MINTS
        )
    else:
        usd_value = sum(
            float(t.get("tokenAmount", 0) or 0)
            for t in token_transfers
            if t.get("toUserAccount") == wallet and t.get("mint") in STABLE_MINTS
        )

    if amount <= 0:
        return None

    # No real SOL/stablecoin counter-leg found -- this wasn't a trade
    # (e.g. a plain wallet-to-wallet transfer, an airdrop, or a claim).
    # Without this check every non-swap token movement would get counted
    # as a $0 "trade", which would wreck the wallet/retention math.
    if usd_value <= 0:
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


def debug_fetch(token_mint: str, api_key: str, sample: int = 200) -> dict:
    """
    Diagnostic helper: pulls a small sample of signatures + parsed
    transactions for token_mint and reports what came back, without
    doing the full trade-conversion pipeline. Meant to be called from
    the UI when fetch_trades() returns empty, so we can see *why*
    without needing a separate script.
    """
    from collections import Counter

    info = {
        "signature_count": 0,
        "parsed_count": 0,
        "type_breakdown": {},
        "events_swap_count": 0,
        "sample_signature": None,
        "sample_tx_keys": None,
        "error": None,
    }

    try:
        sigs = _get_signatures(token_mint, api_key, sample)
        info["signature_count"] = len(sigs)
        if sigs:
            info["sample_signature"] = sigs[0]
    except Exception as e:
        info["error"] = f"getSignaturesForAddress failed: {e}"
        return info

    if not sigs:
        return info

    try:
        resp = requests.post(
            HELIUS_PARSE_URL,
            params={"api-key": api_key},
            json={"transactions": sigs[:100]},
            timeout=30,
        )
        if resp.status_code != 200:
            info["error"] = f"parse call returned {resp.status_code}: {resp.text[:300]}"
            return info

        batch = resp.json()
        if not isinstance(batch, list):
            info["error"] = f"unexpected parse response shape: {batch}"
            return info

        info["parsed_count"] = len(batch)
        types = Counter(tx.get("type") for tx in batch if tx)
        info["type_breakdown"] = dict(types)
        info["events_swap_count"] = sum(
            1 for tx in batch if tx and (tx.get("events") or {}).get("swap")
        )
        if batch and batch[0]:
            info["sample_tx_keys"] = list(batch[0].keys())
    except Exception as e:
        info["error"] = f"parse call failed: {e}"

    return info


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
