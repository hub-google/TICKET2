import sqlite3
import pandas as pd
import os

DB_FILE = "flights.db"
EXCEL_FILE = "flights_database.xlsx"

def export_db_to_excel():
    if not os.path.exists(DB_FILE):
        print(f"找不到資料庫檔案 {DB_FILE}！")
        return
        
    print(f"正在讀取 {DB_FILE}...")
    
    # 連線至 SQLite 資料庫
    conn = sqlite3.connect(DB_FILE)
    
    # 讀取整個 flights 資料表 (原汁原味，不改任何欄位名稱與內容)
    df = pd.read_sql_query("SELECT * FROM flights", conn)
    
    # 匯出至 Excel
    df.to_excel(EXCEL_FILE, index=False)
    
    conn.close()
    
    print(f"成功將 {len(df)} 筆資料匯出至 {EXCEL_FILE}！")

if __name__ == "__main__":
    export_db_to_excel()
