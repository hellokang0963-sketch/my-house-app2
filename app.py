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
def analyze(address: str = Query(...)
