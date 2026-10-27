# 檔案名稱：app.py (v5.1 語法修正 + 急速穩定版)
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

app = FastAPI(title="好房 AI 房屋規格與同社區行情系統")

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

ROAD_DATA_CACHE = {}

def cn_to_num(s: str) -> int:
    if not s:
        return 0
    s = str(s).replace("層", "").replace("樓", "").replace("F", "").strip()
    if s.isdigit():
        return int(s)
    if "地下" in s or "B" in s.upper():
        return -1
    cmap = {'零': 0, '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
    if s == '十':
        return 10
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
    if not isinstance(addr, str):
        return ""
    addr = addr.replace("台", "臺").replace(" ", "").strip()
    full_to_half = str.maketrans("０１２３４５６７８９－～Ｆ", "0123456789-~F")
    addr = addr.translate(full_to_half)
    def repl_sec(m):
        return f"{cn_to_num(m.group(1))}{m.group(2)}"
    addr = re.sub(r'([一二三四五六七八九十]+)(段|樓|層|巷|弄|號)', repl_sec, addr)
    return addr

def parse_user_address(clean_addr: str):
    clean_addr = re.sub(r'^\d{3,5}', '', clean_addr)
    clean_addr = re.sub(r'[^區鄉鎮市]+?[里村]\d+鄰', '', clean_addr)
    m = re.search(
        r'([臺台]北市|新北市|桃園市|[臺台]中市|[臺台]南市|高雄市|.{2}[縣市])?'
        r'([^0-9號巷弄]+?[區鄉鎮市])?'
        r'([^0-9號巷弄區鄉鎮市]+?(?:路|街|大道|段))'
        r'(?:(\d+)巷)?'
        r'(?:(\d+)弄)?'
        r'(\d+)(?:之(\d+)|-(\d+))?號',
        clean_addr
    )
    if not m:
        return None
    fl_m = re.search(r'號.*?(\d+)[樓層F]', clean_addr)
    floor_num = int(fl_m.group(1)) if fl_m else 0
    return {
        "city": m.group(1) or "",
        "district": m.group(2) or "",
        "road": m.group(3) or "",
        "lane": m.group(4) or "",
        "alley": m.group(5) or "",
        "num": int(m.group(6)),
        "floor": floor_num
    }

def parse_roc_date(date_str: str) -> int:
    if not isinstance(date_str, str) or len(date_str) < 6:
        return 0
    try:
        year = int(date_str[:-4]) + 1911
        month = int(date_str[-4:-2])
        return year * 12 + month
    except Exception:
        return 0

def get_col(df: pd.DataFrame, keyword: str, default_val=""):
    for c in df.columns:
        if keyword in str(c):
            return df[c]
    return pd.Series([default_val] * len(df), index=df.index)

def fetch_road_deep_data(city_code: str, road_name: str) -> pd.DataFrame:
    cache_key = f"{city_code}_{road_name}"
    if cache_key in ROAD_DATA_CACHE:
        return ROAD_DATA_CACHE[cache_key]

    if len(ROAD_DATA_CACHE) > 8:
        ROAD_DATA_CACHE.clear()
        gc.collect()

    dfs = []
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    target_filename = f"{city_code}_lvr_land_a.csv"

    urls_to_fetch = ["https://plvr.land.moi.gov.tw/Download?type=zip&fileName=lvr_landcsv.zip"]
    now = datetime.now()
    roc_year = now.year - 1911
    for y in range(roc_year, roc_year - 4, -1):
        for s in [4, 3, 2, 1]:
            urls_to_fetch.append(f"https://plvr.land.moi.gov.tw/DownloadSeason?season={y}S{s}&type=zip&fileName=lvr_landcsv.zip")

    keep_keywords = [
        '鄉鎮市區', '交易標的', '門牌', '交易年月日', '移轉層次', '總樓層數',
        '建物型態', '主要用途', '建築完成', '建物移轉總面積', '主建物面積',
        '附屬建物面積', '陽台面積', '格局-房', '格局-廳', '格局-衛',
        '總價元', '單價元平方公尺', '車位移轉總面積', '車位總價元', '備註', '有無管理組織'
    ]

    road_base = re.sub(r'\d+段', '', road_name)

    success_seasons = 0
    for url in urls_to_fetch:
        if success_seasons >= 6:  # 抓最新一期 + 過去5季，確保15秒內快速完成不超時
            break
        try:
            resp = requests.get(url, headers=headers, timeout=12, verify=False)
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
                                sub_df = sub_df.iloc[1:]
                                addr_col = get_col(sub_df, '門牌', '').astype(str)
                                road_mask = addr_col.str.contains(road_base, na=False, regex=False)
                                sub_df = sub_df[road_mask].copy()
                                if not sub_df.empty:
                                    target_type_s = get_col(sub_df, '交易標的', '')
                                    sub_df = sub_df[target_type_s.str.contains('房地|建物', na=False)]
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
    df = df[df['clean_addr'].str.contains(road_name, na=False, regex=False)].copy()
    if df.empty:
        return pd.DataFrame()

    raw_df = raw_df.loc[df.index]
    df['date_str'] = get_col(raw_df, '交易年月日', '').astype(str).str.strip()
    df['floor_raw'] = get_col(raw_df, '移轉層次', '-')
    df['floor_num'] = df['floor_raw'].apply(cn_to_num)
    df['total_floors_raw'] = get_col(raw_df, '總樓層數', '-')
    df['total_floors_num'] = df['total_floors_raw'].apply(cn_to_num)
    df['b_type'] = get_col(raw_df, '建物型態', '住宅大樓').astype(str).apply(lambda x: x.split('(')[0])
    df['usage'] = get_col(raw_df, '主要用途', '住家用').astype(str)
    df['mgmt'] = get_col(raw_df, '有無管理組織', '有').astype(str)
    df['build_ym'] = get_col(raw_df, '建築完成', '').astype(str).str.strip()
    df['rooms'] = get_col(raw_df, '格局-房', '3')
    df['halls'] = get_col(raw_df, '格局-廳', '2')
    df['baths'] = get_col(raw_df, '格局-衛', '2')
    df['note'] = get_col(raw_df, '備註', '').fillna('')

    df['total_ping'] = pd.to_numeric(get_col(raw_df, '建物移轉總面積', '0'), errors='coerce').fillna(0) * 0.3025
    df['main_ping'] = pd.to_numeric(get_col(raw_df, '主建物面積', '0'), errors='coerce').fillna(0) * 0.3025
    df['sub_ping'] = (pd.to_numeric(get_col(raw_df, '附屬建物面積', '0'), errors='coerce').fillna(0) +
                      pd.to_numeric(get_col(raw_df, '陽台面積', '0'), errors='coerce').fillna(0)) * 0.3025
    df['parking_ping'] = pd.to_numeric(get_col(raw_df, '車位移轉總面積', '0'), errors='coerce').fillna(0) * 0.3025

    df['total_price'] = pd.to_numeric(raw_df['總價元'] if '總價元' in raw_df.columns else get_col(raw_df, '總價元', '0'), errors='coerce').fillna(0) / 10000
    df['parking_price'] = pd.to_numeric(get_col(raw_df, '車位總價元', '0'), errors='coerce').fillna(0) / 10000
    df['moi_unit_price'] = pd.to_numeric(get_col(raw_df, '單價元平方公尺', '0'), errors='coerce').fillna(0) * 0.0003305785

    del raw_df
    gc.collect()

    current_roc_year = datetime.now().year - 1911
    def calc_age(ym):
        if isinstance(ym, str) and len(ym) >= 5 and ym[:-4].isdigit():
            return max(0, current_roc_year - int(ym[:-4]))
        return -1
    df['age'] = df['build_ym'].apply(calc_age)

    df['house_ping'] = df['total_ping'] - df['parking_ping']
    mask_no_p_ping = (df['parking_price'] > 0) & (df['parking_ping'] == 0) & (df['moi_unit_price'] > 5)
    df.loc[mask_no_p_ping, 'house_ping'] = (df.loc[mask_no_p_ping, 'total_price'] - df.loc[mask_no_p_ping, 'parking_price']) / df.loc[mask_no_p_ping, 'moi_unit_price']

    df['public_ping'] = (df['house_ping'] - df['main_ping'] - df['sub_ping']).clip(lower=0)

    df['real_unit_price'] = df['moi_unit_price']
    has_explicit_park = (df['parking_price'] > 0) & (df['parking_ping'] > 0) & (df['house_ping'] > 1)
    df.loc[has_explicit_park, 'real_unit_price'] = (df['total_price'] - df['parking_price']) / df['house_ping']
    fallback_mask = (df['real_unit_price'] <= 1) & (df['house_ping'] > 1)
    df.loc[fallback_mask, 'real_unit_price'] = df['total_price'] / df['house_ping']

    df['month_val'] = df['date_str'].apply(parse_roc_date)

    df = df[~df['note'].str.contains('親友|特殊|急買|急賣|瑕疵|共有人|標售|債權', na=False)]
    df = df[(df['real_unit_price'] > 6) & (df['real_unit_price'] < 400) & (df['house_ping'] > 3)]
    df = df.drop_duplicates(subset=['clean_addr', 'date_str', 'total_price'])

    extracted = df['clean_addr'].str.extract(
        r'([^0-9號巷弄區鄉鎮市]+?(?:路|街|大道|段))'
        r'(?:(\d+)巷)?'
        r'(?:(\d+)弄)?'
        r'(\d+)(?:之\d+|-\d+)?號'
    )
    df['p_road'] = extracted[0].fillna('')
    df['p_lane'] = extracted[1].fillna('')
    df['p_alley'] = extracted[2].fillna('')
    df['p_num'] = pd.to_numeric(extracted[3], errors='coerce').fillna(-999).astype(int)

    ROAD_DATA_CACHE[cache_key] = df
    gc.collect()
    return df

@app.get("/api/analyze")
def analyze(address: str = Query(...), custom_ping: float = Query(0), range_ratio: float = Query(0.25)):
    try:
        clean_input = normalize_address(address)
        feat = parse_user_address(clean_input)
        if not feat:
            return {"error": "請輸入包含『縣市、路街、號』的完整地址（例：台北市內湖區成功路四段180號6樓）"}

        city_code = "a"
        for city_name, code in CITY_CODE_MAP.items():
            if city_name in clean_input or city_name.replace("臺", "台") in address:
                city_code = code
                break

        road = feat['road']
        lane = feat['lane']
        alley = feat['alley']
        num = feat['num']
        target_floor = feat['floor']
        district = feat['district']

        road_df = fetch_road_deep_data(city_code, road)
        if road_df.empty:
            return {"error": f"查無『{district}{road}』之建物與實價資料，請確認縣市與路名是否正確。"}

        if district and not road_df[road_df['district'] == district].empty:
            road_df = road_df[road_df['district'] == district].copy()

        same_street_df = road_df[(road_df['p_lane'] == lane) & (road_df['p_alley'] == alley)].copy()
        if same_street_df.empty:
            same_street_df = road_df[road_df['p_lane'] == lane].copy()
        if same_street_df.empty:
            same_street_df = road_df.copy()

        exact_building = same_street_df[same_street_df['p_num'] == num].copy()
        adj_building = same_street_df[
            (same_street_df['p_num'] % 2 == num % 2) &
            ((same_street_df['p_num'] - num).abs() <= 8)
        ].copy()

        if not exact_building.empty:
            b_pool = exact_building
            match_level = f"✅ 已調出本棟門牌【{road}{lane+'巷' if lane else ''}{num}號】建物規格檔案"
        elif not adj_building.empty:
            if target_floor > 0:
                vf = adj_building[adj_building['total_floors_num'] >= target_floor]
                if not vf.empty:
                    adj_building = vf
            adj_building['num_diff'] = (adj_building['p_num'] - num).abs()
            adj_building = adj_building.sort_values('num_diff')
            nearest_ym = adj_building.iloc[0]['build_ym']
            b_pool = adj_building[adj_building['build_ym'] == nearest_ym] if nearest_ym else adj_building
            match_level = f"✅ 已由同社區連號門牌【{b_pool.iloc[0]['p_num']}號】調出本社區建物規格檔案"
        else:
            cand = same_street_df.copy()
            if target_floor > 0:
                vf = cand[cand['total_floors_num'] >= target_floor]
                if not vf.empty:
                    cand = vf
            cand['num_diff'] = (cand['p_num'] - num).abs()
            cand = cand.sort_values('num_diff')
            nearest_ym = cand.iloc[0]['build_ym']
            b_pool = cand[cand['build_ym'] == nearest_ym] if nearest_ym else cand.head(5)
            match_level = f"ℹ️ 已由同路段緊鄰建物【{b_pool.iloc[0]['p_num']}號】推估建物規格（可點選下方房型按鈕切換）"

        ref_b = b_pool.iloc[0]
        target_build_ym = str(ref_b['build_ym'])
        target_age = int(ref_b['age']) if ref_b['age'] >= 0 else 15
        target_total_floors = int(b_pool['total_floors_num'].mode().iloc[0]) if not b_pool['total_floors_num'].mode().empty else int(ref_b['total_floors_num'])
        b_type = str(ref_b['b_type'])
        usage = str(ref_b['usage'])
        mgmt = "有管委會" if "有" in str(ref_b['mgmt']) else "無管委會"

        def is_same_community(row):
            if row['p_road'] != road or row['p_lane'] != lane:
                return False
            if row['p_num'] == num:
                return True
            if target_build_ym and len(target_build_ym) >= 5 and row['build_ym'] == target_build_ym:
                if abs(row['p_num'] - num) <= 50:
                    return True
            if (row['p_num'] % 2 == num % 2) and abs(row['p_num'] - num) <= 10:
                if target_total_floors > 0 and row['total_floors_num'] == target_total_floors and abs(row['age'] - target_age) <= 1:
                    return True
            return False

        road_df['is_same_comm'] = road_df.apply(is_same_community, axis=1)
        comm_all_history = road_df[road_df['is_same_comm']].copy()
        if comm_all_history.empty:
            comm_all_history = b_pool.copy()

        comm_pings = comm_all_history[comm_all_history['floor_num'] != 1]['house_ping']
        if comm_pings.empty:
            comm_pings = comm_all_history['house_ping']

        ping_options = sorted(list(set(comm_pings.round(0).astype(int).tolist())))
        merged_options = []
        for p in ping_options:
            if not merged_options or (p - merged_options[-1]) >= 3:
                cluster_val = round(float(comm_pings[(comm_pings >= p - 1.5) & (comm_pings <= p + 1.5)].median()), 1)
                merged_options.append(cluster_val)

        same_floor_df = comm_all_history[comm_all_history['floor_num'] == target_floor] if target_floor > 0 else pd.DataFrame()
        if custom_ping > 0:
            target_ping = custom_ping
        elif not exact_building[exact_building['floor_num'] == target_floor].empty:
            target_ping = round(float(exact_building[exact_building['floor_num'] == target_floor].iloc[0]['house_ping']), 1)
        elif not same_floor_df.empty:
            target_ping = round(float(same_floor_df['house_ping'].median()), 1)
        else:
            target_ping = round(float(comm_pings.median()), 1)

        comm_all_history['ping_diff'] = (comm_all_history['house_ping'] - target_ping).abs()
        best_spec_row = comm_all_history.sort_values('ping_diff').iloc[0]

        scale_factor = target_ping / best_spec_row['house_ping'] if best_spec_row['house_ping'] > 0 else 1.0
        main_ping = round(float(best_spec_row['main_ping']) * scale_factor, 1)
        sub_ping = round(float(best_spec_row['sub_ping']) * scale_factor, 1)
        pub_ping = round(max(0.0, target_ping - main_ping - sub_ping), 1)
        pub_ratio = round((pub_ping / target_ping) * 100, 1) if target_ping > 0 else 32.0
        layout = f"{best_spec_row['rooms']}房{best_spec_row['halls']}廳{best_spec_row['baths']}衛"

        comm_nums = sorted(comm_all_history['p_num'].unique().tolist())
        comm_addr_range = f"{road}{lane+'巷' if lane else ''} {comm_nums[0]}號 ~ {comm_nums[-1]}號" if len(comm_nums) > 1 else f"{road}{lane+'巷' if lane else ''}{num}號"
        floors_display = f"{target_floor}樓 / 共 {target_total_floors} 樓" if (target_floor > 0 and target_total_floors > 0) else f"共 {target_total_floors} 樓"

        latest_db_month = int(road_df['month_val'].max())
        one_year_cutoff = latest_db_month - 12

        min_ping = round(target_ping * (1 - range_ratio), 1)
        max_ping = round(target_ping * (1 + range_ratio), 1)

        comm_1yr_df = comm_all_history[comm_all_history['month_val'] >= one_year_cutoff].copy()
        if target_floor != 1:
            comm_1yr_df = comm_1yr_df[comm_1yr_df['floor_num'] != 1]
        comm_1yr_df = comm_1yr_df.sort_values('month_val', ascending=False)

        comm_note = f"本社區近一年內共成交 {len(comm_1yr_df)} 筆"
        if comm_1yr_df.empty and not comm_all_history.empty:
            comm_1yr_df = comm_all_history[comm_all_history['floor_num'] != 1].sort_values('month_val', ascending=False).head(10).copy()
            comm_note = f"本社區近一年無交易，已調出本社區歷年成交共 {len(comm_1yr_df)} 筆"

        nearby_1yr_df = road_df[
            (~road_df['is_same_comm']) &
            (road_df['month_val'] >= one_year_cutoff) &
            (road_df['house_ping'] >= min_ping) &
            (road_df['house_ping'] <= max_ping) &
            ((road_df['age'] - target_age).abs() <= 6) &
            (road_df['b_type'] == b_type)
        ].copy()
        if target_floor != 1:
            nearby_1yr_df = nearby_1yr_df[nearby_1yr_df['floor_num'] != 1]
        nearby_1yr_df['num_dist'] = (nearby_1yr_df['p_num'] - num).abs()
        nearby_1yr_df = nearby_1yr_df.sort_values(['num_dist', 'month_val'], ascending=[True, False])

        comps_combined = pd.concat([comm_1yr_df, nearby_1yr_df], ignore_index=True)
        if comps_combined.empty:
            comps_combined = road_df[road_df['month_val'] >= one_year_cutoff].sort_values('month_val', ascending=False).head(15).copy()

        comm_1yr_fit_ping = comm_1yr_df[(comm_1yr_df['house_ping'] >= min_ping) & (comm_1yr_df['house_ping'] <= max_ping)]
        if len(comm_1yr_fit_ping) >= 2:
            stat_df = comm_1yr_fit_ping
            val_basis = f"依【本社區同坪數 ({len(stat_df)}筆)】成交行情計算"
        elif len(comm_1yr_df) >= 1:
            stat_df = pd.concat([comm_1yr_df, nearby_1yr_df.head(5)], ignore_index=True)
            val_basis = f"依【本社區 ({len(comm_1yr_df)}筆) ＋ 緊鄰同屋齡社區】行情計算"
        else:
            stat_df = comps_combined.head(12)
            val_basis = f"依【同路段相近屋齡坪數 ({len(stat_df)}筆)】一年內行情計算"

        q = stat_df['real_unit_price'].quantile([0.25, 0.5, 0.75]).to_dict()
        low_u, med_u, high_u = round(q[0.25], 1), round(q[0.5], 1), round(q[0.75], 1)
        avg_u = round(float(stat_df['real_unit_price'].mean()), 1)

        comp_list = []
        for _, r in comps_combined.head(30).iterrows():
            d_str = str(r['date_str'])
            f_date = f"{d_str[:-4]}/{d_str[-4:-2]}" if len(d_str) >= 5 else d_str
            f_num = r['floor_num']
            t_num = r['total_floors_num']
            fl_display = f"{f_num}F / {t_num}F" if (f_num > 0 and t_num > 0) else f"{r['floor_raw']}/{r['total_floors_raw']}"
            is_recent_1yr = bool(r['month_val'] >= one_year_cutoff)
            comp_list.append({
                "date": f_date,
                "is_1yr": is_recent_1yr,
                "addr": r['clean_addr'],
                "b_type": r['b_type'],
                "age": f"{r['age']}年" if r['age'] >= 0 else "-",
                "build_ym": r['build_ym'][:5] if len(r['build_ym']) >= 5 else "-",
                "ping": round(float(r['house_ping']), 1),
                "main_ping": round(float(r['main_ping']), 1),
                "total_ping": round(float(r['total_ping']), 1),
                "floor": fl_display,
                "unit": round(float(r['real_unit_price']), 1),
                "total": round(float(r['total_price']), 0),
                "park": round(float(r['parking_price']), 0),
                "layout": f"{r['rooms']}房{r['halls']}廳",
                "is_same": bool(r['is_same_comm'])
            })

        return {
            "target": {
                "addr": clean_input,
                "comm_range": comm_addr_range,
                "ping": target_ping,
                "main_ping": main_ping,
                "sub_ping": sub_ping,
                "pub_ping": pub_ping,
                "pub_ratio": pub_ratio,
                "ping_options": merged_options,
                "min_ping": min_ping, "max_ping": max_ping,
                "age": target_age,
                "build_ym": f"民國 {target_build_ym[:-4]}年{target_build_ym[-4:-2]}月" if len(target_build_ym) >= 5 else "未知",
                "floors": floors_display,
                "type": b_type,
                "usage": usage,
                "mgmt": mgmt,
                "layout": layout,
                "note": match_level,
                "comm_note": comm_note,
                "basis": val_basis
            },
            "val": {
                "count": len(comps_combined),
                "same_count": len(comm_1yr_df),
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
    <title>好房 AI 房屋規格調閱與同社區一年內行情系統 v5.1</title>
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
                <div class="bg-blue-600 p-2 rounded-lg"><i class="fa-solid fa-building-shield text-xl text-white"></i></div>
                <div>
                    <span class="font-black text-lg tracking-wide">好房 AI 房屋建籍 ＆ 同社區行情系統</span>
                    <span class="hidden md:inline-block ml-2 text-xs bg-emerald-500/20 text-emerald-300 border border-emerald-400/30 px-2 py-0.5 rounded-full">v5.1 免本戶實價即可調出規格＋鎖定同社區一年內行情</span>
                </div>
            </div>
        </div>
    </nav>

    <div class="bg-gradient-to-r from-slate-900 via-blue-900 to-indigo-900 text-white py-7 px-4 shadow-inner">
        <div class="max-w-7xl mx-auto">
            <h1 class="text-2xl md:text-3xl font-extrabold mb-2">輸入地址 ➡️ 直接調出這間房屋規格 ➡️ 比對「同社區一年內成交行情」</h1>
            <p class="text-blue-200 text-xs md:text-sm mb-5">即使這間房子從未買賣過，系統也能調出該棟大樓規格（主建物、公設、完工年月），並鎖定同社區近一年成交價！</p>

            <div class="bg-white p-4 rounded-2xl shadow-xl text-slate-800 grid grid-cols-1 md:grid-cols-12 gap-3 items-end">
                <div class="md:col-span-5">
                    <label class="text-xs font-bold text-slate-500 block mb-1">📍 輸入目標房屋地址 (有無成交過皆可查)</label>
                    <input type="text" id="addr" value="台北市內湖區成功路四段180號6樓" 
                           placeholder="例：新北市板橋區文化路一段188號10樓"
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3.5 py-2.5 text-sm font-bold focus:outline-none focus:ring-2 focus:ring-blue-600">
                </div>
                <div class="md:col-span-2">
                    <label class="text-xs font-bold text-slate-500 block mb-1">📐 扣車位坪數 (選填)</label>
                    <input type="number" id="ping" placeholder="自動調閱本棟坪數" 
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3 py-2.5 text-sm font-bold focus:outline-none">
                </div>
                <div class="md:col-span-2">
                    <label class="text-xs font-bold text-slate-500 block mb-1">🚗 車位價值 (萬元)</label>
                    <input type="number" id="park" value="0" 
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3 py-2.5 text-sm font-bold focus:outline-none">
                </div>
                <div class="md:col-span-1">
                    <label class="text-xs font-bold text-slate-500 block mb-1">🔍 坪數範圍</label>
                    <select id="ratio" class="w-full bg-slate-50 border border-slate-300 rounded-xl px-2 py-2.5 text-sm font-bold focus:outline-none">
                        <option value="0.15">±15%</option>
                        <option value="0.25" selected>±25%</option>
                        <option value="0.35">±3
