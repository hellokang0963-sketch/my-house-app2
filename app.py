# 檔案名稱：app.py (智慧欄位自動匹配 + 雲端省記憶體最終版)
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import pandas as pd
import requests
import urllib3
import zipfile
import io
import re
import gc
from datetime import datetime

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(title="好房 AI 實價登錄估價網站")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CITY_CODE_MAP = {
    "臺北市": "a", "台北市": "a", "臺中市": "b", "台中市": "b",
    "基隆市": "c", "臺南市": "d", "台南市": "d", "高雄市": "e",
    "新北市": "f", "宜蘭縣": "g", "桃園市": "h", "嘉義市": "i",
    "新竹縣": "j", "苗栗縣": "k", "南投縣": "m", "彰化縣": "n",
    "新竹市": "o", "雲林縣": "p", "嘉義縣": "q", "屏東縣": "t",
    "花蓮縣": "u", "臺東縣": "v", "台東縣": "v", "澎湖縣": "x",
    "金門縣": "w", "連江縣": "z"
}

CITY_DATA_CACHE = {}

def normalize_address(addr: str) -> str:
    if not isinstance(addr, str): return ""
    addr = addr.replace("台", "臺").strip()
    full_to_half = str.maketrans("０１２３４５６７８９", "0123456789")
    addr = addr.translate(full_to_half)
    ch_num = {'一':'1','二':'2','三':'3','四':'4','五':'5','六':'6','七':'7','八':'8','九':'9','十':'10'}
    for k, v in ch_num.items():
        addr = re.sub(f"{k}(段|樓|層)", f"{v}\\1", addr)
    return addr

def parse_roc_date(date_str: str) -> int:
    if not isinstance(date_str, str) or len(date_str) < 6: return 0
    try:
        year = int(date_str[:-4]) + 1911
        month = int(date_str[-4:-2])
        return year * 12 + month
    except:
        return 0

def get_col(df: pd.DataFrame, keyword: str, default_val=""):
    """智慧尋找欄位：不管內政部有沒有加括號都能自動找到對應欄位"""
    for c in df.columns:
        if keyword in str(c):
            return df[c]
    return pd.Series([default_val] * len(df), index=df.index)

