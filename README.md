# 🚀 Web Scraping Universal Template & User Manual
ชุดสคริปต์สำเร็จรูปสำหรับพัฒนา Web Scraper ดึงข้อมูลขนาดใหญ่ รองรับ Multi-threading, ระบบข้ามการดึงซ้ำ, ส่งออก CSV ภาษาไทย และเชื่อมต่อ Supabase Cloud

---

## 🏎️ `super_scraper.py` (ALL-IN-ONE MAX-SPEED CLI ENGINE)

เครื่องมือสำเร็จรูปคำสั่งเดียวสำหรับใช้งานเอง (เน้นความเร็วสูงสุด):

```bash
# 1. ดักจับ API + Token อัตโนมัติใน 5 วินาที
python super_scraper.py --sniff "https://target-website.com/catalog"

# 2. เริ่มดึงข้อมูลขนาน 20 Workers ทันที (Auto-Flattener JSON)
python super_scraper.py --scrape --api "https://api.target-website.com/items?page={page}" --workers 20

# 3. ส่งออกเป็นไฟล์ CSV ภาษาไทย (UTF-8 BOM)
python super_scraper.py --export-csv

# 4. อัปโหลดขึ้น Supabase Cloud (Batch 1,000 rows / 2,500+ rows/sec)
python super_scraper.py --sync-supabase --supabase-url "https://xyz.supabase.co" --supabase-key "YOUR_KEY"
```

---

## 📁 รายชื่อไฟล์ในชุดเครื่องมือ

| ชื่อไฟล์ | หน้าที่การทำงาน |
|---|---|
| 🏎️ `super_scraper.py` | เครื่องมือ All-in-One รันผ่านคำสั่ง CLI ความเร็วสูงสุด 20-50 Workers + Auto Flattener |
| 🔑 `auth_interceptor.py` | เปิด Browser เพื่อล็อกอินและดักจับ `headers.json` (Authorization Bearer Token & Cookies) อัตโนมัติ |
| ⚡ `parallel_scraper_template.py` | สคริปต์ดึงข้อมูลแบบขนาน (Multi-threaded) พร้อมระบบตัดข้อมูลซ้ำ และดึงต่อจากเดิมถ้าหลุด |
| ☁️ `supabase_sync.py` | สคริปต์อัปโหลดข้อมูลเข้า Supabase Cloud PostgreSQL ความเร็วสูง (Batch 1,000 rows) |
| 📄 `export_csv.py` | ส่งออกข้อมูลเป็นไฟล์ `.csv` ภาษาไทย/อังกฤษ (เปิดใน Excel ได้ทันที ภาษาไทยไม่ต่างดาว) |

---

## 🔍 ขั้นตอนก่อนเริ่ม: การวิเคราะห์เว็บไซต์เป้าหมาย (Website Inspection Checklist)

ก่อนเริ่มเขียนหรือรันสคริปต์กับเว็บใหม่ ให้เปิดเบราว์เซอร์แล้วกด **`F12` (Developer Tools)** เพื่อวิเคราะห์ 5 จุดสำคัญ:

```
1. ประเภทการส่งข้อมูล (Fetch/XHR)
   ├── เป็น JSON / REST API  ---> ยิง API ตรงๆ ได้เลย (เร็วที่สุด)
   └── เป็น HTML Webpage     ---> ต้องใช้ BeautifulSoup แกะ Element <div> <table>

2. ระบบล็อกอินและความปลอดภัย (Security & Auth)
   ├── ต้องล็อกอิน          ---> ใช้ auth_interceptor.py หรือ --sniff ดักจับ Bearer Token / Cookies
   └── มี Cloudflare/CAPTCHA ---> ต้องใช้ Playwright รันเปิดหน้าเว็บจริงเพื่อผ่านด่าน

3. โครงสร้าง URL และการเปลี่ยนหน้า (URL Pattern & Pagination)
   ├── แบบ ID สัมพันธ์        ---> example.com/product/1001, 1002
   └── แบบ Page             ---> example.com/items?page=1, page=2

4. อัตราการยิงข้อมูล (Rate Limit)
   ├── ถ้ายิงเร็วเกินไปอาจติด  ---> HTTP 429 (Too Many Requests) หรือ HTTP 403 (Forbidden)
   └── วิธีแก้                ---> ปรับจำนวน WORKERS (--workers 10) หรือใส่ delay

5. นำผลวิเคราะห์ไประบุใน Template
   └── ใส่ URL, Headers และ Field Names ในฟังก์ชัน fetch_single_item() หรือคำสั่ง --api
```

---

## 🛠️ ขั้นตอนการใช้งาน (Step-by-Step Guide)

### ขั้นตอนที่ 1: ติดตั้ง Libraries ที่จำเป็น

เปิด Terminal (PowerShell หรือ Command Prompt) แล้วรันคำสั่ง:

```bash
pip install requests playwright urllib3
playwright install chromium
```

---

### ขั้นตอนที่ 2: ดักจับ Auth Token ด้วย `auth_interceptor.py` หรือ `super_scraper.py --sniff`

หากเว็บเป้าหมายต้องล็อกอินเพื่อดูข้อมูล:

1. รันสคริปต์:
   ```bash
   python super_scraper.py --sniff "https://target-website.com"
   ```
2. หน้าต่าง Chromium Browser จะเปิดขึ้นมา ให้กดล็อกอินตามปกติ
3. สคริปต์จะดักจับ Token และบันทึกเป็นไฟล์ `headers.json` อัตโนมัติ

---

### ขั้นตอนที่ 3: ดึงข้อมูลขนานด้วย `super_scraper.py --scrape`

```bash
python super_scraper.py --scrape --api "https://api-example.com/items/{id}" --workers 30
```
- ข้อมูลจะถูกดึงแบบขนาน 30 Workers พร้อมกัน
- มีระบบ Auto JSON Flattener แปลง JSON ให้เป็นโครงสร้างตารางให้อัตโนมัติ
- บันทึกลง SQLite `super_data.db` ทันทีแบบเรียลไทม์ (หลุดเมื่อไหร่ รันใหม่จะดึงต่อจากเดิมได้ทันที)

---

### ขั้นตอนที่ 4: ส่งออกไฟล์ CSV ด้วย `export_csv.py` หรือ `super_scraper.py --export-csv`

```bash
python super_scraper.py --export-csv
```
จะได้ไฟล์ `exports/super_scraped_data.csv` ที่พร้อมดับเบิ้ลคลิกเปิดใน Microsoft Excel ได้ทันที

---

### ขั้นตอนที่ 5 (Optional): อัปโหลดเข้า Supabase Cloud ด้วย `super_scraper.py --sync-supabase`

```bash
python super_scraper.py --sync-supabase --supabase-url "https://xyz.supabase.co" --supabase-key "YOUR_SERVICE_KEY"
```
สคริปต์จะยิงอัปโหลดที่ความเร็ว 1,500 - 2,500 รายการ/วินาที

---

## ⚡ เทคนิคการปรับประสิทธิภาพ (Pro Tips)
- **ปรับความเร็ว**: สามารถเพิ่ม `--workers 30` หรือ `--workers 50` ใน `super_scraper.py` ได้
- **กันการโดนบล็อค**: หากเว็บเป้าหมายมีการจำกัด Request ให้ลดเหลือ `--workers 5`
