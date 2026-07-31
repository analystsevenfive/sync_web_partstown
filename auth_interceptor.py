"""
auth_interceptor.py
===================
Universal Authentication & Header Capturing Engine
--------------------------------------------------
เปิด Browser อัตโนมัติด้วย Playwright เพื่อดักจับ:
- Bearer Tokens / Authorization Headers
- Custom Headers (x-api-key, x-customer-code ฯลฯ)
- Authentication Cookies

วิธีใช้:
1. กำหนด TARGET_URL และ LOGIN_URL
2. รัน `python auth_interceptor.py`
3. ระบบจะเปิด Browser และบันทึก `headers.json` ให้อัตโนมัติ
"""
import asyncio
import json
import sys
from pathlib import Path
from playwright.async_api import async_playwright

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# --- CONFIGURATION (ปรับแต่งตามเว็บเป้าหมาย) ---
TARGET_DOMAIN = "sirman.com"              # โดเมนของ API ที่ต้องการจับ Token
LOGIN_URL     = "https://service.sirman.com/login" # หน้าล็อกอิน
CATALOG_URL   = "https://service.sirman.com/catalog" # หน้าหลังล็อกอิน
OUTPUT_FILE   = Path(__file__).parent / "headers.json"

async def capture_auth():
    print("=" * 60)
    print("  UNIVERSAL AUTHENTICATION & HEADER CAPTURER")
    print("=" * 60)

    captured_headers = {}
    captured_cookies = []

    async with async_playwright() as p:
        # เปิด Chromium Browser
        browser = await p.chromium.launch(headless=False, slow_mo=100)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()

        # Event Listener ดักจับ Request HTTP Headers
        async def on_request(req):
            if TARGET_DOMAIN in req.url:
                hdrs = dict(req.headers)
                # เช็คการมีอยู่ของ Auth Header
                has_auth = "authorization" in hdrs or "x-api-key" in hdrs
                if has_auth:
                    captured_headers.clear()
                    captured_headers.update({k: v for k, v in hdrs.items() if not k.startswith(":")})
                    print(f"  [FOUND TOKEN] Captured from: {req.url[:70]}...")

        page.on("request", on_request)

        print(f"[1] Navigating to: {LOGIN_URL}")
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=15000)
        except Exception as e:
            print(f"    Notice: {e}")

        print("\n[2] กรุณาทำการ Log-in บนหน้าต่าง Browser ที่เปิดขึ้นมา...")
        print("    (ระบบกำลังดักจับ Token ให้อัตโนมัติเมื่อเว็บโหลดข้อมูลเรียบร้อย)\n")

        # รอดักจับสูงสุด 60 วินาที
        for i in range(30):
            if captured_headers:
                print("  [SUCCESS] ดักจับ Auth Headers สำเร็จ!")
                break
            await asyncio.sleep(2)
            if i % 5 == 0:
                print(f"  ...กำลังรอการเข้าสู่ระบบ ({i*2}s)...")

        captured_cookies = await ctx.cookies()
        await browser.close()

    if captured_headers:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "headers": captured_headers,
                "cookies": captured_cookies
            }, f, indent=2, ensure_ascii=False)
        print(f"\n[SAVED] บันทึก Session ลงในไฟล์: {OUTPUT_FILE.name}")
        return True
    else:
        print("\n[ERROR] ไม่สามารถดักจับ Auth Header ได้ กรุณาลองรันใหม่อีกครั้ง")
        return False

if __name__ == "__main__":
    asyncio.run(capture_auth())
