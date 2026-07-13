"""
Analysis layer. Pure functions: list[Trade] in, DataFrames/dicts out.
No knowledge of Helius, Streamlit, or any data source -- this is the part
that stays stable no matter where trades came from.
"""

import time
import pandas as pd
from models import Trade


def trades_to_df(trades: list[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(
            columns=[
                "wallet", "token_mint", "direction", "token_amount",
                "usd_value", "timestamp", "tx_sig",
            ]
        )
    return pd.DataFrame([t.__dict__ for t in trades])


def wallet_summary(df: pd.DataFrame) -> pd.DataFrame:
    """
    One row per wallet: bought/sold amounts, current balance, capital
    invested, retention %, buy count, days held.

    Cost basis here is deliberately approximate (total USD in / total
    tokens bought = average price), not a per-lot weighted ledger. That's
    the "90% accuracy, 10x more tokens analyzable" tradeoff from the
    design discussion -- fine for this tool's purpose.
    """
    if df.empty:
        return pd.DataFrame()

    now = int(time.time())

    g = df.groupby("wallet")

    bought = g.apply(lambda d: d.loc[d.direction == "buy", "token_amount"].sum())
    sold = g.apply(lambda d: d.loc[d.direction == "sell", "token_amount"].sum())
    usd_in = g.apply(lambda d: d.loc[d.direction == "buy", "usd_value"].sum())
    usd_out = g.apply(lambda d: d.loc[d.direction == "sell", "usd_value"].sum())
    buy_count = g.apply(lambda d: (d.direction == "buy").sum())
    first_buy = g.apply(
        lambda d: d.loc[d.direction == "buy", "timestamp"].min()
        if (d.direction == "buy").any() else pd.NA
    )

    ws = pd.DataFrame({
        "bought": bought,
        "sold": sold,
        "usd_invested": usd_in,
        "usd_realized": usd_out,
        "buy_count": buy_count,
        "first_buy_time": first_buy,
    })

    ws["current_balance"] = (ws["bought"] - ws["sold"]).clip(lower=0)
    ws["retention_pct"] = (ws["current_balance"] / ws["bought"].replace(0, pd.NA)) * 100
    ws["avg_entry_price"] = ws["usd_invested"] / ws["bought"].replace(0, pd.NA)
    ws["days_held"] = ((now - ws["first_buy_time"]) / 86400).round(1)

    return ws.reset_index().sort_values("usd_invested", ascending=False)


def seller_quality(ws: pd.DataFrame) -> pd.DataFrame:
    """
    For wallets that sold anything: approximate realized ROI, using the
    same average-cost-basis approximation as wallet_summary.
    """
    if ws.empty:
        return pd.DataFrame()

    sellers = ws[ws["sold"] > 0].copy()
    if sellers.empty:
        return sellers

    sellers["avg_exit_price"] = sellers["usd_realized"] / sellers["sold"]
    sellers["realized_roi_x"] = (
        sellers["avg_exit_price"] / sellers["avg_entry_price"].replace(0, pd.NA)
    )
    return sellers.sort_values("usd_realized", ascending=False)


def ownership_migration(ws: pd.DataFrame, retention_floor: float = 20.0) -> dict:
    """
    Capital that left (low-retention wallets) vs capital that came in and
    stayed (high-retention wallets). This is the "ownership quality"
    signal -- not a new data pull, just a different read of wallet_summary.
    """
    if ws.empty:
        return {
            "capital_exited": 0.0, "capital_retained": 0.0,
            "net_migration": 0.0, "n_exited": 0, "n_retained": 0,
        }

    exited = ws[ws["retention_pct"] < retention_floor]
    retained = ws[ws["retention_pct"] >= 80]

    capital_exited = exited["usd_invested"].sum()
    capital_retained = retained["usd_invested"].sum()

    return {
        "capital_exited": round(capital_exited, 2),
        "capital_retained": round(capital_retained, 2),
        "net_migration": round(capital_retained - capital_exited, 2),
        "n_exited": len(exited),
        "n_retained": len(retained),
    }


def summary_stats(ws: pd.DataFrame, sq: pd.DataFrame) -> dict:
    if ws.empty:
        return {}

    total_capital = ws["usd_invested"].sum()
    still_holding_95 = ws[ws["retention_pct"] >= 95]
    trimmed = ws[(ws["retention_pct"] < 80) & (ws["retention_pct"] >= 20)]
    exited = ws[ws["retention_pct"] < 20]
    repeat_buyers = ws[ws["buy_count"] > 1]

    profit_sellers_pct = 0.0
    underwater_sellers_pct = 0.0
    avg_realized_roi = None
    if not sq.empty and sq["realized_roi_x"].notna().any():
        total_realized = sq["usd_realized"].sum()
        profit_realized = sq.loc[sq["realized_roi_x"] >= 1, "usd_realized"].sum()
        underwater_realized = sq.loc[sq["realized_roi_x"] < 1, "usd_realized"].sum()
        if total_realized > 0:
            profit_sellers_pct = round(100 * profit_realized / total_realized, 1)
            underwater_sellers_pct = round(100 * underwater_realized / total_realized, 1)
        avg_realized_roi = round(sq["realized_roi_x"].median(), 2)

    return {
        "n_wallets": len(ws),
        "total_capital": round(total_capital, 2),
        "avg_purchase": round(ws["usd_invested"].mean(), 2) if len(ws) else 0,
        "largest_purchase": round(ws["usd_invested"].max(), 2) if len(ws) else 0,
        "n_strong_holders": len(still_holding_95),
        "n_trimmed": len(trimmed),
        "n_exited": len(exited),
        "n_repeat_buyers": len(repeat_buyers),
        "median_realized_roi_x": avg_realized_roi,
        "pct_selling_from_profit": profit_sellers_pct,
        "pct_selling_underwater": underwater_sellers_pct,
    }
