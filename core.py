"""
core.py — shared, UI-agnostic analytics reused by both Streamlit apps
(watchlist.py and portfolio.py).

Nothing here touches MotherDuck. The only Streamlit dependency is the
``@st.cache_data`` decorator on the price download, which simply memoizes the
Yahoo fetch per running app — it does not require any particular app layout.

Price history always comes from Yahoo Finance; the *list* of tickers (and, for
the portfolio app, their weights) is supplied by the caller.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import streamlit as st
import yfinance as yf


# ── Correlation windows & frequencies ────────────────────────────────────────
# Lookback window: label → DateOffset to slice the price history (None = Max).
_CORR_WINDOWS: dict[str, "pd.DateOffset | None"] = {
    "6M": pd.DateOffset(months=6),
    "1Y": pd.DateOffset(years=1),
    "2Y": pd.DateOffset(years=2),
    "3Y": pd.DateOffset(years=3),
    "5Y": pd.DateOffset(years=5),
    "Max": None,
}
# Return frequency: label → (resample rule, periods/year for annualizing, min obs
# required per pair). The minimum scales with frequency so monthly returns aren't
# wiped out by a daily-sized threshold (a 1Y monthly window only has ~12 points).
_CORR_FREQ: dict[str, tuple[str | None, int, int]] = {
    "Daily":   (None,    252, 20),
    "Weekly":  ("W-FRI",  52, 12),
    "Monthly": ("ME",     12,  8),
}


# ── Price download (Yahoo) ───────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_perf_history(tickers_tuple: tuple[str, ...], auto_adjust: bool = True) -> dict[str, pd.Series]:
    """Download up to 11 years of daily close prices in one batched call.
    auto_adjust=True  → adjusted close (total return incl. dividends)
    auto_adjust=False → unadjusted close (price return only)"""
    if not tickers_tuple:
        return {}
    try:
        data = yf.download(
            list(tickers_tuple),
            period="16y",
            interval="1d",
            group_by="ticker",
            progress=False,
            threads=True,
            auto_adjust=auto_adjust,
        )
    except Exception:
        return {}
    if data is None or data.empty:
        return {}
    out: dict[str, pd.Series] = {}
    multi = isinstance(data.columns, pd.MultiIndex)
    for t in tickers_tuple:
        try:
            closes = (data[t]["Close"] if multi else data["Close"]).dropna()
            if not closes.empty:
                out[t] = closes
        except Exception:
            continue
    return out


# ── Per-ticker return/risk stats ──────────────────────────────────────────────
def _corr_ticker_stats(prices: pd.Series, rets: pd.Series, periods_per_year: int) -> dict:
    """Per-ticker return/risk over the window: CAGR, annualized volatility, and their
    ratio (a Sharpe-like return-per-unit-of-risk with a 0% risk-free rate)."""
    p = prices.dropna()
    r = rets.dropna()
    out = {"cagr": None, "vol": None, "ratio": None}
    if len(p) >= 2:
        years = (p.index[-1] - p.index[0]).days / 365.25
        if years > 0 and p.iloc[0] > 0:
            out["cagr"] = (p.iloc[-1] / p.iloc[0]) ** (1 / years) - 1
    if len(r) >= 2:
        out["vol"] = float(r.std() * (periods_per_year ** 0.5))
    if out["cagr"] is not None and out["vol"]:
        out["ratio"] = out["cagr"] / out["vol"]
    return out


# ── Prices → periodic returns ─────────────────────────────────────────────────
def compute_returns(
    hist: dict[str, pd.Series], window: str, freq: str
) -> tuple[pd.DataFrame, int, int, list[str]]:
    """Turn a {ticker: price Series} dict into a periodic-returns DataFrame.

    Mirrors the watchlist correlation page: slice to the window, resample to the
    chosen frequency, percentage-change, then drop tickers that don't have enough
    overlapping observations to correlate.

    Returns (returns_df, periods_per_year, min_obs, dropped_thin_tickers).
    """
    have = [t for t, s in hist.items() if s is not None and not s.empty]
    if len(have) < 2:
        return pd.DataFrame(), 0, 0, []

    prices = pd.DataFrame({t: hist[t] for t in have}).sort_index()
    offset = _CORR_WINDOWS[window]
    if offset is not None:
        cutoff = prices.index[-1] - offset
        prices = prices[prices.index >= cutoff]

    rule, periods_per_year, min_obs = _CORR_FREQ[freq]
    sampled = prices if rule is None else prices.resample(rule).last()
    returns = sampled.pct_change().dropna(how="all")

    enough = [c for c in returns.columns if returns[c].notna().sum() >= min_obs]
    thin = [c for c in returns.columns if c not in enough]
    return returns[enough], periods_per_year, min_obs, thin


# ── Portfolio-level correlation & risk ────────────────────────────────────────
def weighted_avg_correlation(corr: pd.DataFrame, weights: pd.Series) -> float | None:
    """Output #1 — a single diversification score.

    The market-value-weighted average of every *pairwise* correlation, weighting
    each pair (i, j) by wᵢ·wⱼ. Self-pairs (the diagonal, always 1.0) are excluded.
    Because it's a ratio of weighted sums, absolute weight scale cancels — only the
    relative sizes of your positions matter.

        ρ̄ = Σ_{i≠j} wᵢ wⱼ ρ_ij  /  Σ_{i≠j} wᵢ wⱼ

    Near +1 → holdings move almost as one (little diversification). Near 0 →
    largely independent. Can be negative if positions genuinely hedge each other.
    """
    cols = [c for c in corr.columns if c in weights.index]
    if len(cols) < 2:
        return None
    w = weights.reindex(cols).fillna(0.0).to_numpy(dtype=float)
    C = corr.loc[cols, cols].to_numpy(dtype=float)
    W = np.outer(w, w)
    off = ~np.eye(len(cols), dtype=bool)
    valid = off & ~np.isnan(C)
    num = float(np.sum(W[valid] * C[valid]))
    den = float(np.sum(W[valid]))
    return num / den if den > 0 else None


def portfolio_risk(
    returns: pd.DataFrame, weights: pd.Series, periods_per_year: int
) -> dict:
    """Output #2 — risk the covariance matrix actually implies.

    Weights are restricted to the tickers present in ``returns`` and renormalized
    to sum to 1, so dropped (thin-data) holdings don't distort the result.

    Returns a dict:
      port_vol   annualized volatility of the weighted portfolio = √(wᵀ Σ w)·√ppy
      wavg_vol   weighted average of the individual annualized vols (Σ wᵢ σᵢ) —
                 i.e. the "no-diversification" volatility you'd have if every
                 holding moved in lockstep
      div_ratio  wavg_vol / port_vol. 1.0 = no diversification benefit; higher is
                 better (e.g. 1.4 ⇒ real vol ≈ 29% below the weighted-average vol)
      vol_reduction  1 − port_vol / wavg_vol, the % of weighted-average vol that
                 diversification removes
    """
    out = {"port_vol": None, "wavg_vol": None, "div_ratio": None, "vol_reduction": None}
    cols = [c for c in returns.columns if c in weights.index]
    if len(cols) < 2:
        return out

    w = weights.reindex(cols).fillna(0.0).to_numpy(dtype=float)
    s = w.sum()
    if s <= 0:
        return out
    w = w / s

    r = returns[cols]
    cov = r.cov().to_numpy(dtype=float)                       # periodic covariance
    port_var = float(w @ cov @ w)
    port_vol = (port_var ** 0.5) * (periods_per_year ** 0.5)  # annualized

    ind_vol = r.std().to_numpy(dtype=float) * (periods_per_year ** 0.5)
    wavg_vol = float((w * ind_vol).sum())

    out["port_vol"] = port_vol
    out["wavg_vol"] = wavg_vol
    if port_vol > 0:
        out["div_ratio"] = wavg_vol / port_vol
    if wavg_vol > 0:
        out["vol_reduction"] = 1.0 - port_vol / wavg_vol
    return out


# ── Currency conversion (for mapped tickers in a foreign currency) ────────────
BASE_CURRENCY = "EUR"


def fx_tickers_for(currencies, base: str = BASE_CURRENCY) -> list[str]:
    """Yahoo FX symbols needed to convert the given currencies into ``base``.

    Uses base-quoted pairs, e.g. EURUSD=X (USD per 1 EUR), EURJPY=X (JPY per EUR)
    — the most reliably-quoted direction on Yahoo. The base currency itself needs
    no pair.
    """
    wanted = {c.strip().upper() for c in currencies if c and c.strip().upper() != base}
    return [f"{base}{c}=X" for c in sorted(wanted)]


def convert_to_base(price: pd.Series, cur_per_base: pd.Series | None) -> pd.Series:
    """Convert a price series quoted in currency C into the base currency.

    ``cur_per_base`` is the 'C per 1 base' FX series (e.g. EURUSD=X = USD per EUR).
    Since a price in C divided by (C per base) yields the base amount:
        price_base = price_C / (C per base)
    FX is aligned to the price index and forward/back-filled over gaps (FX and
    equity calendars differ slightly). ``None`` means the price is already in base.
    """
    if cur_per_base is None:
        return price
    fx = cur_per_base.reindex(price.index).ffill().bfill()
    return (price / fx).dropna()

