import asyncio
import urllib.parse
from bs4 import BeautifulSoup
import re
import logging
from playwright.async_api import async_playwright
import time

async def abort_unnecessary_requests(route, request):
    # Only allow document, script, xhr, fetch
    if request.resource_type in ["image", "stylesheet", "font", "media"]:
        await route.abort()
    else:
        await route.continue_()

from datetime import date

async def test_date_fallback(context, task):
    origin, dest, dep_date, trip_type, return_days, ret_date, ci_mode = task
    scan_date = date.today().strftime("%Y-%m-%d")
    page = await context.new_page()
    await page.route("**/*", abort_unnecessary_requests)
    
    if trip_type == "oneway":
        q = f"{dep_date} {origin} 到 {dest} 單程"
    else:
        q = f"{dep_date} 到 {ret_date} {origin} 到 {dest} 來回"
        
    encoded_query = urllib.parse.quote_plus(q)
    url = f"https://www.google.com/travel/flights?q={encoded_query}&hl=zh-TW&curr=TWD"
    
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=35000)
        # Wait for the main flight list container
        await page.wait_for_selector('div[role="main"]', timeout=20000)
        await page.wait_for_timeout(2000)
        
        html = await page.content()
        soup = BeautifulSoup(html, 'html.parser')
        
        flights = []
        for li in soup.find_all('li'):
            text = li.get_text(separator=' ', strip=True)
            # Find price
            price_match = re.search(r'\$?(\d{1,3}(?:,\d{3})+)', text)
            if price_match and ('分鐘' in text or '小時' in text):
                price_str = price_match.group(1).replace(',', '')
                price = int(price_str)
                
                # Extract basic info
                # Simple heuristic mapping since we don't have the full JSON
                # 航空公司 is usually somewhere in the text
                airline = "Unknown"
                if "廈門航空" in text: airline = "XiamenAir"
                elif "中華航空" in text: airline = "China Airlines"
                elif "中國東方" in text: airline = "China Eastern"
                elif "國泰航空" in text: airline = "Cathay Pacific"
                elif "長榮航空" in text: airline = "EVA Air"
                elif "星宇航空" in text: airline = "STARLUX"
                else: 
                    # Try to guess airline by taking the first recognizable text parts
                    parts = text.split()
                    if len(parts) > 4:
                        airline = " / ".join(parts[2:4])
                
                flight = {
                    "price": price,
                    "airline": airline,
                    "flight_number": "Unknown",
                    "departure_time": "Unknown",
                    "arrival_time": "Unknown",
                    "duration": "Unknown",
                    "stops": "Unknown",
                    "booking_url": url
                }
                flights.append(flight)
                
        if flights:
            logging.info(f"[Fallback Playwright] [{dest}] {dep_date} {trip_type} 成功補漏！抓回 {len(flights)} 筆")
            if not ci_mode:
                from scraper import smart_upsert
                smart_upsert(origin, dest, dep_date, trip_type, scan_date, flights, fallback_price=None)
            return {"task": task, "flights": flights}
        else:
            logging.warning(f"[Fallback Playwright] [{dest}] {dep_date} {trip_type} 補漏失敗 (-1)")
            return {"task": task, "flights": []}
            
    except Exception as e:
        logging.error(f"[Fallback Playwright] [{dest}] {dep_date} {trip_type} 錯誤: {e}")
        return {"task": task, "flights": []}
    finally:
        await page.close()

async def run_fallback_playwright_async(retry_tasks):
    logging.info(f"開始執行 Playwright 補漏，共有 {len(retry_tasks)} 個任務...")
    start_time = time.time()
    
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, 
            args=['--disable-blink-features=AutomationControlled']
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="zh-TW"
        )
        
        # To avoid memory issues but maximize throughput on CI (4 cores, 16GB), process in batches of 20
        batch_size = 20
        for i in range(0, len(retry_tasks), batch_size):
            batch = retry_tasks[i:i+batch_size]
            tasks = [test_date_fallback(context, task) for task in batch]
            batch_results = await asyncio.gather(*tasks)
            results.extend(batch_results)
            
        await browser.close()
        
    elapsed = time.time() - start_time
    logging.info(f"Playwright 補漏執行完畢！花費時間: {elapsed:.2f} 秒")
    return results

def run_fallback_playwright(retry_tasks):
    """供主程式呼叫的同步進入點"""
    if not retry_tasks:
        return []
    return asyncio.run(run_fallback_playwright_async(retry_tasks, batch_size=10))
