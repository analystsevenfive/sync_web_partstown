"""
export_csv.py
=============
Universal UTF-8-BOM CSV Exporter
--------------------------------
ส่งออกข้อมูลจาก SQLite เป็นไฟล์ CSV ที่รองรับภาษาไทยใน Microsoft Excel 100%
"""
import sys
import csv
import sqlite3
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR   = Path(__file__).parent
DB_FILE    = BASE_DIR / "scraped_data.db"
OUTPUT_CSV = BASE_DIR / "output_data.csv"

def export_to_csv():
    if not DB_FILE.exists():
        print(f"[ERROR] ไม่พบไฟล์ {DB_FILE.name}")
        return

    print("=" * 60)
    print("  UNIVERSAL UTF-8-BOM CSV EXPORTER")
    print("=" * 60)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # อ่านข้อมูล
    rows = c.execute("SELECT id, code, title, category, price, scraped_at FROM items ORDER BY category, id").fetchall()
    conn.close()

    if not rows:
        print("  [WARN] ไม่มีข้อมูลในตาราง")
        return

    headers = ["Item ID", "Code", "Title / Name", "Category", "Price", "Scraped At"]

    # บันทึกไฟล์ CSV รหัส UTF-8-SIG (รองรับภาษาไทยใน Excel)
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

    print(f"  [SUCCESS] ส่งออกข้อมูล {len(rows):,} รายการเรียบร้อย!")
    print(f"  📁 ไฟล์: {OUTPUT_CSV.name}")

if __name__ == "__main__":
    export_to_csv()
