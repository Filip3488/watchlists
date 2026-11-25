# ── config.py ────────────────────────────────────────────────────────────────
# Single source of truth for every table, sequence, env-key, and seed-data
# name used by watchlist.py and populate_names.py.
#
# Rename anything here and the whole app picks it up — no hunting for string
# literals scattered across SQL queries.
# ─────────────────────────────────────────────────────────────────────────────

# ── Environment variable / secret keys ───────────────────────────────────────
TOKEN_ENV_KEY = "MOTHERDUCK_TOKEN"   # MotherDuck access token
DB_ENV_KEY    = "DB_NAME"            # MotherDuck database name key
DB_DEFAULT    = "watchlists"         # default value when DB_NAME is not set

# ── Table names ───────────────────────────────────────────────────────────────
TBL_WATCHLISTS     = "watchlists"
TBL_ITEMS          = "items"
TBL_STOCK_TAGS     = "stock_tags"
TBL_STOCK_LEVELS   = "stock_levels"
TBL_STOCK_NOTES    = "stock_notes"
TBL_LEVEL_TYPES    = "level_types"
TBL_TICKER_NAMES   = "ticker_names"
TBL_PERF_PRESETS   = "perf_presets"
TBL_FILTER_PRESETS = "filter_presets"
TBL_TAG_GROUPS     = "tag_groups"
TBL_TAG_GROUP_MAP  = "tag_group_map"
TBL_LEVEL_ALERT_DISMISS = "level_alert_dismiss"

# ── Sequence names ────────────────────────────────────────────────────────────
SEQ_WATCHLISTS   = "seq_watchlists"
SEQ_ITEMS        = "seq_items"
SEQ_STOCK_LEVELS = "seq_stock_levels"

# ── Seed / default data ───────────────────────────────────────────────────────
# Loaded into the DB on first run when the tables are empty.

DEFAULT_WATCHLISTS: dict[str, list[str]] = {
    "US Tech":       ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AMD", "INTC", "ORCL"],
    "US Finance":    ["JPM", "BAC", "GS", "MS", "WFC", "C", "BLK", "AXP", "V", "MA"],
    "EU Blue Chips": ["SAP.DE", "ASML.AS", "LVMH.PA", "MC.PA", "SIE.DE", "ALV.DE", "BAS.DE", "BMW.DE", "DTE.DE", "ADS.DE"],
    "Crypto":        ["BTC-USD", "ETH-USD", "BNB-USD", "SOL-USD", "XRP-USD", "ADA-USD", "DOGE-USD", "AVAX-USD", "DOT-USD", "MATIC-USD"],
}

TAGS: list[str] = ["Sell", "Stock up", "Monitor"]

# Displayed in the per-stock price-level tracker (Analysis → Levels tab).
LEVEL_TYPES: list[str] = ["Support", "Resistance", "Buy target", "Sell target", "Stop-loss"]
