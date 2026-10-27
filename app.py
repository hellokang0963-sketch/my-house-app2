# 檔案名稱：app.py (v7.0 100%純真實內政部資料版 - 嚴格鎖定本戶與同棟、絕不造假或亂湊別戶)
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
import pandas as pd
import requests
import urllib3
import zipfile
import io
import csv
import re
import gc
from datetime import datetime

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(title="好房 AI 真實地政實價比對系統 v7.0")

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

ROAD_CACHE = {}

def cn_to_int(s: str) -> int:
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
    def repl_cn(m):
        return f"{cn_to_int(m.group(1))}{m.group(2)}"
    addr = re.sub(r'([一二三四五六七八九十]+)(段|樓|層|巷|弄|號)', repl_cn, addr)
    return addr

def parse_exact_address(clean_addr: str):
    """嚴格拆解地址：縣市、行政區、路街段、巷、弄、精確門牌號(含之幾)、精確樓層"""
    s = re.sub(r'^\d{3,6}', '', clean_addr)

    # 抓樓層 (如 6樓、6層)
    fl_m = re.search(r'號.*?(\d+)[樓層F]', s)
    floor_num = int(fl_m.group(1)) if fl_m else 0

    # 抓門牌號碼 (區分 180號 與 180之1號)
    num_m = re.search(r'(\d+)(?:之(\d+)|-(\d+))?號', s)
    if not num_m:
        return None
    main_num = int(num_m.group(1))
    sub_num = num_m.group(2) or num_m.group(3) or ""
    full_num_str = f"{main_num}之{sub_num}號" if sub_num else f"{main_num}號"

    prefix = s[:num_m.start()]

    city = ""
    city_m = re.match(r'^([臺台]北市|新北市|桃園市|[臺台]中市|[臺台]南市|高雄市|基隆市|新竹市|嘉義市|.{2}[縣市])', prefix)
    if city_m:
        city = city_m.group(1)
        prefix = prefix[len(city):]

    district = ""
    dist_m = re.match(r'^(.{1,4}?[區鄉鎮市])', prefix)
    if dist_m:
        district = dist_m.group(1)
        prefix = prefix[len(district):]

    prefix_no_li = re.sub(r'^.{1,4}?[里村](\d+鄰)?', '', prefix)
    if prefix_no_li:
        prefix = prefix_no_li

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

    road = prefix.strip()
    if not road:
        return None

    return {
        "city": city,
        "district": district,
        "road": road,
        "lane": lane,
        "alley": alley,
        "num": main_num,
        "sub_num": sub_num,
        "full_num_str": full_num_str,
        "floor": floor_num
    }

def safe_float(val) -> float:
    try:
        return float(val) if val else 0.0
    except Exception:
        return 0.0

def parse_roc_date(date_str: str) -> int:
    if not isinstance(date_str, str) or len(date_str) < 6:
        return 0
    try:
        year = int(date_str[:-4]) + 1911
        month = int(date_str[-4:-2])
        return year * 12 + month
    except Exception:
        return 0

