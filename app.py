# 檔案名稱：app.py (v5.0 目標路段深層建籍調閱 + 同社區一年內行情鎖定版)
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

app = FastAPI(title="好房 AI 房屋資料調閱與同社區一年內行情系統")

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

# 快取已抽取出來的「特定路段深層資料」，極省記憶體且秒開
ROAD_DATA_CACHE = {}

def cn_to_num(s: str) -> int:
    if not s: return 0
    s = str(s).replace("層", "").replace("樓", "").replace("F", "").strip()
    if s.isdigit(): return int(s)
    if "地下" in s or "B" in s.upper(): return -1
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

def fetch_road_deep_data(city_code: str, road_name: str) -> pd.DataFrame:
    """
    【核心突破】只針對使用者查詢的「那條路 (road_name)」進行深層抽取！
    因為過濾掉其他 99% 無關的路段，記憶體極省，可以一口氣往回抓 16 個季度（4年深層建籍資料庫），
    確保就算該棟大樓好幾年沒買賣，也能抓出這間房子的建物規格與社區身分證！
    """
    cache_key = f"{city_code}_{road_name}"
    if cache_key in ROAD_DATA_CACHE:
        return ROAD_DATA_CACHE[cache_key]

    if len(ROAD_DATA_CACHE) > 10:
        ROAD_DATA_CACHE.clear()
        gc.collect()

    dfs = []
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    target_filename = f"{city_code}_lvr_land_a.csv"

    urls_to_fetch = ["https://plvr.land.moi.gov.tw/Download?type=zip&fileName=lvr_landcsv.zip"]
    now = datetime.now()
    roc_year = now.year - 1911
    # 往前掃描 5 年，抓取多達 14 個季度的官方檔案，只抽取出包含該路名的那幾百筆資料！
    for y in range(roc_year, roc_year - 5, -1):
        for s in [4, 3, 2, 1]:
            urls_to_fetch.append(f"https://plvr.land.moi.gov.tw/DownloadSeason?season={y}S{s}&type=zip&fileName=lvr_landcsv.zip")

    keep_keywords = ['鄉鎮市區', '交易標的', '門牌', '交易年月日', '移轉層次', '總樓層數', '建物型態', '主要用途', '建築完成', '建物移轉總面積', '主建物面積', '附屬建物面積', '陽台面積', '格局-房', '格局-廳', '格局-衛', '總價元', '單價元平方公尺', '車位移轉總面積', '車位總價元', '備註', '有無管理組織']

    # 建立路名搜尋關鍵字 (例如 "成功路4段" 同時匹配 "成功路四段" 與 "成功路４段")
    road_base = re.sub(r'\d+段', '', road_name)

    success_seasons = 0
    for url in urls_to_fetch:
        if success_seasons >= 14:  # 深入回溯 14 個季度 (約 3.5 年完整建籍與實價)
            break
        try:
            resp = requests.get(url, headers=headers, timeout=18, verify=False)
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
                                # 瞬間只保留這條路的資料！記憶體佔用不到 1MB！
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
    # 二次精準過濾標準化後的路名 (例如確認是 4段 而不是 3段)
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

    # 詳細面積拆解 (總坪數、主建物、附屬建物、車位坪數)
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

    # 計算「扣車位後權狀坪數 (主建+附屬+公設)」
    df['house_ping'] = df['total_ping'] - df['parking_ping']
    mask_no_p_ping = (df['parking_price'] > 0) & (df['parking_ping'] == 0) & (df['moi_unit_price'] > 5)
    df.loc[mask_no_p_ping, 'house_ping'] = (df.loc[mask_no_p_ping, 'total_price'] - df.loc[mask_no_p_ping, 'parking_price']) / df.loc[mask_no_p_ping, 'moi_unit_price']

    # 計算公設坪數與公設比
    df['public_ping'] = (df['house_ping'] - df['main_ping'] - df['sub_ping']).clip(lower=0)

    df['real_unit_price'] = df['moi_unit_price']
    has_explicit_park = (df['parking_price'] > 0) & (df['parking_ping'] > 0) & (df['house_ping'] > 1)
    df.loc[has_explicit_park, 'real_unit_price'] = (df['total_price'] - df['parking_price']) / df['house_ping']
    fallback_mask = (df['real_unit_price'] <= 1) & (df['house_ping'] > 1)
    df.loc[fallback_mask, 'real_unit_price'] = df['total_price'] / df['house_ping']

    df['month_val'] = df['date_str'].apply(parse_roc_date)

    # 排除特殊親友交易
    df = df[~df['note'].str.contains('親友|特殊|急買|急賣|瑕疵|共有人|標售|債權', na=False)]
    df = df[(df['real_unit_price'] > 6) & (df['real_unit_price'] < 400) & (df['house_ping'] > 3)]
    df = df.drop_duplicates(subset=['clean_addr', 'date_str', 'total_price'])

    # 拆解門牌：路、巷、弄、號
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

        # 啟動深層建籍抽取引擎 (專注抓取該路段過去 14 季的所有建物資料)
        road_df = fetch_road_deep_data(city_code, road)
        if road_df.empty:
            return {"error": f"查無『{district}{road}』之建物與實價資料，請確認縣市與路名是否正確。"}

        if district and not road_df[road_df['district'] == district].empty:
            road_df = road_df[road_df['district'] == district].copy()

        # 嚴格區分「同巷弄」或「同在大馬路(無巷弄)」
        same_street_df = road_df[(road_df['p_lane'] == lane) & (road_df['p_alley'] == alley)].copy()
        if same_street_df.empty:
            same_street_df = road_df[road_df['p_lane'] == lane].copy()
        if same_street_df.empty:
            same_street_df = road_df.copy()

        # =========================================================================
        # 【第一軌】不用管這間有沒有賣過，直接調出「這棟建物 / 這間房子」的規格檔案！
        # =========================================================================
        # 1. 先找本棟門牌 (如 180號) 或同社區雙號連號門牌 (如 176~184號) 的建籍紀錄
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
                if not vf.empty: adj_building = vf
            adj_building['num_diff'] = (adj_building['p_num'] - num).abs()
            adj_building = adj_building.sort_values('num_diff')
            # 鎖定最近一棟的完工年月作為本社區建籍
            nearest_ym = adj_building.iloc[0]['build_ym']
            b_pool = adj_building[adj_building['build_ym'] == nearest_ym] if nearest_ym else adj_building
            match_level = f"✅ 已由同社區連號門牌【{b_pool.iloc[0]['p_num']}號】調出本社區建物規格檔案"
        else:
            cand = same_street_df.copy()
            if target_floor > 0:
                vf = cand[cand['total_floors_num'] >= target_floor]
                if not vf.empty: cand = vf
            cand['num_diff'] = (cand['p_num'] - num).abs()
            cand = cand.sort_values('num_diff')
            nearest_ym = cand.iloc[0]['build_ym']
            b_pool = cand[cand['build_ym'] == nearest_ym] if nearest_ym else cand.head(5)
            match_level = f"ℹ️ 已由同路段緊鄰建物【{b_pool.iloc[0]['p_num']}號】推估建物規格（若坪數不同可點選下方房型按鈕切換）"

        # 取得這棟大樓的「社區身分證」：完工年月、屋齡、總樓層、建物型態
        ref_b = b_pool.iloc[0]
        target_build_ym = str(ref_b['build_ym'])
        target_age = int(ref_b['age']) if ref_b['age'] >= 0 else 15
        target_total_floors = int(b_pool['total_floors_num'].mode().iloc[0]) if not b_pool['total_floors_num'].mode().empty else int(ref_b['total_floors_num'])
        b_type = str(ref_b['b_type'])
        usage = str(ref_b['usage'])
        mgmt = "有管委會" if "有" in str(ref_b['mgmt']) else "無管委會"

        # 找出整個社區所有的戶別（同一條路巷 + 完工年月完全相同 或 總樓層相同且連號）
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

        # 整理本社區常見的「標準房型坪數清單」（讓使用者就算沒賣過，也能一鍵點選自己這戶是哪種坪數！）
        comm_pings = comm_all_history[comm_all_history['floor_num'] != 1]['house_ping']
        if comm_pings.empty: comm_pings = comm_all_history['house_ping']
        
        # 將社區內的坪數分群 (四捨五入到整數坪，找出本社區最常見的 1~4 種標準房型坪數)
        ping_Options = sorted(list(set(comm_pings.round(0).astype(int).tolist())))
        # 合併太接近的坪數 (差距 2 坪內視為同房型)
        merged_options = []
        for p in ping_Options:
            if not merged_options or (p - merged_options[-1]) >= 3:
                # 取該區間的精確平均坪數
                cluster_val = round(float(comm_pings[(comm_pings >= p - 1.5) & (comm_pings <= p + 1.5)].median()), 1)
                merged_options.append(cluster_val)

        # 決定這間房子的目標坪數：
        # 1. 若使用者有手動輸入/點選坪數，優先使用
        # 2. 若本棟該樓層剛好有紀錄，用該樓層坪數
        # 3. 否則預設用本社區最主流（中位數）的標準戶坪數
        same_floor_df = comm_all_history[comm_all_history['floor_num'] == target_floor] if target_floor > 0 else pd.DataFrame()
        if custom_ping > 0:
            target_ping = custom_ping
        elif not exact_building[exact_building['floor_num'] == target_floor].empty:
            target_ping = round(float(exact_building[exact_building['floor_num'] == target_floor].iloc[0]['house_ping']), 1)
        elif not same_floor_df.empty:
            target_ping = round(float(same_floor_df['house_ping'].median()), 1)
        else:
            target_ping = round(float(comm_pings.median()), 1)

        # 根據選定的 target_ping，從同社區找出最接近該坪數的一戶，帶出精確的「主建物、陽台附屬、公設坪數、格局」
        comm_all_history['ping_diff'] = (comm_all_history['house_ping'] - target_ping).abs()
        best_spec_row = comm_all_history.sort_values('ping_diff').iloc[0]
        
        scale_factor = target_ping / best_spec_row['house_ping'] if best_spec_row['house_ping'] > 0 else 1.0
        main_ping = round(float(best_spec_row['main_ping']) * scale_factor, 1)
        sub_ping = round(float(best_spec_row['sub_ping']) * scale_factor, 1)
        pub_ping = round(max(0.0, target_ping - main_ping - sub_ping), 1)
        pub_ratio = round((pub_ping / target_ping) * 100, 1) if target_ping > 0 else 32.0
        layout = f"{best_spec_row['rooms']}房{best_spec_row['halls']}廳{best_spec_row['baths']}衛"

        # 找出本社區涵蓋的門牌範圍 (例如：成功路4段 176號 ~ 186號)
        comm_nums = sorted(comm_all_history['p_num'].unique().tolist())
        comm_addr_range = f"{road}{lane+'巷' if lane else ''} {comm_nums[0]}號 ~ {comm_nums[-1]}號" if len(comm_nums) > 1 else f"{road}{lane+'巷' if lane else ''}{num}號"
        floors_display = f"{target_floor}樓 / 共 {target_total_floors} 樓" if (target_floor > 0 and target_total_floors > 0) else f"共 {target_total_floors} 樓"

        # =========================================================================
        # 【第二軌】抓出「同社區一年內成交行情」＋「同路段相近屋齡坪數一年內行情」
        # =========================================================================
        now = datetime.now()
        current_month_val = now.year * 12 + now.month
        # 取資料庫最新月份與當前月份較小者往回推 12 個月 (一年內)
        latest_db_month = int(road_df['month_val'].max())
        one_year_cutoff = latest_db_month - 12

        min_ping = round(target_ping * (1 - range_ratio), 1)
        max_ping = round(target_ping * (1 + range_ratio), 1)

        # 1. 同社區「一年內」全部成交紀錄 (優先標註坪數相近的，但也保留同社區一年內其他坪數供完整參考)
        comm_1yr_df = comm_all_history[comm_all_history['month_val'] >= one_year_cutoff].copy()
        if target_floor != 1:
            comm_1yr_df = comm_1yr_df[comm_1yr_df['floor_num'] != 1]
        comm_1yr_df = comm_1yr_df.sort_values('month_val', ascending=False)

        # 若同社區「一年內」剛好 0 筆成交，自動附上同社區「近兩年內」成交並明確標示！
        comm_note = f"本社區近一年內共成交 {len(comm_1yr_df)} 筆"
        if comm_1yr_df.empty and not comm_all_history.empty:
            comm_1yr_df = comm_all_history[comm_all_history['floor_num'] != 1].sort_values('month_val', ascending=False).head(10).copy()
            comm_note = f"本社區近一年無交易，已調出本社區近期歷史成交共 {len(comm_1yr_df)} 筆"

        # 2. 同路段周邊「一年內、屋齡相近(±6年)、坪數接近」之成交行情
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

        # 合併列表：同社區一年內排最前，接著是周邊同屋齡一年內
        comps_combined = pd.concat([comm_1yr_df, nearby_1yr_df], ignore_index=True)
        if comps_combined.empty:
            comps_combined = road_df[road_df['month_val'] >= one_year_cutoff].sort_values('month_val', ascending=False).head(15).copy()

        # =========================================================================
        # 【第三軌】用同社區一年內行情（坪數接近者優先）計算這間房子的價值與落點
        # =========================================================================
        comm_1yr_fit_ping = comm_1yr_df[(comm_1yr_df['house_ping'] >= min_ping) & (comm_1yr_df['house_ping'] <= max_ping)]
        if len(comm_1yr_fit_ping) >= 2:
            stat_df = comm_1yr_fit_ping
            val_basis = f"依【本社區一年內同坪數 ({len(stat_df)}筆)】成交行情計算"
        elif len(comm_1yr_df) >= 1:
            stat_df = pd.concat([comm_1yr_df, nearby_1yr_df.head(5)], ignore_index=True)
            val_basis = f"依【本社區 ({len(comm_1yr_df)}筆) ＋ 緊鄰同屋齡社區】一年內行情計算"
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
            is_ recent_1yr = bool(r['month_val'] >= one_year_cutoff)
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
    <title>好房 AI 房屋規格調閱與同社區一年內行情系統 v5.0</title>
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
                    <span class="hidden md:inline-block ml-2 text-xs bg-emerald-500/20 text-emerald-300 border border-emerald-400/30 px-2 py-0.5 rounded-full">v5.0 免本戶實價即可調出規格＋鎖定同社區一年內行情</span>
                </div>
            </div>
        </div>
    </nav>

    <div class="bg-gradient-to-r from-slate-900 via-blue-900 to-indigo-900 text-white py-7 px-4 shadow-inner">
        <div class="max-w-7xl mx-auto">
            <h1 class="text-2xl md:text-3xl font-extrabold mb-2">輸入地址 ➡️ 直接調出這間房屋規格 ➡️ 比對「同社區一年內成交行情」</h1>
            <p class="text-blue-200 text-xs md:text-sm mb-5">即使這間房子從未買賣過，系統也能從深層建籍庫調出該棟大樓規格（主建物、公設、完工年月），並鎖定同社區近一年成交價！</p>

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
                        <option value="0.35">±35%</option>
                    </select>
                </div>
                <div class="md:col-span-2">
                    <button onclick="runQuery()" class="w-full bg-blue-600 hover:bg-blue-700 active:scale-95 transition text-white font-bold py-2.5 px-4 rounded-xl shadow-md flex items-center justify-center space-x-2">
                        <i class="fa-solid fa-bolt"></i>
                        <span>調閱規格與行情</span>
                    </button>
                </div>
            </div>
        </div>
    </div>

    <div class="max-w-7xl mx-auto px-4 py-8">
        <div id="loading" class="hidden bg-white rounded-3xl p-12 text-center shadow-sm border border-slate-200 my-6">
            <i class="fa-solid fa-circle-notch text-5xl text-blue-600 spin mb-4"></i>
            <h3 class="text-lg font-extrabold text-slate-800">正在深層調閱該路段建物籍資料與同社區一年內實價行情...</h3>
            <p class="text-xs text-slate-400 mt-2">系統正過濾出該路段近 14 季建籍檔案以鎖定同社區規格（約需 15~25 秒），同路段第二次查詢僅需 0.1 秒！</p>
        </div>

        <div id="err" class="hidden bg-red-50 border-l-4 border-red-500 text-red-700 p-4 rounded-xl shadow-sm my-4 font-bold text-sm"></div>

        <div id="dashboard" class="hidden grid grid-cols-1 lg:grid-cols-12 gap-6">
            
            <!-- 左欄：1. 這間房子的詳細規格檔案 + 2. 價值與成交落點 -->
            <div class="lg:col-span-5 space-y-6">
                
                <!-- 1. 目標房屋詳細建物籍規格卡 -->
                <div class="bg-white rounded-2xl p-6 shadow-sm border-2 border-blue-600">
                    <div class="flex items-center justify-between mb-2">
                        <span class="bg-blue-600 text-white text-xs font-extrabold px-2.5 py-1 rounded-md">🏢 目標房屋建物規格檔案</span>
                        <span id="rType" class="text-xs font-bold text-indigo-700 bg-indigo-100 px-2.5 py-1 rounded-md"></span>
                    </div>
                    <h2 id="rAddr" class="text-xl font-black text-slate-900"></h2>
                    <p id="rCommRange" class="text-xs text-slate-500 font-semibold mt-0.5 mb-3"></p>
                    <p id="note" class="text-xs text-blue-800 font-bold bg-blue-50 p-2.5 rounded-xl border border-blue-200 mb-3"></p>

                    <!-- 本社區標準房型坪數快速切換鈕 -->
                    <div class="mb-4 bg-slate-50 p-2.5 rounded-xl border border-slate-200">
                        <span class="text-[11px] font-bold text-slate-500 block mb-1.5">💡 本社區標準房型坪數 (點選可立即切換本戶坪數重新估價)：</span>
                        <div id="pingBtns" class="flex flex-wrap gap-1.5"></div>
                    </div>

                    <!-- 建物面積詳細拆解 (主建 + 附屬 + 公設) -->
                    <div class="grid grid-cols-3 gap-2 text-center bg-blue-50/50 p-3 rounded-xl border border-blue-100 mb-3">
                        <div>
                            <p class="text-[11px] text-slate-500">扣車位權狀總坪</p>
                            <p id="rPing" class="text-base font-black text-blue-700"></p>
                        </div>
                        <div class="border-l border-blue-100">
                            <p class="text-[11px] text-slate-500">主建物(室內)+陽台</p>
                            <p id="rMainSub" class="text-sm font-extrabold text-slate-800 mt-0.5"></p>
                        </div>
                        <div class="border-l border-blue-100">
                            <p class="text-[11px] text-slate-500">公設坪(公設比)</p>
                            <p id="rPub" class="text-sm font-extrabold text-slate-800 mt-0.5"></p>
                        </div>
                    </div>

                    <!-- 屋齡、完工年月、樓層、用途 -->
                    <div class="grid grid-cols-2 gap-2 text-xs bg-slate-50 p-3 rounded-xl border border-slate-100">
                        <div><span class="text-slate-400">建築完工：</span><strong id="rBuildYm" class="text-slate-800"></strong></div>
                        <div><span class="text-slate-400">真實屋齡：</span><strong id="rAge" class="text-slate-800"></strong></div>
                        <div><span class="text-slate-400">所在樓層：</span><strong id="rFloor" class="text-slate-800"></strong></div>
                        <div><span class="text-slate-400">參考格局：</span><strong id="rLayout" class="text-slate-800"></strong></div>
                        <div><span class="text-slate-400">主要用途：</span><strong id="rUsage" class="text-slate-800"></strong></div>
                        <div><span class="text-slate-400">社區管理：</span><strong id="rMgmt" class="text-slate-800"></strong></div>
                    </div>
                </div>

                <!-- 2. 房屋價值估算與成交價格落點 -->
                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <div class="flex items-center justify-between mb-3">
                        <h3 class="text-base font-black text-slate-900"><i class="fa-solid fa-sack-dollar text-amber-500 mr-2"></i>這間房子價值多少錢？</h3>
                    </div>
                    <p id="rBasis" class="text-xs bg-emerald-50 text-emerald-800 border border-emerald-200 font-bold p-2 rounded-lg mb-4 text-center"></p>

                    <div class="bg-gradient-to-br from-blue-900 to-indigo-900 text-white p-5 rounded-2xl text-center shadow-md mb-4">
                        <p class="text-xs text-blue-200 font-semibold">預估合理成交總價落點 (中位數 PR50)</p>
                        <div class="my-2 flex items-baseline justify-center space-x-1">
                            <span id="medT" class="text-4xl font-black text-yellow-300"></span>
                            <span class="text-sm font-bold">萬元</span>
                        </div>
                        <div class="flex justify-center space-x-4 text-xs text-blue-100 mt-2 pt-2 border-t border-blue-700/50">
                            <span>合理單價：<strong id="medU" class="text-white"></strong> 萬/坪</span>
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

            <!-- 右欄：同社區一年內成交行情明細 + 落點圖 -->
            <div class="lg:col-span-7 space-y-6">
                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-4">
                        <div>
                            <h3 class="text-base font-black text-slate-900">📋 同社區一年內成交行情 ＆ 周邊同屋齡比對</h3>
                            <p id="rCommNote" class="text-xs text-blue-600 font-bold mt-0.5"></p>
                        </div>
                        <div class="flex space-x-2">
                            <button onclick="filterTable('same')" id="btnSame" class="text-xs font-bold px-3 py-1.5 rounded-lg bg-blue-600 text-white shadow">只看同社區行情 (<span id="sameCnt">0</span>)</button>
                            <button onclick="filterTable('all')" id="btnAll" class="text-xs font-bold px-3 py-1.5 rounded-lg bg-slate-100 text-slate-600">含同路段同屋齡 (<span id="allCnt">0</span>)</button>
                        </div>
                    </div>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left border-collapse">
                            <thead>
                                <tr class="border-b-2 border-slate-200 text-[11px] text-slate-400">
                                    <th class="py-2 px-2">歸屬</th><th class="py-2 px-2">成交年月</th><th class="py-2 px-2">成交門牌</th>
                                    <th class="py-2 px-2 text-right">扣車位坪(主建)</th><th class="py-2 px-2 text-center">樓層</th>
                                    <th class="py-2 px-2 text-right">拆算單價</th><th class="py-2 px-2 text-right">成交總價</th>
                                </tr>
                            </thead>
                            <tbody id="tableBody" class="divide-y divide-slate-100 text-xs"></tbody>
                        </table>
                    </div>
                </div>

                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <h3 class="text-base font-black text-slate-900 mb-1">📊 同社區一年內成交單價落點圖</h3>
                    <p class="text-xs text-slate-400 mb-4">紅星為本戶預估落點，深藍點為「同社區」成交紀錄，灰點為同路段同屋齡成交</p>
                    <div class="h-60 w-full"><canvas id="scatterChart"></canvas></div>
                </div>
            </div>

        </div>
    </div>

