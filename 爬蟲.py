import sqlite3
import os
import urllib.parse
from datetime import date, timedelta
import time
import re
import logging
import random
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import argparse
import json
from playwright_scraper import run_fallback_playwright

# 設定 Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("scraper.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)

DB_FILE = "flights.db"
db_lock = threading.Lock()

# 航線設定
ORIGIN = "TPE"
ASIA_DESTINATIONS = ["HND", "ICN", "SIN", "KUL", "URC", "KMG", "SGN"]
ASIA_RETURN_DAYS = 7
LONGHAUL_DESTINATIONS = ["LHR", "CDG", "MCO", "CAI", "OSL", "FCO", "ATH"]
LONGHAUL_RETURN_DAYS = 14

# Thread-local 用於儲存每個線程專屬的 httpx.Client
thread_local = threading.local()

def get_session():
    if not hasattr(thread_local, "session"):
        # 啟用 curl_cffi 模擬真實瀏覽器指紋
        thread_local.session = cffi_requests.Session(impersonate="chrome124")
    return thread_local.session

def init_db():
    with db_lock:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS flights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin TEXT,
            destination TEXT,
            departure_date TEXT,
            trip_type TEXT,
            price INTEGER,
            airline TEXT,
            flight_number TEXT,
            departure_time TEXT,
            arrival_time TEXT,
            duration TEXT,
            stops TEXT,
            booking_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    try:
        cursor.execute('ALTER TABLE flights ADD COLUMN scan_date TEXT')
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
    logging.info(f"資料庫 {DB_FILE} 初始化完成。")

def build_google_flights_url(origin: str, dest: str, dep_date: str, return_date: str = None) -> str:
    if return_date:
        q = f"flights from {origin} to {dest} on {dep_date} to {return_date}"
    else:
        q = f"one way flights from {origin} to {dest} on {dep_date}"
    encoded_query = urllib.parse.quote_plus(q)
    url = f"https://www.google.com/travel/flights?q={encoded_query}&hl=zh-TW&curr=TWD"
    return url

def insert_flight_record(cursor, data):
    cursor.execute('''
        INSERT INTO flights (
            origin, destination, departure_date, trip_type, scan_date,
            price, airline, flight_number, departure_time,
            arrival_time, duration, stops, booking_url
        ) VALUES (
            :origin, :destination, :departure_date, :trip_type, :scan_date,
            :price, :airline, :flight_number, :departure_time,
            :arrival_time, :duration, :stops, :booking_url
        )
    ''', data)

