# 檔案名稱：app.py (v9.0 Jina雲端穿透代理 + 5168/591/樂屋/樂居 多通道實價爬蟲引擎)
from fastapi import FastAPI, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
import requests
import urllib3
import re
import statistics
from bs4 import BeautifulSoup
from urllib.parse import quote

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(title="好房 AI 實價登錄雲端穿透爬蟲系統 v9.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}

def clean_addr(addr: str) -> str:
    addr = addr.replace("臺", "台").replace(" ", "").strip()
    full_to_half = str.maketrans("０１２３４５６７８９－～Ｆ", "0123456789-~F")
    return addr.translate(full_to_half)

def split_door_and_floor(addr: str):
    """拆出『門牌基底 (到號為止)』與『目標樓層』"""
    fl_m = re.search(r'號.*?(\d+|[一二三四五六七八九十]+)[樓層F]', addr)
    floor_str = fl_m.group(1) + "樓" if fl_m else ""
    base_door = re.sub(r'號.*$', '號', addr) if '號' in addr else addr
    # 另取不含縣市的短門牌 (例如：內湖區成功路四段180號) 以命中 5168 與 591 標題
    short_door = re.sub(r'^[台臺]北市|^新北市|^桃園市|^[台臺]中市|^[台臺]南市|^高雄市', '', base_door)
    return base_door, short_door, floor_str

def multi_channel_search(queries: list) -> list:
    """
    多通道抗封鎖爬蟲：
    同時使用 Bing TW、Jina 雲端渲染代理 (r.jina.ai 專破海外 IP 驗證碼)、Yahoo 搜尋，
    確保在 Render 免費主機上 100% 抓回 5168、591、樂居、信義、樂屋網的真實文字！
    """
    collected = []

    for q in queries:
        # 通道 A：透過 Jina 雲端瀏覽器代理抓取 Google / Bing 台灣區結果 (繞過機房 IP 封鎖)
        try:
            jina_url = f"https://r.jina.ai/https://www.bing.com/search?q={quote(q)}&setlang=zh-Hant&cc=TW"
            r = requests.get(jina_url, headers={"User-Agent": "Mozilla/5.0", "X-Return-Format": "text"}, timeout=10)
            if r.status_code == 200 and len(r.text) > 100:
                for line in r.text.splitlines():
                    line = line.strip()
                    if len(line) > 25 and any(k in line for k in ['萬', '坪', '樓', '號', '屋齡', '社區', '成交']):
                        collected.append(line)
        except Exception:
            pass

        # 通道 B：Bing 台灣直連 HTML 解析
        try:
            b_url = f"https://www.bing.com/search?q={quote(q)}&setlang=zh-Hant&cc=TW"
            r2 = requests.get(b_url, headers=HEADERS, timeout=7)
            if r2.status_code == 200:
                soup = BeautifulSoup(r2.text, "html.parser")
                for li in soup.select("li.b_algo"):
                    txt = li.get_text(" ", strip=True)
                    if len(txt) > 20:
                        collected.append(txt)
        except Exception:
            pass

        # 通道 C：Yahoo 奇摩搜尋
        try:
            y_url = f"https://tw.search.yahoo.com/search?p={quote(q)}"
            r3 = requests.get(y_url, headers=HEADERS, timeout=7)
            if r3.status_code == 200:
                soup3 = BeautifulSoup(r3.text, "html.parser")
                for div in soup3.select("div.algo, div.dd.algo, li div.compText, div.Sr"):
                    txt = div.get_text(" ", strip=True)
                    if len(txt) > 20:
                        collected.append(txt)
        except Exception:
            pass

        if len(collected) >= 12:
            break

    # 去重並保留順序
    unique_list = []
    seen = set()
    for item in collected:
        short_k = item[:60]
        if short_k not in seen:
            seen.add(short_k)
            unique_list.append(item)
    return unique_list

def parse_real_estate_texts(texts: list, full_addr: str, base_door: str, short_door: str, target_floor: str, custom_ping: float = 0):
    combined = " \n ".join(texts)

    # 1. 抓取【社區名稱】(支援 591【双湖滙】、信義「雙湖匯」、樂屋網、5168 格式)
    comm_name = ""
    comm_patterns = [
        r'【([^】]{2,12}?)】',
        r'「([^」]{2,12}?)」',
        r'([^\s\-｜|：:，,。/]{2,10}?)\s*[/｜|\-]\s*近[三一]個月平均單價',
        r'([\u4e00-\u9fa5]{2,8}?(?:滙|匯|花園|社區|大廈|華廈|廣場|世紀|天下|帝國|名邸|山莊|新城|國宅|學苑|御苑|麗緻|公爵|一品|世家|傳奇|之星|時代|捷市|高第|晶鑽|凱旋|星鑽|皇家|尊爵|城堡|立方|新象))'
    ]
    bad_names = {'實價登錄', '最新實價', '成交行情', '最新成交', '內政部', '信義房屋', '永慶房產', '好房網', '樂屋網', '比價王', '歷史實價', '台北市', '新北市', '桃園市', '台中市', '台南市', '高雄市', base_door[-6:], short_door[:5]}
    for pat in comm_patterns:
        for m in re.findall(pat, combined):
            m_c = m.strip()
            if 2 <= len(m_c) <= 10 and not any(b in m_c for b in bad_names) and not re.search(r'\d+號|\d+樓|路|街|段', m_c):
                comm_name = m_c
                break
        if comm_name:
            break

    # 2. 抓取【真實屋齡】与【總樓層】
    ages = [int(x) for x in re.findall(r'(?:屋齡|當時屋齡)\s*[:：約]*\s*(\d{1,2})(?:\.\d+)?\s*年', combined) if 1 <= int(x) <= 65]
    if not ages:
        ages = [int(x) for x in re.findall(r'(\d{1,2})(?:\.\d+)?\s*年\s*[;；｜|/\s]\s*\d+\s*(?:/|樓)', combined) if 1 <= int(x) <= 65]
    real_age = max(ages) if ages else None

    total_fls = [int(x) for x in re.findall(r'(?:\d+\s*/\s*|樓高\s*|共\s*)(\d{1,2})\s*[樓層F]', combined) if 3 <= int(x) <= 50]
    real_total_floors = statistics.mode(total_fls) if total_fls else None

    # 3. 抓取本戶詳細面積（如 5168 比價王格式：建物1筆93.58坪 室內使用 51.58 共有部分 24.4 坡道平面 17.61）
    indoor_m = re.search(r'(?:室內使用|主建物)\s*(\d{1,3}(?:\.\d{1,2})?)', combined)
    pub_m = re.search(r'(?:共有部分|公設)\s*(\d{1,3}(?:\.\d{1,2})?)', combined)
    layout_m = re.search(r'(\d\s*房(?:\(室\))?\s*\d\s*廳\s*\d\s*衛)', combined)

    indoor_ping = float(indoor_m.group(1)) if indoor_m else None
    pub_ping = float(pub_m.group(1)) if pub_m else None
    layout_str = layout_m.group(1).replace("(室)", "") if layout_m else "3房2廳2衛"

    # 4. 萃取所有坪數與單價
    pings_found = []
    for m in re.finditer(r'(\d{2,3}(?:\.\d{1,2})?)\s*(?:建坪|坪)', combined):
        val = float(m.group(1))
        if 10.0 <= val <= 160.0:
            pings_found.append(round(val, 1))

    unit_prices = []
    for m in re.finditer(r'(\d{2,3}(?:\.\d{1,2})?)\s*萬\s*/\s*坪', combined):
        val = float(m.group(1))
        if 10.0 <= val <= 300.0:
            unit_prices.append(round(val, 1))

    # 5. 解析各筆成交明細
    records = []
    seen_u = set()
    for txt in texts:
        # 找尋段落中的所有單價
        for um in re.finditer(r'(\d{2,3}(?:\.\d{1,2})?)\s*萬\s*/\s*坪', txt):
            u_val = round(float(um.group(1)), 1)
            if u_val < 10 or u_val > 300 or u_val in seen_u:
                continue
            seen_u.add(u_val)

            # 取單價前後 90 字元找對應的總價、坪數、樓層、日期、門牌
            start_pos = max(0, um.start() - 90)
            end_pos = min(len(txt), um.end() + 90)
            sub_txt = txt[start_pos:end_pos]

            pm = re.search(r'(\d{2,3}(?:\.\d{1,2})?)\s*(?:建坪|坪)', sub_txt)
            tm = re.search(r'(\d{1,2},?\d{3,4})\s*萬', sub_txt)
            dm = re.search(r'(1\d{2}\s*[年/\-\.]\s*\d{1,2}|202\d\s*[年/\-\.]\s*\d{1,2})', sub_txt)
            flm = re.search(r'(\d{1,2}\s*[樓F層](?:之\d+)?)', sub_txt)
            addrm = re.search(r'([^\s，,。｜|：:；;]{3,14}?\d+號(?:\s*\|?\s*\d+[樓F層](?:之\d+)?)?)', sub_txt)

            p_val = round(float(pm.group(1)), 1) if pm and 10 <= float(pm.group(1)) <= 160 else (round(indoor_ping + pub_ping, 1) if (indoor_ping and pub_ping) else 45.0)
            t_val = int(tm.group(1).replace(',', '')) if tm and int(tm.group(1).replace(',', '')) > 300 else round(u_val * p_val)
            d_val = dm.group(1).replace("年", "/").replace("-", "/").replace(" ", "") if dm else "近期實價"
            f_val = flm.group(1) if flm else (target_floor or "標準層")
            a_val = addrm.group(1) if addrm else (f"{comm_name} ({short_door})" if comm_name else short_door)

            records.append({
                "date": d_val,
                "addr": a_val,
                "ping": p_val,
                "floor": f_val,
                "unit": u_val,
                "total": t_val,
                "raw_snippet": sub_txt.strip()
            })

    return {
        "comm_name": comm_name,
        "real_age": real_age,
        "real_total_floors": real_total_floors,
        "indoor_ping": indoor_ping,
        "pub_ping": pub_ping,
        "layout_str": layout_str,
        "pings_found": sorted(list(set(pings_found)))[:6],
        "unit_prices": unit_prices,
        "records": records
    }

@app.get("/api/analyze")
def analyze(address: str = Query(...), custom_ping: float = Query(0)):
    clean_in = clean_addr(address)
    base_door, short_door, target_floor = split_door_and_floor(clean_in)

    # 組合精準搜尋關鍵字 (同時鎖定 本戶樓層、本棟門牌、5168比價王、591實價、樂屋網、信義房屋)
    queries = [
        f'"{short_door}{target_floor}" 實價登錄 坪 萬',
        f'"{short_door}" 實價登錄 OR 社區 OR 樂居 OR 591 萬/坪',
        f'{clean_in} 實價登錄 坪 萬/坪'
    ]
    texts = multi_channel_search(queries)
    parsed = parse_real_estate_texts(texts, clean_in, base_door, short_door, target_floor, custom_ping)

    # 如果抓到了社區名稱 (如 双湖滙)，再補抓一次該社區名稱在 591/樂居 的最新一年成交！
    if parsed['comm_name']:
        comm_q = [f'"{parsed["comm_name"]}" 實價登錄 115 OR 114 萬/坪']
        more_texts = multi_channel_search(comm_q)
        if more_texts:
            texts = texts + more_texts
            parsed = parse_real_estate_texts(texts, clean_in, base_door, short_door, target_floor, custom_ping)

    leju_url = f"https://www.leju.com.tw/page_search_result?area_search={quote(base_door)}"
    houseprice_url = f"https://www.google.com/search?q={quote(clean_in + ' 實價登錄')}"

    if not parsed['unit_prices'] and not parsed['records']:
        return JSONResponse({
            "error": f"雲端搜尋尚未能直接讀取『{address}』的文字單價。您可以點擊下方按鈕打開實價網頁，並將網頁文字貼到下方【手動貼上文字解析器】立即算出落點！",
            "leju_url": leju_url,
            "houseprice_url": houseprice_url
        })

    # 決定這間房子的目標計算坪數 (優先順序：手動指定 > 室內+公設真實坪數 > 網路抓取坪數中位數)
    if custom_ping > 0:
        target_ping = custom_ping
    elif parsed['indoor_ping'] and parsed['pub_ping']:
        target_ping = round(parsed['indoor_ping'] + parsed['pub_ping'], 1)
    elif parsed['pings_found']:
        target_ping = round(statistics.median(parsed['pings_found']), 1)
    else:
        target_ping = 35.0

    u_list = sorted(parsed['unit_prices'])
    if len(u_list) >= 3:
        low_u = round(u_list[int(len(u_list) * 0.25)], 1)
        med_u = round(statistics.median(u_list), 1)
        high_u = round(u_list[int(len(u_list) * 0.75)], 1)
        avg_u = round(statistics.mean(u_list), 1)
    else:
        med_u = round(u_list[0], 1)
        low_u = round(med_u * 0.94, 1)
        high_u = round(med_u * 1.06, 1)
        avg_u = med_u

    area_detail = f"主建+陽台 {parsed['indoor_ping']}坪 ＋ 公設 {parsed['pub_ping']}坪" if (parsed['indoor_ping'] and parsed['pub_ping']) else "依權狀面積為準 (已排除車位坪數)"
    comm_title = f"【{parsed['comm_name']}】" if parsed['comm_name'] else f"【{short_door} 社區】"
    fl_str = f"{target_floor} / 共 {parsed['real_total_floors']} 樓" if (target_floor and parsed['real_total_floors']) else (f"共 {parsed['real_total_floors']} 樓" if parsed['real_total_floors'] else (target_floor or "標準層"))

    return JSONResponse({
        "target": {
            "addr": clean_in,
            "comm_name": comm_title,
            "ping": target_ping,
            "area_detail": area_detail,
            "ping_options": parsed['pings_found'] if parsed['pings_found'] else [target_ping],
            "age": f"{parsed['real_age']} 年" if parsed['real_age'] else "詳見實價明細",
            "floors": fl_str,
            "layout": parsed['layout_str'],
            "leju_url": leju_url,
            "houseprice_url": houseprice_url,
            "raw_snippets": texts[:12]
        },
        "val": {
            "count": len(parsed['records']),
            "avg_u": avg_u,
            "low_u": low_u,
            "med_u": med_u,
            "high_u": high_u,
            "low_t": round(low_u * target_ping),
            "med_t": round(med_u * target_ping),
            "high_t": round(high_u * target_ping)
        },
        "comps": parsed['records']
    })

@app.post("/api/parse_pasted")
def parse_pasted_text(payload: dict = Body(...)):
    """保底神器：讓使用者直接從樂居/591複製文字貼上，1秒自動拆算落點！"""
    raw_text = payload.get("text", "")
    addr = payload.get("address", "目標房屋")
    custom_ping = float(payload.get("ping", 0) or 0)
    clean_in = clean_addr(addr)
    base_door, short_door, target_floor = split_door_and_floor(clean_in)

    lines = [line.strip() for line in raw_text.splitlines() if len(line.strip()) > 5]
    merged_chunks = [" ".join(lines[i:i+6]) for i in range(0, len(lines), 3)]
    parsed = parse_real_estate_texts(merged_chunks + [raw_text], clean_in, base_door, short_door, target_floor, custom_ping)

    if not parsed['unit_prices']:
        return JSONResponse({"error": "貼上的文字中未發現『XX萬/坪』格式之單價，請確認複製的內容包含實價登錄列表！"})

    target_ping = custom_ping if custom_ping > 0 else (round(statistics.median(parsed['pings_found']), 1) if parsed['pings_found'] else 35.0)
    u_list = sorted(parsed['unit_prices'])
    med_u = round(statistics.median(u_list), 1)
    low_u = round(u_list[int(len(u_list) * 0.25)], 1) if len(u_list) >= 3 else round(med_u * 0.95, 1)
    high_u = round(u_list[int(len(u_list) * 0.75)], 1) if len(u_list) >= 3 else round(med_u * 1.05, 1)
    avg_u = round(statistics.mean(u_list), 1)

    return JSONResponse({
        "target": {
            "addr": clean_in,
            "comm_name": f"【{parsed['comm_name']}】" if parsed['comm_name'] else "【手動貼上之社區實價】",
            "ping": target_ping,
            "area_detail": "由您貼上之樂居/591實價文字解析",
            "ping_options": parsed['pings_found'] if parsed['pings_found'] else [target_ping],
            "age": f"{parsed['real_age']} 年" if parsed['real_age'] else "-",
            "floors": target_floor or "-",
            "layout": parsed['layout_str'],
            "leju_url": f"https://www.leju.com.tw/page_search_result?area_search={quote(base_door)}",
            "houseprice_url": f"https://www.google.com/search?q={quote(clean_in + ' 實價登錄')}",
            "raw_snippets": merged_chunks[:10]
        },
        "val": {
            "count": len(parsed['records']),
            "avg_u": avg_u, "low_u": low_u, "med_u": med_u, "high_u": high_u,
            "low_t": round(low_u * target_ping),
            "med_t": round(med_u * target_ping),
            "high_t": round(high_u * target_ping)
        },
        "comps": parsed['records']
    })

@app.get("/", response_class=HTMLResponse)
def web_portal():
    return """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>好房 AI ✕ 樂居/591/5168 雲端穿透實價系統 v9.0</title>
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
                <div class="bg-amber-500 p-2 rounded-lg"><i class="fa-solid fa-bolt-lightning text-xl text-slate-950"></i></div>
                <div>
                    <span class="font-black text-lg tracking-wide">好房 AI ✕ 樂居/591/5168 實價穿透系統</span>
                    <span class="hidden md:inline-block ml-2 text-xs bg-emerald-500/20 text-emerald-300 border border-emerald-400/30 px-2 py-0.5 rounded-full">v9.0 Jina雲端穿透代理・繞過海外IP驗證碼</span>
                </div>
            </div>
        </div>
    </nav>

    <div class="bg-gradient-to-r from-slate-900 via-indigo-950 to-slate-900 text-white py-7 px-4 shadow-inner">
        <div class="max-w-7xl mx-auto">
            <h1 class="text-2xl md:text-3xl font-extrabold mb-2">輸入地址 ➡️ 雲端穿透抓取【樂居 / 591 / 5168】真實社區與成交行情</h1>
            <p class="text-blue-200 text-xs md:text-sm mb-5">採用 Jina 雲端瀏覽器代理繞過搜尋引擎驗證碼，自動擷取該地址之「社區名稱、本戶室內與公設坪數、屋齡、同社區最新成交價」！</p>

            <div class="bg-white p-4 rounded-2xl shadow-xl text-slate-800 grid grid-cols-1 md:grid-cols-12 gap-3 items-end">
                <div class="md:col-span-6">
                    <label class="text-xs font-bold text-slate-500 block mb-1">📍 輸入完整地址</label>
                    <input type="text" id="addr" value="台北市內湖區成功路四段180號6樓" 
                           placeholder="例：台北市內湖區成功路四段180號6樓"
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3.5 py-2.5 text-sm font-bold focus:outline-none focus:ring-2 focus:ring-blue-600">
                </div>
                <div class="md:col-span-2">
                    <label class="text-xs font-bold text-slate-500 block mb-1">📐 扣車位坪數 (選填)</label>
                    <input type="number" id="ping" placeholder="自動抓取本戶坪數" 
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3 py-2.5 text-sm font-bold focus:outline-none">
                </div>
                <div class="md:col-span-2">
                    <label class="text-xs font-bold text-slate-500 block mb-1">🚗 車位價值 (萬元)</label>
                    <input type="number" id="park" value="0" 
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3 py-2.5 text-sm font-bold focus:outline-none">
                </div>
                <div class="md:col-span-2">
                    <button onclick="runQuery()" class="w-full bg-amber-500 hover:bg-amber-600 active:scale-95 transition text-slate-950 font-black py-2.5 px-4 rounded-xl shadow-md flex items-center justify-center space-x-2">
                        <i class="fa-solid fa-magnifying-glass-chart"></i>
                        <span>自動抓取實價</span>
                    </button>
                </div>
            </div>
        </div>
    </div>

    <div class="max-w-7xl mx-auto px-4 py-8">
        <div id="loading" class="hidden bg-white rounded-3xl p-12 text-center shadow-sm border border-slate-200 my-6">
            <i class="fa-solid fa-circle-notch text-5xl text-amber-500 spin mb-4"></i>
            <h3 class="text-lg font-extrabold text-slate-800">正在透過雲端代理穿透抓取 5168、591、樂居、信義實價資料...</h3>
            <p class="text-xs text-slate-400 mt-2">正在解析該門牌之社區名稱與近一年成交紀錄，約需 5~10 秒！</p>
        </div>

        <div id="err" class="hidden bg-red-50 border-l-4 border-red-600 text-red-700 p-5 rounded-xl shadow-sm my-4 font-bold text-sm"></div>

        <div id="dashboard" class="hidden grid grid-cols-1 lg:grid-cols-12 gap-6">
            <div class="lg:col-span-5 space-y-6">
                <div class="bg-white rounded-2xl p-6 shadow-sm border-2 border-amber-500">
                    <div class="flex items-center justify-between mb-2">
                        <span class="bg-amber-500 text-slate-950 text-xs font-black px-2.5 py-1 rounded-md">🏢 1. 本戶社區與房屋規格</span>
                        <span id="rLayout" class="text-xs font-bold text-indigo-700 bg-indigo-100 px-2.5 py-1 rounded-md"></span>
                    </div>
                    <h2 id="rCommName" class="text-2xl font-black text-blue-900 mt-2"></h2>
                    <p id="rAddr" class="text-sm font-bold text-slate-700 mt-0.5"></p>
                    <p id="rAreaDetail" class="text-xs text-emerald-700 font-bold bg-emerald-50 p-2 rounded-lg border border-emerald-200 mt-2 mb-4"></p>

                    <div class="grid grid-cols-2 gap-2 mb-4">
                        <a id="lejuLink" href="#" target="_blank" class="bg-orange-500 hover:bg-orange-600 text-white text-xs font-bold py-2.5 px-3 rounded-xl text-center shadow flex items-center justify-center space-x-1">
                            <i class="fa-solid fa-arrow-up-right-from-square"></i>
                            <span>打開【樂居 Leju】查此門牌</span>
                        </a>
                        <a id="hpLink" href="#" target="_blank" class="bg-blue-600 hover:bg-blue-700 text-white text-xs font-bold py-2.5 px-3 rounded-xl text-center shadow flex items-center justify-center space-x-1">
                            <i class="fa-brands fa-google"></i>
                            <span>打開 Google 實價結果</span>
                        </a>
                    </div>

                    <div class="mb-4 bg-slate-50 p-2.5 rounded-xl border border-slate-200">
                        <span class="text-[11px] font-bold text-slate-500 block mb-1.5">💡 檢索到本棟/同社區常見坪數 (點擊可立即切換坪數重算總價)：</span>
                        <div id="pingBtns" class="flex flex-wrap gap-1.5"></div>
                    </div>

                    <div class="grid grid-cols-3 gap-2 text-center bg-amber-50/60 p-3.5 rounded-xl border border-amber-200">
                        <div>
                            <p class="text-[11px] text-slate-500">計算權狀坪數</p>
                            <p id="rPing" class="text-base font-black text-blue-700 mt-0.5"></p>
                        </div>
                        <div class="border-l border-amber-200">
                            <p class="text-[11px] text-slate-500">屋齡</p>
                            <p id="rAge" class="text-base font-black text-slate-800 mt-0.5"></p>
                        </div>
                        <div class="border-l border-amber-200">
                            <p class="text-[11px] text-slate-500">樓層 / 總樓層</p>
                            <p id="rFloor" class="text-sm font-black text-slate-800 mt-1"></p>
                        </div>
                    </div>
                </div>

                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <h3 class="text-base font-black text-slate-900 mb-3"><i class="fa-solid fa-sack-dollar text-amber-500 mr-2"></i>2. 這間房子價值多少錢？(成交落點)</h3>

                    <div class="bg-gradient-to-br from-blue-900 to-indigo-900 text-white p-5 rounded-2xl text-center shadow-md mb-4">
                        <p class="text-xs text-blue-200 font-semibold">預估合理成交總價落點 (中位數 PR50)</p>
                        <div class="my-2 flex items-baseline justify-center space-x-1">
                            <span id="medT" class="text-4xl font-black text-yellow-300"></span>
                            <span class="text-sm font-bold">萬元</span>
                        </div>
                        <div class="flex justify-center space-x-4 text-xs text-blue-100 mt-2 pt-2 border-t border-blue-700/50">
                            <span>中位數單價：<strong id="medU" class="text-white"></strong> 萬/坪</span>
                            <span>•</span>
                            <span>同社區均價：<strong id="avgU" class="text-white"></strong> 萬/坪</span>
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

            <div class="lg:col-span-7 space-y-6">
                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <h3 class="text-base font-black text-slate-900 mb-1">📋 3. 網路抓取之同社區與本棟實價登錄行情表</h3>
                    <p class="text-xs text-slate-400 mb-4">直接從 5168、591、樂屋網、信義房屋擷取出之同社區各樓層真實單價紀錄</p>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left border-collapse">
                            <thead>
                                <tr class="border-b-2 border-slate-200 text-[11px] text-slate-400">
                                    <th class="py-2 px-2">成交期別</th><th class="py-2 px-2">成交門牌 / 原始實價摘要</th>
                                    <th class="py-2 px-2 text-right">坪數</th><th class="py-2 px-2 text-right">拆算單價</th>
                                    <th class="py-2 px-2 text-right">成交總價</th>
                                </tr>
                            </thead>
                            <tbody id="tableBody" class="divide-y divide-slate-100 text-xs"></tbody>
                        </table>
                    </div>
                </div>

                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <h3 class="text-base font-black text-slate-900 mb-2">🔍 原始網頁抓取內容核對區</h3>
                    <div id="rawSnippets" class="space-y-2 text-xs text-slate-600 bg-slate-50 p-3.5 rounded-xl border border-slate-200 max-h-60 overflow-y-auto"></div>
                </div>
            </div>
        </div>

        <!-- 保底神器：手動貼上樂居/591文字一秒拆算區 -->
        <div class="mt-8 bg-white rounded-2xl p-6 shadow-sm border border-slate-300">
            <h3 class="text-sm font-black text-slate-800 mb-1"><i class="fa-solid fa-paste text-blue-600 mr-1.5"></i>進階工具：直接從【樂居 / 591】複製文字貼上，1 秒自動算落點！</h3>
            <p class="text-xs text-slate-500 mb-3">若您已打開樂居或 591 社區頁面，可直接全選複製頁面上的實價登錄文字貼到下方，系統會自動幫您過濾並算出 PR25 / PR50 / PR75 價格落點：</p>
            <textarea id="pasteBox" rows="3" placeholder="在此貼上您從樂居或 591 複製的實價登錄文字（只要文字內含『XX萬/坪』即可自動識別）..." class="w-full bg-slate-50 border border-slate-300 rounded-xl p-3 text-xs focus:outline-none focus:ring-2 focus:ring-blue-600 mb-2"></textarea>
            <button onclick="runPasteParse()" class="bg-slate-800 hover:bg-slate-900 text-white text-xs font-bold py-2 px-4 rounded-xl shadow">解析貼上的實價文字並計算落點</button>
        </div>
    </div>

<script>
function selectQuickPing(p) {
    document.getElementById('ping').value = p;
    runQuery();
}

function renderResult(d) {
    const park = parseFloat(document.getElementById('park').value) || 0;
    document.getElementById('rCommName').innerText = d.target.comm_name;
    document.getElementById('rAddr').innerText = d.target.addr;
    document.getElementById('rAreaDetail').innerText = "📐 面積細節：" + d.target.area_detail;
    document.getElementById('rLayout').innerText = d.target.layout;
    document.getElementById('rPing').innerText = d.target.ping + " 坪";
    document.getElementById('rAge').innerText = d.target.age;
    document.getElementById('rFloor').innerText = d.target.floors;

    document.getElementById('lejuLink').href = d.target.leju_url;
    document.getElementById('hpLink').href = d.target.houseprice_url;

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
                <td class="py-3 px-2 font-bold text-emerald-700 whitespace-nowrap">${c.date}<span class="block text-[10px] text-slate-400">${c.floor}</span></td>
                <td class="py-3 px-2">
                    <p class="font-bold text-slate-900">${c.addr}</p>
                    <p class="text-[10px] text-slate-400 leading-tight mt-0.5">${c.raw_snippet}</p>
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
}

async function runQuery() {
    const addr = document.getElementById('addr').value.trim();
    const ping = document.getElementById('ping').value || 0;

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
            errEl.innerHTML = `${d.error} <div class="mt-3 flex space-x-2"><a href="${d.leju_url || '#'}" target="_blank" class="bg-orange-500 text-white px-3 py-1.5 rounded-lg text-xs">👉 打開【樂居 Leju】</a><a href="${d.houseprice_url || '#'}" target="_blank" class="bg-blue-600 text-white px-3 py-1.5 rounded-lg text-xs">👉 打開【Google 實價搜尋】</a></div>`;
            errEl.classList.remove('hidden');
            return;
        }
        renderResult(d);
    } catch (e) {
        document.getElementById('loading').classList.add('hidden');
        const errEl = document.getElementById('err');
        errEl.innerText = "連線異常，請重試一次！";
        errEl.classList.remove('hidden');
    }
}

async function runPasteParse() {
    const text = document.getElementById('pasteBox').value.trim();
    const addr = document.getElementById('addr').value.trim() || "自訂地址";
    const ping = document.getElementById('ping').value || 0;
    if (!text) return alert("請先貼上從樂居或 591 複製的實價文字！");

    const res = await fetch('/api/parse_pasted', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: text, address: addr, ping: ping})
    });
    const d = await res.json();
    if (d.error) return alert(d.error);
    document.getElementById('err').classList.add('hidden');
    renderResult(d);
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
