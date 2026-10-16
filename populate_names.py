"""Enrich ticker metadata (name, exchange, currency) in the MotherDuck database
from Yahoo Finance.

The Streamlit app reads these fields *only* from the database and never calls
yfinance for them — because Yahoo's `.info` endpoint is routinely blocked on cloud
IPs (e.g. Streamlit Community Cloud). Run this script locally (where yfinance works)
to fill in anything missing; the deployed app then just reads the results.

Usage:
    python populate_names.py          # fetch only tickers that have no name yet
    python populate_names.py --all    # refetch everything (overwrites existing rows)

The same `enrich()` function is also called by the "Fetch missing names from Yahoo"
button on the app's Names tab.
"""
from __future__ import annotations

import os
from pathlib import Path

import duckdb
import yfinance as yf


def _load_env(path: Path) -> None:
    """Minimal .env loader (no extra dependency). Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def connect() -> duckdb.DuckDBPyConnection:
    """Open a standalone MotherDuck connection using the local .env (for CLI use)."""
    _load_env(Path(__file__).parent / ".env")
    token = os.environ.get("MOTHERDUCK_TOKEN")
    if not token:
        raise SystemExit("MOTHERDUCK_TOKEN is not set — add it to EmbeddedTradingView/.env")
    os.environ["motherduck_token"] = token
    return duckdb.connect(f"md:{os.environ.get('DB_NAME', 'watchlists')}")


def _ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
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
    cols = [r[1] for r in conn.execute("PRAGMA table_info('ticker_names')").fetchall()]
    if "exchange" not in cols:
        conn.execute("ALTER TABLE ticker_names ADD COLUMN exchange TEXT")
    if "currency" not in cols:
        conn.execute("ALTER TABLE ticker_names ADD COLUMN currency TEXT")


def _upsert(conn, ticker: str, name: str, exchange: str | None, currency: str | None) -> None:
    conn.execute("DELETE FROM ticker_names WHERE ticker = ?", [ticker])
    conn.execute(
        "INSERT INTO ticker_names (ticker, name, exchange, currency) VALUES (?, ?, ?, ?)",
        [ticker, name, exchange, currency],
    )


def enrich(conn: duckdb.DuckDBPyConnection, force: bool = False, log=lambda m: None) -> dict:
    """Fill in ticker metadata from yfinance.

    force=False: only tickers with no name yet (preserves existing/manual names).
    force=True:  every ticker in the watchlists (overwrites existing rows).

    Returns a summary dict: {total, processed, added, failed:[tickers]}.
    """
    _ensure_schema(conn)
    tickers = [r[0] for r in conn.execute(
        "SELECT DISTINCT ticker FROM items ORDER BY ticker"
    ).fetchall()]

    if force:
        todo = tickers
    else:
        have = {r[0] for r in conn.execute(
            "SELECT ticker FROM ticker_names WHERE name IS NOT NULL AND name <> ''"
        ).fetchall()}
        todo = [t for t in tickers if t not in have]

    added, failed = 0, []
    for t in todo:
        try:
            info = yf.Ticker(t).info
            name = info.get("longName") or info.get("shortName")
            if not name:
                failed.append(t)
                log(f"  ! {t}: no name returned")
                continue
            exchange = info.get("fullExchangeName") or info.get("exchange") or None
            currency = info.get("currency") or None
            _upsert(conn, t, name, exchange, currency)
            added += 1
            log(f"  + {t}: {name}")
        except Exception as e:
            failed.append(t)
            log(f"  ! {t}: {e}")

    return {"total": len(tickers), "processed": len(todo), "added": added, "failed": failed}


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Populate ticker metadata from Yahoo Finance.")
    ap.add_argument("--all", action="store_true",
                    help="refetch every ticker (overwrites existing names), not just missing ones")
    args = ap.parse_args()

    conn = connect()
    mode = "all tickers" if args.all else "tickers missing a name"
    print(f"Enriching {mode} from yfinance…")
    s = enrich(conn, force=args.all, log=print)
    print(f"\nDone. {s['total']} tickers total · processed {s['processed']} · "
          f"added {s['added']} · failed {len(s['failed'])}.")
    if s["failed"]:
        print("Failed:", ", ".join(s["failed"]))


if __name__ == "__main__":
    main()
