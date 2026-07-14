import streamlit as st

from helius import fetch_trades, HeliusError
from analysis import trades_to_df, wallet_summary, seller_quality, ownership_migration, summary_stats
from report import conviction_score, render_report

st.set_page_config(page_title="Conviction Accumulation Scanner", layout="wide")
st.title("Conviction Accumulation Scanner")
st.caption("Phase 1: single-token ownership analysis. Not a live scanner (yet).")

with st.sidebar:
    st.header("Settings")
    api_key = st.text_input(
        "Helius API key",
        type="password",
        value=st.secrets.get("HELIUS_API_KEY", "") if hasattr(st, "secrets") else "",
        help="Get one free at helius.dev. Stored only for this session.",
    )
    max_tx = st.slider("Max transactions to pull", 100, 3000, 1000, step=100)
    st.markdown("---")
    st.markdown(
        "Cost basis is approximate (avg price, not per-lot). "
        "See the project README for what this tool does and doesn't do."
    )

token_mint = st.text_input("Token mint address", placeholder="e.g. DezXAZ8z7...")
run = st.button("Run analysis", type="primary")

if run:
    if not api_key:
        st.error("Add a Helius API key in the sidebar first.")
        st.stop()
    if not token_mint:
        st.error("Enter a token mint address.")
        st.stop()

    progress = st.progress(0, text="Fetching swaps from Helius...")

    def _progress(n):
        pct = min(n / max_tx, 1.0)
        progress.progress(pct, text=f"Fetched {n} transactions...")

    fetch_stats = {}

    try:
        trades = fetch_trades(
            token_mint.strip(),
            api_key.strip(),
            max_transactions=max_tx,
            progress_callback=_progress,
            stats=fetch_stats,
        )
    except HeliusError as e:
        progress.empty()
        st.error(f"Helius error: {e}")
        st.stop()
    except Exception as e:
        progress.empty()
        import traceback
        st.error("Unexpected error fetching data:")
        st.code(traceback.format_exc())
        st.stop()

    progress.empty()

    with st.expander("Fetch diagnostics", expanded=not trades):
        st.json(fetch_stats)
        if fetch_stats.get("signatures_found", 0) >= max_tx:
            st.caption(
                f"Hit the {max_tx}-transaction cap -- there may be older "
                "trades not included. Raise 'Max transactions to pull' in "
                "the sidebar and rerun to widen the window."
            )

    if not trades:
        st.warning(
            "No decodable swap transactions found for this token. "
            "Check the diagnostics above for why -- it'll show how many "
            "signatures were found and why each one was skipped."
        )
        st.stop()

    df = trades_to_df(trades)
    ws = wallet_summary(df)
    sq = seller_quality(ws)
    migration = ownership_migration(ws)
    stats = summary_stats(ws, sq)
    score = conviction_score(ws)

    st.markdown(render_report(token_mint.strip(), stats, migration, score))

    st.markdown("---")
    st.subheader("Wallet-level data")
    tab1, tab2 = st.tabs(["All buyers", "Sellers"])

    with tab1:
        st.dataframe(
            ws[[
                "wallet", "bought", "sold", "current_balance", "retention_pct",
                "usd_invested", "buy_count", "days_held",
            ]].round(2),
            use_container_width=True,
        )

    with tab2:
        if sq.empty:
            st.write("No sell activity found.")
        else:
            st.dataframe(
                sq[[
                    "wallet", "sold", "usd_realized", "avg_entry_price",
                    "avg_exit_price", "realized_roi_x",
                ]].round(4),
                use_container_width=True,
            )

    st.download_button(
        "Download wallet data as CSV",
        ws.to_csv(index=False),
        file_name=f"{token_mint.strip()}_wallets.csv",
        mime="text/csv",
    )
else:
    st.info("Enter a Helius API key and a token mint address, then click **Run analysis**.")