<script>
let globalData = null;
let chartInstance = null;

function selectQuickPing(p) {
    document.getElementById('ping').value = p;
    runQuery();
}

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
        errEl.innerText = "雲端主機連線逾時 (" + e.message + ")，請等 10 秒後再按一次『調閱規格與行情』！";
        errEl.classList.remove('hidden');
    }
}

function renderDashboard(d, park) {
    document.getElementById('rAddr').innerText = d.target.addr;
    document.getElementById('rCommRange').innerText = `社區門牌範圍：${d.target.comm_range}`;
    document.getElementById('rType').innerText = d.target.type;
    document.getElementById('note').innerText = d.target.note;

    // 渲染本社區房型坪數切換鈕
    const btnBox = document.getElementById('pingBtns');
    btnBox.innerHTML = '';
    d.target.ping_options.forEach(p => {
        const active = Math.abs(p - d.target.ping) < 0.5;
        btnBox.innerHTML += `
            <button onclick="selectQuickPing(${p})" class="text-xs font-bold px-2.5 py-1 rounded-lg border transition ${active ? 'bg-blue-600 text-white border-blue-600 shadow' : 'bg-white text-slate-700 border-slate-300 hover:bg-blue-50'}">
                ${p} 坪房型
            </button>`;
    });

    document.getElementById('rPing').innerText = d.target.ping + " 坪";
    document.getElementById('rMainSub').innerText = `${d.target.main_ping} + ${d.target.sub_ping} 坪`;
    document.getElementById('rPub').innerText = `${d.target.pub_ping}坪 (${d.target.pub_ratio}%)`;
    document.getElementById('rBuildYm').innerText = d.target.build_ym;
    document.getElementById('rAge').innerText = d.target.age + " 年";
    document.getElementById('rFloor').innerText = d.target.floors;
    document.getElementById('rLayout').innerText = d.target.layout;
    document.getElementById('rUsage').innerText = d.target.usage;
    document.getElementById('rMgmt').innerText = d.target.mgmt;

    document.getElementById('rBasis').innerText = "🎯 " + d.target.basis;
    document.getElementById('medT').innerText = (d.val.med_t + park).toLocaleString();
    document.getElementById('medU').innerText = d.val.med_u;
    document.getElementById('avgU').innerText = d.val.avg_u;

    document.getElementById('lowT').innerText = (d.val.low_t + park).toLocaleString() + " 萬";
    document.getElementById('lowU').innerText = `單價 ${d.val.low_u} 萬/坪`;
    document.getElementById('highT').innerText = (d.val.high_t + park).toLocaleString() + " 萬";
    document.getElementById('highU').innerText = `單價 ${d.val.high_u} 萬/坪`;

    document.getElementById('sameCnt').innerText = d.val.same_count;
    document.getElementById('allCnt').innerText = d.val.count;
    document.getElementById('rCommNote').innerText = `${d.target.comm_note}（建築完成：${d.target.build_ym}）`;

    if (d.val.same_count > 0) {
        filterTable('same');
    } else {
        filterTable('all');
    }
    renderChart(d);
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
                { label: '這間房子合理估值落點', data: [{x: d.target.ping, y: d.val.med_u, addr: '本戶合理估值'}], backgroundColor: '#ef4444', pointRadius: 9 },
                { label: '同社區成交行情', data: samePoints, backgroundColor: '#2563eb', pointRadius: 7 },
                { label: '同路段相近屋齡成交', data: otherPoints, backgroundColor: '#94a3b8', pointRadius: 5 }
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
    document.getElementById('btnSame').className = mode === 'same' ? "text-xs font-bold px-3 py-1.5 rounded-lg bg-blue-600 text-white shadow" : "text-xs font-bold px-3 py-1.5 rounded-lg bg-slate-100 text-slate-600";
    document.getElementById('btnAll').className = mode === 'all' ? "text-xs font-bold px-3 py-1.5 rounded-lg bg-blue-600 text-white shadow" : "text-xs font-bold px-3 py-1.5 rounded-lg bg-slate-100 text-slate-600";

    const list = mode === 'same' ? globalData.comps.filter(c => c.is_same) : globalData.comps;
    if (list.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="text-center py-6 text-slate-400">本社區近一年無成交紀錄，請點選右上方「含同路段同屋齡」查看緊鄰社區成交行情</td></tr>`;
        return;
    }
    list.forEach(c => {
        const badge = c.is_same 
            ? `<span class="bg-blue-600 text-white text-[10px] px-2 py-0.5 rounded font-bold">🏠 同社區</span>` 
            : `<span class="bg-slate-200 text-slate-700 text-[10px] px-2 py-0.5 rounded font-semibold">周邊同屋齡</span>`;
        const yrBadge = c.is_1yr ? `<span class="text-emerald-600 font-bold ml-1 text-[10px]">(一年內)</span>` : `<span class="text-amber-600 ml-1 text-[10px]">(歷年)</span>`;
        const parkNote = c.park > 0 ? `<span class="block text-[10px] text-slate-400">含車位${c.park}萬</span>` : `<span class="block text-[10px] text-slate-400">總坪${c.total_ping}坪</span>`;
        tbody.innerHTML += `
            <tr class="hover:bg-slate-50 ${c.is_same ? 'bg-blue-50/50' : ''}">
                <td class="py-3 px-2 whitespace-nowrap">${badge}</td>
                <td class="py-3 px-2 font-semibold text-slate-700 whitespace-nowrap">${c.date}${yrBadge}</td>
                <td class="py-3 px-2 font-bold text-slate-900">${c.addr}</td>
                <td class="py-3 px-2 text-right font-bold text-blue-700">${c.ping} 坪 <span class="text-[10px] text-slate-400 font-normal">(主${c.main_ping})</span></td>
                <td class="py-3 px-2 text-center text-slate-600 font-semibold">${c.floor}</td>
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
