"""Populate the stock master-data table in MotherDuck from financedatabase.

Loads the primary-listing equities from the (offline, no-API-key) financedatabase
package and writes them to MotherDuck as a FULL LOAD: the target table is dropped
and recreated from scratch on every run via CREATE OR REPLACE TABLE.

Usage:
    python populate_stock_master_data.py
"""
from __future__ import annotations

import financedatabase as fd

from config import TBL_STOCK_MASTER
from populate_names import connect


def main() -> None:
    # Lädt die Aktien-Datenbank (kein API-Key nötig)
    equities = fd.Equities()

    # Nur die Hauptnotierung (Primary Listing) je Unternehmen abrufen
    df_primary = equities.select(only_primary_listing=True).reset_index()
    # Align with the rest of the schema (ticker_names etc.): symbol → ticker.
    df_primary = df_primary.rename(columns={"symbol": "ticker"})
    print(f"Fetched {len(df_primary):,} primary-listing equities from financedatabase.")

    conn = connect()
    try:
        # Full load: drop + recreate atomically from the local DataFrame.
        conn.execute(
            f"CREATE OR REPLACE TABLE {TBL_STOCK_MASTER} AS SELECT * FROM df_primary"
        )
        rows = conn.execute(f"SELECT COUNT(*) FROM {TBL_STOCK_MASTER}").fetchone()[0]
        print(f"Wrote {rows:,} rows to MotherDuck table '{TBL_STOCK_MASTER}'.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