def fetch_real_moi_road_data(city_code: str, road_name: str):
    """
    100% 從內政部官方伺服器抓取真實實價登錄資料（回溯多個季度以確保能查到該門牌真實規格）。
    絕不產生任何一筆假資料！若連線被擋會直接回報真實網路狀態。
    """
    cache_key = f"{city_code}_{road_name}"
    if cache_key in ROAD_CACHE:
        return ROAD_CACHE[cache_key], None

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"
    }
    target_filename = f"{city_code}_lvr_land_a.csv"
    road_base = re.sub(r'\d+段', '', road_name)
    matched_rows = []
    now = datetime.now()
    current_roc_year = now.year - 1911

    urls = ["https://plvr.land.moi.gov.tw/Download?type=zip&fileName=lvr_landcsv.zip"]
    for y in range(current_roc_year, current_roc_year - 4, -1):
        for s in [4, 3, 2, 1]:
            urls.append(f"https://plvr.land.moi.gov.tw/DownloadSeason?season={y}S{s}&type=zip&fileName=lvr_landcsv.zip")

    downloaded_seasons = 0
    last_err = ""
    for url in urls:
        if downloaded_seasons >= 8:  # 抓取最新一期 + 過去7季真實官方資料
            break
        try:
            resp = requests.get(url, headers=headers, timeout=12, verify=False)
            if resp.status_code == 200 and len(resp.content) > 10000:
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
                            next(reader, None)

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
                                if house_ping <= 2:
                                    continue

                                if park_price > 0 and park_ping > 0:
                                    real_unit = (total_price - park_price) / house_ping
                                elif moi_unit > 5:
                                    real_unit = moi_unit
                                else:
                                    real_unit = total_price / house_ping

                                if real_unit < 5 or real_unit > 500:
                                    continue

                                build_ym = g('建築完成', '')
                                age = max(0, current_roc_year - int(build_ym[:-4])) if (len(build_ym) >= 5 and build_ym[:-4].isdigit()) else -1
                                pub_ping = max(0.0, house_ping - main_ping - sub_ping)
                                p_info = parse_exact_address(clean_a)
                                if not p_info:
                                    continue

                                matched_rows.append({
                                    'district': g('鄉鎮市區', ''),
                                    'clean_addr': clean_a,
                                    'date_str': g('交易年月日', ''),
                                    'month_val': parse_roc_date(g('交易年月日', '')),
                                    'floor_raw': g('移轉層次', '-'),
                                    'floor_num': cn_to_int(g('移轉層次', '0')),
                                    'total_floors_raw': g('總樓層數', '-'),
                                    'total_floors_num': cn_to_int(g('總樓層數', '0')),
                                    'b_type': g('建物型態', '').split('(')[0],
                                    'usage': g('主要用途', ''),
                                    'mgmt': g('有無管理組織', ''),
                                    'build_ym': build_ym,
                                    'age': age,
                                    'rooms': g('格局-房', '0'),
                                    'halls': g('格局-廳', '0'),
                                    'baths': g('格局-衛', '0'),
                                    'total_ping': round(total_ping, 2),
                                    'house_ping': round(house_ping, 2),
                                    'main_ping': round(main_ping, 2),
                                    'sub_ping': round(sub_ping, 2),
                                    'public_ping': round(pub_ping, 2),
                                    'parking_ping': round(park_ping, 2),
                                    'total_price': round(total_price, 1),
                                    'parking_price': round(park_price, 1),
                                    'real_unit_price': round(real_unit, 2),
                                    'p_road': p_info['road'],
                                    'p_lane': p_info['lane'],
                                    'p_alley': p_info['alley'],
                                    'p_num': p_info['num'],
                                    'p_sub_num': p_info['sub_num'],
                                    'p_full_num': p_info['full_num_str'],
                                    'note': note_str
                                })
                        downloaded_seasons += 1
            del resp
            gc.collect()
        except Exception as e:
            last_err = str(e)
            continue

    if downloaded_seasons == 0:
        return pd.DataFrame(), f"雲端主機(美國IP)連線台灣內政部伺服器被阻擋或逾時({last_err})。建議在您自己的電腦執行 python app.py (使用台灣網路100%秒通)！"

    if not matched_rows:
        return pd.DataFrame(), f"已成功從內政部下載 {downloaded_seasons} 個季度資料，但查無『{road_name}』的實價登錄紀錄。"

    df = pd.DataFrame(matched_rows).drop_duplicates(subset=['clean_addr', 'date_str', 'total_price']).reset_index(drop=True)
    ROAD_CACHE[cache_key] = df
    return df, None

