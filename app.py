# 檔案名稱：app.py (v4.0 歷史門牌反查 + 同社區建築指紋精準鎖定版)
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

app = FastAPI(title="好房 AI 同社區實價精準比對系統")

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

# 快取已下載的縣市資料
CITY_DATA_CACHE = {}

def cn_to_num(s: str) -> int:
    """將中文數字 (如 '十二層', '二十五樓', '五') 轉為阿拉伯數字"""
    if not s: return 0
    s = str(s).replace("層", "").replace("樓", "").replace("F", "").strip()
    if s.isdigit(): return int(s)
    # 若包含地下層則回傳負數或0
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
    """將地址統一轉為標準半形與阿拉伯數字段/巷/弄/號/樓"""
    if not isinstance(addr, str): return ""
    addr = addr.replace("台", "臺").replace(" ", "").strip()
    full_to_half = str.maketrans("０１２３４５６７８９－～Ｆ", "0123456789-~F")
    addr = addr.translate(full_to_half)
    def repl_sec(m):
        return f"{cn_to_num(m.group(1))}{m.group(2)}"
    addr = re.sub(r'([一二三四五六七八九十]+)(段|樓|層|巷|弄|號)', repl_sec, addr)
    return addr

def parse_user_address(clean_addr: str):
    """精準拆解使用者輸入的地址：縣市、行政區、路街段、巷、弄、號、之幾、樓層"""
    # 移除郵遞區號與里鄰
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
    
    sub_num = m.group(7) or m.group(8) or ""
    fl_m = re.search(r'號.*?(\d+)[樓層F]', clean_addr)
    floor_num = int(fl_m.group(1)) if fl_m else 0
    
    return {
        "city": m.group(1) or "",
        "district": m.group(2) or "",
        "road": m.group(3) or "",
        "lane": m.group(4) or "",
        "alley": m.group(5) or "",
        "num": int(m.group(6)),
        "sub_num": sub_num,
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
    """
    載入內政部實價登錄資料（擴大抓取近 10 個季度＝2.5年歷史資料，
    同時每季在記憶體內立刻丟棄無關欄位，確保能查到該門牌歷史紀錄又不會爆記憶體！）
    """
    if city_code in CITY_DATA_CACHE:
        return CITY_DATA_CACHE[city_code]

    CITY_DATA_CACHE.clear()
    gc.collect()

    dfs = []
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    target_filename = f"{city_code}_lvr_land_a.csv"

    urls_to_fetch = ["https://plvr.land.moi.gov.tw/Download?type=zip&fileName=lvr_landcsv.zip"]
    now = datetime.now()
    roc_year = now.year - 1911
    # 往前掃描 3 年 (12個季度)，湊滿 8 個有效季度壓縮檔，確保能查到該社區歷史成交！
    for y in [roc_year, roc_year - 1, roc_year - 2, roc_year - 3]:
        for s in [4, 3, 2, 1]:
            urls_to_fetch.append(f"https://plvr.land.moi.gov.tw/DownloadSeason?season={y}S{s}&type=zip&fileName=lvr_landcsv.zip")

    keep_keywords = ['鄉鎮市區', '交易標的', '門牌', '交易年月日', '移轉層次', '總樓層數', '建物型態', '建築完成', '建物移轉總面積', '格局-房', '格局-廳', '總價元', '單價元平方公尺', '車位移轉總面積', '車位總價元', '備註']

    success_seasons = 0
    for url in urls_to_fetch:
        if success_seasons >= 8:  # 抓滿最新一期 + 過去7季 (約2年完整實價庫)
            break
        try:
            resp = requests.get(url, headers=headers, timeout=20, verify=False)
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
    df['note'] = get_col(raw_df, '備註', '').fillna('')

    # 從備註欄萃取社區簡稱 (如有寫社區名稱)
    df['total_ping'] = pd.to_numeric(get_col(raw_df, '建物移轉總面積', '0'), errors='coerce').fillna(0) * 0.3025
    df['parking_ping'] = pd.to_numeric(get_col(raw_df, '車位移轉總面積', '0'), errors='coerce').fillna(0) * 0.3025
    df['total_price'] = pd.to_numeric(raw_df['總價元'] if '總價元' in raw_df.columns else get_col(raw_df, '總價元', '0'), errors='coerce').fillna(0) / 10000
    df['parking_price'] = pd.to_numeric(get_col(raw_df, '車位總價元', '0'), errors='coerce').fillna(0) / 10000
    df['moi_unit_price'] = pd.to_numeric(get_col(raw_df, '單價元平方公尺', '0'), errors='coerce').fillna(0) * 0.0003305785

    del raw_df
    gc.collect()

    # 計算屋齡 (精確到年)
    current_roc_year = datetime.now().year - 1911
    def calc_age(ym):
        if isinstance(ym, str) and len(ym) >= 5 and ym[:-4].isdigit():
            return max(0, current_roc_year - int(ym[:-4]))
        return -1
    df['age'] = df['build_ym'].apply(calc_age)

    # 精確計算「扣除車位後的房屋權狀坪數 (主建+附屬+公設)」與「拆車位單價」
    df['house_ping'] = df['total_ping'] - df['parking_ping']
    # 若該筆交易有含車位，但車位坪數填0，利用內政部官方單價反推扣車位坪數
    mask_no_p_ping = (df['parking_price'] > 0) & (df['parking_ping'] == 0) & (df['moi_unit_price'] > 5)
    df.loc[mask_no_p_ping, 'house_ping'] = (df.loc[mask_no_p_ping, 'total_price'] - df.loc[mask_no_p_ping, 'parking_price']) / df.loc[mask_no_p_ping, 'moi_unit_price']

    df['real_unit_price'] = df['moi_unit_price']
    has_explicit_park = (df['parking_price'] > 0) & (df['parking_ping'] > 0) & (df['house_ping'] > 1)
    df.loc[has_explicit_park, 'real_unit_price'] = (df['total_price'] - df['parking_price']) / df['house_ping']
    fallback_mask = (df['real_unit_price'] <= 1) & (df['house_ping'] > 1)
    df.loc[fallback_mask, 'real_unit_price'] = df['total_price'] / df['house_ping']

    df['month_val'] = df['date_str'].apply(parse_roc_date)

    # 排除親友、特殊交易
    df = df[~df['note'].str.contains('親友|特殊|急買|急賣|瑕疵|共有人|標售|債權', na=False)]
    df = df[(df['real_unit_price'] > 6) & (df['real_unit_price'] < 400) & (df['house_ping'] > 3)]
    df = df.drop_duplicates(subset=['clean_addr', 'date_str', 'total_price'])

    # 預先拆解每筆實價登錄的「路街段、巷、弄、號」確保 100% 精準對位
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

    CITY_DATA_CACHE[city_code] = df
    gc.collect()
    return df

@app.get("/api/analyze")
def analyze(address: str = Query(...), custom_ping: float = Query(0), range_ratio: float = Query(0.20)):
    try:
        clean_input = normalize_address(address)
        feat = parse_user_address(clean_input)
        if not feat:
            return {"error": "地址格式無法解析，請輸入包含『縣市、區、路街、號』的完整地址（例：台北市內湖區成功路四段180號6樓）"}

        city_code = "a"
        for city_name, code in CITY_CODE_MAP.items():
            if city_name in clean_input or city_name.replace("臺", "台") in address:
                city_code = code
                break

        df = fetch_moi_online_data(city_code)
        if df.empty:
            return {"error": "內政部雲端伺服器連線繁忙，請等待 10 秒後再試一次！"}

        road = feat['road']
        lane = feat['lane']
        alley = feat['alley']
        num = feat['num']
        target_floor = feat['floor']
        district = feat['district']

        # 1. 鎖定同行政區、同一條路 (且嚴格區分「大馬路」與「巷弄」)
        road_df = df[df['p_road'] == road].copy()
        if road_df.empty:
            road_df = df[df['clean_addr'].str.contains(road, na=False, regex=False)].copy()
        if district and not road_df[road_df['district'] == district].empty:
            road_df = road_df[road_df['district'] == district].copy()

        if road_df.empty:
            return {"error": f"查無『{district}{road}』之實價登錄資料，請確認縣市與路名是否正確。"}

        # 嚴格區分「同巷弄」或「同在大馬路(無巷弄)」
        same_street_df = road_df[(road_df['p_lane'] == lane) & (road_df['p_alley'] == alley)].copy()
        if same_street_df.empty:
            same_street_df = road_df[road_df['p_lane'] == lane].copy()
        if same_street_df.empty:
            same_street_df = road_df.copy()

        # =========================================================================
        # 【步驟一】精準帶出你輸入這間房子的資料 (並取得該大樓的「建築完成年月」指紋)
        # =========================================================================
        # A. 找完全同門牌號 (例如精準 180 號)
        exact_num_df = same_street_df[same_street_df['p_num'] == num].sort_values('month_val', ascending=False)
        # B. 找完全同門牌號 + 完全同樓層 (這間房子本尊的成交紀錄！)
        exact_unit_df = exact_num_df[exact_num_df['floor_num'] == target_floor] if target_floor > 0 else pd.DataFrame()
        # C. 找同側緊鄰門牌 (同單雙號，差距 ±6 號內，例如 176, 178, 180, 182, 184 號)
        adj_num_df = same_street_df[
            (same_street_df['p_num'] % 2 == num % 2) &
            ((same_street_df['p_num'] - num).abs() <= 6)
        ].copy()

        target_build_ym = ""
        target_total_floors = 0

        if not exact_unit_df.empty:
            # 100% 命中你輸入的這間房子本尊！
            ref = exact_unit_df.iloc[0]
            target_ping = round(float(ref['house_ping']), 1) if custom_ping <= 0 else custom_ping
            target_total_ping = round(float(ref['total_ping']), 1)
            target_age = int(ref['age']) if ref['age'] >= 0 else 15
            target_build_ym = str(ref['build_ym'])
            target_total_floors = int(ref['total_floors_num'])
            b_type = str(ref['b_type'])
            layout = f"{ref['rooms']}房{ref['halls']}廳"
            last_deal_info = f"（本戶曾於民國 {ref['date_str'][:-4]}年{ref['date_str'][-4:-2]}月 以總價 {int(ref['total_price'])}萬 成交）"
            source_msg = f"🎯 100% 精準命中本戶【{ref['clean_addr']}】地政實價檔案！{last_deal_info}"

        elif not exact_num_df.empty:
            # 命中同門牌大樓 (例如 180號 的其他樓層)！屋齡與總樓層 100% 準確！
            # 若同一門牌號有多種坪數，優先抓同樓層高度或中位數坪數
            ref = exact_num_df.iloc[0]
            target_build_ym = str(ref['build_ym'])
            target_total_floors = int(ref['total_floors_num'])
            target_age = int(ref['age']) if ref['age'] >= 0 else 15
            b_type = str(ref['b_type'])
            
            # 列出該棟所有出現過的坪數供參考，預設取最接近樓層或中位數
            if custom_ping > 0:
                target_ping = custom_ping
            else:
                target_ping = round(float(exact_num_df['house_ping'].median()), 1)
            target_total_ping = round(float(exact_num_df['total_ping'].median()), 1)
            layout = f"{ref['rooms']}房{ref['halls']}廳"
            source_msg = f"✅ 精準命中本棟門牌【{road}{lane+'巷' if lane else ''}{num}號】大樓檔案（共檢索到本棟 {len(exact_num_df)} 戶交易，完工年月：{target_build_ym[:5] if len(target_build_ym)>=5 else '詳見明細'}）"

        elif not adj_num_df.empty:
            # 該單一門牌號近2.5年無人賣出，從緊鄰連號門牌 (同社區其他棟別) 抓取！
            adj_num_df['num_diff'] = (adj_num_df['p_num'] - num).abs()
            # 如果使用者有輸入樓層 (例如 8樓)，則目標大樓的總樓層數一定 >= 8樓！
            if target_floor > 0:
                valid_fl = adj_num_df[adj_num_df['total_floors_num'] >= target_floor]
                if not valid_fl.empty:
                    adj_num_df = valid_fl
            adj_num_df = adj_num_df.sort_values(['num_diff', 'month_val'], ascending=[True, False])
            ref = adj_num_df.iloc[0]
            target_build_ym = str(ref['build_ym'])
            target_total_floors = int(ref['total_floors_num'])
            # 把緊鄰且同完工年月的同社區抓出來算中位數坪數
            same_comm_init = adj_num_df[adj_num_df['build_ym'] == target_build_ym] if target_build_ym else adj_num_df
            target_ping = round(float(same_comm_init['house_ping'].median()), 1) if custom_ping <= 0 else custom_ping
            target_total_ping = round(float(same_comm_init['total_ping'].median()), 1)
            target_age = int(ref['age']) if ref['age'] >= 0 else 15
            b_type = str(ref['b_type'])
            layout = f"{ref['rooms']}房{ref['halls']}廳"
            source_msg = f"✅ 本號無近期交易，已自動鎖定緊鄰同社區門牌【{ref['clean_addr']}】之社區規格（完工年月：{target_build_ym[:5] if len(target_build_ym)>=5 else '-'}）"

        else:
            # 連隔壁幾號都沒交易，才從同巷或同路找最接近的門牌
            cand_df = same_street_df.copy()
            if target_floor > 0:
                vf = cand_df[cand_df['total_floors_num'] >= target_floor]
                if not vf.empty: cand_df = vf
            cand_df['num_diff'] = (cand_df['p_num'] - num).abs()
            cand_df = cand_df.sort_values('num_diff')
            ref = cand_df.iloc[0]
            target_build_ym = str(ref['build_ym'])
            target_total_floors = int(ref['total_floors_num'])
            target_ping = round(float(cand_df.head(5)['house_ping'].median()), 1) if custom_ping <= 0 else custom_ping
            target_total_ping = target_ping
            target_age = int(ref['age']) if ref['age'] >= 0 else 20
            b_type = str(ref['b_type'])
            layout = f"{ref['rooms']}房{ref['halls']}廳"
            source_msg = f"⚠️ 『{num}號』及緊鄰門牌近2年無交易，暫帶入同路段最近門牌【{ref['clean_addr']}】規格，建議於上方輸入實際坪數與屋齡！"

        floors_display = f"{target_floor}F / 共{target_total_floors}F" if (target_floor > 0 and target_total_floors > 0) else f"共 {target_total_floors} 層"

        # =========================================================================
        # 【步驟二】比對「同社區」與「同路段坪數+屋齡接近」的實價登錄
        # =========================================================================
        # 什麼叫做「100% 同社區」？
        # 條件：(1) 完全同門牌號 (如 180號 各樓層)
        #    或 (2) 同條路巷 + 建築完成年月(精確到月)完全相同 + 總樓層數相同 + 門牌差距 <= 30號
        #    或 (3) 同條路巷 + 同單雙號連號(差距<=8號) + 屋齡差距<=1年 + 總樓層數相同
        def check_is_same_community(row):
            if row['p_road'] != road or row['p_lane'] != lane:
                return False
            if row['p_num'] == num:
                return True
            if target_build_ym and len(target_build_ym) >= 5 and row['build_ym'] == target_build_ym:
                if abs(row['p_num'] - num) <= 40:
                    return True
            if (row['p_num'] % 2 == num % 2) and abs(row['p_num'] - num) <= 8:
                if target_total_floors > 0 and row['total_floors_num'] == target_total_floors and abs(row['age'] - target_age) <= 1:
                    return True
            return False

        road_df['is_same_comm'] = road_df.apply(check_is_same_community, axis=1)

        min_ping = round(target_ping * (1 - range_ratio), 1)
        max_ping = round(target_ping * (1 + range_ratio), 1)

        # 1. 先抓「同社區」且坪數接近的交易 (不限1年，近2年內同社區交易最具參考價值！)
        same_comm_all = road_df[road_df['is_same_comm']].copy()
        same_comm_fit_ping = same_comm_all[
            (same_comm_all['house_ping'] >= min_ping) &
            (same_comm_all['house_ping'] <= max_ping)
        ].copy()

        # 2. 再抓「周邊同路段、屋齡接近(±6年)、坪數接近、同建物型態」之近一年交易
        max_m = df['month_val'].max()
        is_apt = "公寓" in b_type
        is_town = "透天" in b_type

        nearby_fit = road_df[
            (~road_df['is_same_comm']) &
            (road_df['month_val'] >= max_m - 14) &
            (road_df['house_ping'] >= min_ping) &
            (road_df['house_ping'] <= max_ping) &
            ((road_df['age'] - target_age).abs() <= 6)
        ].copy()

        if is_apt:
            nearby_fit = nearby_fit[nearby_fit['b_type'].str.contains('公寓', na=False)]
        elif is_town:
            nearby_fit = nearby_fit[nearby_fit['b_type'].str.contains('透天', na=False)]
        else:
            nearby_fit = nearby_fit[nearby_fit['b_type'].str.contains('大樓|華廈', na=False)]

        # 若目標不是 1 樓，排除 1 樓特殊店面/庭院交易
        if target_floor != 1:
            same_comm_fit_ping = same_comm_fit_ping[same_comm_fit_ping['floor_num'] != 1]
            nearby_fit = nearby_fit[nearby_fit['floor_num'] != 1]

        # 將「同社區」與「同路段屋齡坪數相近」合併，且「同社區」永遠排最前面！
        nearby_fit['num_dist'] = (nearby_fit['p_num'] - num).abs() + nearby_fit['p_lane'].apply(lambda x: 0 if x == lane else 100)
        nearby_fit = nearby_fit.sort_values(['num_dist', 'month_val'], ascending=[True, False])
        same_comm_fit_ping = same_comm_fit_ping.sort_values('month_val', ascending=False)

        # 若同社區坪數相近的交易太少，把同社區其他坪數也放進來參考
        if len(same_comm_fit_ping) < 2 and not same_comm_all.empty:
            same_comm_fit_ping = same_comm_all.sort_values('month_val', ascending=False)

        comps = pd.concat([same_comm_fit_ping, nearby_fit], ignore_index=True)

        # 若同條路上屋齡±6年真的太少，放寬至屋齡±10年
        if len(comps) < 3:
            fallback_nearby = road_df[
                (road_df['house_ping'] >= min_ping) &
                (road_df['house_ping'] <= max_ping) &
                ((road_df['age'] - target_age).abs() <= 12)
            ].copy()
            fallback_nearby['num_dist'] = (fallback_nearby['p_num'] - num).abs()
            fallback_nearby = fallback_nearby.sort_values(['is_same_comm', 'num_dist', 'month_val'], ascending=[False, True, False])
            comps = fallback_nearby

        if comps.empty:
            return {"error": f"查無『{road}』周邊坪數介於 {min_ping}~{max_ping} 坪、屋齡約 {target_age} 年之實價登錄，請將上方『坪數誤差』放寬至 ±30%。"}

        # =========================================================================
        # 【步驟三】計算成交價格落點：只要同社區有 >= 2 筆交易，100% 用同社區算落點！
        # =========================================================================
        same_comm_count = int(comps['is_same_comm'].sum())
        if same_comm_count >= 2:
            stat_df = comps[comps['is_same_comm']].head(15)
            val_basis = f"100% 採用本社區 ({same_comm_count}筆) 實價計算"
        else:
            stat_df = comps.head(15)
            val_basis = f"採用本社區+緊鄰同屋齡 ({len(stat_df)}筆) 實價計算"

        q = stat_df['real_unit_price'].quantile([0.25, 0.5, 0.75]).to_dict()
        low_u, med_u, high_u = round(q[0.25], 1), round(q[0.5], 1), round(q[0.75], 1)
        avg_u = round(float(stat_df['real_unit_price'].mean()), 1)

        comp_list = []
        for _, r in comps.head(30).iterrows():
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
                "build_ym": r['build_ym'][:5] if len(r['build_ym']) >= 5 else "-",
                "ping": round(float(r['house_ping']), 1),
                "total_ping": round(float(r['total_ping']), 1),
                "floor": fl_display,
                "unit": round(float(r['real_unit_price']), 1),
                "total": round(float(r['total_price']), 0),
                "park": round(float(r['parking_price']), 0),
                "is_same": bool(r['is_same_comm'])
            })

        return {
            "target": {
                "addr": clean_input, "district": district, "road": road,
                "ping": target_ping, "total_ping": target_total_ping,
                "min_ping": min_ping, "max_ping": max_ping,
                "age": target_age, "build_ym": target_build_ym[:5] if len(target_build_ym)>=5 else "未知",
                "floors": floors_display, "type": b_type,
                "layout": layout, "note": source_msg, "basis": val_basis
            },
            "val": {
                "count": len(comps), "same_count": same_comm_count,
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
    <title>好房 AI 同社區實價登錄精準比對系統 v4.0</title>
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
                <div class="bg-blue-600 p-2 rounded-lg"><i class="fa-solid fa-building-user text-xl text-white"></i></div>
                <div>
                    <span class="font-black text-lg tracking-wide">好房 AI 同社區實價比對王</span>
                    <span class="hidden md:inline-block ml-2 text-xs bg-amber-500/20 text-amber-300 border border-amber-400/30 px-2 py-0.5 rounded-full">v4.0 完工年月指紋鎖定同社區引擎</span>
                </div>
            </div>
        </div>
    </nav>

    <div class="bg-gradient-to-r from-slate-900 via-blue-900 to-indigo-900 text-white py-8 px-4 shadow-inner">
        <div class="max-w-7xl mx-auto">
            <h1 class="text-2xl md:text-3xl font-extrabold mb-2">輸入地址 ➡️ 自動調出本戶規格 ➡️ 鎖定「同社區」與相近屋齡坪數實價</h1>
            <p class="text-blue-200 text-xs md:text-sm mb-6">透過內政部歷史實價庫反查該門牌「建築完成年月」與「總樓層」，100% 鎖定同社區大樓與相近坪數屋齡成交紀錄。</p>

            <div class="bg-white p-4 rounded-2xl shadow-xl text-slate-800 grid grid-cols-1 md:grid-cols-12 gap-3 items-end">
                <div class="md:col-span-5">
                    <label class="text-xs font-bold text-slate-500 block mb-1">📍 請輸入完整地址 (含號與樓層)</label>
                    <input type="text" id="addr" value="台北市內湖區成功路四段180號6樓" 
                           placeholder="例：新北市板橋區文化路一段188號10樓"
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3.5 py-2.5 text-sm font-bold focus:outline-none focus:ring-2 focus:ring-blue-600">
                </div>
                <div class="md:col-span-2">
                    <label class="text-xs font-bold text-slate-500 block mb-1">📐 扣車位坪數 (選填)</label>
                    <input type="number" id="ping" placeholder="自動抓取本棟坪數" 
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3 py-2.5 text-sm font-bold focus:outline-none">
                </div>
                <div class="md:col-span-2">
                    <label class="text-xs font-bold text-slate-500 block mb-1">🚗 外加車位價 (萬)</label>
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
                        <i class="fa-solid fa-magnifying-glass-location"></i>
                        <span>查本戶與同社區</span>
                    </button>
                </div>
            </div>
        </div>
    </div>

    <div class="max-w-7xl mx-auto px-4 py-8">
        <div id="loading" class="hidden bg-white rounded-3xl p-12 text-center shadow-sm border border-slate-200 my-6">
            <i class="fa-solid fa-circle-notch text-5xl text-blue-600 spin mb-4"></i>
            <h3 class="text-lg font-extrabold text-slate-800">正在檢索內政部多季度歷史實價庫、鎖定本棟與同社區資料...</h3>
            <p class="text-xs text-slate-400 mt-2">首次查詢該縣市需下載近 2 年完整實價壓縮包（約需 15~25 秒），完成後同縣市查詢僅需 0.1 秒！</p>
        </div>

        <div id="err" class="hidden bg-red-50 border-l-4 border-red-500 text-red-700 p-4 rounded-xl shadow-sm my-4 font-bold text-sm"></div>

        <div id="dashboard" class="hidden grid grid-cols-1 lg:grid-cols-12 gap-6">
            <div class="lg:col-span-5 space-y-6">
                <!-- 1. 目標房屋真實規格 -->
                <div class="bg-white rounded-2xl p-6 shadow-sm border-2 border-blue-600">
                    <div class="flex items-center justify-between mb-3">
                        <span class="bg-blue-600 text-white text-xs font-extrabold px-2.5 py-1 rounded-md">1. 目標地址房屋檔案</span>
                        <span id="rType" class="text-xs font-bold text-indigo-700 bg-indigo-100 px-2.5 py-1 rounded-md"></span>
                    </div>
                    <h2 id="rAddr" class="text-xl font-black text-slate-900 mb-2"></h2>
                    <p id="note" class="text-xs text-blue-800 font-bold bg-blue-50 p-3 rounded-xl border border-blue-200 mb-4 leading-relaxed"></p>
                    <div class="grid grid-cols-4 gap-2 text-center bg-slate-50 p-3 rounded-xl border border-slate-200">
                        <div>
                            <p class="text-[11px] text-slate-400">主+附+公設</p>
                            <p id="rPing" class="text-sm font-black text-blue-700 mt-0.5"></p>
                            <p id="rTotalPing" class="text-[10px] text-slate-400"></p>
                        </div>
                        <div class="border-l">
                            <p class="text-[11px] text-slate-400">真實屋齡</p>
                            <p id="rAge" class="text-sm font-black text-slate-800 mt-0.5"></p>
                            <p id="rBuildYm" class="text-[10px] text-slate-400"></p>
                        </div>
                        <div class="border-l">
                            <p class="text-[11px] text-slate-400">樓層/總樓層</p>
                            <p id="rFloor" class="text-sm font-black text-slate-800 mt-0.5"></p>
                        </div>
                        <div class="border-l">
                            <p class="text-[11px] text-slate-400">標準格局</p>
                            <p id="rLayout" class="text-xs font-bold text-slate-800 mt-1"></p>
                        </div>
                    </div>
                </div>

                <!-- 2. 同社區成交價格落點 -->
                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <div class="flex items-center justify-between mb-4">
                        <h3 class="text-base font-black text-slate-900"><i class="fa-solid fa-bullseye text-red-500 mr-2"></i>2. 房屋價值與成交落點分析</h3>
                        <span id="rBasis" class="text-[11px] bg-emerald-100 text-emerald-800 font-extrabold px-2.5 py-1 rounded-full"></span>
                    </div>
                    <div class="bg-gradient-to-br from-blue-900 to-indigo-900 text-white p-5 rounded-2xl text-center shadow-md mb-5">
                        <p class="text-xs text-blue-200 font-semibold">預估合理成交總價 (中位數 PR50)</p>
                        <div class="my-2 flex items-baseline justify-center space-x-1">
                            <span id="medT" class="text-4xl font-black text-yellow-300"></span>
                            <span class="text-sm font-bold">萬元</span>
                        </div>
                        <div class="flex justify-center space-x-4 text-xs text-blue-100 mt-2 pt-2 border-t border-blue-700/50">
                            <span>合理中位數單價：<strong id="medU" class="text-white"></strong> 萬/坪</span>
                            <span>•</span>
                            <span>同社區/同質均價：<strong id="avgU" class="text-white"></strong> 萬/坪</span>
                        </div>
                    </div>
                    <div class="space-y-3">
                        <div class="flex items-center justify-between p-3.5 rounded-xl bg-emerald-50 border border-emerald-200">
                            <div>
                                <span class="text-xs font-extrabold text-emerald-800 block">📉 買方保守出價落點 (PR25)</span>
                                <span class="text-[11px] text-emerald-600">社區低樓層或買方首輪議價起點</span>
                            </div>
                            <div class="text-right"><p id="lowT" class="text-lg font-black text-emerald-900"></p><p id="lowU" class="text-xs font-bold text-emerald-700"></p></div>
                        </div>
                        <div class="flex items-center justify-between p-3.5 rounded-xl bg-amber-50 border border-amber-200">
                            <div>
                                <span class="text-xs font-extrabold text-amber-800 block">📈 屋主樂觀開價落點 (PR75)</span>
                                <span class="text-[11px] text-amber-600">社區高樓層或帶裝潢戶成交上限</span>
                            </div>
                            <div class="text-right"><p id="highT" class="text-lg font-black text-amber-900"></p><p id="highU" class="text-xs font-bold text-amber-700"></p></div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 右側欄：同社區與相近屋齡坪數實價明細 -->
            <div class="lg:col-span-7 space-y-6">
                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-4">
                        <div>
                            <h3 class="text-base font-black text-slate-900">📋 同社區 ＆ 周邊相近坪數屋齡實價登錄比對表</h3>
                            <p id="filterNote" class="text-xs text-slate-500 mt-0.5 font-semibold"></p>
                        </div>
                        <div class="flex space-x-2">
                            <button onclick="filterTable('same')" id="btnSame" class="text-xs font-bold px-3 py-1.5 rounded-lg bg-blue-600 text-white shadow">只看同社區 (<span id="sameCnt">0</span>)</button>
                            <button onclick="filterTable('all')" id="btnAll" class="text-xs font-bold px-3 py-1.5 rounded-lg bg-slate-100 text-slate-600">含周邊同屋齡 (<span id="allCnt">0</span>)</button>
                        </div>
                    </div>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left border-collapse">
                            <thead>
                                <tr class="border-b-2 border-slate-200 text-[11px] text-slate-400">
                                    <th class="py-2 px-2">社區關聯</th><th class="py-2 px-2">成交年月</th><th class="py-2 px-2">成交門牌</th>
                                    <th class="py-2 px-2">屋齡(完工)</th><th class="py-2 px-2 text-right">扣車位坪</th>
                                    <th class="py-2 px-2 text-center">樓層</th><th class="py-2 px-2 text-right">拆算單價</th><th class="py-2 px-2 text-right">成交總價</th>
                                </tr>
                            </thead>
                            <tbody id="tableBody" class="divide-y divide-slate-100 text-xs"></tbody>
                        </table>
                    </div>
                </div>

                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <h3 class="text-base font-black text-slate-900 mb-1">📊 同社區 vs 周邊相近屋齡「單價落點分佈圖」</h3>
                    <p class="text-xs text-slate-400 mb-4">紅點為本戶預估落點，深藍點為「同社區」實際成交，灰點為同路段相近屋齡成交</p>
                    <div class="h-60 w-full"><canvas id="scatterChart"></canvas></div>
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
        errEl.innerText = "雲端主機正在下載多季度實價資料庫或連線逾時 (" + e.message + ")，請等 10 秒後再按一次『查本戶與同社區』！";
        errEl.classList.remove('hidden');
    }
}

