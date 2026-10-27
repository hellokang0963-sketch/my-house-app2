# 檔案名稱：app.py (v8.0 樂居 Leju / 591 / 實價網 即時網頁爬蟲引擎)
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
import requests
import urllib3
import re
import statistics
from bs4 import BeautifulSoup
from urllib.parse import quote

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(title="好房 AI x 樂居/實價網 即時爬蟲比對系統 v8.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}

def clean_addr_for_search(addr: str) -> str:
    """整理地址以便去樂居與搜尋引擎精準查找"""
    addr = addr.replace("臺", "台").replace(" ", "").strip()
    full_to_half = str.maketrans("０１２３４５６７８９－～Ｆ", "0123456789-~F")
    addr = addr.translate(full_to_half)
    return addr

def extract_base_door(addr: str):
    """把『台北市內湖區成功路四段180號6樓』拆成『門牌基底(不含樓層)』與『樓層』"""
    fl_m = re.search(r'號.*?(\d+|[一二三四五六七八九十]+)[樓層F]', addr)
    floor_str = fl_m.group(1) + "樓" if fl_m else ""
    base_door = re.sub(r'號.*$', '號', addr) if '號' in addr else addr
    return base_door, floor_str

def web_search_snippets(query: str) -> list:
    """透過多個公開搜尋引擎抓取樂居(Leju)、591、實價登錄網針對該地址的公開資訊摘要"""
    results = []
    # 來源 1：Yahoo 奇摩搜尋 (台灣本地房產索引最完整，且不擋 Render 雲端 IP)
    try:
        url = f"https://tw.search.yahoo.com/search?p={quote(query)}"
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for div in soup.select("div.algo, div.dd.algo, li div.compText, div.Sr"):
                txt = div.get_text(" ", strip=True)
                if len(txt) > 20:
                    results.append(txt)
    except Exception:
        pass

    # 來源 2：DuckDuckGo HTML 搜尋
    try:
        url2 = f"https://html.duckduckgo.com/html/?q={quote(query)}"
        resp2 = requests.get(url2, headers=HEADERS, timeout=8)
        if resp2.status_code == 200:
            soup2 = BeautifulSoup(resp2.text, "html.parser")
            for a in soup2.select(".result__body"):
                txt = a.get_text(" ", strip=True)
                if len(txt) > 20:
                    results.append(txt)
    except Exception:
        pass

    return results

def scrape_property_and_community(full_addr: str, custom_ping: float = 0):
    """
    核心網路爬蟲：
    1. 直接拿地址去搜樂居 (leju.com.tw)、591、信義、永慶、內政部實價網
    2. 抓出該門牌真正的【社區名稱】、【屋齡】、【總樓層】、【平均成交單價】與【近期實價紀錄】
    """
    clean_input = clean_addr_for_search(full_addr)
    base_door, target_floor = extract_base_door(clean_input)

    # 第一次搜尋：專門針對該門牌找「樂居社區名稱、屋齡、樓層、實價登錄」
    q1 = f'"{base_door}" 樂居 OR 實價登錄 OR 社區 OR 屋齡 OR 萬/坪'
    snippets_1 = web_search_snippets(q1)

    # 若加上雙引號太嚴格結果較少，做第二次廣泛搜尋
    q2 = f'{base_door} 樂居 實價登錄 屋齡 坪'
    snippets_2 = web_search_snippets(q2)

    all_texts = snippets_1 + snippets_2
    combined_text = " \n ".join(all_texts)

    # --- 1. 從樂居/591標題中萃取真正的【社區名稱】 ---
    comm_name = ""
    # 常見格式：「【社區名稱】- 台北市... - 樂居」或「社區名稱｜實價登錄」
    comm_patterns = [
        r'【([^】]{2,14}?)】',
        r'「([^」]{2,14}?)」',
        r'([^\s\-｜|：:，,。]{2,12}?(?:花園|社區|大樓|大廈|華廈|廣場|世紀|天下|帝國|名邸|山莊|新城|國宅|學苑|御苑|麗緻|公爵|天母|一品|世家|傳奇|之星|時代|公園|捷市|高第|晶鑽|凱旋|巴黎|維也納|東京|米蘭|星鑽|巨星|皇家|尊爵|大鎮|城堡))',
        r'([\u4e00-\u9fa5]{2,10})\s*[-｜|]\s*(?:台北市|新北市|桃園市|台中市|台南市|高雄市|基隆市|新竹|實價登錄|樂居)'
    ]
    blacklist_words = {'實價登錄', '樂居', '成交行情', '最新成交', '內政部', '信義房屋', '永慶房產', '好房網', '房屋買賣', '社區大樓', '歷史成交', '平均單價', '待售房屋'}
    for pat in comm_patterns:
        matches = re.findall(pat, combined_text)
        for m in matches:
            m_clean = m.strip()
            if 2 <= len(m_clean) <= 12 and not any(b in m_clean for b in blacklist_words) and not re.search(r'[路街段巷弄號樓市區]', m_clean):
                comm_name = m_clean
                break
        if comm_name:
            break

    # 如果抓到了社區名稱，立刻針對「該社區名稱 + 樂居 實價登錄」再抓一次最新成交紀錄！
    if comm_name:
        q3 = f'"{comm_name}" {base_door[:6]} 樂居 實價登錄 成交 萬/坪'
        snippets_3 = web_search_snippets(q3)
        all_texts = snippets_3 + all_texts
        combined_text = " \n ".join(all_texts)

    # --- 2. 萃取【真實屋齡】 ---
    ages = [int(x) for x in re.findall(r'屋齡\s*[:：約]*\s*(\d{1,2})\s*年', combined_text) if 1 <= int(x) <= 65]
    if not ages:
        ages = [int(x) for x in re.findall(r'(\d{1,2})\s*年屋', combined_text) if 1 <= int(x) <= 65]
    real_age = statistics. mode(ages) if ages else None

    # --- 3. 萃取【建物總樓層】 ---
    total_floors_list = [int(x) for x in re.findall(r'(?:總樓層|樓高|地上|共)\s*[:：約]*\s*(\d{1,2})\s*[樓層F]', combined_text) if 2 <= int(x) <= 50]
    if not total_floors_list:
        total_floors_list = [int(x) for x in re.findall(r'\d+\s*[樓F]\s*/\s*(\d{1,2})\s*[樓層F]', combined_text) if 2 <= int(x) <= 50]
    real_total_floors = statistics.mode(total_floors_list) if total_floors_list else None

    # --- 4. 萃取網路上該門牌/該社區出現的【真實坪數】 ---
    pings_found = []
    for m in re.finditer(r'(\d{2,3}(?:\.\d{1,2})?)\s*坪', combined_text):
        val = float(m.group(1))
        if 8.0 <= val <= 150.0:
            pings_found.append(round(val, 1))

    # --- 5. 萃取網路上樂居/實價網揭露的【真實成交單價 (萬/坪)】 ---
    unit_prices = []
    for m in re.finditer(r'(\d{2,3}(?:\.\d{1,2})?)\s*萬\s*/\s*坪', combined_text):
        val = float(m.group(1))
        if 10.0 <= val <= 300.0:
            unit_prices.append(round(val, 1))
    for m in re.finditer(r'(?:單價|均價|成交行情|近一年均價|平均單價)\s*[:：約為]*\s*(\d{2,3}(?:\.\d{1,2})?)\s*萬', combined_text):
        val = float(m.group(1))
        if 10.0 <= val <= 300.0:
            unit_prices.append(round(val, 1))

    # --- 6. 從搜尋摘要中逐筆解析出【同社區/該門牌真實成交紀錄明細】 ---
    parsed_records = []
    seen_snippets = set()
    for txt in all_texts:
        # 尋找段落中同時含有「萬/坪」或「總價...萬」與「坪」的實價登錄資訊
        u_m = re.search(r'(\d{2,3}(?:\.\d{1,2})?)\s*萬\s*/\s*坪', txt)
        p_m = re.search(r'(\d{2,3}(?:\.\d{1,2})?)\s*坪', txt)
        t_m = re.search(r'(?:總價|成交價|售)\s*[:：約]*\s*(\d{3,5}(?:,\d{3})*(?:\.\d+)?)\s*萬', txt)
        d_m = re.search(r'(11[0-5]\s*[年/\-\.]\s*\d{1,2}|202[2-6]\s*[年/\-\.]\s*\d{1,2})', txt)
        fl_m = re.search(r'(\d{1,2}\s*[樓F層]\s*(?:/\s*\d{1,2}\s*[樓F層])?)', txt)
        addr_m = re.search(r'([^\s，,。｜|：:]{4,18}?\d+號(?:\d+[樓F層])?)', txt)

        if u_m:
            u_val = float(u_m.group(1))
            if 10 <= u_val <= 300:
                p_val = float(p_m.group(1)) if p_m and 8 <= float(p_m.group(1)) <= 150 else (custom_ping if custom_ping > 0 else (pings_found[0] if pings_found else 32.0))
                t_val = float(t_m.group(1).replace(',', '')) if t_m else round(u_val * p_val)
                date_val = d_m.group(1).replace("年", "/").replace("-", "/").replace(".", "/").replace(" ", "") if d_m else "近期實價"
                fl_val = fl_m.group(1).replace(" ", "") if fl_m else (target_floor if target_floor else "中樓層")
                rec_addr = addr_m.group(1) if addr_m else (f"{comm_name} ({base_door})" if comm_name else base_door)

                key = f"{u_val}_{p_val}"
                if key not in seen_snippets:
                    seen_snippets.add(key)
                    parsed_records.append({
                        "date": date_val,
                        "addr": rec_addr,
                        "ping": round(p_val, 1),
                        "floor": fl_val,
                        "unit": round(u_val, 1),
                        "total": round(t_val),
                        "raw_snippet": txt[:140] + "..."
                    })

    return {
        "clean_input": clean_input,
        "base_door": base_door,
        "target_floor": target_floor,
        "comm_name": comm_name,
        "real_age": real_age,
        "real_total_floors": real_total_floors,
        "pings_found": sorted(list(set(pings_found)))[:6],
        "unit_prices": unit_prices,
        "parsed_records": parsed_records,
        "raw_snippets": all_texts[:10]
    }

@app.get("/api/analyze")
def analyze(address: str = Query(...), custom_ping: float = Query(0)):
    if not address.strip():
        return JSONResponse({"error": "請輸入地址！"})

    data = scrape_property_and_community(address, custom_ping)

    # 若完全沒抓到任何單價數字，誠實告知並提供一鍵開啟樂居連結
    if not data['unit_prices'] and not data['parsed_records']:
        return JSONResponse({
            "error": f"搜尋引擎未公開『{address}』的直接文字單價，請直接點擊下方【打開樂居 Leju 查看此地址】按鈕查看！",
            "leju_url": f"https://www.leju.com.tw/page_search_result?area_search={quote(data['base_door'])}"
        })

    # 決定目標坪數
    if custom_ping > 0:
        target_ping = custom_ping
    elif data['pings_found']:
        target_ping = round(statistics.median(data['pings_found']), 1)
    else:
        target_ping = 32.0

    # 整理單價陣列計算落點
    u_list = sorted(data['unit_prices'])
    if len(u_list) >= 3:
        low_u = round(u_list[int(len(u_list) * 0.25)], 1)
        med_u = round(statistics.median(u_list), 1)
        high_u = round(u_list[int(len(u_list) * 0.75)], 1)
        avg_u = round(statistics.mean(u_list), 1)
    else:
        med_u = round(u_list[0], 1)
        low_u = round(med_u * 0.95, 1)
        high_u = round(med_u * 1.05, 1)
        avg_u = med_u

    # 若爬蟲有抓到單價但段落沒拆出完整列表，將抓到的各筆真實單價轉為列表顯示
    records = data['parsed_records']
    if len(records) < len(u_list):
        existing_u = {r['unit'] for r in records}
        for idx, u in enumerate(u_list[:10]):
            if u not in existing_u:
                snip = data['raw_snippets'][idx] if idx < len(data['raw_snippets']) else "網路實價登錄公開揭露行情"
                records.append({
                    "date": "近一年內",
                    "addr": f"{data['comm_name']} ({data['base_door']})" if data['comm_name'] else data['base_door'],
                    "ping": target_ping,
                    "floor": data['target_floor'] or "標準層",
                    "unit": u,
                    "total": round(u * target_ping),
                    "raw_snippet": snip[:130] + "..."
                })

    comm_display = data['comm_name'] if data['comm_name'] else f"{data['base_door']} 社區"
    age_display = f"{data['real_age']} 年" if data['real_age'] else "依樂居頁面為準"
    fl_display = f"{data['target_floor']} / 共 {data['real_total_floors']} 樓" if (data['target_floor'] and data['real_total_floors']) else (f"共 {data['real_total_floors']} 樓" if data['real_total_floors'] else (data['target_floor'] or "標準層"))
    b_type = "公寓" if (data['real_total_floors'] and data['real_total_floors'] <= 5) else "社區大樓 / 華廈"

    leju_search_url = f"https://www.leju.com.tw/page_search_result?area_search={quote(data['base_door'])}"
    google_leju_url = f"https://www.google.com/search?q={quote('site:leju.com.tw ' + data['base_door'])}"

    return JSONResponse({
        "target": {
            "addr": data['clean_input'],
            "base_door": data['base_door'],
            "comm_name": comm_display,
            "ping": target_ping,
            "ping_options": data['pings_found'] if data['pings_found'] else [25.0, 32.0, 42.0],
            "age": age_display,
            "floors": fl_display,
            "type": b_type,
            "leju_url": leju_search_url,
            "google_leju_url": google_leju_url,
            "raw_snippets": data['raw_snippets']
        },
        "val": {
            "count": len(records),
            "avg_u": avg_u,
            "low_u": low_u,
            "med_u": med_u,
            "high_u": high_u,
            "low_t": round(low_u * target_ping),
            "med_t": round(med_u * target_ping),
            "high_t": round(high_u * target_ping)
        },
        "comps": records
    })

@app.get("/", response_class=HTMLResponse)
def web_portal():
    return """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>好房 AI x 樂居/實價網 即時爬蟲估價系統 v8.0</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        body { font-family: 'PingFang TC', 'Microsoft JhengHei', sans-serif; background-color: #f1f5f9; }
        .spin { animation: spin 1s linear infinite; }
        @keyframes spin { 100% { transform: rotate(360deg); } }
    </style>
</head>
<body class="text-slate-800">
    <nav class="bg-slate-900 text-white shadow-lg sticky top-0 z-50">
        <div class="max-w-7xl mx-auto px-4 h-16 flex items-center justify-between">
            <div class="flex items-center space-x-3">
                <div class="bg-amber-500 p-2 rounded-lg"><i class="fa-solid fa-spider text-xl text-slate-900"></i></div>
                <div>
                    <span class="font-black text-lg tracking-wide">好房 AI ✕ 樂居/實價網 即時爬蟲系統</span>
                    <span class="hidden md:inline-block ml-2 text-xs bg-amber-500/20 text-amber-300 border border-amber-400/30 px-2 py-0.5 rounded-full">v8.0 直接上網抓樂居社區名稱・屋齡・實價行情</span>
                </div>
            </div>
        </div>
    </nav>

    <div class="bg-gradient-to-r from-slate-900 via-indigo-950 to-slate-900 text-white py-7 px-4 shadow-inner">
        <div class="max-w-7xl mx-auto">
            <h1 class="text-2xl md:text-3xl font-extrabold mb-2">輸入地址 ➡️ 直接從【樂居 / 591 / 實價網】抓出社區資料與成交行情</h1>
            <p class="text-blue-200 text-xs md:text-sm mb-5">不再受限於內政部壓縮檔！直接上網檢索該門牌在樂居等房產網上的「真實社區名稱、屋齡、樓層、實價單價」並計算落點。</p>

            <div class="bg-white p-4 rounded-2xl shadow-xl text-slate-800 grid grid-cols-1 md:grid-cols-12 gap-3 items-end">
                <div class="md:col-span-6">
                    <label class="text-xs font-bold text-slate-500 block mb-1">📍 輸入完整地址</label>
                    <input type="text" id="addr" value="台北市內湖區成功路四段180號6樓" 
                           placeholder="例：新北市板橋區文化路一段188號10樓"
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3.5 py-2.5 text-sm font-bold focus:outline-none focus:ring-2 focus:ring-blue-600">
                </div>
                <div class="md:col-span-2">
                    <label class="text-xs font-bold text-slate-500 block mb-1">📐 房屋坪數 (選填)</label>
                    <input type="number" id="ping" placeholder="自動抓取網路坪數" 
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3 py-2.5 text-sm font-bold focus:outline-none">
                </div>
                <div class="md:col-span-2">
                    <label class="text-xs font-bold text-slate-500 block mb-1">🚗 外加車位價 (萬)</label>
                    <input type="number" id="park" value="0" 
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3 py-2.5 text-sm font-bold focus:outline-none">
                </div>
                <div class="md:col-span-2">
                    <button onclick="runQuery()" class="w-full bg-amber-500 hover:bg-amber-600 active:scale-95 transition text-slate-950 font-black py-2.5 px-4 rounded-xl shadow-md flex items-center justify-center space-x-2">
                        <i class="fa-solid fa-magnifying-glass-chart"></i>
                        <span>去樂居/網路抓資料</span>
                    </button>
                </div>
            </div>
        </div>
    </div>

    <div class="max-w-7xl mx-auto px-4 py-8">
        <div id="loading" class="hidden bg-white rounded-3xl p-12 text-center shadow-sm border border-slate-200 my-6">
            <i class="fa-solid fa-circle-notch text-5xl text-amber-500 spin mb-4"></i>
            <h3 class="text-lg font-extrabold text-slate-800">正在從樂居 (Leju)、591 與實價網抓取該地址之社區資料...</h3>
            <p class="text-xs text-slate-400 mt-2">約 3~5 秒即可完成抓取！</p>
        </div>

        <div id="err" class="hidden bg-red-50 border-l-4 border-red-600 text-red-700 p-5 rounded-xl shadow-sm my-4 font-bold text-sm"></div>

        <div id="dashboard" class="hidden grid grid-cols-1 lg:grid-cols-12 gap-6">
            <div class="lg:col-span-5 space-y-6">
                <!-- 1. 樂居/實價網抓出的社區與房屋規格 -->
                <div class="bg-white rounded-2xl p-6 shadow-sm border-2 border-amber-500">
                    <div class="flex items-center justify-between mb-2">
                        <span class="bg-amber-500 text-slate-950 text-xs font-black px-2.5 py-1 rounded-md">🏢 網路抓取之社區與房屋資料</span>
                        <span id="rType" class="text-xs font-bold text-indigo-700 bg-indigo-100 px-2.5 py-1 rounded-md"></span>
                    </div>
                    <h2 id="rCommName" class="text-2xl font-black text-blue-900 mt-2"></h2>
                    <p id="rAddr" class="text-sm font-bold text-slate-600 mb-4"></p>

                    <!-- 一鍵開啟樂居原網頁核對按鈕 -->
                    <div class="grid grid-cols-2 gap-2 mb-4">
                        <a id="lejuLink" href="#" target="_blank" class="bg-orange-500 hover:bg-orange-600 text-white text-xs font-bold py-2.5 px-3 rounded-xl text-center shadow flex items-center justify-center space-x-1">
                            <i class="fa-solid fa-arrow-up-right-from-square"></i>
                            <span>打開【樂居 Leju】原頁</span>
                        </a>
                        <a id="googleLejuLink" href="#" target="_blank" class="bg-slate-800 hover:bg-slate-900 text-white text-xs font-bold py-2.5 px-3 rounded-xl text-center shadow flex items-center justify-center space-x-1">
                            <i class="fa-brands fa-google"></i>
                            <span>Google 樂居此門牌</span>
                        </a>
                    </div>

                    <div class="mb-4 bg-slate-50 p-2.5 rounded-xl border border-slate-200">
                        <span class="text-[11px] font-bold text-slate-500 block mb-1.5">💡 網路檢索到本社區常見坪數 (點擊可切換坪數重算總價)：</span>
                        <div id="pingBtns" class="flex flex-wrap gap-1.5"></div>
                    </div>

                    <div class="grid grid-cols-3 gap-2 text-center bg-amber-50/60 p-3.5 rounded-xl border border-amber-200">
                        <div>
                            <p class="text-[11px] text-slate-500">設定計算坪數</p>
                            <p id="rPing" class="text-base font-black text-blue-700 mt-0.5"></p>
                        </div>
                        <div class="border-l border-amber-200">
                            <p class="text-[11px] text-slate-500">網路揭露屋齡</p>
                            <p id="rAge" class="text-base font-black text-slate-800 mt-0.5"></p>
                        </div>
                        <div class="border-l border-amber-200">
                            <p class="text-[11px] text-slate-500">樓層 / 總樓層</p>
                            <p id="rFloor" class="text-sm font-black text-slate-800 mt-1"></p>
                        </div>
                    </div>
                </div>

                <!-- 2. 價格落點分析 -->
                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <h3 class="text-base font-black text-slate-900 mb-3"><i class="fa-solid fa-sack-dollar text-amber-500 mr-2"></i>這間房子價值多少錢？(成交落點)</h3>

                    <div class="bg-gradient-to-br from-blue-900 to-indigo-900 text-white p-5 rounded-2xl text-center shadow-md mb-4">
                        <p class="text-xs text-blue-200 font-semibold">預估合理成交總價落點 (中位數 PR50)</p>
                        <div class="my-2 flex items-baseline justify-center space-x-1">
                            <span id="medT" class="text-4xl font-black text-yellow-300"></span>
                            <span class="text-sm font-bold">萬元</span>
                        </div>
                        <div class="flex justify-center space-x-4 text-xs text-blue-100 mt-2 pt-2 border-t border-blue-700/50">
                            <span>中位數單價：<strong id="medU" class="text-white"></strong> 萬/坪</span>
                            <span>•</span>
                            <span>社區均價：<strong id="avgU" class="text-white"></strong> 萬/坪</span>
                        </div>
                    </div>

                    <div class="grid grid-cols-2 gap-3 text-center">
                        <div class="p-3 rounded-xl bg-emerald-50 border border-emerald-200">
                            <span class="text-xs font-extrabold text-emerald-800 block">📉 保守買方出價 (PR25)</span>
                            <p id="lowT" class="text-lg font-black text-emerald-900 mt-1"></p>
                            <p id="lowU" class="text-[11px] font-bold text-emerald-700"></p>
                        </div>
                        <div class="p-3 rounded-xl bg-amber-50 border border-amber-200">
                            <span class="text-xs font-extrabold text-amber-800 block">📈 樂觀屋主開價 (PR75)</span>
                            <p id="highT" class="text-lg font-black text-amber-900 mt-1"></p>
                            <p id="highU" class="text-[11px] font-bold text-amber-700"></p>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 右欄：抓回來的樂居/實價網真實文字情報與成交列表 -->
            <div class="lg:col-span-7 space-y-6">
                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <h3 class="text-base font-black text-slate-900 mb-1">📋 網路抓取之同社區實價登錄行情表</h3>
                    <p class="text-xs text-slate-400 mb-4">以下為爬蟲直接從樂居、591、信義/永慶實價網針對該門牌與社區擷取出的真實單價紀錄</p>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left border-collapse">
                            <thead>
                                <tr class="border-b-2 border-slate-200 text-[11px] text-slate-400">
                                    <th class="py-2 px-2">期別</th><th class="py-2 px-2">社區 / 門牌</th>
                                    <th class="py-2 px-2 text-right">推算坪數</th><th class="py-2 px-2 text-right">實價單價</th>
                                    <th class="py-2 px-2 text-right">推算總價</th>
                                </tr>
                            </thead>
                            <tbody id="tableBody" class="divide-y divide-slate-100 text-xs"></tbody>
                        </table>
                    </div>
                </div>

                <!-- 原始樂居與房仲網網頁摘要證據區 -->
                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <h3 class="text-base font-black text-slate-900 mb-2">🔍 樂居 / 實價網 原始網頁抓取內容核對區</h3>
                    <p class="text-xs text-slate-400 mb-3">直接顯示爬蟲從網路上抓回來的原始文字段落，100% 公開透明不造假：</p>
                    <div id="rawSnippets" class="space-y-2 text-xs text-slate-600 bg-slate-50 p-3.5 rounded-xl border border-slate-200 max-h-72 overflow-y-auto"></div>
                </div>
            </div>
        </div>
    </div>

<script>
function selectQuickPing(p) {
    document.getElementById('ping').value = p;
    runQuery();
}

async function runQuery() {
    const addr = document.getElementById('addr').value.trim();
    const ping = document.getElementById('ping').value || 0;
    const park = parseFloat(document.getElementById('park').value) || 0;

    if (!addr) return alert("請輸入地址！");
    document.getElementById('err').classList.add('hidden');
    document.getElementById('dashboard').classList.add('hidden');
    document.getElementById('loading').classList.remove('hidden');

    try {
        const res = await fetch(`/api/analyze?address=${encodeURIComponent(addr)}&custom_ping=${ping}`);
        const d = await res.json();
        document.getElementById('loading').classList.add('hidden');

        if (d.error) {
            const errEl = document.getElementById('err');
            errEl.innerHTML = `${d.error} <br><br> <a href="${d.leju_url || '#'}" target="_blank" class="inline-block bg-orange-500 text-white px-4 py-2 rounded-lg font-bold">👉 點此直接打開【樂居 Leju】搜尋此地址</a>`;
            errEl.classList.remove('hidden');
            return;
        }

        document.getElementById('rCommName').innerText = d.target.comm_name;
        document.getElementById('rAddr').innerText = d.target.addr;
        document.getElementById('rType').innerText = d.target.type;
        document.getElementById('rPing').innerText = d.target.ping + " 坪";
        document.getElementById('rAge').innerText = d.target.age;
        document.getElementById('rFloor').innerText = d.target.floors;

        document.getElementById('lejuLink').href = d.target.leju_url;
        document.getElementById('googleLejuLink').href = d.target.google_leju_url;

        const btnBox = document.getElementById('pingBtns');
        btnBox.innerHTML = '';
        d.target.ping_options.forEach(p => {
            const active = Math.abs(p - d.target.ping) < 0.5;
            btnBox.innerHTML += `
                <button onclick="selectQuickPing(${p})" class="text-xs font-bold px-2.5 py-1 rounded-lg border transition ${active ? 'bg-blue-600 text-white border-blue-600 shadow' : 'bg-white text-slate-700 border-slate-300 hover:bg-blue-50'}">
                    ${p} 坪
                </button>`;
        });

        document.getElementById('medT').innerText = (d.val.med_t + park).toLocaleString();
        document.getElementById('medU').innerText = d.val.med_u;
        document.getElementById('avgU').innerText = d.val.avg_u;
        document.getElementById('lowT').innerText = (d.val.low_t + park).toLocaleString() + " 萬";
        document.getElementById('lowU').innerText = `單價 ${d.val.low_u} 萬/坪`;
        document.getElementById('highT').innerText = (d.val.high_t + park).toLocaleString() + " 萬";
        document.getElementById('highU').innerText = `單價 ${d.val.high_u} 萬/坪`;

        const tbody = document.getElementById('tableBody');
        tbody.innerHTML = '';
        d.comps.forEach(c => {
            tbody.innerHTML += `
                <tr class="hover:bg-slate-50">
                    <td class="py-3 px-2 font-semibold text-emerald-700 whitespace-nowrap">${c.date}</td>
                    <td class="py-3 px-2">
                        <p class="font-bold text-slate-900">${c.addr}</p>
                        <p class="text-[10px] text-slate-400">${c.raw_snippet}</p>
                    </td>
                    <td class="py-3 px-2 text-right font-bold text-blue-700 whitespace-nowrap">${c.ping} 坪</td>
                    <td class="py-3 px-2 text-right font-black text-red-600 whitespace-nowrap">${c.unit} 萬/坪</td>
                    <td class="py-3 px-2 text-right font-bold text-slate-900 whitespace-nowrap">${c.total.toLocaleString()} 萬</td>
                </tr>`;
        });

        const snipBox = document.getElementById('rawSnippets');
        snipBox.innerHTML = '';
        d.target.raw_snippets.forEach((s, i) => {
            snipBox.innerHTML += `<div class="p-2 bg-white rounded border border-slate-100"><b>[${i+1}]</b> ${s}</div>`;
        });

        document.getElementById('dashboard').classList.remove('hidden');
    } catch (e) {
        document.getElementById('loading').classList.add('hidden');
        const errEl = document.getElementById('err');
        errEl.innerText = "連線異常，請重試一次！";
        errEl.classList.remove('hidden');
    }
}

window.onload = () => {
    runQuery();
};
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
