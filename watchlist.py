import streamlit as st
import yfinance as yf
import pandas as pd
import duckdb
import os
import re
import html
import json
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import quote

from populate_names import enrich as enrich_ticker_meta
from config import (
    DB_ENV_KEY, DB_DEFAULT, TOKEN_ENV_KEY,
    TBL_WATCHLISTS, TBL_ITEMS, TBL_STOCK_TAGS, TBL_STOCK_LEVELS,
    TBL_STOCK_NOTES, TBL_LEVEL_TYPES, TBL_TICKER_NAMES,
    TBL_PERF_PRESETS, TBL_FILTER_PRESETS, TBL_TAG_GROUPS, TBL_TAG_GROUP_MAP,
    SEQ_WATCHLISTS, SEQ_ITEMS, SEQ_STOCK_LEVELS,
    DEFAULT_WATCHLISTS, TAGS, LEVEL_TYPES,
)
from core import (
    _CORR_WINDOWS, _CORR_FREQ, _fetch_perf_history, _corr_ticker_stats,
    validate_ticker, search_tickers,
)

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


DB_NAME = _secret(DB_ENV_KEY, DB_DEFAULT)

# DEFAULT_WATCHLISTS, TAGS, LEVEL_TYPES — defined in config.py


# ── Database (MotherDuck) ───────────────────────────────────────────────────────
@st.cache_resource
def _db() -> duckdb.DuckDBPyConnection:
    """One cached MotherDuck connection for the whole app (auth/handshake once)."""
    token = _secret(TOKEN_ENV_KEY)
    if not token:
        st.error(
            "MOTHERDUCK_TOKEN is not set — add it to Watchlists/.env locally, "
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
        conn.execute(f"CREATE SEQUENCE IF NOT EXISTS {SEQ_WATCHLISTS} START 1")
        conn.execute(f"CREATE SEQUENCE IF NOT EXISTS {SEQ_ITEMS} START 1")
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TBL_WATCHLISTS} (
                id     INTEGER PRIMARY KEY DEFAULT nextval('{SEQ_WATCHLISTS}'),
                name   TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'target'
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TBL_ITEMS} (
                id           INTEGER PRIMARY KEY DEFAULT nextval('{SEQ_ITEMS}'),
                watchlist_id INTEGER NOT NULL,
                ticker       TEXT NOT NULL,
                UNIQUE(watchlist_id, ticker)
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TBL_STOCK_TAGS} (
                ticker TEXT NOT NULL,
                tag    TEXT NOT NULL,
                UNIQUE(ticker, tag)
            )
            """
        )
        conn.execute(f"CREATE SEQUENCE IF NOT EXISTS {SEQ_STOCK_LEVELS} START 1")
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TBL_STOCK_LEVELS} (
                id         INTEGER PRIMARY KEY DEFAULT nextval('{SEQ_STOCK_LEVELS}'),
                ticker     TEXT    NOT NULL,
                price      DOUBLE  NOT NULL,
                level_type TEXT    NOT NULL DEFAULT 'Support',
                note       TEXT
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TBL_STOCK_NOTES} (
                ticker TEXT PRIMARY KEY,
                note   TEXT
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TBL_LEVEL_TYPES} (
                name     TEXT PRIMARY KEY,
                position INTEGER
            )
            """
        )
        # Seed the level types with the defaults the first time only.
        if conn.execute(f"SELECT COUNT(*) FROM {TBL_LEVEL_TYPES}").fetchone()[0] == 0:
            conn.executemany(
                f"INSERT INTO {TBL_LEVEL_TYPES} (name, position) VALUES (?, ?)",
                [(name, pos) for pos, name in enumerate(LEVEL_TYPES)],
            )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TBL_TICKER_NAMES} (
                ticker   TEXT PRIMARY KEY,
                name     TEXT,
                exchange TEXT,
                currency TEXT
            )
            """
        )
        # Migrate older DBs that predate the watchlist status column
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{TBL_WATCHLISTS}')").fetchall()]
        if "status" not in cols:
            conn.execute(f"ALTER TABLE {TBL_WATCHLISTS} ADD COLUMN status TEXT NOT NULL DEFAULT 'target'")
        if "position" not in cols:
            conn.execute(f"ALTER TABLE {TBL_WATCHLISTS} ADD COLUMN position INTEGER")
            conn.execute(f"UPDATE {TBL_WATCHLISTS} SET position = id WHERE position IS NULL")

        # Migrate ticker_names created before the exchange/currency columns existed
        tn_cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{TBL_TICKER_NAMES}')").fetchall()]
        if "exchange" not in tn_cols:
            conn.execute(f"ALTER TABLE {TBL_TICKER_NAMES} ADD COLUMN exchange TEXT")
        if "currency" not in tn_cols:
            conn.execute(f"ALTER TABLE {TBL_TICKER_NAMES} ADD COLUMN currency TEXT")

        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TBL_PERF_PRESETS} (
                name        TEXT PRIMARY KEY,
                tickers     TEXT NOT NULL,
                main_ticker TEXT
            )
            """
        )

        # Migrate perf_presets created before the main_ticker column existed
        pp_cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{TBL_PERF_PRESETS}')").fetchall()]
        if "main_ticker" not in pp_cols:
            conn.execute(f"ALTER TABLE {TBL_PERF_PRESETS} ADD COLUMN main_ticker TEXT")

        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TBL_FILTER_PRESETS} (
                scope    TEXT NOT NULL,
                name     TEXT NOT NULL,
                settings TEXT NOT NULL,
                PRIMARY KEY (scope, name)
            )
            """
        )

        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TBL_TAG_GROUPS} (
                name     TEXT PRIMARY KEY,
                position INTEGER
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TBL_TAG_GROUP_MAP} (
                tag        TEXT PRIMARY KEY,
                group_name TEXT NOT NULL
            )
            """
        )

        # Seed defaults only if the DB is empty
        empty = conn.execute(f"SELECT COUNT(*) FROM {TBL_WATCHLISTS}").fetchone()[0] == 0
        if empty:
            for pos, (name, tickers) in enumerate(DEFAULT_WATCHLISTS.items()):
                wl_id = conn.execute(
                    f"INSERT INTO {TBL_WATCHLISTS} (name, status, position) VALUES (?, 'target', ?) RETURNING id",
                    [name, pos],
                ).fetchone()[0]
                conn.executemany(
                    f"INSERT INTO {TBL_ITEMS} (watchlist_id, ticker) VALUES (?, ?)",
                    [(wl_id, t) for t in tickers],
                )


def get_watchlists() -> dict[str, list[str]]:
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT w.name, i.ticker
            FROM {TBL_WATCHLISTS} w
            LEFT JOIN {TBL_ITEMS} i ON i.watchlist_id = w.id
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
                f"SELECT COALESCE(MAX(position), -1) + 1 FROM {TBL_WATCHLISTS}"
            ).fetchone()[0]
            conn.execute(
                f"INSERT INTO {TBL_WATCHLISTS} (name, status, position) VALUES (?, ?, ?)",
                [name, status, next_pos],
            )
        return True
    except duckdb.ConstraintException:
        return False


def set_watchlist_order(names_in_order: list[str]):
    with get_conn() as conn:
        for pos, name in enumerate(names_in_order):
            conn.execute(f"UPDATE {TBL_WATCHLISTS} SET position = ? WHERE name = ?", [pos, name])


def get_watchlist_statuses() -> dict[str, str]:
    with get_conn() as conn:
        return dict(conn.execute(f"SELECT name, status FROM {TBL_WATCHLISTS}").fetchall())


def set_watchlist_status(name: str, status: str):
    with get_conn() as conn:
        conn.execute(f"UPDATE {TBL_WATCHLISTS} SET status = ? WHERE name = ?", [status, name])


def add_item(watchlist_name: str, ticker: str) -> bool:
    try:
        with get_conn() as conn:
            wl = conn.execute(
                f"SELECT id FROM {TBL_WATCHLISTS} WHERE name = ?", [watchlist_name]
            ).fetchone()
            if wl is None:
                return False
            conn.execute(
                f"INSERT INTO {TBL_ITEMS} (watchlist_id, ticker) VALUES (?, ?)", [wl[0], ticker]
            )
        return True
    except duckdb.ConstraintException:
        return False


def delete_watchlist(name: str):
    # DuckDB has no ON DELETE CASCADE, so remove child rows first
    with get_conn() as conn:
        wl = conn.execute(f"SELECT id FROM {TBL_WATCHLISTS} WHERE name = ?", [name]).fetchone()
        if wl is not None:
            conn.execute(f"DELETE FROM {TBL_ITEMS} WHERE watchlist_id = ?", [wl[0]])
            conn.execute(f"DELETE FROM {TBL_WATCHLISTS} WHERE id = ?", [wl[0]])


def rename_watchlist(old_name: str, new_name: str) -> bool:
    try:
        with get_conn() as conn:
            conn.execute(
                f"UPDATE {TBL_WATCHLISTS} SET name = ? WHERE name = ?", [new_name, old_name]
            )
        return True
    except duckdb.ConstraintException:
        return False


def delete_item(watchlist_name: str, ticker: str):
    with get_conn() as conn:
        conn.execute(
            f"""
            DELETE FROM {TBL_ITEMS}
            WHERE ticker = ?
              AND watchlist_id = (SELECT id FROM {TBL_WATCHLISTS} WHERE name = ?)
            """,
            [ticker, watchlist_name],
        )


def get_tags_map() -> dict[str, list[str]]:
    with get_conn() as conn:
        rows = conn.execute(f"SELECT ticker, tag FROM {TBL_STOCK_TAGS} ORDER BY ticker, tag").fetchall()
    result: dict[str, list[str]] = {}
    for ticker, tag in rows:
        result.setdefault(ticker, []).append(tag)
    return result


