"""
portfolio.py — local, private portfolio correlation & risk analysis.

A standalone Streamlit app (run: ``streamlit run portfolio.py``) that reads a
Portfolio Performance "Statement of Assets" CSV export from the gitignored
``portfolio_data/`` folder and computes, for your *actual* holdings weighted by
current market value:

  #1  a single weighted-average correlation (diversification score), and
  #2  the portfolio volatility the covariance matrix implies, plus how much
      diversification reduces risk versus holding everything in lockstep.

Optionally reads ``portfolio_data/ticker_map.csv`` to map each Portfolio
Performance ticker to a better Yahoo ticker (e.g. a US primary listing with
longer history) and its currency. Foreign-currency price series are converted
to the portfolio base currency (EUR) before returns are computed, so
correlations and volatility are measured from the EUR investor's perspective.

Your portfolio data never enters the MotherDuck database and never leaves this
machine — it lives only in ``portfolio_data/`` (gitignored). Price history and
FX rates are downloaded from Yahoo Finance via the shared helpers in core.py.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from core import (
    BASE_CURRENCY,
    _CORR_WINDOWS,
    _CORR_FREQ,
    _fetch_perf_history,
    compute_returns,
    convert_to_base,
    fx_tickers_for,
    weighted_avg_correlation,
    portfolio_risk,
)

st.set_page_config(page_title="Portfolio Correlation", layout="wide")

DATA_DIR = Path(__file__).parent / "portfolio_data"
MAP_FILE = DATA_DIR / "ticker_map.csv"

# Exact column headers in a Portfolio Performance "Vermögensaufstellung" export.
COL_NAME = "Name"
COL_SYMBOL = "Symbol"
COL_VALUE = "Marktwert"


def _table_height(n_rows: int) -> int:
    """Pixel height that shows all rows of an st.dataframe without scrolling
    (≈35px per row + header)."""
    return (int(n_rows) + 1) * 35 + 3


def _corr_bg(v) -> str:
    """Red→Yellow→Green background for a correlation cell, without matplotlib.
    +1 → red (little diversification), 0 → yellow, −1 → green."""
    if pd.isna(v):
        return ""
    x = max(-1.0, min(1.0, float(v)))
    yellow, red, green = (255, 255, 191), (215, 48, 39), (26, 152, 80)
    if x >= 0:
        t, end = x, red
    else:
        t, end = -x, green
    r = int(yellow[0] + (end[0] - yellow[0]) * t)
    g = int(yellow[1] + (end[1] - yellow[1]) * t)
    b = int(yellow[2] + (end[2] - yellow[2]) * t)
    return f"background-color: rgb({r},{g},{b}); color: black"


# ── Load & parse the Portfolio Performance CSV ───────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def load_portfolio(path: str) -> pd.DataFrame:
    """Parse a PP 'Statement of Assets' CSV (German number format) into a tidy
    table of real positions: Name, Symbol, Marktwert (EUR), Weight.

    PP exports group/subtotal rows (Core, DivWachstum, …) and a grand-total
    'Summe' row — all of which have an empty Symbol, so we keep only rows that
    carry a ticker. German numbers use '.' as thousands and ',' as decimal.
    """
    df = pd.read_csv(
        path,
        sep=";",
        decimal=",",
        thousands=".",
        encoding="utf-8-sig",
        dtype={COL_SYMBOL: "string", COL_NAME: "string"},
    )

    missing = {COL_NAME, COL_SYMBOL, COL_VALUE} - set(df.columns)
    if missing:
        raise ValueError(
            f"CSV is missing expected column(s): {', '.join(sorted(missing))}. "
            f"Found: {', '.join(df.columns)}"
        )

    # Keep only real positions (group headers & the 'Summe' total have no Symbol).
    df = df[df[COL_SYMBOL].notna() & (df[COL_SYMBOL].str.strip() != "")].copy()
    df[COL_SYMBOL] = df[COL_SYMBOL].str.strip()
    df[COL_VALUE] = pd.to_numeric(df[COL_VALUE], errors="coerce")
    df = df.dropna(subset=[COL_VALUE])
    df = df[df[COL_VALUE] > 0]

    # Collapse any duplicate tickers (same security held in two sleeves).
    g = df.groupby(COL_SYMBOL, as_index=False).agg(
        Name=(COL_NAME, "first"),
        Marktwert=(COL_VALUE, "sum"),
    )
    total = g["Marktwert"].sum()
    g["Weight"] = g["Marktwert"] / total if total > 0 else 0.0
    return g.sort_values("Marktwert", ascending=False).reset_index(drop=True)


@st.cache_data(ttl=300, show_spinner=False)
def load_mapping(path: str) -> pd.DataFrame:
    """Load the PP→Yahoo ticker map: columns pp_symbol, yahoo_ticker, currency.

    Rows with a blank yahoo_ticker fall back to the PP symbol; a blank currency
    falls back to the base currency. Returns an empty frame if the file is absent.
    """
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=["pp_symbol", "yahoo_ticker", "currency"])
    m = pd.read_csv(p, dtype=str, encoding="utf-8-sig").fillna("")
    m.columns = [c.strip().lower() for c in m.columns]
    for col in ("pp_symbol", "yahoo_ticker", "currency"):
        if col not in m.columns:
            m[col] = ""
    for col in ("pp_symbol", "yahoo_ticker", "currency"):
        m[col] = m[col].astype(str).str.strip()
    m = m[m["pp_symbol"] != ""]
    return m[["pp_symbol", "yahoo_ticker", "currency"]]


def apply_mapping(holdings: pd.DataFrame, mapping: pd.DataFrame) -> pd.DataFrame:
    """Attach a Yahoo ticker and currency to each PP position."""
    h = holdings.copy()
    lut = {r.pp_symbol: (r.yahoo_ticker, r.currency) for r in mapping.itertuples()}
    h["Yahoo"] = h["Symbol"].map(lambda s: (lut.get(s, ("", ""))[0] or s))
    h["Currency"] = h["Symbol"].map(
        lambda s: (lut.get(s, ("", ""))[1] or BASE_CURRENCY).upper()
    )
    return h


def _find_csvs() -> list[Path]:
    if not DATA_DIR.exists():
        return []
    return sorted(
        [p for p in DATA_DIR.glob("*.csv") if p.name != MAP_FILE.name],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


# ── Page ─────────────────────────────────────────────────────────────────────
st.title("📁 Portfolio Correlation & Risk")
st.caption(
    "How diversified is your actual portfolio? Positions are weighted by current "
    "market value — a big holding counts more than a tiny one. Data stays local."
)

with st.expander("ℹ️ How this is calculated"):
    st.markdown(
        f"""
