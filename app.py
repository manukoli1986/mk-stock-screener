"""mk-stock-screener — NSE market screener + Buffett-lens long-term analysis.

Data source: Yahoo Finance (via yfinance). Educational use only, not investment advice.
"""

import csv
import io
import json
import os
import threading
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from flask import Flask, render_template, request

from nse_client import nse

app = Flask(__name__)

IST = ZoneInfo("Asia/Kolkata")

SCREENER_TTL = 300   # 5 minutes, matches the page auto-refresh
HISTORY_TTL = 1800   # daily-bar history for technicals; the slow yfinance batch
FUNDAMENTALS_TTL = 1800  # fundamentals move slowly; refetch every 30 minutes
INTRADAY_TTL = 300

NSE_LIVE_QUOTES = "/api/heatmap-symbols?type=Broad%20Market%20Indices&indices=NIFTY%2050"
NSE_ALL_INDICES = "/api/allIndices"

INDICES = {
    "^NSEI": "NIFTY 50",
    "^NSEBANK": "BANK NIFTY",
    "^BSESN": "SENSEX",
}

# Screener universe auto-populates once per day from NSE's official NIFTY 50
# constituent list, so index additions/removals show up without code changes.
UNIVERSE_SOURCES = [
    "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv",
]
UNIVERSE_CACHE_FILE = os.path.join(os.path.dirname(__file__), "universe_cache.json")

# Used only if both NSE sources and the disk cache are unavailable.
FALLBACK_UNIVERSE = [
    "RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS", "TCS.NS",
    "SBIN.NS", "AXISBANK.NS", "KOTAKBANK.NS", "LT.NS", "ITC.NS",
    "HINDUNILVR.NS", "BHARTIARTL.NS", "BAJFINANCE.NS", "ASIANPAINT.NS",
    "MARUTI.NS", "TITAN.NS", "SUNPHARMA.NS", "NTPC.NS", "POWERGRID.NS",
    "TATAMOTORS.NS", "TATASTEEL.NS", "JSWSTEEL.NS", "ADANIENT.NS",
    "ULTRACEMCO.NS", "WIPRO.NS", "HCLTECH.NS", "TECHM.NS", "NESTLEIND.NS",
    "PIDILITIND.NS", "DMART.NS",
]


def _fetch_universe():
    for url in UNIVERSE_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read().decode("utf-8-sig")
            symbols = [
                row["Symbol"].strip() + ".NS"
                for row in csv.DictReader(io.StringIO(text))
                if row.get("Symbol", "").strip()
            ]
            # A truncated/garbled response must not shrink the scan silently.
            if len(symbols) >= 40:
                return symbols
        except Exception:
            continue
    return None


