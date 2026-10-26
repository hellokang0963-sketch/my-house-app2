# 檔案名稱：app.py
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import pandas as pd
import requests
import zipfile
import io
import re
from datetime import datetime

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全台縣市對應內政部實價登錄代碼表
CITY_CODE_MAP = {
    "臺北市": "a", "台北市": "a",
    "臺中市": "b", "台中市": "b",
    "基隆市": "c",
    "臺南市": "d", "台南市": "d",
    "高雄市": "e",
    "新北市": "f",
    "宜蘭縣": "g",
    "桃園市": "h",
    "嘉義市": "i",
    "新竹縣": "j",
    "苗栗縣": "k",
    "南投縣": "m",
    "彰化縣": "n",
    "新竹市": "o",
    "雲林縣": "p",
    "嘉義縣": "q",
    "屏東縣": "t",
    "花蓮縣": "u",
    "臺東縣": "v", "台東縣": "v",
    "澎湖縣": "x",
    "金門縣": "w",
    "連江縣": "z"
}

# 快取記憶體：抓過的縣市存在記憶體，查詢速度0.1秒
CITY_DATA_CACHE = {}

def normalize_address(addr: str) -> str:
    if not isinstance(addr, str): return ""
    addr = addr.replace("台", "臺").strip()
    full_to_half = str.maketrans("０１２３４５６７８９", "0123456789")
    addr = addr.translate(full_to_half)
    # 將中文樓層或段轉阿拉伯數字方便統一比對
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

def fetch_moi_online_data(city_code: str) -> pd.DataFrame:
    """直接從內政部伺服器抓取最新與近一年的實價登錄網路資料"""
    if city_code in CITY_DATA_CACHE:
        return CITY_DATA_CACHE[city_code]

    print(f"🌐 正在從內政部伺服器即時抓取 [{city_code.upper()}] 縣市之實價登錄網路資料...")
    dfs = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    target_filename = f"{city_code}_lvr_land_a.csv"

    # 1. 先抓內政部「最新一期」公開資料 ZIP
    urls_to_fetch = ["https://plvr.land.moi.gov.tw/Download?type=zip&fileName=lvr_landcsv.zip"]
    
    # 2. 自動推算並抓取過去 4 季的歷史資料 ZIP (湊滿完整一年資料)
    now = datetime.now()
    roc_year = now.year - 1911
    for y in [roc_year, roc_year - 1]:
        for s in [4, 3, 2, 1]:
            urls_to_fetch.append(f"https://plvr.land.moi.gov.tw/DownloadSeason?season={y}S{s}&type=zip&fileName=lvr_landcsv.zip")

    # 抓取最新一期 + 最近存在的前幾季資料 (湊齊足夠的一年期樣本即停止，加快速度)
    success_seasons = 0
    for url in urls_to_fetch:
        if success_seasons >= 4:  # 抓滿近4季+本期(約1年)即停止
            break
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            if resp.status_code == 200 and len(resp.content) > 1000:
                with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                    if target_filename in z.namelist():
                        with z.open(target_filename) as f:
                            sub_df = pd.read_csv(f, dtype=str, on_bad_lines='skip')
                            if len(sub_df) > 1:
                                dfs.append(sub_df.iloc[1:]) # 跳過第二行英文標頭
                                success_seasons += 1
        except Exception as e:
            continue

    if not dfs:
        return pd.DataFrame()

    df = pd.concat(dfs, ignore_index=True)
    
    # 清洗網路抓下來的原始資料
    df['clean_addr'] = df['土地位置建物門牌'].astype(str).apply(normalize_address)
    df['total_ping'] = pd.to_numeric(df['建物移轉總面積平方公尺'], errors='coerce') * 0.3025
    df['parking_ping'] = pd.to_numeric(df['車位移轉總面積(平方公尺)'], errors='coerce').fillna(0) * 0.3025
    df['house_ping'] = df['total_ping'] - df['parking_ping']
    
    df['total_price'] = pd.to_numeric(df['總價元'], errors='coerce') / 10000
    df['parking_price'] = pd.to_numeric(df['車位總價元'], errors='coerce').fillna(0) / 10000

    def calc_unit(row):
        if row['house_ping'] <= 1: return 0
        if row['parking_price'] > 0 and row['parking_ping'] > 0:
            return (row['total_price'] - row['parking_price']) / row['house_ping']
        return row['total_price'] / row['total_ping']

    df['real_unit_price'] = df.apply(calc_unit, axis=1)
    df['month_val'] = df['交易年月日'].apply(parse_roc_date)

    # 過濾掉特殊交易與純車位/純土地交易
    df = df[df['交易標的'].str.contains('房地', na=False)]
    df = df[~df['備註'].astype(str).str.contains('親友|特殊|急買|急賣|瑕疵|增建|政府', na=False)]
    df = df[(df['real_unit_price'] > 5) & (df['real_unit_price'] < 400)]
    df = df.drop_duplicates(subset=['clean_addr', '交易年月日', '總價元'])

    CITY_DATA_CACHE[city_code] = df
    print(f"✅ [{city_code.upper()}] 網路資料抓取完成，共 {len(df)} 筆真實交易！")
    return df

