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
    latest_fx_to_base,
    search_tickers,
    validate_ticker,
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
COL_SHARES = "Bestand"

# Flag a position whose live value differs from the PP export by more than this
# (likely a share-ratio / wrong-listing mapping, not normal price drift).
VAL_FLAG_THRESHOLD = 0.40


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
def load_portfolio(path: str) -> tuple[pd.DataFrame, float]:
    """Parse a PP 'Statement of Assets' CSV (German number format).

    Returns (positions, pp_total) where:
      positions  tidy frame: Name, Symbol, Shares, Marktwert (EUR, as exported), Weight
      pp_total   the grand-total market value from the 'Summe' row (for the
                 live-valuation cross-check)

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
    has_shares = COL_SHARES in df.columns

    # Grand total from the 'Summe' row (before we drop the non-position rows).
    pp_total = 0.0
    summe = df[df[COL_NAME].astype("string").str.strip() == "Summe"]
    if not summe.empty:
        pp_total = float(pd.to_numeric(summe[COL_VALUE], errors="coerce").dropna().iloc[0])

    # Keep only real positions (group headers & the 'Summe' total have no Symbol).
    df = df[df[COL_SYMBOL].notna() & (df[COL_SYMBOL].str.strip() != "")].copy()
    df[COL_SYMBOL] = df[COL_SYMBOL].str.strip()
    df[COL_VALUE] = pd.to_numeric(df[COL_VALUE], errors="coerce")
    df[COL_SHARES] = pd.to_numeric(df[COL_SHARES], errors="coerce") if has_shares else 0.0
    df = df.dropna(subset=[COL_VALUE])
    df = df[df[COL_VALUE] > 0]

    # Collapse any duplicate tickers (same security held in two sleeves).
    g = df.groupby(COL_SYMBOL, as_index=False).agg(
        Name=(COL_NAME, "first"),
        Shares=(COL_SHARES, "sum"),
        Marktwert=(COL_VALUE, "sum"),
    )
    total = g["Marktwert"].sum()
    g["Weight"] = g["Marktwert"] / total if total > 0 else 0.0
    if pp_total <= 0:
        pp_total = float(total)
    return g.sort_values("Marktwert", ascending=False).reset_index(drop=True), pp_total


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

**What-if edits.** You can **add** simulated positions (search + share count)
and **exclude** existing ones (uncheck *Include* in the holdings table, to
simulate selling). Weights are always recomputed over whatever set is active, so
every number below reflects the what-if portfolio.

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
    holdings, pp_total = load_portfolio(source)
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

# ── Simulate adding stocks (what-if) ─────────────────────────────────────────
st.session_state.setdefault("sim_holdings", [])

with st.expander("➕ Simulate adding stocks (what-if)"):
    st.caption(
        "Search Yahoo, enter a share count, preview the market value, then add. "
        "Simulated positions are included in the correlation & risk below alongside "
        "your real holdings. They are not saved anywhere."
    )
    query = st.text_input("Search Yahoo (ticker or company name)",
                          placeholder="e.g. AAPL, Nestlé, VWCE.DE", key="sim_query")
    results = search_tickers(query) if query.strip() else []

    if query.strip() and not results:
        st.caption("No matches — try a different term or an exact ticker.")
    if results:
        labels = {
            i: f"{r['symbol']} — {r['name']}"
               + (f" · {r['exchange']}" if r['exchange'] else "")
               + (f" · {r['type']}" if r['type'] else "")
            for i, r in enumerate(results)
        }
        idx = st.selectbox("Match", list(labels.keys()),
                           format_func=lambda i: labels[i], key="sim_pick")
        chosen = results[idx]
        quote = validate_ticker(chosen["symbol"])

        if not quote.get("ok"):
            st.warning(quote.get("error", "Could not fetch a quote for this symbol."))
        else:
            cur = quote["currency"] or BASE_CURRENCY
            price = float(quote["price"])
            c_sh, c_pv = st.columns([1, 2])
            with c_sh:
                shares = st.number_input("Number of shares", min_value=0.0,
                                         value=1.0, step=1.0, format="%.4f",
                                         key="sim_shares")
            eur_per = latest_fx_to_base(cur)
            with c_pv:
                if eur_per is None:
                    st.warning(f"No {cur}→{BASE_CURRENCY} FX rate available — cannot value this.")
                    mv_eur = None
                else:
                    mv_eur = shares * price * eur_per
                    fx_note = "" if cur == BASE_CURRENCY else f" · {cur}→{BASE_CURRENCY} @ {eur_per:.4f}"
                    st.metric(
                        f"Market value ({BASE_CURRENCY})",
                        f"{mv_eur:,.2f}",
                        help=f"{shares:g} × {price:,.2f} {cur}{fx_note}",
                    )
                    st.caption(f"Last price: {price:,.2f} {cur} · as of {quote['last_update']}")

            disabled = (mv_eur is None) or (shares <= 0)
            if st.button("➕ Add to simulation", disabled=disabled, key="sim_add"):
                st.session_state["sim_holdings"].append({
                    "name": chosen["name"],
                    "ticker": chosen["symbol"],
                    "currency": cur,
                    "shares": float(shares),
                    "price": price,
                    "mv_eur": float(mv_eur),
                })
                st.rerun()

    sims = st.session_state["sim_holdings"]
    if sims:
        st.markdown("**Simulated additions**")
        for i, s in enumerate(sims):
            c1, c2 = st.columns([6, 1])
            c1.write(
                f"`{s['ticker']}` {s['name']} — {s['shares']:g} sh "
                f"≈ **{s['mv_eur']:,.0f} {BASE_CURRENCY}** ({s['currency']})"
            )
            if c2.button("✕", key=f"sim_rm_{i}", help="Remove"):
                st.session_state["sim_holdings"].pop(i)
                st.rerun()
        if st.button("Clear all simulated", key="sim_clear"):
            st.session_state["sim_holdings"] = []
            st.rerun()

# Merge simulated positions into the holdings, then (re)compute weights over the
# combined total so every weight reflects the what-if portfolio.
holdings["Sim"] = ""
sims = st.session_state["sim_holdings"]
if sims:
    sim_rows = pd.DataFrame([{
        "Symbol": f"SIM:{s['ticker']}",
        "Name": s["name"],
        "Shares": s["shares"],
        "Marktwert": s["mv_eur"],
        "Yahoo": s["ticker"],
        "Currency": s["currency"],
        "Sim": "➕",
    } for s in sims])
    holdings = pd.concat([holdings, sim_rows], ignore_index=True)

holdings = holdings.reset_index(drop=True)

# ── Include / exclude positions (simulate selling) ───────────────────────────
# Persist the excluded set (by Symbol) so toggles survive reruns and feed the
# compute signature below. Unchecking a holding re-weights the rest over the
# smaller total — i.e. as if you had sold it.
excluded = set(st.session_state.get("pf_excluded", []))
inc_mask = ~holdings["Symbol"].isin(excluded)
inc_total = float(holdings.loc[inc_mask, "Marktwert"].sum())

editor = holdings.copy()
editor["Include"] = inc_mask
editor["Marktwert"] = editor["Marktwert"].round(2)
editor["Weight %"] = (
    (holdings["Marktwert"].where(inc_mask, 0.0) / inc_total * 100).round(2)
    if inc_total > 0 else 0.0
)

st.caption(
    "Uncheck **Include** to simulate selling a position — the rest are "
    "re-weighted over the smaller total, and correlation & risk recompute on the "
    "remaining holdings."
)
edited = st.data_editor(
    editor[["Include", "Sim", "Name", "Symbol", "Yahoo", "Currency", "Marktwert", "Weight %"]],
    column_config={
        "Include": st.column_config.CheckboxColumn(
            "Include",
            help="Uncheck to exclude this position (simulate selling it).",
            default=True,
        ),
    },
    disabled=["Sim", "Name", "Symbol", "Yahoo", "Currency", "Marktwert", "Weight %"],
    hide_index=True,
    use_container_width=True,
    height=_table_height(len(editor)),
    key="pf_holdings_editor",
)

# Persist any change to the excluded set, then rerun so weights/labels refresh.
new_excluded = set(edited.loc[~edited["Include"], "Symbol"])
if new_excluded != excluded:
    st.session_state["pf_excluded"] = sorted(new_excluded)
    st.rerun()

# Keep only the included positions and re-weight over their combined total.
holdings = holdings[inc_mask].copy()
if len(holdings) < 2:
    st.info("Keep at least two positions included to compute correlation & risk.")
    st.stop()

total = holdings["Marktwert"].sum()
holdings["Weight"] = holdings["Marktwert"] / total if total > 0 else 0.0

total_value = float(holdings["Marktwert"].sum())
n_excluded = int((~inc_mask).sum())
st.caption(
    f"**{len(holdings)} positions** · total value **{total_value:,.0f} {BASE_CURRENCY}**"
    + (f" · {n_excluded} excluded" if n_excluded else "")
)

# ── Controls ─────────────────────────────────────────────────────────────────
c_win, c_freq = st.columns(2)
with c_win:
    window = st.radio("Lookback window", list(_CORR_WINDOWS.keys()),
                      index=1, horizontal=True, key="pf_window")
with c_freq:
    freq = st.radio("Return frequency", list(_CORR_FREQ.keys()),
                    index=1, horizontal=True, key="pf_freq")

VAL_SNAPSHOT = "PP snapshot (as exported)"
VAL_LIVE = "Live (shares × current price)"
val_mode = st.radio(
    "Valuation", [VAL_SNAPSHOT, VAL_LIVE], index=1, horizontal=True, key="pf_val_mode",
    help="Snapshot: use the market values from the CSV export. "
         "Live: recompute each position as shares × current Yahoo price, converted "
         "to EUR — always up to date, but assumes 1 CSV share = 1 Yahoo-ticker share.",
)

# Signature includes the mapping, market values AND valuation mode (so re-mapping,
# changing a simulated position, or switching valuation invalidates a stale result).
map_sig = tuple(sorted(
    (r.Symbol, r.Yahoo, r.Currency, round(float(r.Marktwert), 2), round(float(r.Shares), 6))
    for r in holdings.itertuples()
))
sig = (map_sig, window, freq, val_mode)
if st.button("📊 Compute portfolio correlation", type="primary", key="pf_compute"):
    st.session_state["pf_sig"] = sig
if st.session_state.get("pf_sig") != sig:
    st.info("Set the options above, then press **📊 Compute portfolio correlation**.")
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

# ── Live valuation (Option C): shares × latest price → EUR, per position ──────
def _latest(s) -> float | None:
    return float(s.iloc[-1]) if (s is not None and not s.empty) else None

live_val: dict[str, float] = {}
for r in holdings.itertuples():
    px = _latest(hist_native.get(r.Yahoo))
    if px is None:
        continue
    if r.Currency == BASE_CURRENCY:
        epu = 1.0
    else:
        last_fx = _latest(cur_per_base.get(r.Currency))
        if not last_fx:
            continue
        epu = 1.0 / last_fx
    live_val[r.Symbol] = float(r.Shares) * px * epu

# Cross-check live vs PP export (real positions only — sims aren't in the PP total)
# and flag positions that diverge a lot (likely a share-ratio / wrong-listing map).
snap_by_sym = holdings.set_index("Symbol")["Marktwert"]
real_syms = set(holdings.loc[holdings["Sim"] != "➕", "Symbol"])
live_real_total = sum(v for s, v in live_val.items() if s in real_syms)
val_flags = []
for s, lv in live_val.items():
    snap = float(snap_by_sym.get(s, 0.0))
    if snap > 0 and abs(lv / snap - 1.0) > VAL_FLAG_THRESHOLD:
        nm = holdings.loc[holdings["Symbol"] == s, "Name"].iloc[0]
        val_flags.append((s, nm, snap, lv, lv / snap - 1.0))

# Apply the chosen valuation to the Marktwert / Weight used downstream.
if val_mode == VAL_LIVE:
    holdings = holdings.copy()
    holdings["Marktwert"] = [live_val.get(s, m) for s, m in
                             zip(holdings["Symbol"], holdings["Marktwert"])]
    _tot = holdings["Marktwert"].sum()
    holdings["Weight"] = holdings["Marktwert"] / _tot if _tot > 0 else 0.0

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

# ── Valuation cross-check (live mode) ─────────────────────────────────────────
if val_mode == VAL_LIVE:
    st.divider()
    st.subheader("Live valuation")
    # When positions are excluded, compare against the snapshot total of the
    # *included* real positions (not the full PP export) so the check stays
    # apples-to-apples and doesn't look like a mapping error.
    if n_excluded:
        base_total = float(snap_by_sym.reindex(sorted(real_syms)).fillna(0.0).sum())
        base_label = f"Export total, included ({BASE_CURRENCY})"
    else:
        base_total = pp_total
        base_label = f"PP export total ({BASE_CURRENCY})"
    dev = (live_real_total / base_total - 1.0) if base_total > 0 else None
    # What-if total: every INCLUDED position valued live — real holdings you kept
    # checked plus any simulated additions (excluded positions are already gone).
    live_total_all = float(sum(live_val.values()))
    cc1, cc2, cc3, cc4 = st.columns(4)
    cc1.metric(
        f"What-if live total ({BASE_CURRENCY})", f"{live_total_all:,.0f}",
        help="Every included position valued live: the real holdings you kept "
             "checked plus any simulated additions. Excluded positions are left "
             "out. This is the portfolio the correlation & risk below are based on.",
    )
    cc2.metric(f"Live value, real positions ({BASE_CURRENCY})", f"{live_real_total:,.0f}",
               help="Included real holdings only — no simulated positions. This is "
                    "the figure cross-checked against the export total.")
    cc3.metric(base_label, f"{base_total:,.0f}")
    cc4.metric("Difference", f"{dev*100:+.1f}%" if dev is not None else "—")
    if dev is not None and abs(dev) > 0.10:
        st.warning(
            f"Live total differs from the PP export by {dev*100:+.1f}%. Some drift is "
            "normal (prices/FX move after export), but a large gap can also mean a "
            "wrong ticker mapping or a share-ratio mismatch (e.g. an ADR)."
        )

    lv_tbl = holdings.copy()
    lv_tbl["Live value"] = lv_tbl["Marktwert"].round(2)
    lv_tbl["Weight %"] = (lv_tbl["Weight"] * 100).round(2)
    lv_tbl["Shares"] = lv_tbl["Shares"].round(4)
    lv_tbl = lv_tbl.sort_values("Marktwert", ascending=False)
    st.dataframe(
        lv_tbl[["Sim", "Name", "Symbol", "Yahoo", "Currency", "Shares", "Live value", "Weight %"]],
        hide_index=True, use_container_width=True, height=_table_height(len(lv_tbl)),
    )

    if val_flags:
        with st.expander(f"⚠️ {len(val_flags)} position(s) differ a lot from the export — check mapping"):
            fdf = pd.DataFrame(
                [{"Symbol": s, "Name": nm,
                  f"Export ({BASE_CURRENCY})": round(snap, 2),
                  f"Live ({BASE_CURRENCY})": round(lv, 2),
                  "Δ %": round(d * 100, 1)}
                 for s, nm, snap, lv, d in sorted(val_flags, key=lambda x: -abs(x[4]))],
            )
            st.caption(
                "Large gaps often mean the Yahoo ticker isn't 1:1 with your holding "
                "(wrong listing, or an ADR representing ≠1 share). Fix the ticker in "
                f"`{MAP_FILE.name}`, or switch back to **{VAL_SNAPSHOT}**."
            )
            st.dataframe(fdf, hide_index=True, use_container_width=True,
                         height=_table_height(len(fdf)))

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
# Label rows/cols by Yahoo ticker (positions are keyed internally by PP symbol).
yahoo_by_sym = holdings.set_index("Symbol")["Yahoo"].to_dict()
name_by_sym = holdings.set_index("Symbol")["Name"].to_dict()
corr_disp = corr.rename(index=lambda s: yahoo_by_sym.get(s, s),
                        columns=lambda s: yahoo_by_sym.get(s, s))
st.dataframe(
    corr_disp.round(2).style.map(_corr_bg),
    use_container_width=True,
    height=_table_height(len(corr_disp)),
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
            "Pair": f"{name_by_sym.get(a, a)} ↔ {name_by_sym.get(b, b)}",
            "ρ": round(float(rij), 2),
            "Combined weight %": round(float(weights.get(a, 0) + weights.get(b, 0)) * 100, 1),
            "_sim": str(a).startswith("SIM:") or str(b).startswith("SIM:"),
        })
if pairs:
    st.subheader("Most-correlated pairs")
    pdf = pd.DataFrame(pairs).sort_values("ρ", ascending=False).head(15).reset_index(drop=True)
    sim_rows = pdf["_sim"].to_numpy()
    pdf = pdf.drop(columns="_sim")

    def _hi_sim(row):
        # Highlight any pair that includes a simulated position.
        hit = bool(sim_rows[row.name])
        return ["background-color: #fff3cd; color: black" if hit else ""] * len(row)

    styler = pdf.style.apply(_hi_sim, axis=1)
    if sim_rows.any():
        st.caption("Highlighted pairs include a simulated (`SIM:`) position.")
    st.dataframe(styler, hide_index=True, use_container_width=True,
                 height=_table_height(len(pdf)))