def get_tags(ticker: str) -> list[str]:
    with get_conn() as conn:
        return [
            r[0]
            for r in conn.execute(
                f"SELECT tag FROM {TBL_STOCK_TAGS} WHERE ticker = ? ORDER BY tag", [ticker]
            ).fetchall()
        ]


def set_tags(ticker: str, tags: list[str]):
    """Replace the full set of tags for a ticker."""
    with get_conn() as conn:
        conn.execute(f"DELETE FROM {TBL_STOCK_TAGS} WHERE ticker = ?", [ticker])
        if tags:
            conn.executemany(
                f"INSERT INTO {TBL_STOCK_TAGS} (ticker, tag) VALUES (?, ?)",
                [(ticker, tg) for tg in tags],
            )


def get_all_tags() -> list[str]:
    """All known tags: the predefined set plus any user-created ones in the DB."""
    with get_conn() as conn:
        used = [r[0] for r in conn.execute(f"SELECT DISTINCT tag FROM {TBL_STOCK_TAGS}").fetchall()]
    return sorted(set(TAGS) | set(used))


# ── Stock price levels & per-stock notes ────────────────────────────────────────
def get_levels(ticker: str) -> list[dict]:
    """Price levels recorded for one ticker, ordered by price."""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, price, level_type, note FROM {TBL_STOCK_LEVELS} "
            "WHERE ticker = ? ORDER BY price",
            [ticker],
        ).fetchall()
    return [
        {"id": r[0], "price": r[1], "level_type": r[2], "note": r[3] or ""}
        for r in rows
    ]


def get_levels_map() -> dict[str, list[dict]]:
    """All price levels grouped by ticker (for watchlist/monitor overlays)."""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT ticker, id, price, level_type, note FROM {TBL_STOCK_LEVELS} "
            "ORDER BY ticker, price"
        ).fetchall()
    result: dict[str, list[dict]] = {}
    for ticker, _id, price, level_type, note in rows:
        result.setdefault(ticker, []).append(
            {"id": _id, "price": price, "level_type": level_type, "note": note or ""}
        )
    return result


def replace_levels(ticker: str, levels: list[dict]):
    """Replace the full set of price levels for a ticker."""
    with get_conn() as conn:
        conn.execute(f"DELETE FROM {TBL_STOCK_LEVELS} WHERE ticker = ?", [ticker])
        if levels:
            conn.executemany(
                f"INSERT INTO {TBL_STOCK_LEVELS} (ticker, price, level_type, note) "
                "VALUES (?, ?, ?, ?)",
                [
                    (ticker, lv["price"], lv["level_type"], lv.get("note") or None)
                    for lv in levels
                ],
            )


def get_stock_note(ticker: str) -> str:
    """The free-text general comment for a ticker ("" if none)."""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT note FROM {TBL_STOCK_NOTES} WHERE ticker = ?", [ticker]
        ).fetchone()
    return (row[0] if row else "") or ""


def get_stock_notes_map() -> dict[str, str]:
    """All per-stock general comments keyed by ticker (blank notes excluded)."""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT ticker, note FROM {TBL_STOCK_NOTES} ORDER BY ticker"
        ).fetchall()
    return {t: n for t, n in rows if (n or "").strip()}


def get_level_types() -> list[str]:
    """The user-maintained list of level types (falls back to defaults if empty)."""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT name FROM {TBL_LEVEL_TYPES} ORDER BY COALESCE(position, 999), name"
        ).fetchall()
    return [r[0] for r in rows] or list(LEVEL_TYPES)


def set_level_types(names: list[str]):
    """Replace the full set of level types, preserving the given order."""
    seen, ordered = set(), []
    for n in names:
        n = (n or "").strip()
        if n and n not in seen:
            seen.add(n)
            ordered.append(n)
    with get_conn() as conn:
        conn.execute(f"DELETE FROM {TBL_LEVEL_TYPES}")
        if ordered:
            conn.executemany(
                f"INSERT INTO {TBL_LEVEL_TYPES} (name, position) VALUES (?, ?)",
                [(n, pos) for pos, n in enumerate(ordered)],
            )


def set_stock_note(ticker: str, note: str):
    """Upsert the general comment for a ticker; blank removes it."""
    note = (note or "").strip()
    with get_conn() as conn:
        conn.execute(f"DELETE FROM {TBL_STOCK_NOTES} WHERE ticker = ?", [ticker])
        if note:
            conn.execute(
                f"INSERT INTO {TBL_STOCK_NOTES} (ticker, note) VALUES (?, ?)", [ticker, note]
            )


def get_tag_groups() -> list[str]:
    with get_conn() as conn:
        return [r[0] for r in conn.execute(
            f"SELECT name FROM {TBL_TAG_GROUPS} ORDER BY COALESCE(position, 999), name"
        ).fetchall()]


def create_tag_group(name: str) -> bool:
    try:
        with get_conn() as conn:
            next_pos = conn.execute(
                f"SELECT COALESCE(MAX(position), -1) + 1 FROM {TBL_TAG_GROUPS}"
            ).fetchone()[0]
            conn.execute(f"INSERT INTO {TBL_TAG_GROUPS} (name, position) VALUES (?, ?)", [name, next_pos])
        return True
    except duckdb.ConstraintException:
        return False


def delete_tag_group(name: str):
    with get_conn() as conn:
        conn.execute(f"DELETE FROM {TBL_TAG_GROUP_MAP} WHERE group_name = ?", [name])
        conn.execute(f"DELETE FROM {TBL_TAG_GROUPS} WHERE name = ?", [name])


def rename_tag_group(old: str, new: str) -> bool:
    try:
        with get_conn() as conn:
            conn.execute(f"UPDATE {TBL_TAG_GROUP_MAP} SET group_name = ? WHERE group_name = ?", [new, old])
            conn.execute(f"UPDATE {TBL_TAG_GROUPS} SET name = ? WHERE name = ?", [new, old])
        return True
    except duckdb.ConstraintException:
        return False


def get_tag_group_map() -> dict[str, str]:
    """Returns {tag: group_name} for all tags that have been assigned to a group."""
    with get_conn() as conn:
        return dict(conn.execute(f"SELECT tag, group_name FROM {TBL_TAG_GROUP_MAP}").fetchall())


def set_tag_group(tag: str, group_name: str | None):
    with get_conn() as conn:
        conn.execute(f"DELETE FROM {TBL_TAG_GROUP_MAP} WHERE tag = ?", [tag])
        if group_name:
            conn.execute(
                f"INSERT INTO {TBL_TAG_GROUP_MAP} (tag, group_name) VALUES (?, ?)", [tag, group_name]
            )


def get_ticker_meta() -> dict[str, tuple]:
    """All stored metadata keyed by ticker: (name, exchange, currency). Populated by
    the Names editor and/or the populate_names.py enrichment script. The app reads
    names/exchange/currency only from here — it never calls yfinance for them."""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT ticker, name, exchange, currency FROM {TBL_TICKER_NAMES}"
        ).fetchall()
    return {t: (n, e, c) for t, n, e, c in rows}


def set_ticker_name(ticker: str, name: str):
    """Set (or clear, when blank) the manual display name, preserving exchange/currency."""
    name = (name or "").strip() or None
    with get_conn() as conn:
        exists = conn.execute(f"SELECT 1 FROM {TBL_TICKER_NAMES} WHERE ticker = ?", [ticker]).fetchone()
        if exists:
            conn.execute(f"UPDATE {TBL_TICKER_NAMES} SET name = ? WHERE ticker = ?", [name, ticker])
        elif name:
            conn.execute(f"INSERT INTO {TBL_TICKER_NAMES} (ticker, name) VALUES (?, ?)", [ticker, name])


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


# validate_ticker moved to core.py (shared with portfolio.py)


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


# ── Filter-preset session helpers ───────────────────────────────────────────────
def _filter_state_keys(selected_list: str) -> list[str]:
    """The session_state keys that make up the filter/grouping configuration for a
    monitor view. Status- and watchlist-filtering only exist on the aggregate All
    view, so those keys are only included there."""
    keys = [
        f"only_today::{selected_list}",
        f"grouptype::{selected_list}",
        f"groupwl::{selected_list}",
        f"groupby::{selected_list}",
        f"wlfilter::{selected_list}",
        f"tagfilter::{selected_list}",
    ]
    if selected_list == ALL_LABEL:
        keys.append("list_status_filter")
    return keys


def _collect_filter_settings(selected_list: str) -> dict:
    """Snapshot the current filter values from session_state (last committed by the
    widgets on the previous run)."""
    return {
        k: st.session_state[k]
        for k in _filter_state_keys(selected_list)
        if k in st.session_state
    }


def _apply_filter_settings(selected_list: str, settings: dict):
    """Write saved filter values back into session_state. Must run before the filter
    widgets are instantiated (Streamlit forbids changing a widget's key after it is
    created), so the caller writes then st.rerun()s."""
    valid = set(_filter_state_keys(selected_list))
    for k, v in settings.items():
        if k in valid:
            st.session_state[k] = v


