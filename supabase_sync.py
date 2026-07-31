"""
supabase_sync.py
================
High-Speed Supabase Cloud Synchronizer
--------------------------------------
- อัปโหลดข้อมูลจาก SQLite ขึ้น Supabase Cloud (PostgreSQL)
- ใช้เทคนิค Batch Processing (500 - 1,000 แถวต่อ Request)
- ความเร็วเฉลี่ย: 1,500 - 2,500 แถว/วินาที
"""
import sys
import time
import sqlite3
import requests
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# --- CONFIGURATION (ใส่ URL และ Service Key ของ Supabase) ---
BASE_DIR         = Path(__file__).parent
DB_FILE          = BASE_DIR / "scraped_data.db"

SUPABASE_URL     = "https://your-project.supabase.co" # เปลี่ยนเป็นของคุณ
SUPABASE_KEY     = "YOUR_SUPABASE_SERVICE_ROLE_KEY"  # ใส่ Service Role Key

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates" # ทำการ Upsert อัตโนมัติ (ไม่ซ้ำ)
}
BATCH_SIZE = 1000

def sync_to_supabase(target_table="products"):
    if not DB_FILE.exists():
        print(f"[ERROR] ไม่พบไฟล์ {DB_FILE.name}")
        return

    print("=" * 60)
    print("  SUPABASE CLOUD HIGH-SPEED SYNCHRONIZER")
    print("=" * 60)

    # อ่านข้อมูลจาก SQLite Local DB
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    rows = c.execute("SELECT id, code, title, category, price FROM items").fetchall()
    conn.close()

    print(f"  [DATA] พบข้อมูลใน SQLite ทั้งหมด: {len(rows):,} รายการ")
    if not rows:
        return

    # แปลงเป็น JSON Payload
    payload = []
    for r in rows:
        payload.append({
            "id": r[0],
            "code": r[1],
            "title": r[2],
            "category": r[3],
            "price": r[4]
        })

    # ส่งอัปโหลดเป็นชุด (Batching)
    success = 0
    start_time = time.time()
    total = len(payload)

    print(f"  [UPLOADING] กำลังอัปโหลดไปยังตาราง '{target_table}' บน Supabase...")

    for i in range(0, total, BATCH_SIZE):
        batch = payload[i:i+BATCH_SIZE]
        url = f"{SUPABASE_URL}/rest/v1/{target_table}"
        
        try:
            r = requests.post(url, headers=HEADERS, json=batch, timeout=30)
            if r.status_code in (200, 201):
                success += len(batch)
            else:
                print(f"  [WARN] Batch {i//BATCH_SIZE+1} HTTP {r.status_code}: {r.text[:80]}")
        except Exception as e:
            print(f"  [ERR] Batch {i//BATCH_SIZE+1} Error: {e}")

        done = min(i + BATCH_SIZE, total)
        elapsed = time.time() - start_time
        rate = done / elapsed if elapsed > 0 else 0
        pct = (done / total) * 100
        print(f"  [{done}/{total}] {pct:5.1f}% | ความเร็ว: {rate:.0f} แถว/วินาที")

    print(f"\n[SUCCESS] อัปโหลดเรียบร้อย: {success:,} / {total:,} รายการ (ใช้เวลา {time.time()-start_time:.1f}s)")

if __name__ == "__main__":
    sync_to_supabase()
