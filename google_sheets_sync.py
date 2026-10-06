"""Publish the local Parts Town SQLite catalog to a Google Sheet."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

import partstown_sync

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
HEADERS = [
    "Parts Town #", "Title", "Listing Brand", "Manufacturer", "Manufacturer Part #",
    "List Price USD", "My Price USD", "Currency", "Availability", "Quantity Available",
    "Previous Part #", "Units", "Fits Models", "Fits Model Count", "Fits Models Link",
    "Proposition 65 Warning", "Product Link", "Image Link", "Last Synced",
]


def worksheet():
    spreadsheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not spreadsheet_id:
        raise RuntimeError("Set GOOGLE_SHEET_ID in the environment.")
    if not service_account_json:
        raise RuntimeError("Set GOOGLE_SERVICE_ACCOUNT_JSON to the service-account JSON contents.")
    credentials = Credentials.from_service_account_info(
        __import__("json").loads(service_account_json), scopes=SCOPES
    )
    client = gspread.authorize(credentials)
    spreadsheet = client.open_by_key(spreadsheet_id)
    try:
        return spreadsheet.worksheet("Parts Town")
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title="Parts Town", rows=1000, cols=len(HEADERS))


def main() -> int:
    partstown_sync.init_db()
    database = Path(os.environ.get("PARTSTOWN_DB", partstown_sync.DB_FILE))
    with sqlite3.connect(database) as conn:
        rows = conn.execute("""SELECT id,title,brand,manufacturer,sku,price,my_price,currency,
            availability,previous_part_numbers,units,fits_models,fits_model_count,fits_models_url,
            prop65_warning,url,image,raw_json,synced_at FROM products ORDER BY brand,title""").fetchall()

    values = [HEADERS]
    for row in rows:
        metadata = __import__("json").loads(row[17]) if row[17] else {}
        values.append([
            *row[:9], metadata.get("quantity_available", ""), *row[9:17], row[18]
        ])

    sheet = worksheet()
    sheet.clear()
    if values:
        sheet.update(values=values, range_name="A1", value_input_option="RAW")
    sheet.freeze(rows=1)
    sheet.format("A1:S1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.15, "green": 0.18, "blue": 0.22}, "horizontalAlignment": "CENTER"})
    print(f"Published {len(rows):,} product rows to Google Sheets worksheet 'Parts Town'.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Google Sheets sync failed: {error}", file=sys.stderr)
        sys.exit(1)
