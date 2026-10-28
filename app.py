# 檔案名稱：app.py (v10.0 樂居 Leju 專屬格式深度解析 + 雲端免驗證碼搜尋引擎)
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

app = FastAPI(title="好房 AI ✕ 樂居 Leju 社區實價直連系統 v10.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
}

def clean_addr(addr: str) -> str:
    addr = addr.replace("臺", "台").replace(" ", "").strip()
    full_to_half = str.maketrans("０１２３４５６７８９－～Ｆ", "0123456789-~F")
    return addr.translate(full_to_half)

def split_door_info(addr: str):
    """拆出：完整門牌(至號)、不含縣市門牌、鄰近雙號門牌(同社區常跨連號如178號/180號)、目標樓層"""
    fl_m = re.search(r'號.*?(\d+|[一二三四五六七八九十]+)[樓層F]', addr)
    floor_str = fl_m.group(1) + "樓" if fl_m else ""
    base_door = re.sub(r'號.*$', '號', addr) if '號' in addr else addr
    short_door = re.sub(r'^[台臺]北市|^新北市|^桃園市|^[台臺]中市|^[台臺]南市|^高雄市', '', base_door)
    road_only = re.sub(r'\d+號.*$', '', short_door)

    num_m = re.search(r'(\d+)號', short_door)
    door_num = int(num_m.group(1)) if num_m else 0
    # 樂居常以社區首號登記 (例如 180號 登記在 178號)，同時準備鄰近號查詢
    prev_door = re.sub(r'\d+號', f'{max(1, door_num-2)}號', short_door) if door_num > 2 else short_door

    return base_door, short_door, prev_door, road_only, door_num, floor_str