def _render_filter_presets(selected_list: str):
    """Save / load / delete bar for the current view's filter configuration. Rendered
    after the per-list setdefaults and before the filter widgets so a loaded preset
    is picked up by the widgets on the rerun."""
    presets = get_filter_presets(selected_list)
    if presets:
        p_col, load_col, del_col = st.columns([4, 1.4, 0.7])
        with p_col:
            choice = st.selectbox(
                "Filter preset",
                options=[""] + list(presets.keys()),
                format_func=lambda x: "— saved filter presets —" if x == "" else x,
                key=f"filter_preset_choice::{selected_list}",
                label_visibility="collapsed",
            )
        with load_col:
            if st.button("⬇ Load filters", use_container_width=True,
                         disabled=not choice, key=f"filter_preset_load::{selected_list}"):
                _apply_filter_settings(selected_list, presets[choice])
                st.rerun()
        with del_col:
            if st.button("🗑️", use_container_width=True, disabled=not choice,
                         help="Delete this filter preset",
                         key=f"filter_preset_del::{selected_list}"):
                delete_filter_preset(selected_list, choice)
                st.rerun()

    with st.expander("💾 Save current filters as preset"):
        s_col, btn_col = st.columns([3, 1], vertical_alignment="bottom")
        with s_col:
            new_name = st.text_input(
                "Preset name", placeholder="e.g. Held, grouped by watchlist",
                key=f"filter_preset_name::{selected_list}",
            )
        with btn_col:
            if st.button("Save", use_container_width=True,
                         disabled=not new_name.strip(),
                         key=f"filter_preset_save::{selected_list}"):
                save_filter_preset(
                    selected_list, new_name.strip(),
                    _collect_filter_settings(selected_list),
                )
                st.success(f'Saved filter preset "{new_name.strip()}".')
                st.rerun()


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

    # Save/load the current filter configuration. Rendered before the filter widgets
    # so a loaded preset's values are in session_state when the widgets read them.
    _render_filter_presets(selected_list)

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

    tab_create, tab_rename, tab_reorder, tab_add, tab_status, tab_tags, tab_tag_groups, tab_names, tab_del_item, tab_del_list = st.tabs(
        ["Create list", "Rename list", "Reorder", "Add stock", "Set status", "Tags", "Tag Groups", "Names", "Remove stock", "Delete list"]
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
            query = st.text_input(
                "Search Yahoo (ticker or company name)",
                placeholder="e.g. AAPL, Nestlé, VWCE.DE", key="add_query",
            )
            results = search_tickers(query) if query.strip() else []

            if results:
                labels = {
                    i: f"{r['symbol']} — {r['name']}"
                       + (f" · {r['exchange']}" if r['exchange'] else "")
                       + (f" · {r['type']}" if r['type'] else "")
                    for i, r in enumerate(results)
                }
                pick = st.selectbox("Match", list(labels.keys()),
                                    format_func=lambda i: labels[i], key="add_pick")
                if st.button("Check symbol"):
                    sym = results[pick]["symbol"]
                    with st.spinner(f"Looking up {sym}…"):
                        st.session_state["pending_add"] = validate_ticker(sym)
            elif query.strip():
                st.caption("No search matches — you can still check it as an exact ticker.")
                if st.button(f"Check “{query.strip().upper()}” as exact ticker"):
                    with st.spinner(f"Looking up {query.strip().upper()}…"):
                        st.session_state["pending_add"] = validate_ticker(query.strip().upper())

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
            st.info(f"No {TBL_WATCHLISTS} to update.")
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

    # ── Manage tag groups ──────────────────────────────────────────────────────
    with tab_tag_groups:
        with st.form("new_tag_group_form", clear_on_submit=True):
            new_grp_name = st.text_input("New group name")
            if st.form_submit_button("Create group", type="primary"):
                name = new_grp_name.strip()
                if not name:
                    st.warning("Enter a name.")
                elif create_tag_group(name):
                    st.success(f"Created group \"{name}\".")
                    st.rerun()
                else:
                    st.error(f"\"{name}\" already exists.")

        groups = get_tag_groups()
        all_tag_list = get_all_tags()

        if groups:
            with st.expander("Rename or delete a group"):
                grp_sel = st.selectbox("Group", groups, key="tg_edit_group")
                new_grp_nm = st.text_input("New name", value=grp_sel, key="tg_new_name")
                if st.button("Rename", key="tg_rename"):
                    clean = new_grp_nm.strip()
                    if not clean:
                        st.warning("Enter a name.")
                    elif clean == grp_sel:
                        st.info("Name unchanged.")
                    elif rename_tag_group(grp_sel, clean):
                        st.success(f"Renamed to \"{clean}\".")
                        st.rerun()
                    else:
                        st.error(f"\"{clean}\" already exists.")
                st.divider()
                confirm_del = st.checkbox("Confirm deletion", key="tg_del_confirm")
                if st.button("Delete group", type="primary", disabled=not confirm_del, key="tg_del"):
                    delete_tag_group(grp_sel)
                    st.success(f"Deleted \"{grp_sel}\".")
                    st.rerun()

        if not all_tag_list:
            st.info("No tags defined yet. Assign tags to stocks first.")
        elif not groups:
            st.info("Create a group above to start assigning tags.")
        else:
            st.caption("Assign each tag to a group, or leave blank to keep it ungrouped.")
            tg_map = get_tag_group_map()
            tg_df = pd.DataFrame({
                "Tag":   all_tag_list,
                "Group": [tg_map.get(t, "") for t in all_tag_list],
            })
            edited_tg = st.data_editor(
                tg_df,
                hide_index=True,
                use_container_width=True,
                disabled=["Tag"],
                column_config={
                    "Tag":   st.column_config.TextColumn("Tag"),
                    "Group": st.column_config.SelectboxColumn(
                        "Group", options=[""] + groups,
                        help="Assign to a group, or leave blank for ungrouped",
                    ),
                },
                key="tag_group_editor",
            )
            if st.button("Save", type="primary", key="tg_save"):
                changed = 0
                for _, row in edited_tg.iterrows():
                    new_g = (row["Group"] or "").strip()
                    if new_g != tg_map.get(row["Tag"], ""):
                        set_tag_group(row["Tag"], new_g or None)
                        changed += 1
                st.success(f"Saved {changed} change(s).")
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
            st.info(f"No {TBL_WATCHLISTS} to delete.")
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
        rows = conn.execute(f"SELECT name, tickers, main_ticker FROM {TBL_PERF_PRESETS} ORDER BY name").fetchall()
    return {
        name: {"tickers": tickers.split(","), "main_ticker": main_ticker}
        for name, tickers, main_ticker in rows
    }


def save_perf_preset(name: str, tickers: list[str], main_ticker: str | None = None):
    with get_conn() as conn:
        conn.execute(
            f"""
            INSERT INTO {TBL_PERF_PRESETS} (name, tickers, main_ticker) VALUES (?, ?, ?)
            ON CONFLICT (name) DO UPDATE SET tickers = excluded.tickers, main_ticker = excluded.main_ticker
            """,
            [name, ",".join(tickers), main_ticker or None],
        )


def delete_perf_preset(name: str):
    with get_conn() as conn:
        conn.execute(f"DELETE FROM {TBL_PERF_PRESETS} WHERE name = ?", [name])


# ── Filter presets (saved filter/grouping configurations per monitor view) ──────
def get_filter_presets(scope: str) -> dict[str, dict]:
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT name, settings FROM {TBL_FILTER_PRESETS} WHERE scope = ? ORDER BY name",
            [scope],
        ).fetchall()
    out: dict[str, dict] = {}
    for name, settings in rows:
        try:
            out[name] = json.loads(settings)
        except (ValueError, TypeError):
            continue
    return out


def save_filter_preset(scope: str, name: str, settings: dict):
    with get_conn() as conn:
        conn.execute(
            f"""
            INSERT INTO {TBL_FILTER_PRESETS} (scope, name, settings) VALUES (?, ?, ?)
            ON CONFLICT (scope, name) DO UPDATE SET settings = excluded.settings
            """,
            [scope, name, json.dumps(settings)],
        )


def delete_filter_preset(scope: str, name: str):
    with get_conn() as conn:
        conn.execute(
            f"DELETE FROM {TBL_FILTER_PRESETS} WHERE scope = ? AND name = ?", [scope, name]
        )


# _fetch_perf_history moved to core.py (shared with portfolio.py)


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


def _ulcer_index(series: pd.Series) -> float | None:
    """Ulcer Index — root-mean-square of the drawdown series (each day's % below its
    prior peak). Unlike max drawdown it reflects the depth AND duration of *every*
    correction, so a stock that spends long stretches underwater scores high even if
    its single worst drop was moderate. Positive; lower = shallower, briefer
    drawdowns overall."""
    if series is None or series.empty:
        return None
    dd = series / series.cummax() - 1.0
    if dd.empty:
        return None
    return float(((dd ** 2).mean()) ** 0.5)


def _annualized_vol(series: pd.Series) -> float | None:
    """Std dev of daily returns, annualized (×√252)."""
    if series.empty or len(series) < 2:
        return None
    rets = series.pct_change().dropna()
    if rets.empty:
        return None
    return float(rets.std() * (252 ** 0.5))


def _k_ratio(series: pd.Series, years: int | None = None) -> float | None:
    """Kestner (2013 revised) K-Ratio — how *consistent* the price trend is, not how
    fast it rose. Regress cumulative log price on time and take
    K = (slope / std-error-of-slope) × √252 / n. The √252/n scaling is length-
    normalized, so stocks with different history lengths stay comparable. High K =
    a steady, low-variance climb; near zero or negative = noisy, sideways or
    falling. Uses the full available history by default, or the last `years` of
    daily closes when `years` is given (falling back to full history when shorter)."""
    import numpy as np
    if series is None or series.empty:
        return None
    s = series
    if years is not None:
        end = series.index[-1]
        s = series[series.index >= end - pd.DateOffset(years=years)]
        if len(s) < 30:
            s = series
    s = s[s > 0]
    if len(s) < 30:
        return None
    y = np.log(s.to_numpy(dtype=float))
    n = len(y)
    x = np.arange(n, dtype=float)
    x_dev = x - x.mean()
    ss_xx = float((x_dev ** 2).sum())
    if ss_xx <= 0:
        return None
    slope = float((x_dev * (y - y.mean())).sum() / ss_xx)
    resid = y - (y.mean() + slope * x_dev)
    dof = n - 2
    if dof <= 0:
        return None
    se_slope = float((resid @ resid / dof) ** 0.5 / (ss_xx ** 0.5))
    if se_slope <= 0:
        return None
    return float((slope / se_slope) * (252.0 ** 0.5) / n)


