"""Sync Parts Town rendered listing and PDP data using a persistent browser.

The browser profile is dedicated to this script. Sign in manually on the first
run; the profile then keeps the site's session between runs. This script never
reads or stores account credentials and does not attempt to solve challenges.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import subprocess
import time
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import urlopen

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

import partstown_sync

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / ".partstown-profile"
CDP_PROFILE_DIR = BASE_DIR / ".partstown-cdp-profile"
BRAND_URL = "https://www.partstown.com/b/bakers-pride"
IMAGE_CODES = {"bakers-pride": "BKP", "star": "STA", "magikitchn": "MK", "apw-wyott": "APW"}


def text_of(locator) -> str:
    try:
        return " ".join(locator.inner_text(timeout=1500).split())
    except Exception:
        return ""


def parse_money(text: str, label: str) -> str | None:
    match = re.search(rf"{re.escape(label)}\s*\$?\s*([\d,]+\.\d{{2}})", text, re.I)
    return match.group(1).replace(",", "") if match else None


def product_cards(page) -> list[dict]:
    """Read visible product cards from the rendered brand listing."""
    return page.locator("a[href*='/p/']").evaluate_all("""anchors => {
      const out = [], seen = new Set();
      for (const a of anchors) {
        const href = new URL(a.getAttribute('href'), location.href).href;
        if (!/\/p\/[^/]+\/[^/]+/.test(new URL(href).pathname) || seen.has(href)) continue;
        let card = a;
        for (let i=0; i<8 && card.parentElement; i++) {
          const t = (card.innerText || card.parentElement.innerText || '');
          if (/Parts Town\s*#|Mfr Part\s*#|List Price|My Price/i.test(t)) break;
          card = card.parentElement;
        }
        const text = (card.innerText || '').replace(/\\s+/g, ' ').trim();
        const img = [...card.querySelectorAll('img')].map(x => x.currentSrc || x.src).find(Boolean) || '';
        const title = [...card.querySelectorAll('h1,h2,h3,h4')].map(x=>x.innerText.trim()).find(Boolean) ||
          (a.innerText || a.getAttribute('aria-label') || '').trim() || '';
        const get = re => { const m=text.match(re); return m ? m[1].trim() : ''; };
        const pt = get(/Parts Town\\s*#\\s*([A-Z0-9-]+)/i);
        const sku = get(/Mfr Part\\s*#\\s*([A-Z0-9-]+)/i);
        const qty = get(/(?:Quantity Available|Quantity)\\s*:?\\s*([\\d,]+)/i);
        const list = get(/List Price\\s*:?\\s*\\$?([\\d,.]+)/i);
        const my = get(/My Price\\s*:?\\s*\\$?([\\d,.]+)/i);
        if (!pt && !sku && !title) continue;
        seen.add(href);
        out.push({href, title, image:img, card_text:text, partstown_number:pt,
          manufacturer_part_number:sku, quantity_available:qty.replace(/,/g,''),
          list_price_usd:list.replace(/,/g,''), my_price_usd:my.replace(/,/g,'')});
      }
      return out;
    }""")


def restore_listing(page, listing_url: str) -> None:
    """Return from a PDP to its listing and restore the scroll position."""
    page.goto(listing_url, wait_until="domcontentloaded", timeout=60000)


def fallback_image_url(product: dict) -> str | None:
    if product.get("image_url"):
        return product["image_url"]
    brand_slug = product["url"].split("/p/")[-1].split("/")[0]
    code = IMAGE_CODES.get(brand_slug)
    part_no = product.get("partstown_number")
    if code and part_no:
        return f"https://partstown.sirv.com/products/{code}/{part_no}.view?thumb&image.rules=G0&w=200"
    return None


def extract_pdp(page, listing_item: dict) -> dict:
    page.goto(listing_item["url"], wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(800)
    body = text_of(page.locator("body"))
    # Product facts appear as label/value rows in the PDP details panel.
    def label_value(label: str) -> str | None:
        match = re.search(rf"{re.escape(label)}\s*:?\s*(.*?)(?=\s+(?:Quantity Available|Manufacturer|Manufacturer #|Parts Town #|Previous Part #|Units|Fits Models|California Residents)\s*:|$)", body, re.I)
        return match.group(1).strip() if match and match.group(1).strip() else None

    manufacturer = label_value("Manufacturer")
    mfr_no = label_value("Manufacturer #") or listing_item.get("manufacturer_part_number")
    pt_no = label_value("Parts Town #") or listing_item.get("partstown_number")
    units = label_value("Units")
    previous = None
    prev_match = re.search(r"Previous Part #\s*:?\s*(.*?)(?=\s+Units\s*:|\s+Fits Models\s*:|$)", body, re.I)
    if prev_match:
        previous = ", ".join(dict.fromkeys(re.findall(r"[A-Z0-9][A-Z0-9-]{2,}", prev_match.group(1), re.I))) or None

    fits_link = page.locator("a").filter(has_text=re.compile(r"View Models List", re.I)).first
    fits_url = None
    try:
        href = fits_link.get_attribute("href", timeout=1200)
        if href:
            fits_url = urljoin(page.url, href)
    except Exception:
        pass
    fits_count = None
    count_match = re.search(r"(?:fits\s+)?(\d[\d,]*)\s+models?|([\d,]+)\s+results? found", body, re.I)
    if count_match:
        fits_count = int((count_match.group(1) or count_match.group(2)).replace(",", ""))
    # Capture displayed popular model identifiers; an explicit models-list link
    # and count are retained when the site paginates the complete compatibility list.
    popular_match = re.search(r"Fits Popular Models\s*:?\s*(.*?)(?=\s+(?:Quantity Available|Manufacturer|Manufacturer #|Parts Town #|Previous Part #|Units|California Residents)\s*:|$)", body, re.I)
    fits_models = None
    if popular_match:
        fits_models = ", ".join(dict.fromkeys(re.findall(r"[A-Z0-9][A-Z0-9-]{1,}", popular_match.group(1), re.I))) or None
    elif fits_link.count() and fits_count:
        fits_models = None  # Complete list is linked; do not mistake its modal for a partial list.

    detail = {
        "manufacturer": manufacturer or "Bakers Pride",
        "manufacturer_part_number": mfr_no,
        "partstown_number": pt_no,
        "quantity_available": listing_item.get("quantity_available") or label_value("Quantity Available"),
        "previous_part_numbers": previous,
        "units": units,
        "fits_models": fits_models,
        "fits_model_count": fits_count,
        "fits_models_url": fits_url,
        "prop65_warning": "Proposition 65 Warning" if re.search(r"Proposition 65 Warning", body, re.I) else None,
        "list_price_usd": listing_item.get("list_price_usd") or parse_money(body, "List Price"),
        "my_price_usd": listing_item.get("my_price_usd") or parse_money(body, "My Price"),
        "availability": "In Stock, Ships today" if re.search(r"In Stock, Ships today", body, re.I) else None,
    }
    return detail


def upsert(product: dict, detail: dict) -> None:
    item = {
        "id": product["partstown_number"],
        "url": product["url"],
        "title": product["title"],
        "brand": "Bakers Pride",
        "manufacturer": detail.get("manufacturer"),
        "sku": detail.get("manufacturer_part_number"),
        "description": None,
        "price": detail.get("list_price_usd"),
        "my_price": detail.get("my_price_usd"),
        "currency": "USD",
        "availability": (detail.get("availability") or "") + (f" (quantity: {detail['quantity_available']})" if detail.get("quantity_available") else ""),
        "image": product.get("image_url"),
        "previous_part_numbers": detail.get("previous_part_numbers"),
        "units": detail.get("units"),
        "fits_models": detail.get("fits_models"),
        "fits_model_count": detail.get("fits_model_count"),
        "fits_models_url": detail.get("fits_models_url"),
        "prop65_warning": detail.get("prop65_warning"),
        "raw_json": json.dumps({"listing": product, "detail": detail}, ensure_ascii=False),
    }
    with sqlite3.connect(partstown_sync.DB_FILE) as conn:
        conn.execute("""INSERT INTO products
          (id,url,title,brand,manufacturer,sku,description,price,my_price,currency,availability,image,
           previous_part_numbers,units,fits_models,fits_model_count,fits_models_url,prop65_warning,raw_json,synced_at)
          VALUES (:id,:url,:title,:brand,:manufacturer,:sku,:description,:price,:my_price,:currency,:availability,:image,
           :previous_part_numbers,:units,:fits_models,:fits_model_count,:fits_models_url,:prop65_warning,:raw_json,datetime('now'))
          ON CONFLICT(id) DO UPDATE SET url=excluded.url,title=excluded.title,brand=excluded.brand,
           manufacturer=excluded.manufacturer,sku=excluded.sku,price=excluded.price,my_price=excluded.my_price,
           currency=excluded.currency,availability=excluded.availability,image=excluded.image,
           previous_part_numbers=excluded.previous_part_numbers,units=excluded.units,fits_models=excluded.fits_models,
           fits_model_count=excluded.fits_model_count,fits_models_url=excluded.fits_models_url,
           prop65_warning=excluded.prop65_warning,raw_json=excluded.raw_json,synced_at=datetime('now')""", item)


def wait_for_sign_in(page, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        body = text_of(page.locator("body"))
        # Account-specific prices indicate an authenticated session.
        if re.search(r"My Price\s*\$", body, re.I):
            return True
        page.wait_for_timeout(1500)
    return False


def start_regular_chrome(port: int) -> None:
    """Start a normal, headed Chrome process, then attach Playwright over CDP."""
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


def run(max_pages: int, max_products: int | None, login_timeout: int, delay: float,
        cdp_mode: bool = False, cdp_port: int = 9222) -> int:
    partstown_sync.init_db()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    products: dict[str, dict] = {}
    with sync_playwright() as playwright:
        if cdp_mode:
            start_regular_chrome(cdp_port)
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
            context = browser.contexts[0]
            page = next((p for p in context.pages if "/b/bakers-pride" in p.url), None)
            page = page or (context.pages[0] if context.pages else context.new_page())
            if "/b/bakers-pride" not in page.url:
                page.goto(BRAND_URL, wait_until="domcontentloaded", timeout=60000)
            print("Regular Chrome opened. Sign in and complete any site verification manually; this script only continues after My Price appears.")
        else:
            context = playwright.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=False, channel="chrome", viewport={"width": 1440, "height": 1000}
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(BRAND_URL, wait_until="domcontentloaded", timeout=60000)
            print("Browser opened. Sign in manually if needed; this script will continue once account prices appear.")
        if not wait_for_sign_in(page, login_timeout):
            print("Timed out waiting for the signed-in My Price listing. No credentials were entered or stored.")
            if not cdp_mode:
                context.close()
            return 2

        for page_no in range(1, max_pages + 1):
            page.wait_for_timeout(1000)
            cards = product_cards(page)
            if not cards:
                print(f"Page {page_no}: no product cards found; stopping.")
                break
            listing_url = page.url
            for card in cards:
                if max_products and len(products) >= max_products:
                    break
                card["url"] = card.pop("href")
                card["image_url"] = card.pop("image") or None
                card["partstown_number"] = card.get("partstown_number") or card["url"].rstrip("/").split("/")[-1].upper()
                card["title"] = card.get("title") or card["partstown_number"]
                card["manufacturer_part_number"] = card.get("manufacturer_part_number") or None
                card["image_url"] = fallback_image_url(card)
                if card["partstown_number"] in products:
                    continue
                listing_scroll = page.evaluate("window.scrollY")
                try:
                    details = extract_pdp(page, card)
                    upsert(card, details)
                    products[card["partstown_number"]] = {"product": card, "detail": details}
                    print(f"[{len(products)}] {card['partstown_number']}: My Price ${details.get('my_price_usd') or '—'}")
                except Exception as exc:
                    print(f"[ERROR] {card['url']}: {exc}")
                if delay:
                    page.wait_for_timeout(int(delay * 1000))
                # Return to the same listing page and restore its scroll position
                # so lazy-loaded cards and pagination state remain available.
                restore_listing(page, listing_url)
                page.evaluate("y => window.scrollTo(0, y)", listing_scroll)
            if max_products and len(products) >= max_products:
                break
            if page_no == max_pages:
                break
            next_link = page.locator("a[rel='next'], a[aria-label*='next' i], a[title*='next' i]").last
            try:
                href = next_link.get_attribute("href", timeout=1500)
                if not href or "disabled" in (next_link.get_attribute("class") or "").lower():
                    print("No next listing page found; stopping.")
                    break
                page.goto(urljoin(page.url, href), wait_until="domcontentloaded", timeout=60000)
            except PlaywrightTimeoutError:
                print("No next listing page found; stopping.")
                break
        if not cdp_mode:
            context.close()
    partstown_sync.export_csv()
    print(f"Finished: {len(products)} product(s) enriched.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync signed-in Parts Town Bakers Pride listing and product details.")
    parser.add_argument("--max-pages", type=int, default=1, help="Maximum listing pages to process (default: 1)")
    parser.add_argument("--max-products", type=int, help="Optional cap on product details to visit")
    parser.add_argument("--login-timeout", type=int, default=900, help="Seconds to wait for manual sign-in (default: 900)")
    parser.add_argument("--delay", type=float, default=1.0, help="Pause between product pages in seconds")
    parser.add_argument("--cdp", action="store_true",
                        help="Use a regular Chrome process (instead of Playwright's automation-launched Chrome); sign in/verify manually")
    parser.add_argument("--cdp-port", type=int, default=9222, help="Local Chrome debugging port for --cdp (default: 9222)")
    args = parser.parse_args()
    if args.max_pages < 1 or args.login_timeout < 1 or args.delay < 0 or (args.max_products is not None and args.max_products < 1):
        parser.error("page count and timeout must be positive; delay must be non-negative")
    if args.cdp_port < 1 or args.cdp_port > 65535:
        parser.error("--cdp-port must be between 1 and 65535")
    return run(args.max_pages, args.max_products, args.login_timeout, args.delay, args.cdp, args.cdp_port)


if __name__ == "__main__":
    sys.exit(main())