def fetch_moi_online_data(city_code: str) -> pd.DataFrame:
    if city_code in CITY_DATA_CACHE:
        return CITY_DATA_CACHE[city_code]

    CITY_DATA_CACHE.clear()
    gc.collect()

    dfs = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*"
    }
    target_filename = f"{city_code}_lvr_land_a.csv"

    urls_to_fetch = ["https://plvr.land.moi.gov.tw/Download?type=zip&fileName=lvr_landcsv.zip"]
    now = datetime.now()
    roc_year = now.year - 1911
    for y in [roc_year, roc_year - 1, roc_year - 2]:
        for s in [4, 3, 2, 1]:
            urls_to_fetch.append(f"https://plvr.land.moi.gov.tw/DownloadSeason?season={y}S{s}&type=zip&fileName=lvr_landcsv.zip")

    # 只要欄位名稱包含這些關鍵字就保留，完美相容有沒有括號的版本
    keep_keywords = ['鄉鎮市區', '交易標的', '門牌', '交易年月日', '移轉層次', '總樓層數', '建物型態', '建築完成', '建物移轉總面積', '格局-房', '格局-廳', '總價元', '車位移轉總面積', '車位總價元', '備註']

    success_seasons = 0
    for url in urls_to_fetch:
        if success_seasons >= 3:
            break
        try:
            resp = requests.get(url, headers=headers, timeout=25, verify=False)
            if resp.status_code == 200 and len(resp.content) > 5000:
                with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                    if target_filename in z.namelist():
                        with z.open(target_filename) as f:
                            sub_df = pd.read_csv(
                                f, dtype=str,
                                usecols=lambda c: any(k in str(c) for k in keep_keywords),
                                on_bad_lines='skip'
                            )
                            if len(sub_df) > 1:
                                sub_df = sub_df.iloc[1:].copy()
                                target_type_s = get_col(sub_df, '交易標的', '')
                                sub_df = sub_df[target_type_s.str.contains('房地', na=False)]
                                dfs.append(sub_df)
                                success_seasons += 1
            del resp
            gc.collect()
        except Exception as e:
            print(f"略過網址 {url}: {e}")
            continue

    if not dfs:
        return pd.DataFrame()

    raw_df = pd.concat(dfs, ignore_index=True)
    del dfs
    gc.collect()

    # 建立乾淨統一的標準化資料表
    df = pd.DataFrame()
    df['district'] = get_col(raw_df, '鄉鎮市區', '')
    df['clean_addr'] = get_col(raw_df, '門牌', '').astype(str).apply(normalize_address)
    df['date_str'] = get_col(raw_df, '交易年月日', '')
    df['floor'] = get_col(raw_df, '移轉層次', '-')
    df['total_floors'] = get_col(raw_df, '總樓層數', '-')
    df['b_type'] = get_col(raw_df, '建物型態', '住宅大樓')
    df['build_ym'] = get_col(raw_df, '建築完成', '')
    df['rooms'] = get_col(raw_df, '格局-房', '3')
    df['halls'] = get_col(raw_df, '格局-廳', '2')
    df['note'] = get_col(raw_df, '備註', '')

    df['total_ping'] = pd.to_numeric(get_col(raw_df, '建物移轉總面積', '0'), errors='coerce').fillna(0) * 0.3025
    df['parking_ping'] = pd.to_numeric(get_col(raw_df, '車位移轉總面積', '0'), errors='coerce').fillna(0) * 0.3025
    df['house_ping'] = df['total_ping'] - df['parking_ping']
    
    df['total_price'] = pd.to_numeric(raw_df['總價元'] if '總價元' in raw_df.columns else get_col(raw_df, '總價元', '0'), errors='coerce').fillna(0) / 10000
    df['parking_price'] = pd.to_numeric(get_col(raw_df, '車位總價元', '0'), errors='coerce').fillna(0) / 10000

    del raw_df
    gc.collect()

    # 當車位有計價但未登記車位坪數時，若房屋總坪數大於25坪，預設扣除標準車位坪數 8 坪以免稀釋單價
    no_park_ping_mask = (df['parking_price'] > 0) & (df['parking_ping'] == 0) & (df['total_ping'] > 25)
    df.loc[no_park_ping_mask, 'house_ping'] = df.loc[no_park_ping_mask, 'total_ping'] - 8.0

    has_park = (df['parking_price'] > 0) & (df['house_ping'] > 1)
    df['real_unit_price'] = 0.0
    valid_house = df['house_ping'] > 1
    df.loc[valid_house & has_park, 'real_unit_price'] = (df['total_price'] - df['parking_price']) / df['house_ping']
    df.loc[valid_house & (~has_park), 'real_unit_price'] = df['total_price'] / df['total_ping']

    df['month_val'] = df['date_str'].apply(parse_roc_date)

    df = df[~df['note'].astype(str).str.contains('親友|特殊|急買|急賣|瑕疵|增建|政府', na=False)]
    df = df[(df['real_unit_price'] > 5) & (df['real_unit_price'] < 400)]
    df = df.drop_duplicates(subset=['clean_addr', 'date_str', 'total_price'])

    CITY_DATA_CACHE[city_code] = df
    gc.collect()
    return df

