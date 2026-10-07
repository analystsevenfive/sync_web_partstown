"""Backfill missing Parts Town prices across saved brand catalogs.

Uses the user's regular signed-in Chrome session, visits only rows missing List
Price or My Price, follows replacement links, and checkpoints every product.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

import partstown_sync
import partstown_full_sync as full_sync

BASE_DIR = Path(__file__).resolve().parent
BRANDS = {
    "Bakers Pride": (BASE_DIR / "partstown_data.db", "bakers-pride"),
    "Middleby": (BASE_DIR / "partstown_data.db", "middleby"),
    "CTX": (BASE_DIR / "partstown_ctx_data.db", "ctx"),
    "Pitco": (BASE_DIR / "partstown_pitco_data.db", "pitco"),
    "Crown Steam": (BASE_DIR / "partstown_crown-steam_data.db", "crown-steam"),
}
PAGE_SIZE = 24


def quantity_from_body(body: str) -> str:
    match = re.search(r"Quantity Available\s*:?\s*([\d,]+)", body, re.I)
    if not match:
        match = re.search(r"\bQuantity\s*:?\s*([\d,]+)", body, re.I)
    return match.group(1).replace(",", "") if match else ""


def current_parts_town_number(body: str) -> str | None:
    match = re.search(r"Parts Town #\s*:?\s*([A-Z0-9-]+)", body, re.I)
    return match.group(1).upper() if match else None


def replacement_number(body: str) -> str | None:
    match = re.search(r"Replaced by PT #\s*:?\s*([A-Z0-9-]+)", body, re.I)
    return match.group(1).upper() if match else None


def previous_numbers(body: str) -> list[str]:
    match = re.search(r"Previous Part #\s*:?(.*?)(?=\n\s*Units\s*:|\n\s*Fits Models\s*:|$)", body, re.I | re.S)
    if not match:
        return []
    return list(dict.fromkeys(
        item for item in re.findall(r"[A-Z0-9][A-Z0-9-]{2,}", match.group(1), re.I)
        if item.upper() not in {"SHOW", "MORE", "REPLACED"}
    ))


async def open_product(page, url: str) -> dict:
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.locator("h1").first.wait_for(state="visible", timeout=30000)
    # Price and stock fields hydrate after the product shell appears.
    try:
        await page.wait_for_function(
            "() => /(?:List Price|My Price|Quantity Available|Replaced by PT #)/i.test(document.body.innerText)",
            timeout=12000,
        )
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(1000)
    await page.wait_for_timeout(500)
    body = (await page.locator("body").inner_text(timeout=15000)).replace("\xa0", " ")
    title = (await page.locator("h1").first.inner_text()).strip()
    manufacturer_match = re.search(
        r"Manufacturer\s*:?\s*(.*?)(?=\s+Manufacturer #|\s+Parts Town #|\s+Previous Part #|\s+Units\s*:|$)",
        body, re.I | re.S,
    )
    replacement = replacement_number(body)
    replacement_url = None
    if replacement:
        links = await page.locator('a[href*="/p/"]').evaluate_all(
            "nodes => nodes.map(a => ({text:(a.innerText||'').trim(),href:a.href}))"
        )
        target = replacement.casefold()
        match = next((item for item in links if target in item["href"].casefold() or target in item["text"].casefold()), None)
        if match:
            replacement_url = match["href"]
        else:
            slug = urlparse(page.url).path.split("/p/")[-1].split("/")[0]
            replacement_url = f"https://www.partstown.com/p/{slug}/{replacement.lower()}"
    pt_no = current_parts_town_number(body)
    image = None
    if pt_no:
        code = full_sync.image_code_for(urlparse(page.url).path.split("/p/")[-1].split("/")[0], pt_no)
        if code:
            image = f"https://partstown.sirv.com/products/{code}/{pt_no}.view?thumb&image.rules=G0&w=200"
    return {
        "url": page.url,
        "title": title,
        "manufacturer": " ".join(manufacturer_match.group(1).split()) if manufacturer_match else None,
        "parts_town_number": pt_no,
        "replacement_number": replacement,
        "replacement_url": replacement_url,
        "list_price_usd": full_sync.parse_price(body, "List Price"),
        "my_price_usd": full_sync.parse_price(body, "My Price"),
        "quantity_available": quantity_from_body(body),
        "previous_part_numbers": previous_numbers(body),
        "image_url": image,
        "body": body,
    }


def load_jobs(db_file: Path, brand: str, limit: int | None, retry_attempted: bool) -> list[dict]:
    with sqlite3.connect(db_file, timeout=30) as conn:
        rows = conn.execute("""SELECT id,url,title,price,my_price,raw_json FROM products
          WHERE lower(brand)=lower(?) ORDER BY rowid""", (brand,)).fetchall()
    jobs = []
    for row in rows:
        meta = json.loads(row[5]) if row[5] else {}
        listing = meta.get("listing", {})
        # Recover any listing prices retained in raw_json before opening a PDP.
        lp = row[3] or listing.get("list_price_usd")
        mp = row[4] or listing.get("my_price_usd")
        if lp or mp:
            with sqlite3.connect(db_file, timeout=30) as conn:
                conn.execute("UPDATE products SET price=COALESCE(NULLIF(price,''),?), my_price=COALESCE(NULLIF(my_price,''),?) WHERE id=?", (lp, mp, row[0]))
            row = (*row[:3], lp, mp, row[5])
        if row[3] and row[4]:
            continue
        prior = meta.get("price_backfill", {})
        if prior.get("status") == "checked_no_price" and not retry_attempted:
            continue
        jobs.append({"id": row[0], "url": row[1], "title": row[2], "price": row[3], "my_price": row[4]})
    return jobs[:limit] if limit else jobs


def save_result(db_file: Path, brand: str, job: dict, chain: list[dict], error: str | None = None) -> None:
    if not chain:
        return
    chosen = next((item for item in reversed(chain) if item.get("list_price_usd") or item.get("my_price_usd")), chain[-1])
    with sqlite3.connect(db_file, timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        row = conn.execute("SELECT price,my_price,raw_json FROM products WHERE id=?", (job["id"],)).fetchone()
        if not row:
            return
        meta = json.loads(row[2]) if row[2] else {}
        replacement = len(chain) > 1 or (chosen.get("parts_town_number") and chosen["parts_town_number"] != job["id"])
        meta["price_backfill"] = {
            "status": "updated" if chosen.get("list_price_usd") or chosen.get("my_price_usd") else "checked_no_price",
            "captured_date": date.today().isoformat(),
            "attempted_at": datetime.now().isoformat(timespec="seconds"),
            "source_url": chosen.get("url"),
            "current_parts_town_number": chosen.get("parts_town_number"),
            "replacement_chain": [x.get("parts_town_number") or x.get("replacement_number") for x in chain],
            "error": error,
        }
        if replacement:
            meta["replacement_snapshot"] = {
                "original_parts_town_number": job["id"],
                "parts_town_number": chosen.get("parts_town_number"),
                "title": chosen.get("title"),
                "list_price_usd": chosen.get("list_price_usd"),
                "my_price_usd": chosen.get("my_price_usd"),
                "quantity_available": chosen.get("quantity_available"),
                "previous_part_numbers": ", ".join(chosen.get("previous_part_numbers", [])),
                "captured_date": date.today().isoformat(),
            }
        if chosen.get("quantity_available"):
            meta["quantity_available"] = chosen["quantity_available"]
        new_list = chosen.get("list_price_usd") or row[0]
        new_my = chosen.get("my_price_usd") or row[1]
        q = chosen.get("quantity_available")
        availability = None
        if replacement:
            if q and int(q) > 0:
                availability = f"Replacement in stock (quantity: {q})"
            elif q == "0":
                availability = "Replacement quantity 0"
            else:
                availability = "Replacement availability not shown"
        elif q:
            availability = f"Quantity available: {q}"
        previous = ", ".join(chosen.get("previous_part_numbers", [])) or None
        conn.execute("""UPDATE products SET price=?,my_price=?,availability=COALESCE(?,availability),
          title=CASE WHEN ? THEN ? ELSE title END,
          manufacturer=COALESCE(?,manufacturer),
          image=COALESCE(?,image),previous_part_numbers=COALESCE(?,previous_part_numbers),
          raw_json=?,synced_at=datetime('now') WHERE id=?""",
          (new_list, new_my, availability, int(replacement), chosen.get("title"), chosen.get("manufacturer") if replacement else None,
           chosen.get("image_url"), previous, json.dumps(meta, ensure_ascii=False), job["id"]))


async def backfill_brand(context, brand: str, db_file: Path, args) -> tuple[int, int, int]:
    jobs = load_jobs(db_file, brand, args.max_products, args.retry_attempted)
    print(f"{brand}: {len(jobs):,} rows missing at least one price field.", flush=True)
    queue: asyncio.Queue[dict] = asyncio.Queue()
    for job in jobs:
        queue.put_nowait(job)
    completed = updated = failed = 0
    lock = asyncio.Lock()

    async def worker(page):
        nonlocal completed, updated, failed
        while not queue.empty():
            job = await queue.get()
            chain: list[dict] = []
            error = None
            url = job["url"]
            visited: set[str] = set()
            try:
                for _ in range(4):
                    if not url or url in visited:
                        break
                    visited.add(url)
                    item = await open_product(page, url)
                    chain.append(item)
                    replacement_url = item.get("replacement_url")
                    if (item.get("list_price_usd") or item.get("my_price_usd")) and not replacement_url:
                        break
                    if replacement_url and replacement_url not in visited:
                        url = replacement_url
                        continue
                    break
                found = any(x.get("list_price_usd") or x.get("my_price_usd") for x in chain)
                save_result(db_file, brand, job, chain, error)
                async with lock:
                    completed += 1
                    if found:
                        updated += 1
                    if completed % 50 == 0 or completed == len(jobs):
                        print(f"{brand} PDP prices [{completed}/{len(jobs)}]; price data found={updated:,}; errors={failed}.", flush=True)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                if chain:
                    save_result(db_file, brand, job, chain, error)
                async with lock:
                    failed += 1
                    completed += 1
                    print(f"[{brand} PRICE ERROR] {job['id']} {job['url']}: {error}", flush=True)
            finally:
                queue.task_done()
                if args.delay:
                    try:
                        if page.is_closed():
                            page = await context.new_page()
                        else:
                            await page.wait_for_timeout(int(args.delay * 1000))
                    except Exception as exc:
                        # A closed tab must not cancel the other workers; replace it
                        # when the shared signed-in context is still available.
                        try:
                            page = await context.new_page()
                        except Exception:
                            print(f"[WORKER PAGE CLOSED] {exc}", flush=True)

    pages = [await context.new_page() for _ in range(min(args.workers, max(1, len(jobs))))] if jobs else []
    if pages:
        await asyncio.gather(*(worker(page) for page in pages))
    return len(jobs), updated, failed


async def run(args) -> int:
    selected = list(BRANDS) if not args.brand else args.brand
    unknown = [brand for brand in selected if brand not in BRANDS]
    if unknown:
        raise ValueError(f"Unknown brand(s): {', '.join(unknown)}. Choose from: {', '.join(BRANDS)}")
    first_slug = BRANDS[selected[0]][1]
    full_sync.BRAND_URL = f"https://www.partstown.com/b/{first_slug}"
    async with async_playwright() as playwright:
        full_sync.start_regular_chrome(args.cdp_port)
        browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{args.cdp_port}")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(full_sync.BRAND_URL, wait_until="domcontentloaded", timeout=60000)
        print("Price backfill opened regular Chrome. Sign in/complete verification manually; it waits for My Price.", flush=True)
        if not await full_sync.wait_for_signed_in(page, args.login_timeout):
            print("Timed out waiting for My Price; no prices were changed.", flush=True)
            return 2

        total_jobs = total_found = total_failed = 0
        for brand in selected:
            db_file, slug = BRANDS[brand]
            if not db_file.exists():
                print(f"{brand}: database not found ({db_file.name}); skipped.", flush=True)
                continue
            partstown_sync.DB_FILE = db_file
            jobs, found, failed = await backfill_brand(context, brand, db_file, args)
            total_jobs += jobs
            total_found += found
            total_failed += failed
            # Export a fresh brand workbook rather than overwrite an open workbook.
            from export_partstown_excel import main as export_excel
            filename = f"exports/partstown_{slug.replace('-', '_')}_prices_filled.xlsx"
            export_excel(brand=brand, output_file=filename)
        print(f"Finished: checked={total_jobs:,}; price data found={total_found:,}; page errors={total_failed:,}.", flush=True)
    return 0 if total_failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill missing Parts Town List Price and My Price fields.")
    parser.add_argument("--brand", action="append", choices=list(BRANDS), help="Brand to process (repeatable); default processes all saved brands.")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent product pages, 1-4 (default 4).")
    parser.add_argument("--delay", type=float, default=0.25, help="Pause after each product per worker in seconds.")
    parser.add_argument("--login-timeout", type=int, default=900, help="Seconds to wait for manual sign-in.")
    parser.add_argument("--cdp-port", type=int, default=9222, help="Chrome remote debugging port.")
    parser.add_argument("--max-products", type=int, help="Optional per-brand cap, useful for a small trial.")
    parser.add_argument("--retry-attempted", action="store_true", help="Retry rows previously checked without any price.")
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 4 or args.delay < 0 or args.login_timeout < 1:
        parser.error("workers must be 1-4, delay non-negative, and login timeout positive")
    if args.max_products is not None and args.max_products < 1:
        parser.error("--max-products must be positive")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