def _trend_resid_std(series: pd.Series) -> float | None:
    """Scatter of price around its own log-price trend line — the 'noise' term that
    K-Ratio divides out, shown on its own. Fits ln(price) vs time over the full
    history and returns the residual std-dev as a multiplicative band (0.22 ≈ the
    price typically sits within ±~22% of its trend, in either direction). Unlike
    K-Ratio this is NOT inflated by a strong trend: a stock that swings widely around
    its line scores high no matter how fast it climbs."""
    import numpy as np
    if series is None or series.empty:
        return None
    s = series[series > 0]
    if len(s) < 30:
        return None
    y = np.log(s.to_numpy(dtype=float))
    n = len(y)
    x = np.arange(n, dtype=float)
    x_dev = x - x.mean()
    ss_xx = float((x_dev ** 2).sum())
    if ss_xx <= 0:
        return None
    slope = float((x_dev * (y - y.mean())).sum() / ss_xx)
    resid = y - (y.mean() + slope * x_dev)
    dof = n - 2
    if dof <= 0:
        return None
    sigma = float((resid @ resid / dof) ** 0.5)  # log units
    return float(np.exp(sigma) - 1.0)


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



@st.fragment
def _rebased_chart(history: dict, main_ticker: str, all_tickers: list[str]):
    """Indexed growth: every stock rebased to 100 at the start of the chosen window,
    on a log axis so equal % moves are equal visual distances and the whole path —
    not just the two endpoints — is visible. Plotly so it zooms/pans; falls back to
    a native line chart if plotly isn't installed in the runtime."""
    period = st.radio(
        "Period", ["1Y", "5Y CAGR", "10Y CAGR", "15Y CAGR"],
        horizontal=True, key="rebase_period",
        format_func=lambda p: p.replace(" CAGR", ""),
    )

    # Rebase each series to 100 at the start of the chosen window.
    rebased: dict[str, pd.Series] = {}
    for t in all_tickers:
        s = history.get(t)
        if s is None or s.empty:
            continue
        start = _period_start(s, period)
        s2 = s[s.index >= start]
        if s2.empty or float(s2.iloc[0]) <= 0:
            continue
        rebased[t] = s2 / float(s2.iloc[0]) * 100

    if not rebased:
        st.caption("No data available for the selected period.")
        return

    try:
        import plotly.graph_objects as go
    except ModuleNotFoundError:
        # Fallback: native line chart (no log axis / per-line styling, but no extra dep).
        st.line_chart(pd.DataFrame(rebased), height=440)
        st.caption(
            "Growth rebased to 100 (linear axis). Install `plotly` for the "
            "interactive log-scale chart."
        )
        return

    fig = go.Figure()
    for t, rb in rebased.items():
        is_main = t == main_ticker
        fig.add_trace(go.Scatter(
            x=rb.index, y=rb.values, name=t, mode="lines",
            line=dict(width=3.5 if is_main else 1.5),
            hovertemplate=f"<b>{t}</b><br>%{{x|%Y-%m-%d}}<br>Index: %{{y:.1f}}<extra></extra>",
        ))

    fig.update_yaxes(type="log", title="Growth (start = 100)")
    fig.update_xaxes(title=None)
    fig.update_layout(
        height=440,
        margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title=None),
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)


# Soft pastel row backgrounds, one per watchlist loaded into the comparison
_WL_ROW_PALETTE = [
    "#dbeafe", "#dcfce7", "#fce7f3", "#ffedd5",
    "#e0e7ff", "#d1fae5", "#fee2e2", "#f3e8ff",
]


