"""
Turns analysis output into a readable report, plus the optional composite
score. Score is intentionally a thin, tunable layer on top of the
analysis -- change the weights here without touching analysis.py.
"""

import pandas as pd


def conviction_score(ws: pd.DataFrame) -> float | None:
    """
    Weighted sum (not a product -- see design discussion: a product lets
    any single near-zero factor collapse the score, which wrongly
    penalizes very fresh accumulation). Each component is normalized to
    roughly 0-1 before weighting. Treat this as a first draft to tune
    once you've read enough reports to know what "high" should look like.
    """
    if ws.empty:
        return None

    holding = ws[ws["current_balance"] > 0]
    if holding.empty:
        return 0.0

    weighted_retention = (
        (holding["retention_pct"] / 100 * holding["usd_invested"]).sum()
        / holding["usd_invested"].sum()
    )
    capital_component = min(holding["usd_invested"].sum() / 100_000, 1.0)
    days_component = min((holding["days_held"].clip(lower=0)).median() / 14, 1.0)
    rebuy_component = min((holding["buy_count"] > 1).mean() * 2, 1.0)

    score = (
        0.40 * weighted_retention
        + 0.30 * capital_component
        + 0.15 * days_component
        + 0.15 * rebuy_component
    )
    return round(score * 100, 1)


def render_report(token_mint: str, stats: dict, migration: dict, score: float | None) -> str:
    if not stats:
        return f"### No swap data found for `{token_mint}`\n\nEither the token has no recent swap activity, or Helius didn't return decodable swap events for it."

    lines = [f"## Ownership Report — `{token_mint}`", ""]

    if score is not None:
        lines += [f"### Conviction Score: **{score}/100**", ""]

    lines += [
        "### Buyers",
        f"- **{stats['n_wallets']}** wallets accumulated",
        f"- Total capital: **${stats['total_capital']:,.0f}**",
        f"- Average purchase: **${stats['avg_purchase']:,.0f}**",
        f"- Largest purchase: **${stats['largest_purchase']:,.0f}**",
        "",
        "### Retention",
        f"- **{stats['n_strong_holders']}** wallets still hold ≥95% of what they bought",
        f"- **{stats['n_trimmed']}** wallets trimmed to 20–80%",
        f"- **{stats['n_exited']}** wallets are effectively out (<20% retained)",
        "",
        "### Repeated Accumulation",
        f"- **{stats['n_repeat_buyers']}** wallets bought more than once",
        "",
        "### Sellers",
    ]

    if stats.get("median_realized_roi_x") is not None:
        lines += [
            f"- Median realized ROI on sells: **{stats['median_realized_roi_x']}x**",
            f"- **{stats['pct_selling_from_profit']}%** of realized sell volume came from profitable wallets",
            f"- **{stats['pct_selling_underwater']}%** came from wallets selling at a loss",
        ]
    else:
        lines += ["- No sell data to analyze yet"]

    lines += [
        "",
        "### Ownership Migration",
        f"- Capital exited (low-retention wallets): **${migration['capital_exited']:,.0f}** ({migration['n_exited']} wallets)",
        f"- Capital retained (high-retention wallets): **${migration['capital_retained']:,.0f}** ({migration['n_retained']} wallets)",
        f"- Net migration: **${migration['net_migration']:,.0f}**"
        + (" (improving)" if migration["net_migration"] > 0 else " (deteriorating)"),
        "",
        "---",
        "*Cost basis is approximate (average price, not per-lot). Treat this as directional, not accounting-grade.*",
    ]

    return "\n".join(lines)
