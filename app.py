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

def cn_num_to_str(cn: str) -> str:
    """將中文數字 (如: 十二、二十三、九) 精準轉為阿拉伯數字字串"""
    if not cn:
        return ""
    if cn.isdigit():
        return cn
    cn_map = {'零': 0, '〇': 0, '○': 0, '一': 1, '二': 2, '三': 3, '四': 4,
              '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
    if '十' in cn:
        parts = cn.split('十')
        tens = cn_map.get(parts[0], 1) if parts[0] != '' else 1
        ones = cn_map.get(parts[1], 0) if len(parts) > 1 and parts[1] != '' else 0
        return str(tens * 10 + ones)
    return "".join(str(cn_map.get(c, c)) for c in cn)

def parse_floor_int(fl_str: str) -> int:
    """將『九層』、『14樓』、『十二層』轉為整數樓層"""
    if not isinstance(fl_str, str):
        return 0
    fl_str = fl_str.split('，')[0].split(',')[0].strip()
    if '地下' in fl_str or 'B' in fl_str.upper():
        return -1
    m = re.search(r'([0-9一二三四五六七八九十〇○]+)', fl_str)
    if m:
        val = cn_num_to_str(m.group(1))
        return int(val) if val.isdigit() else 0
    return 0

def normalize_address(addr: str) -> str:
    """統一門牌格式：全形轉半形、台轉臺、重複縣市剔除、中文段樓層之幾轉阿拉伯數字"""
    if not isinstance(addr, str):
        return ""
    addr = addr.replace("台", "臺").strip()
    full_to_half = str.maketrans("０１２３４５６７８９－—～", "0123456789---")
    addr = addr.translate(full_to_half)
    # 剔除重複輸入的縣市或行政區 (例如：新竹市新竹市西濱路 -> 新竹市西濱路)
    addr = re.sub(r'([臺北新桃中南高基宜竹苗彰投雲嘉屏花東澎金連]{2}[市縣])\1+', r'\1', addr)
    # 轉換中文數字 + (段|樓|層|巷|弄|號)
    addr = re.sub(
        r'([一二三四五六七八九十〇○]+)(段|樓|層|巷|弄|號)',
        lambda m: f"{cn_num_to_str(m.group(1))}{'樓' if m.group(2) == '層' else m.group(2)}",
        addr
    )
    addr = re.sub(r'(\d+)層', r'\1樓', addr)
    # 轉換「之三」->「之3」
    addr = re.sub(
        r'之([一二三四五六七八九十〇○]+)',
        lambda m: f"之{cn_num_to_str(m.group(1))}",
        addr
    )
    return addr

def parse_roc_date(date_str: str) -> int:
    if not isinstance(date_str, str) or len(date_str) < 6:
        return 0
    try:
        year = int(date_str[:-4]) + 1911
        month = int(date_str[-4:-2])
        return year * 12 + month
    except:
        return 0

def fetch_moi_online_data(city_code: str) -> pd.DataFrame:
    """直接從內政部伺服器抓取最新與歷史實價登錄網路資料"""
    if city_code in CITY_DATA_CACHE:
        return CITY_DATA_CACHE[city_code]

    print(f"🌐 正在從內政部伺服器即時抓取 [{city_code.upper()}] 縣市之實價登錄資料...")
    dfs = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    target_filename = f"{city_code}_lvr_land_a.csv"

    # 1. 先抓內政部「最新一期」公開資料 ZIP
    urls_to_fetch = ["https://plvr.land.moi.gov.tw/Download?type=zip&fileName=lvr_landcsv.zip"]

    # 2. 自動推算並抓取過去 8 季 (約 2 年) 的資料，確保同社區與本戶房屋資料充足
    now = datetime.now()
    roc_year = now.year - 1911
    for y in [roc_year, roc_year - 1, roc_year - 2]:
        for s in [4, 3, 2, 1]:
            urls_to_fetch.append(f"https://plvr.land.moi.gov.tw/DownloadSeason?season={y}S{s}&type=zip&fileName=lvr_landcsv.zip")

    success_seasons = 0
    for url in urls_to_fetch:
        if success_seasons >= 6:  # 抓滿最新一期 + 近5~6季資料，兼顧速度與社區覆蓋率
            break
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            if resp.status_code == 200 and len(resp.content) > 1000:
                with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                    if target_filename in z.namelist():
                        with z.open(target_filename) as f:
                            sub_df = pd.read_csv(f, dtype=str, on_bad_lines='skip')
                            if len(sub_df) > 1:
                                dfs.append(sub_df.iloc[1:])  # 跳過第二行英文標頭
                                success_seasons += 1
        except Exception:
            continue

    if not dfs:
        return pd.DataFrame()

    df = pd.concat(dfs, ignore_index=True)

    # 清洗網路抓下來的原始資料
    df['clean_addr'] = df['土地位置建物門牌'].astype(str).apply(normalize_address)
    df['total_ping'] = pd.to_numeric(df['建物移轉總面積平方公尺'], errors='coerce').fillna(0) * 0.3025
    df['parking_ping'] = pd.to_numeric(df['車位移轉總面積(平方公尺)'], errors='coerce').fillna(0) * 0.3025
    df['house_ping'] = (df['total_ping'] - df['parking_ping']).clip(lower=0)

    # 主建物、附屬建物、陽台面積 (平方公尺轉坪)
    df['main_ping'] = pd.to_numeric(df.get('主建物面積', 0), errors='coerce').fillna(0) * 0.3025
    df['sub_ping'] = pd.to_numeric(df.get('附屬建物面積', 0), errors='coerce').fillna(0) * 0.3025
    df['balcony_ping'] = pd.to_numeric(df.get('陽台面積', 0), errors='coerce').fillna(0) * 0.3025

    df['total_price'] = pd.to_numeric(df['總價元'], errors='coerce').fillna(0) / 10000
    df['parking_price'] = pd.to_numeric(df['車位總價元'], errors='coerce').fillna(0) / 10000

    def calc_unit(row):
        if row['house_ping'] <= 1:
            return 0
        if row['parking_price'] > 0 and row['parking_ping'] > 0:
            return (row['total_price'] - row['parking_price']) / row['house_ping']
        return row['total_price'] / row['total_ping'] if row['total_ping'] > 0 else 0

    df['real_unit_price'] = df.apply(calc_unit, axis=1)
    df['month_val'] = df['交易年月日'].apply(parse_roc_date)
    df['floor_num'] = df['移轉層次'].apply(parse_floor_int)
    df['total_floor_num'] = df['總樓層數'].apply(parse_floor_int)

    # 過濾掉特殊交易與純車位/純土地交易
    df = df[df['交易標的'].str.contains('房地', na=False)]
    df = df[~df['備註'].astype(str).str.contains('親友|特殊|急買|急賣|瑕疵|增建|政府|債權', na=False)]
    df = df[(df['real_unit_price'] > 5) & (df['real_unit_price'] < 400)]
    df = df.drop_duplicates(subset=['clean_addr', '交易年月日', '總價元'])

    CITY_DATA_CACHE[city_code] = df
    print(f"✅ [{city_code.upper()}] 網路資料抓取完成，共 {len(df)} 筆真實交易！")
    return df


@app.get("/api/analyze")
def analyze(
    address: str = Query(...),
    custom_ping: float = Query(0),
    custom_park_price: float = Query(-1),
    ping_tol: float = Query(0.15)  # 預設坪數接近範圍 ±15%
):
    clean_input = normalize_address(address)

    # 1. 自動判斷縣市
    city_code = "o" if "新竹市" in clean_input else "a"
    for city_name, code in CITY_CODE_MAP.items():
        if city_name in clean_input:
            city_code = code
            break

    df = fetch_moi_online_data(city_code)
    if df.empty:
        return {"error": "目前內政部開放資料伺服器連線逾時，請稍後再試。"}

    # 2. 解析使用者輸入的門牌：行政區 + 路街段 + 巷 + 弄 + 號 + 樓 + 之幾
    pattern = r'([臺北新桃中南高基宜竹苗彰投雲嘉屏花東澎金連]{2}[市縣])?(.+?[區鄉鎮市])?(.+?[路街段大道])(\d+巷)?(\d+弄)?(\d+(?:之\d+)?號)(\d+樓)?(之\d+)?'
    match = re.search(pattern, clean_input)
    if not match:
        return {"error": "請輸入包含『縣市、路街、號』的完整地址，例如：新竹市西濱路一段185號9樓之3"}

    district = match.group(2) or ""
    road = match.group(3) or ""
    lane = match.group(4) or ""
    alley = match.group(5) or ""
    number = match.group(6) or ""       # 例：185號
    floor_str = match.group(7) or ""    # 例：9樓
    sub_unit = match.group(8) or ""     # 例：之3

    input_floor_num = int(re.sub(r'\D', '', floor_str)) if floor_str else 0
    num_val = int(re.sub(r'\D', '', number.split('之')[0]))

    # 建立門牌比對正則
    base_doorplate = f"{road}{lane}{alley}{number}"
    # 同側連號門牌 (例如 185號 -> 181, 183, 185, 187, 189號)
    same_side_nums = "|".join([f"{n}號" for n in range(max(1, num_val - 6), num_val + 7, 2)])
    nearby_doorplate_regex = f"{re.escape(road + lane + alley)}({same_side_nums})"

    # =========================================================================
    # 功能 1：帶出「我這間房屋資料」 (四層精準匹配：同戶 -> 同棟同之幾標準層 -> 同門牌 -> 同社區)
    # =========================================================================
    exact_door_df = df[df['clean_addr'].str.contains(
        f"{re.escape(base_doorplate)}(?![0-9])", na=False
    )].sort_values(by='month_val', ascending=False)

    unit_match_df = pd.DataFrame()
    match_level = ""

    # Level 1: 完全同戶 (同號 + 同樓 + 同之幾)
    if not exact_door_df.empty and (floor_str or sub_unit):
        cond = pd.Series(True, index=exact_door_df.index)
        if input_floor_num > 0:
            cond &= (exact_door_df['floor_num'] == input_floor_num)
        if sub_unit:
            cond &= exact_door_df['clean_addr'].str.contains(re.escape(sub_unit), na=False)
        if cond.any():
            unit_match_df = exact_door_df[cond]
            match_level = f"🎯 精準命中本戶歷史實價資料（{base_doorplate}{floor_str}{sub_unit}）"

    # Level 2: 同棟同柱標準層 (同號 + 相同「之幾」，台灣大樓同柱垂直樓層格局與坪數完全相同)
    if unit_match_df.empty and not exact_door_df.empty and sub_unit:
        cond = exact_door_df['clean_addr'].str.contains(re.escape(sub_unit), na=False)
        if cond.any():
            unit_match_df = exact_door_df[cond]
            match_level = f"✅ 自動帶出同棟同戶別標準層規格（{base_doorplate}*{sub_unit}，垂直同柱格局坪數一致）"

    # Level 3: 同門牌大樓 (同號)
    if unit_match_df.empty and not exact_door_df.empty:
        unit_match_df = exact_door_df
        match_level = f"✅ 自動帶出同棟門牌規格（{base_doorplate}）"

    # Level 4: 緊鄰連號同社區門牌
    nearby_df = df[df['clean_addr'].str.contains(nearby_doorplate_regex, na=False)].sort_values(by='month_val', ascending=False)
    if unit_match_df.empty and not nearby_df.empty:
        if sub_unit and nearby_df['clean_addr'].str.contains(re.escape(sub_unit), na=False).any():
            unit_match_df = nearby_df[nearby_df['clean_addr'].str.contains(re.escape(sub_unit), na=False)]
            match_level = f"✅ 自動帶出同社區同戶別（{sub_unit}）標準規格"
        else:
            unit_match_df = nearby_df
            match_level = f"ℹ️ 自動帶出同社區鄰近門牌（{road}{number}周邊）標準規格"

    # Level 5: 同路段備援
    road_df = df[df['clean_addr'].str.contains(re.escape(road), na=False)].sort_values(by='month_val', ascending=False)
    if unit_match_df.empty:
        if road_df.empty:
            return {"error": f"內政部近期資料查無『{road}』的交易，請確認縣市與路名是否正確。"}
        unit_match_df = road_df
        match_level = f"ℹ️ 該門牌近期無交易，已帶入『{road}』同路段代表規格（可於上方微調坪數）"

    # 取出最具代表性的本戶房屋資料列
    ref_row = unit_match_df.iloc[0]
    current_roc_year = datetime.now().year - 1911
    build_ym = str(ref_row.get('建築完成年月', '')).strip()
    build_y = int(build_ym[:-4]) if len(build_ym) >= 5 and build_ym[:-4].isdigit() else (current_roc_year - 15)
    target_age = max(1, current_roc_year - build_y)
    build_ym_fmt = f"民國{build_ym[:-4]}年{build_ym[-4:-2]}月" if len(build_ym) >= 5 else "未揭露"

    # 計算本戶各項坪數
    target_house_ping = round(float(unit_match_df['house_ping'].median()), 2) if custom_ping <= 0 else round(float(custom_ping), 2)
    target_main_ping = round(float(unit_match_df['main_ping'].median()), 2)
    target_sub_ping = round(float((unit_match_df['sub_ping'] + unit_match_df['balcony_ping']).median()), 2)
    target_park_ping = round(float(unit_match_df['parking_ping'].median()), 2)
    target_total_ping = round(target_house_ping + target_park_ping, 2)

    # 車位參考價自動推算 (若有車位則抓同社區車位中位數價)
    park_rows = unit_match_df[unit_match_df['parking_price'] > 0]
    if park_rows.empty and not nearby_df.empty:
        park_rows = nearby_df[nearby_df['parking_price'] > 0]
    auto_park_price = round(float(park_rows['parking_price'].median()), 0) if not park_rows.empty and target_park_ping > 0 else 0
    final_park_price = auto_park_price if custom_park_price < 0 else custom_park_price

    # 格局、樓層、建材、車位型態
    rooms = str(ref_row.get('建物現況格局-房', '0'))
    halls = str(ref_row.get('建物現況格局-廳', '0'))
    baths = str(ref_row.get('建物現況格局-衛', '0'))
    layout_str = f"{rooms}房{halls}廳{baths}衛" if rooms.isdigit() and int(rooms) > 0 else "開放式/未揭露"
    total_floors_str = str(ref_row.get('總樓層數', '')).replace('層', '樓')
    total_floor_int = int(ref_row.get('total_floor_num', 0))
    display_floor = f"{input_floor_num}樓" if input_floor_num > 0 else str(ref_row.get('移轉層次', '')).replace('層', '樓')
    b_type = str(ref_row.get('建物型態', '')).split('(')[0]
    material = str(ref_row.get('主要建材', '鋼筋混凝土造'))
    manage_org = "有管委會" if str(ref_row.get('有無管理組織', '')) == '有' else "無管委會"
    elevator = "有電梯" if str(ref_row.get('電梯', '有')) == '有' or '大樓' in b_type or '華廈' in b_type else "無電梯"
    park_type = str(ref_row.get('車位類別', '')).strip()
    if not park_type or park_type == 'nan':
        park_type = "坡道平面" if target_park_ping > 0 else "無車位/未拆分"

    # 整理本戶/同柱歷史成交紀錄 (最多列3筆供房仲開發參考)
    unit_history = []
    for _, hr in unit_match_df.head(3).iterrows():
        d_s = str(hr['交易年月日'])
        unit_history.append({
            "date": f"{d_s[:-4]}/{d_s[-4:-2]}" if len(d_s) >= 5 else d_s,
            "addr": hr['clean_addr'],
            "total_ping": round(float(hr['total_ping']), 2),
            "house_ping": round(float(hr['house_ping']), 2),
            "total_price": round(float(hr['total_price']), 0),
            "unit_price": round(float(hr['real_unit_price']), 1)
        })

    # =========================================================================
    # 功能 2：比對「同社區 ＋ 坪數接近」的實價登錄
    # =========================================================================
    # 智慧識別同社區：同條路 + (相同建築完成年月前5碼 或 緊鄰連號且同總樓層數)
    build_prefix = build_ym[:5] if len(build_ym) >= 5 else ""
    if build_prefix:
        same_comm_mask = (
            df['clean_addr'].str.contains(re.escape(road), na=False) &
            (
                (df['建築完成年月'].astype(str).str[:5] == build_prefix) |
                df['clean_addr'].str.contains(nearby_doorplate_regex, na=False)
            )
        )
    else:
        same_comm_mask = df['clean_addr'].str.contains(nearby_doorplate_regex, na=False)

    df['is_same_comm'] = same_comm_mask
    df['is_same_sub'] = df['clean_addr'].str.contains(re.escape(sub_unit), na=False) if sub_unit else False
    df['ping_diff'] = (df['house_ping'] - target_house_ping).abs()

    max_m = df['month_val'].max()
    # 坪數接近區間：依使用者設定之百分比 (預設 ±15%) 或至少 ±4.5 坪容許值
    min_p = min(target_house_ping * (1 - ping_tol), target_house_ping - 4.5)
    max_p = max(target_house_ping * (1 + ping_tol), target_house_ping + 4.5)

    # 優先篩選：同社區 + 坪數接近 + 近一年半內
    comm_close_comps = df[
        df['is_same_comm'] &
        (df['house_ping'] >= min_p) &
        (df['house_ping'] <= max_p) &
        (df['month_val'] >= max_m - 18)
    ].copy()

    # 若近一年半同社區相似坪數不足 3 筆，放寬至同社區全期程相似坪數
    if len(comm_close_comps) < 3:
        comm_close_comps = df[
            df['is_same_comm'] &
            (df['house_ping'] >= min_p) &
            (df['house_ping'] <= max_p)
        ].copy()

    # 若同社區相似坪數仍不足 3 筆，才加入同路段同屋齡段(±5年)相似坪數作為輔助參考
    if len(comm_close_comps) >= 3:
        comps = comm_close_comps
        comp_scope_note = f"✅ 100% 採用【同社區】且坪數接近（{round(min_p,1)}~{round(max_p,1)}坪）之實價登錄進行估價"
    else:
        road_close_comps = df[
            df['clean_addr'].str.contains(re.escape(road), na=False) &
            (df['house_ping'] >= min_p) &
            (df['house_ping'] <= max_p) &
            (df['month_val'] >= max_m - 15)
        ].copy()
        comps = pd.concat([comm_close_comps, road_close_comps]).drop_duplicates(subset=['clean_addr', '交易年月日', '總價元'])
        comp_scope_note = f"ℹ️ 同社區相似坪數共 {len(comm_close_comps)} 筆，已自動納入同路段相似坪數共 {len(comps)} 筆協同估價"

    if comps.empty:
        return {"error": f"查無該社區周邊坪數接近 {target_house_ping} 坪之交易，請嘗試調整坪數或放寬坪數範圍。"}

    # 排序：同社區優先 > 同戶別(之幾)優先 > 交易時間最新 > 坪數最接近
    comps = comps.sort_values(
        by=['is_same_comm', 'is_same_sub', 'month_val', 'ping_diff'],
        ascending=[False, False, False, True]
    )

    # =========================================================================
    # 功能 3：房仲實戰估價 — 建議售價、建議開價、建議底價(含服務費)
    # =========================================================================
    # 計算基礎優先以「同社區相似坪數」為主，避免被外圍低價公寓拉低
    val_source_df = comm_close_comps if len(comm_close_comps) >= 2 else comps
    q = val_source_df['real_unit_price'].quantile([0.25, 0.5, 0.75]).to_dict()
    low_u, med_u, high_u = round(q[0.25], 1), round(q[0.5], 1), round(q[0.75], 1)

    # 樓層高低微調係數 (若使用者有輸入樓層：中高樓層微調 +1.5%，低樓層 2~3樓 -1.5%)
    floor_factor = 1.0
    floor_adj_note = "標準樓層係數 (1.00)"
    if input_floor_num > 0 and total_floor_int > 4:
        ratio = input_floor_num / total_floor_int
        if input_floor_num == 4:
            floor_factor = 0.98
            floor_adj_note = "4樓諧音微調 (-2%)"
        elif ratio >= 0.75 and input_floor_num < total_floor_int:
            floor_factor = 1.02
            floor_adj_note = "中高樓層景觀加分 (+2%)"
        elif input_floor_num == total_floor_int:
            floor_factor = 0.99
            floor_adj_note = "頂樓係數微調 (-1%)"
        elif input_floor_num <= 3:
            floor_factor = 0.985
            floor_adj_note = "低樓層係數微調 (-1.5%)"

    # 1. 建議售價 (合理市場成交價) = 調整後單價 × 本戶扣車位建坪 + 車位價格
    suggested_unit = round(med_u * floor_factor, 1)
    suggested_sale_price = round(suggested_unit * target_house_ping + final_park_price)

    # 2. 建議開價 (預留 10% 買方議價空間，前端可動態調整)
    asking_price = round(suggested_sale_price * 1.10)
    asking_unit = round((asking_price - final_park_price) / target_house_ping, 1) if target_house_ping > 0 else 0

    # 3. 建議底價 (含 4% 賣方服務費) 與 屋主實拿金額
    # 底價(含服務費) 設定為合理成交價之 97% (或貼近 PR35 區間)，確保具備成交競爭力且內含仲介費
    bottom_price_incl_fee = round(suggested_sale_price * 0.97)
    default_fee_rate = 0.04  # 預設賣方 4% 服務費
    service_fee_amt = round(bottom_price_incl_fee * default_fee_rate, 1)
    seller_net_proceeds = round(bottom_price_incl_fee - service_fee_amt, 1)

    # 整理實價登錄比對清單 (最多15筆)
    comp_list = []
    for _, r in comps.head(15).iterrows():
        d_str = str(r['交易年月日'])
        f_date = f"{d_str[:-4]}/{d_str[-4:-2]}" if len(d_str) >= 5 else d_str
        p_diff = round(float(r['house_ping']) - target_house_ping, 1)
        p_diff_str = f"+{p_diff}坪" if p_diff > 0 else (f"{p_diff}坪" if p_diff < 0 else "坪數相同")
        comp_list.append({
            "date": f_date,
            "addr": r['clean_addr'],
            "total_ping": round(float(r['total_ping']), 1),
            "house_ping": round(float(r['house_ping']), 1),
            "ping_diff": p_diff_str,
            "floor": f"{str(r['移轉層次']).replace('層','樓')}/{str(r['總樓層數']).replace('層','樓')}",
            "layout": f"{r.get('建物現況格局-房','0')}房{r.get('建物現況格局-廳','0')}廳{r.get('建物現況格局-衛','0')}衛",
            "unit": round(float(r['real_unit_price']), 1),
            "total": round(float(r['total_price']), 0),
            "park": round(float(r['parking_price']), 0),
            "is_same_comm": bool(r['is_same_comm']),
            "is_same_sub": bool(r['is_same_sub'])
        })

    return {
        "target": {
            "addr": clean_input,
            "match_level": match_level,
            "total_ping": target_total_ping,
            "house_ping": target_house_ping,
            "main_ping": target_main_ping,
            "sub_ping": target_sub_ping,
            "park_ping": target_park_ping,
            "park_type": park_type,
            "park_price": final_park_price,
            "layout": layout_str,
            "age": target_age,
            "build_ym": build_ym_fmt,
            "floor": display_floor,
            "total_floors": total_floors_str,
            "type": b_type,
            "material": material,
            "manage_org": manage_org,
            "elevator": elevator,
            "history": unit_history
        },
        "val": {
            "scope_note": comp_scope_note,
            "total_count": len(comps),
            "comm_count": len(comm_close_comps),
            "floor_adj_note": floor_adj_note,
            "low_u": low_u,
            "med_u": med_u,
            "high_u": high_u,
            "suggested_unit": suggested_unit,
            "suggested_sale": suggested_sale_price,
            "asking_price": asking_price,
            "asking_unit": asking_unit,
            "bottom_incl_fee": bottom_price_incl_fee,
            "service_fee": service_fee_amt,
            "seller_net": seller_net_proceeds
        },
        "comps": comp_list
    }


@app.get("/", response_class=HTMLResponse)
def home():
    return """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>房仲實戰估價系統｜同社區實價比對＆委託底價試算</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #0f172a; }
        .app-box { max-width: 540px; margin: 0 auto; min-height: 100vh; background-color: #f1f5f9; padding-bottom: 48px; }
        .spin { animation: spin 1s linear infinite; }
        @keyframes spin { 100% { transform: rotate(360deg); } }
    </style>
</head>
<body>
<div class="app-box shadow-2xl">
    <!-- 頂部搜尋列 -->
    <div class="bg-gradient-to-br from-slate-900 via-blue-900 to-indigo-900 text-white p-5 rounded-b-3xl shadow-lg">
        <div class="flex items-center justify-between mb-1.5">
            <h1 class="text-lg font-extrabold tracking-tight">
                <i class="fa-solid fa-building-circle-check text-amber-400 mr-1.5"></i>房仲社區實價＆委託估價系統
            </h1>
            <span class="text-[10px] bg-emerald-500 text-white px-2.5 py-0.5 rounded-full font-bold shadow">內政部即時連線</span>
        </div>
        <p class="text-blue-200 text-xs mb-4">輸入門牌自動帶出本戶產權規格、鎖定同社區相近坪數實價，並計算建議售價、開價與含服務費底價。</p>

        <div class="bg-white p-4 rounded-2xl text-gray-800 shadow-xl space-y-3">
            <div>
                <label class="text-[11px] font-bold text-slate-500">📍 輸入完整門牌地址（含樓層/之幾）：</label>
                <input type="text" id="addr" value="新竹市西濱路一段185號9樓之3"
                       placeholder="例：新竹市西濱路一段185號9樓之3"
                       class="w-full text-sm font-extrabold text-slate-900 border-b-2 border-blue-200 py-1.5 focus:outline-none focus:border-blue-600">
            </div>

            <div class="grid grid-cols-3 gap-2">
                <div class="bg-slate-50 p-2 rounded-xl border border-slate-200">
                    <label class="text-[10px] text-gray-500 font-semibold block">扣車位建坪 (選填)</label>
                    <input type="number" step="0.1" id="ping" placeholder="自動抓取本戶" class="w-full text-xs font-bold text-blue-700 bg-transparent focus:outline-none mt-0.5">
                </div>
                <div class="bg-slate-50 p-2 rounded-xl border border-slate-200">
                    <label class="text-[10px] text-gray-500 font-semibold block">車位價值 (萬元)</label>
                    <input type="number" id="park" placeholder="自動推算" class="w-full text-xs font-bold text-blue-700 bg-transparent focus:outline-none mt-0.5">
                </div>
                <div class="bg-slate-50 p-2 rounded-xl border border-slate-200">
                    <label class="text-[10px] text-gray-500 font-semibold block">比對坪數誤差</label>
                    <select id="tol" class="w-full text-xs font-bold text-slate-700 bg-transparent focus:outline-none mt-0.5">
                        <option value="0.10">極近 (±10%)</option>
                        <option value="0.15" selected>相近 (±15%)</option>
                        <option value="0.25">放寬 (±25%)</option>
                    </select>
                </div>
            </div>

            <button onclick="runQuery()" class="w-full bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-700 hover:to-indigo-700 active:scale-95 transition text-white font-bold py-3 rounded-xl shadow-md flex justify-center items-center space-x-2">
                <i class="fa-solid fa-magnifying-glass-chart"></i>
                <span>帶出本戶資料 ＆ 執行社區比價估價</span>
            </button>
        </div>
    </div>

    <!-- 讀取動畫 -->
    <div id="loading" class="hidden flex-col items-center justify-center py-14 px-6 text-center">
        <i class="fa-solid fa-circle-notch text-4xl text-blue-600 spin mb-3"></i>
        <p class="text-sm font-bold text-gray-700">正在連線內政部資料庫檢索本戶與同社區實價...</p>
        <p class="text-xs text-gray-400 mt-1">首次載入該縣市約需 5~10 秒，快取完成後僅需 0.1 秒！</p>
    </div>

    <!-- 錯誤訊息 -->
    <div id="err" class="hidden m-4 p-4 bg-red-50 border border-red-200 text-red-600 rounded-xl text-xs font-bold"></div>

    <!-- 結果區 -->
    <div id="res" class="hidden px-3.5 mt-4 space-y-4">

        <!-- 區塊 1：這間房屋的完整資料 -->
        <div class="bg-white p-4 rounded-2xl shadow-sm border-t-4 border-blue-600">
            <div class="flex items-center justify-between mb-1">
                <span class="text-[11px] bg-blue-50 text-blue-700 px-2 py-0.5 rounded font-bold" id="matchLevel"></span>
                <span class="text-[11px] text-gray-400 font-semibold" id="buildYm"></span>
            </div>
            <h2 id="rAddr" class="text-base font-black text-slate-800 mt-1"></h2>

            <!-- 核心坪數拆解 -->
            <div class="grid grid-cols-4 gap-1.5 text-center bg-slate-50 p-2.5 rounded-xl mt-3 border border-slate-200">
                <div>
                    <p class="text-[10px] text-gray-400">權狀總坪</p>
                    <p id="rTotalPing" class="text-sm font-black text-slate-800"></p>
                </div>
                <div class="border-l border-slate-200">
                    <p class="text-[10px] text-blue-600 font-bold">扣車位建坪</p>
                    <p id="rHousePing" class="text-sm font-black text-blue-700"></p>
                </div>
                <div class="border-l border-slate-200">
                    <p class="text-[10px] text-gray-400">主建物坪</p>
                    <p id="rMainPing" class="text-sm font-bold text-slate-700"></p>
                </div>
                <div class="border-l border-slate-200">
                    <p class="text-[10px] text-gray-400">附屬+陽台</p>
                    <p id="rSubPing" class="text-sm font-bold text-slate-700"></p>
                </div>
            </div>

            <!-- 房屋規格細節 -->
            <div class="grid grid-cols-3 gap-2 text-xs mt-2.5 bg-blue-50/40 p-2.5 rounded-xl border border-blue-100">
                <div><span class="text-gray-400">格局：</span><strong id="rLayout" class="text-slate-800"></strong></div>
                <div><span class="text-gray-400">樓層：</span><strong id="rFloor" class="text-slate-800"></strong></div>
                <div><span class="text-gray-400">屋齡：</span><strong id="rAge" class="text-slate-800"></strong></div>
                <div><span class="text-gray-400">型態：</span><strong id="rType" class="text-slate-800"></strong></div>
                <div><span class="text-gray-400">車位：</span><strong id="rPark" class="text-slate-800"></strong></div>
                <div><span class="text-gray-400">社區：</span><strong id="rManage" class="text-slate-800"></strong></div>
            </div>

            <!-- 本戶/同柱歷史成交紀錄 -->
            <div id="histBox" class="mt-3 pt-2.5 border-t border-slate-100 hidden">
                <p class="text-[11px] font-bold text-slate-500 mb-1.5"><i class="fa-solid fa-clock-rotate-left mr-1"></i>本戶 / 同柱標準層歷次成交紀錄：</p>
                <div id="histList" class="space-y-1 text-[11px]"></div>
            </div>
        </div>

        <!-- 區塊 2：估價報告 (建議售價、開價、含服務費底價) -->
        <div class="bg-white p-4 rounded-2xl shadow-sm border-t-4 border-indigo-600">
            <div class="flex justify-between items-center mb-2">
                <h3 class="font-extrabold text-slate-800 text-sm"><i class="fa-solid fa-sack-dollar text-amber-500 mr-1.5"></i>房仲委託估價建議書</h3>
                <span id="rFloorAdj" class="text-[10px] bg-indigo-50 text-indigo-700 px-2 py-0.5 rounded-full font-bold"></span>
            </div>
            <p id="scopeNote" class="text-[11px] text-emerald-700 bg-emerald-50 px-2.5 py-1 rounded-lg font-bold mb-3"></p>

            <!-- 三大關鍵價格卡片 -->
            <div class="grid grid-cols-3 gap-2 text-center mb-3">
                <!-- 1. 建議開價 -->
                <div class="bg-amber-50/80 p-3 rounded-2xl border border-amber-200 flex flex-col justify-between">
                    <div>
                        <span class="text-[10px] bg-amber-200 text-amber-900 px-1.5 py-0.5 rounded font-bold">1. 建議開價</span>
                        <p class="text-[10px] text-amber-700 mt-1">預留議價空間</p>
                    </div>
                    <div class="my-1.5">
                        <p id="vAskingTotal" class="text-xl font-black text-amber-700"></p>
                        <p class="text-[10px] text-gray-500">萬元</p>
                    </div>
                    <p id="vAskingUnit" class="text-[10px] font-bold text-amber-800 bg-amber-100/80 py-0.5 rounded"></p>
                </div>

                <!-- 2. 建議售價 (合理成交) -->
                <div class="bg-gradient-to-b from-blue-600 to-indigo-700 text-white p-3 rounded-2xl shadow-md flex flex-col justify-between transform scale-[1.03]">
                    <div>
                        <span class="text-[10px] bg-yellow-400 text-slate-900 px-1.5 py-0.5 rounded font-extrabold">2. 建議售價</span>
                        <p class="text-[10px] text-blue-100 mt-1">合理市場成交價</p>
                    </div>
                    <div class="my-1.5">
                        <p id="vSaleTotal" class="text-2xl font-black text-white"></p>
                        <p class="text-[10px] text-blue-200">萬元</p>
                    </div>
                    <p id="vSaleUnit" class="text-[10px] font-bold text-yellow-300 bg-white/15 py-0.5 rounded"></p>
                </div>

                <!-- 3. 建議底價 (含服務費) -->
                <div class="bg-emerald-50/80 p-3 rounded-2xl border border-emerald-200 flex flex-col justify-between">
                    <div>
                        <span class="text-[10px] bg-emerald-200 text-emerald-900 px-1.5 py-0.5 rounded font-bold">3. 建議底價</span>
                        <p class="text-[10px] text-emerald-700 mt-1">含仲介服務費</p>
                    </div>
                    <div class="my-1.5">
                        <p id="vBottomTotal" class="text-xl font-black text-emerald-700"></p>
                        <p class="text-[10px] text-gray-500">萬元</p>
                    </div>
                    <p id="vBottomUnit" class="text-[10px] font-bold text-emerald-800 bg-emerald-100/80 py-0.5 rounded"></p>
                </div>
            </div>

            <!-- 互動式委託底價 ＆ 服務費拆算器 -->
            <div class="bg-slate-50 p-3.5 rounded-2xl border border-slate-200 space-y-2.5">
                <div class="flex items-center justify-between border-b border-slate-200 pb-2">
                    <span class="text-xs font-extrabold text-slate-700"><i class="fa-solid fa-calculator text-blue-600 mr-1"></i>委託底價（含服務費）與屋主實拿拆算</span>
                    <span class="text-[10px] text-gray-400">可即時調整參數</span>
                </div>

                <div class="grid grid-cols-2 gap-2 text-xs">
                    <div>
                        <label class="text-[10px] text-gray-500 font-bold block mb-0.5">賣方服務費率 (%)</label>
                        <select id="feeRate" onchange="recalcPricing()" class="w-full bg-white border border-slate-300 rounded-lg px-2 py-1.5 font-bold text-slate-800">
                            <option value="0.04" selected>4% (標準賣方服務費)</option>
                            <option value="0.03">3% (優惠服務費)</option>
                            <option value="0.02">2% (專案服務費)</option>
                            <option value="0.05">5% (含買方統包)</option>
                            <option value="0.06">6% (法定上限)</option>
                        </select>
                    </div>
                    <div>
                        <label class="text-[10px] text-gray-500 font-bold block mb-0.5">開價加價議價空間 (%)</label>
                        <select id="markupRate" onchange="recalcPricing()" class="w-full bg-white border border-slate-300 rounded-lg px-2 py-1.5 font-bold text-slate-800">
                            <option value="0.08">+8% (平實開價)</option>
                            <option value="0.10" selected>+10% (標準議價空間)</option>
                            <option value="0.12">+12% (充裕議價空間)</option>
                            <option value="0.15">+15% (高開議價空間)</option>
                        </select>
                    </div>
                </div>

                <!-- 拆算結果明細 -->
                <div class="bg-white p-3 rounded-xl border border-slate-200 text-xs space-y-1.5">
                    <div class="flex justify-between items-center">
                        <span class="text-gray-500">📌 委託底價總額（含服務費）：</span>
                        <strong id="calcBottomIncl" class="text-sm font-black text-emerald-700"></strong>
                    </div>
                    <div class="flex justify-between items-center">
                        <span class="text-gray-500">💼 內含仲介服務費金額：</span>
                        <strong id="calcFeeAmt" class="text-rose-600 font-bold"></strong>
                    </div>
                    <div class="flex justify-between items-center pt-1 border-t border-dashed border-slate-200">
                        <span class="text-slate-800 font-bold">💰 屋主扣除服務費後【實拿淨額】：</span>
                        <strong id="calcSellerNet" class="text-base font-black text-blue-700"></strong>
                    </div>
                </div>

                <!-- 屋主期望實拿反推工具 -->
                <div class="pt-1">
                    <label class="text-[10px] text-indigo-700 font-bold block mb-1">💡 若屋主指定「扣服務費後要實拿 (萬元)」，自動反推含服務費底價：</label>
                    <div class="flex space-x-2">
                        <input type="number" id="customNetInput" oninput="reverseFromNet()" placeholder="輸入屋主想實拿的萬數..." class="flex-1 bg-white border border-indigo-200 rounded-lg px-2.5 py-1 text-xs font-bold focus:outline-none focus:border-indigo-500">
                        <span id="reverseResult" class="text-xs font-extrabold text-indigo-800 bg-indigo-50 px-3 py-1 rounded-lg flex items-center whitespace-nowrap">底價(含服務費)：-- 萬</span>
                    </div>
                </div>
            </div>
        </div>

        <!-- 區塊 3：同社區且坪數接近之實價登錄列表 -->
        <div class="bg-white p-4 rounded-2xl shadow-sm">
            <div class="flex justify-between items-center mb-1">
                <h3 class="font-extrabold text-slate-800 text-sm">📋 同社區＆相近坪數實價登錄明細</h3>
                <span id="rCount" class="text-[11px] bg-blue-100 text-blue-800 px-2.5 py-0.5 rounded-full font-bold"></span>
            </div>
            <p class="text-[11px] text-gray-400 mb-3">優先排列【同社區、同柱戶別、坪數最接近】之真實交易（已排除特殊交易）</p>
            <div id="list" class="space-y-2.5"></div>
        </div>
    </div>
</div>

<script>
let currentData = null;

async function runQuery() {
    const addr = document.getElementById('addr').value.trim();
    const ping = document.getElementById('ping').value || 0;
    const parkInput = document.getElementById('park').value;
    const park = parkInput === "" ? -1 : parseFloat(parkInput);
    const tol = document.getElementById('tol').value || 0.15;

    if (!addr) return alert("請輸入地址！");
    document.getElementById('err').classList.add('hidden');
    document.getElementById('res').classList.add('hidden');
    document.getElementById('loading').classList.remove('hidden');
    document.getElementById('loading').classList.add('flex');

    try {
        const url = `/api/analyze?address=${encodeURIComponent(addr)}&custom_ping=${ping}&custom_park_price=${park}&ping_tol=${tol}`;
        const res = await fetch(url);
        const d = await res.json();
        document.getElementById('loading').classList.add('hidden');
        document.getElementById('loading').classList.remove('flex');

        if (d.error) {
            const errEl = document.getElementById('err');
            errEl.innerText = d.error;
            errEl.classList.remove('hidden');
            return;
        }

        currentData = d;
        renderData(d);
    } catch (e) {
        document.getElementById('loading').classList.add('hidden');
        document.getElementById('err').innerText = "網路連線異常，請重試。";
        document.getElementById('err').classList.remove('hidden');
    }
}

function renderData(d) {
    // 1. 渲染本戶資料
    document.getElementById('matchLevel').innerText = d.target.match_level;
    document.getElementById('buildYm').innerText = `建築完成：${d.target.build_ym}`;
    document.getElementById('rAddr').innerText = d.target.addr;
    document.getElementById('rTotalPing').innerText = `${d.target.total_ping} 坪`;
    document.getElementById('rHousePing').innerText = `${d.target.house_ping} 坪`;
    document.getElementById('rMainPing').innerText = d.target.main_ping > 0 ? `${d.target.main_ping} 坪` : "未拆分";
    document.getElementById('rSubPing').innerText = d.target.sub_ping > 0 ? `${d.target.sub_ping} 坪` : "未拆分";

    document.getElementById('rLayout').innerText = d.target.layout;
    document.getElementById('rFloor').innerText = `${d.target.floor} / 共${d.target.total_floors}`;
    document.getElementById('rAge').innerText = `${d.target.age} 年`;
    document.getElementById('rType').innerText = d.target.type;
    const parkInfo = d.target.park_ping > 0 ? `${d.target.park_ping}坪 (${d.target.park_price}萬)` : "無車位";
    document.getElementById('rPark').innerText = parkInfo;
    document.getElementById('rManage').innerText = `${d.target.manage_org}・${d.target.elevator}`;

    // 本戶歷史紀錄
    const histBox = document.getElementById('histBox');
    const histList = document.getElementById('histList');
    histList.innerHTML = '';
    if (d.target.history && d.target.history.length > 0) {
        histBox.classList.remove('hidden');
        d.target.history.forEach(h => {
            histList.innerHTML += `
                <div class="flex justify-between items-center bg-slate-50 px-2.5 py-1.5 rounded-lg border border-slate-200">
                    <span class="text-slate-700 font-semibold">${h.date}｜${h.addr} <span class="text-gray-400">(建坪${h.house_ping}坪)</span></span>
                    <span class="font-bold text-blue-700">總價 ${h.total_price} 萬 <span class="text-rose-600">(${h.unit_price}萬/坪)</span></span>
                </div>`;
        });
    } else {
        histBox.classList.add('hidden');
    }

    // 2. 渲染估價區塊
    document.getElementById('scopeNote').innerText = d.val.scope_note;
    document.getElementById('rFloorAdj').innerText = d.val.floor_adj_note;
    recalcPricing();

    // 3. 渲染同社區相似坪數實價列表
    document.getElementById('rCount').innerText = `同社區 ${d.val.comm_count} 筆 / 共比對 ${d.val.total_count} 筆`;
    const list = document.getElementById('list');
    list.innerHTML = '';
    d.comps.forEach(c => {
        let badge = `<span class="bg-slate-200 text-slate-700 text-[10px] px-1.5 py-0.5 rounded font-bold">同路段相似坪數</span>`;
        if (c.is_same_comm && c.is_same_sub) {
            badge = `<span class="bg-indigo-600 text-white text-[10px] px-1.5 py-0.5 rounded font-bold">★ 同社區同戶別</span>`;
        } else if (c.is_same_comm) {
            badge = `<span class="bg-blue-600 text-white text-[10px] px-1.5 py-0.5 rounded font-bold">🏢 同社區相似坪數</span>`;
        }
        const parkTxt = c.park > 0 ? `含車位 ${c.park} 萬` : `無車位或未拆分`;
        list.innerHTML += `
            <div class="p-3 rounded-xl border ${c.is_same_comm ? 'border-blue-300 bg-blue-50/40' : 'border-slate-200 bg-slate-50'} flex justify-between items-center">
                <div class="pr-2 space-y-1">
                    <div class="flex items-center flex-wrap gap-1">${badge}<span class="text-xs font-extrabold text-slate-800">${c.addr}</span></div>
                    <div class="text-[11px] text-slate-600">
                        📅 <b>${c.date}</b> • 扣車位 <b class="text-blue-700">${c.house_ping}坪</b> <span class="text-[10px] bg-amber-100 text-amber-800 px-1 rounded font-bold">${c.ping_diff}</span> • ${c.floor}
                    </div>
                    <div class="text-[10px] text-gray-400">權狀總坪 ${c.total_ping} 坪｜${c.layout}｜${parkTxt}</div>
                </div>
                <div class="text-right whitespace-nowrap pl-2 border-l border-slate-200">
                    <p class="text-sm font-black text-rose-600">${c.unit} <span class="text-[10px]">萬/坪</span></p>
                    <p class="text-xs font-extrabold text-slate-800">總價 ${c.total.toLocaleString()} 萬</p>
                </div>
            </div>`;
    });

    document.getElementById('res').classList.remove('hidden');
}

function recalcPricing() {
    if (!currentData) return;
    const feeRate = parseFloat(document.getElementById('feeRate').value) || 0.04;
    const markupRate = parseFloat(document.getElementById('markupRate').value) || 0.10;

    const housePing = currentData.target.house_ping;
    const parkPrice = currentData.target.park_price;
    const saleTotal = currentData.val.suggested_sale;
    const saleUnit = currentData.val.suggested_unit;

    // 1. 建議開價
    const askingTotal = Math.round(saleTotal * (1 + markupRate));
    const askingUnit = housePing > 0 ? ((askingTotal - parkPrice) / housePing).toFixed(1) : 0;

    // 2. 建議底價 (含服務費)
    const bottomIncl = currentData.val.bottom_incl_fee;
    const bottomUnit = housePing > 0 ? ((bottomIncl - parkPrice) / housePing).toFixed(1) : 0;
    const feeAmt = (bottomIncl * feeRate).toFixed(1);
    const sellerNet = (bottomIncl - feeAmt).toFixed(1);

    document.getElementById('vAskingTotal').innerText = askingTotal.toLocaleString();
    document.getElementById('vAskingUnit').innerText = `單價 ${askingUnit} 萬/坪`;

    document.getElementById('vSaleTotal').innerText = saleTotal.toLocaleString();
    document.getElementById('vSaleUnit').innerText = `單價 ${saleUnit} 萬/坪`;

    document.getElementById('vBottomTotal').innerText = bottomIncl.toLocaleString();
    document.getElementById('vBottomUnit').innerText = `單價 ${bottomUnit} 萬/坪`;

    document.getElementById('calcBottomIncl').innerText = `${bottomIncl.toLocaleString()} 萬元`;
    document.getElementById('calcFeeAmt').innerText = `${feeAmt} 萬元 (${Math.round(feeRate*100)}%)`;
    document.getElementById('calcSellerNet').innerText = `${sellerNet} 萬元`;

    // 同步更新反推預設值
    const netInput = document.getElementById('customNetInput');
    if (!netInput.value) {
        netInput.value = Math.round(sellerNet);
    }
    reverseFromNet();
}

function reverseFromNet() {
    const netVal = parseFloat(document.getElementById('customNetInput').value);
    const feeRate = parseFloat(document.getElementById('feeRate').value) || 0.04;
    const outEl = document.getElementById('reverseResult');
    if (!netVal || netVal <= 0) {
        outEl.innerText = "底價(含服務費)：-- 萬";
        return;
    }
    // 含服務費底價 = 屋主實拿 ÷ (1 - 服務費率)
    const reqBottom = Math.ceil(netVal / (1 - feeRate));
    const fee = (reqBottom - netVal).toFixed(1);
    outEl.innerText = `應簽底價：${reqBottom} 萬 (含費${fee}萬)`;
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