def get_universe():
    """NIFTY 50 members, refreshed once per IST calendar day, cached on disk."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    cached = None
    try:
        with open(UNIVERSE_CACHE_FILE) as f:
            cached = json.load(f)
        if cached.get("date") == today:
            return cached["symbols"], f"NSE NIFTY 50 ({len(cached['symbols'])} stocks, {cached['date']})"
    except (OSError, ValueError, KeyError):
        cached = None

    symbols = _fetch_universe()
    if symbols:
        with open(UNIVERSE_CACHE_FILE, "w") as f:
            json.dump({"date": today, "symbols": symbols}, f)
        return symbols, f"NSE NIFTY 50 ({len(symbols)} stocks, {today})"
    if cached and cached.get("symbols"):
        return cached["symbols"], f"NSE NIFTY 50 (cached {cached['date']})"
    return FALLBACK_UNIVERSE, f"built-in list ({len(FALLBACK_UNIVERSE)} stocks)"

# Buffett-lens universe: businesses with a plausible durable moat.
# The qualitative note is the "why this business" half; live metrics are the other half.
BUFFETT_UNIVERSE = {
    "HDFCBANK.NS": "India's largest private bank; low-cost deposit franchise and two decades of compounding book value.",
    "ICICIBANK.NS": "Retail-led private bank with best-in-class digital adoption and improving return ratios.",
    "TCS.NS": "Sticky multi-year IT contracts, negligible debt, and industry-leading cash conversion.",
    "INFY.NS": "Global IT franchise with high repeat business and a long dividend + buyback record.",
    "HINDUNILVR.NS": "Distribution moat across 9M+ retail outlets; owns brands Indians buy weekly regardless of cycle.",
    "NESTLEIND.NS": "Maggi/Nescafe brand monopoly-like shelf power; decades of pricing power in food.",
    "ITC.NS": "Cigarette cash cow funding FMCG brands; enormous free cash flow and dividend yield.",
    "ASIANPAINT.NS": "Half the Indian paint market; dealer network and supply chain rivals have failed to copy for 50 years.",
    "PIDILITIND.NS": "Fevicol is a verb in India — adhesive brand moat with ~70% category share.",
    "TITAN.NS": "Tanishq trust premium in jewellery, a category where trust IS the moat.",
    "BAJFINANCE.NS": "Consumer-lending data moat and cross-sell engine; long runway in under-penetrated credit.",
    "DMART.NS": "Everyday-low-price retail with owned stores; cost discipline compounding like early Walmart.",
    "MARUTI.NS": "Half of India's car market, unmatched service network in small towns.",
    "LT.NS": "India's infrastructure proxy; engineering execution record no private rival matches.",
}

_cache_lock = threading.Lock()
_cache = {}  # key -> {"data": ..., "ts": float}


def _cached(key, ttl, builder, force=False):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and not force and time.time() - entry["ts"] < ttl:
            return entry["data"], entry["ts"]
    data = builder()
    with _cache_lock:
        _cache[key] = {"data": data, "ts": time.time()}
    return data, _cache[key]["ts"]


def _rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def _atr(df, period=14):
    high, low, close = df["High"], df["Low"], df["Close"]
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def build_history():
    """The slow yfinance batch (1y daily bars). Cached 30 min — daily bars
    barely change intraday, so page refreshes don't pay for this."""
    universe, universe_label = get_universe()
    tickers = universe + list(INDICES)
    raw = yf.download(
        tickers, period="1y", interval="1d",
        group_by="ticker", auto_adjust=True, progress=False, threads=True,
    )
    return {"universe": universe, "label": universe_label, "raw": raw}


def build_screener(force=False):
    hist, _ = _cached("history", HISTORY_TTL, build_history)
    universe, universe_label, raw = hist["universe"], hist["label"], hist["raw"]

    # Live overlay from NSE (fast, one call for all 50). Falls back to the
    # last daily close from Yahoo history if NSE is unreachable.
    live_rows = nse.get(NSE_LIVE_QUOTES, ttl=30 if force else 240)
    live = {}
    if live_rows:
        for q in live_rows:
            try:
                live[q["symbol"]] = {
                    "price": float(q["lastPrice"]),
                    "change_pct": float(q["pChange"]),
                    "volume": float(q["totalTradedVolume"]),
                }
            except (KeyError, ValueError):
                continue
    source = "NSE live quotes + Yahoo history" if live else "Yahoo history (NSE unreachable)"

    indices = []
    idx_data = nse.get(NSE_ALL_INDICES, ttl=30 if force else 240)
    if idx_data:
        want = {"NIFTY 50": "NIFTY 50", "NIFTY BANK": "BANK NIFTY", "INDIA VIX": "INDIA VIX"}
        for row in idx_data.get("data", []):
            if row.get("index") in want:
                indices.append({
                    "name": want[row["index"]],
                    "level": row["last"],
                    "change_pct": row["percentChange"],
                })
    if not indices:
        for symbol, name in INDICES.items():
            try:
                df = raw[symbol].dropna()
                last, prev = df["Close"].iloc[-1], df["Close"].iloc[-2]
                indices.append({
                    "name": name,
                    "level": last,
                    "change_pct": (last / prev - 1) * 100,
                })
            except (KeyError, IndexError):
                continue

    rows = []
    for symbol in universe:
        try:
            df = raw[symbol].dropna()
            if len(df) < 60:
                continue
            close = df["Close"]
            last, prev = close.iloc[-1], close.iloc[-2]
            change_pct = (last / prev - 1) * 100
            sma20 = close.rolling(20).mean().iloc[-1]
            sma50 = close.rolling(50).mean().iloc[-1]
            rsi = _rsi(close).iloc[-1]
            atr = _atr(df).iloc[-1]
            vol = df["Volume"].iloc[-1]
            high_52w = df["High"].max()
            high_20d = df["High"].iloc[-21:-1].max()
            swing_low = df["Low"].iloc[-10:].min()

            lq = live.get(symbol.replace(".NS", ""))
            if lq:
                last = lq["price"]
                change_pct = lq["change_pct"]
                vol = lq["volume"]
            vol_avg = df["Volume"].rolling(20).mean().iloc[-1]
            vol_ratio = vol / vol_avg if vol_avg else 0
            high_52w = max(high_52w, last)
            off_high_pct = (last / high_52w - 1) * 100

            if last > high_20d and vol_ratio >= 1.5:
                signal, signal_kind = "Breakout", "good"
            elif rsi >= 70:
                signal, signal_kind = "Overbought", "serious"
            elif rsi <= 30:
                signal, signal_kind = "Oversold", "warning"
            elif last > sma20 > sma50 and rsi >= 55:
                signal, signal_kind = "Momentum", "good"
            elif last < sma20 < sma50:
                signal, signal_kind = "Downtrend", "critical"
            else:
                signal, signal_kind = "Neutral", "neutral"

            # Mapped levels: pullback entry near 20-DMA, stop under recent swing
            # low, target at the 52-week high (or 2*ATR above if already there).
            target = high_52w if last < high_52w * 0.995 else last + 2 * atr
            rows.append({
                "symbol": symbol.replace(".NS", ""),
                "price": last,
                "change_pct": change_pct,
                "rsi": rsi,
                "vol_ratio": vol_ratio,
                "off_high_pct": off_high_pct,
                "signal": signal,
                "signal_kind": signal_kind,
                "entry": sma20,
                "stop": swing_low,
                "target": target,
            })
        except (KeyError, IndexError):
            continue

    signal_rank = {"Breakout": 0, "Momentum": 1, "Oversold": 2,
                   "Overbought": 3, "Neutral": 4, "Downtrend": 5}
    rows.sort(key=lambda r: (signal_rank[r["signal"]], -r["vol_ratio"]))
    return {"indices": indices, "rows": rows, "universe_label": universe_label,
            "source": source}


