import sqlite3
import os
import urllib.parse
from datetime import date, timedelta
import time
import re
import logging
import random
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# 設定 Logging (改用 test_sin.log 區別)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("test_sin.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)

DB_FILE = "flights.db"
db_lock = threading.Lock()

# Thread-local 用於儲存每個線程專屬的 requests.Session()
thread_local = threading.local()

def get_session():
    if not hasattr(thread_local, "session"):
        thread_local.session = requests.Session()
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
    with db_lock: # 使用 Lock 防止多線程同時寫入導致 DB Locked
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
                if len(existing_records) == 1 and existing_records[0][1] is None and fallback_price == -1:
                    cursor.execute('UPDATE flights SET price=-1 WHERE id=?', (existing_records[0][0],))
            else:
                cursor.execute('''
                    DELETE FROM flights 
                    WHERE origin=? AND destination=? AND departure_date=? AND trip_type=? AND scan_date=? AND (price IS NULL OR price = -1)
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

def parse_aria_label(label: str) -> dict:
    details = {
        'price': None, 'airline': '未知航空', 'stops': '',
        'departure_time': '', 'arrival_time': '', 'duration': ''
    }
    price_match = re.search(r'([0-9,]+)\s*(?:新台幣|TWD)|(?:NT\$|\$|TWD)\s*([0-9,]+)', label)
    if price_match:
        val = price_match.group(1) or price_match.group(2)
        details['price'] = int(val.replace(',', ''))
        
    airline_match = re.search(r'搭乘([^的]+)的', label)
    if airline_match:
        details['airline'] = airline_match.group(1).strip()
        
    if '直達航班' in label or '直飛' in label:
        details['stops'] = ""
    else:
        stops_match = re.search(r'(轉機\s*\d+\s*次)', label)
        details['stops'] = stops_match.group(1) if stops_match else "有轉機"
        
    dept_time_match = re.search(r'(?:上午|下午|清晨|晚上|凌晨)?\s*(\d{1,2}:\d{2})\s*於', label)
    if dept_time_match:
        details['departure_time'] = dept_time_match.group(1)
        
    arr_time_match = re.search(r'(?:上午|下午|清晨|晚上|凌晨)?\s*(\d{1,2}:\d{2})\s*抵達', label)
    if arr_time_match:
        details['arrival_time'] = arr_time_match.group(1)
        
    duration_match = re.search(r'總交通時間：(.*?)(?:\s+選擇|$)', label)
    if duration_match:
        details['duration'] = duration_match.group(1).strip()
        
    return details

def fetch_flights_task(origin, dest, dep_date, trip_type, return_days, ret_date, scan_date):
    url = build_google_flights_url(origin, dest, dep_date, ret_date)
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0'
    ]
    headers = {
        'User-Agent': random.choice(user_agents),
        'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Sec-Ch-Ua': '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Upgrade-Insecure-Requests': '1',
    }
    
    session = get_session()
    
    # 隨機延遲保護 (因為有多線程併發，延遲可以錯開打到伺服器的時間)
    time.sleep(random.uniform(0.1, 1.0))
    
    try:
        response = session.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            logging.warning(f"[{dest}] {dep_date} {trip_type} HTTP {response.status_code} - 抓取錯誤，寫入 NULL")
            smart_upsert(origin, dest, dep_date, trip_type, scan_date, [], fallback_price=None)
            return False
            
        soup = BeautifulSoup(response.text, 'html.parser')
        
        items = soup.find_all('li', class_='pIav2d')
        if not items:
            items = soup.find_all(lambda tag: tag.has_attr('aria-label') and 
                                ('抵達' in tag['aria-label']) and 
                                ('新台幣' in tag['aria-label'] or 'TWD' in tag['aria-label'] or '$' in tag['aria-label']))
                                
        if not items:
            logging.warning(f"[{dest}] {dep_date} {trip_type} 找不到任何航班 (確認為無班機)，寫入 -1")
            smart_upsert(origin, dest, dep_date, trip_type, scan_date, [], fallback_price=-1)
            return False
            
        new_flights = []
        for item in items:
            if item.has_attr('aria-label'):
                aria_label = item['aria-label']
            else:
                aria_tag = item.find(attrs={'aria-label': True})
                if not aria_tag: continue
                aria_label = aria_tag['aria-label']
                
            if "新台幣" not in aria_label and "TWD" not in aria_label:
                continue
                
            details = parse_aria_label(aria_label)
            if not details.get('price'): continue
            
            itinerary_match = re.search(r'itinerary=([A-Z0-9]{2,3}-[A-Z0-9]{2,3}-[A-Z0-9]{2}-\d{1,4}-\d{8})', str(item))
            flight_num = ''
            if itinerary_match:
                parts = itinerary_match.group(1).split('-')
                if len(parts) >= 4:
                    flight_num = f"{parts[2]}-{parts[3]}"
                    
            data = {
                'origin': origin,
                'destination': dest,
                'departure_date': dep_date,
                'trip_type': trip_type,
                'price': details['price'],
                'airline': details['airline'],
                'flight_number': flight_num,
                'departure_time': details['departure_time'],
                'arrival_time': details['arrival_time'],
                'duration': details['duration'],
                'stops': details['stops'],
                'booking_url': url
            }
            new_flights.append(data)
            
        smart_upsert(origin, dest, dep_date, trip_type, scan_date, new_flights, fallback_price=-1 if not new_flights else None)
            
        if new_flights:
            logging.info(f"[{dest}] {dep_date} {trip_type} 成功更新/寫入 {len(new_flights)} 筆航班資料")
            return True
        else:
            logging.warning(f"[{dest}] {dep_date} {trip_type} 無法解析出有效航班，寫入 -1")
            return False
            
    except Exception as e:
        logging.error(f"[{dest}] {dep_date} {trip_type} 請求失敗: {e} - 寫入 NULL")
        smart_upsert(origin, dest, dep_date, trip_type, scan_date, [], fallback_price=None)
        return False

def scrape_sin():
    tasks = []
    today = date.today()
    scan_date = today.strftime("%Y-%m-%d")
    
    # 建立未來 1 到 330 天的任務 (單程 + 7天來回)，目標改為 SIN
    for i in range(1, 331):
        dep_date = (today + timedelta(days=i)).strftime("%Y-%m-%d")
        ret_date = (today + timedelta(days=i+7)).strftime("%Y-%m-%d")
        tasks.append(("TPE", "SIN", dep_date, "oneway", 0, None, scan_date))
        tasks.append(("TPE", "SIN", dep_date, "roundtrip", 7, ret_date, scan_date))

    logging.info(f"開始多線程極速測試: 新加坡(SIN)航線，共 {len(tasks)} 個請求...")
    start_time = time.time()
    
    # 使用 ThreadPoolExecutor，開啟 5 個 worker (兼顧速度與防 ban)
    MAX_WORKERS = 5
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # 將任務交給線程池
        futures = {executor.submit(fetch_flights_task, *task): task for task in tasks}
        
        # 等待所有任務完成
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                logging.error(f"任務發生未預期錯誤: {e}")
                
    elapsed = time.time() - start_time
    logging.info(f"新加坡(SIN) 330天所有航線爬取完成！總花費時間: {elapsed:.2f} 秒")

if __name__ == "__main__":
    init_db()
    scrape_sin()
