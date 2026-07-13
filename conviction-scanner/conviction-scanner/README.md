# Conviction Accumulation Scanner — Phase 1

Given a Solana token mint address, this produces an ownership report:
who's buying, whether they're holding, whether sellers are taking profit
or capitulating, and whether "ownership quality" is improving.

This is deliberately **Phase 1 only**: one token at a time, run on demand,
no database, no scanning of hundreds of tokens, no alerts. The point right
now is to find out whether this kind of analysis is actually predictive
before building automation on top of it.

## How it works

```
Helius (SWAP transactions for a mint)
        │
        ▼
   helius.py    → converts raw Helius events into list[Trade]
        │
        ▼
  analysis.py    → pandas: wallet_summary, seller_quality, ownership_migration
        │
        ▼
   report.py     → conviction_score() + render_report()
        │
        ▼
    app.py       → Streamlit UI
```

`models.py` defines the one shared `Trade` format. Nothing in
`analysis.py` or `report.py` knows anything about Helius — if you later
add a CSV importer or another API, it just needs its own adapter that
returns `list[Trade]`, and everything downstream is unchanged.

## Known limitations (by design, for now)

- **Cost basis is approximate**: average price (total USD in ÷ tokens
  bought), not a per-lot weighted ledger. Good enough to tell "up 15x"
  from "down 20%", not built for tax-grade accuracy.
- **Self-transfers between a user's own wallets** will look like a sell
  from one wallet and a zero-cost "buy" in another. Not handled yet —
  worth knowing when a report looks odd for a wallet cluster.
- **Only transactions Helius decodes as `SWAP`** are counted. Exotic
  routers or brand-new pools it doesn't parse will be silently skipped
  (you'll see fewer wallets than actually traded).
- **No caching or persistence.** Every run re-fetches from Helius. Fine
  for occasional analysis, not fine if you start running this
  continuously — that's Phase 2 (see prior design discussion).

## Local setup

```bash
git clone <your-repo-url>
cd conviction-scanner
python -m venv venv && source venv/bin/activate   # or venv\Scripts\activate on Windows
pip install -r requirements.txt

cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edit .streamlit/secrets.toml and paste in your Helius API key
# (free tier at https://helius.dev is enough to start)

streamlit run app.py
```

You can also skip secrets.toml entirely and just paste the API key into
the sidebar text field each session — the app supports both.

## Deploying: GitHub → Streamlit Community Cloud

1. Push this folder to a new GitHub repo (public or private — Streamlit
   Cloud's free tier works with both if you connect your GitHub account).

   ```bash
   git init
   git add .
   git commit -m "Phase 1: conviction accumulation scanner"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```

2. Go to [share.streamlit.io](https://share.streamlit.io), sign in with
   GitHub, click **New app**, and point it at this repo and `app.py`.

3. In the app's **Settings → Secrets**, paste:

   ```toml
   HELIUS_API_KEY = "your-real-key-here"
   ```

   This is the cloud equivalent of the local `secrets.toml` file — it
   pre-fills the sidebar field so you don't have to paste the key in
   every visit, but the field also still works if you leave it blank in
   Secrets and just type it in manually per session.

4. Deploy. First run will be slow (~30s+) if you set `max_transactions`
   high — that's expected; it's pulling and paginating full swap history
   for the token.

## Where this goes next (not built yet, on purpose)

- **Phase 2**: automate periodic ingestion for a watchlist instead of
  running on demand (add SQLite, a `transfers` table, incremental
  fetch by last-seen slot).
- **Phase 3**: scan hundreds of tokens with a cheap first-pass filter
  before running full wallet reconstruction on the survivors.

Don't build these until Phase 1's reports have actually been useful on
real tokens — that was the whole point of doing it this way.
