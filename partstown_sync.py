"""Polite Parts Town product catalog synchronizer.

Discovers English product URLs from the public sitemap and stores accessible
product metadata in SQLite. The target currently returns HTTP 403 to some
automated clients; those responses are reported and are not treated as data.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
SITEMAP_INDEX = "https://www.partstown.com/sitemap.xml"
DB_FILE = BASE_DIR / "partstown_data.db"
EXPORT_FILE = BASE_DIR / "exports" / "partstown_products.csv"
NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PartsTownCatalogSync/1.0; +https://www.partstown.com/)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def get_xml(response: requests.Response) -> ET.Element:
    body = response.content
    if response.url.endswith(".gz") or body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return ET.fromstring(body)


def product_sitemaps(client: requests.Session) -> list[str]:
    response = client.get(SITEMAP_INDEX, timeout=30)
    response.raise_for_status()
    root = get_xml(response)
    # Product sitemaps are the only large catalog indexes we need. Ignore model,
    # category and non-English duplicate URLs to keep the crawl focused.
    return sorted(
        loc.text.strip()
        for loc in root.findall(".//sm:loc", NS)
        if loc.text and re.search(r"/product-com-\d+\.xml\.gz$", loc.text)
    )


def product_urls(client: requests.Session, limit: int | None, brand: str | None) -> list[str]:
    urls: list[str] = []
    for sitemap_url in product_sitemaps(client):
        response = client.get(sitemap_url, timeout=45)
        response.raise_for_status()
        root = get_xml(response)
        for loc in root.findall(".//sm:loc", NS):
            if not loc.text:
                continue
            url = loc.text.strip()
            parsed = urlparse(url)
            path_match = re.match(r"^/p/([^/]+)/", parsed.path)
            if (
                parsed.netloc.lower() == "www.partstown.com"
                and path_match
                and (brand is None or path_match.group(1).casefold() == brand.casefold())
            ):
                urls.append(url)
                if limit and len(urls) >= limit:
                    return urls
    return urls


def flatten_product_json(value):
    if isinstance(value, list):
        for item in value:
            yield from flatten_product_json(item)
    elif isinstance(value, dict):
        kind = value.get("@type")
        if kind == "Product" or (isinstance(kind, list) and "Product" in kind):
            yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from flatten_product_json(child)


def parse_product(url: str, response: requests.Response) -> dict:
    soup = BeautifulSoup(response.text, "html.parser")
    product = None
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        product = next(flatten_product_json(payload), None)
        if product:
            break

    def meta(name: str, *, prop: bool = False) -> str | None:
        node = soup.find("meta", attrs={"property" if prop else "name": name})
        return node.get("content", "").strip() or None if node else None

    title = (product or {}).get("name") or meta("og:title", prop=True)
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    if not title:
        raise ValueError("Product title was not present in page metadata")

    offers = (product or {}).get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    brand = (product or {}).get("brand") or {}
    if isinstance(brand, dict):
        brand = brand.get("name")
    image = (product or {}).get("image")
    if isinstance(image, list):
        image = image[0] if image else None

    return {
        "id": url.rstrip("/").split("/")[-1],
        "url": url,
        "title": title,
        "brand": brand or urlparse(url).path.split("/")[2].replace("-", " ").title(),
        "sku": (product or {}).get("sku") or (product or {}).get("mpn"),
        "description": (product or {}).get("description") or meta("description"),
        "price": offers.get("price") if isinstance(offers, dict) else None,
        "currency": offers.get("priceCurrency") if isinstance(offers, dict) else None,
        "availability": offers.get("availability") if isinstance(offers, dict) else None,
        "image": image or meta("og:image", prop=True),
        "raw_json": json.dumps(product, ensure_ascii=False) if product else "{}",
    }


def init_db() -> None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL,
            brand TEXT, sku TEXT, description TEXT, price TEXT, my_price TEXT, currency TEXT,
            availability TEXT, image TEXT, manufacturer TEXT, previous_part_numbers TEXT,
            units TEXT, fits_models TEXT, fits_model_count INTEGER, fits_models_url TEXT,
            prop65_warning TEXT, raw_json TEXT, synced_at TEXT NOT NULL
        )""")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
        migrations = {
            "my_price": "TEXT", "manufacturer": "TEXT", "previous_part_numbers": "TEXT",
            "units": "TEXT", "fits_models": "TEXT", "fits_model_count": "INTEGER",
            "fits_models_url": "TEXT", "prop65_warning": "TEXT",
        }
        for name, sql_type in migrations.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE products ADD COLUMN {name} {sql_type}")


