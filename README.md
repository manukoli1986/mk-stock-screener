# mk-stock-screener

NSE market screener + Warren Buffett-style long-term analysis. Data from Yahoo
Finance (trusted, delayed NSE quotes) via `yfinance`.

## Run

```bash
python3 -m venv .venv          # first time only
.venv/bin/pip install flask yfinance
.venv/bin/python app.py
```

Then open:

- **Screener** — http://127.0.0.1:5050/
- **Buffett Lens** — http://127.0.0.1:5050/buffett

## Pages

### Screener (`/`)

Scans all NIFTY 50 constituents plus NIFTY 50 / BANK NIFTY / SENSEX index tiles.

The stock list **auto-populates once per day** from NSE's official constituent
CSV (archives.nseindia.com, with niftyindices.com as fallback), so index
additions/removals appear automatically. The day's list is cached in
`universe_cache.json`; if NSE is unreachable it falls back to the last cached
list, then to a built-in list.

- **Patterns / signals**: Breakout (close above 20-day high on ≥1.5× average
  volume), Momentum (price > 20-DMA > 50-DMA with RSI ≥ 55), Overbought
  (RSI ≥ 70), Oversold (RSI ≤ 30), Downtrend.
- **Mapped entry/exit points**: entry ≈ 20-DMA pullback, stop = 10-day swing
  low, target = 52-week high (or 2×ATR above it once at the high).
- **Auto-refresh**: every 5 minutes (visible countdown), plus a manual
  "Refresh now" button that forces a fresh fetch.

### Intraday (`/intraday`)

Global-cues dashboard for stock/index derivative traders:

- **Index derivatives dashboard** — NIFTY & BANKNIFTY nearest-expiry option
  chain from NSE: PCR, max pain, support (highest put OI) and resistance
  (highest call OI), with a bullish/bearish/range bias.
- **Global cues → India impact** — US overnight close, live Asia, Europe,
  Brent, gold, copper, USD/INR, DXY, US 10Y — each with a one-line read for
  the Indian open. An "opening read" tile aggregates them into a
  gap-up / flat / gap-down call.
- **Expected uptrend / downtrend tables** — sectors where the triggered cues
  line up, with key F&O names and the drivers.
- **Session playbook** — IST timeline of what to check through the day.

## Data sources

- **NSE India** (live quotes, indices, option chains) via a managed client
  (`nse_client.py`): browser-session cookie warm-up, ≥1s spacing between
  requests, retry + session refresh on 401/403, per-endpoint caching, and
  stale-cache fallback — polite to NSE's bot protection and rate limits.
- **Yahoo Finance** (`yfinance`) for daily history (technicals), global
  indices/commodities/FX, and fundamentals. The slow 1-year history batch is
  cached 30 min; live prices come from NSE, so page refreshes are fast.

### Buffett Lens (`/buffett`)

Scores 14 moat businesses on six Buffett-style checks: ROE ≥ 15%,
debt/equity < 0.6, net margin ≥ 10%, growing earnings, positive free cash
flow, P/E < 35. Score 5–6 = strong candidate, 4 = watchlist. Each card carries
a one-line "why this business" moat note plus the live numbers.

## Notes

- Server caches: screener 5 min, fundamentals 30 min (kind to Yahoo's limits).
- Dark mode follows the OS setting.
- Educational tool only — not investment advice.
