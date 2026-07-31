"""
parallel_scraper_template.py
============================
High-Performance Parallel Scraper Engine
----------------------------------------
- รองรับการดึงข้อมูลแบบขนาน (Multi-threading / Concurrency)
- ระบบตัดข้อมูลซ้ำอัตโนมัติ (Deduplication)
- ระบบดึงต่อจากจุดเดิมเมื่อหลุด (Resume Capability)
- บันทึกข้อมูลลง SQLite Cache ทันที เพื่อป้องกันข้อมูลหาย
"""
import sys
import json
import time
import sqlite3
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# --- CONFIGURATION (ปรับแต่งตามโปรเจกต์) ---
BASE_DIR     = Path(__file__).parent
HEADERS_FILE = BASE_DIR / "headers.json"
DB_FILE      = BASE_DIR / "scraped_data.db"
MAX_WORKERS  = 10  # จำนวน Thread ทำงานขนานกัน (ปรับตามความเหมาะสม 5-15)

def get_authenticated_session():
    """โหลด Header & Cookies จาก headers.json"""
    if not HEADERS_FILE.exists():
        print(f"[ERROR] ไม่พบไฟล์ {HEADERS_FILE.name} กรุณารัน auth_interceptor.py ก่อน!")
        sys.exit(1)

    with open(HEADERS_FILE, encoding="utf-8") as f:
        data = json.load(f)

    session = requests.Session()
    session.headers.update(data.get("headers", {}))
    
    cookies = data.get("cookies", [])
    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    if cookie_str:
        session.headers["cookie"] = cookie_str
        
    return session

def init_database():
    """สร้างตาราง SQLite สำหรับเก็บข้อมูล"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            code TEXT,
            title TEXT,
            category TEXT,
            price REAL,
            data_json TEXT,
            scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def load_scraped_ids():
    """ดึง ID ทั้งหมดที่เคยดึงแล้ว เพื่อข้ามการดึงซ้ำ"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    rows = c.execute("SELECT id FROM items").fetchall()
    conn.close()
    return set(r[0] for r in rows)

def fetch_single_item(item_id, session):
    """
    ฟังก์ชันสำหรับดึงข้อมูลสินค้า 1 ชิ้นจาก API/เว็บเป้าหมาย
    (ปรับแต่ง URL และ Field Mapping ตามเว็บเป้าหมาย)
    """
    # ตัวอย่าง URL (เปลี่ยนให้ตรงกับเว็บที่ต้องการ Scrape)
    url = f"https://api-example.com/items/{item_id}"
    try:
        r = session.get(url, timeout=12)
        if r.status_code == 200:
            data = r.json()
            return {
                "id": str(item_id),
                "code": data.get("code", f"CODE-{item_id}"),
                "title": data.get("name") or data.get("title", ""),
                "category": data.get("category", "General"),
                "price": float(data.get("price", 0.0)),
                "raw_json": json.dumps(data, ensure_ascii=False)
            }, None
        else:
            return None, f"HTTP {r.status_code}"
    except Exception as e:
        return None, str(e)

def save_batch_to_db(items_batch):
    """บันทึกข้อมูลเป็นชุดลง SQLite"""
    if not items_batch:
        return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    for item in items_batch:
        c.execute("""
            INSERT OR REPLACE INTO items (id, code, title, category, price, data_json)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (item["id"], item["code"], item["title"], item["category"], item["price"], item["raw_json"]))
    conn.commit()
    conn.close()

def run_parallel_scraper(target_ids):
    """รันกระบวนการ Scrape แบบขนานด้วย ThreadPoolExecutor"""
    init_database()
    session = get_authenticated_session()
    already_scraped = load_scraped_ids()

    # กรองเอาเฉพาะ ID ที่ยังไม่เคยดึง (Deduplication / Resume)
    pending_ids = [i for i in target_ids if str(i) not in already_scraped]

    print("=" * 60)
    print("  HIGH-PERFORMANCE PARALLEL SCRAPER")
    print("=" * 60)
    print(f"  [STATUS] ทั้งหมด: {len(target_ids):,} รายการ | ดึงแล้ว: {len(already_scraped):,} | ต้องดึงเพิ่ม: {len(pending_ids):,}")

    if not pending_ids:
        print("  [COMPLETE] ข้อมูลทั้งหมดถูกดึงครบถ้วนแล้ว!")
        return

    start_time = time.time()
    completed = 0
    batch_buffer = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_single_item, item_id, session): item_id for item_id in pending_ids}

        for future in as_completed(futures):
            completed += 1
            item, err = future.result()
            
            if item:
                batch_buffer.append(item)

            # แสดง Progress ทุกๆ 50 รายการ
            if completed % 50 == 0 or completed == len(pending_ids):
                elapsed = time.time() - start_time
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (len(pending_ids) - completed) / rate if rate > 0 else 0
                pct = (completed / len(pending_ids)) * 100
                print(f"  [{completed}/{len(pending_ids)}] {pct:5.1f}% | ความเร็ว: {rate:.1f} ชิ้น/วินาที | เหลือเวลาประมาณ: {eta:.0f} วินาที")

                # Save buffer to DB
                save_batch_to_db(batch_buffer)
                batch_buffer = []

    # Final Save
    save_batch_to_db(batch_buffer)
    print(f"\n[FINISHED] ดึงข้อมูลเสร็จสมบูรณ์! ใช้เวลาทั้งหมด: {time.time()-start_time:.1f} วินาที")

if __name__ == "__main__":
    # ตัวอย่างการใช้งาน: ใส่ List ของ ID ที่ต้องการดึง
    sample_ids = range(1001, 1050)
    run_parallel_scraper(sample_ids)
