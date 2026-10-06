"""Concurrent, resumable sync of a signed-in Parts Town brand catalog.

Run with a headed Chrome profile so the user can sign in manually. Listing
pages and PDPs are processed by a small pool of pages sharing that profile.
Progress is committed to SQLite after each product, so interrupted runs can
continue without redoing completed detail pages.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sqlite3
import sys
import subprocess
import time
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs
from urllib.request import urlopen

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

import partstown_sync

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / ".partstown-profile"
CDP_PROFILE_DIR = BASE_DIR / ".partstown-cdp-profile"
BRAND_URL = "https://www.partstown.com/b/bakers-pride"
IMAGE_CODES = {"bakers-pride": "BKP", "star": "STA", "magikitchn": "MK", "apw-wyott": "APW"}
PAGE_SIZE = 24


def parse_price(text: str, label: str) -> str | None:
    match = re.search(rf"{re.escape(label)}\s*:?\s*\$?\s*([\d,]+\.\d{{2}})", text, re.I)
    return match.group(1).replace(",", "") if match else None


def start_regular_chrome(port: int) -> None:
    """Start ordinary headed Chrome for manual sign-in, then attach over localhost CDP."""
    chrome_paths = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
    ]
    chrome = next((path for path in chrome_paths if path.exists()), None)
    if chrome is None:
        raise FileNotFoundError("Google Chrome was not found in Program Files.")
    CDP_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([
        str(chrome), f"--remote-debugging-port={port}", "--remote-allow-origins=*",
        f"--user-data-dir={CDP_PROFILE_DIR}", BRAND_URL,
    ])
    endpoint = f"http://127.0.0.1:{port}/json/version"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urlopen(endpoint, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise TimeoutError(f"Chrome did not open its local debugging endpoint at {endpoint}")


def fallback_image_url(product: dict) -> str | None:
    if product.get("image_url"):
        return product["image_url"]
    slug = urlparse(product["url"]).path.split("/p/")[-1].split("/")[0]
    code = IMAGE_CODES.get(slug)
    part_no = product.get("partstown_number")
    if code and part_no:
        return f"https://partstown.sirv.com/products/{code}/{part_no}.view?thumb&image.rules=G0&w=200"
    return None


async def extract_listing_items(page) -> list[dict]:
    return await page.evaluate("""() => {
      const anchors = [...document.querySelectorAll('a[href*="/p/"]')];
      const seen = new Set(), out = [];
      for (const a of anchors) {
        const url = new URL(a.getAttribute('href'), location.href);
        if (!/^\\/p\\/[^/]+\\/[^/]+/.test(url.pathname) || seen.has(url.href)) continue;
        let card = a;
        for (let i=0; i<10 && card.parentElement; i++) {
          if (/Parts Town\\s*#\\s*[A-Z0-9-]+/i.test(card.innerText || '')) break;
          card = card.parentElement;
        }
        const text = (card.innerText || '').replace(/\\s+/g, ' ').trim();
        const get = re => { const m = text.match(re); return m ? m[1].trim() : ''; };
        const imgs = [...card.querySelectorAll('img')].map(x => x.currentSrc || x.src).filter(Boolean);
        const title = [...card.querySelectorAll('h1,h2,h3,h4')].map(x => x.innerText.trim()).find(Boolean)
          || (a.innerText || a.getAttribute('aria-label') || '').trim();
        const brandSlug = url.pathname.split('/')[2] || '';
        const item = {
          url: url.href, title,
          partstown_number: get(/Parts Town\\s*#\\s*([A-Z0-9-]+)/i),
          manufacturer_part_number: get(/Mfr Part\\s*#\\s*([A-Z0-9-]+)/i),
          quantity_available: get(/(?:Quantity Available|Quantity)\\s*:?\\s*([\\d,]+)/i).replace(/,/g, ''),
          list_price_usd: get(/List Price\\s*:?\\s*\\$?([\\d,.]+)/i).replace(/,/g, ''),
          my_price_usd: get(/My Price\\s*:?\\s*\\$?([\\d,.]+)/i).replace(/,/g, ''),
          availability: (text.match(/In Stock[^.]*|Out of Stock|Backordered|Unavailable/i) || [''])[0],
          image_url: imgs[0] || '', brand_slug: brandSlug
        };
        if (!item.partstown_number) item.partstown_number = url.pathname.split('/').pop().toUpperCase();
        if (!item.title) item.title = item.partstown_number;
        seen.add(url.href);
        out.push(item);
      }
      return out;
    }""")


async def listing_page_count(page) -> tuple[int, int]:
    values = await page.evaluate("""() => {
      const tab = document.querySelector('#tab-parts');
      const countMatch = (tab?.innerText || document.body.innerText).match(/([\\d,]+)\\s*\\)?\\s*$/);
      const count = countMatch ? Number(countMatch[1].replace(/,/g,'')) : 0;
      const last = [...document.querySelectorAll('a[aria-label]')].find(a => a.getAttribute('aria-label')?.toLowerCase().includes('go to last page'));
      const href = last?.href || '';
      const page = Number(new URL(href || location.href).searchParams.get('page') || 0);
      return {count, lastPage: page};
    }""")
    total = int(values.get("count") or 0)
    last_page = int(values.get("lastPage") or 0)
    if total and last_page:
        return total, last_page + 1
    return total, max(1, math.ceil(total / PAGE_SIZE)) if total else 0


def save_listing(product: dict, snapshot_date: str) -> None:
    meta = {
        "listing": product,
        "snapshot_date": snapshot_date,
        "quantity_available": product.get("quantity_available", ""),
    }
    with sqlite3.connect(partstown_sync.DB_FILE, timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        old = conn.execute("SELECT raw_json FROM products WHERE id=?", (product["partstown_number"],)).fetchone()
        if old and old[0]:
            try:
                previous = json.loads(old[0])
                if previous.get("pdp_tabs"):
                    meta["pdp_tabs"] = previous["pdp_tabs"]
                if previous.get("detail"):
                    meta["detail"] = previous["detail"]
            except (json.JSONDecodeError, TypeError):
                pass
        conn.execute("""INSERT INTO products
          (id,url,title,brand,manufacturer,sku,description,price,my_price,currency,availability,image,raw_json,synced_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
          ON CONFLICT(id) DO UPDATE SET url=excluded.url,title=excluded.title,brand=excluded.brand,
          sku=COALESCE(excluded.sku,products.sku),price=excluded.price,my_price=excluded.my_price,
          currency='USD',availability=excluded.availability,image=excluded.image,
          raw_json=excluded.raw_json,synced_at=datetime('now')""",
          (product["partstown_number"], product["url"], product["title"], "Bakers Pride",
           None, product.get("manufacturer_part_number") or None, None,
           product.get("list_price_usd") or None, product.get("my_price_usd") or None, "USD",
           (product.get("availability") or "") + (f" (quantity: {product['quantity_available']})" if product.get("quantity_available") else ""),
           product.get("image_url"), json.dumps(meta, ensure_ascii=False)))


async def wait_for_signed_in(page, seconds: int) -> bool:
    for _ in range(max(1, seconds // 2)):
        body = (await page.locator("body").inner_text(timeout=5000)).replace("\xa0", " ")
        if re.search(r"My Price\s*\$", body, re.I):
            return True
        await page.wait_for_timeout(2000)
    return False


async def wait_for_catalog(page) -> None:
    await page.locator("#tab-parts").wait_for(state="visible", timeout=60000)
    await page.wait_for_function("document.querySelectorAll('a.product-card-name-full[href*=\"/p/\"]').length > 0", timeout=30000)


async def scrape_listing_pages(context, max_pages: int | None, workers: int, delay: float) -> list[dict]:
    pages = [context.pages[0]] if context.pages else []
    while len(pages) < workers:
        pages.append(await context.new_page())
    await pages[0].goto(BRAND_URL, wait_until="domcontentloaded", timeout=60000)
    await wait_for_catalog(pages[0])
    total, discovered_pages = await listing_page_count(pages[0])
    page_total = min(discovered_pages, max_pages) if max_pages else discovered_pages
    if page_total <= 0:
        raise RuntimeError("Could not read the total number of Parts Town catalog pages.")
    snapshot = date.today().isoformat()
    print(f"Listing reports {total:,} products across {discovered_pages:,} pages; syncing {page_total:,} page(s).", flush=True)
    queue: asyncio.Queue[int] = asyncio.Queue()
    for page_no in range(page_total):
        queue.put_nowait(page_no)
    found: dict[str, dict] = {}
    completed = 0
    lock = asyncio.Lock()

    async def worker(page):
        nonlocal completed
        while not queue.empty():
            page_no = await queue.get()
            try:
                url = BRAND_URL if page_no == 0 else f"{BRAND_URL}?page={page_no}"
                cards = []
                last_error = None
                for attempt in range(3):
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        await wait_for_catalog(page)
                        cards = await extract_listing_items(page)
                        if not cards:
                            raise RuntimeError("No product cards found after the page loaded.")
                        break
                    except Exception as exc:
                        last_error = exc
                        if attempt < 2:
                            await page.wait_for_timeout(2000 * (attempt + 1))
                if not cards:
                    raise RuntimeError(f"Failed after 3 attempts: {last_error}")
                for card in cards:
                    card["brand"] = "Bakers Pride"
                    card["source_url"] = BRAND_URL
                    card["snapshot_date"] = snapshot
                    code = IMAGE_CODES.get(card.pop("brand_slug", ""))
                    card["image_url"] = card.get("image_url") or (
                        f"https://partstown.sirv.com/products/{code}/{card['partstown_number']}.view?thumb&image.rules=G0&w=200"
                        if code else "")
                    save_listing(card, snapshot)
                    found[card["partstown_number"]] = card
                async with lock:
                    completed += 1
                    if completed % 10 == 0 or completed == page_total:
                        print(f"Listing pages [{completed}/{page_total}]; products captured={len(found):,}.", flush=True)
                if delay:
                    await page.wait_for_timeout(int(delay * 1000))
            except Exception as exc:
                async with lock:
                    completed += 1
                    print(f"[LISTING PAGE {page_no}] {exc}", flush=True)
            finally:
                queue.task_done()

    await asyncio.gather(*(worker(page) for page in pages))
    if len(found) != total and page_total == discovered_pages:
        print(f"Warning: listing total is {total:,}; captured {len(found):,} distinct product numbers.", flush=True)
    return list(found.values())


async def get_fits_state(page) -> dict:
    return await page.evaluate("""() => {
      const root = document.querySelector('#productFitsModels');
      const title = root?.querySelector('.fm-header__title')?.innerText || '';
      const count = Number((title.match(/([\\d,]+)\\s*models/i) || [])[1]?.replace(/,/g,'') || 0);
      const models = [...(root?.querySelectorAll('.js-model-item .js-edit-popup-name a') || [])]
        .map(a => a.innerText.trim()).filter(Boolean);
      return {count, models, more: root?.querySelector('.btn.btn-primary')?.innerText || ''};
    }""")


async def click_load_more(page) -> bool:
    button = page.locator("#productFitsModels .btn.btn-primary")
    if not await button.count():
        return False
    before = len((await get_fits_state(page))["models"])
    more_text = await button.inner_text()
    match = re.search(r"(\d+)\s+more", more_text, re.I)
    expected = before + (int(match.group(1)) if match else 1)
    await button.scroll_into_view_if_needed(timeout=10000)
    for attempt in range(3):
        try:
            await button.click(timeout=20000)
            break
        except Exception:
            if attempt == 2:
                raise
            await page.wait_for_timeout(1500)
    try:
        await page.wait_for_function(
            "target => document.querySelectorAll('#productFitsModels .js-model-item').length >= target",
            expected, timeout=20000)
    except PlaywrightTimeoutError:
        await page.wait_for_timeout(2000)
    return True


async def collect_fits_models(page) -> tuple[int, list[str], str | None]:
    await page.goto(page.url.split("#")[0] + "#manualsDiagrams", wait_until="domcontentloaded", timeout=60000)
    await page.locator("#productFitsModels .fm-header__title").wait_for(state="visible", timeout=30000)
    select = page.locator("#productFitsModels select")
    if await select.count():
        options = await select.locator("option").evaluate_all("nodes => nodes.map(n => n.value)")
        if options:
            await select.select_option(options[-1], timeout=15000)
            await page.wait_for_timeout(1500)
    state = await get_fits_state(page)
    total = state["count"]
    if not total:
        return 0, [], None
    complete: list[str] = []
    first_page = True
    for _ in range(12):
        state = await get_fits_state(page)
        # Each result-page block can expose up to 200 rows after "Load more".
        while state["more"] and len(state["models"]) < 200:
            await click_load_more(page)
            await page.wait_for_timeout(700)
            state = await get_fits_state(page)
        complete.extend(state["models"])
        if len(complete) >= total:
            break
        labels = await page.locator("#productFitsModels .pagination-item a[aria-label]").evaluate_all(
            "nodes => nodes.map(n => n.getAttribute('aria-label')).filter(Boolean)")
        candidates = []
        for label in labels:
            match = re.search(r"Go to page (\d+) of (\d+)", label or "")
            if match and (int(match.group(1)) - 1) * 100 >= len(complete) - 1:
                candidates.append((int(match.group(1)), label))
        if not candidates:
            break
        _, label = min(candidates)
        target = page.locator(f'#productFitsModels .pagination-item a[aria-label="{label}"]')
        await target.click(timeout=20000)
        await page.wait_for_timeout(1500)
        first_page = False
    if len(complete) != total:
        raise RuntimeError(f"Fits Models list incomplete: collected {len(complete)} of {total}.")
    link = await page.locator('a[href*="#manualsDiagrams"]').filter(has_text=re.compile("View Models List", re.I)).first.get_attribute("href") if await page.locator('a[href*="#manualsDiagrams"]').filter(has_text=re.compile("View Models List", re.I)).count() else None
    return total, complete, urljoin(page.url, link) if link else None


async def extract_product_details(page, product: dict) -> dict:
    await page.goto(product["url"], wait_until="domcontentloaded", timeout=60000)
    await page.locator("h1").first.wait_for(state="visible", timeout=30000)
    body = (await page.locator("body").inner_text(timeout=15000)).replace("\xa0", " ")
    fits_count, fits_list, fits_url = await collect_fits_models(page)
    specs = await page.locator("#spec").inner_text(timeout=10000) if await page.locator("#spec").count() else ""
    if not specs.strip():
        spec_tab = page.get_by_role("tab", name=re.compile("SPECS", re.I))
        if await spec_tab.count():
            await spec_tab.click(timeout=10000)
            await page.wait_for_timeout(400)
            specs = await page.locator("#spec").inner_text(timeout=10000) if await page.locator("#spec").count() else ""
    specs = " ".join(specs.split())

    labels = re.compile(r"\s+(?:Quantity Available|Manufacturer|Manufacturer #|Parts Town #|Previous Part #|Units|Fits Models|California Residents)\s*:?")
    def label_value(label: str) -> str | None:
        match = re.search(rf"{re.escape(label)}\s*:?\s*(.*?)(?={labels.pattern}|$)", body, re.I)
        return match.group(1).strip() if match and match.group(1).strip() else None

    prev = re.search(r"Previous Part #\s*:?(.*?)(?=\s+Units\s*:|\s+Fits Models\s*:|$)", body, re.I)
    previous = ", ".join(dict.fromkeys(re.findall(r"[A-Z0-9][A-Z0-9-]{2,}", prev.group(1), re.I))) if prev else ""
    manufacturer = label_value("Manufacturer")
    mfr_no = label_value("Manufacturer #") or product.get("manufacturer_part_number")
    quantity = label_value("Quantity Available") or product.get("quantity_available", "")
    units = label_value("Units") or ""
    prop65 = "Proposition 65 Warning" if re.search(r"Proposition 65 Warning", body, re.I) else ""
    list_price = parse_price(body, "List Price") or product.get("list_price_usd")
    my_price = parse_price(body, "My Price") or product.get("my_price_usd")
    return {
        "manufacturer": manufacturer or product.get("brand", "Bakers Pride"),
        "manufacturer_part_number": mfr_no,
        "partstown_number": product["partstown_number"],
        "quantity_available": quantity,
        "previous_part_numbers": previous,
        "units": units,
        "fits_models": ", ".join(fits_list),
        "fits_models_list": fits_list,
        "fits_model_count": fits_count,
        "fits_models_url": fits_url,
        "specs": specs,
        "prop65_warning": prop65,
        "list_price_usd": list_price,
        "my_price_usd": my_price,
        "availability": product.get("availability", ""),
    }


def save_details(product: dict, detail: dict, snapshot: str) -> None:
    with sqlite3.connect(partstown_sync.DB_FILE, timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        old = conn.execute("SELECT raw_json FROM products WHERE id=?", (product["partstown_number"],)).fetchone()
        meta = json.loads(old[0]) if old and old[0] else {}
        meta["listing"] = product
        meta["detail"] = detail
        meta["snapshot_date"] = snapshot
        meta["quantity_available"] = detail.get("quantity_available", "")
        meta["pdp_tabs"] = {
            "captured_date": snapshot,
            "fits_models": f"Part {product['partstown_number']} fits {detail['fits_model_count']} models",
            "fits_model_count": detail["fits_model_count"],
            "fits_models_list": detail["fits_models_list"],
            "specs": detail["specs"],
        }
        conn.execute("""UPDATE products SET manufacturer=?,sku=?,price=?,my_price=?,availability=?,image=?,
          previous_part_numbers=?,units=?,fits_models=?,fits_model_count=?,fits_models_url=?,prop65_warning=?,
          raw_json=?,synced_at=datetime('now') WHERE id=?""",
          (detail.get("manufacturer"), detail.get("manufacturer_part_number"), detail.get("list_price_usd"),
           detail.get("my_price_usd"), (detail.get("availability") or "") + (f" (quantity: {detail['quantity_available']})" if detail.get("quantity_available") else ""),
           product.get("image_url"), detail.get("previous_part_numbers"), detail.get("units"),
           detail.get("fits_models"), detail.get("fits_model_count"), detail.get("fits_models_url"),
           detail.get("prop65_warning"), json.dumps(meta, ensure_ascii=False), product["partstown_number"]))


async def enrich_products(context, products: list[dict], workers: int, delay: float) -> tuple[int, int]:
    queue: asyncio.Queue[dict] = asyncio.Queue()
    for item in products:
        with sqlite3.connect(partstown_sync.DB_FILE) as conn:
            row = conn.execute("SELECT raw_json FROM products WHERE id=?", (item["partstown_number"],)).fetchone()
        meta = json.loads(row[0]) if row and row[0] else {}
        pdp = meta.get("pdp_tabs", {})
        if pdp.get("fits_models_list") and pdp.get("specs"):
            continue
        queue.put_nowait(item)
    todo = queue.qsize()
    pages = [await context.new_page() for _ in range(min(workers, max(1, todo)))] if todo else []
    done = failed = 0
    lock = asyncio.Lock()

    async def worker(page):
        nonlocal done, failed
        while not queue.empty():
            item = await queue.get()
            try:
                detail = None
                last_error = None
                for attempt in range(2):
                    try:
                        detail = await extract_product_details(page, item)
                        break
                    except Exception as exc:
                        last_error = exc
                        if attempt == 0:
                            await page.wait_for_timeout(2500)
                if detail is None:
                    raise RuntimeError(f"Failed after 2 attempts: {last_error}")
                save_details(item, detail, date.today().isoformat())
                async with lock:
                    done += 1
                    if done % 25 == 0 or done == todo:
                        print(f"PDP details [{done}/{todo}]; failed={failed}.", flush=True)
            except Exception as exc:
                async with lock:
                    failed += 1
                    print(f"[DETAIL ERROR] {item['partstown_number']} {item['url']}: {exc}", flush=True)
            finally:
                queue.task_done()
                if delay:
                    await page.wait_for_timeout(int(delay * 1000))

    if pages:
        await asyncio.gather(*(worker(page) for page in pages))
    return done, failed


async def run(args) -> int:
    partstown_sync.init_db()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        cdp_mode = args.cdp
        if cdp_mode:
            start_regular_chrome(args.cdp_port)
            browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{args.cdp_port}")
            if not browser.contexts:
                raise RuntimeError("No Chrome browser context is available over CDP.")
            context = browser.contexts[0]
        else:
            context = await playwright.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=False, channel="chrome", viewport={"width": 1440, "height": 1000}
            )
        login_page = context.pages[0] if context.pages else await context.new_page()
        await login_page.goto(BRAND_URL, wait_until="domcontentloaded", timeout=60000)
        if cdp_mode:
            print("Regular Chrome is open. Sign in and complete any site verification manually; sync waits for My Price.", flush=True)
        else:
            print("Chrome is open. Sign in manually if needed; the sync waits for My Price to appear.", flush=True)
        if not await wait_for_signed_in(login_page, args.login_timeout):
            print("Timed out waiting for the signed-in listing. No credentials were entered or stored.", flush=True)
            if not cdp_mode:
                await context.close()
            return 2
        products = await scrape_listing_pages(context, args.max_pages, args.workers, args.delay)
        if args.max_products:
            products = products[:args.max_products]
        print("Listing sync complete. Enriching each product with Specs and the full Fits Models list.", flush=True)
        detail_products = products[:args.max_products] if args.max_products else products
        done, failed = await enrich_products(context, detail_products, args.workers, args.delay)
        if not cdp_mode:
            await context.close()
    partstown_sync.export_csv()
    from export_partstown_excel import main as export_excel
    export_excel()
    print(f"Finished: {len(products):,} listing products; enriched={done:,}; detail errors={failed:,}.", flush=True)
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Concurrent, resumable Parts Town Bakers Pride full sync.")
    parser.add_argument("--max-pages", type=int, help="Optional page cap; default syncs all pages.")
    parser.add_argument("--max-products", type=int, help="Optional product cap for the detail stage.")
    parser.add_argument("--workers", type=int, default=3, help="Concurrent browser pages (1-4; default 3).")
    parser.add_argument("--login-timeout", type=int, default=900, help="Seconds to wait for manual sign-in (default 900).")
    parser.add_argument("--delay", type=float, default=0.5, help="Pause after each page request in seconds.")
    parser.add_argument("--cdp", action="store_true",
                        help="Use a regular Chrome process for manual sign-in/verification instead of automation-launched Chrome.")
    parser.add_argument("--cdp-port", type=int, default=9222, help="Local Chrome debugging port for --cdp (default 9222).")
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 4 or args.delay < 0 or args.login_timeout < 1:
        parser.error("workers must be 1-4, delay non-negative, and login timeout positive")
    if args.max_pages is not None and args.max_pages < 1:
        parser.error("--max-pages must be positive")
    if args.max_products is not None and args.max_products < 1:
        parser.error("--max-products must be positive")
    if args.cdp_port < 1 or args.cdp_port > 65535:
        parser.error("--cdp-port must be between 1 and 65535")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