def save_product(item: dict) -> None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""INSERT INTO products
            (id,url,title,brand,sku,description,price,currency,availability,image,raw_json,synced_at)
            VALUES (:id,:url,:title,:brand,:sku,:description,:price,:currency,:availability,:image,:raw_json,datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
            url=excluded.url,title=excluded.title,brand=excluded.brand,sku=excluded.sku,
            description=excluded.description,price=excluded.price,currency=excluded.currency,
            availability=excluded.availability,image=excluded.image,raw_json=excluded.raw_json,
            synced_at=datetime('now')""", item)


def sync(limit: int | None, delay: float, workers: int, brand: str | None) -> None:
    init_db()
    client = session()
    print("Reading Parts Town product sitemap…")
    urls = product_urls(client, limit, brand)
    scope = f"brand '{brand}'" if brand else "all brands"
    print(f"Found {len(urls):,} English product URLs for {scope}; fetching with {workers} worker(s).")
    ok = failed = 0

    def fetch(url: str):
        response = client.get(url, timeout=25)
        response.raise_for_status()
        return parse_product(url, response)

    # Keep request pacing conservative and bounded. No login, cart or search
    # endpoints are requested by this sync.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for idx, url in enumerate(urls):
            if idx and delay:
                time.sleep(delay / workers)
            futures[pool.submit(fetch, url)] = url
        for done, future in enumerate(as_completed(futures), 1):
            url = futures[future]
            try:
                save_product(future.result())
                ok += 1
            except requests.HTTPError as exc:
                failed += 1
                status = exc.response.status_code if exc.response is not None else "?"
                print(f"[HTTP {status}] {url}")
            except Exception as exc:
                failed += 1
                print(f"[ERROR] {url}: {exc}")
            if done % 25 == 0 or done == len(urls):
                print(f"[{done}/{len(urls)}] synced={ok:,}, unavailable/errors={failed:,}")
    print(f"Finished: {ok:,} product records updated; {failed:,} pages unavailable or unparseable.")


def export_csv() -> None:
    init_db()
    EXPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_FILE) as conn, EXPORT_FILE.open("w", newline="", encoding="utf-8-sig") as out:
        rows = conn.execute("""SELECT id,title,brand,manufacturer,sku,price,my_price,currency,
            availability,previous_part_numbers,units,fits_models,fits_model_count,fits_models_url,
            prop65_warning,url,image,raw_json,synced_at FROM products ORDER BY brand,title""")
        writer = csv.writer(out)
        writer.writerow(["parts_town_number", "title", "listing_brand", "manufacturer", "manufacturer_part_number", "list_price_usd", "my_price_usd", "currency", "availability", "quantity_available", "previous_part_numbers", "units", "fits_models", "fits_model_count", "fits_models_url", "prop65_warning", "product_url", "image_url", "synced_at"])
        for row in rows:
            metadata = json.loads(row[17]) if row[17] else {}
            writer.writerow([*row[:9], metadata.get("quantity_available", ""), *row[9:17], row[18]])
    print(f"Exported to {EXPORT_FILE}")


def import_listing_csv(input_file: Path) -> None:
    """Import a read-only snapshot captured from the rendered brand listing."""
    init_db()
    imported = 0
    with input_file.open("r", newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            item = {
                "id": row["partstown_number"],
                "url": row.get("product_url") or row["source_url"],
                "title": row["title"],
                "brand": row["listing_brand"],
                "manufacturer": row.get("manufacturer") or row["listing_brand"],
                "sku": row["manufacturer_part_number"],
                "description": None,
                "price": row["price_usd"],
                "my_price": row.get("my_price_usd") or None,
                "currency": "USD",
                "availability": f"{row['availability']} (quantity: {row['quantity_available']})",
                "image": row.get("image_url") or None,
                "previous_part_numbers": row.get("previous_part_numbers") or None,
                "units": row.get("units") or None,
                "fits_models": row.get("fits_models") or None,
                "fits_model_count": int(row["fits_model_count"]) if row.get("fits_model_count") else None,
                "fits_models_url": row.get("fits_models_url") or None,
                "prop65_warning": row.get("prop65_warning") or None,
                "raw_json": json.dumps(row, ensure_ascii=False),
            }
            with sqlite3.connect(DB_FILE) as conn:
                conn.execute("""INSERT INTO products
                    (id,url,title,brand,manufacturer,sku,description,price,my_price,currency,availability,image,
                     previous_part_numbers,units,fits_models,fits_model_count,fits_models_url,prop65_warning,raw_json,synced_at)
                    VALUES (:id,:url,:title,:brand,:manufacturer,:sku,:description,:price,:my_price,:currency,:availability,:image,
                     :previous_part_numbers,:units,:fits_models,:fits_model_count,:fits_models_url,:prop65_warning,:raw_json,datetime('now'))
                    ON CONFLICT(id) DO UPDATE SET
                    url=excluded.url,title=excluded.title,brand=excluded.brand,manufacturer=excluded.manufacturer,sku=excluded.sku,
                    description=excluded.description,price=excluded.price,my_price=excluded.my_price,currency=excluded.currency,
                    availability=excluded.availability,image=excluded.image,previous_part_numbers=excluded.previous_part_numbers,
                    units=excluded.units,fits_models=excluded.fits_models,fits_model_count=excluded.fits_model_count,
                    fits_models_url=excluded.fits_models_url,prop65_warning=excluded.prop65_warning,raw_json=excluded.raw_json,
                    synced_at=datetime('now')""", item)
            imported += 1
    print(f"Imported {imported:,} rendered listing records from {input_file.name}.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync publicly listed Parts Town product pages to SQLite and CSV.")
    parser.add_argument("--brand", default="bakers-pride", help="Manufacturer slug from /b/<slug> (default: bakers-pride); use 'all' for every brand")
    parser.add_argument("--limit", type=int, help="Maximum product pages to fetch (default: all sitemap entries)")
    parser.add_argument("--delay", type=float, default=0.5, help="Approximate delay between page requests in seconds (default: 0.5)")
    parser.add_argument("--workers", type=int, default=1, help="Concurrent page requests (default: 1)")
    parser.add_argument("--export-csv", action="store_true", help="Export records already in SQLite instead of fetching")
    parser.add_argument("--import-listing-csv", type=Path, help="Import a read-only rendered listing snapshot into SQLite")
    parser.add_argument("--google-sheets", action="store_true", help="Publish the resulting database to the configured Google Sheet")
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 4:
        parser.error("--workers must be between 1 and 4 to keep requests modest")
    if args.delay < 0 or (args.limit is not None and args.limit < 1):
        parser.error("--delay must be non-negative and --limit must be positive")
    if args.import_listing_csv:
        import_listing_csv(args.import_listing_csv)
        export_csv()
    elif args.export_csv:
        export_csv()
    else:
        sync(args.limit, args.delay, args.workers, None if args.brand.casefold() == "all" else args.brand.strip("/"))
        export_csv()
    if args.google_sheets:
        from google_sheets_sync import main as sync_google_sheets
        sync_google_sheets()
    return 0


if __name__ == "__main__":
    sys.exit(main())