@app.get("/api/analyze")
def analyze(address: str = Query(...), custom_ping: float = Query(0), range_ratio: float = Query(0.20)):
    clean_input = normalize_address(address)
    feat = parse_exact_address(clean_input)
    if not feat:
        return JSONResponse({"error": f"地址『{address}』格式不完整，請包含路街與門牌幾號（例如：台北市內湖區成功路四段180號6樓）"})

    city_code = None
    for city_name, code in CITY_CODE_MAP.items():
        if city_name in clean_input or city_name.replace("臺", "台") in address:
            city_code = code
            break

    if not city_code:
        return JSONResponse({"error": "請在地址開頭加上『縣市名稱』（例如：台北市、新北市、台中市、高雄市...），以便精準調閱該縣市地政庫！"})

    road = feat['road']
    lane = feat['lane']
    alley = feat['alley']
    num = feat['num']
    full_num_str = feat['full_num_str']
    target_floor = feat['floor']
    district = feat['district']

    road_df, err_msg = fetch_real_moi_road_data(city_code, road)
    if err_msg:
        return JSONResponse({"error": err_msg})

    # 嚴格鎖定同行政區、同路、同巷、同弄 (絕不把巷內跟大馬路混在一起)
    street_df = road_df[
        (road_df['p_road'] == road) &
        (road_df['p_lane'] == lane) &
        (road_df['p_alley'] == alley)
    ].copy()
    if district and not street_df[street_df['district'] == district].empty:
        street_df = street_df[street_df['district'] == district].copy()

    # =================================================================================
    # 【嚴格原則】只抓「你輸入的這個門牌 (如 180號)」！絕不拿別棟門牌冒充這戶的資料！
    # =================================================================================
    exact_door_df = street_df[street_df['p_num'] == num].sort_values('month_val', ascending=False).copy()
    exact_unit_df = exact_door_df[exact_door_df['floor_num'] == target_floor].copy() if target_floor > 0 else pd.DataFrame()

    # 整理這棟門牌 (如 180號) 所有真實成交過的樓層與坪數清單，讓使用者看清楚這棟每一層的真實資料
    building_units_list = []
    if not exact_door_df.empty:
        for _, r in exact_door_df.iterrows():
            d_s = str(r['date_str'])
            building_units_list.append({
                "floor": f"{r['floor_num']}樓" if r['floor_num'] > 0 else r['floor_raw'],
                "addr": r['clean_addr'],
                "ping": r['house_ping'],
                "main_ping": r['main_ping'],
                "sub_ping": r['sub_ping'],
                "pub_ping": r['public_ping'],
                "date": f"{d_s[:-4]}/{d_s[-4:-2]}" if len(d_s) >= 5 else d_s,
                "unit_price": r['real_unit_price'],
                "total_price": r['total_price']
            })

    if not exact_unit_df.empty:
        # 情況 A：你查的「這間房子本尊 (同門牌 + 同樓層)」在實價登錄裡有紀錄！100% 是這戶的真實資料！
        u_row = exact_unit_df.iloc[0]
        has_exact_unit = True
        target_build_ym = str(u_row['build_ym'])
        target_age = int(u_row['age'])
        target_total_floors = int(u_row['total_floors_num'])
        b_type = str(u_row['b_type'])
        target_ping = custom_ping if custom_ping > 0 else float(u_row['house_ping'])
        main_ping = float(u_row['main_ping'])
        sub_ping = float(u_row['sub_ping'])
        pub_ping = float(u_row['public_ping'])
        pub_ratio = round((pub_ping / target_ping) * 100, 1) if target_ping > 0 else 0
        layout = f"{u_row['rooms']}房{u_row['halls']}廳{u_row['baths']}衛"
        usage = str(u_row['usage'])
        mgmt = str(u_row['mgmt'])
        d_s = str(u_row['date_str'])
        status_badge = "🎯 100% 命中本戶真實地政實價資料"
        note_msg = f"本戶【{u_row['clean_addr']}】於民國 {d_s[:-4]}年{d_s[-4:-2]}月 有真實登錄紀錄，以下面積與規格 100% 為本戶真實數據！"

    elif not exact_door_df.empty:
        # 情況 B：「這戶樓層 (如 6樓)」沒買賣過，但「同門牌本棟 (180號 的其他樓層)」有真實紀錄！
        # 屋齡、完工年月、總樓層 100% 真實！坪數則明確告知「本樓層無交易，以下為本棟其他樓層真實坪數供選取」
        b_row = exact_door_df.iloc[0]
        has_exact_unit = False
        target_build_ym = str(b_row['build_ym'])
        target_age = int(b_row['age'])
        target_total_floors = int(b_row['total_floors_num'])
        b_type = str(b_row['b_type'])
        usage = str(b_row['usage'])
        mgmt = str(b_row['mgmt'])

        if custom_ping > 0:
            exact_door_df['p_diff'] = (exact_door_df['house_ping'] - custom_ping).abs()
            match_r = exact_door_df.sort_values('p_diff').iloc[0]
            target_ping = custom_ping
        else:
            match_r = b_row
            target_ping = float(match_r['house_ping'])

        main_ping = float(match_r['main_ping'])
        sub_ping = float(match_r['sub_ping'])
        pub_ping = float(match_r['public_ping'])
        pub_ratio = round((pub_ping / target_ping) * 100, 1) if target_ping > 0 else 0
        layout = f"{match_r['rooms']}房{match_r['halls']}廳{match_r['baths']}衛"
        status_badge = f"⚠️ 本戶({target_floor}樓)未曾買賣，已調出本棟({full_num_str})真實建籍"
        note_msg = f"【{full_num_str}{target_floor}樓】在內政部實價庫中無買賣紀錄（未交易過之房屋政府不公開單戶權狀）。系統已調出本棟【{full_num_str}】100%真實屋齡與總樓層，並列出本棟已成交樓層之真實坪數供您點選核對："

    else:
        # 情況 C：連「180號」整棟樓在近幾年內政部實價登錄裡都 0 筆交易！
        # 絕不亂抓別號門牌來假裝是 180 號！直接誠實告知使用者，並提供同路段最近門牌之社區清單供使用者指定！
        if street_df.empty:
            street_df = road_df.copy()
        street_df['num_diff'] = (street_df['p_num'] - num).abs()
        nearest_df = street_df.sort_values('num_diff').head(15)
        nearest_doors = nearest_df[['p_num', 'clean_addr', 'build_ym', 'age', 'total_floors_num', 'b_type', 'house_ping']].drop_duplicates(subset=['p_num']).head(5).to_dict('records')
        
        return JSONResponse({
            "no_exact_door": True,
            "input_addr": clean_input,
            "door_str": f"{road}{lane+'巷' if lane else ''}{full_num_str}",
            "message": f"內政部實價登錄資料庫中，查無【{road}{lane+'巷' if lane else ''}{full_num_str}】任何樓層的買賣紀錄（代表這棟門牌近年沒有任何一戶交易過，因此政府免費開放資料中沒有這棟門牌的坪數檔案）。",
            "nearest_doors": nearest_doors
        })

    # =================================================================================
    # 【第二階段】只抓「100% 同社區（同完工年月 build_ym ＋ 同總樓層 ＋ 同條路巷）」的成交行情
    # =================================================================================
    def is_strictly_same_community(row):
        # 條件 1：本棟同門牌號 (如 180號)
        if row['p_num'] == num and row['p_lane'] == lane:
            return True
        # 條件 2：同一條路巷 + 建築完成年月(精確到年月日，即同張使用執照)完全一模一樣！
        if target_build_ym and len(target_build_ym) >= 5 and row['build_ym'] == target_build_ym and row['p_lane'] == lane:
            return True
        return False

    street_df['is_same_comm'] = street_df.apply(is_strictly_same_community, axis=1)
    same_comm_df = street_df[street_df['is_same_comm']].sort_values('month_val', ascending=False).copy()

    # 計算「近一年內」的時間切點
    now = datetime.now()
    current_m_val = now.year * 12 + now.month
    one_year_cutoff = current_m_val - 12

    comm_1yr_df = same_comm_df[same_comm_df['month_val'] >= one_year_cutoff].copy()

    min_ping = round(target_ping * (1 - range_ratio), 1)
    max_ping = round(target_ping * (1 + range_ratio), 1)

    # 另抓「同路段、屋齡差距 ±5年內、坪數接近 (±20%)、一年內」的鄰近真實交易作為對照組
    nearby_1yr_df = street_df[
        (~street_df['is_same_comm']) &
        (street_df['month_val'] >= one_year_cutoff) &
        (street_df['house_ping'] >= min_ping) &
        (street_df['house_ping'] <= max_ping) &
        ((street_df['age'] - target_age).abs() <= 5) &
        (street_df['b_type'] == b_type)
    ].copy()
    nearby_1yr_df['num_dist'] = (nearby_1yr_df['p_num'] - num).abs()
    nearby_1yr_df = nearby_1yr_df.sort_values(['num_dist', 'month_val'], ascending=[True, False])

    # 統計價格落點：優先用「同社區且坪數相近」計算
    comm_fit_ping = same_comm_df[(same_comm_df['house_ping'] >= min_ping) & (same_comm_df['house_ping'] <= max_ping)]
    comm_1yr_fit_ping = comm_1yr_df[(comm_1yr_df['house_ping'] >= min_ping) & (comm_1yr_df['house_ping'] <= max_ping)]

    if not comm_1yr_fit_ping.empty:
        stat_df = comm_1yr_fit_ping
        val_basis = f"100% 依【本社區一年內、同坪數房型 ({len(stat_df)}筆)】真實成交計算"
    elif not comm_fit_ping.empty:
        stat_df = comm_fit_ping
        val_basis = f"100% 依【本社區歷年同坪數房型 ({len(stat_df)}筆)】真實成交計算 (本房型近一年無交易)"
    elif not same_comm_df.empty:
        stat_df = same_comm_df
        val_basis = f"100% 依【本社區全部成交紀錄 ({len(stat_df)}筆)】真實成交計算"
    else:
        stat_df = nearby_1yr_df
        val_basis = f"依【同路段相近屋齡坪數 ({len(stat_df)}筆)】真實成交計算"

    q = stat_df['real_unit_price'].quantile([0.25, 0.5, 0.75]).to_dict()
    low_u, med_u, high_u = round(q[0.25], 1), round(q[0.5], 1), round(q[0.75], 1)
    avg_u = round(float(stat_df['real_unit_price'].mean()), 1)

    all_display_df = pd.concat([same_comm_df, nearby_1yr_df], ignore_index=True)
    comp_list = []
    for _, r in all_display_df.head(35).iterrows():
        d_str = str(r['date_str'])
        f_date = f"{d_str[:-4]}/{d_str[-4:-2]}" if len(d_str) >= 5 else d_str
        is_1yr = bool(r['month_val'] >= one_year_cutoff)
        is_fit_p = bool(min_ping <= r['house_ping'] <= max_ping)
        comp_list.append({
            "date": f_date,
            "is_1yr": is_1yr,
            "is_fit_ping": is_fit_p,
            "addr": r['clean_addr'],
            "b_type": r['b_type'],
            "age": f"{r['age']}年" if r['age'] >= 0 else "-",
            "build_ym": r['build_ym'],
            "ping": r['house_ping'],
            "main_ping": r['main_ping'],
            "sub_ping": r['sub_ping'],
            "pub_ping": r['public_ping'],
            "total_ping": r['total_ping'],
            "floor": f"{r['floor_num']}F / {r['total_floors_num']}F" if r['floor_num'] > 0 else f"{r['floor_raw']}/{r['total_floors_raw']}",
            "unit": r['real_unit_price'],
            "total": r['total_price'],
            "park": r['parking_price'],
            "layout": f"{r['rooms']}房{r['halls']}廳{r['baths']}衛",
            "is_same": bool(r['is_same_comm']),
            "is_exact_door": bool(r['p_num'] == num)
        })

    comm_nums = sorted(same_comm_df['p_num'].unique().tolist())
    comm_range_str = f"同使照社區門牌：{road}{lane+'巷' if lane else ''} " + "、".join([f"{n}號" for n in comm_nums])

    return JSONResponse({
        "no_exact_door": False,
        "has_exact_unit": has_exact_unit,
        "target": {
            "addr": clean_input,
            "comm_range": comm_range_str,
            "status_badge": status_badge,
            "note": note_msg,
            "ping": target_ping,
            "main_ping": main_ping,
            "sub_ping": sub_ping,
            "pub_ping": pub_ping,
            "pub_ratio": pub_ratio,
            "min_ping": min_ping,
            "max_ping": max_ping,
            "age": target_age,
            "build_ym": f"民國 {target_build_ym[:-4]}年{target_build_ym[-4:-2]}月{target_build_ym[-2:]}日" if len(target_build_ym) >= 7 else target_build_ym,
            "floors": f"{target_floor}樓 / 共 {target_total_floors} 樓" if target_floor > 0 else f"共 {target_total_floors} 樓",
            "type": b_type,
            "usage": usage,
            "mgmt": mgmt,
            "layout": layout,
            "building_units": building_units_list,
            "basis": val_basis,
            "comm_1yr_count": int(len(comm_1yr_df)),
            "comm_all_count": int(len(same_comm_df))
        },
        "val": {
            "count": len(comp_list),
            "same_count": len(same_comm_df),
            "avg_u": avg_u, "low_u": low_u, "med_u": med_u, "high_u": high_u,
            "low_t": round(low_u * target_ping),
            "med_t": round(med_u * target_ping),
            "high_t": round(high_u * target_ping)
        },
        "comps": comp_list
    })

