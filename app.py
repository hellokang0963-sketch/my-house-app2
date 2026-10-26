# 檔案名稱：app.py (地政級門牌精準鎖定 + 同型態同屋齡嚴格比對版)
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

app = FastAPI(title="好房 AI 實價登錄精準估價網")

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

def cn_to_num(s: str) -> int:
    """精準將中文數字 (如 '十二', '二十五', '五') 轉為整數"""
    if not s: return 0
    s = str(s).replace("層", "").replace("樓", "").replace("F", "").strip()
    if s.isdigit(): return int(s)
    cmap = {'零':0, '一':1, '二':2, '三':3, '四':4, '五':5, '六':6, '七':7, '八':8, '九':9}
    if s == '十': return 10
    if '十' in s:
        parts = s.split('十')
        tens = cmap.get(parts[0], 1) if parts[0] != '' else 1
        ones = cmap.get(parts[1], 0) if len(parts) > 1 and parts[1] != '' else 0
        return tens * 10 + ones
    res = 0
    for ch in s:
        if ch in cmap:
            res = res * 10 + cmap[ch]
    return res

def normalize_address(addr: str) -> str:
    if not isinstance(addr, str): return ""
    addr = addr.replace("台", "臺").replace(" ", "").strip()
    full_to_half = str.maketrans("０１２３４５６７８９－～", "0123456789-~")
    addr = addr.translate(full_to_half)
    # 轉換「一段~十段」為「1段~10段」
    def repl_sec(m):
        return f"{cn_to_num(m.group(1))}{m.group(2)}"
    addr = re.sub(r'([一二三四五六七八九十]+)(段|樓|層|巷|弄|號)', repl_sec, addr)
    return addr

def extract_addr_features(clean_addr: str):
    """精準拆解地址：行政區、主幹道(路/街/段)、巷、弄、門牌號碼、之幾、樓層"""
    m = re.search(r'([臺台]北市|新北市|桃園市|[臺台]中市|[臺台]南市|高雄市|.{2}[縣市])?([^0-9號巷弄]+?[區鄉鎮市])?([^0-9號巷弄]+?[路街段大道])(?:(\d+)巷)?(?:(\d+)弄)?(\d+(?:-\d+)?)號', clean_addr)
    if not m:
        return None
    main_num_str = m.group(6).split('-')[0]
    # 解析樓層
    fl_m = re.search(r'號.*?(\d+)[樓層F]', clean_addr)
    floor_num = int(fl_m.group(1)) if fl_m else 0
    return {
        "city": m.group(1) or "",
        "district": m.group(2) or "",
        "road": m.group(3) or "",
        "lane": m.group(4) or "",     # 巷 (若無則為空字串)
        "alley": m.group(5) or "",    # 弄
        "num_raw": m.group(6) or "",  # 完整號 (如 180 或 180-1)
        "num": int(main_num_str) if main_num_str.isdigit() else 0,
        "floor": floor_num
    }

def parse_roc_date(date_str: str) -> int:
    if not isinstance(date_str, str) or len(date_str) < 6: return 0
    try:
        year = int(date_str[:-4]) + 1911
        month = int(date_str[-4:-2])
        return year * 12 + month
    except:
        return 0

