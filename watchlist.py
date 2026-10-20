import streamlit as st
import yfinance as yf
import pandas as pd
import duckdb
import os
import re
import html
from pathlib import Path
from datetime import datetime, timedelta

from populate_names import enrich as enrich_ticker_meta

st.set_page_config(page_title="Stock Monitor", layout="wide")


def _load_env(path: Path):
    """Minimal .env loader (no extra dependency). Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env(Path(__file__).parent / ".env")


def _secret(name: str, default: str | None = None) -> str | None:
    """Resolve a secret from, in order: Streamlit secrets (the Streamlit Cloud
    dashboard or a local .streamlit/secrets.toml), then environment variables / the
    local .env file. This lets the same code run locally and on Streamlit Cloud."""
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass  # no secrets.toml present (e.g. a plain local run) — fall through to env
    return os.environ.get(name, default)


DB_NAME = _secret("DB_NAME", "watchlists")

DEFAULT_WATCHLISTS = {
    "US Tech":      ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AMD", "INTC", "ORCL"],
    "US Finance":   ["JPM", "BAC", "GS", "MS", "WFC", "C", "BLK", "AXP", "V", "MA"],
    "EU Blue Chips":["SAP.DE", "ASML.AS", "LVMH.PA", "MC.PA", "SIE.DE", "ALV.DE", "BAS.DE", "BMW.DE", "DTE.DE", "ADS.DE"],
    "Crypto":       ["BTC-USD", "ETH-USD", "BNB-USD", "SOL-USD", "XRP-USD", "ADA-USD", "DOGE-USD", "AVAX-USD", "DOT-USD", "MATIC-USD"],
}

TAGS = ["Sell", "Stock up", "Monitor"]


# ── Database (MotherDuck) ───────────────────────────────────────────────────────
@st.cache_resource
def _db() -> duckdb.DuckDBPyConnection:
    """One cached MotherDuck connection for the whole app (auth/handshake once)."""
    token = _secret("MOTHERDUCK_TOKEN")
    if not token:
        st.error(
            "MOTHERDUCK_TOKEN is not set — add it to EmbeddedTradingView/.env locally, "
            "or to the app's Secrets on Streamlit Cloud."
        )
        st.stop()
    os.environ["motherduck_token"] = token  # read by the MotherDuck extension
    return duckdb.connect(f"md:{DB_NAME}")


def get_conn() -> duckdb.DuckDBPyConnection:
    # A short-lived cursor over the one cached MotherDuck connection. Each caller gets
    # its own cursor, so existing `with get_conn() as conn:` sites keep working — the
    # `with` closes the cursor, not the shared connection — and no new network
    # connection is opened per call.
    return _db().cursor()


def init_db():
    with get_conn() as conn:
        conn.execute("CREATE SEQUENCE IF NOT EXISTS seq_watchlists START 1")
        conn.execute("CREATE SEQUENCE IF NOT EXISTS seq_items START 1")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlists (
                id     INTEGER PRIMARY KEY DEFAULT nextval('seq_watchlists'),
                name   TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'target'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id           INTEGER PRIMARY KEY DEFAULT nextval('seq_items'),
                watchlist_id INTEGER NOT NULL,
                ticker       TEXT NOT NULL,
                UNIQUE(watchlist_id, ticker)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_tags (
                ticker TEXT NOT NULL,
                tag    TEXT NOT NULL,
                UNIQUE(ticker, tag)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ticker_names (
                ticker   TEXT PRIMARY KEY,
                name     TEXT,
                exchange TEXT,
                currency TEXT
            )
            """
        )
        # Migrate older DBs that predate the watchlist status column
        cols = [r[1] for r in conn.execute("PRAGMA table_info('watchlists')").fetchall()]
        if "status" not in cols:
            conn.execute("ALTER TABLE watchlists ADD COLUMN status TEXT NOT NULL DEFAULT 'target'")
        if "position" not in cols:
            conn.execute("ALTER TABLE watchlists ADD COLUMN position INTEGER")
            conn.execute("UPDATE watchlists SET position = id WHERE position IS NULL")

        # Migrate ticker_names created before the exchange/currency columns existed
        tn_cols = [r[1] for r in conn.execute("PRAGMA table_info('ticker_names')").fetchall()]
        if "exchange" not in tn_cols:
            conn.execute("ALTER TABLE ticker_names ADD COLUMN exchange TEXT")
        if "currency" not in tn_cols:
            conn.execute("ALTER TABLE ticker_names ADD COLUMN currency TEXT")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS perf_presets (
                name        TEXT PRIMARY KEY,
                tickers     TEXT NOT NULL,
                main_ticker TEXT
            )
            """
        )

        # Migrate perf_presets created before the main_ticker column existed
        pp_cols = [r[1] for r in conn.execute("PRAGMA table_info('perf_presets')").fetchall()]
        if "main_ticker" not in pp_cols:
            conn.execute("ALTER TABLE perf_presets ADD COLUMN main_ticker TEXT")

        # Seed defaults only if the DB is empty
        empty = conn.execute("SELECT COUNT(*) FROM watchlists").fetchone()[0] == 0
        if empty:
            for pos, (name, tickers) in enumerate(DEFAULT_WATCHLISTS.items()):
                wl_id = conn.execute(
                    "INSERT INTO watchlists (name, status, position) VALUES (?, 'target', ?) RETURNING id",
                    [name, pos],
                ).fetchone()[0]
                conn.executemany(
                    "INSERT INTO items (watchlist_id, ticker) VALUES (?, ?)",
                    [(wl_id, t) for t in tickers],
                )


def get_watchlists() -> dict[str, list[str]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT w.name, i.ticker
            FROM watchlists w
            LEFT JOIN items i ON i.watchlist_id = w.id
            ORDER BY COALESCE(w.position, w.id), w.id, i.id
            """
        ).fetchall()
    result: dict[str, list[str]] = {}
    for name, ticker in rows:
        result.setdefault(name, [])
        if ticker is not None:
            result[name].append(ticker)
    return result


def create_watchlist(name: str, status: str = "target") -> bool:
    try:
        with get_conn() as conn:
            next_pos = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM watchlists"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO watchlists (name, status, position) VALUES (?, ?, ?)",
                [name, status, next_pos],
            )
        return True
    except duckdb.ConstraintException:
        return False


def set_watchlist_order(names_in_order: list[str]):
    with get_conn() as conn:
        for pos, name in enumerate(names_in_order):
            conn.execute("UPDATE watchlists SET position = ? WHERE name = ?", [pos, name])


def get_watchlist_statuses() -> dict[str, str]:
    with get_conn() as conn:
        return dict(conn.execute("SELECT name, status FROM watchlists").fetchall())


def set_watchlist_status(name: str, status: str):
    with get_conn() as conn:
        conn.execute("UPDATE watchlists SET status = ? WHERE name = ?", [status, name])


def add_item(watchlist_name: str, ticker: str) -> bool:
    try:
        with get_conn() as conn:
            wl = conn.execute(
                "SELECT id FROM watchlists WHERE name = ?", [watchlist_name]
            ).fetchone()
            if wl is None:
                return False
            conn.execute(
                "INSERT INTO items (watchlist_id, ticker) VALUES (?, ?)", [wl[0], ticker]
            )
        return True
    except duckdb.ConstraintException:
        return False


def delete_watchlist(name: str):
    # DuckDB has no ON DELETE CASCADE, so remove child rows first
    with get_conn() as conn:
        wl = conn.execute("SELECT id FROM watchlists WHERE name = ?", [name]).fetchone()
        if wl is not None:
            conn.execute("DELETE FROM items WHERE watchlist_id = ?", [wl[0]])
            conn.execute("DELETE FROM watchlists WHERE id = ?", [wl[0]])


def rename_watchlist(old_name: str, new_name: str) -> bool:
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE watchlists SET name = ? WHERE name = ?", [new_name, old_name]
            )
        return True
    except duckdb.ConstraintException:
        return False


def delete_item(watchlist_name: str, ticker: str):
    with get_conn() as conn:
        conn.execute(
            """
            DELETE FROM items
            WHERE ticker = ?
              AND watchlist_id = (SELECT id FROM watchlists WHERE name = ?)
            """,
            [ticker, watchlist_name],
        )


def get_tags_map() -> dict[str, list[str]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT ticker, tag FROM stock_tags ORDER BY ticker, tag").fetchall()
    result: dict[str, list[str]] = {}
    for ticker, tag in rows:
        result.setdefault(ticker, []).append(tag)
    return result


def get_tags(ticker: str) -> list[str]:
    with get_conn() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT tag FROM stock_tags WHERE ticker = ? ORDER BY tag", [ticker]
            ).fetchall()
        ]


def set_tags(ticker: str, tags: list[str]):
    """Replace the full set of tags for a ticker."""
    with get_conn() as conn:
        conn.execute("DELETE FROM stock_tags WHERE ticker = ?", [ticker])
        if tags:
            conn.executemany(
                "INSERT INTO stock_tags (ticker, tag) VALUES (?, ?)",
                [(ticker, tg) for tg in tags],
            )


def get_all_tags() -> list[str]:
    """All known tags: the predefined set plus any user-created ones in the DB."""
    with get_conn() as conn:
        used = [r[0] for r in conn.execute("SELECT DISTINCT tag FROM stock_tags").fetchall()]
    return sorted(set(TAGS) | set(used))