def build_buffett():
    picks = []
    for symbol, moat in BUFFETT_UNIVERSE.items():
        try:
            info = yf.Ticker(symbol).info
        except Exception:
            continue
        roe = info.get("returnOnEquity")
        de = info.get("debtToEquity")  # yfinance reports this as a percentage
        margin = info.get("profitMargins")
        growth = info.get("earningsGrowth")
        fcf = info.get("freeCashflow")
        pe = info.get("trailingPE")
        price = info.get("currentPrice")

        checks = [
            ("ROE ≥ 15%", roe is not None and roe >= 0.15,
             f"{roe:.0%}" if roe is not None else "n/a"),
            ("Debt/Equity < 0.6", de is not None and de < 60,
             f"{de / 100:.2f}" if de is not None else "n/a"),
            ("Net margin ≥ 10%", margin is not None and margin >= 0.10,
             f"{margin:.0%}" if margin is not None else "n/a"),
            ("Earnings growing", growth is not None and growth > 0,
             f"{growth:+.0%}" if growth is not None else "n/a"),
            ("Positive free cash flow", fcf is not None and fcf > 0,
             "yes" if fcf and fcf > 0 else "no" if fcf is not None else "n/a"),
            ("P/E < 35", pe is not None and pe < 35,
             f"{pe:.1f}" if pe is not None else "n/a"),
        ]
        score = sum(1 for _, ok, _ in checks if ok)
        if score >= 5:
            verdict, verdict_kind = "Strong candidate", "good"
        elif score == 4:
            verdict, verdict_kind = "Watchlist", "warning"
        else:
            verdict, verdict_kind = "Pass for now", "neutral"

        picks.append({
            "symbol": symbol.replace(".NS", ""),
            "name": info.get("shortName", symbol),
            "price": price,
            "pe": pe,
            "score": score,
            "verdict": verdict,
            "verdict_kind": verdict_kind,
            "moat": moat,
            "checks": checks,
        })
    picks.sort(key=lambda p: -p["score"])
    return picks


# ---------------------------------------------------------------------------
# Intraday: global cues -> India impact, plus index option-chain levels
# ---------------------------------------------------------------------------