@app.get("/api/analyze")
def analyze(address: str = Query(...), custom_ping: float = Query(0)):
    clean_input = normalize_address(address)
    
    # 1. 自動判斷縣市以決定要去抓內政部哪一個縣市的網路資料
    city_code = "a" # 預設台北市
    for city_name, code in CITY_CODE_MAP.items():
        if city_name in clean_input or city_name.replace("臺", "台") in address:
            city_code = code
            break

    df = fetch_moi_online_data(city_code)
    if df.empty:
        return {"error": "目前內政部開放資料伺服器連線逾時，請稍後再試。"}

    # 2. 解析使用者輸入的門牌 (行政區 + 路街段 + 巷弄 + 號)
    match = re.search(r'([臺台]北市|新北市|桃園市|[臺台]中市|[臺台]南市|高雄市|.{2}[縣市])?(.+?[區鄉鎮市])?(.+?[路街段])(\d+巷)?(\d+弄)?(\d+號)', clean_input)
    if not match:
        return {"error": "請輸入包含『縣市、路街、號』的完整地址，例如：台北市內湖區成功路4段180號6樓"}

    district = match.group(2) or ""
    road = match.group(3) or ""
    lane = match.group(4) or ""
    number = match.group(6) or ""
    
    # 擷取同社區/同棟大樓門牌基底 (例如：成功路4段180號)
    num_val = int(re.sub(r'\D', '', number))
    # 台灣同社區門牌常為連號 (如 176號~184號)，建立同棟與鄰近門牌正則
    nearby_nums = "|".join([f"{n}號" for n in range(max(1, num_val-4), num_val+5, 2)])
    same_community_pattern = f"{road}{lane}({nearby_nums})"
    exact_building_addr = f"{road}{lane}{number}"

    current_roc_year = datetime.now().year - 1911

    # 3. 自動從網路實價資料帶出這間房子的真實坪數、屋齡、樓層
    exact_matches = df[df['clean_addr'].str.contains(exact_building_addr, na=False)]
    community_matches = df[df['clean_addr'].str.contains(same_community_pattern, na=False)]
    
    target_source_df = exact_matches if not exact_matches.empty else community_matches

    if not target_source_df.empty:
        ref = target_source_df.iloc[0]
        target_ping = round(float(target_source_df['house_ping'].median()), 1) if custom_ping <= 0 else custom_ping
        build_ym = str(ref['建築完成年月'])
        build_y = int(build_ym[:-4]) if len(build_ym) >= 5 and build_ym[:-4].isdigit() else (current_roc_year - 15)
        target_age = max(1, current_roc_year - build_y)
        total_floors = str(ref['總樓層數'])
        b_type = str(ref['建物型態']).split('(')[0]
        source_msg = f"✅ 已自動從網路抓取【{ref['clean_addr']}】同棟地政規格"
    else:
        road_matches = df[df['clean_addr'].str.contains(road, na=False)]
        if road_matches.empty:
            return {"error": f"內政部近期資料查無『{road}』的交易，請確認縣市與路名是否正確。"}
        target_ping = round(float(road_matches['house_ping'].median()), 1) if custom_ping <= 0 else custom_ping
        target_age = 15
        total_floors = str(road_matches.iloc[0]['總樓層數'])
        b_type = str(road_matches.iloc[0]['建物型態']).split('(')[0]
        source_msg = f"ℹ️ 該門牌近期無交易，已帶入『{road}』標準坪數（可於上方手動調整坪數）"

    # 4. 篩選近一年、坪數接近 (±25% 內) 的真實實價登錄
    max_m = df['month_val'].max()
    comps = df[
        (df['clean_addr'].str.contains(road, na=False)) &
        (df['month_val'] >= max_m - 12) &
        (df['house_ping'] >= target_ping * 0.75) &
        (df['house_ping'] <= target_ping * 1.25)
    ].copy()

    # 若同條路一年內同坪數交易不足 3 筆，自動擴大至同行政區同型態建物
    if len(comps) < 3 and district:
        comps = df[
            (df['鄉鎮市區'] == district) &
            (df['建物型態'].str.contains(b_type[:2], na=False)) &
            (df['month_val'] >= max_m - 12) &
            (df['house_ping'] >= target_ping * 0.82) &
            (df['house_ping'] <= target_ping * 1.18)
        ].copy()

    if comps.empty:
        return {"error": f"查無周邊坪數接近 {target_ping} 坪之近期交易，請嘗試微調坪數。"}

    comps['is_same'] = comps['clean_addr'].str.contains(same_community_pattern, na=False)
    comps = comps.sort_values(by=['is_same', 'month_val'], ascending=[False, False])

    # 5. 計算 PR25 / 中位數 / PR75 價格落點
    q = comps['real_unit_price'].quantile([0.25, 0.5, 0.75]).to_dict()
    low_u, med_u, high_u = round(q[0.25], 1), round(q[0.5], 1), round(q[0.75], 1)

    comp_list = []
    for _, r in comps.head(12).iterrows():
        d_str = str(r['交易年月日'])
        f_date = f"{d_str[:-4]}/{d_str[-4:-2]}" if len(d_str) >= 5 else d_str
        comp_list.append({
            "date": f_date,
            "addr": r['clean_addr'],
            "ping": round(float(r['house_ping']), 1),
            "floor": f"{r['移轉層次']}/{r['總樓層數']}",
            "unit": round(float(r['real_unit_price']), 1),
            "total": round(float(r['total_price']), 0),
            "park": round(float(r['parking_price']), 0),
            "is_same": bool(r['is_same'])
        })

    return {
        "target": {
            "addr": clean_input,
            "ping": target_ping,
            "age": target_age,
            "floors": total_floors,
            "type": b_type,
            "note": source_msg
        },
        "val": {
            "count": len(comps),
            "low_u": low_u, "med_u": med_u, "high_u": high_u,
            "low_t": round(low_u * target_ping),
            "med_t": round(med_u * target_ping),
            "high_t": round(high_u * target_ping)
        },
        "comps": comp_list
    }

