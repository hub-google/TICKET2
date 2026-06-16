import os
import glob
import json
import sqlite3
import logging
import threading

# 載入原本的資料庫模組與匯出模組
from 爬蟲 import init_db, smart_upsert
import export_excel

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

def merge_all_results():
    init_db()
    
    json_files = glob.glob("artifacts/*_results.json")
    if not json_files:
        json_files = glob.glob("*_results.json")
        
    if not json_files:
        logging.warning("找不到任何 JSON 暫存檔，無法執行合併！")
        return

    logging.info(f"找到 {len(json_files)} 個城市的 JSON 結果，開始匯整入資料庫...")
    
    total_records = 0
    for file_path in json_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            for record in data:
                origin = record['origin']
                dest = record['dest']
                dep_date = record['dep_date']
                trip_type = record['trip_type']
                scan_date = record['scan_date']
                new_flights = record['flights']
                fallback_price = record['fallback_price']
                
                smart_upsert(origin, dest, dep_date, trip_type, scan_date, new_flights, fallback_price)
                total_records += len(new_flights) if new_flights else 1
                
            logging.info(f"成功合併 {file_path}")
        except Exception as e:
            logging.error(f"合併 {file_path} 時發生錯誤: {e}")
            
    logging.info(f"所有資料庫更新完畢，共寫入/檢查 {total_records} 個航線任務。")
    
    logging.info("開始轉出 Excel 報表...")
    export_excel.export_to_excel()
    logging.info("Excel 轉出完成！")

if __name__ == "__main__":
    merge_all_results()