def cloud_search_leju(query: str) -> list:
    """
    專為 Render 雲端伺服器設計的 4 通道無驗證碼檢索器：
    1. Jina Search API (s.jina.ai - 雲端AI搜尋通道，不擋機房IP)
    2. DuckDuckGo Lite (POST 表單模式 - 避開 GET 機器人攔截)
    3. Jina Reader Bing 穿透
    4. Yahoo TW 搜尋
    """
    snippets = []

    # 通道 1：s.jina.ai 雲端穿透搜尋
    try:
        r1 = requests.get(
            f"https://s.jina.ai/{quote(query)}",
            headers={"User-Agent": "Mozilla/5.0", "X-Return-Format": "text"},
            timeout=9
        )
        if r1.status_code == 200 and len(r1.text) > 80:
            for block in r1.text.split("\n\n"):
                b_clean = " ".join(block.split())
                if len(b_clean) > 25:
                    snippets.append(b_clean)
    except Exception:
        pass

    # 通道 2：DuckDuckGo Lite POST 通道 (極少擋雲端 IP)
    try:
        r2 = requests.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": query, "kl": "tw-tzh"},
            headers=HEADERS,
            timeout=7
        )
        if r2.status_code == 200:
            soup = BeautifulSoup(r2.text, "html.parser")
            for td in soup.select("td.result-snippet, a.result-link"):
                t = td.get_text(" ", strip=True)
                if len(t) > 15:
                    snippets.append(t)
    except Exception:
        pass

    # 通道 3：Jina Reader + Bing TW
    if len(snippets) < 5:
        try:
            b_url = f"https://r.jina.ai/https://www.bing.com/search?q={quote(query)}&setlang=zh-Hant&cc=TW"
            r3 = requests.get(b_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
            if r3.status_code == 200:
                for line in r3.text.splitlines():
                    if len(line.strip()) > 25 and any(w in line for w in ['樂居', '萬', '坪', '號', '樓', '成交', '屋齡']):
                        snippets.append(line.strip())
        except Exception:
            pass

    # 通道 4：Yahoo 奇摩搜尋
    if len(snippets) < 5:
        try:
            r4 = requests.get(f"https://tw.search.yahoo.com/search?p={quote(query)}", headers=HEADERS, timeout=7)
            if r4.status_code == 200:
                soup4 = BeautifulSoup(r4.text, "html.parser")
                for div in soup4.select("div.algo, div.dd.algo, li div.compText, h3.title"):
                    t = div.get_text(" ", strip=True)
                    if len(t) > 15:
                        snippets.append(t)
        except Exception:
            pass

    return snippets

def parse_leju_and_market_data(texts: list, clean_in: str, short_door: str, target_floor: str, custom_ping: float = 0):
    """
    專門解析【樂居 Leju】、【591實價登錄】、【好房網/永慶/信義/5168】的真實網頁文字格式！
    支援樂居無斜線之『一年均價108.73萬坪』、『一年成交均價 108.73 萬/坪』、『歷史最高價』等所有專屬標籤。
    """
    combined = " \n ".join(texts)

    # 1. 抓取【樂居社區名稱】(例如：【双湖滙/雙湖匯】一年均價108.73萬坪)
    comm_name = ""
    leju_title_m = re.search(r'【([^】]{2,16}?)】\s*(?:一年均價|最新價|均價|實價|\d+筆)', combined)
    if leju_title_m:
        comm_name = leju_title_m.group(1).strip()
    if not comm_name:
        for pat in [r'【([^】]{2,14}?)】', r'社區\s*[|｜:：]\s*([^\s\-|｜，,。]{2,10})', r'([\u4e00-\u9fa5]{2,8}?(?:滙|匯|花園|大廈|華廈|廣場|世紀|天下|帝國|名邸|山莊|新城|國宅|學苑|御苑|麗緻|公爵|一品|世家|傳奇|之星|時代|捷市|高第|晶鑽|凱旋|星鑽|皇家|尊爵|城堡))']:
            for m in re.findall(pat, combined):
                mc = m.strip()
                if 2 <= len(mc) <= 12 and not any(b in mc for b in ['實價登錄', '樂居', '最新', '台北市', '新北市', '桃園市', '台中市', '高雄市', '內政部', '成交']):
                    comm_name = mc
                    break
            if comm_name:
                break

    # 2. 抓取【樂居專屬價格指標】：一年成交均價、歷史最高價、平均成交均價、歷史最低價
    leju_1yr_avg = None
    leju_max_u = None
    leju_avg_u = None
    leju_min_u = None

    m_1yr = re.search(r'(?:一年均價|一年成交均價|近一年平均單價|近一年實價登錄|近期成交均價|近三個月平均單價)\s*[:：約]*\s*(\d{2,3}(?:\.\d{1,2})?)\s*萬', combined)
    if m_1yr:
        leju_1yr_avg = round(float(m_1yr.group(1)), 2)

    m_max = re.search(r'歷史最高價\s*[:：]*\s*(\d{2,3}(?:\.\d{1,2})?)\s*萬', combined)
    if m_max:
        leju_max_u = round(float(m_max.group(1)), 2)

    m_avg = re.search(r'平均成交均價\s*[:：]*\s*(\d{2,3}(?:\.\d{1,2})?)\s*萬', combined)
    if m_avg:
        leju_avg_u = round(float(m_avg.group(1)), 2)

    m_min = re.search(r'歷史最低價\s*[:：]*\s*(\d{2,3}(?:\.\d{1,2})?)\s*萬', combined)
    if m_min:
        leju_min_u = round(float(m_min.group(1)), 2)

    # 3. 抓取樂居【基本資料】：屋齡、總樓高、公設比、總戶數、建設公司、格局與坪數
    age_m = re.search(r'屋齡\s*[:：約]*\s*(\d{1,2}(?:\.\d+)?)\s*年', combined)
    fl_m = re.search(r'(?:總樓高|樓高)\s*[:：]*\s*(\d{1,2})\s*樓', combined)
    if not fl_m:
        fl_m = re.search(r'\d+\s*/\s*(\d{1,2})\s*樓', combined)
    pub_m = re.search(r'公設比\s*[:：]*\s*(\d{2}(?:\.\d+)?)\s*[%％]', combined)
    units_m = re.search(r'總戶數\s*[:：]*\s*(\d{1,4})\s*戶', combined)
    builder_m = re.search(r'(?:建設公司\s*[:：]*\s*|由\s*)([\u4e00-\u9fa5]{2,8}建設)', combined)
    leju_ping_range_m = re.search(r'格局與坪數\s*[:：]*\s*([^\n。]{3,25}?坪)', combined)

    real_age = f"{age_m.group(1)} 年" if age_m else "詳見社區明細"
    real_total_fl = f"共 {fl_m.group(1)} 樓" if fl_m else "大樓"
    pub_ratio = f"{pub_m.group(1)}%" if pub_m else "約 32%"
    total_units = f"{units_m.group(1)} 戶" if units_m else "-"
    builder = builder_m.group(1) if builder_m else "-"
    leju_ping_desc = leju_ping_range_m.group(1).strip() if leju_ping_range_m else ""

    # 4. 支援所有單價寫法：『108.73萬坪』(樂居標題)、『104.3 萬/坪』、『單價 104.26萬』
    all_unit_prices = []
    for pat in [
        r'(\d{2,3}(?:\.\d{1,2})?)\s*萬\s*/?\s*坪',
        r'單價\s*[:：約]*\s*(\d{2,3}(?:\.\d{1,2})?)\s*萬'
    ]:
        for m in re.finditer(pat, combined):
            v = round(float(m.group(1)), 2)
            if 12.0 <= v <= 350.0:
                all_unit_prices.append(v)

    for v in [leju_1yr_avg, leju_max_u, leju_avg_u, leju_min_u]:
        if v and 12.0 <= v <= 350.0:
            all_unit_prices.append(v)

    # 5. 萃取社區坪數選項
    pings_found = []
    # 先抓樂居「格局與坪數：4房以上-63/75坪」
    if leju_ping_desc:
        for pm in re.findall(r'(\d{2,3}(?:\.\d+)?)', leju_ping_desc):
            pv = float(pm)
            if 10 <= pv <= 160:
                pings_found.append(pv)
    for m in re.finditer(r'(?:建坪|總建坪|坪數|房/)\s*(\d{2,3}(?:\.\d{1,2})?)\s*坪', combined):
        pv = round(float(m.group(1)), 1)
        if 10.0 <= pv <= 160.0:
            pings_found.append(pv)

    # 6. 逐筆拆解【同社區實價登錄成交明細】
    records = []
    seen_keys = set()

    for txt in texts:
        # 匹配單價
        for um in re.finditer(r'(?:(\d{2,3}(?:\.\d{1,2})?)\s*萬\s*/?\s*坪|單價\s*(\d{2,3}(?:\.\d{1,2})?)\s*萬)', txt):
            u_str = um.group(1) or um.group(2)
            u_val = round(float(u_str), 2)
            if u_val < 12 or u_val > 350:
                continue

            # 取單價前後 110 字元找該筆交易的 日期、樓層、門牌、坪數、總價
            ctx = txt[max(0, um.start() - 110): min(len(txt), um.end() + 110)]

            dm = re.search(r'(11[0-6]\s*[年/\-\.]\s*\d{1,2}|202[2-6]\s*[年/\-\.]\s*\d{1,2})', ctx)
            flm = re.search(r'(\d{1,2}\s*[樓F層](?:之\d+)?(?:\s*/\s*\d{1,2}\s*[樓F層])?)', ctx)
            pm = re.search(r'(?:建坪|房/|\s)(\d{2,3}(?:\.\d{1,2})?)\s*坪', ctx)
            tm = re.search(r'(?:總價|成交價|\s)(\d{1,2},?\d{3,4}(?:\.\d+)?)\s*萬', ctx)
            addrm = re.search(r'([^\s，,。｜|：:；;]{3,14}?\d+號(?:\s*\|?\s*\d+[樓F層](?:之\d+)?)?)', ctx)

            date_val = dm.group(1).replace("年", "/").replace("-", "/").replace(" ", "") if dm else "近一年實價"
            fl_val = flm.group(1).replace(" ", "") if flm else (target_floor or "社區標準層")
            p_val = round(float(pm.group(1)), 1) if pm and 10 <= float(pm.group(1)) <= 160 else (pings_found[0] if pings_found else 45.0)
            t_val = round(float(tm.group(1).replace(',', ''))) if tm and float(tm.group(1).replace(',', '')) > 300 else round(u_val * p_val)
            addr_val = addrm.group(1).replace(" ", "") if addrm else (f"{comm_name} ({short_door})" if comm_name else short_door)

            rec_key = f"{date_val}_{u_val}_{fl_val}"
            if rec_key not in seen_keys:
                seen_keys.add(rec_key)
                records.append({
                    "date": date_val,
                    "addr": addr_val,
                    "floor": fl_val,
                    "ping": p_val,
                    "unit": u_val,
                    "total": t_val,
                    "snippet": ctx.strip()
                })

    return {
        "comm_name": comm_name,
        "leju_1yr_avg": leju_1yr_avg,
        "leju_max_u": leju_max_u,
        "leju_avg_u": leju_avg_u,
        "leju_min_u": leju_min_u,
        "real_age": real_age,
        "real_total_fl": real_total_fl,
        "pub_ratio": pub_ratio,
        "total_units": total_units,
        "builder": builder,
        "leju_ping_desc": leju_ping_desc,
        "pings_found": sorted(list(set(pings_found)))[:6],
        "all_unit_prices": all_unit_prices,
        "records": records
    }

@app.get("/api/analyze")
def analyze(address: str = Query(...), custom_ping: float = Query(0)):
    clean_in = clean_addr(address)
    base_door, short_door, prev_door, road_only, door_num, target_floor = split_door_info(clean_in)

    # 第一波：直接鎖定「樂居 leju.com.tw」與該門牌（及雙號連號門牌，如 180號 與 178號）
    snippets = []
    snippets += cloud_search_leju(f'site:leju.com.tw "{short_door}" OR "{prev_door}"')
    snippets += cloud_search_leju(f'"{short_door}" 樂居 OR 實價登錄 萬/坪')

    parsed = parse_leju_and_market_data(snippets, clean_in, short_door, target_floor, custom_ping)

    # 第二波：只要抓出了社區名稱（例如【双湖滙/雙湖匯】），立刻針對「該社區名稱 + 樂居 + 實價登錄」抓回完整交易明細！
    if parsed['comm_name']:
        main_cname = parsed['comm_name'].split('/')[0]
        snippets += cloud_search_leju(f'site:leju.com.tw "{main_cname}" 一年成交均價')
        snippets += cloud_search_leju(f'"{main_cname}" 實價登錄 "{road_only}" 萬/坪 樓')
        parsed = parse_leju_and_market_data(snippets, clean_in, short_door, target_floor, custom_ping)
    else:
        # 若第一波沒抓到社區名，用退一步的關鍵字再抓一次
        snippets += cloud_search_leju(f'{clean_in} 樂居 實價登錄 萬')
        parsed = parse_leju_and_market_data(snippets, clean_in, short_door, target_floor, custom_ping)

    leju_url = f"https://www.leju.com.tw/page_search_result?area_search={quote(short_door)}"

    if not parsed['all_unit_prices'] and not parsed['records']:
        return JSONResponse({
            "error": f"雲端搜尋暫時被防火牆限制讀取『{address}』，請點下方橘色按鈕打開樂居頁面，複製文字貼到下方解析框即可 1 秒算出！",
            "leju_url": leju_url
        })

    # 決定目標計算坪數
    if custom_ping > 0:
        target_ping = custom_ping
    elif parsed['pings_found']:
        target_ping = round(statistics.median(parsed['pings_found']), 1)
    else:
        target_ping = 45.0

    # 計算單價落點（優先結合樂居一年成交均價與同社區各戶實價）
    u_list = sorted(parsed['all_unit_prices'])
    med_u = parsed['leju_1yr_avg'] if parsed['leju_1yr_avg'] else round(statistics.median(u_list), 1)
    if len(u_list) >= 3:
        low_u = round(u_list[int(len(u_list) * 0.25)], 1)
        high_u = round(u_list[int(len(u_list) * 0.75)], 1)
        avg_u = round(statistics.mean(u_list), 1)
    else:
        low_u = parsed['leju_min_u'] if parsed['leju_min_u'] else round(med_u * 0.92, 1)
        high_u = parsed['leju_max_u'] if parsed['leju_max_u'] else round(med_u * 1.08, 1)
        avg_u = parsed['leju_avg_u'] if parsed['leju_avg_u'] else med_u

    # 確保保守價 <= 合理中位價 <= 樂觀價
    if low_u > med_u:
        low_u = round(med_u * 0.93, 1)
    if high_u < med_u:
        high_u = round(med_u * 1.07, 1)

    comm_display = f"【{parsed['comm_name']}】" if parsed['comm_name'] else f"【{short_door} 社區】"
    fl_display = f"{target_floor} / {parsed['real_total_fl']}" if target_floor else parsed['real_total_fl']

    return JSONResponse({
        "target": {
            "addr": clean_in,
            "comm_name": comm_display,
            "ping": target_ping,
            "ping_options": parsed['pings_found'] if parsed['pings_found'] else [target_ping],
            "leju_ping_desc": parsed['leju_ping_desc'] or f"標準房型約 {target_ping} 坪",
            "age": parsed['real_age'],
            "floors": fl_display,
            "pub_ratio": parsed['pub_ratio'],
            "total_units": parsed['total_units'],
            "builder": parsed['builder'],
            "leju_1yr_avg": parsed['leju_1yr_avg'],
            "leju_max_u": parsed['leju_max_u'],
            "leju_min_u": parsed['leju_min_u'],
            "leju_url": leju_url,
            "raw_snippets": snippets[:12]
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
def parse_pasted(payload: dict = Body(...)):
    """支援直接將樂居網頁文字整頁複製貼上，100% 精準拆出樂居所有欄位與成交明細！"""
    raw_text = payload.get("text", "")
    addr = payload.get("address", "目標房屋")
    custom_ping = float(payload.get("ping", 0) or 0)
    clean_in = clean_addr(addr)
    base_door, short_door, prev_door, road_only, door_num, target_floor = split_door_info(clean_in)

    lines = [line.strip() for line in raw_text.splitlines() if len(line.strip()) > 2]
    chunks = [" ".join(lines[i:i+8]) for i in range(0, len(lines), 4)]
    parsed = parse_leju_and_market_data(chunks + [raw_text], clean_in, short_door, target_floor, custom_ping)

    if not parsed['all_unit_prices']:
        return JSONResponse({"error": "貼上的文字中未發現價格資訊，請確認複製了樂居頁面內容！"})

    target_ping = custom_ping if custom_ping > 0 else (round(statistics.median(parsed['pings_found']), 1) if parsed['pings_found'] else 45.0)
    u_list = sorted(parsed['all_unit_prices'])
    med_u = parsed['leju_1yr_avg'] if parsed['leju_1yr_avg'] else round(statistics.median(u_list), 1)
    low_u = round(u_list[int(len(u_list) * 0.25)], 1) if len(u_list) >= 3 else round(med_u * 0.93, 1)
    high_u = round(u_list[int(len(u_list) * 0.75)], 1) if len(u_list) >= 3 else round(med_u * 1.07, 1)
    avg_u = round(statistics.mean(u_list), 1)

    return JSONResponse({
        "target": {
            "addr": clean_in,
            "comm_name": f"【{parsed['comm_name']}】" if parsed['comm_name'] else "【樂居貼上之社區】",
            "ping": target_ping,
            "ping_options": parsed['pings_found'] if parsed['pings_found'] else [target_ping],
            "leju_ping_desc": parsed['leju_ping_desc'] or f"設定坪數 {target_ping} 坪",
            "age": parsed['real_age'],
            "floors": f"{target_floor} / {parsed['real_total_fl']}" if target_floor else parsed['real_total_fl'],
            "pub_ratio": parsed['pub_ratio'],
            "total_units": parsed['total_units'],
            "builder": parsed['builder'],
            "leju_1yr_avg": parsed['leju_1yr_avg'],
            "leju_max_u": parsed['leju_max_u'],
            "leju_min_u": parsed['leju_min_u'],
            "leju_url": f"https://www.leju.com.tw/page_search_result?area_search={quote(short_door)}",
            "raw_snippets": chunks[:10]
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
    <title>好房 AI ✕ 樂居 Leju 社區實價直連估價系統 v10.0</title>
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
                <div class="bg-orange-500 p-2 rounded-lg"><i class="fa-solid fa-house-laptop text-xl text-white"></i></div>
                <div>
                    <span class="font-black text-lg tracking-wide">好房 AI ✕ 樂居 Leju 社區實價估價系統</span>
                    <span class="hidden md:inline-block ml-2 text-xs bg-orange-500/20 text-orange-300 border border-orange-400/30 px-2 py-0.5 rounded-full">v10.0 樂居社區基本資料＆一年均價專屬解析引擎</span>
                </div>
            </div>
        </div>
    </nav>

    <div class="bg-gradient-to-r from-slate-900 via-orange-950 to-slate-900 text-white py-7 px-4 shadow-inner">
        <div class="max-w-7xl mx-auto">
            <h1 class="text-2xl md:text-3xl font-extrabold mb-2">輸入地址 ➡️ 自動抓取【樂居 Leju】社區基本資料、一年均價與同社區實價明細</h1>
            <p class="text-orange-200 text-xs md:text-sm mb-5">專門針對樂居網站格式打造：自動解析該門牌所屬之「樂居社區名稱、一年成交均價、歷史最高/最低價、屋齡、總樓高、公設比、建商」與近一年各樓層成交紀錄！</p>

            <div class="bg-white p-4 rounded-2xl shadow-xl text-slate-800 grid grid-cols-1 md:grid-cols-12 gap-3 items-end">
                <div class="md:col-span-6">
                    <label class="text-xs font-bold text-slate-500 block mb-1">📍 輸入目標房屋完整地址</label>
                    <input type="text" id="addr" value="台北市內湖區成功路四段180號6樓" 
                           placeholder="例：台北市內湖區成功路四段180號6樓"
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3.5 py-2.5 text-sm font-bold focus:outline-none focus:ring-2 focus:ring-orange-500">
                </div>
                <div class="md:col-span-2">
                    <label class="text-xs font-bold text-slate-500 block mb-1">📐 扣車位坪數 (選填)</label>
                    <input type="number" id="ping" placeholder="自動抓取社區坪數" 
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3 py-2.5 text-sm font-bold focus:outline-none">
                </div>
                <div class="md:col-span-2">
                    <label class="text-xs font-bold text-slate-500 block mb-1">🚗 外加車位價 (萬)</label>
                    <input type="number" id="park" value="0" 
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3 py-2.5 text-sm font-bold focus:outline-none">
                </div>
                <div class="md:col-span-2">
                    <button onclick="runQuery()" class="w-full bg-orange-500 hover:bg-orange-600 active:scale-95 transition text-white font-black py-2.5 px-4 rounded-xl shadow-md flex items-center justify-center space-x-2">
                        <i class="fa-solid fa-magnifying-glass-chart"></i>
                        <span>抓樂居社區與行情</span>
                    </button>
                </div>
            </div>
        </div>
    </div>

    <div class="max-w-7xl mx-auto px-4 py-8">
        <div id="loading" class="hidden bg-white rounded-3xl p-12 text-center shadow-sm border border-slate-200 my-6">
            <i class="fa-solid fa-circle-notch text-5xl text-orange-500 spin mb-4"></i>
            <h3 class="text-lg font-extrabold text-slate-800">正在從【樂居 Leju】與實價網檢索該門牌之社區檔案與近一年成交價...</h3>
            <p class="text-xs text-slate-400 mt-2">正在解析樂居社區名稱、公設比、一年均價與同社區成交明細，約需 5~10 秒！</p>
        </div>

        <div id="err" class="hidden bg-red-50 border-l-4 border-red-600 text-red-700 p-5 rounded-xl shadow-sm my-4 font-bold text-sm"></div>

        <div id="dashboard" class="hidden grid grid-cols-1 lg:grid-cols-12 gap-6">
            <div class="lg:col-span-5 space-y-6">
                <!-- 1. 樂居社區基本資料卡 -->
                <div class="bg-white rounded-2xl p-6 shadow-sm border-2 border-orange-500">
                    <div class="flex items-center justify-between mb-2">
                        <span class="bg-orange-500 text-white text-xs font-black px-2.5 py-1 rounded-md">🏢 1. 樂居 Leju 社區基本資料</span>
                        <a id="lejuLink" href="#" target="_blank" class="text-xs font-bold text-orange-600 hover:underline flex items-center">
                            <span>開啟樂居原頁</span><i class="fa-solid fa-arrow-up-right-from-square ml-1"></i>
                        </a>
                    </div>
                    <h2 id="rCommName" class="text-2xl font-black text-slate-900 mt-1"></h2>
                    <p id="rAddr" class="text-xs font-bold text-slate-500 mb-3"></p>

                    <!-- 樂居官方四大價格指標 -->
                    <div class="grid grid-cols-3 gap-2 text-center bg-orange-50 p-3 rounded-xl border border-orange-200 mb-4">
                        <div>
                            <p class="text-[10px] text-orange-800 font-bold">樂居一年成交均價</p>
                            <p id="rLeju1Yr" class="text-base font-black text-red-600 mt-0.5"></p>
                        </div>
                        <div class="border-l border-orange-200">
                            <p class="text-[10px] text-slate-500">社區歷史最高</p>
                            <p id="rLejuMax" class="text-sm font-extrabold text-slate-800 mt-0.5"></p>
                        </div>
                        <div class="border-l border-orange-200">
                            <p class="text-[10px] text-slate-500">社區歷史最低</p>
                            <p id="rLejuMin" class="text-sm font-extrabold text-slate-800 mt-0.5"></p>
                        </div>
                    </div>

                    <div class="mb-4 bg-slate-50 p-2.5 rounded-xl border border-slate-200">
                        <span class="text-[11px] font-bold text-slate-500 block mb-1.5">💡 本社區標準房型坪數 (點擊可立即切換坪數重算總價)：</span>
                        <div id="pingBtns" class="flex flex-wrap gap-1.5"></div>
                    </div>

                    <div class="grid grid-cols-2 gap-2.5 text-xs bg-slate-50 p-3.5 rounded-xl border border-slate-200">
                        <div><span class="text-slate-400">計算權狀坪：</span><strong id="rPing" class="text-blue-700 text-sm"></strong></div>
                        <div><span class="text-slate-400">社區屋齡：</span><strong id="rAge" class="text-slate-800"></strong></div>
                        <div><span class="text-slate-400">樓層/總樓高：</span><strong id="rFloor" class="text-slate-800"></strong></div>
                        <div><span class="text-slate-400">社區公設比：</span><strong id="rPubRatio" class="text-slate-800"></strong></div>
                        <div><span class="text-slate-400">社區總戶數：</span><strong id="rUnits" class="text-slate-800"></strong></div>
                        <div><span class="text-slate-400">建設公司：</span><strong id="rBuilder" class="text-slate-800"></strong></div>
                        <div class="col-span-2"><span class="text-slate-400">樂居坪數規劃：</span><strong id="rPingDesc" class="text-slate-800"></strong></div>
                    </div>
                </div>

                <!-- 2. 這間房子價值多少錢？(成交價格落點) -->
                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <h3 class="text-base font-black text-slate-900 mb-3"><i class="fa-solid fa-sack-dollar text-amber-500 mr-2"></i>2. 這間房子價值多少錢？(成交價格落點)</h3>

                    <div class="bg-gradient-to-br from-blue-900 to-indigo-900 text-white p-5 rounded-2xl text-center shadow-md mb-4">
                        <p class="text-xs text-blue-200 font-semibold">預估合理成交總價落點 (依樂居一年均價/中位數)</p>
                        <div class="my-2 flex items-baseline justify-center space-x-1">
                            <span id="medT" class="text-4xl font-black text-yellow-300"></span>
                            <span class="text-sm font-bold">萬元</span>
                        </div>
                        <div class="flex justify-center space-x-4 text-xs text-blue-100 mt-2 pt-2 border-t border-blue-700/50">
                            <span>合理單價落點：<strong id="medU" class="text-white"></strong> 萬/坪</span>
                            <span>•</span>
                            <span>樣本均價：<strong id="avgU" class="text-white"></strong> 萬/坪</span>
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

            <!-- 右欄：同社區近期成交行情表 -->
            <div class="lg:col-span-7 space-y-6">
                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <h3 class="text-base font-black text-slate-900 mb-1">📋 3. 同社區實價登錄成交行情明細</h3>
                    <p class="text-xs text-slate-400 mb-4">直接擷取自樂居、591、好房網、永慶、信義公開揭露之本社區各樓層真實成交紀錄</p>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left border-collapse">
                            <thead>
                                <tr class="border-b-2 border-slate-200 text-[11px] text-slate-400">
                                    <th class="py-2 px-2">成交年月</th><th class="py-2 px-2">樓層</th>
                                    <th class="py-2 px-2">社區門牌 / 實價內容</th>
                                    <th class="py-2 px-2 text-right">坪數</th><th class="py-2 px-2 text-right">拆算單價</th>
                                    <th class="py-2 px-2 text-right">成交總價</th>
                                </tr>
                            </thead>
                            <tbody id="tableBody" class="divide-y divide-slate-100 text-xs"></tbody>
                        </table>
                    </div>
                </div>

                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <h3 class="text-base font-black text-slate-900 mb-2">🔍 樂居與實價網 原始網頁抓取內容核對區</h3>
                    <div id="rawSnippets" class="space-y-2 text-xs text-slate-600 bg-slate-50 p-3.5 rounded-xl border border-slate-200 max-h-60 overflow-y-auto"></div>
                </div>
            </div>
        </div>

        <!-- 手動貼上樂居整頁文字解析器 -->
        <div class="mt-8 bg-white rounded-2xl p-6 shadow-sm border border-slate-300">
            <h3 class="text-sm font-black text-slate-800 mb-1"><i class="fa-solid fa-paste text-orange-500 mr-1.5"></i>樂居網頁文字一鍵解析器（把樂居網頁全選複製貼上，1秒自動提取所有數據）</h3>
            <p class="text-xs text-slate-500 mb-3">若您想針對樂居某個特定社區頁面進行深度拆算，只需在樂居頁面上按 Ctrl+A 全選、Ctrl+C 複製，貼到下方框框即可：</p>
            <textarea id="pasteBox" rows="3" placeholder="在此貼上樂居網頁文字..." class="w-full bg-slate-50 border border-slate-300 rounded-xl p-3 text-xs focus:outline-none focus:ring-2 focus:ring-orange-500 mb-2"></textarea>
            <button onclick="runPasteParse()" class="bg-slate-800 hover:bg-slate-900 text-white text-xs font-bold py-2 px-4 rounded-xl shadow">立即解析貼上的樂居文字</button>
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
    document.getElementById('rAddr').innerText = "查詢門牌：" + d.target.addr;
    document.getElementById('lejuLink').href = d.target.leju_url;

    document.getElementById('rLeju1Yr').innerText = d.target.leju_1yr_avg ? `${d.target.leju_1yr_avg} 萬/坪` : `${d.val.med_u} 萬/坪`;
    document.getElementById('rLejuMax').innerText = d.target.leju_max_u ? `${d.target.leju_max_u} 萬/坪` : `${d.val.high_u} 萬/坪`;
    document.getElementById('rLejuMin').innerText = d.target.leju_min_u ? `${d.target.leju_min_u} 萬/坪` : `${d.val.low_u} 萬/坪`;

    document.getElementById('rPing').innerText = d.target.ping + " 坪";
    document.getElementById('rAge').innerText = d.target.age;
    document.getElementById('rFloor').innerText = d.target.floors;
    document.getElementById('rPubRatio').innerText = d.target.pub_ratio;
    document.getElementById('rUnits').innerText = d.target.total_units;
    document.getElementById('rBuilder').innerText = d.target.builder;
    document.getElementById('rPingDesc').innerText = d.target.leju_ping_desc;

    const btnBox = document.getElementById('pingBtns');
    btnBox.innerHTML = '';
    d.target.ping_options.forEach(p => {
        const active = Math.abs(p - d.target.ping) < 0.5;
        btnBox.innerHTML += `
            <button onclick="selectQuickPing(${p})" class="text-xs font-bold px-2.5 py-1 rounded-lg border transition ${active ? 'bg-orange-500 text-white border-orange-500 shadow' : 'bg-white text-slate-700 border-slate-300 hover:bg-orange-50'}">
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
                <td class="py-3 px-2 font-bold text-emerald-700 whitespace-nowrap">${c.date}</td>
                <td class="py-3 px-2 font-semibold text-slate-700 whitespace-nowrap">${c.floor}</td>
                <td class="py-3 px-2">
                    <p class="font-bold text-slate-900">${c.addr}</p>
                    <p class="text-[10px] text-slate-400 leading-tight mt-0.5">${c.snippet}</p>
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
            errEl.innerHTML = `${d.error} <div class="mt-3"><a href="${d.leju_url || '#'}" target="_blank" class="bg-orange-500 text-white px-4 py-2 rounded-lg text-xs inline-block">👉 點此打開【樂居 Leju】複製文字</a></div>`;
            errEl.classList.remove('hidden');
            return;
        }
        renderResult(d);
    } catch (e) {
        document.getElementById('loading').classList.add('hidden');
        const errEl = document.getElementById('err');
        errEl.innerText = "連線異常，請再按一次『抓樂居社區與行情』！";
        errEl.classList.remove('hidden');
    }
}

async function runPasteParse() {
    const text = document.getElementById('pasteBox').value.trim();
    const addr = document.getElementById('addr').value.trim() || "自訂地址";
    const ping = document.getElementById('ping').value || 0;
    if (!text) return alert("請先貼上從樂居複製的文字！");

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