function renderDashboard(d, park) {
    document.getElementById('rAddr').innerText = d.target.addr;
    document.getElementById('rType').innerText = d.target.type;
    document.getElementById('note').innerText = d.target.note;
    document.getElementById('rPing').innerText = d.target.ping + " 坪";
    document.getElementById('rTotalPing').innerText = `(含車位約 ${d.target.total_ping}坪)`;
    document.getElementById('rAge').innerText = d.target.age + " 年";
    document.getElementById('rBuildYm').innerText = `(民國${d.target.build_ym}完工)`;
    document.getElementById('rFloor').innerText = d.target.floors;
    document.getElementById('rLayout').innerText = d.target.layout;

    document.getElementById('rBasis').innerText = d.target.basis;
    document.getElementById('medT').innerText = (d.val.med_t + park).toLocaleString();
    document.getElementById('medU').innerText = d.val.med_u;
    document.getElementById('avgU').innerText = d.val.avg_u;

    document.getElementById('lowT').innerText = (d.val.low_t + park).toLocaleString() + " 萬";
    document.getElementById('lowU').innerText = `單價 ${d.val.low_u} 萬/坪`;
    document.getElementById('highT').innerText = (d.val.high_t + park).toLocaleString() + " 萬";
    document.getElementById('highU').innerText = `單價 ${d.val.high_u} 萬/坪`;

    document.getElementById('sameCnt').innerText = d.val.same_count;
    document.getElementById('allCnt').innerText = d.val.count;

    document.getElementById('filterNote').innerText = 
        `鎖定條件：同社區完工指紋 (民國${d.target.build_ym}) ｜ 扣車位 ${d.target.min_ping} ~ ${d.target.max_ping} 坪 ｜ 屋齡約 ${d.target.age} 年`;

    // 若同社區有交易，預設直接切到「只看同社區」標籤，讓使用者一眼看到最準的同社區資料！
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
                { label: '本戶合理預估落點', data: [{x: d.target.ping, y: d.val.med_u, addr: '目標房屋預估落點'}], backgroundColor: '#ef4444', pointRadius: 9 },
                { label: '同社區實價成交', data: samePoints, backgroundColor: '#2563eb', pointRadius: 7 },
                { label: '周邊同屋齡同型態成交', data: otherPoints, backgroundColor: '#94a3b8', pointRadius: 5 }
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
        tbody.innerHTML = `<tr><td colspan="8" class="text-center py-6 text-slate-400">本社區近期無相近坪數交易，請點選右上方「含周邊同屋齡」查看緊鄰社區成交紀錄</td></tr>`;
        return;
    }
    list.forEach(c => {
        const badge = c.is_same ? `<span class="bg-blue-600 text-white text-[10px] px-2 py-0.5 rounded font-bold">🏠 同社區</span>` : `<span class="bg-slate-200 text-slate-700 text-[10px] px-2 py-0.5 rounded font-semibold">同路相近屋齡</span>`;
        const parkNote = c.park > 0 ? `<span class="block text-[10px] text-slate-400">含車位${c.park}萬</span>` : `<span class="block text-[10px] text-slate-400">總坪${c.total_ping}坪</span>`;
        tbody.innerHTML += `
            <tr class="hover:bg-slate-50 ${c.is_same ? 'bg-blue-50/50' : ''}">
                <td class="py-3 px-2 whitespace-nowrap">${badge}</td>
                <td class="py-3 px-2 font-semibold text-slate-600">${c.date}</td>
                <td class="py-3 px-2 font-bold text-slate-900">${c.addr}</td>
                <td class="py-3 px-2 text-slate-500 whitespace-nowrap">${c.age} <span class="text-[10px] text-slate-400">(${c.build_ym})</span></td>
                <td class="py-3 px-2 text-right font-bold text-blue-700">${c.ping} 坪</td>
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