def get_col(df: pd.DataFrame, keyword: str, default_val=""):
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
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    target_filename = f"{city_code}_lvr_land_a.csv"

    # 抓最新一期 + 過去 4 季 (確保涵蓋完整一年以上資料)
    urls_to_fetch = ["https://plvr.land.moi.gov.tw/Download?type=zip&fileName=lvr_landcsv.zip"]
    now = datetime.now()
    roc_year = now.year - 1911
    for y in [roc_year, roc_year - 1, roc_year - 2]:
        for s in [4, 3, 2, 1]:
            urls_to_fetch.append(f"https://plvr.land.moi.gov.tw/DownloadSeason?season={y}S{s}&type=zip&fileName=lvr_landcsv.zip")

    keep_keywords = ['鄉鎮市區', '交易標的', '門牌', '交易年月日', '移轉層次', '總樓層數', '建物型態', '建築完成', '建物移轉總面積', '格局-房', '格局-廳', '總價元', '單價元平方公尺', '車位移轉總面積', '車位總價元', '備註']

    success_seasons = 0
    for url in urls_to_fetch:
        if success_seasons >= 4:
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
        except Exception:
            continue

    if not dfs:
        return pd.DataFrame()

    raw_df = pd.concat(dfs, ignore_index=True)
    del dfs
    gc.collect()

    df = pd.DataFrame()
    df['district'] = get_col(raw_df, '鄉鎮市區', '').astype(str).str.strip()
    df['clean_addr'] = get_col(raw_df, '門牌', '').astype(str).apply(normalize_address)
    df['date_str'] = get_col(raw_df, '交易年月日', '').astype(str).str.strip()
    df['floor_raw'] = get_col(raw_df, '移轉層次', '-')
    df['floor_num'] = df['floor_raw'].apply(cn_to_num)
    df['total_floors_raw'] = get_col(raw_df, '總樓層數', '-')
    df['total_floors_num'] = df['total_floors_raw'].apply(cn_to_num)
    df['b_type'] = get_col(raw_df, '建物型態', '住宅大樓').astype(str).apply(lambda x: x.split('(')[0])
    df['build_ym'] = get_col(raw_df, '建築完成', '').astype(str).str.strip()
    df['rooms'] = get_col(raw_df, '格局-房', '3')
    df['halls'] = get_col(raw_df, '格局-廳', '2')
    df['note'] = get_col(raw_df, '備註', '')

    # 計算真實屋齡
    current_roc_year = datetime.now().year - 1911
    def calc_age(ym):
        if isinstance(ym, str) and len(ym) >= 5 and ym[:-4].isdigit():
            return max(0, current_roc_year - int(ym[:-4]))
        return -1 # 未知屋齡標記為 -1
    df['age'] = df['build_ym'].apply(calc_age)

    # 面積與價格換算
    df['total_ping'] = pd.to_numeric(get_col(raw_df, '建物移轉總面積', '0'), errors='coerce').fillna(0) * 0.3025
    df['parking_ping'] = pd.to_numeric(get_col(raw_df, '車位移轉總面積', '0'), errors='coerce').fillna(0) * 0.3025
    df['house_ping'] = df['total_ping'] - df['parking_ping']
    
    df['total_price'] = pd.to_numeric(raw_df['總價元'] if '總價元' in raw_df.columns else get_col(raw_df, '總價元', '0'), errors='coerce').fillna(0) / 10000
    df['parking_price'] = pd.to_numeric(get_col(raw_df, '車位總價元', '0'), errors='coerce').fillna(0) / 10000
    # 內政部官方計算之每平方公尺單價 (元/m2 -> 萬/坪：除以 0.3025 再除以 10000 = 乘以 0.0003305785)
    df['moi_unit_price'] = pd.to_numeric(get_col(raw_df, '單價元平方公尺', '0'), errors='coerce').fillna(0) * 0.0003305785

    del raw_df
    gc.collect()

    # 若有車位總價但無車位坪數，不可把車位坪數算進房屋坪數裡，改用 (總價-車位價)/官方單價 反推真實房屋坪數
    mask_infer_ping = (df['parking_price'] > 0) & (df['parking_ping'] == 0) & (df['moi_unit_price'] > 5)
    df.loc[mask_infer_ping, 'house_ping'] = (df.loc[mask_infer_ping, 'total_price'] - df.loc[mask_infer_ping, 'parking_price']) / df.loc[mask_infer_ping, 'moi_unit_price']

    # 計算精準單價：優先採用 (總價-車位價)/扣車位坪數，若無拆車位則採用內政部官方單價
    df['real_unit_price'] = df['moi_unit_price']
    has_explicit_park = (df['parking_price'] > 0) & (df['parking_ping'] > 0) & (df['house_ping'] > 1)
    df.loc[has_explicit_park, 'real_unit_price'] = (df['total_price'] - df['parking_price']) / df['house_ping']
    fallback_mask = (df['real_unit_price'] <= 1) & (df['house_ping'] > 1)
    df.loc[fallback_mask, 'real_unit_price'] = df['total_price'] / df['house_ping']

    df['month_val'] = df['date_str'].apply(parse_roc_date)

    # 排除特殊關係、親友、增建、毛胚、露台特殊交易與一樓店面(除非目標也是1樓)
    df = df[~df['note'].astype(str).str.contains('親友|特殊|急買|急賣|瑕疵|增建|政府|共有人|標售|債權', na=False)]
    df = df[(df['real_unit_price'] > 8) & (df['real_unit_price'] < 350) & (df['house_ping'] > 3)]
    df = df.drop_duplicates(subset=['clean_addr', 'date_str', 'total_price'])

    # 預先拆解每筆實價登錄的「路、巷、門牌號」以供 100% 精準比對 (解決 18號 誤配 118號 的問題)
    addr_extracted = df['clean_addr'].str.extract(r'([^0-9號巷弄區鄉鎮市]+?[路街段大道])(?:(\d+)巷)?(?:(\d+)弄)?(\d+)(?:-\d+)?號')
    df[' parsed_road'] = addr_extracted[0].fillna('')
    df['parsed_lane'] = addr_extracted[1].fillna('')
    df['parsed_num'] = pd.to_numeric(addr_extracted[3], errors='coerce').fillna(0).astype(int)

    CITY_DATA_CACHE[city_code] = df
    gc.collect()
    return df