# ticker -> (display name, role, group). Role drives the impact rules.
GLOBAL_TICKERS = {
    "^GSPC": ("S&P 500", "us", "United States (overnight close)"),
    "^IXIC": ("Nasdaq", "us_tech", "United States (overnight close)"),
    "^DJI": ("Dow Jones", "us", "United States (overnight close)"),
    "^VIX": ("CBOE VIX", "vix", "United States (overnight close)"),
    "^N225": ("Nikkei 225", "asia", "Asia (live in IST morning)"),
    "^KS11": ("KOSPI", "asia", "Asia (live in IST morning)"),
    "^HSI": ("Hang Seng", "china", "Asia (live in IST morning)"),
    "000001.SS": ("Shanghai Composite", "china", "Asia (live in IST morning)"),
    "^FTSE": ("FTSE 100", "europe", "Europe (previous session)"),
    "^GDAXI": ("DAX", "europe", "Europe (previous session)"),
    "BZ=F": ("Brent Crude", "crude", "Commodities"),
    "GC=F": ("Gold", "gold", "Commodities"),
    "HG=F": ("Copper", "metals", "Commodities"),
    "INR=X": ("USD/INR", "usdinr", "Currency & rates"),
    "DX-Y.NYB": ("Dollar Index (DXY)", "dxy", "Currency & rates"),
    "^TNX": ("US 10Y yield", "us10y", "Currency & rates"),
}

# What a move in each cue means for the Indian open (shown in the cues table).
IMPACT_TEXT = {
    "us": ("Positive Wall Street handoff — supports a gap-up in NIFTY futures",
           "Weak Wall Street close — gap-down risk for NIFTY futures"),
    "us_tech": ("Nasdaq strength lifts IT — TCS, INFY, HCLTECH longs favoured",
                "Nasdaq weakness drags IT — avoid fresh IT longs at open"),
    "vix": ("US fear gauge rising — risk-off, keep overnight longs light",
            "US volatility easing — supportive for risk appetite"),
    "asia": ("Supportive Asian tape into our open",
             "Weak Asian tape — cautious opening tone"),
    "china": ("China risk-on — supports metals (TATASTEEL, JSWSTEEL, HINDALCO)",
              "China weakness — headwind for metal names"),
    "europe": ("Positive European close — mild sentiment support",
               "Weak European close — mild sentiment drag"),
    "crude": ("Brent up — ONGC gains; OMCs, aviation, paints under pressure",
              "Brent down — OMCs, aviation, paints breathe; ONGC soft"),
    "gold": ("Gold bid — risk-off undertone; jewellery input costs rise",
             "Gold easing — mild risk-on; helps jewellery demand"),
    "metals": ("Copper up — global demand signal, backs metal longs",
               "Copper down — demand worry, metals heavy"),
    "usdinr": ("Rupee weaker — IT/pharma exporters gain; importers hurt",
               "Rupee stronger — trims exporter margins; helps OMCs"),
    "dxy": ("Dollar strength — FII outflow pressure on EM including India",
            "Dollar softening — supportive for FII flows into India"),
    "us10y": ("US yields rising — FII outflow risk, financials heavy",
              "US yields easing — supportive for financials and FII flows"),
}

# Minimum % move for a cue to count as directional (else "flat").
ROLE_THRESHOLD = {"vix": 3.0, "us10y": 1.0, "gold": 0.5, "crude": 0.5,
                  "usdinr": 0.15, "dxy": 0.25}
DEFAULT_THRESHOLD = 0.25

# (role, cue direction, sector, effect on sector, reason)
SECTOR_RULES = [
    ("us", +1, "Index heavyweights", +1, "positive Wall Street close"),
    ("us", -1, "Index heavyweights", -1, "weak Wall Street close"),
    ("asia", +1, "Index heavyweights", +1, "supportive Asian tape"),
    ("asia", -1, "Index heavyweights", -1, "weak Asian tape"),
    ("vix", +1, "Index heavyweights", -1, "US VIX spiking (risk-off)"),
    ("vix", -1, "Index heavyweights", +1, "volatility cooling"),
    ("us_tech", +1, "IT", +1, "Nasdaq strength"),
    ("us_tech", -1, "IT", -1, "Nasdaq weakness"),
    ("usdinr", +1, "IT", +1, "weaker rupee lifts export realisations"),
    ("usdinr", -1, "IT", -1, "stronger rupee trims export margins"),
    ("usdinr", +1, "Pharma", +1, "weaker rupee lifts export realisations"),
    ("usdinr", -1, "Pharma", -1, "stronger rupee trims export margins"),
    ("us10y", +1, "Banks / Financials", -1, "rising US yields, FII outflow risk"),
    ("us10y", -1, "Banks / Financials", +1, "US yields easing"),
    ("dxy", +1, "Banks / Financials", -1, "strong dollar pressures FII flows"),
    ("dxy", -1, "Banks / Financials", +1, "softer dollar supports FII flows"),
    ("crude", +1, "Oil & gas producers", +1, "Brent rising lifts realisations"),
    ("crude", -1, "Oil & gas producers", -1, "Brent slipping"),
    ("crude", +1, "OMCs / Aviation / Paints", -1, "crude is a cost headwind"),
    ("crude", -1, "OMCs / Aviation / Paints", +1, "softer crude eases margins"),
    ("metals", +1, "Metals", +1, "copper strength, global demand signal"),
    ("metals", -1, "Metals", -1, "copper weakness"),
    ("china", +1, "Metals", +1, "China risk-on supports metal prices"),
    ("china", -1, "Metals", -1, "China weakness hits metal demand"),
    ("gold", +1, "Jewellery / Consumer", -1, "gold spike raises input costs"),
    ("gold", -1, "Jewellery / Consumer", +1, "gold easing helps jewellery demand"),
]