@app.get("/api/analyze")
def analyze(address: str = Query(...), custom_ping: float = Query(0), range_ratio: float = Query(0.25)):
    try:
        clean_input = normalize_address(address)
        
        city_code = "a"
        for city_name, code in CITY_CODE_MAP.items():
            if city_name in clean_input or city_name.replace("臺", "台") in address:
                city_code = code
                break

        df = fetch_moi_online_data(city_code)
        if df.empty:
            return {"error": "內政部開放資料伺服器目前繁忙，請等待 10 秒後再按一次！"}

        match = re.search(r'([臺台]北市|新北市|桃園市|[臺台]中市|[臺台]南市|高雄市|.{2}[縣市])?(.+?[區鄉鎮市])?(.+?[路街段])(\d+巷)?(\d+弄)?(\d+號)', clean_input)
        if not match:
            return {"error": "地址格式無法辨識，請輸入包含『縣市、路街、號』的完整門牌，例如：台北市內湖區成功路四段180號6樓"}

        district = match.group(2) or ""
        road = match.group(3) or ""
        lane = match.group(4) or ""
        number = match.group(6) or ""
        
        num_val = int(re.sub(r'\D', '', number))
        nearby_nums = "|".join([f"{n}號" for n in range(max(1, num_val-6), num_val+7, 2)])
        same_community_pattern = f"{road}{lane}({nearby_nums})"
        exact_building_addr = f"{road}{lane}{number}"

        current_roc_year = datetime.now().year - 1911

        exact_matches = df[df['clean_addr'].str.contains(exact_building_addr, na=False, regex=False)]
        community_matches = df[df['clean_addr'].str.contains(same_community_pattern, na=False, regex=True)]
        target_source_df = exact_matches if not exact_matches.empty else community_matches

        if not target_source_df.empty:
            ref = target_source_df.iloc[0]
            target_ping = round(float(target_source_df['house_ping'].median()), 1) if custom_ping <= 0 else custom_ping
            build_ym = str(ref['build_ym'])
            build_y = int(build_ym[:-4]) if len(build_ym) >= 5 and build_ym[:-4].isdigit() else (current_roc_year - 15)
            target_age = max(1, current_roc_year - build_y)
            total_floors = str(ref['total_floors'])
            b_type = str(ref['b_type']).split('(')[0]
            layout = f"{ref['rooms']}房{ref['halls']}廳"
            source_msg = f"✅ 已從內政部實價庫自動配對同社區【{ref['clean_addr']}】之真實規格"
        else:
            road_matches = df[df['clean_addr'].str.contains(road, na=False, regex=False)]
            if road_matches.empty:
                return {"error": f"查無『{road}』之近期實價登錄，請確認縣市與路名是否正確。"}
            target_ping = round(float(road_matches['house_ping'].median()), 1) if custom_ping <= 0 else custom_ping
            target_age = 15
            total_floors = str(road_matches.iloc[0]['total_floors'])
            b_type = str(road_matches.iloc[0]['b_type']).split('(')[0]
            layout = "標準格局"
            source_msg = f"ℹ️ 該門牌近期無交易，已帶入『{road}』標準坪數（可於上方手動微調）"

        max_m = df['month_val'].max()
        min_ping = round(target_ping * (1 - range_ratio), 1)
        max_ping = round(target_ping * (1 + range_ratio), 1)

        comps = df[
            (df['clean_addr'].str.contains(road, na=False, regex=False)) &
            (df['month_val'] >= max_m - 12) &
            (df['house_ping'] >= min_ping) &
            (df['house_ping'] <= max_ping)
        ].copy()

        if len(comps) < 5 and district:
            comps = df[
                (df['district'] == district) &
                (df['b_type'].str.contains(b_type[:2], na=False, regex=False)) &
                (df['month_val'] >= max_m - 12) &
                (df['house_ping'] >= min_ping) &
                (df['house_ping'] <= max_ping)
            ].copy()

        if comps.empty:
            return {"error": f"查無周邊坪數介於 {min_ping}~{max_ping} 坪之近期交易，請將上方『坪數範圍』調大至 ±35%。"}

        comps['is_same'] = comps['clean_addr'].str.contains(same_community_pattern, na=False, regex=True)
        comps = comps.sort_values(by=['is_same', 'month_val'], ascending=[False, False])

        q = comps['real_unit_price'].quantile([0.25, 0.5, 0.75]).to_dict()
        low_u, med_u, high_u = round(q[0.25], 1), round(q[0.5], 1), round(q[0.75], 1)
        avg_u = round(float(comps['real_unit_price'].mean()), 1)

        comp_list = []
        for _, r in comps.head(30).iterrows():
            d_str = str(r['date_str'])
            f_date = f"{d_str[:-4]}/{d_str[-4:-2]}" if len(d_str) >= 5 else d_str
            comp_list.append({
                "date": f_date,
                "addr": r['clean_addr'],
                "ping": round(float(r['house_ping']), 1),
                "total_ping": round(float(r['total_ping']), 1),
                "floor": f"{r['floor']} / {r['total_floors']}",
                "unit": round(float(r['real_unit_price']), 1),
                "total": round(float(r['total_price']), 0),
                "park": round(float(r['parking_price']), 0),
                "layout": f"{r['rooms']}房{r['halls']}廳",
                "is_same": bool(r['is_same'])
            })

        return {
            "target": {
                "addr": clean_input, "district": district, "road": road,
                "ping": target_ping, "min_ping": min_ping, "max_ping": max_ping,
                "age": target_age, "floors": total_floors, "type": b_type,
                "layout": layout, "note": source_msg
            },
            "val": {
                "count": len(comps), "same_count": int(comps['is_same'].sum()),
                "avg_u": avg_u, "low_u": low_u, "med_u": med_u, "high_u": high_u,
                "low_t": round(low_u * target_ping),
                "med_t": round(med_u * target_ping),
                "high_t": round(high_u * target_ping)
            },
            "comps": comp_list
        }
    except Exception as e:
        return {"error": f"系統處理異常：{str(e)}"}