**Source.** Your holdings come from a Portfolio Performance CSV export in the
local `portfolio_data/` folder (gitignored — never committed, never uploaded to
the database). Each position's **market value** sets its **weight**. Market
value and weight come straight from Portfolio Performance in **{BASE_CURRENCY}**
and need no conversion.

**Ticker mapping (optional).** If `portfolio_data/ticker_map.csv` exists, each
Portfolio Performance ticker is mapped to the Yahoo ticker you specify (e.g. a US
primary listing with longer, cleaner history) plus its **currency**.

**Currency.** Price history is fetched in each Yahoo ticker's own currency, then
**converted to {BASE_CURRENCY}** using Yahoo FX rates before returns are computed
— so correlation and volatility reflect what you, a {BASE_CURRENCY} investor,
actually experience (including the FX component of foreign holdings). Positions
already in {BASE_CURRENCY} are untouched.

**Returns, not prices.** As in the watchlist Correlation tab, we correlate
periodic **returns** (not price levels), so a shared upward trend doesn't
inflate the numbers. Pick the frequency and lookback window below.

**#1 — Weighted-average correlation (diversification score).**
The average of every pairwise correlation, each pair weighted by the product of
the two positions' weights (wᵢ·wⱼ). One number: `+1` ≈ everything moves as one,
`0` ≈ largely independent, negative ≈ genuine hedging. Big positions dominate.