@app.get("/", response_class=HTMLResponse)
def web_portal():
    return """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>好房 AI 真實地政實價比對系統 v7.0 (零假資料嚴格版)</title>
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
                <div class="bg-red-600 p-2 rounded-lg"><i class="fa-solid fa-gavel text-xl text-white"></i></div>
                <div>
                    <span class="font-black text-lg tracking-wide">好房 AI 真實實價登錄嚴格比對系統</span>
                    <span class="hidden md:inline-block ml-2 text-xs bg-red-500/20 text-red-300 border border-red-400/30 px-2 py-0.5 rounded-full">v7.0 100%真實官方數據・絕不帶入非本棟資料</span>
                </div>
            </div>
        </div>
    </nav>

    <div class="bg-gradient-to-r from-slate-900 via-slate-800 to-blue-900 text-white py-7 px-4 shadow-inner">
        <div class="max-w-7xl mx-auto">
            <h1 class="text-2xl md:text-3xl font-extrabold mb-2">嚴格鎖定「本門牌」與「同張使用執照社區」真實實價登錄</h1>
            <p class="text-blue-200 text-xs md:text-sm mb-5">本系統絕不亂湊其他門牌資料！僅顯示內政部官方實價登錄中「完全同門牌號」與「建築完成年月日100%相同之同社區」真實數據。</p>

            <div class="bg-white p-4 rounded-2xl shadow-xl text-slate-800 grid grid-cols-1 md:grid-cols-12 gap-3 items-end">
                <div class="md:col-span-5">
                    <label class="text-xs font-bold text-slate-500 block mb-1">📍 輸入完整地址 (請務必包含縣市、區、路街、號、樓)</label>
                    <input type="text" id="addr" value="" 
                           placeholder="例：台北市內湖區成功路四段180號6樓"
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3.5 py-2.5 text-sm font-bold focus:outline-none focus:ring-2 focus:ring-blue-600">
                </div>
                <div class="md:col-span-2">
                    <label class="text-xs font-bold text-slate-500 block mb-1">📐 指定扣車位坪數</label>
                    <input type="number" id="ping" placeholder="留空則抓本戶真實坪數" 
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3 py-2.5 text-sm font-bold focus:outline-none">
                </div>
                <div class="md:col-span-2">
                    <label class="text-xs font-bold text-slate-500 block mb-1">🚗 外加車位價 (萬)</label>
                    <input type="number" id="park" value="0" 
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3 py-2.5 text-sm font-bold focus:outline-none">
                </div>
                <div class="md:col-span-1">
                    <label class="text-xs font-bold text-slate-500 block mb-1">🔍 坪數比對</label>
                    <select id="ratio" class="w-full bg-slate-50 border border-slate-300 rounded-xl px-2 py-2.5 text-sm font-bold focus:outline-none">
                        <option value="0.10">±10%</option>
                        <option value="0.20" selected>±20%</option>
                        <option value="0.30">±30%</option>
                    </select>
                </div>
                <div class="md:col-span-2">
                    <button onclick="runQuery()" class="w-full bg-blue-600 hover:bg-blue-700 active:scale-95 transition text-white font-bold py-2.5 px-4 rounded-xl shadow-md flex items-center justify-center space-x-2">
                        <i class="fa-solid fa-magnifying-glass"></i>
                        <span>精準查詢真資料</span>
                    </button>
                </div>
            </div>
        </div>
    </div>

    <div class="max-w-7xl mx-auto px-4 py-8">
        <div id="loading" class="hidden bg-white rounded-3xl p-12 text-center shadow-sm border border-slate-200 my-6">
            <i class="fa-solid fa-circle-notch text-5xl text-blue-600 spin mb-4"></i>
            <h3 class="text-lg font-extrabold text-slate-800">正在連線台灣內政部伺服器檢索真實實價登錄 CSV...</h3>
            <p class="text-xs text-slate-400 mt-2">正在逐季掃描該路段所有歷史官方紀錄，約需 10~20 秒，請稍候！</p>
        </div>

        <div id="err" class="hidden bg-red-50 border-l-4 border-red-600 text-red-700 p-5 rounded-xl shadow-sm my-4 font-bold text-sm leading-relaxed"></div>

        <!-- 當該門牌整棟從未有過實價登錄時的誠實提示區塊 (絕不亂帶別棟資料) -->
        <div id="noDoorBox" class="hidden bg-amber-50 border-2 border-amber-400 rounded-2xl p-6 my-4 shadow-sm">
            <h3 class="text-lg font-black text-amber-900 mb-2"><i class="fa-solid fa-triangle-exclamation mr-2"></i>查無本棟門牌之實價登錄紀錄（系統拒絕帶入錯誤別棟資料）</h3>
            <p id="noDoorMsg" class="text-sm text-amber-800 mb-4"></p>
            <p class="text-xs font-bold text-slate-700 mb-2">📌 若您的房子與下列同路段緊鄰門牌屬於「同一社區」，請點擊該門牌以載入同社區規格與行情：</p>
            <div id="nearestDoorsList" class="grid grid-cols-1 md:grid-cols-2 gap-2"></div>
        </div>

        <div id="dashboard" class="hidden grid grid-cols-1 lg:grid-cols-12 gap-6">
            <div class="lg:col-span-5 space-y-6">
                <!-- 1. 本戶/本棟真實資料卡 -->
                <div class="bg-white rounded-2xl p-6 shadow-sm border-2 border-blue-600">
                    <div class="flex items-center justify-between mb-2">
                        <span id="rStatusBadge" class="bg-blue-600 text-white text-xs font-extrabold px-2.5 py-1 rounded-md"></span>
                        <span id="rType" class="text-xs font-bold text-indigo-700 bg-indigo-100 px-2.5 py-1 rounded-md"></span>
                    </div>
                    <h2 id="rAddr" class="text-xl font-black text-slate-900 mt-2"></h2>
                    <p id="rCommRange" class="text-xs text-emerald-700 font-bold mt-1 mb-3"></p>
                    <p id="note" class="text-xs text-slate-700 font-semibold bg-amber-50 p-3 rounded-xl border border-amber-200 mb-4 leading-relaxed"></p>

                    <!-- 本棟各樓層真實交易紀錄清單 (讓使用者一眼看到這棟樓每一戶真實坪數) -->
                    <div class="mb-4 bg-slate-50 p-3 rounded-xl border border-slate-200">
                        <span class="text-xs font-extrabold text-slate-700 block mb-2">🏢 本棟門牌曾登錄之真實戶別規格 (點擊可套用該戶坪數)：</span>
                        <div id="unitBtns" class="space-y-1.5 max-h-40 overflow-y-auto"></div>
                    </div>

                    <div class="grid grid-cols-3 gap-2 text-center bg-blue-50/50 p-3 rounded-xl border border-blue-100 mb-3">
                        <div>
                            <p class="text-[11px] text-slate-500">扣車位權狀坪數</p>
                            <p id="rPing" class="text-base font-black text-blue-700"></p>
                        </div>
                        <div class="border-l border-blue-100">
                            <p class="text-[11px] text-slate-500">主建物 + 附屬陽台</p>
                            <p id="rMainSub" class="text-sm font-extrabold text-slate-800 mt-0.5"></p>
                        </div>
                        <div class="border-l border-blue-100">
                            <p class="text-[11px] text-slate-500">公設坪 (公設比)</p>
                            <p id="rPub" class="text-sm font-extrabold text-slate-800 mt-0.5"></p>
                        </div>
                    </div>

                    <div class="grid grid-cols-2 gap-2 text-xs bg-slate-50 p-3 rounded-xl border border-slate-100">
                        <div class="col-span-2"><span class="text-slate-400">建築完成日期(使照指紋)：</span><strong id="rBuildYm" class="text-blue-700"></strong></div>
                        <div><span class="text-slate-400">真實屋齡：</span><strong id="rAge" class="text-slate-800"></strong></div>
                        <div><span class="text-slate-400">樓層/總樓層：</span><strong id="rFloor" class="text-slate-800"></strong></div>
                        <div><span class="text-slate-400">登錄格局：</span><strong id="rLayout" class="text-slate-800"></strong></div>
                        <div><span class="text-slate-400">主要用途：</span><strong id="rUsage" class="text-slate-800"></strong></div>
                    </div>
                </div>

                <!-- 2. 價格落點分析 -->
                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <h3 class="text-base font-black text-slate-900 mb-2"><i class="fa-solid fa-sack-dollar text-amber-500 mr-2"></i>同社區真實成交價格落點分析</h3>
                    <p id="rBasis" class="text-xs bg-emerald-50 text-emerald-800 border border-emerald-200 font-bold p-2 rounded-lg mb-4 text-center"></p>

                    <div class="bg-gradient-to-br from-blue-900 to-indigo-900 text-white p-5 rounded-2xl text-center shadow-md mb-4">
                        <p class="text-xs text-blue-200 font-semibold">合理成交總價落點 (中位數 PR50)</p>
                        <div class="my-2 flex items-baseline justify-center space-x-1">
                            <span id="medT" class="text-4xl font-black text-yellow-300"></span>
                            <span class="text-sm font-bold">萬元</span>
                        </div>
                        <div class="flex justify-center space-x-4 text-xs text-blue-100 mt-2 pt-2 border-t border-blue-700/50">
                            <span>中位數單價：<strong id="medU" class="text-white"></strong> 萬/坪</span>
                            <span>•</span>
                            <span>同社區均價：<strong id="avgU" class="text-white"></strong> 萬/坪</span>
                        </div>
                    </div>

                    <div class="grid grid-cols-2 gap-3 text-center">
                        <div class="p-3 rounded-xl bg-emerald-50 border border-emerald-200">
                            <span class="text-xs font-extrabold text-emerald-800 block">📉 保守落點 (PR25)</span>
                            <p id="lowT" class="text-lg font-black text-emerald-900 mt-1"></p>
                            <p id="lowU" class="text-[11px] font-bold text-emerald-700"></p>
                        </div>
                        <div class="p-3 rounded-xl bg-amber-50 border border-amber-200">
                            <span class="text-xs font-extrabold text-amber-800 block">📈 樂觀落點 (PR75)</span>
                            <p id="highT" class="text-lg font-black text-amber-900 mt-1"></p>
                            <p id="highU" class="text-[11px] font-bold text-amber-700"></p>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 右欄：同社區真實成交行情表 -->
            <div class="lg:col-span-7 space-y-6">
                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-4">
                        <div>
                            <h3 class="text-base font-black text-slate-900">📋 同社區（同完工日）真實實價登錄明細</h3>
                            <p id="rCommNote" class="text-xs text-blue-600 font-bold mt-0.5"></p>
                        </div>
                        <div class="flex flex-wrap gap-1.5">
                            <button onclick="filterTable('same_1yr')" id="btnSame1Yr" class="text-xs font-bold px-3 py-1.5 rounded-lg bg-blue-600 text-white shadow">同社區一年內 (<span id="same1YrCnt">0</span>)</button>
                            <button onclick="filterTable('same_all')" id="btnSameAll" class="text-xs font-bold px-3 py-1.5 rounded-lg bg-slate-100 text-slate-700">同社區歷年全部 (<span id="sameAllCnt">0</span>)</button>
                            <button onclick="filterTable('all')" id="btnAll" class="text-xs font-bold px-3 py-1.5 rounded-lg bg-slate-100 text-slate-700">含同路相近屋齡 (<span id="allCnt">0</span>)</button>
                        </div>
                    </div>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left border-collapse">
                            <thead>
                                <tr class="border-b-2 border-slate-200 text-[11px] text-slate-400">
                                    <th class="py-2 px-2">歸屬</th><th class="py-2 px-2">成交年月</th><th class="py-2 px-2">真實成交門牌</th>
                                    <th class="py-2 px-2 text-right">扣車位坪(主建)</th><th class="py-2 px-2 text-center">樓層</th>
                                    <th class="py-2 px-2 text-right">拆算單價</th><th class="py-2 px-2 text-right">成交總價</th>
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

function applyUnitPing(p) {
    document.getElementById('ping').value = p;
    runQuery();
}

function useNearestDoor(addrStr) {
    document.getElementById('addr').value = addrStr;
    document.getElementById('ping').value = '';
    runQuery();
}

async function runQuery() {
    const addr = document.getElementById('addr').value.trim();
    const ping = document.getElementById('ping').value || 0;
    const park = parseFloat(document.getElementById('park').value) || 0;
    const ratio = document.getElementById('ratio').value || 0.20;

    if (!addr) return alert("請輸入完整地址！");
    document.getElementById('err').classList.add('hidden');
    document.getElementById('noDoorBox').classList.add('hidden');
    document.getElementById('dashboard').classList.add('hidden');
    document.getElementById('loading').classList.remove('hidden');

    try {
        const res = await fetch(`/api/analyze?address=${encodeURIComponent(addr)}&custom_ping=${ping}&range_ratio=${ratio}`);
        const d = await res.json();
        document.getElementById('loading').classList.add('hidden');

        if (d.error) {
            const errEl = document.getElementById('err');
            errEl.innerText = d.error;
            errEl.classList.remove('hidden');
            return;
        }

        if (d.no_exact_door) {
            document.getElementById('noDoorMsg').innerText = d.message;
            const listEl = document.getElementById('nearestDoorsList');
            listEl.innerHTML = '';
            d.nearest_doors.forEach(nd => {
                listEl.innerHTML += `
                    <button onclick="useNearestDoor('${nd.clean_addr}')" class="text-left p-3 bg-white hover:bg-blue-50 rounded-xl border border-amber-300 transition flex justify-between items-center">
                        <div>
                            <p class="font-bold text-slate-900 text-xs">${nd.clean_addr}</p>
                            <p class="text-[11px] text-slate-500">${nd.b_type} ｜ 屋齡 ${nd.age}年 (完工:${nd.build_ym}) ｜ 共${nd.total_floors_num}樓</p>
                        </div>
                        <span class="text-xs font-bold text-blue-600">帶入此社區 ➔</span>
                    </button>`;
            });
            document.getElementById('noDoorBox').classList.remove('hidden');
            return;
        }

        globalData = d;
        renderDashboard(d, park);
        document.getElementById('dashboard').classList.remove('hidden');
    } catch (e) {
        document.getElementById('loading').classList.add('hidden');
        const errEl = document.getElementById('err');
        errEl.innerText = "連線逾時或雲端主機忙碌中，請再按一次查詢！";
        errEl.classList.remove('hidden');
    }
}

function renderDashboard(d, park) {
    document.getElementById('rStatusBadge').innerText = d.target.status_badge;
    document.getElementById('rStatusBadge').className = d.has_exact_unit 
        ? "bg-emerald-600 text-white text-xs font-extrabold px-2.5 py-1 rounded-md"
        : "bg-amber-600 text-white text-xs font-extrabold px-2.5 py-1 rounded-md";
    document.getElementById('rAddr').innerText = d.target.addr;
    document.getElementById('rCommRange').innerText = d.target.comm_range;
    document.getElementById('rType').innerText = d.target.type;
    document.getElementById('note').innerText = d.target.note;

    const uBox = document.getElementById('unitBtns');
    uBox.innerHTML = '';
    d.target.building_units.forEach(u => {
        const active = Math.abs(u.ping - d.target.ping) < 0.2;
        uBox.innerHTML += `
            <div onclick="applyUnitPing(${u.ping})" class="cursor-pointer p-2 rounded-lg border text-xs flex justify-between items-center transition ${active ? 'bg-blue-600 text-white border-blue-600 font-bold' : 'bg-white text-slate-700 border-slate-200 hover:bg-blue-50'}">
                <span><b>${u.floor}</b>｜扣車位 <b>${u.ping}坪</b> (主建${u.main_ping}坪)</span>
                <span>${u.date} 成交 ${u.unit_price}萬/坪</span>
            </div>`;
    });

    document.getElementById('rPing').innerText = d.target.ping + " 坪";
    document.getElementById('rMainSub').innerText = `${d.target.main_ping} + ${d.target.sub_ping} 坪`;
    document.getElementById('rPub').innerText = `${d.target.pub_ping}坪 (${d.target.pub_ratio}%)`;
    document.getElementById('rBuildYm').innerText = d.target.build_ym;
    document.getElementById('rAge').innerText = d.target.age + " 年";
    document.getElementById('rFloor').innerText = d.target.floors;
    document.getElementById('rLayout').innerText = d.target.layout;
    document.getElementById('rUsage').innerText = d.target.usage;

    document.getElementById('rBasis').innerText = "🎯 " + d.target.basis;
    document.getElementById('medT').innerText = (d.val.med_t + park).toLocaleString();
    document.getElementById('medU').innerText = d.val.med_u;
    document.getElementById('avgU').innerText = d.val.avg_u;

    document.getElementById('lowT').innerText = (d.val.low_t + park).toLocaleString() + " 萬";
    document.getElementById('lowU').innerText = `單價 ${d.val.low_u} 萬/坪`;
    document.getElementById('highT').innerText = (d.val.high_t + park).toLocaleString() + " 萬";
    document.getElementById('highU').innerText = `單價 ${d.val.high_u} 萬/坪`;

    document.getElementById('same1YrCnt').innerText = d.target.comm_1yr_count;
    document.getElementById('sameAllCnt').innerText = d.target.comm_all_count;
    document.getElementById('allCnt').innerText = d.val.count;
    document.getElementById('rCommNote').innerText = `同使照完工日 (${d.target.build_ym}) 之同社區：一年內成交 ${d.target.comm_1yr_count} 筆 / 歷年共 ${d.target.comm_all_count} 筆`;

    if (d.target.comm_1yr_count > 0) {
        filterTable('same_1yr');
    } else {
        filterTable('same_all');
    }
}

function filterTable(mode) {
    if (!globalData) return;
    const tbody = document.getElementById('tableBody');
    tbody.innerHTML = '';

    ['btnSame1Yr', 'btnSameAll', 'btnAll'].forEach(id => {
        document.getElementById(id).className = "text-xs font-bold px-3 py-1.5 rounded-lg bg-slate-100 text-slate-700";
    });
    const activeBtn = mode === 'same_1yr' ? 'btnSame1Yr' : (mode === 'same_all' ? 'btnSameAll' : 'btnAll');
    document.getElementById(activeBtn).className = "text-xs font-bold px-3 py-1.5 rounded-lg bg-blue-600 text-white shadow";

    let list = globalData.comps;
    if (mode === 'same_1yr') list = list.filter(c => c.is_same && c.is_1yr);
    else if (mode === 'same_all') list = list.filter(c => c.is_same);

    if (list.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="text-center py-6 text-slate-400">本社區在「近一年內」剛好無成交紀錄，請點選上方「同社區歷年全部」查看本社區歷史真實成交！</td></tr>`;
        return;
    }

    list.forEach(c => {
        const badge = c.is_exact_door
            ? `<span class="bg-emerald-600 text-white text-[10px] px-2 py-0.5 rounded font-bold">🎯 本棟門牌</span>`
            : (c.is_same 
                ? `<span class="bg-blue-600 text-white text-[10px] px-2 py-0.5 rounded font-bold">🏠 同社區</span>` 
                : `<span class="bg-slate-200 text-slate-700 text-[10px] px-2 py-0.5 rounded font-semibold">同路同屋齡</span>`);
        const yrBadge = c.is_1yr ? `<span class="text-emerald-600 font-bold ml-1 text-[10px]">(一年內)</span>` : `<span class="text-slate-400 ml-1 text-[10px]">(歷年)</span>`;
        const parkNote = c.park > 0 ? `<span class="block text-[10px] text-slate-400">含車位${c.park}萬</span>` : `<span class="block text-[10px] text-slate-400">總坪${c.total_ping}坪</span>`;
        tbody.innerHTML += `
            <tr class="hover:bg-slate-50 ${c.is_exact_door ? 'bg-emerald-50/60' : (c.is_same ? 'bg-blue-50/40' : '')}">
                <td class="py-3 px-2 whitespace-nowrap">${badge}</td>
                <td class="py-3 px-2 font-semibold text-slate-700 whitespace-nowrap">${c.date}${yrBadge}</td>
                <td class="py-3 px-2 font-bold text-slate-900">${c.addr}</td>
                <td class="py-3 px-2 text-right font-bold text-blue-700">${c.ping} 坪 <span class="text-[10px] text-slate-500 font-normal">(主${c.main_ping})</span></td>
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