SECTOR_STOCKS = {
    "Index heavyweights": "NIFTY / BANKNIFTY futures · RELIANCE, HDFCBANK, ICICIBANK",
    "IT": "TCS, INFY, HCLTECH, TECHM, WIPRO",
    "Banks / Financials": "HDFCBANK, ICICIBANK, SBIN, AXISBANK, BAJFINANCE",
    "Pharma": "SUNPHARMA, CIPLA, DRREDDY",
    "Metals": "TATASTEEL, JSWSTEEL, HINDALCO, VEDL",
    "Oil & gas producers": "ONGC, OIL, RELIANCE",
    "OMCs / Aviation / Paints": "BPCL, HPCL, IOC, INDIGO, ASIANPAINT",
    "Jewellery / Consumer": "TITAN, KALYANKJIL",
}


def _option_levels(symbol):
    """PCR, max-pain and OI-based support/resistance for the nearest expiry."""
    info = nse.get(f"/api/option-chain-contract-info?symbol={symbol}", ttl=3600)
    if not info or not info.get("expiryDates"):
        return None
    expiry = info["expiryDates"][0]
    oc = nse.get(
        f"/api/option-chain-v3?type=Indices&symbol={symbol}&expiry={expiry}",
        ttl=180,
    )
    if not oc or "records" not in oc:
        return None
    records = oc["records"]
    spot = records.get("underlyingValue")
    ce_oi, pe_oi = {}, {}
    for row in records.get("data", []):
        k = row.get("strikePrice")
        if k is None:
            continue
        if row.get("CE"):
            ce_oi[k] = ce_oi.get(k, 0) + (row["CE"].get("openInterest") or 0)
        if row.get("PE"):
            pe_oi[k] = pe_oi.get(k, 0) + (row["PE"].get("openInterest") or 0)
    if not ce_oi or not pe_oi:
        return None

    total_ce, total_pe = sum(ce_oi.values()), sum(pe_oi.values())
    pcr = total_pe / total_ce if total_ce else 0
    resistance = max(ce_oi, key=ce_oi.get)
    support = max(pe_oi, key=pe_oi.get)
    strikes = sorted(set(ce_oi) | set(pe_oi))
    max_pain = min(
        strikes,
        key=lambda s: sum(oi * max(0, s - k) for k, oi in ce_oi.items())
        + sum(oi * max(0, k - s) for k, oi in pe_oi.items()),
    )
    if pcr >= 1.2:
        bias, bias_kind = "Bullish (put writing)", "good"
    elif pcr <= 0.8:
        bias, bias_kind = "Bearish (call writing)", "critical"
    else:
        bias, bias_kind = "Neutral / range", "neutral"
    return {
        "symbol": symbol, "expiry": expiry, "spot": spot, "pcr": pcr,
        "support": support, "resistance": resistance, "max_pain": max_pain,
        "bias": bias, "bias_kind": bias_kind,
    }


