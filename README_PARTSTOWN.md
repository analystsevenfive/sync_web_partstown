# Parts Town catalog sync

สคริปต์ `partstown_sync.py` อ่าน URL สินค้าภาษาอังกฤษจาก public sitemap ของ Parts Town, กรองตาม slug แบรนด์, ดึงข้อมูลเมตาที่หน้าเว็บเปิดให้เข้าถึง และบันทึกลง SQLite เพื่อให้อัปเดตซ้ำได้ ค่าเริ่มต้นตรงกับหน้า Bakers Pride (`/b/bakers-pride`) ส่วน `My Price` เป็นราคาตามบัญชีที่ล็อกอิน จึงนำเข้าจาก snapshot ของหน้า listing ที่แสดงราคานี้

## ซิงก์เข้า Google Sheets ด้วย GitHub Actions

มี workflow ที่ `.github/workflows/sync-partstown.yml` ตั้งเวลาให้ทำงานทุกวัน 09:15 น. เวลาไทย และกดรันเองได้จากแท็บ Actions → **Trial sync Parts Town page 1 to Google Sheets** → **Run workflow** รอบทดลองนี้จำกัด 24 URL แรกของ Bakers Pride จาก public product sitemap

ตั้งค่าครั้งเดียว:

1. สร้าง Google Sheet แล้วคัดลอก Spreadsheet ID จาก URL รูปแบบ `https://docs.google.com/spreadsheets/d/<ID>/edit`
2. สร้าง Google Cloud service account เปิด Google Sheets API แล้วดาวน์โหลด JSON key
3. แชร์ Sheet ให้ email ของ service account โดยให้สิทธิ์ Editor
4. ใน GitHub repository เพิ่ม Actions secrets `GOOGLE_SHEET_ID` และ `GOOGLE_SERVICE_ACCOUNT_JSON` (ใส่เนื้อหา JSON key ทั้งก้อนใน secret หลัง) ห้าม commit key ลง repository
5. Push โฟลเดอร์โปรเจกต์ขึ้น GitHub แล้วเปิด Actions ให้ทำงาน

workflow จะเขียนข้อมูลใหม่ทั้งแท็บ `Parts Town` ทุกครั้ง (หากไม่มีแท็บจะสร้างให้อัตโนมัติ) และเก็บคอลัมน์ Part #, ราคา, จำนวน, Previous Part #, รุ่นที่รองรับ, ลิงก์สินค้าและรูปภาพ หาก Parts Town ตอบ HTTP 403 ใน runner จะไม่มีข้อมูลใหม่ให้เขียน

**ข้อจำกัด My Price:** workflow นี้รันในเครื่อง Linux ชั่วคราวของ GitHub Actions จึงไม่มี browser session ที่ล็อกอิน Parts Town และไม่สามารถดึง `My Price` ผ่านตัวอ่าน public sitemap ได้ ช่องนี้จึงอาจว่างในข้อมูลที่มาจาก workflow; การดึง My Price ต้องมีช่องทาง authenticated ที่ปลอดภัยสำหรับ runner ก่อน

## เริ่มใช้งาน

ต้องติดตั้ง `requests` และ `beautifulsoup4` ก่อน (โปรเจกต์นี้ใช้ `requests` อยู่แล้ว):

```powershell
python -m pip install requests beautifulsoup4
```

ลอง sync สินค้า Bakers Pride 20 รายการก่อน:

```powershell
python partstown_sync.py --limit 20
```

หากต้องการดึงสินค้าทั้งหมดของ Bakers Pride:

```powershell
python partstown_sync.py
```

หากต้องการแบรนด์อื่น ให้ระบุ slug จาก URL `/b/<slug>` เช่น `--brand vulcan-hart`; ใช้ `--brand all` เพื่อเลือกทุกแบรนด์:

```powershell
python partstown_sync.py --brand all --limit 100
```

ตั้งจำนวนคำขอพร้อมกันได้ 1–4 workers และปรับช่วงห่างระหว่างคำขอด้วย `--workers` กับ `--delay` (หน่วยวินาที):

```powershell
python partstown_sync.py --limit 100 --workers 1 --delay 1
```

ส่งออกข้อมูลที่ sync ได้แล้วเป็น CSV:

```powershell
python partstown_sync.py --export-csv
```