@app.get("/", response_class=HTMLResponse)
def web_portal():
    return """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>好房 AI 實價登錄智慧估價平台</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
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
                <div class="bg-blue-600 p-2 rounded-lg"><i class="fa-solid fa-building-wheat text-xl text-white"></i></div>
                <div>
                    <span class="font-black text-lg tracking-wide">好房 AI 實價估價網</span>
                    <span class="hidden md:inline-block ml-2 text-xs bg-emerald-500/20 text-emerald-300 border border-emerald-400/30 px-2 py-0.5 rounded-full">內政部實價登錄即時直連</span>
                </div>
            </div>
        </div>
    </nav>

    <div class="bg-gradient-to-r from-slate-900 via-blue-900 to-indigo-900 text-white py-8 px-4 shadow-inner">
        <div class="max-w-7xl mx-auto">
            <h1 class="text-2xl md:text-3xl font-extrabold mb-2">輸入門牌地址，即時產出不動產估價與落點報告</h1>
            <p class="text-blue-200 text-xs md:text-sm mb-6">自動檢索目標房屋地政規格、比對近一年周邊同坪數實價登錄，並推算買賣最佳成交落點。</p>

            <div class="bg-white p-4 rounded-2xl shadow-xl text-slate-800 grid grid-cols-1 md:grid-cols-12 gap-3 items-end">
                <div class="md:col-span-5">
                    <label class="text-xs font-bold text-slate-500 block mb-1">📍 目標房屋完整門牌地址</label>
                    <input type="text" id="addr" value="台北市內湖區成功路四段180號6樓" 
                           placeholder="例：新北市板橋區文化路一段188號10樓"
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3.5 py-2.5 text-sm font-bold focus:outline-none focus:ring-2 focus:ring-blue-600">
                </div>
                <div class="md:col-span-2">
                    <label class="text-xs font-bold text-slate-500 block mb-1">📐 主+公設坪數 (選填)</label>
                    <input type="number" id="ping" placeholder="自動抓取同棟坪數" 
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3 py-2.5 text-sm font-bold focus:outline-none">
                </div>
                <div class="md:col-span-2">
                    <label class="text-xs font-bold text-slate-500 block mb-1">🚗 外加車位預算 (萬)</label>
                    <input type="number" id="park" value="0" 
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3 py-2.5 text-sm font-bold focus:outline-none">
                </div>
                <div class="md:col-span-1">
                    <label class="text-xs font-bold text-slate-500 block mb-1">🔍 坪數範圍</label>
                    <select id="ratio" class="w-full bg-slate-50 border border-slate-300 rounded-xl px-2 py-2.5 text-sm font-bold focus:outline-none">
                        <option value="0.2">±20%</option>
                        <option value="0.25" selected>±25%</option>
                        <option value="0.35">±35%</option>
                    </select>
                </div>
                <div class="md:col-span-2">
                    <button onclick="runQuery()" class="w-full bg-blue-600 hover:bg-blue-700 active:scale-95 transition text-white font-bold py-2.5 px-4 rounded-xl shadow-md flex items-center justify-center space-x-2">
                        <i class="fa-solid fa-magnifying-glass-chart"></i>
                        <span>立即估價分析</span>
                    </button>
                </div>
            </div>
        </div>
    </div>

    <div class="max-w-7xl mx-auto px-4 py-8">
        <div id="loading" class="hidden bg-white rounded-3xl p-12 text-center shadow-sm border border-slate-200 my-6">
            <i class="fa-solid fa-circle-notch text-5xl text-blue-600 spin mb-4"></i>
            <h3 class="text-lg font-extrabold text-slate-800">正在從內政部伺服器下載最新實價登錄...</h3>
            <p class="text-xs text-slate-400 mt-2">首次載入該縣市約需 10~20 秒，完成後再次查詢僅需 0.1 秒，請稍候！</p>
        </div>

        <div id="err" class="hidden bg-red-50 border-l-4 border-red-500 text-red-700 p-4 rounded-xl shadow-sm my-4 font-bold text-sm"></div>

        <div id="dashboard" class="hidden grid grid-cols-1 lg:grid-cols-12 gap-6">
            <div class="lg:col-span-5 space-y-6">
                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <div class="flex items-center justify-between mb-3">
                        <span class="bg-blue-100 text-blue-800 text-xs font-extrabold px-2.5 py-1 rounded-md">目標房屋基本檔案</span>
                        <span id="rType" class="text-xs font-bold text-slate-500 bg-slate-100 px-2.5 py-1 rounded-md"></span>
                    </div>
                    <h2 id="rAddr" class="text-xl font-black text-slate-900 mb-2"></h2>
                    <p id="note" class="text-xs text-blue-600 font-semibold bg-blue-50 p-2.5 rounded-lg border border-blue-100 mb-4"></p>
                    <div class="grid grid-cols-4 gap-2 text-center bg-slate-50 p-3 rounded-xl border border-slate-100">
                        <div><p class="text-[11px] text-slate-400">扣車位坪數</p><p id="rPing" class="text-sm font-black text-blue-700 mt-0.5"></p></div>
                        <div class="border-l"><p class="text-[11px] text-slate-400">估算屋齡</p><p id="rAge" class="text-sm font-black text-slate-800 mt-0.5"></p></div>
                        <div class="border-l"><p class="text-[11px] text-slate-400">總樓層</p><p id="rFloor" class="text-sm font-black text-slate-800 mt-0.5"></p></div>
                        <div class="border-l"><p class="text-[11px] text-slate-400">參考格局</p><p id="rLayout" class="text-xs font-bold text-slate-800 mt-1"></p></div>
                    </div>
                </div>

                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <div class="flex items-center justify-between mb-4">
                        <h3 class="text-base font-black text-slate-900"><i class="fa-solid fa-bullseye text-red-500 mr-2"></i>成交價格落點分析</h3>
                        <span id="rCount" class="text-xs bg-indigo-100 text-indigo-800 font-bold px-2.5 py-1 rounded-full"></span>
                    </div>
                    <div class="bg-gradient-to-br from-blue-900 to-indigo-900 text-white p-5 rounded-2xl text-center shadow-md mb-5">
                        <p class="text-xs text-blue-200 font-semibold">市場最可能成交合理總價 (中位數 PR50)</p>
                        <div class="my-2 flex items-baseline justify-center space-x-1">
                            <span id="medT" class="text-4xl font-black text-yellow-300"></span>
                            <span class="text-sm font-bold">萬元</span>
                        </div>
                        <div class="flex justify-center space-x-4 text-xs text-blue-100 mt-2 pt-2 border-t border-blue-700/50">
                            <span>中位數單價：<strong id="medU" class="text-white"></strong> 萬/坪</span>
                            <span>•</span>
                            <span>區域均價：<strong id="avgU" class="text-white"></strong> 萬/坪</span>
                        </div>
                    </div>
                    <div class="space-y-3">
                        <div class="flex items-center justify-between p-3.5 rounded-xl bg-emerald-50 border border-emerald-200">
                            <div>
                                <span class="text-xs font-extrabold text-emerald-800 block">📉 買方保守出價落點 (PR25)</span>
                                <span class="text-[11px] text-emerald-600">低樓層、待整理屋況或首輪出價參考</span>
                            </div>
                            <div class="text-right"><p id="lowT" class="text-lg font-black text-emerald-900"></p><p id="lowU" class="text-xs font-bold text-emerald-700"></p></div>
                        </div>
                        <div class="flex items-center justify-between p-3.5 rounded-xl bg-amber-50 border border-amber-200">
                            <div>
                                <span class="text-xs font-extrabold text-amber-800 block">📈 屋主樂觀開價落點 (PR75)</span>
                                <span class="text-[11px] text-amber-600">高樓層景觀戶、帶裝潢之成交上限</span>
                            </div>
                            <div class="text-right"><p id="highT" class="text-lg font-black text-amber-900"></p><p id="highU" class="text-xs font-bold text-amber-700"></p></div>
                        </div>
                    </div>
                    <div class="mt-5 p-4 bg-slate-50 rounded-xl border border-slate-200 text-xs text-slate-600 leading-relaxed">
                        <p class="font-bold text-slate-800 mb-1"><i class="fa-solid fa-user-tie text-blue-600 mr-1"></i> 系統估價分析總結：</p>
                        <p id="aiSummary"></p>
                    </div>
                </div>
            </div>

            <div class="lg:col-span-7 space-y-6">
                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <h3 class="text-base font-black text-slate-900 mb-1">📊 周邊相近坪數「單價 vs 坪數」落點分佈圖</h3>
                    <p class="text-xs text-slate-400 mb-4">紅點為目標房屋合理估價點位，藍點為周邊近一年真實成交案例</p>
                    <div class="h-64 w-full"><canvas id="scatterChart"></canvas></div>
                </div>

                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-4">
                        <div>
                            <h3 class="text-base font-black text-slate-900">📋 近一年相似坪數實價登錄比對明細</h3>
                            <p id="filterNote" class="text-xs text-slate-400 mt-0.5"></p>
                        </div>
                        <div class="flex space-x-2">
                            <button onclick="filterTable('all')" id="btnAll" class="text-xs font-bold px-3 py-1.5 rounded-lg bg-blue-600 text-white">全部顯示</button>
                            <button onclick="filterTable('same')" id="btnSame" class="text-xs font-bold px-3 py-1.5 rounded-lg bg-slate-100 text-slate-600">只看同社區/鄰近</button>
                        </div>
                    </div>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left border-collapse">
                            <thead>
                                <tr class="border-b-2 border-slate-200 text-[11px] text-slate-400">
                                    <th class="py-2 px-2">關聯</th><th class="py-2 px-2">年月</th><th class="py-2 px-2">成交門牌</th>
                                    <th class="py-2 px-2 text-right">扣車位坪</th><th class="py-2 px-2 text-center">樓層</th>
                                    <th class="py-2 px-2 text-right">單價</th><th class="py-2 px-2 text-right">總價</th>
                                </tr>
                            </thead>
                            <tbody id="tableBody" class="divide-y divide-slate-100 text-xs"></tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </div>

<script>
let globalData = null;
let chartInstance = null;

async function runQuery() {
    const addr = document.getElementById('addr').value.trim();
    const ping = document.getElementById('ping').value || 0;
    const park = parseFloat(document.getElementById('park').value) || 0;
    const ratio = document.getElementById('ratio').value || 0.25;

    if (!addr) return alert("請輸入完整地址！");
    document.getElementById('err').classList.add('hidden');
    document.getElementById('dashboard').classList.add('hidden');
    document.getElementById('loading').classList.remove('hidden');

    try {
        const res = await fetch(`/api/analyze?address=${encodeURIComponent(addr)}&custom_ping=${ping}&range_ratio=${ratio}`);
        if (!res.ok) throw new Error(`HTTP 狀態碼 ${res.status}`);
        const d = await res.json();
        document.getElementById('loading').classList.add('hidden');

        if (d.error) {
            const errEl = document.getElementById('err');
            errEl.innerText = d.error;
            errEl.classList.remove('hidden');
            return;
        }

        globalData = d;
        renderDashboard(d, park);
        document.getElementById('dashboard').classList.remove('hidden');
    } catch (e) {
        document.getElementById('loading').classList.add('hidden');
        const errEl = document.getElementById('err');
        errEl.innerText = "雲端主機正在重新啟動或連線逾時 (" + e.message + ")，請等 10 秒後再按一次『立即估價分析』！";
        errEl.classList.remove('hidden');
    }
}

function renderDashboard(d, park) {
    document.getElementById('rAddr').innerText = d.target.addr;
    document.getElementById('rType').innerText = d.target.type;
    document.getElementById('note').innerText = d.target.note;
    document.getElementById('rPing').innerText = d.target.ping + " 坪";
    document.getElementById('rAge').innerText = d.target.age + " 年";
    document.getElementById('rFloor').innerText = d.target.floors;
    document.getElementById('rLayout').innerText = d.target.layout;

    document.getElementById('rCount').innerText = `共比對 ${d.val.count} 筆真實成交`;
    document.getElementById('medT').innerText = (d.val.med_t + park).toLocaleString();
    document.getElementById('medU').innerText = d.val.med_u;
    document.getElementById('avgU').innerText = d.val.avg_u;

    document.getElementById('lowT').innerText = (d.val.low_t + park).toLocaleString() + " 萬";
    document.getElementById('lowU').innerText = `單價 ${d.val.low_u} 萬/坪`;
    document.getElementById('highT').innerText = (d.val.high_t + park).toLocaleString() + " 萬";
    document.getElementById('highU').innerText = `單價 ${d.val.high_u} 萬/坪`;

    document.getElementById('filterNote').innerText = 
        `篩選條件：${d.target.road}周邊 ｜ 主+公設 ${d.target.min_ping} ~ ${d.target.max_ping} 坪 ｜ 近一年實價登錄`;

    document.getElementById('aiSummary').innerText = 
        `目標房屋設定為 ${d.target.ping} 坪，周邊共檢索出 ${d.val.count} 筆相近坪數交易（含 ${d.val.same_count} 筆同社區/鄰近門牌）。拆算車位後合理單價區間為 ${d.val.low_u} ~ ${d.val.high_u} 萬/坪。買方建議從 ${(d.val.low_t + park).toLocaleString()} 萬切入議價，最可能成交落點為 ${(d.val.med_t + park).toLocaleString()} 萬元。`;

    renderChart(d);
    filterTable('all');
}

function renderChart(d) {
    const ctx = document.getElementById('scatterChart').getContext('2d');
    if (chartInstance) chartInstance.destroy();
    const samePoints = d.comps.filter(c => c.is_same).map(c => ({x: c.ping, y: c.unit, addr: c.addr}));
    const otherPoints = d.comps.filter(c => !c.is_same).map(c => ({x: c.ping, y: c.unit, addr: c.addr}));

    chartInstance = new Chart(ctx, {
        type: 'scatter',
        data: {
            datasets: [
                { label: '目標房屋合理估值點', data: [{x: d.target.ping, y: d.val.med_u, addr: '目標房屋預估落點'}], backgroundColor: '#ef4444', pointRadius: 8 },
                { label: '同社區 / 鄰近門牌', data: samePoints, backgroundColor: '#2563eb', pointRadius: 6 },
                { label: '周邊相似成交', data: otherPoints, backgroundColor: '#94a3b8', pointRadius: 5 }
            ]
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { tooltip: { callbacks: { label: (ctx) => `${ctx.raw.addr}：${ctx.raw.x}坪 / 單價 ${ctx.raw.y}萬` } } },
            scales: { x: { title: { display: true, text: '扣車位坪數 (坪)' } }, y: { title: { display: true, text: '拆算單價 (萬/坪)' } } }
        }
    });
}

function filterTable(mode) {
    if (!globalData) return;
    const tbody = document.getElementById('tableBody');
    tbody.innerHTML = '';
    document.getElementById('btnAll').className = mode === 'all' ? "text-xs font-bold px-3 py-1.5 rounded-lg bg-blue-600 text-white" : "text-xs font-bold px-3 py-1.5 rounded-lg bg-slate-100 text-slate-600";
    document.getElementById('btnSame').className = mode === 'same' ? "text-xs font-bold px-3 py-1.5 rounded-lg bg-blue-600 text-white" : "text-xs font-bold px-3 py-1.5 rounded-lg bg-slate-100 text-slate-600";

    const list = mode === 'same' ? globalData.comps.filter(c => c.is_same) : globalData.comps;
    if (list.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="text-center py-6 text-slate-400">無符合此篩選條件之交易紀錄</td></tr>`;
        return;
    }
    list.forEach(c => {
        const badge = c.is_same ? `<span class="bg-blue-600 text-white text-[10px] px-2 py-0.5 rounded font-bold">同社區/鄰近</span>` : `<span class="bg-slate-100 text-slate-600 text-[10px] px-2 py-0.5 rounded font-semibold">周邊相似</span>`;
        const parkNote = c.park > 0 ? `<span class="block text-[10px] text-slate-400">含車位${c.park}萬</span>` : ``;
        tbody.innerHTML += `
            <tr class="hover:bg-slate-50 ${c.is_same ? 'bg-blue-50/40' : ''}">
                <td class="py-3 px-2 whitespace-nowrap">${badge}</td>
                <td class="py-3 px-2 font-semibold text-slate-600">${c.date}</td>
                <td class="py-3 px-2 font-bold text-slate-800">${c.addr}</td>
                <td class="py-3 px-2 text-right font-bold text-blue-700">${c.ping} 坪</td>
                <td class="py-3 px-2 text-center text-slate-500">${c.floor}</td>
                <td class="py-3 px-2 text-right font-black text-red-600">${c.unit} 萬/坪</td>
                <td class="py-3 px-2 text-right font-bold text-slate-900">${c.total.toLocaleString()} 萬 ${parkNote}</td>
            </tr>`;
    });
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