def build_intraday():
    raw = yf.download(
        list(GLOBAL_TICKERS), period="5d", interval="1d",
        group_by="ticker", auto_adjust=True, progress=False, threads=True,
    )

    cues, role_moves = [], {}
    for ticker, (name, role, group) in GLOBAL_TICKERS.items():
        try:
            close = raw[ticker]["Close"].dropna()
            last, prev = close.iloc[-1], close.iloc[-2]
        except (KeyError, IndexError):
            continue
        chg = (last / prev - 1) * 100
        threshold = ROLE_THRESHOLD.get(role, DEFAULT_THRESHOLD)
        direction = 1 if chg >= threshold else -1 if chg <= -threshold else 0
        up_text, down_text = IMPACT_TEXT[role]
        impact = up_text if direction > 0 else down_text if direction < 0 else \
            "Flat — no directional signal from this cue"
        cues.append({
            "name": name, "group": group, "last": last, "change_pct": chg,
            "direction": direction, "impact": impact,
            "fmt": "pct" if ticker == "^TNX" else "num",
        })
        role_moves.setdefault(role, []).append((chg, direction, name))

    # Sector engine: every triggered rule adds/removes a point for its sector.
    sectors = {}
    for role, cue_dir, sector, effect, reason in SECTOR_RULES:
        moves = role_moves.get(role, [])
        hits = [(chg, name) for chg, d, name in moves if d == cue_dir]
        if not hits:
            continue
        entry = sectors.setdefault(sector, {"score": 0, "reasons": []})
        entry["score"] += effect * len(hits)
        names = ", ".join(f"{n} {c:+.1f}%" for c, n in hits)
        entry["reasons"].append(f"{reason} ({names})")

    def _table(direction):
        out = []
        for sector, e in sectors.items():
            if direction * e["score"] > 0:
                out.append({
                    "sector": sector,
                    "stocks": SECTOR_STOCKS.get(sector, ""),
                    "drivers": "; ".join(e["reasons"]),
                    "score": abs(e["score"]),
                })
        out.sort(key=lambda r: -r["score"])
        return out

    # Gap read for the index open: US handoff + live Asia + China tone.
    def _role_avg(role):
        moves = role_moves.get(role, [])
        return sum(c for c, _, _ in moves) / len(moves) if moves else 0.0

    gap_score = 0.5 * _role_avg("us") + 0.3 * _role_avg("asia") + 0.2 * _role_avg("china")
    if gap_score >= 0.3:
        gap = ("Cues point to a gap-up / positive open", "good")
    elif gap_score <= -0.3:
        gap = ("Cues point to a gap-down / weak open", "critical")
    else:
        gap = ("Cues are mixed — expect a flat, range-bound open", "neutral")

    indices = []
    idx_data = nse.get(NSE_ALL_INDICES, ttl=240)
    if idx_data:
        want = {"NIFTY 50": "NIFTY 50", "NIFTY BANK": "BANK NIFTY", "INDIA VIX": "INDIA VIX"}
        for row in idx_data.get("data", []):
            if row.get("index") in want:
                indices.append({
                    "name": want[row["index"]],
                    "level": row["last"],
                    "change_pct": row["percentChange"],
                })

    derivatives = [lv for s in ("NIFTY", "BANKNIFTY")
                   if (lv := _option_levels(s))]

    return {
        "cues": cues, "uptrend": _table(+1), "downtrend": _table(-1),
        "gap_text": gap[0], "gap_kind": gap[1],
        "indices": indices, "derivatives": derivatives,
    }


def _stamp(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%d %b %Y, %H:%M:%S IST")


def _market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 30


@app.route("/")
def screener():
    force = request.args.get("force") == "1"
    data, ts = _cached("screener", SCREENER_TTL,
                       lambda: build_screener(force=force), force=force)
    return render_template(
        "screener.html", page="screener", updated=_stamp(ts),
        market_open=_market_open(), **data,
    )


@app.route("/intraday")
def intraday():
    force = request.args.get("force") == "1"
    data, ts = _cached("intraday", INTRADAY_TTL, build_intraday, force=force)
    return render_template(
        "intraday.html", page="intraday", updated=_stamp(ts),
        market_open=_market_open(), **data,
    )


@app.route("/buffett")
def buffett():
    force = request.args.get("force") == "1"
    picks, ts = _cached("buffett", FUNDAMENTALS_TTL, build_buffett, force=force)
    return render_template(
        "buffett.html", page="buffett", updated=_stamp(ts),
        market_open=_market_open(), picks=picks,
    )


if __name__ == "__main__":
    # Local dev server. In production a WSGI server runs `app` directly
    # (see Procfile: gunicorn app:app). PORT is injected by most cloud hosts.
    port = int(os.environ.get("PORT", 5050))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    app.run(host=host, port=port, debug=False)