def render_performance_page():
    st.title("📊 Performance Comparison")

    # ── Ticker selector ───────────────────────────────────────────────────────
    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT DISTINCT i.ticker, COALESCE(n.name, i.ticker) AS display_name
            FROM {TBL_ITEMS} i
            LEFT JOIN {TBL_TICKER_NAMES} n ON n.ticker = i.ticker
            ORDER BY i.ticker
        """).fetchall()

    if not rows:
        st.info("No tickers found in your watchlists. Add some stocks first.")
        return

    name_map = {t: name for t, name in rows}
    _meta = get_ticker_meta()
    ex_map = {t: (_meta.get(t) or ("", "", ""))[1] or "" for t in name_map}

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
                    st.warning(f"Skipped {len(missing)} ticker(s) no longer in your {TBL_WATCHLISTS}: {', '.join(sorted(missing))}")
                st.session_state.pop("perf_ticker_source", None)
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

    # ── Load tickers from watchlist(s) ───────────────────────────────────────
    watchlists_data = get_watchlists()
    if watchlists_data:
        with st.expander("📋 Load tickers from watchlist(s)"):
            wl_pick = st.multiselect(
                "Watchlists",
                options=list(watchlists_data.keys()),
                placeholder="Choose one or more watchlists…",
                key="perf_wl_pick",
                label_visibility="collapsed",
            )
            if st.button("Load tickers", disabled=not wl_pick, key="perf_load_wl"):
                _src = dict(st.session_state.get("perf_ticker_source", {}))
                _cur = list(st.session_state.get("perf_compare_tickers", []))
                _cur_set = set(_cur)
                for wl in wl_pick:
                    for t in watchlists_data.get(wl, []):
                        if t in name_map:
                            if t not in _src:
                                _src[t] = wl
                            if t not in _cur_set:
                                _cur.append(t)
                                _cur_set.add(t)
                st.session_state["perf_compare_tickers"] = _cur
                st.session_state["perf_ticker_source"] = _src
                st.rerun()

    # ── Load tickers from tag(s) ──────────────────────────────────────────────
    with st.expander("🏷️ Load tickers from tag(s)"):
        _all_tags = get_all_tags()
        if not _all_tags:
            st.caption("No tags defined yet.")
        else:
            tag_pick = st.multiselect(
                "Tags",
                options=_all_tags,
                placeholder="Choose one or more tags…",
                key="perf_tag_pick",
                label_visibility="collapsed",
            )
            if st.button("Load tickers", disabled=not tag_pick, key="perf_load_tags"):
                _tags_map = get_tags_map()
                _src = dict(st.session_state.get("perf_ticker_source", {}))
                _cur = list(st.session_state.get("perf_compare_tickers", []))
                _cur_set = set(_cur)
                for tag in tag_pick:
                    for t, t_tags in _tags_map.items():
                        if tag in t_tags and t in name_map:
                            if t not in _src:
                                _src[t] = tag
                            if t not in _cur_set:
                                _cur.append(t)
                                _cur_set.add(t)
                st.session_state["perf_compare_tickers"] = _cur
                st.session_state["perf_ticker_source"] = _src
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

    # ── Row coloring ──────────────────────────────────────────────────────────
    color_by = st.radio(
        "Color rows by:",
        options=["None", "Watchlist", "Watchlist type", "Tag group"],
        horizontal=True,
        key="perf_color_by",
    )
    wl_bg: dict[str, str] = {}
    ticker_bg: dict[str, str] = {}

    if color_by == "Watchlist":
        _wls = get_watchlists()
        _seen_wl: set[str] = set()
        _ordered_wls: list[str] = []
        _t_wl: dict[str, str] = {}
        for wl_name, wl_tickers in _wls.items():
            for t in wl_tickers:
                if t in all_tickers and t not in _t_wl:
                    _t_wl[t] = wl_name
                    if wl_name not in _seen_wl:
                        _seen_wl.add(wl_name)
                        _ordered_wls.append(wl_name)
        wl_bg = {n: _WL_ROW_PALETTE[i % len(_WL_ROW_PALETTE)] for i, n in enumerate(_ordered_wls)}
        ticker_bg = {t: wl_bg[wl] for t, wl in _t_wl.items()}

    elif color_by == "Watchlist type":
        _STATUS_BG = {"active": "#d4edda", "target": "#dbeafe", "info": "#e9ecef"}
        _STATUS_LEGEND = {"active": "🟢 Held", "target": "🎯 Target", "info": "ℹ️ Info"}
        _wls = get_watchlists()
        _statuses = get_watchlist_statuses()
        _t_status: dict[str, str] = {}
        for wl_name, wl_tickers in _wls.items():
            s = _statuses.get(wl_name, "target")
            for t in wl_tickers:
                if t in all_tickers and t not in _t_status:
                    _t_status[t] = s
        present = [s for s in STATUS_OPTIONS if any(v == s for v in _t_status.values())]
        wl_bg = {_STATUS_LEGEND[s]: _STATUS_BG[s] for s in present}
        ticker_bg = {t: _STATUS_BG[s] for t, s in _t_status.items()}

    elif color_by == "Tag group":
        _groups = get_tag_groups()
        if not _groups:
            st.caption("No tag groups defined yet — create some in Settings → Edit Watchlists → Tag Groups.")
        else:
            _sel_grp = st.selectbox(
                "Tag group",
                options=_groups,
                key="perf_color_tag_group",
                label_visibility="collapsed",
            )
            _tg_map = get_tag_group_map()
            _tags_in_grp = sorted(tag for tag, grp in _tg_map.items() if grp == _sel_grp)
            _tags_m = get_tags_map()
            _t_tag: dict[str, str] = {}
            _tag_order: list[str] = []
            _seen_tags: set[str] = set()
            for t in all_tickers:
                for tag in _tags_m.get(t, []):
                    if tag in _tags_in_grp:
                        _t_tag[t] = tag
                        if tag not in _seen_tags:
                            _seen_tags.add(tag)
                            _tag_order.append(tag)
                        break
            wl_bg = {tag: _WL_ROW_PALETTE[i % len(_WL_ROW_PALETTE)] for i, tag in enumerate(_tag_order)}
            ticker_bg = {t: wl_bg[tag] for t, tag in _t_tag.items()}

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

    # ── Point-to-point CAGR table ─────────────────────────────────────────────
    if wl_bg:
        _mode_label = {
            "Watchlist": "watchlist", "Watchlist type": "watchlist type", "Tag group": "tag group",
        }.get(color_by, "")
        chips = "".join(
            f'<span style="display:inline-block; background:{color}; border:1px solid #c0c8d8; '
            f'border-radius:8px; padding:2px 10px; margin:2px 4px; font-size:.82em; '
            f'font-weight:600; color:#1a1a2e">{name}</span>'
            for name, color in wl_bg.items()
        )
        st.markdown(
            f'<div style="margin-bottom:4px; font-size:.85em; color:#555">'
            f'Color by {_mode_label}: {chips}</div>',
            unsafe_allow_html=True,
        )
    st.subheader("Point-to-point CAGR")

    def _fmt(val):
        return "N/A" if pd.isna(val) else f"{val * 100:+.2f}%"

    def _cell_color(val):
        if pd.isna(val):
            return "color: #999"
        return f"color: {'#28a745' if val >= 0 else '#d9534f'}; font-weight: bold"

    def _row_highlight(row):
        t = row.name  # DataFrame index = the real ticker
        if t == main_ticker:
            return ["background-color: #fff8e1"] * len(row)
        bg = ticker_bg.get(t, "")
        return [f"background-color: {bg}" if bg else ""] * len(row)

    _link_cfg = {
        "Ticker": st.column_config.LinkColumn(
            "Ticker", display_text=r"yfsym=(.+)$", help="Open chart on TradingView ↗"
        ),
    }

    display_df = (
        df[["Name"] + PERIODS]
        .sort_values("10Y CAGR", ascending=False, na_position="last")
        .copy()
    )
    # Index stays the real ticker (used for row highlighting); the visible Ticker
    # column holds a TradingView URL with the real symbol appended as a query param
    # so the LinkColumn's regex can show it as the label.
    display_df.insert(
        0, "Ticker",
        [f"{tradingview_url(t, ex_map.get(t, ''))}&yfsym={t}" for t in display_df.index],
    )
    styled = (
        display_df
        .style
        .apply(_row_highlight, axis=1)
        .map(_cell_color, subset=PERIODS)
        .format(_fmt, subset=PERIODS, na_rep="N/A")
        .hide(axis="index")
    )
    st.dataframe(styled, use_container_width=True,
                 height=(min(len(display_df), 50) + 1) * 35 + 3,
                 column_config=_link_cfg)
    st.caption(
        "Plain point-to-point CAGR: latest close vs the close exactly that many "
        "years ago (YTD and 1Y are simple returns). One start date, so sensitive "
        "to where the window happens to begin — the table below smooths that out."
    )

    # ── Trend & consistency table ─────────────────────────────────────────────
    st.subheader("Trend & consistency")
    trend_rows = []
    for ticker in all_tickers:
        s = history.get(ticker)
        if s is None or s.empty:
            roll5 = roll10 = roll15 = ratio3 = ratio5 = kr = tdev = ulcer = None
            n5 = n10 = n15 = 0
            since = None
        else:
            roll5, n5 = _rolling_median_cagr(s, 5)
            roll10, n10 = _rolling_median_cagr(s, 10)
            roll15, n15 = _rolling_median_cagr(s, 15)
            denom_ok = roll10 is not None and roll10 > 0
            p3 = _point_cagr(s, 3)
            p5 = _point_cagr(s, 5)
            ratio3 = (p3 / roll10) if (p3 is not None and denom_ok) else None
            ratio5 = (p5 / roll10) if (p5 is not None and denom_ok) else None
            kr = _k_ratio(s)
            tdev = _trend_resid_std(s)
            ulcer = _ulcer_index(s)
            since = s.index[0]
        trend_rows.append({
            "Ticker": ticker, "Name": name_map.get(ticker, ticker),
            "Rolling 5Y CAGR": roll5, "Rolling 10Y CAGR": roll10, "Rolling 15Y CAGR": roll15,
            "3Y/10Y Ratio": ratio3, "5Y/10Y Ratio": ratio5,
            "K-Ratio": kr, "Trend dev": tdev, "Ulcer": ulcer,
            "5Y n": n5, "10Y n": n10, "15Y n": n15,
            "Data since": since.strftime("%Y-%m-%d") if since is not None else "—",
        })
    rolling_cols = ["Rolling 5Y CAGR", "Rolling 10Y CAGR", "Rolling 15Y CAGR"]
    ratio_cols = ["3Y/10Y Ratio", "5Y/10Y Ratio"]
    risk_cols = ["Trend dev", "Ulcer"]
    n_cols = ["5Y n", "10Y n", "15Y n"]
    col_order = [
        "Ticker", "Name",
        "Rolling 5Y CAGR", "5Y n",
        "Rolling 10Y CAGR", "10Y n",
        "Rolling 15Y CAGR", "15Y n",
        "3Y/10Y Ratio", "5Y/10Y Ratio",
        "K-Ratio", "Trend dev", "Ulcer",
        "Data since",
    ]
    trend_df = (
        pd.DataFrame(trend_rows)
        .sort_values("Rolling 10Y CAGR", ascending=False, na_position="last")
        .set_index("Ticker", drop=False)
    )
    # Visible Ticker column → TradingView link; the index keeps the real symbol for
    # row highlighting (and the display_text regex reads the appended yfsym param).
    trend_df["Ticker"] = [
        f"{tradingview_url(t, ex_map.get(t, ''))}&yfsym={t}" for t in trend_df.index
    ]
    trend_df = trend_df[col_order]

    def _fmt_ratio(val):
        return "N/A" if pd.isna(val) else f"{val:.2f}×"

    def _fmt_num(val):
        return "N/A" if pd.isna(val) else f"{val:.2f}"

    def _fmt_pct_plain(val):
        return "N/A" if pd.isna(val) else f"{val * 100:.1f}%"

    def _color_ratio(val):
        if pd.isna(val):
            return "color: #999"
        return f"color: {'#28a745' if val >= 1 else '#d9534f'}; font-weight: bold"

    def _color_risk(val):
        return "color: #999" if pd.isna(val) else "color: #d9534f"

    trend_styled = (
        trend_df
        .style
        .apply(_row_highlight, axis=1)
        .map(_cell_color, subset=rolling_cols)
        .map(_color_ratio, subset=ratio_cols)
        .map(_cell_color, subset=["K-Ratio"])
        .map(_color_risk, subset=risk_cols)
        .format(_fmt, subset=rolling_cols, na_rep="N/A")
        .format(_fmt_ratio, subset=ratio_cols, na_rep="N/A")
        .format(_fmt_num, subset=["K-Ratio"], na_rep="N/A")
        .format(_fmt_pct_plain, subset=risk_cols, na_rep="N/A")
        .format("{:d}", subset=n_cols)
        .hide(axis="index")
    )
    st.dataframe(trend_styled, use_container_width=True,
                 height=(min(len(trend_df), 50) + 1) * 35 + 3,
                 column_config=_link_cfg)
    st.caption("\n".join([
        "- **Rolling 5Y / 10Y / 15Y CAGR** — median CAGR over every month-end start date "
        "for that window length, so each reflects a *typical* period rather than one lucky "
        "or unlucky start.",
        "- **5Y n / 10Y n / 15Y n** — how many such windows existed in the available history "
        "(more = a more reliable median).",
        "- **3Y/10Y** and **5Y/10Y Ratio** — the last 3-year resp. 5-year (point-to-point) "
        "CAGR ÷ the rolling 10Y median: >1.00× means recent growth is running above its "
        "long-run norm, <1.00× below (shown only when the 10Y median is positive).",
        "- **K-Ratio** — Kestner (2013) trend *strength*: log price regressed on time over the "
        "**full** history, length-normalized, so higher = a cleaner, more dominant trend.",
        "- **Trend dev** — the scatter K-Ratio divides out: how far price typically sits from "
        "its own trend line, up or down (±% band); high = a bumpy ride regardless of trend "
        "strength.",
        "- **Ulcer** — root-mean-square of all drawdowns (depth *and* time spent underwater), "
        "so it reflects regular corrections, not just the single worst.",
        "- **Data since** — first date of available price history.",
    ]))

    # ── Indexed growth (rebased to 100) ───────────────────────────────────────
    st.subheader("Indexed growth (rebased to 100)")
    _rebased_chart(history, main_ticker, all_tickers)


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
        height=(min(len(disp), 50) + 1) * 35 + 3,
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


def render_color_settings_page():
    pass


# ── Correlation analysis ────────────────────────────────────────────────────────
# Lookback windows: label → date offset back from the latest available date.

# _CORR_WINDOWS, _CORR_FREQ, _corr_ticker_stats moved to core.py (shared with portfolio.py)


def render_correlation_page():
    st.title("🔗 Correlation")
    st.caption(
        "Find positions that move together. Highly correlated holdings add little "
        "diversification, so the most-correlated pairs are candidates to trim."
    )

    with st.expander("ℹ️ How this is calculated"):
        st.markdown(
            """