**#2 — Portfolio volatility & diversification ratio.**
Using the full covariance matrix, the portfolio's annualized volatility
`√(wᵀΣw)` is compared to the **weighted-average of the individual volatilities**
(what you'd have if every holding moved in lockstep). The **diversification
ratio** is the second ÷ the first: `1.0` = no benefit, higher is better.

> Positions whose ticker has too little overlapping price history (or no FX rate)
> in the chosen window are dropped and listed; the remaining weights are
> renormalized, and the **% of portfolio value covered** is reported.
        """
    )

# ── Pick the data file ───────────────────────────────────────────────────────
csvs = _find_csvs()
if not csvs:
    st.warning(
        f"No portfolio CSV found in `{DATA_DIR}`.\n\n"
        "Export a **Statement of Assets** (*Vermögensaufstellung*) from Portfolio "
        "Performance as CSV and drop it into that folder, or upload one below."
    )
    up = st.file_uploader("Upload a Portfolio Performance CSV", type=["csv"])
    if up is None:
        st.stop()
    DATA_DIR.mkdir(exist_ok=True)
    dest = DATA_DIR / up.name
    dest.write_bytes(up.getbuffer())
    st.success(f"Saved to `{dest}`. Reload to use it from the folder.")
    source = str(dest)
else:
    names = [p.name for p in csvs]
    pick = st.selectbox("Portfolio file", names, index=0,
                        help=f"CSV files found in {DATA_DIR} (newest first).")
    source = str(DATA_DIR / pick)

# ── Load holdings + mapping ──────────────────────────────────────────────────
try:
    holdings = load_portfolio(source)
except Exception as e:
    st.error(f"Could not read the portfolio CSV: {e}")
    st.stop()

if holdings.empty or len(holdings) < 2:
    st.info("Need at least two positions with a ticker and a market value.")
    st.stop()

mapping = load_mapping(str(MAP_FILE))
holdings = apply_mapping(holdings, mapping)

if mapping.empty:
    st.caption(
        f"No `{MAP_FILE.name}` found — using Portfolio Performance tickers as-is "
        f"and assuming {BASE_CURRENCY}. Add one to remap tickers / set currencies."
    )
else:
    remapped = int((holdings["Yahoo"] != holdings["Symbol"]).sum())
    foreign = int((holdings["Currency"] != BASE_CURRENCY).sum())
    st.caption(
        f"Using `{MAP_FILE.name}`: {remapped} ticker(s) remapped · "
        f"{foreign} position(s) in a non-{BASE_CURRENCY} currency (converted to "
        f"{BASE_CURRENCY})."
    )

total_value = float(holdings["Marktwert"].sum())
st.caption(f"**{len(holdings)} positions** · total value **{total_value:,.0f} {BASE_CURRENCY}**")

show = holdings.copy()
show["Weight %"] = (show["Weight"] * 100).round(2)
show["Marktwert"] = show["Marktwert"].round(2)
st.dataframe(
    show[["Name", "Symbol", "Yahoo", "Currency", "Marktwert", "Weight %"]],
    hide_index=True,
    use_container_width=True,
    height=_table_height(len(show)),
)

# ── Controls ─────────────────────────────────────────────────────────────────
c_win, c_freq = st.columns(2)
with c_win:
    window = st.radio("Lookback window", list(_CORR_WINDOWS.keys()),
                      index=1, horizontal=True, key="pf_window")
with c_freq:
    freq = st.radio("Return frequency", list(_CORR_FREQ.keys()),
                    index=1, horizontal=True, key="pf_freq")

# Signature includes the mapping so re-mapping invalidates a stale result.
map_sig = tuple(sorted(zip(holdings["Symbol"], holdings["Yahoo"], holdings["Currency"])))
sig = (map_sig, window, freq)
if st.button("📊 Compute portfolio correlation", type="primary", key="pf_compute"):
    st.session_state["pf_sig"] = sig
if st.session_state.get("pf_sig") != sig:
    st.info("Set the window and frequency, then press **📊 Compute portfolio correlation**.")
    st.stop()

# ── Fetch prices + FX, convert to base currency ──────────────────────────────
fetch_tickers = tuple(sorted(set(holdings["Yahoo"])))
currencies = set(holdings["Currency"])
fx_syms = tuple(fx_tickers_for(currencies))

with st.spinner("Fetching price history and FX rates from Yahoo…"):
    hist_native = _fetch_perf_history(fetch_tickers)
    fx_hist = _fetch_perf_history(fx_syms) if fx_syms else {}

# FX series keyed by currency: 'C per base' (e.g. USD per EUR from EURUSD=X).
cur_per_base = {c: fx_hist.get(f"{BASE_CURRENCY}{c}=X") for c in currencies if c != BASE_CURRENCY}

# Build an EUR-converted price series per *position* (keyed by PP symbol).
hist_by_pos: dict[str, pd.Series] = {}
no_price: list[str] = []
no_fx: list[str] = []
for r in holdings.itertuples():
    native = hist_native.get(r.Yahoo)
    if native is None or native.empty:
        no_price.append(r.Symbol)
        continue
    if r.Currency == BASE_CURRENCY:
        hist_by_pos[r.Symbol] = native
    else:
        fx = cur_per_base.get(r.Currency)
        if fx is None or fx.empty:
            no_fx.append(f"{r.Symbol} ({r.Currency})")
            continue
        hist_by_pos[r.Symbol] = convert_to_base(native, fx)

returns, ppy, min_obs, thin = compute_returns(hist_by_pos, window, freq)

if returns.shape[1] < 2:
    st.error("Not enough positions have sufficient price history in this window.")
    dropped_all = sorted(set(no_price) | set(thin) | set(no_fx))
    if dropped_all:
        st.caption("Unusable: " + ", ".join(dropped_all))
    st.stop()

weights = holdings.set_index("Symbol")["Weight"]
mv = holdings.set_index("Symbol")["Marktwert"]
survivors = list(returns.columns)
covered = float(mv.reindex(survivors).sum() / mv.sum()) if mv.sum() > 0 else 0.0

if no_price:
    st.warning("No Yahoo price history for: " + ", ".join(sorted(no_price)))
if no_fx:
    st.warning("No FX rate available (excluded): " + ", ".join(sorted(no_fx)))
if thin:
    st.warning(
        f"Too little data in this window (need ≥{min_obs} {freq.lower()} obs): "
        + ", ".join(sorted(thin))
    )
if no_price or no_fx or thin:
    st.caption("Weights renormalized over the remaining holdings.")

corr = returns.corr(min_periods=min_obs)

# ── Output #1: weighted-average correlation ──────────────────────────────────
st.divider()
st.subheader("① Weighted-average correlation")
rho = weighted_avg_correlation(corr, weights)

m1, m2 = st.columns([1, 2])
with m1:
    st.metric("Portfolio ρ̄", f"{rho:+.2f}" if rho is not None else "—")
with m2:
    st.caption(
        f"Covers **{covered*100:.0f}%** of portfolio value "
        f"({len(survivors)} of {len(holdings)} positions) · "
        f"{freq.lower()} returns · {window} window · "
        f"as of {returns.index[-1].strftime('%Y-%m-%d')}"
    )
    if rho is not None:
        if rho >= 0.85:
            msg = "Very high — holdings move almost as one; little diversification."
        elif rho >= 0.6:
            msg = "High — strong shared movement; some redundancy."
        elif rho >= 0.3:
            msg = "Moderate — related but with meaningful independent movement."
        else:
            msg = "Low — holdings move largely independently; well diversified."
        st.caption(msg)
st.caption(
    "Average of all pairwise correlations, each weighted by the two positions' "
    "value weights (wᵢ·wⱼ). Your largest holdings drive this number."
)

# ── Output #2: portfolio volatility & diversification ─────────────────────────
st.divider()
st.subheader("② Portfolio volatility & diversification")
risk = portfolio_risk(returns, weights, ppy)

if risk["port_vol"] is None:
    st.info("Could not compute portfolio volatility for this selection.")
else:
    k1, k2, k3 = st.columns(3)
    k1.metric("Portfolio volatility", f"{risk['port_vol']*100:.1f}%",
              help="Annualized std-dev of the weighted portfolio = √(wᵀΣw). "
                   "The risk you actually carry, after holdings partly offset.")
    k2.metric("No-diversification vol", f"{risk['wavg_vol']*100:.1f}%",
              help="Weighted average of each holding's own volatility — what you'd "
                   "have if everything moved in perfect lockstep.")
    k3.metric("Diversification ratio", f"{risk['div_ratio']:.2f}×",
              help="No-diversification vol ÷ portfolio vol. 1.0 = no benefit; "
                   "higher is better.")
    if risk["vol_reduction"] is not None:
        st.caption(
            f"Diversification removes about **{risk['vol_reduction']*100:.0f}%** of "
            f"the volatility you'd have if your holdings all moved together "
            f"({risk['wavg_vol']*100:.1f}% → {risk['port_vol']*100:.1f}%)."
        )

# ── Correlation matrix & top pairs ───────────────────────────────────────────
st.divider()
st.subheader("Correlation matrix")
st.dataframe(
    corr.round(2).style.map(_corr_bg),
    use_container_width=True,
    height=_table_height(len(corr)),
)

# Most-correlated pairs, weighted by combined position size.
pairs = []
cols = list(corr.columns)
for i in range(len(cols)):
    for j in range(i + 1, len(cols)):
        a, b = cols[i], cols[j]
        rij = corr.iloc[i, j]
        if pd.isna(rij):
            continue
        pairs.append({
            "Pair": f"{a} ↔ {b}",
            "ρ": round(float(rij), 2),
            "Combined weight %": round(float(weights.get(a, 0) + weights.get(b, 0)) * 100, 1),
        })
if pairs:
    st.subheader("Most-correlated pairs")
    pdf = pd.DataFrame(pairs).sort_values("ρ", ascending=False).head(15)
    st.dataframe(pdf, hide_index=True, use_container_width=True,
                 height=_table_height(len(pdf)))