def get_ticker_meta() -> dict[str, tuple]:
    """All stored metadata keyed by ticker: (name, exchange, currency). Populated by
    the Names editor and/or the populate_names.py enrichment script. The app reads
    names/exchange/currency only from here — it never calls yfinance for them."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ticker, name, exchange, currency FROM ticker_names"
        ).fetchall()
    return {t: (n, e, c) for t, n, e, c in rows}


def set_ticker_name(ticker: str, name: str):
    """Set (or clear, when blank) the manual display name, preserving exchange/currency."""
    name = (name or "").strip() or None
    with get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM ticker_names WHERE ticker = ?", [ticker]).fetchone()
        if exists:
            conn.execute("UPDATE ticker_names SET name = ? WHERE ticker = ?", [name, ticker])
        elif name:
            conn.execute("INSERT INTO ticker_names (ticker, name) VALUES (?, ?)", [ticker, name])


init_db()

# ── Global light theme overrides ─────────────────────────────────────────────
st.markdown(
    """
    <style>
    /* Page background */
    [data-testid="stAppViewContainer"] { background: #f0f4f9; }
    [data-testid="stHeader"]           { background: #f0f4f9; }
    /* Sidebar */
    [data-testid="stSidebar"]          { background: #e4ecf5; }
    /* General text */
    html, body, [class*="css"]         { color: #1a1a2e; }
    /* Responsive winners/losers: desktop cards vs mobile popovers */
    .tv-desktop { display: block; }
    [class*="st-key-tvmob-"] { display: none; }
    /* Responsive big table: desktop (all columns) vs mobile (reduced + detail toggle) */
    [class*="st-key-tblmob-"] { display: none; }
    [class*="st-key-tbldetail"] { display: none; }
    @media (max-width: 640px) {
        .tv-desktop { display: none !important; }
        [class*="st-key-tvmob-"] { display: block !important; }
        [class*="st-key-tbldesk-"] { display: none !important; }
        [class*="st-key-tblmob-"] { display: block !important; }
        [class*="st-key-tbldetail"] { display: block !important; }
        /* Tighter stacking between columns (e.g. losers/winners boxes) on phones */
        [data-testid="stHorizontalBlock"] { gap: 0.2rem !important; }
    }
    /* Text / number inputs: white background for contrast */
    [data-testid="stTextInput"] input,
    [data-testid="stNumberInput"] input {
        background-color: #ffffff !important;
        border: 1px solid #c3cfdd !important;
        color: #1a1a2e !important;
    }
    /* Selectbox & multiselect closed control → white (target BaseWeb directly) */
    div[data-baseweb="select"] > div {
        background-color: #ffffff !important;
        border: 1px solid #c3cfdd !important;
        color: #1a1a2e !important;
    }
    div[data-baseweb="select"] input {
        background-color: #ffffff !important;
    }
    /* Selectbox open dropdown menu + options */
    div[data-baseweb="popover"] div[data-baseweb="menu"],
    div[data-baseweb="popover"] ul[role="listbox"] {
        background-color: #ffffff !important;
    }
    div[data-baseweb="popover"] li[role="option"] {
        background-color: #ffffff !important;
        color: #1a1a2e !important;
    }
    div[data-baseweb="popover"] li[role="option"]:hover {
        background-color: #e8f0fb !important;
    }
    /* Sidebar buttons styled as left-aligned menu items */
    [data-testid="stSidebar"] .stButton button {
        justify-content: flex-start;
        text-align: left;
        border: none;
        box-shadow: none;
    }
    /* Nudge the news filter down so its top edge aligns with the table's top edge
       (the dataframe starts slightly lower than the top of its column). */
    [class*="st-key-news_pick"] { margin-top: 1.3rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(ttl=300, show_spinner=False)
def _batch_prices(tickers: tuple[str, ...]) -> dict[str, tuple]:
    """Last price, previous close and as-of date for all tickers in ONE yf.download
    call (the chart endpoint). This replaces ~2 requests per ticker with a single
    batched request — far more reliable on cloud IPs that Yahoo rate-limits."""
    out: dict[str, tuple] = {}
    if not tickers:
        return out
    try:
        data = yf.download(
            list(tickers), period="5d", interval="1d", group_by="ticker",
            progress=False, threads=True, auto_adjust=False,
        )
    except Exception:
        return out
    if data is None or data.empty:
        return out
    multi = isinstance(data.columns, pd.MultiIndex)
    for t in tickers:
        try:
            closes = (data[t]["Close"] if multi else data["Close"]).dropna()
            if closes.empty:
                continue
            last = float(closes.iloc[-1])
            prev = float(closes.iloc[-2]) if len(closes) >= 2 else None
            out[t] = (last, prev, closes.index[-1].to_pydatetime())
        except Exception:
            continue
    return out


@st.cache_data(ttl=300)
def fetch_quotes(tickers: list[str]) -> pd.DataFrame:
    """Prices only — in one batched request. Name/exchange/currency are filled in by
    the caller from the DB (get_ticker_meta); the app no longer calls yfinance for
    metadata, so it works even where Yahoo's .info endpoint is blocked (cloud)."""
    tickers = list(tickers)
    prices = _batch_prices(tuple(tickers))
    rows = []
    for ticker in tickers:
        last, prev, asof = prices.get(ticker, (None, None, None))
        change_pct = (last - prev) / prev * 100 if (prev and last) else None
        rows.append({
            "Name":        ticker,   # placeholder; overridden from the DB by the caller
            "Ticker":      ticker,
            "Exchange":    "—",
            "Price":       last,
            "Change %":    change_pct,
            "Currency":    "",
            "Last Update": asof.strftime("%Y-%m-%d %H:%M") if asof else "—",
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=900, show_spinner=False)
def fetch_news(entries: tuple[tuple[str, str], ...], per_ticker: int = 8, limit: int | None = None) -> list[dict]:
    """Fetch recent Yahoo Finance news for a set of stocks, de-duplicated and tagged.

    `entries` is a tuple of (ticker, company name) pairs. Yahoo's Search returns no
    news for exchange-suffixed international tickers (e.g. SAP.DE), so we query the
    ticker first (precise, works for US symbols and crypto) and fall back to the
    cleaned company name when that is empty. Results are keyed by Yahoo's `uuid`, so
    an article relating to several watchlist stocks appears once, tagged with all of
    them (the intersection of its tickers with the watchlist)."""
    watch = {t for t, _ in entries}
    by_uuid: dict[str, dict] = {}
    for ticker, name in entries:
        items = _search_news(ticker, per_ticker)
        if not items and name:
            items = _search_news(_news_query(name), per_ticker)
        for it in items:
            uid = it.get("uuid") or it.get("link")
            if not uid:
                continue
            rec = by_uuid.setdefault(uid, {
                "title": it.get("title", ""), "link": it.get("link", ""),
                "publisher": it.get("publisher", ""),
                "time": it.get("providerPublishTime", 0) or 0, "tickers": set(),
            })
            rec["tickers"].add(ticker)                   # surfaced via this stock's query
            for rt in (it.get("relatedTickers") or []):  # plus any related watchlist tickers
                if rt in watch:
                    rec["tickers"].add(rt)
    out = sorted(by_uuid.values(), key=lambda r: r["time"], reverse=True)
    # Keep only articles from today and yesterday (from the start of yesterday onward).
    cutoff = (datetime.now() - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    out = [r for r in out if r["time"] >= cutoff]
    for r in out:
        r["tickers"] = sorted(r["tickers"])  # set -> list (picklable for cache)
    return out if limit is None else out[:limit]


# Corporate-form / currency tokens stripped before a company-name news search
_DROP_TOKENS = {
    "INC", "INC.", "CORP", "CORP.", "CORPORATION", "CO", "CO.", "COMPANY", "LTD",
    "LTD.", "LIMITED", "PLC", "AG", "SE", "NV", "N.V.", "SA", "S.A.", "A/S", "AB",
    "SPA", "S.P.A.", "HOLDING", "HOLDINGS", "GROUP", "KGAA", "USD", "(PUBL)", "PUBL", "THE",
}


def _news_query(name: str) -> str:
    """Turn a long company name into a search-friendly query by dropping legal-form
    and currency tokens (e.g. 'Novo Nordisk A/S' -> 'Novo Nordisk', 'Ethereum USD' -> 'Ethereum')."""
    kept = [w for w in name.replace(",", " ").split() if w.upper() not in _DROP_TOKENS]
    return " ".join(kept).strip() or name


def _search_news(query: str, count: int) -> list[dict]:
    try:
        return yf.Search(query, news_count=count).news or []
    except Exception:
        return []


def validate_ticker(ticker: str) -> dict:
    """Look up a ticker on Yahoo and return its metadata plus a freshness check."""
    try:
        t = yf.Ticker(ticker)
        fast = t.fast_info
        info = t.info

        last_price = fast.last_price
        if not last_price:
            return {"ok": False, "error": "No price data found for this symbol."}

        name     = info.get("longName") or info.get("shortName")
        exchange = info.get("fullExchangeName") or info.get("exchange")
        currency = fast.currency or info.get("currency") or ""

        reg_time = info.get("regularMarketTime")
        if reg_time:
            last_update = datetime.fromtimestamp(reg_time)
            age_days = (datetime.now() - last_update).days
            fresh = age_days <= 5          # updated within the last ~trading week
            ts_str = last_update.strftime("%Y-%m-%d %H:%M")
        else:
            fresh, ts_str = False, "—"

        return {
            "ok": True,
            "ticker": ticker,
            "name": name or "(name unavailable)",
            "exchange": exchange or "(exchange unavailable)",
            "price": last_price,
            "currency": currency,
            "last_update": ts_str,
            "fresh": fresh,
        }
    except Exception as e:
        return {"ok": False, "error": f"Lookup failed: {e}"}


def fmt_pct(val):
    if val is None or pd.isna(val):
        return "—"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.2f}%"

def fmt_price(val):
    if val is None or pd.isna(val):
        return "—"
    return f"{val:,.2f}"


def is_stale(last_update) -> bool:
    """True if the quote's Last Update is not from today (or missing)."""
    today = datetime.now().strftime("%Y-%m-%d")
    return not (isinstance(last_update, str) and last_update.startswith(today))


STALE_BG = "#e6e9ee"   # light grey for not-updated-today rows

# Yahoo ticker suffix → TradingView exchange prefix
TV_EXCHANGE_BY_SUFFIX = {
    "DE": "XETR", "F": "FWB", "AS": "EURONEXT", "PA": "EURONEXT", "BR": "EURONEXT",
    "LS": "EURONEXT", "L": "LSE", "CO": "OMXCOP", "HE": "OMXHEX", "ST": "OMXSTO",
    "OL": "OSL", "MI": "MIL", "MC": "BME", "SW": "SIX", "VI": "VIE",
    "HK": "HKEX", "T": "TSE", "TO": "TSX", "V": "TSXV", "AX": "ASX", "NZ": "NZX",
    "SI": "SGX", "KS": "KRX", "TW": "TWSE", "SS": "SSE", "SZ": "SZSE", "BO": "BSE", "NS": "NSE",
}
# US exchange name (from Yahoo) → TradingView exchange prefix
TV_EXCHANGE_US = {
    "NasdaqGS": "NASDAQ", "NasdaqGM": "NASDAQ", "NasdaqCM": "NASDAQ",
    "NYSE": "NYSE", "NYSEArca": "AMEX", "NYSE American": "AMEX", "NYSEAmerican": "AMEX",
}


def tradingview_url(ticker: str, exchange: str) -> str:
    """Best-effort TradingView chart URL for a Yahoo ticker."""
    t = (ticker or "").upper()
    if not t:
        return ""
    base_url = "https://www.tradingview.com/chart/?symbol="
    if t.endswith("-USD"):                       # crypto, e.g. BTC-USD → BTCUSD
        return base_url + t.replace("-", "")
    if "." in t:                                 # international, e.g. SAP.DE
        base, suffix = t.rsplit(".", 1)
        base = base.replace("-", "_")            # TV uses underscores for share classes
        prefix = TV_EXCHANGE_BY_SUFFIX.get(suffix)
        return base_url + (f"{prefix}:{base}" if prefix else base)
    prefix = TV_EXCHANGE_US.get(exchange)        # US / no suffix
    return base_url + (f"{prefix}:{t}" if prefix else t)


def top5_panel(df: pd.DataFrame, title: str, ascending: bool):
    subset = df.dropna(subset=["Change %"]).sort_values("Change %", ascending=ascending).head(5)
    accent = "#d9534f" if ascending else "#28a745"   # red losers / green winners
    bg_card = "#fff5f5" if ascending else "#f3faf4"
    key = "losers" if ascending else "winners"

    # ── Desktop view: rich inline cards (hidden on small screens via CSS) ───────
    rows_html = ""
    for _, row in subset.iterrows():
        pct = row["Change %"]
        up = pct >= 0
        row_accent = "#28a745" if up else "#d9534f"
        arrow = "▲" if up else "▼"
        row_bg = STALE_BG if is_stale(row.get("Last Update")) else "#ffffff"
        tv_url = tradingview_url(row["Ticker"], row.get("Exchange", ""))
        rows_html += f"""
        <div style="display:flex; justify-content:space-between; align-items:center;
                    padding:7px 12px; margin-bottom:5px;
                    background:{row_bg}; border-radius:8px;
                    border-left:4px solid {row_accent};
                    box-shadow:0 1px 3px rgba(0,0,0,.06)">
            <a href="{tv_url}" target="_blank" title="Open chart on TradingView"
               style="font-weight:700; color:#1a1a2e; min-width:70px; text-decoration:none">{row['Ticker']} ↗</a>
            <span style="color:#666; font-size:.86em; flex:1.4; padding:0 10px; overflow:hidden; white-space:nowrap; text-overflow:ellipsis">{row.get('Name', '')}</span>
            <span style="color:#888; font-size:.78em; flex:1; overflow:hidden; white-space:nowrap; text-overflow:ellipsis">{row.get('Exchange', '')}</span>
            <span style="color:#444; font-size:.9em; white-space:nowrap; min-width:95px; text-align:right">{fmt_price(row['Price'])} {row['Currency']}</span>
            <span style="color:{row_accent}; font-weight:700; min-width:70px; text-align:right">{arrow} {fmt_pct(pct)}</span>
            <span style="color:#999; font-size:.74em; white-space:nowrap; min-width:95px; text-align:right">{row.get('Last Update', '')}</span>
        </div>"""

    st.markdown(
        f"""
        <div class="tv-desktop" style="background:{bg_card}; border:1.5px solid {accent};
                    border-radius:14px; padding:16px 18px; box-shadow:0 2px 8px rgba(0,0,0,.08)">
            <div style="font-size:1.05em; font-weight:700; color:{accent}; margin-bottom:10px">{title}</div>
            {rows_html if rows_html else '<div style="color:#888">No data.</div>'}
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Mobile view: compact rows + tap-to-expand popovers (hidden on desktop) ──
    with st.container(key=f"tvmob-{key}", border=True):
        st.markdown(
            f"<div style='font-size:1.05em; font-weight:700; color:{accent}; "
            f"margin-bottom:22px'>{title}</div>",
            unsafe_allow_html=True,
        )
        if subset.empty:
            st.caption("No data.")
        for _, row in subset.iterrows():
            pct = row["Change %"]
            up = pct >= 0
            dot = "🟢" if up else "🔴"
            arrow = "▲" if up else "▼"
            color = "#28a745" if up else "#d9534f"
            label = f"{dot}  {row['Ticker']}   {arrow} {fmt_pct(pct)}"
            with st.popover(label, use_container_width=True):
                st.markdown(f"### {row['Ticker']}")
                st.markdown(f"**{row.get('Name', '')}**")
                st.markdown(
                    f"- **Exchange:** {row.get('Exchange', '—')}\n"
                    f"- **Price:** {fmt_price(row['Price'])} {row['Currency']}\n"
                    f"- **Change:** <span style='color:{color}; font-weight:700'>"
                    f"{arrow} {fmt_pct(pct)}</span>\n"
                    f"- **Last update:** {row.get('Last Update', '—')}",
                    unsafe_allow_html=True,
                )
                st.link_button(
                    "Open chart on TradingView ↗",
                    tradingview_url(row["Ticker"], row.get("Exchange", "")),
                    use_container_width=True,
                )


def news_panel(items: list[dict], max_height: int | None = None):
    """Render a bordered news card: each article links out and is tagged with the
    watchlist ticker(s) it relates to. `max_height` caps the box (with internal
    scroll) so it never runs longer than the table beside it."""
    rows_html = ""
    for it in items:
        title = html.escape(it.get("title", "") or "(untitled)")
        link = html.escape(it.get("link", "") or "#", quote=True)
        publisher = html.escape(it.get("publisher", "") or "")
        ts = it.get("time", 0)
        when = datetime.fromtimestamp(ts).strftime("%b %d, %H:%M") if ts else ""
        meta = " · ".join(p for p in (publisher, when) if p)
        chips = "".join(
            f"""<span style="display:inline-block; background:#e8f0fb; color:#1a4a7a;
                 font-size:.72em; font-weight:700; padding:1px 7px; margin:2px 4px 0 0;
                 border-radius:10px">{html.escape(t)}</span>"""
            for t in it.get("tickers", [])
        )
        rows_html += f"""
        <div style="padding:9px 2px; border-bottom:1px solid #e3e9f0">
            <a href="{link}" target="_blank" rel="noopener"
               style="font-weight:600; color:#1a1a2e; text-decoration:none;
                      font-size:.9em; line-height:1.3">{title}</a>
            <div style="color:#888; font-size:.74em; margin-top:3px">{meta}</div>
            <div style="margin-top:2px">{chips}</div>
        </div>"""

    cap = f"max-height:{max_height}px; overflow-y:auto;" if max_height else ""
    st.markdown(
        f"""
        <div style="background:#ffffff; border:1.5px solid #c3cfdd; border-radius:14px;
                    padding:14px 16px; {cap}
                    box-shadow:0 2px 8px rgba(0,0,0,.08)">
            <div style="font-size:1.05em; font-weight:700; color:#1a1a2e;
                        margin-bottom:6px">📰 News{f" ({len(items)})" if items else ""}</div>
            {rows_html if rows_html else '<div style="color:#888; padding:8px 0">No recent news.</div>'}
        </div>
        """,
        unsafe_allow_html=True,
    )


# ── Screens ───────────────────────────────────────────────────────────────────
STATUS_LABELS = {"active": "🟢 Held", "target": "🎯 Target", "info": "ℹ️ Info"}
STATUS_COLORS = {"active": "#28a745", "target": "#4a90d9", "info": "#6c757d"}
STATUS_ICONS = {"active": "🟢", "target": "🎯", "info": "ℹ️"}
STATUS_OPTIONS = ["target", "active", "info"]


def render_quote_table(frame: pd.DataFrame, tags_map: dict[str, list[str]],
                       type_map: dict[str, list[str]], detail: bool, key_suffix: str):
    """Render a quote table. Desktop shows all columns; mobile shows Name + Change %
    (or all columns when `detail` is on). The two are shown/hidden via CSS."""
    display = frame.copy()
    display["Price"] = display.apply(
        lambda r: f"{fmt_price(r['Price'])} {r['Currency']}".strip() if r["Price"] else "—", axis=1
    )
    display["Type"] = display["Ticker"].map(
        lambda t: " ".join(
            STATUS_ICONS[s] for s in sorted(type_map.get(t, []), key=STATUS_OPTIONS.index)
        )
    )
    display["Tags"] = display["Ticker"].map(lambda t: tags_map.get(t, []))
    # Make the ticker itself the clickable link. The cell value is a TradingView URL;
    # we append the real Yahoo ticker as a harmless query param so the LinkColumn's
    # regex display_text can show it as the label (e.g. "AAPL", "SAP.DE"). Computed
    # last, after Type/Tags have read the plain ticker above.
    display["Ticker"] = display.apply(
        lambda r: f"{tradingview_url(r['Ticker'], r['Exchange'])}&yfsym={r['Ticker']}", axis=1
    )
    display = display[["Name", "Ticker", "Exchange", "Price", "Change %", "Type", "Tags", "Last Update"]]
    display = display.sort_values("Change %", ascending=True, na_position="last")

    full_cols = ["Name", "Ticker", "Exchange", "Price", "Change %", "Type", "Tags", "Last Update"]
    full_cfg = {
        "Ticker": st.column_config.LinkColumn(
            "Ticker", display_text=r"yfsym=(.+)$", help="Open chart on TradingView ↗"
        ),
        "Type": st.column_config.TextColumn("Type", help="Watchlist type(s) this stock belongs to"),
        "Tags": st.column_config.ListColumn("Tags", help="Assigned tags"),
    }

    def _pct_color(v):
        if pd.isna(v):
            return ""
        return f"color: {'#28a745' if v >= 0 else '#d9534f'}; font-weight: bold"

    def _stale_row(row):
        # Look up freshness from the full frame by index so it works for any column subset
        bg = f"background-color: {STALE_BG}" if is_stale(display.loc[row.name, "Last Update"]) else ""
        return [bg] * len(row)

    def _make_styled(cols):
        d = display[cols]
        right = [c for c in ("Price", "Change %") if c in cols]
        return (
            d.style
            .apply(_stale_row, axis=1)
            .format({"Change %": lambda v: "—" if pd.isna(v) else f"{'+' if v >= 0 else ''}{v:.2f}%"})
            .map(_pct_color, subset=["Change %"])
            .set_properties(**{"text-align": "right"}, subset=right)
            .hide(axis="index")
        )

    rows = min(len(display), 50)
    height = (rows + 1) * 35 + 3

    # Desktop: all columns (hidden on mobile via CSS)
    with st.container(key=f"tbldesk-{key_suffix}"):
        st.dataframe(_make_styled(full_cols), use_container_width=True, height=height,
                     hide_index=True, column_config=full_cfg)

    # Mobile: Name + Change % only, or all columns when detail is on (hidden on desktop)
    with st.container(key=f"tblmob-{key_suffix}"):
        if detail:
            st.dataframe(_make_styled(full_cols), use_container_width=True, height=height,
                         hide_index=True, column_config=full_cfg)
        else:
            st.dataframe(_make_styled(["Name", "Change %"]), use_container_width=True,
                         height=height, hide_index=True)


def render_monitor(selected_list: str, watchlists: dict[str, list[str]]):
    if selected_list == ALL_LABEL:
        seen = set()
        tickers = [t for lst in watchlists.values() for t in lst if not (t in seen or seen.add(t))]
    else:
        tickers = watchlists.get(selected_list, [])

    st.title(selected_list)

    # Watchlist-level status badge (not shown for the aggregate "All" view)
    if selected_list != ALL_LABEL:
        status = get_watchlist_statuses().get(selected_list, "target")
        st.markdown(
            f"""<span style="display:inline-block; background:{STATUS_COLORS[status]};
                color:#fff; font-size:.82em; font-weight:700; padding:2px 10px;
                border-radius:12px">{STATUS_LABELS[status]}</span>""",
            unsafe_allow_html=True,
        )

    if not tickers:
        st.info("This watchlist is empty. Add stocks on the Edit watchlists screen.")
        return

    with st.spinner("Fetching quotes…"):
        df = fetch_quotes(tickers)

    # Name / exchange / currency come from the DB (maintained via the Names tab and the
    # populate_names.py enrichment script), falling back to the ticker when absent.
    meta = get_ticker_meta()

    def _meta(t, idx, fallback):
        v = meta.get(t)
        return v[idx] if (v and v[idx]) else fallback

    df["Name"]     = df["Ticker"].map(lambda t: _meta(t, 0, t))
    df["Exchange"] = df["Ticker"].map(lambda t: _meta(t, 1, "—"))
    df["Currency"] = df["Ticker"].map(lambda t: _meta(t, 2, ""))

    st.caption(f"Data as of {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Per-list default control states (applied once; the user can still change them)
    st.session_state.setdefault(f"only_today::{selected_list}", selected_list == ALL_LABEL)
    st.session_state.setdefault(f"groupby::{selected_list}", selected_list == "Indices")
    st.session_state.setdefault(f"groupwl::{selected_list}", selected_list == ALL_LABEL)
    if selected_list == ALL_LABEL:
        st.session_state.setdefault("list_status_filter", ["🟢 Held"])

    # The three groupings are mutually exclusive — turning one on turns the others off
    tag_key = f"groupby::{selected_list}"
    type_key = f"grouptype::{selected_list}"
    wl_key = f"groupwl::{selected_list}"

    def _excl_tag():
        if st.session_state.get(tag_key):
            st.session_state[type_key] = False
            st.session_state[wl_key] = False

    def _excl_type():
        if st.session_state.get(type_key):
            st.session_state[tag_key] = False
            st.session_state[wl_key] = False

    def _excl_wl():
        if st.session_state.get(wl_key):
            st.session_state[tag_key] = False
            st.session_state[type_key] = False

    # The watchlist filter / grouping only make sense on the aggregate "All" view.
    is_all = selected_list == ALL_LABEL
    if is_all:
        c_today, c_status, c_grouptype, c_wlfilter, c_groupwl, c_tags, c_group = st.columns(
            [2, 2, 2, 2, 2, 2, 2]
        )
    else:
        c_today, c_status, c_grouptype, c_tags, c_group = st.columns([2, 2, 2, 3, 2])

    with c_today:
        only_today = st.toggle("Show only stocks updated today", key=f"only_today::{selected_list}")
    with c_status:
        status_filter = (
            st.multiselect(
                "Filter by type",
                ["🎯 Target", "🟢 Held", "ℹ️ Info"],
                key="list_status_filter",
                placeholder="Filter by type",
                label_visibility="collapsed",
            )
            if is_all else []
        )
    with c_grouptype:
        group_by_type = st.toggle("Group by type", key=type_key, on_change=_excl_type)
    if is_all:
        with c_wlfilter:
            wl_filter = st.multiselect(
                "Filter by watchlist",
                list(watchlists.keys()),
                key=f"wlfilter::{selected_list}",
                placeholder="Filter by watchlist",
                label_visibility="collapsed",
            )
        with c_groupwl:
            group_by_watchlist = st.toggle("Group by watchlist", key=wl_key, on_change=_excl_wl)
    else:
        wl_filter = []
        group_by_watchlist = False
    with c_tags:
        tag_filter = st.multiselect(
            "Filter by tags",
            get_all_tags(),
            key=f"tagfilter::{selected_list}",
            placeholder="Filter by tags",
            label_visibility="collapsed",
        )
    with c_group:
        group_by_tag = st.toggle("Group by tag", key=tag_key, on_change=_excl_tag)

    tags_map = get_tags_map()

    # ticker → the status(es) of the watchlist(s) it belongs to (for the Type column)
    all_statuses = get_watchlist_statuses()
    type_map: dict[str, list[str]] = {}
    for name, lst in watchlists.items():
        s = all_statuses.get(name, "target")
        for t in lst:
            type_map.setdefault(t, [])
            if s not in type_map[t]:
                type_map[t].append(s)

    if only_today:
        df = df[~df["Last Update"].apply(is_stale)].reset_index(drop=True)

    if selected_list == ALL_LABEL and status_filter:
        label_to_status = {"🟢 Held": "active", "🎯 Target": "target", "ℹ️ Info": "info"}
        wanted = {label_to_status[s] for s in status_filter}
        statuses = get_watchlist_statuses()
        allowed = {
            t for name, lst in watchlists.items()
            if statuses.get(name, "target") in wanted for t in lst
        }
        df = df[df["Ticker"].isin(allowed)].reset_index(drop=True)

    if tag_filter:
        wanted_tags = set(tag_filter)
        df = df[df["Ticker"].map(lambda t: bool(set(tags_map.get(t, [])) & wanted_tags))].reset_index(drop=True)

    if wl_filter:
        allowed_wl = {t for name in wl_filter for t in watchlists.get(name, [])}
        df = df[df["Ticker"].isin(allowed_wl)].reset_index(drop=True)

    if df.empty:
        st.info("No stocks match the current filters.")
        return

    col_losers, col_winners = st.columns(2, gap="large")
    with col_losers:
        top5_panel(df, "Top 5 Losers", ascending=True)
    with col_winners:
        top5_panel(df, "Top 5 Winners", ascending=False)

    st.markdown("<div style='margin-top:24px'></div>", unsafe_allow_html=True)

    def _safe(s: str) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "-", str(s)).strip("-") or "x"

    # Table on the left, Yahoo Finance news on the right. On phones the columns
    # stack, so the news box drops underneath the table (col_table comes first).
    # The table column is rendered first and the news is fetched inside the news
    # column, so the table shows immediately while the news is still loading. The
    # column layout is fixed up front, so this doesn't disturb the alignment.
    news_entries = tuple(sorted(zip(df["Ticker"], df["Name"])))
    col_table, col_news = st.columns([2, 1], gap="medium")

    with col_table:
        # Mobile-only toggle to expand the reduced phone table to all columns
        # (hidden on desktop via CSS, so it does not offset the table there).
        with st.container(key="tbldetail"):
            detail = st.toggle("Show all columns", key=f"tbldetail::{selected_list}")

        if group_by_type:
            # One table per watchlist type present in the view
            for status in STATUS_OPTIONS:
                subset = df[df["Ticker"].map(lambda t: status in type_map.get(t, []))]
                if subset.empty:
                    continue
                st.subheader(f"{STATUS_LABELS[status]}  ·  {len(subset)}")
                render_quote_table(subset, tags_map, type_map, detail, f"{_safe(selected_list)}-type-{status}")
        elif group_by_tag:
            # One table per tag. Which tags: the active filter, else every tag present.
            if tag_filter:
                group_tags = list(tag_filter)
            else:
                present = set()
                for t in df["Ticker"]:
                    present.update(tags_map.get(t, []))
                group_tags = sorted(present)

            for tag in group_tags:
                subset = df[df["Ticker"].map(lambda t: tag in tags_map.get(t, []))]
                if subset.empty:
                    continue
                st.subheader(f"🏷️ {tag}  ·  {len(subset)}")
                render_quote_table(subset, tags_map, type_map, detail, f"{_safe(selected_list)}-tag-{_safe(tag)}")

            # Stocks with no tags (only when not filtering by specific tags)
            if not tag_filter:
                untagged = df[df["Ticker"].map(lambda t: not tags_map.get(t))]
                if not untagged.empty:
                    st.subheader(f"Untagged  ·  {len(untagged)}")
                    render_quote_table(untagged, tags_map, type_map, detail, f"{_safe(selected_list)}-untagged")
        elif group_by_watchlist:
            # One table per watchlist. Which watchlists: the active filter, else all.
            group_wls = wl_filter if wl_filter else list(watchlists.keys())
            for name in group_wls:
                subset = df[df["Ticker"].isin(set(watchlists.get(name, [])))]
                if subset.empty:
                    continue
                icon = STATUS_ICONS.get(all_statuses.get(name, "target"), "🎯")
                st.subheader(f"{icon} {name}  ·  {len(subset)}")
                render_quote_table(subset, tags_map, type_map, detail, f"{_safe(selected_list)}-wl-{_safe(name)}")
        else:
            render_quote_table(df, tags_map, type_map, detail, f"{_safe(selected_list)}-main")

    with col_news:
        with st.spinner("Fetching news…"):
            news_items = fetch_news(news_entries)
        tickers_with_news = sorted({t for it in news_items for t in it["tickers"]})

        # Stock picker at the top of the news column (aligns with the table top); it
        # only offers stocks that actually have news, and filters the news box below.
        news_pick = st.multiselect(
            "Filter news by stock",
            tickers_with_news,
            key=f"news_pick::{selected_list}",
            placeholder="Filter news by stock",
            label_visibility="collapsed",
            disabled=not tickers_with_news,
        )
        if news_pick:
            sel = set(news_pick)
            shown_news = [it for it in news_items if sel & set(it["tickers"])]
        else:
            shown_news = news_items

        # Cap the news box to the table's height so it never runs longer than the table.
        rows = min(len(df), 50)
        table_height = (rows + 1) * 35 + 3
        news_panel(shown_news, max_height=table_height)


def render_edit_screen(watchlists: dict[str, list[str]]):
    st.title("Edit watchlists")

    tab_create, tab_rename, tab_reorder, tab_add, tab_status, tab_tags, tab_names, tab_del_item, tab_del_list = st.tabs(
        ["Create list", "Rename list", "Reorder", "Add stock", "Set status", "Tags", "Names", "Remove stock", "Delete list"]
    )

    # ── Create a new watchlist ─────────────────────────────────────────────────
    with tab_create:
        with st.form("new_list_form", clear_on_submit=True):
            new_name = st.text_input("New watchlist name")
            new_status = st.radio(
                "Type",
                STATUS_OPTIONS,
                format_func=lambda s: STATUS_LABELS[s],
                horizontal=True,
            )
            if st.form_submit_button("Create", type="primary"):
                name = new_name.strip()
                if not name:
                    st.warning("Enter a name.")
                elif create_watchlist(name, new_status):
                    st.success(f"Created “{name}”.")
                    st.rerun()
                else:
                    st.error(f"“{name}” already exists.")

    # ── Rename a watchlist ─────────────────────────────────────────────────────
    with tab_rename:
        if not watchlists:
            st.info("No watchlists to rename.")
        else:
            old_name = st.selectbox("Watchlist", list(watchlists.keys()), key="rename_target")
            renamed = st.text_input("New name", value=old_name, key="rename_value")
            if st.button("Rename", type="primary"):
                clean = renamed.strip()
                if not clean:
                    st.warning("Enter a name.")
                elif clean == old_name:
                    st.info("Name unchanged.")
                elif rename_watchlist(old_name, clean):
                    st.success(f"Renamed “{old_name}” to “{clean}”.")
                    st.rerun()
                else:
                    st.error(f"“{clean}” already exists.")

    # ── Reorder watchlists ─────────────────────────────────────────────────────
    with tab_reorder:
        names = list(watchlists.keys())
        if not names:
            st.info("No watchlists to reorder.")
        else:
            st.caption("Use the arrows to change the order shown in the left menu.")
            for i, name in enumerate(names):
                c_name, c_up, c_down = st.columns([6, 1, 1])
                c_name.markdown(f"**{i + 1}.** {name}")
                if c_up.button("↑", key=f"mv_up_{name}", disabled=(i == 0)):
                    names[i - 1], names[i] = names[i], names[i - 1]
                    set_watchlist_order(names)
                    st.rerun()
                if c_down.button("↓", key=f"mv_down_{name}", disabled=(i == len(names) - 1)):
                    names[i + 1], names[i] = names[i], names[i + 1]
                    set_watchlist_order(names)
                    st.rerun()

    # ── Add a stock (with Yahoo validation) ────────────────────────────────────
    with tab_add:
        if not watchlists:
            st.info("Create a watchlist first.")
        else:
            target_list = st.selectbox("Watchlist", list(watchlists.keys()), key="add_target")
            new_ticker = st.text_input("Ticker symbol", placeholder="e.g. AAPL", key="add_ticker")

            if st.button("Check symbol"):
                ticker = new_ticker.strip().upper()
                if not ticker:
                    st.session_state.pop("pending_add", None)
                    st.warning("Enter a ticker.")
                else:
                    with st.spinner(f"Looking up {ticker}…"):
                        st.session_state["pending_add"] = validate_ticker(ticker)

            pending = st.session_state.get("pending_add")
            if pending:
                if not pending["ok"]:
                    st.error(pending["error"])
                else:
                    freshness = (
                        "🟢 Regularly updated"
                        if pending["fresh"]
                        else "🟠 Not updated recently — may be stale/delisted"
                    )
                    st.markdown(
                        f"""
                        <div style="background:#ffffff; border:1.5px solid #4a90d9;
                                    border-radius:10px; padding:12px 14px; margin:6px 0;
                                    box-shadow:0 1px 4px rgba(0,0,0,.08); max-width:420px">
                            <div style="font-weight:700; color:#1a1a2e">{pending['name']}</div>
                            <div style="color:#555; font-size:.9em; margin-top:3px">{pending['ticker']} · {pending['exchange']}</div>
                            <div style="color:#555; font-size:.9em; margin-top:3px">{fmt_price(pending['price'])} {pending['currency']}</div>
                            <div style="color:#888; font-size:.82em; margin-top:3px">Last update: {pending['last_update']}</div>
                            <div style="font-size:.85em; margin-top:6px">{freshness}</div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                    c1, c2, _ = st.columns([1, 1, 3])
                    with c1:
                        if st.button("Confirm add", type="primary"):
                            if add_item(target_list, pending["ticker"]):
                                st.success(f"Added {pending['ticker']} to “{target_list}”.")
                                st.session_state.pop("pending_add", None)
                                st.cache_data.clear()
                                st.rerun()
                            else:
                                st.error(f"{pending['ticker']} is already in “{target_list}”.")
                    with c2:
                        if st.button("Cancel"):
                            st.session_state.pop("pending_add", None)
                            st.rerun()

    # ── Set a watchlist's status (held vs target) ──────────────────────────────
    with tab_status:
        if not watchlists:
            st.info("No watchlists to update.")
        else:
            statuses = get_watchlist_statuses()
            s_list = st.selectbox(
                "Watchlist",
                list(watchlists.keys()),
                format_func=lambda n: f"{n}  ({STATUS_LABELS[statuses.get(n, 'target')]})",
                key="status_list",
            )
            new_status = st.radio(
                "Type",
                STATUS_OPTIONS,
                index=STATUS_OPTIONS.index(statuses.get(s_list, "target"))
                if statuses.get(s_list, "target") in STATUS_OPTIONS else 0,
                format_func=lambda s: STATUS_LABELS[s],
                horizontal=True,
                key="status_value",
            )
            if st.button("Save status", type="primary"):
                set_watchlist_status(s_list, new_status)
                st.success(f"“{s_list}” marked as {STATUS_LABELS[new_status]}.")
                st.rerun()

    # ── Assign tags to a stock ─────────────────────────────────────────────────
    with tab_tags:
        non_empty = {k: v for k, v in watchlists.items() if v}
        if not non_empty:
            st.info("No stocks to tag.")
        else:
            tag_list = st.selectbox("Watchlist", list(non_empty.keys()), key="tag_list")
            tag_ticker = st.selectbox("Stock", non_empty[tag_list], key="tag_ticker")
            current = get_tags(tag_ticker)
            options = sorted(set(get_all_tags()) | set(current))
            chosen = st.multiselect(
                "Tags (type to add a new one)",
                options,
                default=current,
                accept_new_options=True,
                key=f"tag_choice::{tag_list}::{tag_ticker}",
            )
            st.caption("Deselect a tag and press Save to remove it from this stock.")
            c_save, c_clear = st.columns([1, 1])
            with c_save:
                if st.button("Save tags", type="primary"):
                    set_tags(tag_ticker, chosen)
                    label = ", ".join(chosen) if chosen else "no tags"
                    st.success(f"{tag_ticker}: {label}.")
                    st.rerun()
            with c_clear:
                if st.button("Remove all tags", disabled=not current):
                    set_tags(tag_ticker, [])
                    st.success(f"Removed all tags from {tag_ticker}.")
                    st.rerun()

    # ── Maintain display names (and run the yfinance enrichment) ───────────────
    with tab_names:
        all_tickers = sorted({t for lst in watchlists.values() for t in lst})
        if not all_tickers:
            st.info("No stocks yet. Add some on the ‘Add stock’ tab.")
        else:
            st.caption(
                "Names, exchange and currency are read from the database (the app never "
                "calls yfinance for them). Edit a Name to override it, or use the button "
                "to fetch missing values from Yahoo. Blank Name falls back to the ticker."
            )

            if st.button("⬇️ Fetch missing names from Yahoo",
                         help="Runs yfinance now. Works locally; on Streamlit Cloud "
                              "Yahoo usually blocks this — run populate_names.py locally instead."):
                with st.spinner("Fetching from Yahoo…"):
                    summary = enrich_ticker_meta(get_conn())
                st.cache_data.clear()
                st.success(
                    f"Processed {summary['processed']} ticker(s): "
                    f"added {summary['added']}, failed {len(summary['failed'])}."
                )
                if summary["failed"]:
                    st.caption("Couldn't fetch: " + ", ".join(summary["failed"]))
                st.rerun()

            meta = get_ticker_meta()
            editor_df = pd.DataFrame({
                "Ticker":   all_tickers,
                "Name":     [(meta.get(t) or ("", "", ""))[0] or "" for t in all_tickers],
                "Exchange": [(meta.get(t) or ("", "", ""))[1] or "" for t in all_tickers],
                "Currency": [(meta.get(t) or ("", "", ""))[2] or "" for t in all_tickers],
            })
            edited = st.data_editor(
                editor_df,
                hide_index=True,
                use_container_width=True,
                disabled=["Ticker", "Exchange", "Currency"],
                column_config={
                    "Ticker": st.column_config.TextColumn("Ticker"),
                    "Name": st.column_config.TextColumn(
                        "Name", help="Display name (overrides the fetched one)"
                    ),
                    "Exchange": st.column_config.TextColumn("Exchange", help="Fetched; read-only"),
                    "Currency": st.column_config.TextColumn("Currency", help="Fetched; read-only"),
                },
                key="names_editor",
            )
            if st.button("Save names", type="primary"):
                current = {t: (meta.get(t) or ("", "", ""))[0] or "" for t in all_tickers}
                changed = 0
                for _, r in edited.iterrows():
                    new = (r["Name"] or "").strip()
                    if new != current.get(r["Ticker"], ""):
                        set_ticker_name(r["Ticker"], new)
                        changed += 1
                st.cache_data.clear()
                st.success(f"Saved {changed} change(s).")
                st.rerun()

    # ── Remove a stock from a watchlist ────────────────────────────────────────
    with tab_del_item:
        non_empty = {k: v for k, v in watchlists.items() if v}
        if not non_empty:
            st.info("No stocks to remove.")
        else:
            del_list = st.selectbox("Watchlist", list(non_empty.keys()), key="del_item_list")
            del_ticker = st.selectbox("Stock to remove", non_empty[del_list], key="del_item_ticker")
            if st.button("Remove stock", type="primary"):
                delete_item(del_list, del_ticker)
                st.success(f"Removed {del_ticker} from “{del_list}”.")
                st.cache_data.clear()
                st.rerun()

    # ── Delete a whole watchlist ───────────────────────────────────────────────
    with tab_del_list:
        if not watchlists:
            st.info("No watchlists to delete.")
        else:
            del_wl = st.selectbox("Watchlist to delete", list(watchlists.keys()), key="del_wl")
            count = len(watchlists.get(del_wl, []))
            st.warning(f"This will permanently delete “{del_wl}” and its {count} stock(s).")
            confirm = st.checkbox("Yes, I'm sure", key="del_wl_confirm")
            if st.button("Delete watchlist", type="primary", disabled=not confirm):
                delete_watchlist(del_wl)
                st.success(f"Deleted “{del_wl}”.")
                st.rerun()


def render_refresh_screen():
    st.title("Refresh options")
    st.caption("Control how the monitor keeps quotes up to date.")

    st.toggle("Auto-refresh", key="auto_refresh")
    if st.session_state.get("auto_refresh"):
        st.selectbox(
            "Refresh every",
            [30, 60, 120, 300],
            format_func=lambda s: f"{s} seconds",
            key="auto_interval",
        )
        st.success("Auto-refresh is on — the monitor reloads quotes on the chosen interval.")
    else:
        st.info("Auto-refresh is off. Use ‘Refresh now’ below for a one-off update.")

    st.divider()
    if st.button("🔄 Refresh now", type="primary"):
        st.cache_data.clear()
        st.toast("Quotes refreshed.")


# ── Performance Comparison ──────────────────────────────────────────────────────

def get_perf_presets() -> dict[str, dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT name, tickers, main_ticker FROM perf_presets ORDER BY name").fetchall()
    return {
        name: {"tickers": tickers.split(","), "main_ticker": main_ticker}
        for name, tickers, main_ticker in rows
    }


def save_perf_preset(name: str, tickers: list[str], main_ticker: str | None = None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO perf_presets (name, tickers, main_ticker) VALUES (?, ?, ?)
            ON CONFLICT (name) DO UPDATE SET tickers = excluded.tickers, main_ticker = excluded.main_ticker
            """,
            [name, ",".join(tickers), main_ticker or None],
        )


def delete_perf_preset(name: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM perf_presets WHERE name = ?", [name])

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


def _calc_performance(series: pd.Series) -> dict[str, float | None]:
    """Return YTD, 1Y, 5Y CAGR, 10Y CAGR, 15Y CAGR as decimals (0.10 = +10%)."""
    if series.empty:
        return {"YTD": None, "1Y": None, "5Y CAGR": None, "10Y CAGR": None, "15Y CAGR": None}

    today = series.index[-1]
    current = float(series.iloc[-1])

    def _prev(target) -> float | None:
        mask = series.index <= pd.Timestamp(target)
        return float(series[mask].iloc[-1]) if mask.any() else None

    ytd_p  = _prev(pd.Timestamp(today.year, 1, 1))
    one_p  = _prev(today - pd.DateOffset(years=1))
    five_p = _prev(today - pd.DateOffset(years=5))
    ten_p   = _prev(today - pd.DateOffset(years=10))
    fifte_p = _prev(today - pd.DateOffset(years=15))

    return {
        "YTD":       (current / ytd_p  - 1)                  if ytd_p   else None,
        "1Y":        (current / one_p  - 1)                   if one_p   else None,
        "5Y CAGR":   (current / five_p) ** (1 / 5)  - 1      if five_p  else None,
        "10Y CAGR":  (current / ten_p)  ** (1 / 10) - 1      if ten_p   else None,
        "15Y CAGR":  (current / fifte_p) ** (1 / 15) - 1     if fifte_p else None,
    }


def _point_cagr(series: pd.Series, years: int) -> float | None:
    """Plain point-to-point CAGR: last price vs the price `years` ago."""
    if series.empty:
        return None
    end_date = series.index[-1]
    target = end_date - pd.DateOffset(years=years)
    if target < series.index[0]:
        return None
    p0 = float(series.asof(target))
    p1 = float(series.iloc[-1])
    if not p0 or p0 <= 0:
        return None
    return (p1 / p0) ** (1 / years) - 1


def _endpoint_avg_cagr(series: pd.Series, years: int, win: int = 21) -> float | None:
    """Endpoint-averaged CAGR: averages ~1 trading month around each endpoint so a
    single freak price on the start or end day cannot dominate the result."""
    if series.empty:
        return None
    end_date = series.index[-1]
    target = end_date - pd.DateOffset(years=years)
    if target < series.index[0]:
        return None
    end_avg = float(series.iloc[-win:].mean())
    pos = series.index.get_indexer([pd.Timestamp(target)], method="nearest")[0]
    lo, hi = max(0, pos - win // 2), min(len(series), pos + win // 2 + 1)
    start_avg = float(series.iloc[lo:hi].mean())
    if not start_avg or start_avg <= 0:
        return None
    return (end_avg / start_avg) ** (1 / years) - 1


def _rolling_median_cagr(series: pd.Series, years: int) -> tuple[float | None, int]:
    """Median of the `years`-length CAGR computed from every monthly start date in
    history. Answers 'what was a typical N-year outcome?', independent of one lucky
    or unlucky start date. Returns (median, number_of_windows_used)."""
    if series.empty:
        return None, 0
    end_date = series.index[-1]
    last_start = end_date - pd.DateOffset(years=years)
    # Underlying data is daily closes; we sample start points at month-end cadence
    # ("ME" labels each bucket at its month-end date, so the start price and the
    # start+years lookup are both anchored to month-end and stay aligned).
    monthly = series.resample("ME").last().dropna()
    starts = monthly.index[monthly.index <= last_start]
    cagrs = []
    for t in starts:
        p0 = float(monthly.loc[t])
        p1 = series.asof(t + pd.DateOffset(years=years))
        if p0 and p0 > 0 and p1 and not pd.isna(p1):
            cagrs.append((float(p1) / p0) ** (1 / years) - 1)
    if not cagrs:
        return None, 0
    return float(pd.Series(cagrs).median()), len(cagrs)


def _period_start(series: pd.Series, period: str) -> pd.Timestamp:
    """Map a period label (YTD / 1Y / 5Y CAGR / …) to a start date for the series."""
    end = series.index[-1]
    if period == "YTD":
        return pd.Timestamp(end.year, 1, 1)
    years = {"1Y": 1, "5Y CAGR": 5, "10Y CAGR": 10, "15Y CAGR": 15}[period]
    return end - pd.DateOffset(years=years)


def _max_drawdown(series: pd.Series) -> float | None:
    """Largest peak-to-trough decline over the whole series (negative, e.g. -0.55)."""
    if series.empty:
        return None
    dd = series / series.cummax() - 1.0
    return float(dd.min())


def _avg_drawdown(series: pd.Series) -> float | None:
    """Mean of the daily below-peak depth (negative). Small when the stock sits near
    new highs most of the time, large when it spends long stretches deep underwater —
    so one freak crash scores very differently from chronic deep corrections."""
    if series.empty:
        return None
    dd = series / series.cummax() - 1.0
    return float(dd.mean())


def _annualized_vol(series: pd.Series) -> float | None:
    """Std dev of daily returns, annualized (×√252)."""
    if series.empty or len(series) < 2:
        return None
    rets = series.pct_change().dropna()
    if rets.empty:
        return None
    return float(rets.std() * (252 ** 0.5))


@st.cache_data(ttl=3600, show_spinner=False)
def _screen_candidates(region: str, min_mcap: float, sectors: tuple[str, ...], limit: int) -> list[tuple[str, str]]:
    """Build a candidate pool from Yahoo's equity screener: stocks in `region` above
    `min_mcap` market cap (optionally in `sectors`), largest first. Returns
    (symbol, name) pairs. Yahoo filters on fundamentals only — the CAGR/drawdown
    scoring happens locally afterwards."""
    from yfinance import EquityQuery as EQ

    filters = [
        EQ("eq", ["region", region]),
        EQ("gt", ["intradaymarketcap", float(min_mcap)]),
    ]
    if sectors:
        if len(sectors) == 1:
            filters.append(EQ("eq", ["sector", sectors[0]]))
        else:
            filters.append(EQ("or", [EQ("eq", ["sector", s]) for s in sectors]))
    query = EQ("and", filters)

    out: list[tuple[str, str]] = []
    offset, page = 0, min(100, limit)
    while len(out) < limit:
        try:
            r = yf.screen(query, offset=offset, size=page,
                          sortField="intradaymarketcap", sortAsc=False)
        except Exception:
            break
        quotes = r.get("quotes", []) if isinstance(r, dict) else []
        if not quotes:
            break
        for qd in quotes:
            sym = qd.get("symbol")
            if sym:
                out.append((sym, qd.get("shortName") or qd.get("longName") or sym))
        offset += page
        if offset >= (r.get("total") or 0):
            break
    return out[:limit]



import altair as alt


@st.fragment
def _perf_charts(df: pd.DataFrame, periods: list[str], main_ticker: str):
    ctrl_col, lbl_col = st.columns([3, 1], vertical_alignment="bottom")
    with ctrl_col:
        period = st.radio("Period", periods, horizontal=True, key="perf_period_radio")
    with lbl_col:
        show_labels = st.toggle("Value labels", value=False)

    chart_df = df[[period]].dropna().copy()
    if chart_df.empty:
        st.caption(f"No {period} data available for the selected stocks.")
        return

    chart_df[period] = chart_df[period] * 100
    chart_df = chart_df.sort_values(period, ascending=False).reset_index()
    chart_df["Sign"] = chart_df[period].map(
        lambda x: "▲ Positive" if x >= 0 else "▼ Negative"
    )
    chart_df["Label"] = chart_df[period].map(lambda x: f"{x:+.1f}%")
    chart_df["TickerLabel"] = chart_df["Ticker"].map(
        lambda t: f"★ {t}" if t == main_ticker else t
    )
    ticker_order = chart_df["TickerLabel"].tolist()

    bar_height = max(240, len(chart_df) * 56)
    base = alt.Chart(chart_df).encode(
        y=alt.Y(
            "TickerLabel:N",
            sort=ticker_order,
            title=None,
            axis=alt.Axis(labelFontSize=13, labelFontWeight=alt.ExprRef(
                "indexof(datum.label, '★') >= 0 ? 'bold' : 'normal'"
            )),
        ),
        x=alt.X(f"{period}:Q", title="Return (%)"),
        color=alt.Color(
            "Sign:N",
            scale=alt.Scale(
                domain=["▲ Positive", "▼ Negative"],
                range=["#28a745", "#d9534f"],
            ),
            legend=alt.Legend(title=None, orient="top"),
        ),
        tooltip=[
            alt.Tooltip("Ticker:N"),
            alt.Tooltip(f"{period}:Q", format="+.2f", title="Return (%)"),
        ],
    )

    bars = base.mark_bar(cornerRadiusEnd=4).properties(height=bar_height)

    zero_rule = (
        alt.Chart(pd.DataFrame({"x": [0]}))
        .mark_rule(color="#888", strokeDash=[4, 4], strokeWidth=1.5)
        .encode(x="x:Q")
    )

    if show_labels:
        labels_pos = base.mark_text(
            align="left", dx=5, fontSize=11, fontWeight="bold", color="#333",
        ).encode(
            text="Label:N",
            x=alt.X(f"{period}:Q"),
        ).transform_filter(
            alt.datum[period] >= 0
        ).properties(height=bar_height)
        labels_neg = base.mark_text(
            align="right", dx=-5, fontSize=11, fontWeight="bold", color="#333",
        ).encode(
            text="Label:N",
            x=alt.X(f"{period}:Q"),
        ).transform_filter(
            alt.datum[period] < 0
        ).properties(height=bar_height)
        st.altair_chart(bars + zero_rule + labels_pos + labels_neg, use_container_width=True)
    else:
        st.altair_chart(bars + zero_rule, use_container_width=True)


@st.fragment
def _cagr_methods(history: dict, name_map: dict, main_ticker: str, all_tickers: list[str]):
    """Compare three ways of computing CAGR side by side for one window, so the
    start-date sensitivity of each method is visible at a glance."""
    window = st.radio("Window", ["5Y", "10Y", "15Y"], index=1, horizontal=True, key="cagr_method_window")
    years = int(window[:-1])
    methods = ["Point-to-point", "Endpoint-avg", "Rolling median"]

    rows = []
    for t in all_tickers:
        s = history.get(t)
        if s is None or s.empty:
            vals = {m: None for m in methods}
            n_windows, since = 0, None
        else:
            roll_median, n_windows = _rolling_median_cagr(s, years)
            vals = {
                "Point-to-point": _point_cagr(s, years),
                "Endpoint-avg":   _endpoint_avg_cagr(s, years),
                "Rolling median": roll_median,
            }
            since = s.index[0]
        rows.append({
            "Ticker": t, "Name": name_map.get(t, t), **vals,
            "Rolling n": n_windows,
            "Data since": since.strftime("%Y-%m-%d") if since is not None else "—",
        })

    mdf = pd.DataFrame(rows)
    mdf = mdf.sort_values("Rolling median", ascending=False, na_position="last")

    def _fmt(val):
        return "N/A" if pd.isna(val) else f"{val * 100:+.2f}%"

    def _cell_color(val):
        if pd.isna(val):
            return "color: #999"
        return f"color: {'#28a745' if val >= 0 else '#d9534f'}; font-weight: bold"

    def _row_highlight(row):
        if row.get("Ticker") == main_ticker:
            return ["background-color: #fff8e1"] * len(row)
        return [""] * len(row)

    styled = (
        mdf[["Ticker", "Name"] + methods + ["Rolling n", "Data since"]]
        .style
        .apply(_row_highlight, axis=1)
        .map(_cell_color, subset=methods)
        .format(_fmt, subset=methods, na_rep="N/A")
        .hide(axis="index")
    )
    st.dataframe(styled, use_container_width=True)
    st.caption(
        "All methods use daily closing prices. "
        "**Point-to-point**: last close vs the close exactly N years ago (one start date). "
        "**Endpoint-avg**: averages ~21 daily closes around each endpoint. "
        "**Rolling median**: median N-year CAGR over every **month-end** start date in history "
        "(**Rolling n** = how many such windows existed; **Data since** = oldest price available)."
    )


@st.fragment
def _rebased_chart(history: dict, main_ticker: str, all_tickers: list[str]):
    """Indexed growth: every stock rebased to 100 at the start of the chosen window,
    on a log axis so equal % moves are equal visual distances and the whole path —
    not just the two endpoints — is visible. Plotly so it zooms/pans."""
    import plotly.graph_objects as go

    period = st.radio(
        "Period", ["1Y", "5Y CAGR", "10Y CAGR", "15Y CAGR"],
        horizontal=True, key="rebase_period",
        format_func=lambda p: p.replace(" CAGR", ""),
    )

    fig = go.Figure()
    plotted = 0
    for t in all_tickers:
        s = history.get(t)
        if s is None or s.empty:
            continue
        start = _period_start(s, period)
        s2 = s[s.index >= start]
        if s2.empty or float(s2.iloc[0]) <= 0:
            continue
        rb = s2 / float(s2.iloc[0]) * 100
        is_main = t == main_ticker
        fig.add_trace(go.Scatter(
            x=rb.index, y=rb.values, name=t, mode="lines",
            line=dict(width=3.5 if is_main else 1.5),
            hovertemplate=f"<b>{t}</b><br>%{{x|%Y-%m-%d}}<br>Index: %{{y:.1f}}<extra></extra>",
        ))
        plotted += 1

    if plotted == 0:
        st.caption("No data available for the selected period.")
        return

    fig.update_yaxes(type="log", title="Growth (start = 100)")
    fig.update_xaxes(title=None)
    fig.update_layout(
        height=440,
        margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title=None),
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)


def render_performance_page():
    st.title("📊 Performance Comparison")

    # ── Ticker selector ───────────────────────────────────────────────────────
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT i.ticker, COALESCE(n.name, i.ticker) AS display_name
            FROM items i
            LEFT JOIN ticker_names n ON n.ticker = i.ticker
            ORDER BY i.ticker
        """).fetchall()

    if not rows:
        st.info("No tickers found in your watchlists. Add some stocks first.")
        return

    name_map = {t: name for t, name in rows}

    # ── Preset loader ─────────────────────────────────────────────────────────
    presets = get_perf_presets()
    if presets:
        p_col, load_col, del_col = st.columns([4, 1.4, 0.7])
        with p_col:
            preset_choice = st.selectbox(
                "Preset",
                options=[""] + list(presets.keys()),
                format_func=lambda x: "— select a preset —" if x == "" else x,
                key="perf_preset_choice",
                label_visibility="collapsed",
            )
        with load_col:
            if st.button("⬇ Load preset", use_container_width=True, disabled=not preset_choice):
                p = presets[preset_choice]
                valid = [t for t in p["tickers"] if t in name_map]
                st.session_state["perf_compare_tickers"] = valid
                if p["main_ticker"] and p["main_ticker"] in name_map:
                    st.session_state["perf_main_ticker"] = p["main_ticker"]
                if len(valid) < len(p["tickers"]):
                    missing = set(p["tickers"]) - set(valid)
                    st.warning(f"Skipped {len(missing)} ticker(s) no longer in your watchlists: {', '.join(sorted(missing))}")
                st.rerun()
        with del_col:
            if st.button("🗑️", use_container_width=True, disabled=not preset_choice, help="Delete this preset"):
                delete_perf_preset(preset_choice)
                st.rerun()

    # ── Save as preset ────────────────────────────────────────────────────────
    with st.expander("💾 Save current selection as preset"):
        s_col, btn_col = st.columns([3, 1], vertical_alignment="bottom")
        with s_col:
            new_name = st.text_input("Preset name", placeholder="e.g. Tech Giants", key="perf_new_preset_name")
        with btn_col:
            current_tickers = st.session_state.get("perf_compare_tickers", [])
            if st.button("Save", use_container_width=True, disabled=not (new_name.strip() and current_tickers)):
                save_perf_preset(new_name.strip(), current_tickers)
                st.success(f'Saved preset "{new_name.strip()}".')
                st.rerun()

    # ── Main stock + comparison selectors ─────────────────────────────────────
    main_col, comp_col = st.columns([1, 2])
    with main_col:
        main_ticker = st.selectbox(
            "Main stock",
            options=[""] + list(name_map.keys()),
            format_func=lambda t: "— none —" if t == "" else (f"{t} — {name_map[t]}" if name_map[t] != t else t),
            key="perf_main_ticker",
        )
    with comp_col:
        selected = st.multiselect(
            "Compare with",
            options=list(name_map.keys()),
            format_func=lambda t: f"{t} — {name_map[t]}" if name_map[t] != t else t,
            placeholder="Choose comparison stocks…",
            key="perf_compare_tickers",
        )

    include_dividends = st.toggle("Include dividends (total return)", value=True)

    # Live selection (main stock always first)
    live_tickers = []
    if main_ticker:
        live_tickers.append(main_ticker)
    for t in selected:
        if t not in live_tickers:
            live_tickers.append(t)

    # Loading is explicit: clicking the button snapshots the current selection into
    # session state, and everything below renders from that snapshot (not the live
    # widgets). This keeps the later radio-driven fragment reruns stable and avoids
    # re-fetching every time a selector changes.
    if st.button(
        "🔄 Load & compare", type="primary", disabled=not live_tickers,
        help="Fetch price history for all selected stocks in one batched request and build the comparison.",
    ):
        st.session_state["perf_loaded"] = {
            "tickers": live_tickers, "main": main_ticker, "dividends": include_dividends,
        }

    loaded = st.session_state.get("perf_loaded")
    if not loaded:
        st.info("Select your stocks above, then click **Load & compare**.")
        return

    if (loaded["tickers"] != live_tickers or loaded["main"] != main_ticker
            or loaded["dividends"] != include_dividends):
        st.caption("⚠️ Selection changed — click **Load & compare** to refresh the results below.")

    all_tickers = loaded["tickers"]
    main_ticker = loaded["main"]
    include_dividends = loaded["dividends"]

    with st.spinner("Fetching price history…"):
        history = _fetch_perf_history(tuple(sorted(all_tickers)), auto_adjust=include_dividends)

    if not history:
        st.error("Could not fetch price data — please try again.")
        return

    # ── Build metrics DataFrame ───────────────────────────────────────────────
    PERIODS = ["YTD", "1Y", "5Y CAGR", "10Y CAGR", "15Y CAGR"]
    records = []
    for ticker in all_tickers:
        series = history.get(ticker)
        metrics = (
            _calc_performance(series)
            if (series is not None and not series.empty)
            else {p: None for p in PERIODS}
        )
        records.append({"Ticker": ticker, "Name": name_map.get(ticker, ticker), **metrics})

    df = pd.DataFrame(records).set_index("Ticker")

    # ── Styled returns table ──────────────────────────────────────────────────
    st.subheader("Returns")

    def _fmt(val):
        return "N/A" if pd.isna(val) else f"{val * 100:+.2f}%"

    def _cell_color(val):
        if pd.isna(val):
            return "color: #999"
        return f"color: {'#28a745' if val >= 0 else '#d9534f'}; font-weight: bold"

    def _row_highlight(row):
        if row.get("Ticker") == main_ticker:
            return ["background-color: #fff8e1"] * len(row)
        return [""] * len(row)

    display_df = df[["Name"] + PERIODS].reset_index()
    styled = (
        display_df
        .style
        .apply(_row_highlight, axis=1)
        .map(_cell_color, subset=PERIODS)
        .format(_fmt, subset=PERIODS, na_rep="N/A")
        .hide(axis="index")
    )
    st.dataframe(styled, use_container_width=True)

    # ── Rank table (main stock only) ──────────────────────────────────────────
    if main_ticker and main_ticker in df.index:
        st.subheader(f"Rank — {main_ticker}")
        rank_row = {}
        for period in PERIODS:
            vals = df[period].dropna()
            main_val = df.loc[main_ticker, period] if main_ticker in vals.index else None
            if main_val is not None and not pd.isna(main_val):
                rank = int((vals > main_val).sum()) + 1
                rank_row[period] = f"{rank} / {len(vals)}"
            else:
                rank_row[period] = "N/A"
        rank_df = pd.DataFrame([rank_row], index=[main_ticker])
        st.dataframe(rank_df, use_container_width=True)

    # ── CAGR method comparison ────────────────────────────────────────────────
    st.subheader("CAGR method comparison")
    _cagr_methods(history, name_map, main_ticker, all_tickers)

    # ── Indexed growth (rebased to 100) ───────────────────────────────────────
    st.subheader("Indexed growth (rebased to 100)")
    _rebased_chart(history, main_ticker, all_tickers)

    # ── Bar charts (one tab per period) ───────────────────────────────────────
    st.subheader("Chart")
    _perf_charts(df, PERIODS, main_ticker)


SCREENER_REGIONS = ["us", "de", "gb", "fr", "nl", "ch", "ca", "jp", "hk", "au", "it", "es", "se"]
SCREENER_SECTORS = [
    "Technology", "Healthcare", "Financial Services", "Consumer Cyclical",
    "Consumer Defensive", "Communication Services", "Industrials", "Energy",
    "Basic Materials", "Real Estate", "Utilities",
]


@st.fragment
def _search_results(mdf: pd.DataFrame):
    """Filter sliders + results table. A fragment so adjusting a threshold re-filters
    the already-computed metrics instantly, without re-screening or re-downloading."""
    c1, c2, c3 = st.columns(3)
    with c1:
        min_cagr = st.slider("Min 10Y rolling CAGR (%)", -10.0, 40.0, 8.0, 0.5)
    with c2:
        max_dd = st.slider("Max drawdown allowed (%)", 10, 95, 60, 5,
                           help="Exclude stocks whose worst peak-to-trough drop was deeper than this.")
    with c3:
        max_vol = st.slider("Max ann. volatility (%, 0 = off)", 0, 100, 0, 5)

    f = mdf[mdf["10Y Rolling CAGR"].notna() & (mdf["10Y Rolling CAGR"] >= min_cagr / 100)].copy()
    f = f[f["Max drawdown"].isna() | (f["Max drawdown"] >= -max_dd / 100)]
    if max_vol > 0:
        f = f[f["Ann. volatility"].isna() | (f["Ann. volatility"] <= max_vol / 100)]
    f = f.sort_values("10Y Rolling CAGR", ascending=False)

    st.caption(f"**{len(f)}** of {len(mdf)} scanned stocks match.")
    if f.empty:
        st.info("No stocks match these filters — loosen the thresholds.")
        return

    pct_cols = ["10Y Rolling CAGR", "10Y CAGR (point)", "Max drawdown", "Avg drawdown", "Ann. volatility"]

    def _fmt(val):
        return "N/A" if pd.isna(val) else f"{val * 100:+.1f}%"

    def _color_cagr(val):
        if pd.isna(val):
            return "color: #999"
        return f"color: {'#28a745' if val >= 0 else '#d9534f'}; font-weight: bold"

    def _color_risk(val):
        return "color: #999" if pd.isna(val) else "color: #d9534f"

    disp = f[["Ticker", "Name"] + pct_cols + ["Rolling n", "Data since"]].copy()
    # Ticker cell holds a TradingView URL; the real symbol is appended as a query
    # param so the LinkColumn's display_text regex can show it as the label.
    disp["Ticker"] = disp["Ticker"].map(lambda t: f"{tradingview_url(t, '')}&yfsym={t}")

    styled = (
        disp
        .style
        .map(_color_cagr, subset=["10Y Rolling CAGR", "10Y CAGR (point)"])
        .map(_color_risk, subset=["Max drawdown", "Avg drawdown", "Ann. volatility"])
        .format(_fmt, subset=pct_cols, na_rep="N/A")
        .hide(axis="index")
    )
    st.dataframe(
        styled, use_container_width=True,
        column_config={
            "Ticker": st.column_config.LinkColumn(
                "Ticker", display_text=r"yfsym=(.+)$", help="Open chart on TradingView ↗"
            ),
        },
    )


def render_search_page():
    st.title("🔎 Stock Search")
    st.caption(
        "Scan a candidate universe and rank by **rolling 10-year CAGR**, with filters on "
        "how deep corrections go. Yahoo can only pre-filter on fundamentals, so returns and "
        "drawdowns are computed locally from downloaded price history."
    )

    mode = st.radio("Candidate universe", ["Yahoo screener", "Custom list"], horizontal=True)

    candidates: list[tuple[str, str]] = []
    if mode == "Yahoo screener":
        c1, c2, c3 = st.columns(3)
        with c1:
            region = st.selectbox("Region", SCREENER_REGIONS, index=0)
        with c2:
            min_mcap_b = st.number_input("Min market cap ($B)", min_value=0.1, value=10.0, step=1.0)
        with c3:
            limit = st.slider("Max candidates", 10, 200, 50, 10,
                              help="More candidates = broader search but a longer download.")
        sectors = st.multiselect("Sectors (optional)", SCREENER_SECTORS, placeholder="All sectors")
        scan_cfg = {"mode": mode, "region": region, "min_mcap": min_mcap_b * 1e9,
                    "sectors": tuple(sectors), "limit": limit}
    else:
        raw = st.text_area(
            "Tickers", placeholder="AAPL, MSFT, NVDA  (comma, space or newline separated)",
            height=100,
        )
        tickers = [t.strip().upper() for t in re.split(r"[,\s]+", raw) if t.strip()]
        scan_cfg = {"mode": mode, "tickers": tickers}

    if st.button("🔎 Scan", type="primary"):
        with st.spinner("Resolving candidates…"):
            if scan_cfg["mode"] == "Yahoo screener":
                candidates = _screen_candidates(
                    scan_cfg["region"], scan_cfg["min_mcap"], scan_cfg["sectors"], scan_cfg["limit"]
                )
            else:
                candidates = [(t, t) for t in scan_cfg["tickers"]]

        if not candidates:
            st.warning("No candidates found — adjust the universe settings.")
            st.session_state.pop("search_metrics", None)
            return

        name_map = {sym: name for sym, name in candidates}
        symbols = list(name_map.keys())
        with st.spinner(f"Downloading history for {len(symbols)} stocks…"):
            history = _fetch_perf_history(tuple(sorted(symbols)), auto_adjust=True)

        rows = []
        for t in symbols:
            s = history.get(t)
            if s is None or s.empty:
                continue
            roll, n = _rolling_median_cagr(s, 10)
            rows.append({
                "Ticker": t, "Name": name_map.get(t, t),
                "10Y Rolling CAGR": roll,
                "10Y CAGR (point)": _point_cagr(s, 10),
                "Max drawdown": _max_drawdown(s),
                "Avg drawdown": _avg_drawdown(s),
                "Ann. volatility": _annualized_vol(s),
                "Rolling n": n,
                "Data since": s.index[0].strftime("%Y-%m-%d"),
            })
        if not rows:
            st.error("Could not fetch usable price history for any candidate.")
            st.session_state.pop("search_metrics", None)
            return
        st.session_state["search_metrics"] = pd.DataFrame(rows)

    mdf = st.session_state.get("search_metrics")
    if mdf is None:
        st.info("Set up your universe above, then click **Scan**.")
        return

    st.divider()
    _search_results(mdf)


# ── Navigation (native sidebar menu) ───────────────────────────────────────────
ALL_LABEL = "⭐ All"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "list"


def _make_monitor_page(list_name: str):
    def _page():
        watchlists = get_watchlists()
        auto = st.session_state.get("auto_refresh", False)
        interval = st.session_state.get("auto_interval", 60)
        if auto:
            @st.fragment(run_every=f"{interval}s")
            def _live():
                st.cache_data.clear()
                render_monitor(list_name, watchlists)
            _live()
        else:
            render_monitor(list_name, watchlists)
    return _page


def _edit_page():
    render_edit_screen(get_watchlists())


WATCHLISTS = get_watchlists()
STATUSES = get_watchlist_statuses()

watchlist_pages = [
    st.Page(_make_monitor_page(ALL_LABEL), title="All", icon="⭐", url_path="all", default=True)
]
for name in WATCHLISTS:
    icon = STATUS_ICONS.get(STATUSES.get(name, "target"), "🎯")
    watchlist_pages.append(
        st.Page(_make_monitor_page(name), title=name, icon=icon, url_path=_slug(name))
    )

analysis_pages = [
    st.Page(render_performance_page, title="Performance", icon="📊", url_path="performance"),
    st.Page(render_search_page, title="Search", icon="🔎", url_path="search"),
]

settings_pages = [
    st.Page(_edit_page, title="Edit Watchlists", icon="✏️", url_path="edit"),
    st.Page(render_refresh_screen, title="Refresh Options", icon="🔄", url_path="refresh"),
]

nav = st.navigation({
    "Watchlists": watchlist_pages,
    "Analysis": analysis_pages,
    "Settings": settings_pages,
})
nav.run()