**Prices → returns.** Two rising stocks always look correlated on *price* (shared
upward trend), so we correlate **returns** instead. Prices are **adjusted closes**
from Yahoo Finance (dividends and splits folded in, i.e. total return), resampled to
the chosen **frequency**, then turned into percentage changes.

**Correlation (ρ).** The Pearson correlation of the two return series over the
lookback window. `ρ = +1` → they move in lockstep; `ρ = 0` → unrelated; `ρ = −1` →
they move oppositely. A pair needs **enough overlapping observations** (≥20 daily,
≥12 weekly, ≥8 monthly) or it is skipped (different listing dates, holidays across
exchanges).

**Frequency & window.** *Weekly* returns over *1 year* is the default — daily returns
are noisy, monthly smooths too much; a 1-year window reflects the current regime.
Correlation is not stable, so try a couple of windows.

**Reading the number.** Because we correlate *returns* (not prices), no two distinct
companies ever reach `1.0` — there's always company-specific week-to-week noise. A
rough guide:

| ρ | Interpretation |
|---|---|
| ~0.0 | unrelated |
| 0.3–0.5 | loosely related (same broad market) |
| 0.6–0.8 | strong sector relationship |
| 0.85–0.95 | about as tight as two distinct companies get |
| 1.0 | effectively the same instrument (same stock, different ticker) |

**Pruning helper.** For each highly correlated pair, each stock's **CAGR**
(annualized price return) is divided by its **annualized volatility** (std. dev. of
returns × √periods-per-year) to get a return-per-unit-of-risk ratio. We suggest
**keeping** the higher-ratio stock and flag the other as a drop candidate.

> Correlation ≠ redundancy. Two correlated names can still differ in risk and
> drawdown. These are **candidates to review**, not instructions.
            """
        )

    # ── Ticker selection ──────────────────────────────────────────────────────
    # ── Ticker selection (mirrors the Performance tab, split by axis) ─────────
    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT DISTINCT i.ticker, COALESCE(n.name, i.ticker) AS display_name
            FROM {TBL_ITEMS} i
            LEFT JOIN {TBL_TICKER_NAMES} n ON n.ticker = i.ticker
            ORDER BY i.ticker
        """).fetchall()
    if not rows:
        st.info("No tickers found in your watchlists. Add some stocks first.")
        return
    name_map = {t: name for t, name in rows}

    watchlists = get_watchlists()

    # Bulk-load helpers append tickers onto one axis's multiselect in session_state.
    def _append_axis(axis: str, to_add: list[str]):
        key = f"corr_tickers_{axis}"
        cur = list(st.session_state.get(key, []))
        cur_set = set(cur)
        for t in to_add:
            if t in name_map and t not in cur_set:
                cur.append(t)
                cur_set.add(t)
        st.session_state[key] = cur

    def _axis_load_buttons(to_add: list[str], enabled: bool, key_prefix: str):
        bx, by = st.columns(2)
        if bx.button("⬇ Load to X axis", disabled=not enabled,
                     key=f"{key_prefix}_x", use_container_width=True):
            _append_axis("x", to_add)
            st.rerun()
        if by.button("⬇ Load to Y axis", disabled=not enabled,
                     key=f"{key_prefix}_y", use_container_width=True):
            _append_axis("y", to_add)
            st.rerun()

    if watchlists:
        with st.expander("📋 Load stocks from watchlist(s)"):
            wl_pick = st.multiselect(
                "Watchlists", options=list(watchlists.keys()),
                placeholder="Choose one or more watchlists…",
                key="corr_wl_pick", label_visibility="collapsed",
            )
            _axis_load_buttons(
                [t for wl in wl_pick for t in watchlists.get(wl, [])],
                bool(wl_pick), "corr_load_wl",
            )

    _all_tags = get_all_tags()
    if _all_tags:
        with st.expander("🏷️ Load stocks from tag(s)"):
            tag_pick = st.multiselect(
                "Tags", options=_all_tags, placeholder="Choose one or more tags…",
                key="corr_tag_pick", label_visibility="collapsed",
            )
            tmap = get_tags_map()
            _axis_load_buttons(
                [t for t, tg in tmap.items() if set(tag_pick) & set(tg)],
                bool(tag_pick), "corr_load_tag",
            )

    _fmt = lambda t: f"{t} — {name_map[t]}" if name_map[t] != t else t
    cx, cy = st.columns(2)
    with cx:
        xsel = st.multiselect(
            "X-axis stocks", options=list(name_map.keys()), format_func=_fmt,
            placeholder="Add stocks, or load from a watchlist/tag above…",
            key="corr_tickers_x",
        )
    with cy:
        ysel = st.multiselect(
            "Y-axis stocks", options=list(name_map.keys()), format_func=_fmt,
            placeholder="Empty → mirror the X axis",
            key="corr_tickers_y",
        )

    xsel = list(dict.fromkeys(xsel))
    ysel = list(dict.fromkeys(ysel))
    # Mirror rule: an empty axis copies the other; both empty = all stocks.
    if not xsel and not ysel:
        xset = yset = list(name_map.keys())
        st.caption("Showing **all stocks** on both axes — add stocks above to narrow, or split the axes.")
    elif not ysel:
        xset = yset = xsel
    elif not xsel:
        xset = yset = ysel
    else:
        xset, yset = xsel, ysel

    union = list(dict.fromkeys(xset + yset))
    if len(union) < 2:
        st.info("Need at least two distinct stocks to compute correlations.")
        return

    # ── Controls ──────────────────────────────────────────────────────────────
    c_win, c_freq, c_thr = st.columns([2, 2, 3])
    with c_win:
        window = st.radio("Lookback window", list(_CORR_WINDOWS.keys()),
                          index=1, horizontal=True, key="corr_window")
    with c_freq:
        freq = st.radio("Return frequency", list(_CORR_FREQ.keys()),
                        index=1, horizontal=True, key="corr_freq")
    with c_thr:
        threshold = st.slider("High-correlation threshold", 0.50, 0.99, 0.60, 0.01,
                              key="corr_threshold")
        st.caption("Pairs at or above this ρ are flagged as redundancy candidates.")

    # ── Compute gate ──────────────────────────────────────────────────────────
    # The fetch + correlation is expensive, so don't run it on page open or on
    # every stock change. Compute only when the button is pressed; keep the last
    # result until the stock set / window / frequency changes.
    sig = (tuple(sorted(union)), window, freq)
    if st.button("📊 Compute correlations", type="primary", key="corr_compute"):
        st.session_state["corr_sig"] = sig
    if st.session_state.get("corr_sig") != sig:
        st.info("Pick your stocks and options above, then press **📊 Compute correlations**.")
        return

    # ── Fetch, slice, resample, returns ──────────────────────────────────────
    with st.spinner("Fetching price history…"):
        hist = _fetch_perf_history(tuple(sorted(union)))

    missing = [t for t in union if t not in hist or hist[t].empty]
    if missing:
        st.warning(f"No price history for: {', '.join(sorted(missing))}")
    have = [t for t in union if t in hist and not hist[t].empty]
    if len(have) < 2:
        st.info("Not enough stocks with price history to compute correlations.")
        return

    prices = pd.DataFrame({t: hist[t] for t in have}).sort_index()
    offset = _CORR_WINDOWS[window]
    if offset is not None:
        cutoff = prices.index[-1] - offset
        prices = prices[prices.index >= cutoff]

    rule, periods_per_year, min_obs = _CORR_FREQ[freq]
    sampled = prices if rule is None else prices.resample(rule).last()
    returns = sampled.pct_change().dropna(how="all")

    # Drop columns that don't have enough observations to correlate at all.
    enough = [c for c in returns.columns if returns[c].notna().sum() >= min_obs]
    thin = [c for c in returns.columns if c not in enough]
    if thin:
        st.warning(
            f"Too little data in this window for: {', '.join(sorted(thin))} "
            f"(need ≥{min_obs} {freq.lower()} observations)."
        )
    returns = returns[enough]
    if returns.shape[1] < 2:
        st.info("Not enough stocks with sufficient history in this window.")
        return

    corr = returns.corr(min_periods=min_obs)

    # Restrict each axis to the stocks that survived the data checks.
    avail = set(returns.columns)
    x_labels = [t for t in xset if t in avail]
    y_labels = [t for t in yset if t in avail]
    if not x_labels or not y_labels:
        st.info("Not enough stocks with sufficient history on both axes.")
        return

    meta = get_ticker_meta()
    def _name(t: str) -> str:
        v = meta.get(t)
        return v[0] if (v and v[0]) else t

    st.caption(
        f"X: {len(x_labels)} · Y: {len(y_labels)} stocks · {freq.lower()} returns · "
        f"{window} window · as of {prices.index[-1].strftime('%Y-%m-%d')}"
    )

    # ── View a: heatmap ───────────────────────────────────────────────────────
    st.subheader("Correlation heatmap")
    import plotly.graph_objects as go
    # When both axes hold the same set, hide the mirror half (keep the lower
    # triangle, i > j) since the upper half is redundant and the diagonal is 1.
    # When the axes differ, show the full rectangle but blank self-pairs (ρ = 1).
    symmetric = x_labels == y_labels
    z, text = [], []
    heatmap_pairs: set[frozenset] = set()
    for i, yt in enumerate(y_labels):
        zrow, trow = [], []
        for j, xt in enumerate(x_labels):
            v = corr.loc[yt, xt]
            show = pd.notna(v) and xt != yt and (not symmetric or i > j)
            zrow.append(float(v) if show else None)
            trow.append(f"{v:.2f}" if show else "")
            if show:
                heatmap_pairs.add(frozenset((xt, yt)))
        z.append(zrow)
        text.append(trow)
    fig = go.Figure(go.Heatmap(
        z=z, x=x_labels, y=y_labels,
        zmin=-1, zmax=1, zmid=0,
        # Diverging blue↔red with a neutral gray midpoint: red = high positive
        # correlation (the thing we're flagging), blue = negative, gray ≈ 0.
        colorscale=[[0.0, "#2a78d6"], [0.5, "#f0efec"], [1.0, "#d03b3b"]],
        colorbar=dict(title="ρ"),
        text=text, texttemplate="%{text}", textfont=dict(size=10),
        hoverongaps=False,
        hovertemplate="%{y} vs %{x}<br>ρ = %{z:.2f}<extra></extra>",
    ))
    fig.update_yaxes(autorange="reversed")
    fig.update_layout(height=max(380, 26 * len(y_labels) + 160),
                      margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig, use_container_width=True)

    # ── View b: most-correlated pairs ────────────────────────────────────────
    st.subheader("Most-correlated pairs")
    cols = list(corr.columns)
    pair_rows = []
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            a, b = cols[i], cols[j]
            # Only pairs that are actually drawn in the heatmap above.
            if frozenset((a, b)) not in heatmap_pairs:
                continue
            rho = corr.iat[i, j]
            if pd.isna(rho):
                continue
            obs = int(returns[[a, b]].dropna().shape[0])
            pair_rows.append({
                "A": a, "Name A": _name(a), "B": b, "Name B": _name(b),
                "ρ": float(rho), "Obs": obs,
            })
    if not pair_rows:
        st.info("No pairs in the heatmap had enough overlapping data to correlate.")
        return
    pairs_df = pd.DataFrame(pair_rows).sort_values("ρ", ascending=False).reset_index(drop=True)

    def _hi(row):
        hot = row["ρ"] >= threshold
        return ["background-color: #fde2e1" if hot else "" for _ in row]

    st.dataframe(
        pairs_df.style.apply(_hi, axis=1).format({"ρ": "{:.2f}"}),
        use_container_width=True, hide_index=True,
    )
    st.caption(f"Rows highlighted in red have ρ ≥ {threshold:.2f}.")

    # ── View c: redundancy / pruning helper ──────────────────────────────────
    st.subheader("Redundancy — candidates to trim")
    hot_pairs = pairs_df[pairs_df["ρ"] >= threshold]
    if hot_pairs.empty:
        st.info(f"No pairs reach ρ ≥ {threshold:.2f} in this window.")
        return

    stats = {
        t: _corr_ticker_stats(prices[t], returns[t], periods_per_year)
        for t in cols
    }

    def _fmt_pct(x):
        return "—" if x is None else f"{x * 100:.1f}%"

    def _fmt_ratio(x):
        return "—" if x is None else f"{x:.2f}"

    prune_rows = []
    for _, r in hot_pairs.iterrows():
        a, b = r["A"], r["B"]
        ra = stats[a]["ratio"]
        rb = stats[b]["ratio"]
        # Keep the higher return/risk ratio; an unknown ratio loses the tie-break.
        if (rb if rb is not None else -1e9) > (ra if ra is not None else -1e9):
            keep, drop = b, a
        else:
            keep, drop = a, b
        prune_rows.append({
            "Pair": f"{a} ↔ {b}",
            "ρ": float(r["ρ"]),
            "Keep": f"{keep} ({_name(keep)})",
            "Keep ret/risk": _fmt_ratio(stats[keep]["ratio"]),
            "Keep CAGR": _fmt_pct(stats[keep]["cagr"]),
            "Drop candidate": f"{drop} ({_name(drop)})",
            "Drop ret/risk": _fmt_ratio(stats[drop]["ratio"]),
            "Drop CAGR": _fmt_pct(stats[drop]["cagr"]),
        })
    prune_df = pd.DataFrame(prune_rows)
    st.dataframe(
        prune_df.style.format({"ρ": "{:.2f}"}),
        use_container_width=True, hide_index=True,
    )
    st.caption(
        "For each flagged pair, the stock with the higher return-per-unit-of-risk "
        "(CAGR ÷ annualized volatility) is suggested to keep. Review before acting — "
        "a drop candidate may still serve a purpose (sector exposure, lower drawdown)."
    )


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


