# 檔案名稱：app.py (v5.3 萬用台灣地址解析器 + 同社區一年內行情極速版)
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import pandas as pd
import requests
import urllib3
import zipfile
import io
import csv
import re
import gc
import traceback
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
    addr = addr.replace("台", "臺").replace(" ", "").replace("　", "").strip()
    full_to_half = str.maketrans("０１２３４５６７８９－～Ｆ", "0123456789-~F")
    addr = addr.translate(full_to_half)
    def repl_sec(m):
        return f"{cn_to_num(m.group(1))}{m.group(2)}"
    addr = re.sub(r'([一二三四五六七八九十]+)(段|樓|層|巷|弄|號)', repl_sec, addr)
    return addr

def parse_any_tw_address(clean_addr: str):
    """
    萬用台灣地址解析器：支援所有『X路X段、大道、街、巷、弄、村里、之幾號、樓之幾』！
    解決『4段』含有數字被舊版正則表達式擋掉的 Bug！
    """
    s = re.sub(r'^\d{3,6}', '', clean_addr)  # 移除開頭郵遞區號

    # 1. 抓出樓層 (在「號」後面的樓層)
    fl_m = re.search(r'號.*?(\d+)[樓層F]', s)
    floor_num = int(fl_m.group(1)) if fl_m else 0

    # 2. 抓出門牌號碼 (例如 180號、180之1號、180-1號)
    num_m = re.search(r'(\d+)(?:之\d+|-\d+)?號', s)
    if not num_m:
        return None
    door_num = int(num_m.group(1))

    # 取「號」之前的字串來拆解縣市、區、路、巷、弄
    prefix = s[:num_m.start()]

    # 3. 抓出縣市
    city = ""
    city_m = re.match(r'^([臺台]北市|新北市|桃園市|[臺台]中市|[臺台]南市|高雄市|基隆市|新竹市|嘉義市|.{2}[縣市])', prefix)
    if city_m:
        city = city_m.group(1)
        prefix = prefix[len(city):]

    # 4. 抓出行政區 (區/鄉/鎮/市)
    district = ""
    dist_m = re.match(r'^(.{1,4}?[區鄉鎮市])', prefix)
    if dist_m:
        district = dist_m.group(1)
        prefix = prefix[len(district):]

    # 移除村里鄰 (例如 瑞陽里12鄰)，但若沒有路名只有村里地名則保留
    prefix_no_li = re.sub(r'^.{1,4}?[里村](\d+鄰)?', '', prefix)
    if prefix_no_li:
        prefix = prefix_no_li

    # 5. 抓出巷、弄
    lane = ""
    alley = ""
    lane_m = re.search(r'(\d+)巷', prefix)
    if lane_m:
        lane = lane_m.group(1)
        prefix = prefix[:lane_m.start()]
    alley_m = re.search(r'(\d+)弄', prefix)
    if alley_m:
        alley = alley_m.group(1)
        prefix = prefix[:alley_m.start()]

    # 剩下的 prefix 就是完整的「路/街/段/大道/地名」(例如：成功路4段、市民大道1段、文化二路1段)
    road = prefix.strip()
    if not road:
        return None

    return {
        "city": city,
        "district": district,
        "road": road,
        "lane": lane,
        "alley": alley,
        "num": door_num,
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

def safe_float(val) -> float:
    try:
        return float(val) if val else 0.0
    except Exception:
        return 0.0

def fetch_road_stream_data(city_code: str, road_name: str) -> pd.DataFrame:
    cache_key = f"{city_code}_{road_name}"
    if cache_key in ROAD_DATA_CACHE:
        return ROAD_DATA_CACHE[cache_key]

    if len(ROAD_DATA_CACHE) > 10:
        ROAD_DATA_CACHE.clear()
        gc.collect()

    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    target_filename = f"{city_code}_lvr_land_a.csv"

    urls_to_fetch = ["https://plvr.land.moi.gov.tw/Download?type=zip&fileName=lvr_landcsv.zip"]
    now = datetime.now()
    roc_year = now.year - 1911
    for y in range(roc_year, roc_year - 3, -1):
        for s in [4, 3, 2, 1]:
            urls_to_fetch.append(f"https://plvr.land.moi.gov.tw/DownloadSeason?season={y}S{s}&type=zip&fileName=lvr_landcsv.zip")

    # 去掉「X段」作為初步快速篩選詞 (例如 "成功路4段" 先篩 "成功路")
    road_base = re.sub(r'\d+段', '', road_name)
    matched_rows = []
    current_roc_year = now.year - 1911

    success_seasons = 0
    for url in urls_to_fetch:
        if success_seasons >= 4:
            break
        try:
            resp = requests.get(url, headers=headers, timeout=12, verify=False)
            if resp.status_code == 200 and len(resp.content) > 5000:
                with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                    if target_filename in z.namelist():
                        with z.open(target_filename) as f:
                            text_stream = io.TextIOWrapper(f, encoding='utf-8', errors='ignore')
                            reader = csv.reader(text_stream)
                            header = next(reader, None)
                            if not header:
                                continue
                            col_idx = {}
                            for i, h in enumerate(header):
                                for kw in ['鄉鎮市區', '交易標的', '門牌', '交易年月日', '移轉層次', '總樓層數', '建物型態', '主要用途', '建築完成', '建物移轉總面積', '主建物面積', '附屬建物面積', '陽台面積', '格局-房', '格局-廳', '格局-衛', '總價元', '單價元平方公尺', '車位移轉總面積', '車位總價元', '備註', '有無管理組織']:
                                    if kw in h and kw not in col_idx:
                                        col_idx[kw] = i

                            addr_i = col_idx.get('門牌', 2)
                            type_i = col_idx.get('交易標的', 1)

                            next(reader, None)  # 跳過英文標頭

                            for row in reader:
                                if len(row) <= addr_i:
                                    continue
                                raw_addr = row[addr_i]
                                if road_base not in raw_addr and road_base.replace("臺", "台") not in raw_addr:
                                    continue
                                t_type = row[type_i] if len(row) > type_i else ""
                                if "房地" not in t_type and "建物" not in t_type:
                                    continue
                                clean_a = normalize_address(raw_addr)
                                if road_name not in clean_a:
                                    continue

                                def g(k, default=""):
                                    idx = col_idx.get(k, -1)
                                    return row[idx].strip() if (idx >= 0 and len(row) > idx) else default

                                note_str = g('備註', '')
                                if any(bad in note_str for bad in ['親友', '特殊', '急買', '急賣', '瑕疵', '共有人', '標售', '債權']):
                                    continue

                                total_ping = safe_float(g('建物移轉總面積', '0')) * 0.3025
                                main_ping = safe_float(g('主建物面積', '0')) * 0.3025
                                sub_ping = (safe_float(g('附屬建物面積', '0')) + safe_float(g('陽台面積', '0'))) * 0.3025
                                park_ping = safe_float(g('車位移轉總面積', '0')) * 0.3025
                                total_price = safe_float(g('總價元', '0')) / 10000.0
                                park_price = safe_float(g('車位總價元', '0')) / 10000.0
                                moi_unit = safe_float(g('單價元平方公尺', '0')) * 0.0003305785

                                house_ping = total_ping - park_ping
                                if park_price > 0 and park_ping == 0 and moi_unit > 5:
                                    house_ping = (total_price - park_price) / moi_unit

                                if house_ping <= 3:
                                    continue

                                if park_price > 0 and park_ping > 0:
                                    real_unit = (total_price - park_price) / house_ping
                                elif moi_unit > 5:
                                    real_unit = moi_unit
                                else:
                                    real_unit = total_price / house_ping

                                if real_unit < 6 or real_unit > 400:
                                    continue

                                build_ym = g('建築完成', '')
                                age = max(0, current_roc_year - int(build_ym[:-4])) if (len(build_ym) >= 5 and build_ym[:-4].isdigit()) else -1
                                pub_ping = max(0.0, house_ping - main_ping - sub_ping)

                                parsed_row_addr = parse_any_tw_address(clean_a)
                                p_lane = parsed_row_addr['lane'] if parsed_row_addr else ""
                                p_alley = parsed_row_addr['alley'] if parsed_row_addr else ""
                                p_num = parsed_row_addr['num'] if parsed_row_addr else -999

                                matched_rows.append({
                                    'district': g('鄉鎮市區', ''),
                                    'clean_addr': clean_a,
                                    'date_str': g('交易年月日', ''),
                                    'month_val': parse_roc_date(g('交易年月日', '')),
                                    'floor_raw': g('移轉層次', '-'),
                                    'floor_num': cn_to_num(g('移轉層次', '-')),
                                    'total_floors_raw': g('總樓層數', '-'),
                                    'total_floors_num': cn_to_num(g('總樓層數', '-')),
                                    'b_type': g('建物型態', '住宅大樓').split('(')[0],
                                    'usage': g('主要用途', '住家用'),
                                    'mgmt': g('有無管理組織', '有'),
                                    'build_ym': build_ym,
                                    'age': age,
                                    'rooms': g('格局-房', '3') or '3',
                                    'halls': g('格局-廳', '2') or '2',
                                    'baths': g('格局-衛', '2') or '2',
                                    'total_ping': total_ping,
                                    'house_ping': house_ping,
                                    'main_ping': main_ping,
                                    'sub_ping': sub_ping,
                                    'public_ping': pub_ping,
                                    'parking_ping': park_ping,
                                    'total_price': total_price,
                                    'parking_price': park_price,
                                    'real_unit_price': real_unit,
                                    'p_lane': p_lane,
                                    'p_alley': p_alley,
                                    'p_num': p_num
                                })
                        success_seasons += 1
            del resp
            gc.collect()
        except Exception:
            continue

    if not matched_rows:
        return pd.DataFrame()

    df = pd.DataFrame(matched_rows)
    df = df.drop_duplicates(subset=['clean_addr', 'date_str', 'total_price']).reset_index(drop=True)

    ROAD_DATA_CACHE[cache_key] = df
    gc.collect()
    return df

@app.get("/api/analyze")
def analyze(address: str = Query(...), custom_ping: float = Query(0), range_ratio: float = Query(0.25)):
    try:
        clean_input = normalize_address(address)
        feat = parse_any_tw_address(clean_input)
        if not feat:
            return {"error": f"無法從『{address}』識別出門牌號碼（需包含路名與幾號，例如：台北市內湖區成功路四段180號6樓）"}

        city_code = None
        for city_name, code in CITY_CODE_MAP.items():
            if city_name in clean_input or city_name.replace("臺", "台") in address:
                city_code = code
                break

        # 若使用者沒寫縣市，自動根據常見行政區推斷縣市
        if not city_code:
            dist_str = feat['district']
            if any(k in dist_str for k in ['內湖', '大安', '信義', '中山', '松山', '中正', '萬華', '文山', '南港', '士林', '北投', '大同']):
                city_code = 'a'
            elif any(k in dist_str for k in ['板橋', '新莊', '中和', '永和', '新店', '三重', '蘆洲', '土城', '汐止', '林口', '淡水', '樹林', '三峽', '鶯歌']):
                city_code = 'f'
            elif any(k in dist_str for k in ['桃園', '中壢', '平鎮', '八德', '楊梅', '蘆竹', '龜山', '龍潭', '大園']):
                city_code = 'h'
            elif any(k in dist_str for k in ['西屯', '北屯', '南屯', '西區', '北區', '南區', '東區', '豐原', '大里', '太平', '沙鹿', '烏日']):
                city_code = 'b'
            elif any(k in dist_str for k in ['永康', '安平', '安南', '東區', '北區', '中西區', '善化', '新市', '仁德']):
                city_code = 'd'
            elif any(k in dist_str for k in ['左營', '鼓山', '三民', '楠梓', '鳳山', '苓雅', '前鎮', '小港', '仁武', '岡山']):
                city_code = 'e'
            elif '竹北' in dist_str or '竹東' in dist_str:
                city_code = 'j'
            else:
                city_code = 'a'

        road = feat['road']
        lane = feat['lane']
        alley = feat['alley']
        num = feat['num']
        target_floor = feat['floor']
        district = feat['district']

        road_df = fetch_road_stream_data(city_code, road)
        if road_df.empty:
            return {"error": f"查無『{district}{road}』之建物與實價資料，請確認地址路名是否正確（若為新北/台中/高雄等縣市，請在開頭加上縣市名稱）。"}

        if district and not road_df[road_df['district'] == district].empty:
            road_df = road_df[road_df['district'] == district].copy()

        same_street_df = road_df[(road_df['p_lane'] == lane) & (road_df['p_alley'] == alley)].copy()
        if same_street_df.empty:
            same_street_df = road_df[road_df['p_lane'] == lane].copy()
        if same_street_df.empty:
            same_street_df = road_df.copy()

        # 1. 調出這間房子的建物規格檔案
        exact_building = same_street_df[same_street_df['p_num'] == num].copy()
        adj_building = same_street_df[
            (same_street_df['p_num'] % 2 == num % 2) &
            ((same_street_df['p_num'] - num).abs() <= 10)
        ].copy()

        if not exact_building.empty:
            b_pool = exact_building
            match_level = f"✅ 已精準調出本棟門牌【{road}{lane+'巷' if lane else ''}{num}號】建物規格檔案"
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
            match_level = f"ℹ️ 已由同路段緊鄰建物【{b_pool.iloc[0]['p_num']}號】推估規格（可點選下方房型坪數切換）"

        ref_b = b_pool.iloc[0]
        target_build_ym = str(ref_b['build_ym'])
        target_age = int(ref_b['age']) if ref_b['age'] >= 0 else 15
        mode_fl = b_pool['total_floors_num'].mode()
        target_total_floors = int(mode_fl.iloc[0]) if not mode_fl.empty else int(ref_b['total_floors_num'])
        b_type = str(ref_b['b_type'])
        usage = str(ref_b['usage'])
        mgmt = "有管委會" if "有" in str(ref_b['mgmt']) else "無管委會"

        # 2. 用社區完工年月與連號門牌鎖定「同社區」
        def is_same_community(row):
            if row['p_lane'] != lane:
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
                sub_p = comm_pings[(comm_pings >= p - 1.5) & (comm_pings <= p + 1.5)]
                cluster_val = round(float(sub_p.median()), 1) if not sub_p.empty else float(p)
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

        # 3. 抓出同社區一年內成交行情
        latest_db_month = int(road_df['month_val'].max())
        one_year_cutoff = latest_db_month - 12

        min_ping = round(target_ping * (1 - range_ratio), 1)
        max_ping = round(target_ping * (1 + range_ratio), 1)

        comm_1yr_df = comm_all_history[comm_all_history['month_val'] >= one_year_cutoff].copy()
        if target_floor != 1 and not comm_1yr_df[comm_1yr_df['floor_num'] != 1].empty:
            comm_1yr_df = comm_1yr_df[comm_1yr_df['floor_num'] != 1]
        comm_1yr_df = comm_1yr_df.sort_values('month_val', ascending=False)

        comm_note = f"本社區近一年內共成交 {len(comm_1yr_df)} 筆"
        if comm_1yr_df.empty and not comm_all_history.empty:
            comm_1yr_df = comm_all_history.sort_values('month_val', ascending=False).head(10).copy()
            comm_note = f"本社區近一年無交易，已調出本社區近期成交共 {len(comm_1yr_df)} 筆"

        nearby_1yr_df = road_df[
            (~road_df['is_same_comm']) &
            (road_df['month_val'] >= one_year_cutoff) &
            (road_df['house_ping'] >= min_ping) &
            (road_df['house_ping'] <= max_ping) &
            ((road_df['age'] - target_age).abs() <= 8) &
            (roa