@app.get("/api/analyze")
def analyze(address: str = Query(...), custom_ping: float = Query(0), range_ratio: float = Query(0.20)):
    try:
        clean_input = normalize_address(address)
        feat = extract_addr_features(clean_input)
        if not feat:
            return {"error": "請輸入包含『縣市、路街、號』的完整地址，例如：台北市內湖區成功路四段180號6樓"}

        city_code = "a"
        for city_name, code in CITY_CODE_MAP.items():
            if city_name in clean_input or city_name.replace("臺", "台") in address:
                city_code = code
                break

        df = fetch_moi_online_data(city_code)
        if df.empty:
            return {"error": "內政部開放資料伺服器目前繁忙，請等待 10 秒後再試一次！"}

        road = feat['road']
        lane = feat['lane']
        num = feat['num']
        target_floor = feat['floor']
        district = feat['district']

        # 先鎖定同一條路 (且嚴格區分「大馬路門牌」與「巷內門牌」)
        road_df = df[df[' parsed_road'] == road].copy()
        if road_df.empty:
            # 退一步用尾字匹配 (避免行政區黏在路名前面)
            road_df = df[df['clean_addr'].str.contains(road, na=False, regex=False)].copy()
        if district and not road_df[road_df['district'] == district].empty:
            road_df = road_df[road_df['district'] == district]

        if road_df.empty:
            return {"error": f"查無『{road}』之實價登錄，請確認縣市與路名是否正確。"}

        # 嚴格區分同巷或同在主幹道
        same_lane_df = road_df[road_df['parsed_lane'] == lane].copy()
        if same_lane_df.empty:
            same_lane_df = road_df

        # === 4 階段精準鎖定目標房屋規格 ===
        # Stage 1: 同路 + 同巷 + 完全同門牌號 (例如精準 180 號，絕不會抓到 80號 或 280號)
        exact_num_df = same_lane_df[same_lane_df['parsed_num'] == num]
        # Stage 1A: 甚至連樓層都一模一樣 (本戶歷史成交)
        exact_unit_df = exact_num_df[exact_num_df['floor_num'] == target_floor] if target_floor > 0 else pd.DataFrame()
        # Stage 2: 同社區連號門牌 (同單雙號，門牌差距 ±6 號內，例如 176, 178, 180, 182, 184 號)
        same_parity_nearby = same_lane_df[
            (same_lane_df['parsed_num'] % 2 == num % 2) &
            ((same_lane_df['parsed_num'] - num).abs() <= 6)
        ]

        if not exact_unit_df.empty:
            ref = exact_unit_df.iloc[0]
            target_ping = round(float(ref['house_ping']), 1) if custom_ping <= 0 else custom_ping
            target_age = int(ref['age']) if ref['age'] >= 0 else 15
            total_floors_num = int(ref['total_floors_num'])
            total_floors_str = f"{target_floor}F / {total_floors_num}F" if total_floors_num > 0 else str(ref['total_floors_raw'])
            b_type = str(ref['b_type'])
            layout = f"{ref['rooms']}房{ref['halls']}廳"
            source_msg = f"🎯 精準命中本戶【{ref['clean_addr']}】歷史實價登錄規格！"
        elif not exact_num_df.empty:
            ref = exact_num_df.iloc[0]
            # 找同棟樓層最接近的一戶坪數
            if target_floor > 0:
                exact_num_df = exact_num_df.assign(fl_diff=(exact_num_df['floor_num'] - target_floor).abs()).sort_values('fl_diff')
                ref = exact_num_df.iloc[0]
            target_ping = round(float(ref['house_ping']), 1) if custom_ping <= 0 else custom_ping
            target_age = int(ref['age']) if ref['age'] >= 0 else 15
            total_floors_num = int(ref['total_floors_num'])
            total_floors_str = f"{target_floor}F / {total_floors_num}F" if (target_floor > 0 and total_floors_num > 0) else f"共 {total_floors_num} 層"
            b_type = str(ref['b_type'])
            layout = f"{ref['rooms']}房{ref['halls']}廳"
            source_msg = f"✅ 精準命中同棟大樓【{road}{lane+'巷' if lane else ''}{num}號】({ref['floor_raw']}) 地政規格"
        elif not same_parity_nearby.empty:
            # 按門牌距離最近排序
            same_parity_nearby = same_parity_nearby.assign(num_diff=(same_parity_nearby['parsed_num'] - num).abs()).sort_values('num_diff')
            ref = same_parity_nearby.iloc[0]
            target_ping = round(float(same_parity_nearby['house_ping'].median()), 1) if custom_ping <= 0 else custom_ping
            target_age = int(ref['age']) if ref['age'] >= 0 else 15
            total_floors_num = int(ref['total_floors_num'])
            total_floors_str = f"{target_floor}F / {total_floors_num}F" if (target_floor > 0 and total_floors_num > 0) else f"共 {total_floors_num} 層"
            b_type = str(ref['b_type'])
            layout = f"{ref['rooms']}房{ref['halls']}廳"
            source_msg = f"✅ 自動配對同社區/緊鄰門牌【{ref['clean_addr']}】之大樓規格"
        else:
            # 若該號與緊鄰門牌皆無交易，根據使用者輸入的樓層智慧推斷是公寓還是大樓
            if target_floor >= 6:
                guess_df = same_lane_df[same_lane_df['total_floors_num'] >= 6]
                if guess_df.empty: guess_df = road_df[road_df['total_floors_num'] >= 6]
            else:
                guess_df = same_lane_df
            if guess_df.empty: guess_df = road_df
            guess_df = guess_df.assign(num_diff=(guess_df['parsed_num'] - num).abs()).sort_values('num_diff')
            ref = guess_df.iloc[0]
            target_ping = round(float(guess_df.head(10)['house_ping'].median()), 1) if custom_ping <= 0 else custom_ping
            target_age = int(ref['age']) if ref['age'] >= 0 else 20
            total_floors_num = int(ref['total_floors_num'])
            total_floors_str = f"約 {total_floors_num} 層"
            b_type = str(ref['b_type'])
            layout = f"{ref['rooms']}房{ref['halls']}廳"
            source_msg = f"⚠️ 該門牌尚無實價紀錄，已帶入最近門牌【{ref['clean_addr']}】規格（請於上方確認坪數）"

        # === 嚴格篩選近一年比對案例 (Comps) ===
        max_m = df['month_val'].max()
        min_ping = round(target_ping * (1 - range_ratio), 1)
        max_ping = round(target_ping * (1 + range_ratio), 1)

        # 過濾條件：
        # 1. 建物型態必須一致 (大樓比大樓/華廈，公寓只比公寓，透天只比透天！絕不混比)
        is_apt = "公寓" in b_type
        is_townhouse = "透天" in b_type

        def filter_candidates(pool_df, months_limit=12, age_tol=12):
            c = pool_df[
                (pool_df['month_val'] >= max_m - months_limit) &
                (pool_df['house_ping'] >= min_ping) &
                (pool_df['house_ping'] <= max_ping)
            ].copy()
            # 嚴格型態過濾
            if is_apt:
                c = c[c['b_type'].str.contains('公寓', na=False)]
            elif is_townhouse:
                c = c[c['b_type'].str.contains('透天', na=False)]
            else:
                c = c[c['b_type'].str.contains('大樓|華廈', na=False)]
            # 屋齡過濾 (避免拿 5年新成屋 去比 40年老大樓)
            if target_age > 0:
                c = c[(c['age'] < 0) | ((c['age'] - target_age).abs() <= age_tol)]
            # 若目標不是 1 樓，排除 1 樓店面/庭院戶以免單價失真
            if target_floor != 1:
                c = c[c['floor_num'] != 1]
            return c

        # 先找同路段 (近 12 個月，屋齡差距 ±10 年內)
        comps = filter_candidates(same_lane_df, months_limit=12, age_tol=10)
        if len(comps) < 5:
            comps = filter_candidates(road_df, months_limit=12, age_tol=12)
        if len(comps) < 5:
            # 放寬到近 18 個月同路段
            comps = filter_candidates(road_df, months_limit=18, age_tol=15)
        if len(comps) < 5 and district:
            # 若該路真的極少交易，才擴大到同行政區同屋齡同型態
            dist_df = df[df['district'] == district]
            comps = filter_candidates(dist_df, months_limit=12, age_tol=8)

        if comps.empty:
            return {"error": f"查無周邊『{b_type}、屋齡相近、坪數 {min_ping}~{max_ping} 坪』之近期交易，請嘗試將上方『坪數範圍』調寬至 ±35%。"}

        # 標記是否為「同社區/同棟連號門牌」(同路+同巷+同單雙號+門牌差<=6號+屋齡差<=3年)
        comps['is_same'] = (
            (comps[' parsed_road'] == road) &
            (comps['parsed_lane'] == lane) &
            (comps['parsed_num'] % 2 == num % 2) &
            ((comps['parsed_num'] - num).abs() <= 6) &
            ((comps['age'] - target_age).abs() <= 3)
        )
        # 計算門牌距離分數，優先顯示「同社區」與「門牌號碼最接近」的交易
        comps['num_dist'] = (comps['parsed_num'] - num).abs() + comps['parsed_lane'].apply(lambda x: 0 if x == lane else 200)
        comps = comps.sort_values(by=['is_same', 'num_dist', 'month_val'], ascending=[False, True, False])

        # 取最接近的前 25 筆計算統計落點 (避免太遠的路段稀釋行情)
        stat_df = comps.head(20) if len(comps[comps['is_same']]) < 3 else comps[comps['is_same']]
        q = stat_df['real_unit_price'].quantile([0.25, 0.5, 0.75]).to_dict()
        low_u, med_u, high_u = round(q[0.25], 1), round(q[0.5], 1), round(q[0.75], 1)
        avg_u = round(float(stat_df['real_unit_price'].mean()), 1)

        comp_list = []
        for _, r in comps.head(25).iterrows():
            d_str = str(r['date_str'])
            f_date = f"{d_str[:-4]}/{d_str[-4:-2]}" if len(d_str) >= 5 else d_str
            f_num = r['floor_num']
            t_num = r['total_floors_num']
            fl_display = f"{f_num}F / {t_num}F" if (f_num > 0 and t_num > 0) else f"{r['floor_raw']}/{r['total_floors_raw']}"
            comp_list.append({
                "date": f_date,
                "addr": r['clean_addr'],
                "b_type": r['b_type'],
                "age": f"{r['age']}年" if r['age'] >= 0 else "-",
                "ping": round(float(r['house_ping']), 1),
                "total_ping": round(float(r['total_ping']), 1),
                "floor": fl_display,
                "unit": round(float(r['real_unit_price']), 1),
                "total": round(float(r['total_price']), 0),
                "park": round(float(r['parking_price']), 0),
                "is_same": bool(r['is_same'])
            })

        val_basis = "同社區實價" if len(comps[comps['is_same']]) >= 3 else "同路段相近屋齡實價"

        return {
            "target": {
                "addr": clean_input, "district": district, "road": road,
                "ping": target_ping, "min_ping": min_ping, "max_ping": max_ping,
                "age": target_age, "floors": total_floors_str, "type": b_type,
                "layout": layout, "note": source_msg, "basis": val_basis
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
    <title>好房 AI 實價登錄精準估價平台</title>
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
                    <span class="hidden md:inline-block ml-2 text-xs bg-emerald-500/20 text-emerald-300 border border-emerald-400/30 px-2 py-0.5 rounded-full">地政級精準門牌鎖定引擎 v3.0</span>
                </div>
            </div>
        </div>
    </nav>

    <div class="bg-gradient-to-r from-slate-900 via-blue-900 to-indigo-900 text-white py-8 px-4 shadow-inner">
        <div class="max-w-7xl mx-auto">
            <h1 class="text-2xl md:text-3xl font-extrabold mb-2">輸入完整門牌，精準鎖定同棟規格與同型態實價落點</h1>
            <p class="text-blue-200 text-xs md:text-sm mb-6">嚴格區分「大樓 vs 公寓」、「主幹道 vs 巷弄」、「相近屋齡与門牌距離」，杜絕跨型態錯誤比對。</p>

            <div class="bg-white p-4 rounded-2xl shadow-xl text-slate-800 grid grid-cols-1 md:grid-cols-12 gap-3 items-end">
                <div class="md:col-span-5">
                    <label class="text-xs font-bold text-slate-500 block mb-1">📍 目標房屋完整門牌地址 (含樓層更準確)</label>
                    <input type="text" id="addr" value="台北市內湖區成功路四段180號6樓" 
                           placeholder="例：新北市板橋區文化路一段188號10樓"
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3.5 py-2.5 text-sm font-bold focus:outline-none focus:ring-2 focus:ring-blue-600">
                </div>
                <div class="md:col-span-2">
                    <label class="text-xs font-bold text-slate-500 block mb-1">📐 扣車位坪數 (選填)</label>
                    <input type="number" id="ping" placeholder="留空則自動抓同棟坪數" 
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3 py-2.5 text-sm font-bold focus:outline-none">
                </div>
                <div class="md:col-span-2">
                    <label class="text-xs font-bold text-slate-500 block mb-1">🚗 外加車位價值 (萬)</label>
                    <input type="number" id="park" value="0" 
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3 py-2.5 text-sm font-bold focus:outline-none">
                </div>
                <div class="md:col-span-1">
                    <label class="text-xs font-bold text-slate-500 block mb-1">🔍 坪數誤差</label>
                    <select id="ratio" class="w-full bg-slate-50 border border-slate-300 rounded-xl px-2 py-2.5 text-sm font-bold focus:outline-none">
                        <option value="0.15">±15%</option>
                        <option value="0.20" selected>±20%</option>
                        <option value="0.30">±30%</option>
                    </select>
                </div>
                <div class="md:col-span-2">
                    <button onclick="runQuery()" class="w-full bg-blue-600 hover:bg-blue-700 active:scale-95 transition text-white font-bold py-2.5 px-4 rounded-xl shadow-md flex items-center justify-center space-x-2">
                        <i class="fa-solid fa-magnifying-glass-chart"></i>
                        <span>精準估價比對</span>
                    </button>
                </div>
            </div>
        </div>
    </div>

    <div class="max-w-7xl mx-auto px-4 py-8">
        <div id="loading" class="hidden bg-white rounded-3xl p-12 text-center shadow-sm border border-slate-200 my-6">
            <i class="fa-solid fa-circle-notch text-5xl text-blue-600 spin mb-4"></i>
            <h3 class="text-lg font-extrabold text-slate-800">正在從內政部實價庫進行精準門牌與同棟比對...</h3>
            <p class="text-xs text-slate-400 mt-2">首次載入該縣市約需 10~20 秒下載官方最新資料，同縣市再次查詢僅需 0.1 秒！</p>
        </div>

        <div id="err" class="hidden bg-red-50 border-l-4 border-red-500 text-red-700 p-4 rounded-xl shadow-sm my-4 font-bold text-sm"></div>

        <div id="dashboard" class="hidden grid grid-cols-1 lg:grid-cols-12 gap-6">
            <div class="lg:col-span-5 space-y-6">
                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <div class="flex items-center justify-between mb-3">
                        <span class="bg-blue-100 text-blue-800 text-xs font-extrabold px-2.5 py-1 rounded-md">目標房屋地政規格</span>
                        <span id="rType" class="text-xs font-bold text-white bg-indigo-600 px-2.5 py-1 rounded-md"></span>
                    </div>
                    <h2 id="rAddr" class="text-xl font-black text-slate-900 mb-2"></h2>
                    <p id="note" class="text-xs text-blue-700 font-bold bg-blue-50 p-2.5 rounded-lg border border-blue-200 mb-4"></p>
                    <div class="grid grid-cols-4 gap-2 text-center bg-slate-50 p-3 rounded-xl border border-slate-100">
                        <div><p class="text-[11px] text-slate-400">扣車位坪數</p><p id="rPing" class="text-sm font-black text-blue-700 mt-0.5"></p></div>
                        <div class="border-l"><p class="text-[11px] text-slate-400">真實屋齡</p><p id="rAge" class="text-sm font-black text-slate-800 mt-0.5"></p></div>
                        <div class="border-l"><p class="text-[11px] text-slate-400">樓層資訊</p><p id="rFloor" class="text-sm font-black text-slate-800 mt-0.5"></p></div>
                        <div class="border-l"><p class="text-[11px] text-slate-400">參考格局</p><p id="rLayout" class="text-xs font-bold text-slate-800 mt-1"></p></div>
                    </div>
                </div>

                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <div class="flex items-center justify-between mb-4">
                        <h3 class="text-base font-black text-slate-900"><i class="fa-solid fa-bullseye text-red-500 mr-2"></i>成交價格落點分析</h3>
                        <span id="rBasis" class="text-xs bg-indigo-100 text-indigo-800 font-bold px-2.5 py-1 rounded-full"></span>
                    </div>
                    <div class="bg-gradient-to-br from-blue-900 to-indigo-900 text-white p-5 rounded-2xl text-center shadow-md mb-5">
                        <p class="text-xs text-blue-200 font-semibold">市場合理成交總價落點 (中位數 PR50)</p>
                        <div class="my-2 flex items-baseline justify-center space-x-1">
                            <span id="medT" class="text-4xl font-black text-yellow-300"></span>
                            <span class="text-sm font-bold">萬元</span>
                        </div>
                        <div class="flex justify-center space-x-4 text-xs text-blue-100 mt-2 pt-2 border-t border-blue-700/50">
                            <span>中位數單價：<strong id="medU" class="text-white"></strong> 萬/坪</span>
                            <span>•</span>
                            <span>樣本均價：<strong id="avgU" class="text-white"></strong> 萬/坪</span>
                        </div>
                    </div>
                    <div class="space-y-3">
                        <div class="flex items-center justify-between p-3.5 rounded-xl bg-emerald-50 border border-emerald-200">
                            <div>
                                <span class="text-xs font-extrabold text-emerald-800 block">📉 買方保守出價落點 (PR25)</span>
                                <span class="text-[11px] text-emerald-600">低樓層、屋況待整理或買方首輪出價參考</span>
                            </div>
                            <div class="text-right"><p id="lowT" class="text-lg font-black text-emerald-900"></p><p id="lowU" class="text-xs font-bold text-emerald-700"></p></div>
                        </div>
                        <div class="flex items-center justify-between p-3.5 rounded-xl bg-amber-50 border border-amber-200">
                            <div>
                                <span class="text-xs font-extrabold text-amber-800 block">📈 屋主樂觀開價落點 (PR75)</span>
                                <span class="text-[11px] text-amber-600">高樓層景觀戶、帶精緻裝潢之成交上限</span>
                            </div>
                            <div class="text-right"><p id="highT" class="text-lg font-black text-amber-900"></p><p id="highU" class="text-xs font-bold text-amber-700"></p></div>
                        </div>
                    </div>
                    <div class="mt-5 p-4 bg-slate-50 rounded-xl border border-slate-200 text-xs text-slate-600 leading-relaxed">
                        <p class="font-bold text-slate-800 mb-1"><i class="fa-solid fa-user-tie text-blue-600 mr-1"></i> 估價師 AI 分析總結：</p>
                        <p id="aiSummary"></p>
                    </div>
                </div>
            </div>

            <div class="lg:col-span-7 space-y-6">
                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <h3 class="text-base font-black text-slate-900 mb-1">📊 同型態物件「單價 vs 坪數」落點分佈圖</h3>
                    <p class="text-xs text-slate-400 mb-4">紅點為目標房屋合理估價點位，藍點為同社區/緊鄰門牌，灰點為同路段相近屋齡成交</p>
                    <div class="h-64 w-full"><canvas id="scatterChart"></canvas></div>
                </div>

                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-4">
                        <div>
                            <h3 class="text-base font-black text-slate-900">📋 近期相似坪數＆同型態實價登錄比對明細</h3>
                            <p id="filterNote" class="text-xs text-slate-400 mt-0.5"></p>
                        </div>
                        <div class="flex space-x-2">
                            <button onclick="filterTable('all')" id="btnAll" class="text-xs font-bold px-3 py-1.5 rounded-lg bg-blue-600 text-white">全部顯示</button>
                            <button onclick="filterTable('same')" id="btnSame" class="text-xs font-bold px-3 py-1.5 rounded-lg bg-slate-100 text-slate-600">只看同社區/緊鄰門牌</button>
                        </div>
                    </div>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left border-collapse">
                            <thead>
                                <tr class="border-b-2 border-slate-200 text-[11px] text-slate-400">
                                    <th class="py-2 px-2">關聯度</th><th class="py-2 px-2">成交年月</th><th class="py-2 px-2">成交門牌</th>
                                    <th class="py-2 px-2">型態/屋齡</th><th class="py-2 px-2 text-right">扣車位坪</th>
                                    <th class="py-2 px-2 text-center">樓層</th><th class="py-2 px-2 text-right">拆算單價</th><th class="py-2 px-2 text-right">成交總價</th>
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
    const ratio = document.getElementById('ratio').value || 0.20;

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
        errEl.innerText = "雲端主機連線逾時 (" + e.message + ")，請等 10 秒後再按一次『精準估價比對』！";
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

    document.getElementById('rBasis').innerText = `${d.target.basis} (共 ${d.val.count} 筆)`;
    document.getElementById('medT').innerText = (d.val.med_t + park).toLocaleString();
    document.getElementById('medU').innerText = d.val.med_u;
    document.getElementById('avgU').innerText = d.val.avg_u;

    document.getElementById('lowT').innerText = (d.val.low_t + park).toLocaleString() + " 萬";
    document.getElementById('lowU').innerText = `單價 ${d.val.low_u} 萬/坪`;
    document.getElementById('highT').innerText = (d.val.high_t + park).toLocaleString() + " 萬";
    document.getElementById('highU').innerText = `單價 ${d.val.high_u} 萬/坪`;

    document.getElementById('filterNote').innerText = 
        `嚴格篩選：${d.target.road} ｜ 限定【${d.target.type}】｜ 扣車位 ${d.target.min_ping} ~ ${d.target.max_ping} 坪 ｜ 依門牌距離排序`;

    document.getElementById('aiSummary').innerText = 
        `目標房屋設定為 ${d.target.ping} 坪之【${d.target.type}】（屋齡約 ${d.target.age} 年）。系統已嚴格排除不同型態與屋齡差距過大之物件，共鎖定 ${d.val.count} 筆同質性極高之實價紀錄（含 ${d.val.same_count} 筆同社區/緊鄰門牌）。拆算車位後合理單價落在 ${d.val.low_u} ~ ${d.val.high_u} 萬/坪，市場最可能成交落點約為 ${(d.val.med_t + park).toLocaleString()} 萬元。`;

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
                { label: '同社區 / 緊鄰門牌', data: samePoints, backgroundColor: '#2563eb', pointRadius: 6 },
                { label: '同路段同型態成交', data: otherPoints, backgroundColor: '#94a3b8', pointRadius: 5 }
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
        tbody.innerHTML = `<tr><td colspan="8" class="text-center py-6 text-slate-400">此門牌近一年無同社區成交，請切換至「全部顯示」查看同路段相近屋齡成交</td></tr>`;
        return;
    }
    list.forEach(c => {
        const badge = c.is_same ? `<span class="bg-blue-600 text-white text-[10px] px-2 py-0.5 rounded font-bold">同社區/緊鄰</span>` : `<span class="bg-slate-100 text-slate-600 text-[10px] px