# ── Level detail charts (click a ticker in the Overview to open) ─────────────
@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_ohlc(ticker: str) -> pd.DataFrame:
    """Daily OHLC for one ticker (up to 16y). Empty frame on failure.

    Uses auto_adjust=False so the candles are UNADJUSTED (split-adjusted only, not
    dividend-adjusted) — matching TradingView's default chart and the prices you
    record levels from. auto_adjust=True would back-adjust every historical bar
    down by the cumulative dividend factor, so bars would sit a few % below
    TradingView (and your level lines wouldn't line up with the bars)."""
    try:
        d = yf.download(ticker, period="16y", interval="1d",
                        auto_adjust=False, progress=False, threads=True)
    except Exception:
        return pd.DataFrame()
    if d is None or d.empty:
        return pd.DataFrame()
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    keep = [c for c in ("Open", "High", "Low", "Close") if c in d.columns]
    return d[keep].dropna()


def _resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample daily OHLC into a coarser candle (first/max/min/last)."""
    if df.empty:
        return df
    return df.resample(rule).agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    ).dropna()


def _candle_fig(df: pd.DataFrame, title: str, levels: list[float], height: int):
    """A log-scale candlestick with a horizontal line at each recorded level."""
    import math
    import plotly.graph_objects as go
    fig = go.Figure(data=[go.Candlestick(
        x=df.index, open=df["Open"], high=df["High"],
        low=df["Low"], close=df["Close"], name=title, showlegend=False,
    )])
    for p in levels:
        if p and p > 0:
            fig.add_hline(
                y=p, line=dict(color="#2962ff", width=1, dash="dash"),
                annotation_text=f"{p:g}", annotation_position="top left",
                annotation_font=dict(color="#2962ff", size=11),
            )
    # Candlesticks don't autorange on a log axis (Plotly draws the whole thing
    # flat near the bottom with an absurd max), so set the range explicitly from
    # the data and the level lines.
    pts = [float(p) for p in levels if p and p > 0]
    lo = min([float(df["Low"].min())] + pts)
    hi = max([float(df["High"].max())] + pts)
    if lo > 0 and hi > lo:
        fig.update_yaxes(type="log", range=[math.log10(lo * 0.95), math.log10(hi * 1.05)])
    else:
        fig.update_yaxes(type="log")
    fig.update_layout(
        title=dict(text=title, font=dict(size=14)), height=height,
        margin=dict(l=10, r=10, t=36, b=10),
        xaxis_rangeslider_visible=False,
    )
    return fig


def _render_level_charts(ticker: str, name: str, levels: list[dict]):
    """Pop-up body: monthly + weekly side by side, daily full width below."""
    st.markdown(f"### {name} · `{ticker}`")
    try:
        import plotly.graph_objects as go  # noqa: F401
    except ModuleNotFoundError:
        st.warning("Install `plotly` to see the interactive log-scale charts.")
        return
    with st.spinner("Fetching price history…"):
        daily = _fetch_ohlc(ticker)
    if daily.empty:
        st.warning(f"No price history available for {ticker}.")
        return

    lv_prices = [float(lv["price"]) for lv in levels if lv.get("price")]
    monthly = _resample_ohlc(daily, "ME")               # full history
    weekly = _resample_ohlc(daily, "W-FRI").tail(260)   # ≈ 5 years
    daily_v = daily.tail(252)                           # ≈ 1 year

    st.caption(
        ("Dashed blue lines mark your recorded levels. " if lv_prices
         else "No levels recorded for this stock yet. ")
        + "Y-axis is log-scaled; drag to zoom."
    )

    c1, c2 = st.columns(2)
    with c1:
        st.plotly_chart(_candle_fig(monthly, "Monthly · full history", lv_prices, 340),
                        use_container_width=True)
    with c2:
        st.plotly_chart(_candle_fig(weekly, "Weekly · ≈5y", lv_prices, 340),
                        use_container_width=True)
    st.plotly_chart(_candle_fig(daily_v, "Daily · ≈1y", lv_prices, 420),
                    use_container_width=True)


# ── Stock levels page ────────────────────────────────────────────────────────
def render_levels_page():
    st.title("🎚️ Levels")
    st.caption(
        "Record the price levels that matter for each stock — support, resistance, "
        "buy/sell targets, stops — with a note per level and a general comment per stock."
    )

    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT DISTINCT i.ticker, COALESCE(n.name, i.ticker) AS display_name
            FROM {TBL_ITEMS} i
            LEFT JOIN {TBL_TICKER_NAMES} n ON n.ticker = i.ticker
            ORDER BY i.ticker
        """).fetchall()
    if not rows:
        st.info("No tickers found in your watchlists. Add some stocks first.")
        return
    name_map = {t: name for t, name in rows}

    tab_edit, tab_overview, tab_types = st.tabs(
        ["✏️ Create / edit", "📋 Overview", "⚙️ Types"]
    )

    # ── Create / edit a single stock ──────────────────────────────────────────
    with tab_edit:
        _fmt = lambda t: f"{t} — {name_map[t]}" if name_map[t] != t else t
        ticker = st.selectbox(
            "Stock", options=list(name_map.keys()), format_func=_fmt, key="levels_ticker"
        )

        # ── General comment ────────────────────────────────────────────────────
        st.subheader("General comment")
        note = st.text_area(
            "Your overall notes for this stock",
            value=get_stock_note(ticker),
            key=f"stock_note_{ticker}",
            label_visibility="collapsed",
            placeholder="e.g. Accumulate on weakness, long-term hold…",
        )
        if st.button("Save comment", type="primary", key=f"save_note_{ticker}"):
            set_stock_note(ticker, note)
            st.success("Comment saved.")
            st.rerun()

        # ── Price levels ───────────────────────────────────────────────────────
        st.subheader("Price levels")
        levels = get_levels(ticker)
        type_options = get_level_types()
        # Keep any legacy type still on a saved level in the options, so the
        # selectbox never chokes on a value that was later removed from Types.
        type_options = list(dict.fromkeys(type_options + [lv["level_type"] for lv in levels]))
        default_type = type_options[0] if type_options else "Support"
        editor_df = pd.DataFrame(
            {
                "Price": pd.Series([lv["price"] for lv in levels], dtype="float64"),
                "Type": pd.Series([lv["level_type"] for lv in levels], dtype="object"),
                "Note": pd.Series([lv["note"] for lv in levels], dtype="object"),
            }
        )
        edited = st.data_editor(
            editor_df,
            num_rows="dynamic",
            hide_index=True,
            use_container_width=True,
            column_config={
                "Price": st.column_config.NumberColumn(
                    "Price", format="%.2f", required=True, min_value=0.0
                ),
                "Type": st.column_config.SelectboxColumn(
                    "Type", options=type_options, required=True, default=default_type
                ),
                "Note": st.column_config.TextColumn("Note", help="Optional note for this level"),
            },
            key=f"levels_editor_{ticker}",
        )
        if st.button("Save levels", type="primary", key=f"save_levels_{ticker}"):
            new_levels = []
            skipped = 0
            for _, r in edited.iterrows():
                price = r["Price"]
                if price is None or pd.isna(price):
                    skipped += 1
                    continue
                new_levels.append(
                    {
                        "price": float(price),
                        "level_type": r["Type"] or default_type,
                        "note": (r["Note"] or "").strip(),
                    }
                )
            replace_levels(ticker, new_levels)
            msg = f"Saved {len(new_levels)} level(s)."
            if skipped:
                msg += f" Skipped {skipped} row(s) with no price."
            st.success(msg)
            st.rerun()

    # ── Overview of everything recorded so far ─────────────────────────────────
    with tab_overview:
        levels_map = get_levels_map()
        notes_map = get_stock_notes_map()
        recorded = sorted(set(levels_map) | set(notes_map))
        if not recorded:
            st.info("Nothing recorded yet. Add levels or a comment in the other tab.")
        else:
            with st.spinner("Fetching current prices…"):
                quotes = fetch_quotes(recorded)
            price_map = dict(zip(quotes["Ticker"], quotes["Price"]))

            rows = []
            for t in recorded:
                cur = price_map.get(t)
                comment = notes_map.get(t, "")
                tlevels = levels_map.get(t, [])
                if tlevels:
                    for lv in tlevels:
                        level = lv["price"]
                        # Signed: level below current price → negative.
                        dist = (level - cur) / cur * 100 if cur else None
                        rows.append({
                            "Ticker": t,
                            "Name": name_map.get(t, t),
                            "Type": lv["level_type"],
                            "Level": level,
                            "Current": cur,
                            "Distance %": dist,
                            "Note": lv["note"],
                            "Comment": comment,
                        })
                else:
                    rows.append({
                        "Ticker": t,
                        "Name": name_map.get(t, t),
                        "Type": "",
                        "Level": None,
                        "Current": cur,
                        "Distance %": None,
                        "Note": "",
                        "Comment": comment,
                    })

            overview_df = pd.DataFrame(rows)
            meta = get_ticker_meta()

            def _dist_bg(col):
                out = []
                for v in col:
                    if v is None or pd.isna(v):
                        out.append("")
                    elif v >= 0:
                        out.append("background-color: #39b36a; color: #ffffff")  # above → green
                    else:
                        out.append("background-color: #e4584f; color: #ffffff")  # below → red
                return out

            def _to_display(df):
                """Turn Ticker / Level into link URLs (label encoded for the regex)."""
                d = df.copy()
                raw = d["Ticker"].astype(str)
                # Level → link to the standalone chart view; price kept for the label.
                d["Level"] = [
                    "" if (lv is None or pd.isna(lv))
                    else f"?lvlchart={quote(t, safe='')}&p={lv:.2f}"
                    for t, lv in zip(raw, d["Level"])
                ]
                # Ticker → TradingView; real symbol appended so the label regex can show it.
                d["Ticker"] = [
                    f"{tradingview_url(t, meta.get(t, ('', '', ''))[1] or '')}&yfsym={t}"
                    for t in raw
                ]
                return d

            link_cfg = {
                "Ticker": st.column_config.LinkColumn(
                    "Ticker", display_text=r"yfsym=(.+)$", help="Open chart on TradingView ↗"),
                "Level": st.column_config.LinkColumn(
                    "Level", display_text=r"&p=([\d.]+)$",
                    help="Open monthly / weekly / daily charts with your levels ↗"),
            }

            def _show(df, cols):
                if df.empty:
                    st.caption("No rows.")
                    return
                styler = (
                    _to_display(df)[cols].style
                    .apply(_dist_bg, subset=["Distance %"])
                    .format({"Current": "{:.2f}", "Distance %": "{:+.2f}%"}, na_rep="—")
                    .hide(axis="index")
                )
                height = (min(len(df), 60) + 1) * 35 + 3
                st.dataframe(styler, column_config=link_cfg, hide_index=True,
                             use_container_width=True, height=height)

            cols_full = ["Ticker", "Name", "Type", "Level", "Current", "Distance %", "Note", "Comment"]
            cols_grouped = ["Ticker", "Name", "Level", "Current", "Distance %", "Note", "Comment"]

            # ── Filter / group controls ─────────────────────────────────────────
            present = set(overview_df["Type"])
            type_order = [t for t in get_level_types() if t in present]
            type_order += [t for t in overview_df["Type"].unique()
                           if t and t not in type_order]
            c_filt, c_grp = st.columns([3, 1])
            with c_filt:
                sel_types = st.multiselect(
                    "Filter by type", options=type_order,
                    placeholder="All types", key="levels_overview_filter",
                )
            with c_grp:
                group = st.checkbox("Group by type", key="levels_overview_group")

            st.caption(
                "Click a **Level** to open monthly / weekly / daily charts with your "
                "levels; click a **ticker** to open it on TradingView ↗."
            )

            view = overview_df
            if sel_types:
                view = view[view["Type"].isin(sel_types)]

            if group:
                shown = [t for t in type_order if not sel_types or t in sel_types]
                for t in shown:
                    sub = view[view["Type"] == t]
                    if sub.empty:
                        continue
                    st.markdown(f"**{t}**")
                    _show(sub, cols_grouped)
                # Comment-only rows (no type) — only when not filtering to specific types
                if not sel_types:
                    rest = view[view["Type"] == ""]
                    if not rest.empty:
                        st.markdown("**(no type)**")
                        _show(rest, cols_grouped)
            else:
                _show(view, cols_full)

            st.caption(
                "Distance is the gap from the current price to the level, as a % of the "
                "current price. Negative (red) = level below the current price; "
                "positive (green) = above."
            )


    # ── Maintain the level types ───────────────────────────────────────────────
    with tab_types:
        st.caption(
            "Maintain the list of level types available in the **Type** dropdown. "
            "Reorder, rename, add or remove rows, then save. Existing levels keep "
            "their stored type even if you remove it here."
        )
        types = get_level_types()
        types_df = pd.DataFrame({"Type": pd.Series(types, dtype="object")})
        edited_types = st.data_editor(
            types_df,
            num_rows="dynamic",
            hide_index=True,
            use_container_width=True,
            column_config={
                "Type": st.column_config.TextColumn("Type", required=True),
            },
            key="level_types_editor",
        )
        if st.button("Save types", type="primary", key="save_level_types"):
            new_types = [(r["Type"] or "").strip() for _, r in edited_types.iterrows()]
            new_types = [t for t in new_types if t]
            if not new_types:
                st.warning("Keep at least one type — nothing saved.")
            else:
                set_level_types(new_types)
                st.success(f"Saved {len(dict.fromkeys(new_types))} type(s).")
                st.rerun()


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
    st.Page(render_correlation_page, title="Correlation", icon="🔗", url_path="correlation"),
    st.Page(render_levels_page, title="Levels", icon="🎚️", url_path="levels"),
    st.Page(render_search_page, title="Search", icon="🔎", url_path="search"),
]

settings_pages = [
    st.Page(_edit_page, title="Edit Watchlists", icon="✏️", url_path="edit"),
    st.Page(render_refresh_screen, title="Refresh Options", icon="🔄", url_path="refresh"),
]

# ── Standalone level-chart view ──────────────────────────────────────────────
# Opened (in a new tab) from a Level link in the Levels → Overview table, via a
# `?lvlchart=<ticker>` query param. Rendered before navigation so it shows the
# charts on their own, regardless of which page the link was clicked from.
_chart_tkr = st.query_params.get("lvlchart")
if _chart_tkr:
    _nm = (get_ticker_meta().get(_chart_tkr, ("", "", ""))[0]) or _chart_tkr
    if st.button("← Back", help="Close this chart view"):
        st.query_params.clear()
        st.rerun()
    _render_level_charts(_chart_tkr, _nm, get_levels(_chart_tkr))
    st.stop()

nav = st.navigation({
    "Watchlists": watchlist_pages,
    "Analysis": analysis_pages,
    "Settings": settings_pages,
})
nav.run()