def smart_upsert(origin, dest, dep_date, trip_type, scan_date, new_flights, fallback_price=None):
    with db_lock:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cursor = conn.cursor()
        
        cursor.execute('''
        SELECT id, price, airline, flight_number, departure_time 
        FROM flights 
        WHERE origin=? AND destination=? AND departure_date=? AND trip_type=? AND scan_date=?
    ''', (origin, dest, dep_date, trip_type, scan_date))
    existing_records = cursor.fetchall()
    
    if not existing_records:
        if not new_flights:
            cursor.execute('''
                INSERT INTO flights (origin, destination, departure_date, trip_type, scan_date, price)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (origin, dest, dep_date, trip_type, scan_date, fallback_price))
        else:
            for f in new_flights:
                f['scan_date'] = scan_date
                insert_flight_record(cursor, f)
    else:
        if not new_flights:
            # 若原本只有一筆空紀錄 (NULL)，而這次確認為沒有班機 (-1) 或被阻擋 (-999)，則更新
            if len(existing_records) == 1 and existing_records[0][1] is None and fallback_price in (-1, -999):
                cursor.execute('UPDATE flights SET price=? WHERE id=?', (fallback_price, existing_records[0][0]))
        else:
            cursor.execute('''
                DELETE FROM flights 
                WHERE origin=? AND destination=? AND departure_date=? AND trip_type=? AND scan_date=? AND (price IS NULL OR price = -1 OR price = -999)
            ''', (origin, dest, dep_date, trip_type, scan_date))
            
            cursor.execute('''
                SELECT id, price, airline, flight_number, departure_time 
                FROM flights 
                WHERE origin=? AND destination=? AND departure_date=? AND trip_type=? AND scan_date=?
            ''', (origin, dest, dep_date, trip_type, scan_date))
            valid_records = cursor.fetchall()
            
            for f in new_flights:
                f['scan_date'] = scan_date
                matched_id = None
                for row in valid_records:
                    r_id, r_price, r_airline, r_fnum, r_deptime = row
                    is_same = False
                    if f['flight_number'] and r_fnum and f['flight_number'] == r_fnum:
                        is_same = True
                    elif not f['flight_number'] and not r_fnum and f['airline'] == r_airline and f['departure_time'] == r_deptime:
                        is_same = True
                        
                    if is_same:
                        matched_id = r_id
                        break
                
                if matched_id:
                    cursor.execute('''
                        UPDATE flights 
                        SET price=:price, booking_url=:booking_url, duration=:duration, 
                            stops=:stops, arrival_time=:arrival_time, departure_time=:departure_time
                        WHERE id=:id
                    ''', {**f, 'id': matched_id})
                else:
                    insert_flight_record(cursor, f)
                        
    conn.commit()
    conn.close()

def parse_flights_from_html(html: str) -> list:
    callbacks = re.findall(r'AF_initDataCallback\((.*?)\);', html, re.DOTALL)
    flight_data = None
    for cb in callbacks:
        if len(cb) > 20000:
            match = re.search(r"data:\s*(\[.*\]),\s*sideChannel:", cb, re.DOTALL)
            if not match: match = re.search(r"data:\s*(\[.*\])", cb, re.DOTALL)
            if match:
                try:
                    flight_data = json.loads(match.group(1).replace("undefined", "null"))
                    break
                except: pass
                
    flights = []
    def extract_flights(obj):
        if not isinstance(obj, list): return
        try:
            if len(obj) >= 2 and isinstance(obj[1], list) and len(obj[1]) >= 1 and isinstance(obj[1][0], list):
                price_val = obj[1][0][1]
                if isinstance(price_val, (int, float)) and price_val > 0:
                    if isinstance(obj[0], list) and len(obj[0]) >= 10 and isinstance(obj[0][2], list):
                        legs = obj[0][2]
                        if len(legs) > 0 and isinstance(legs[0], list):
                            flights.append(obj)
                            return
        except Exception:
            pass
        for item in obj:
            extract_flights(item)
            
    extract_flights(flight_data)
    
    parsed_results = []
    for f in flights:
        try:
            price = f[1][0][1]
            airlines = f[0][1]
            if isinstance(airlines, list):
                airline_str = "、".join([str(a) for a in airlines if isinstance(a, str)])
            else:
                airline_str = "未知航空"
                
            legs = f[0][2]
            stops_count = len(legs) - 1
            stops_str = "" if stops_count == 0 else f"轉機 {stops_count} 次"
            
            dep_time_arr = f[0][5]
            arr_time_arr = f[0][8]
            
            dep_time = f"{dep_time_arr[0]:02d}:{dep_time_arr[1]:02d}" if isinstance(dep_time_arr, list) and len(dep_time_arr) >= 2 else ""
            arr_time = f"{arr_time_arr[0]:02d}:{arr_time_arr[1]:02d}" if isinstance(arr_time_arr, list) and len(arr_time_arr) >= 2 else ""
            
            duration_mins = f[0][9]
            duration_str = ""
            if isinstance(duration_mins, int):
                hours = duration_mins // 60
                mins = duration_mins % 60
                duration_str = f"{hours} 小時 {mins} 分鐘"
                
            flight_num = ""
            if len(legs) > 0 and len(legs[0]) >= 23 and isinstance(legs[0][22], list):
                carrier = legs[0][22][0]
                num = legs[0][22][1]
                if carrier and num:
                    flight_num = f"{carrier}-{num}"
                    
            parsed_results.append({
                'price': price,
                'airline': airline_str,
                'stops': stops_str,
                'departure_time': dep_time,
                'arrival_time': arr_time,
                'duration': duration_str,
                'flight_number': flight_num
            })
        except Exception as e:
            continue
            
    return parsed_results

def fetch_flights_sync(origin, dest, dep_date, trip_type, return_days, ret_date, ci_mode=False):
    url = build_google_flights_url(origin, dest, dep_date, ret_date)
    
    scan_date = date.today().strftime("%Y-%m-%d")
    session = get_session()
    
    # 隨機延遲保護
    time.sleep(random.uniform(0.1, 1.0))
    
    try:
        response = session.get(url, timeout=15)
        if response.status_code != 200:
            logging.warning(f"[{dest}] {dep_date} {trip_type} HTTP {response.status_code} - 抓取錯誤，寫入 NULL")
            if ci_mode:
                return {'origin': origin, 'dest': dest, 'dep_date': dep_date, 'trip_type': trip_type, 'scan_date': scan_date, 'flights': [], 'fallback_price': None}
            smart_upsert(origin, dest, dep_date, trip_type, scan_date, [], fallback_price=None)
            return False
            
        if "Our systems have detected unusual traffic" in response.text or "Captcha" in response.text or "CAPTCHA" in response.text or "unusual traffic" in response.text:
            logging.error(f"[{dest}] {dep_date} {trip_type} 被 Google 防爬蟲機制阻擋！寫入 -999")
            if ci_mode:
                return {'origin': origin, 'dest': dest, 'dep_date': dep_date, 'trip_type': trip_type, 'scan_date': scan_date, 'flights': [], 'fallback_price': -999}
            smart_upsert(origin, dest, dep_date, trip_type, scan_date, [], fallback_price=-999)
            return False
            
        parsed_flights = parse_flights_from_html(response.text)
                                
        if not parsed_flights:
            logging.warning(f"[{dest}] {dep_date} {trip_type} 找不到任何航班 (確認為無班機)，寫入 -1")
            if ci_mode:
                return {'origin': origin, 'dest': dest, 'dep_date': dep_date, 'trip_type': trip_type, 'scan_date': scan_date, 'flights': [], 'fallback_price': -1}
            smart_upsert(origin, dest, dep_date, trip_type, scan_date, [], fallback_price=-1)
            return False
                                
        new_flights = []
        for details in parsed_flights:
            data = {
                'origin': origin,
                'destination': dest,
                'departure_date': dep_date,
                'trip_type': trip_type,
                'price': details['price'],
                'airline': details['airline'],
                'flight_number': details['flight_number'],
                'departure_time': details['departure_time'],
                'arrival_time': details['arrival_time'],
                'duration': details['duration'],
                'stops': details['stops'],
                'booking_url': url
            }
            new_flights.append(data)
            
        fallback = -1 if not new_flights else None
        if ci_mode:
            return {'origin': origin, 'dest': dest, 'dep_date': dep_date, 'trip_type': trip_type, 'scan_date': scan_date, 'flights': new_flights, 'fallback_price': fallback}
            
        # 執行同日智能去重與補漏
        smart_upsert(origin, dest, dep_date, trip_type, scan_date, new_flights, fallback_price=fallback)
            
        if new_flights:
            logging.info(f"[{dest}] {dep_date} {trip_type} 成功更新/寫入 {len(new_flights)} 筆航班資料")
            return True
        else:
            logging.warning(f"[{dest}] {dep_date} {trip_type} 無法解析出有效航班，寫入 -1")
            return False
            
    except Exception as e:
        logging.error(f"[{dest}] {dep_date} {trip_type} 請求失敗: {e} - 寫入 NULL")
        if ci_mode:
            return {'origin': origin, 'dest': dest, 'dep_date': dep_date, 'trip_type': trip_type, 'scan_date': scan_date, 'flights': [], 'fallback_price': None}
        smart_upsert(origin, dest, dep_date, trip_type, scan_date, [], fallback_price=None)
        return False

def scrape_flights(target_city=None, ci_mode=False):
    tasks = []
    today = date.today()
    
    # 建立 14 個城市 x 未來 330 天 x 2種方式(來回/單程) 的終極完整迴圈
    for i in range(1, 331):
        dep_date = (today + timedelta(days=i)).strftime("%Y-%m-%d")
        
        # 亞洲線
        for dest in ASIA_DESTINATIONS:
            if target_city and dest != target_city: continue
            ret_date = (today + timedelta(days=i + ASIA_RETURN_DAYS)).strftime("%Y-%m-%d")
            tasks.append((ORIGIN, dest, dep_date, "oneway", 0, None, ci_mode))
            tasks.append((ORIGIN, dest, dep_date, "roundtrip", ASIA_RETURN_DAYS, ret_date, ci_mode))
            
        # 歐美線
        for dest in LONGHAUL_DESTINATIONS:
            if target_city and dest != target_city: continue
            ret_date = (today + timedelta(days=i + LONGHAUL_RETURN_DAYS)).strftime("%Y-%m-%d")
            tasks.append((ORIGIN, dest, dep_date, "oneway", 0, None, ci_mode))
            tasks.append((ORIGIN, dest, dep_date, "roundtrip", LONGHAUL_RETURN_DAYS, ret_date, ci_mode))

    logging.info(f"開始極速爬取，預計發送 {len(tasks)} 個請求...")
    start_time = time.time()
    
    # 啟動 ThreadPoolExecutor
    MAX_WORKERS = 5
    results = []
    retry_tasks = []
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_flights_sync, *task): task for task in tasks}
        
        for future in as_completed(futures):
            task = futures[future]
            try:
                res = future.result()
                if ci_mode:
                    if res and res.get('fallback_price') == -1:
                        retry_tasks.append(task)
                    elif res:
                        results.append(res)
                else:
                    if res is False:
                        retry_tasks.append(task)
            except Exception as e:
                logging.error(f"任務發生未預期錯誤: {e}")
                
    elapsed = time.time() - start_time
    logging.info(f"所有 {len(tasks)} 條航線極速爬取完成！總花費時間: {elapsed:.2f} 秒")
    
    # 執行第二階段補漏
    if retry_tasks:
        logging.info(f"準備進入第二階段，針對 {len(retry_tasks)} 個失敗任務進行 Playwright 補漏...")
        fallback_results = run_fallback_playwright(retry_tasks)
        if ci_mode and fallback_results:
            scan_date = date.today().strftime("%Y-%m-%d")
            for fr in fallback_results:
                task = fr['task']
                origin, dest, dep_date, trip_type, return_days, ret_date, _ci_mode = task
                if fr['flights']:
                    results.append({
                        'origin': origin, 'dest': dest, 'dep_date': dep_date, 
                        'trip_type': trip_type, 'scan_date': scan_date, 
                        'flights': fr['flights'], 'fallback_price': None
                    })
                else:
                    results.append({
                        'origin': origin, 'dest': dest, 'dep_date': dep_date, 
                        'trip_type': trip_type, 'scan_date': scan_date, 
                        'flights': [], 'fallback_price': -1
                    })
    
    if ci_mode and target_city:
        with open(f"{target_city}_results.json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        logging.info(f"CI模式: 已將結果匯出至 {target_city}_results.json")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", type=str, help="指定抓取的單一城市代碼 (例如 HND)")
    parser.add_argument("--ci", action="store_true", help="啟用 CI 模式，將結果匯出為 JSON 而非寫入 DB")
    args = parser.parse_args()

    if not args.ci:
        init_db()
        
    scrape_flights(target_city=args.city, ci_mode=args.ci)