นำ snapshot ที่อ่านจากหน้า brand ผ่าน browser มาใส่ SQLite และสร้าง export:

```powershell
python partstown_sync.py --import-listing-csv partstown_bakers_pride_page1.csv
```

ไฟล์ `partstown_bakers_pride_page1.csv` เป็น snapshot 24 รายการจากหน้าที่เปิดอยู่ ไม่ใช่ข้อมูลครบทั้งแบรนด์ มีราคาปกติและ My Price, จำนวนคงเหลือ, ลิงก์สินค้า และลิงก์รูปภาพด้วย โดย URL สร้างตามรูปแบบรหัส Parts Town/แบรนด์จากหน้า listing

ข้อมูลอยู่ใน `partstown_data.db` และไฟล์ CSV อยู่ใน `exports/partstown_products.csv` ทั้งสองตำแหน่งถูก exclude จาก Git ตาม `.gitignore`

## ข้อจำกัดการเข้าถึง

สคริปต์ใช้ product sitemap สาธารณะ และไม่เรียกหน้า account, cart หรือ search ที่ robots.txt ระบุว่าไม่ให้ crawl หากหน้า product ตอบ HTTP 403 หรือไม่มีข้อมูล metadata ที่รู้จัก สคริปต์จะแจ้ง URL นั้นและข้ามไป โดยจะไม่บันทึก error page เป็นสินค้า
## Automatic signed-in browser sync

`partstown_browser_sync.py` reads the rendered Bakers Pride listing and product detail pages. It saves list price, account-specific My Price, quantity, manufacturer and part numbers, previous part numbers separated by commas, units, fits-model information, Proposition 65 warning, and product/image links to SQLite and CSV.

Install Playwright once:

```powershell
python -m pip install playwright
python -m playwright install chrome
```

Run:

```powershell
python partstown_browser_sync.py --max-pages 1
```

A dedicated Chrome window opens. Sign in manually and complete any site challenge there. The script continues when My Price is visible. Its separate `.partstown-profile` folder retains the session for later runs; it does not read the normal Chrome profile or store your password. Increase `--max-pages` for more listing pages, or add `--max-products 10` to cap product detail visits.

If the Playwright-launched Chrome cannot complete the site's verification, use `--cdp` to start a regular headed Chrome process and attach to it over localhost CDP. Sign in and complete any verification manually in that window; the script waits until My Price appears and does not solve or bypass challenges. The separate `.partstown-cdp-profile` keeps that browser's session between runs.

```powershell
python partstown_browser_sync.py --cdp --max-pages 1 --max-products 24
```

For the full resumable sync, which also collects Specs and every Fits Models value, use `partstown_full_sync.py`:

```powershell
python partstown_full_sync.py --cdp --max-pages 1 --max-products 24 --workers 1
```

This first run limits work to one listing page and its 24 products. After reviewing the generated Excel file, run without the caps to process the whole brand catalog:

```powershell
python partstown_full_sync.py --cdp --workers 2
```

Output files are `partstown_data.db` and `exports/partstown_products.csv`. When the complete fits-model list is not shown inline, the script records the model count and “View Models List” link.


## Export all saved brands to one Excel workbook

Run this from the `universal-web-scraper` directory after syncing data:

```powershell
python export_partstown_all_brands.py
```

The script reads the saved SQLite databases and creates `exports/partstown_all_brands.xlsx`. Each brand has its own worksheet: Bakers Pride, Middleby, CTX, Pitco, and Crown Steam. The workbook includes product links, image links, List Price, My Price, quantity, previous part numbers, Fits Models, Specs, and other saved product fields. Missing databases are skipped.

To choose a different output path:

```powershell
python export_partstown_all_brands.py --output "exports/partstown_all_brands.xlsx"
```

The `exports/` directory is ignored by Git, so generated workbooks remain local. In particular, My Price can be account-specific; keep the workbook out of a shared repository unless it is appropriate to publish those prices.

## Backfill prices from product pages

After signing in and completing any site verification in the regular Chrome session, run:

```powershell
python partstown_price_backfill.py --workers 4 --delay 0.25
```

This visits saved product pages for the supported brands and checkpoints results in their SQLite databases. It does not retry rows previously checked without a price unless `--retry-attempted` is added. Export the combined workbook again after the backfill to refresh the Excel file.