# 手機版前端網頁直接內嵌回傳
@app.get("/", response_class=HTMLResponse)
def home():
    return """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>全台實價登錄即時連線估價系統</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background-color: #0f172a; }
        .app-box { max-width: 480px; margin: 0 auto; min-height: 100vh; background-color: #f8fafc; padding-bottom: 40px; }
        .spin { animation: spin 1s linear infinite; }
        @keyframes spin { 100% { transform: rotate(360deg); } }
    </style>
</head>
<body>
<div class="app-box shadow-2xl">
    <div class="bg-gradient-to-br from-blue-900 to-indigo-800 text-white p-5 rounded-b-3xl shadow-lg">
        <div class="flex items-center justify-between mb-2">
            <h1 class="text-lg font-extrabold"><i class="fa-solid fa-satellite-dish text-yellow-300 mr-2"></i>實價登錄網路直連估價</h1>
            <span class="text-[10px] bg-emerald-500 text-white px-2 py-0.5 rounded-full font-bold">內政部即時連線</span>
        </div>
        <p class="text-blue-200 text-xs mb-4">免下載檔案！直接輸入全台門牌，即時從內政部雲端抓取最新實價登錄並計算落點。</p>

        <div class="bg-white p-3.5 rounded-2xl text-gray-800 shadow-inner">
            <label class="text-[11px] font-bold text-gray-500">輸入全台完整地址：</label>
            <input type="text" id="addr" value="台北市內湖區成功路四段180號6樓" 
                   placeholder="例：新北市板橋區文化路一段100號8樓"
                   class="w-full text-sm font-bold border-b-2 border-blue-100 py-1.5 mb-3 focus:outline-none focus:border-blue-600">
            
            <div class="grid grid-cols-2 gap-2 mb-3">
                <div class="bg-slate-50 p-2 rounded-xl border border-slate-200">
                    <label class="text-[10px] text-gray-400 block">扣車位坪數 (選填)</label>
                    <input type="number" id="ping" placeholder="自動抓取同棟坪數" class="w-full text-xs font-bold bg-transparent focus:outline-none">
                </div>
                <div class="bg-slate-50 p-2 rounded-xl border border-slate-200">
                    <label class="text-[10px] text-gray-400 block">車位價值 (萬元)</label>
                    <input type="number" id="park" value="0" class="w-full text-xs font-bold bg-transparent focus:outline-none">
                </div>
            </div>

            <button onclick="runQuery()" class="w-full bg-blue-600 hover:bg-blue-700 active:scale-95 transition text-white font-bold py-3 rounded-xl shadow-md flex justify-center items-center space-x-2">
                <i class="fa-solid fa-bolt"></i>
                <span>即時上網抓取資料並估價</span>
            </button>
        </div>
    </div>

    <!-- 讀取動畫 -->
    <div id="loading" class="hidden flex-col items-center justify-center py-12 px-6 text-center">
        <i class="fa-solid fa-circle-notch text-4xl text-blue-600 spin mb-3"></i>
        <p class="text-sm font-bold text-gray-700">正在連線至內政部不動產伺服器...</p>
        <p class="text-xs text-gray-400 mt-1">首次查詢該縣市約需 5~10 秒下載官方最新壓縮檔，第二次起僅需 0.1 秒！</p>
    </div>

    <div id="err" class="hidden m-4 p-4 bg-red-50 border border-red-200 text-red-600 rounded-xl text-xs font-bold"></div>

    <!-- 結果區 -->
    <div id="res" class="hidden px-4 mt-4 space-y-4">
        <div class="bg-white p-4 rounded-2xl shadow-sm border-l-4 border-blue-600">
            <p id="note" class="text-[11px] text-blue-600 font-bold mb-1"></p>
            <h2 id="rAddr" class="text-base font-extrabold text-gray-800"></h2>
            <div class="grid grid-cols-4 gap-2 text-center bg-slate-50 p-2.5 rounded-xl mt-3 border">
                <div><p class="text-[10px] text-gray-400">主+公設坪</p><p id="rPing" class="text-sm font-bold text-blue-700"></p></div>
                <div><p class="text-[10px] text-gray-400">真實屋齡</p><p id="rAge" class="text-sm font-bold"></p></div>
                <div><p class="text-[10px] text-gray-400">總樓層</p><p id="rFloor" class="text-sm font-bold"></p></div>
                <div><p class="text-[10px] text-gray-400">型態</p><p id="rType" class="text-xs font-bold mt-0.5"></p></div>
            </div>
        </div>

        <div class="bg-white p-4 rounded-2xl shadow-sm">
            <div class="flex justify-between items-center mb-3">
                <h3 class="font-bold text-gray-800 text-sm">🎯 實價登錄價格落點分析</h3>
                <span id="rCount" class="text-[11px] bg-blue-100 text-blue-800 px-2.5 py-0.5 rounded-full font-bold"></span>
            </div>
            <div class="bg-gradient-to-r from-blue-50 to-indigo-50 p-4 rounded-2xl text-center border border-blue-100 mb-3">
                <p class="text-xs text-blue-600 font-bold">合理成交總價落點 (中位數 PR50)</p>
                <p class="text-3xl font-black text-blue-900 my-1"><span id="medT"></span> <span class="text-sm">萬元</span></p>
                <p class="text-xs text-gray-500">拆算車位後合理單價：<strong id="medU" class="text-blue-700"></strong> 萬/坪</p>
            </div>
            <div class="grid grid-cols-2 gap-3 text-center text-xs">
                <div class="bg-emerald-50 p-3 rounded-xl border border-emerald-200">
                    <p class="text-emerald-700 font-bold">📉 買方出價落點 (PR25)</p>
                    <p id="lowT" class="text-base font-black text-gray-800 mt-1"></p>
                    <p id="lowU" class="text-[11px] text-gray-500"></p>
                </div>
                <div class="bg-amber-50 p-3 rounded-xl border border-amber-200">
                    <p class="text-amber-700 font-bold">📈 屋主開價落點 (PR75)</p>
                    <p id="highT" class="text-base font-black text-gray-800 mt-1"></p>
                    <p id="highU" class="text-[11px] text-gray-500"></p>
                </div>
            </div>
        </div>

        <div class="bg-white p-4 rounded-2xl shadow-sm">
            <h3 class="font-bold text-gray-800 text-sm mb-1">📋 周邊近一年相似坪數真實成交紀錄</h3>
            <p class="text-[11px] text-gray-400 mb-3">已自動排除親友交易並拆算車位坪數</p>
            <div id="list" class="space-y-2.5"></div>
        </div>
    </div>
</div>

<script>
async function runQuery() {
    const addr = document.getElementById('addr').value.trim();
    const ping = document.getElementById('ping').value || 0;
    const park = parseFloat(document.getElementById('park').value) || 0;

    if (!addr) return alert("請輸入地址！");
    document.getElementById('err').classList.add('hidden');
    document.getElementById('res').classList.add('hidden');
    document.getElementById('loading').classList.remove('hidden');
    document.getElementById('loading').classList.add('flex');

    try {
        const res = await fetch(`/api/analyze?address=${encodeURIComponent(addr)}&custom_ping=${ping}`);
        const d = await res.json();
        document.getElementById('loading').classList.add('hidden');
        document.getElementById('loading').classList.remove('flex');

        if (d.error) {
            const errEl = document.getElementById('err');
            errEl.innerText = d.error;
            errEl.classList.remove('hidden');
            return;
        }

        document.getElementById('note').innerText = d.target.note;
        document.getElementById('rAddr').innerText = d.target.addr;
        document.getElementById('rPing').innerText = d.target.ping + " 坪";
        document.getElementById('rAge').innerText = d.target.age + " 年";
        document.getElementById('rFloor').innerText = d.target.floors;
        document.getElementById('rType').innerText = d.target.type;

        document.getElementById('rCount').innerText = `共比對 ${d.val.count} 筆實價`;
        document.getElementById('medT').innerText = (d.val.med_t + park).toLocaleString();
        document.getElementById('medU').innerText = d.val.med_u;
        document.getElementById('lowT').innerText = (d.val.low_t + park).toLocaleString() + " 萬";
        document.getElementById('lowU').innerText = `單價 ${d.val.low_u} 萬/坪`;
        document.getElementById('highT').innerText = (d.val.high_t + park).toLocaleString() + " 萬";
        document.getElementById('highU').innerText = `單價 ${d.val.high_u} 萬/坪`;

        const list = document.getElementById('list');
        list.innerHTML = '';
        d.comps.forEach(c => {
            const tag = c.is_same 
                ? `<span class="bg-blue-600 text-white text-[10px] px-1.5 py-0.5 rounded font-bold">同社區/鄰近門牌</span>`
                : `<span class="bg-slate-200 text-slate-700 text-[10px] px-1.5 py-0.5 rounded font-bold">同路段相似坪數</span>`;
            const parkTxt = c.park > 0 ? `(含車位${c.park}萬)` : ``;
            list.innerHTML += `
                <div class="p-3 rounded-xl border ${c.is_same ? 'border-blue-300 bg-blue-50/50' : 'border-slate-100 bg-slate-50'} flex justify-between items-center">
                    <div class="pr-2">
                        <div class="flex items-center space-x-1.5 mb-1">${tag}<span class="text-xs font-bold text-gray-800">${c.addr}</span></div>
                        <div class="text-[11px] text-gray-500">📅 ${c.date} • 扣車位 <b>${c.ping}坪</b> • ${c.floor}</div>
                    </div>
                    <div class="text-right whitespace-nowrap">
                        <p class="text-sm font-black text-red-600">${c.unit} <span class="text-[10px]">萬/坪</span></p>
                        <p class="text-[11px] font-bold text-gray-700">總價 ${c.total.toLocaleString()} 萬</p>
                        <p class="text-[9px] text-gray-400">${parkTxt}</p>
                    </div>
                </div>`;
        });
        document.getElementById('res').classList.remove('hidden');
    } catch (e) {
        document.getElementById('loading').classList.add('hidden');
        document.getElementById('err').innerText = "網路連線異常，請重試。";
        document.getElementById('err').classList.remove('hidden');
    }
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)