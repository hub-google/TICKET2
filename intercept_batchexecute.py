import asyncio
from playwright.async_api import async_playwright
import json
import urllib.parse

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        async def handle_request(request):
            if "batchexecute" in request.url and request.method == "POST":
                post_data = request.post_data
                if post_data and "f.req" in post_data:
                    parsed = urllib.parse.parse_qs(post_data)
                    freq = parsed.get("f.req", [""])[0]
                    print("--- Intercepted batchexecute payload ---")
                    print(freq)
                    print("----------------------------------------")
                    
        page.on("request", handle_request)
        
        print("Navigating to Google Flights...")
        # 去程 TPE -> NRT
        await page.goto("https://www.google.com/travel/flights?q=Flights%20to%20NRT%20from%20TPE%20on%202026-10-10%20oneway")
        await page.wait_for_timeout(5000)
        await browser.close()

asyncio.run(main())
