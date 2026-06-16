import httpx
import json
import urllib.parse

def test_google_batchexecute():
    """
    這是一支展示如何破解與呼叫 Google 內部 RPC (batchexecute) 的概念驗證程式碼。
    """
    print("啟動 Google Flights 內部 API (batchexecute) 測試...")

    url = "https://www.google.com/_/TravelFrontendUi/data/batchexecute"
    
    # Header 必須偽裝成從網頁端發出的 AJAX 請求
    headers = {
        "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "x-same-domain": "1"
    }

    # =========================================================================
    # [核心破解機密]
    # Google 的 batchexecute 會把所有的參數包裝成極度複雜的 Protobuf 陣列。
    # 下方這串看似亂碼的 JSON 陣列，其實對應了：
    # [ 出發地 "TPE", 目的地 "NRT", 日期 "2026-10-10", 單程/來回, 艙等, 人數... ]
    # =========================================================================
    
    # (此為經過逆向工程擷取出的最小化 Request Payload 範例)
    # 注意：Google 會不定期更改這個陣列的層級結構！這也是這個方法唯一的缺點。
    flight_request_payload = '[null,null,1,null,[],1,[1,0,0,0],null,null,null,null,null,null,[[[[["TPE",0]]],[[["NRT",0]]],["2026-10-10"]]]]'
    
    # 將 payload 包裝成 f.req 格式 (Google 專用格式)
    # 第一個參數通常是 RPC 函式名稱 (例如 GetFlights 或類似的 ID)
    rpc_function_id = "w01Kpe" # 這是 Google Flights 查詢的特定 ID (可能會變動)
    
    f_req = json.dumps([[
        [rpc_function_id, flight_request_payload, None, "generic"]
    ]])
    
    data = {"f.req": f_req}
    encoded_data = urllib.parse.urlencode(data)

    print(f"\n[發送攔截封包] Payload 大小僅: {len(encoded_data)} Bytes")
    print(f"對比：請求完整 HTML 網頁通常需要 500,000 Bytes 以上！")
    
    # 實際發出請求 (此處不保證回傳成功，因為 rpc_function_id 與 payload 結構具有時效性)
    try:
        response = httpx.post(url, headers=headers, content=encoded_data, timeout=10)
        
        print(f"\n[伺服器回應狀態碼]: {response.status_code}")
        if response.status_code == 200:
            # Google 的 RPC 回應會在開頭塞入防 XSSI 的無意義字元 (例如 )]}' )
            raw_text = response.text
            clean_json_str = raw_text.split('\n', 2)[2] if '\n' in raw_text else raw_text
            
            print("\n[成功取得純資料 (JSON)] 擷取前 300 個字元:")
            print(clean_json_str[:300] + "...\n")
            print("=> 您會發現裡面完全沒有 HTML (<div>, <li>)，全是純粹的字串與陣列！")
            print("=> Python 只要用 json.loads() 解析，就能在 0.01 秒內取出價格，速度是 BeautifulSoup 的百倍。")
        else:
            print(f"請求被拒絕，原因: {response.text}")

    except Exception as e:
        print(f"請求發生錯誤: {e}")

if __name__ == "__main__":
    test_google_batchexecute()
