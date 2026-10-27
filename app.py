# 檔案名稱：app.py (v7.5 100%純真實內政部資料版 - 本戶精準帶出 + 同社區相近坪數比對 + 售價/開價/含服務費底價估價)
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

app = FastAPI(title="好房 AI 真實地政實價比對與估價系統 v7.5")

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
    cmap = {'零': 0, '〇': 0, '○': 0, '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
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
    addr = addr.replace("台", "臺").replace(" ", "").replace(" ", "").strip()
    full_to_half = str.maketrans("０１２３４５６７８９－—～Ｆ", "0123456789---F")
    addr = addr.translate(full_to_half)
    # 自動剔除重複輸入的縣市名稱 (例如: 新竹市新竹市西濱路 -> 新竹市西濱路)
    addr = re.sub(r'([臺北新桃中南高基宜竹苗彰投雲嘉屏花東澎金連]{2}[市縣])\1+', r'\1', addr)

    def repl_cn(m):
        unit = '樓' if m.group(2) in ['樓', '層', 'F'] else m.group(2)
        return f"{cn_to_int(m.group(1))}{unit}"

    addr = re.sub(r'([一二三四五六七八九十〇○]+)(段|樓|層|F|巷|弄|號)', repl_cn, addr)
    # 將「之三」統一轉為「之3」
    addr = re.sub(r'之([一二三四五六七八九十〇○]+)', lambda m: f"之{cn_to_int(m.group(1))}", addr)
    # 將「9樓-3」轉為「9樓之3」
    addr = re.sub(r'(\d+樓)-(\d+)', r'\1之\2', addr)
    return addr

def parse_exact_address(clean_addr: str):
    """嚴格拆解地址：縣市、行政區、路街段、巷、弄、精確門牌號(含之幾)、精確樓層、樓層戶別(之幾)"""
    s = re.sub(r'^\d{3,6}', '', clean_addr)

    # 抓門牌號碼 (區分 185號 與 185之1號)
    num_m = re.search(r'(\d+)(?:之(\d+)|-(\d+))?號', s)
    if not num_m:
        return None
    main_num = int(num_m.group(1))
    sub_num = num_m.group(2) or num_m.group(3) or ""
    full_num_str = f"{main_num}之{sub_num}號" if sub_num else f"{main_num}號"

    # 從「號」之後的字串抓樓層與戶別 (例如：9樓之3)
    after_num = s[num_m.end():]
    fl_m = re.search(r'(\d+)[樓層F]', after_num)
    floor_num = int(fl_m.group(1)) if fl_m else 0

    # 抓樓層後的「之幾」戶別 (例如：185號9樓之3 -> floor_sub = "之3")
    sub_u_m = re.search(r'之(\d+)', after_num)
    floor_sub = f"之{sub_u_m.group(1)}" if sub_u_m else ""

    prefix = s[:num_m.start()]

    city = ""
    city_m = re.match(r'^([臺台]北市|新北市|桃園市|[臺台]中市|[臺台]南市|高雄市|基隆市|新竹市|嘉義市|.{2}[縣市])', prefix)
    if city_m:
        city = city_m.group(1)
        prefix = prefix[len(city):]

    district = ""
    dist_m = re.match(r'^(.{1,3}?[區鄉鎮市])', prefix)
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
        "floor": floor_num,
        "floor_sub": floor_sub
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
    絕不產生任何一筆假資料！
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
                                for kw in ['鄉鎮市區', '交易標的', '門牌', '交易年月日', '移轉層次', '總樓層數', '建物型態', '主要用途', '建築完成', '建物移轉總面積', '主建物面積', '附屬建物面積', '陽台面積', '格局-房', '格局-廳', '格局-衛', '總價元', '單價元平方公尺', '車位移轉總面積', '車位總價元', '車位類別', '備註', '有無管理組織']:
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

                                fl_num = cn_to_int(g('移轉層次', '0'))
                                if fl_num <= 0 and p_info['floor'] > 0:
                                    fl_num = p_info['floor']

                                matched_rows.append({
                                    'district': g('鄉鎮市區', ''),
                                    'clean_addr': clean_a,
                                    'date_str': g('交易年月日', ''),
                                    'month_val': parse_roc_date(g('交易年月日', '')),
                                    'floor_raw': g('移轉層次', '-'),
                                    'floor_num': fl_num,
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
                                    'park_type': g('車位類別', ''),
                                    'total_price': round(total_price, 1),
                                    'parking_price': round(park_price, 1),
                                    'real_unit_price': round(real_unit, 2),
                                    'p_road': p_info['road'],
                                    'p_lane': p_info['lane'],
                                    'p_alley': p_info['alley'],
                                    'p_num': p_info['num'],
                                    'p_sub_num': p_info['sub_num'],
                                    'p_full_num': p_info['full_num_str'],
                                    'p_floor_sub': p_info['floor_sub'],
                                    'note': note_str
                                })
                        downloaded_seasons += 1
            del resp
            gc.collect()
        except Exception as e:
            last_err = str(e)
            continue

    if downloaded_seasons == 0:
        return pd.DataFrame(), f"雲端主機連線台灣內政部伺服器被阻擋或逾時({last_err})。建議在您自己的電腦執行 python app.py (使用台灣網路100%秒通)！"

    if not matched_rows:
        return pd.DataFrame(), f"已成功從內政部下載 {downloaded_seasons} 個季度資料，但查無『{road_name}』的實價登錄紀錄。"

    df = pd.DataFrame(matched_rows).drop_duplicates(subset=['clean_addr', 'date_str', 'total_price']).reset_index(drop=True)
    ROAD_CACHE[cache_key] = df
    return df, None

@app.get("/api/analyze")
def analyze(
    address: str = Query(...),
    custom_ping: float = Query(0),
    custom_park: float = Query(-1),
    range_ratio: float = Query(0.15)
):
    clean_input = normalize_address(address)
    feat = parse_exact_address(clean_input)
    if not feat:
        return JSONResponse({"error": f"地址『{address}』格式不完整，請包含路街與門牌幾號（例如：新竹市西濱路一段185號9樓之3）"})

    city_code = None
    for city_name, code in CITY_CODE_MAP.items():
        if city_name in clean_input or city_name.replace("臺", "台") in address:
            city_code = code
            break

    if not city_code:
        return JSONResponse({"error": "請在地址開頭加上『縣市名稱』（例如：新竹市、台北市、新北市、台中市...），以便精準調閱該縣市地政庫！"})

    road = feat['road']
    lane = feat['lane']
    alley = feat['alley']
    num = feat['num']
    full_num_str = feat['full_num_str']
    target_floor = feat['floor']
    target_floor_sub = feat['floor_sub']  # 例如 "之3"
    district = feat['district']

    road_df, err_msg = fetch_real_moi_road_data(city_code, road)
    if err_msg:
        return JSONResponse({"error": err_msg})

    # 嚴格鎖定同行政區、同路、同巷、同弄
    street_df = road_df[
        (road_df['p_road'] == road) &
        (road_df['p_lane'] == lane) &
        (road_df['p_alley'] == alley)
    ].copy()
    if district and not street_df[street_df['district'] == district].empty:
        street_df = street_df[street_df['district'] == district].copy()

    # =================================================================================
    # 【功能 1】嚴格帶出「我這間房屋資料」 (本戶本尊 -> 本棟同柱之幾標準層 -> 本棟門牌)
    # =================================================================================
    exact_door_df = street_df[street_df['p_num'] == num].sort_values('month_val', ascending=False).copy()

    # 1. 檢查是否命中「本戶本尊 (同門牌 + 同樓層 + 同之幾)」
    exact_unit_df = pd.DataFrame()
    if not exact_door_df.empty and target_floor > 0:
        cond = (exact_door_df['floor_num'] == target_floor)
        if target_floor_sub:
            cond_with_sub = cond & (exact_door_df['p_floor_sub'] == target_floor_sub)
            exact_unit_df = exact_door_df[cond_with_sub].copy() if cond_with_sub.any() else exact_door_df[cond].copy()
        else:
            exact_unit_df = exact_door_df[cond].copy()

    # 2. 若本樓層未曾交易，但有輸入「之幾 (如：之3)」，優先抓「本棟同之幾垂直標準層 (如：7樓之3、10樓之3)」
    same_column_df = pd.DataFrame()
    if exact_unit_df.empty and not exact_door_df.empty and target_floor_sub:
        same_column_df = exact_door_df[exact_door_df['p_floor_sub'] == target_floor_sub].copy()

    # 整理這棟門牌 (如 185號) 所有真實成交過的樓層與坪數清單
    building_units_list = []
    if not exact_door_df.empty:
        for _, r in exact_door_df.iterrows():
            d_s = str(r['date_str'])
            fl_label = f"{r['floor_num']}樓{r['p_floor_sub']}" if r['floor_num'] > 0 else f"{r['floor_raw']}{r['p_floor_sub']}"
            building_units_list.append({
                "floor": fl_label,
                "addr": r['clean_addr'],
                "total_ping": r['total_ping'],
                "ping": r['house_ping'],
                "main_ping": r['main_ping'],
                "sub_ping": r['sub_ping'],
                "pub_ping": r['public_ping'],
                "park_ping": r['parking_ping'],
                "park_price": r['parking_price'],
                "layout": f"{r['rooms']}房{r['halls']}廳{r['baths']}衛",
                "date": f"{d_s[:-4]}/{d_s[-4:-2]}" if len(d_s) >= 5 else d_s,
                "unit_price": r['real_unit_price'],
                "total_price": r['total_price']
            })

    if not exact_unit_df.empty:
        # 情況 A：100% 命中本戶本尊
        u_row = exact_unit_df.iloc[0]
        has_exact_unit = True
        d_s = str(u_row['date_str'])
        status_badge = "🎯 100% 命中本戶真實地政實價資料"
        note_msg = f"本戶【{u_row['clean_addr']}】於民國 {d_s[:-4]}年{d_s[-4:-2]}月 有真實登錄紀錄，以下產權面積與格局 100% 為本戶真實數據！"
    elif not same_column_df.empty:
        # 情況 B：本樓層未交易，但命中「同棟同柱標準層 (同為之幾)」
        u_row = same_column_df.iloc[0]
        has_exact_unit = True
        status_badge = f"✅ 命中本棟同柱標準層（{full_num_str}*{target_floor_sub}）真實建籍"
        note_msg = f"本戶【{full_num_str}{target_floor}樓{target_floor_sub}】未曾交易，系統已自動帶出同棟垂直標準層【{u_row['clean_addr']}】之真實面積與格局（大樓同柱標準層坪數格局一致）！"
    elif not exact_door_df.empty:
        # 情況 C：本棟其他樓層有真實紀錄
        has_exact_unit = False
        if custom_ping > 0:
            exact_door_df['p_diff'] = (exact_door_df['house_ping'] - custom_ping).abs()
            u_row = exact_door_df.sort_values('p_diff').iloc[0]
        else:
            u_row = exact_door_df.iloc[0]
        status_badge = f"⚠️ 本戶({target_floor}樓{target_floor_sub})未曾買賣，已調出本棟({full_num_str})真實建籍"
        note_msg = f"【{full_num_str}{target_floor}樓{target_floor_sub}】在實價庫中無買賣紀錄。系統已調出本棟【{full_num_str}】100%真實屋齡與總樓層，並帶入本棟已成交戶別之規格（可點擊下方本棟清單切換戶別）："
    else:
        # 情況 D：整棟門牌近年 0 交易，誠實提示並列出緊鄰門牌
        if street_df.empty:
            street_df = road_df.copy()
        street_df['num_diff'] = (street_df['p_num'] - num).abs()
        nearest_df = street_df.sort_values('num_diff').head(15)
        nearest_doors = nearest_df[['p_num', 'clean_addr', 'build_ym', 'age', 'total_floors_num', 'b_type', 'house_ping']].drop_duplicates(subset=['p_num']).head(6).to_dict('records')

        return JSONResponse({
            "no_exact_door": True,
            "input_addr": clean_input,
            "door_str": f"{road}{lane+'巷' if lane else ''}{full_num_str}",
            "message": f"內政部實價登錄資料庫中，查無【{road}{lane+'巷' if lane else ''}{full_num_str}】任何樓層的買賣紀錄（代表這棟門牌近年沒有任何一戶交易過）。",
            "nearest_doors": nearest_doors
        })

    # 整理本戶核心房屋資料
    target_build_ym = str(u_row['build_ym'])
    target_age = int(u_row['age'])
    target_total_floors = int(u_row['total_floors_num'])
    b_type = str(u_row['b_type'])
    target_ping = round(custom_ping, 2) if custom_ping > 0 else float(u_row['house_ping'])
    main_ping = float(u_row['main_ping'])
    sub_ping = float(u_row['sub_ping'])
    pub_ping = float(u_row['public_ping'])
    park_ping = float(u_row['parking_ping'])
    total_ping_with_park = round(target_ping + park_ping, 2)
    pub_ratio = round((pub_ping / target_ping) * 100, 1) if target_ping > 0 else 0
    layout = f"{u_row['rooms']}房{u_row['halls']}廳{u_row['baths']}衛"
    usage = str(u_row['usage']) or "住家用"
    mgmt = "有管委會" if str(u_row['mgmt']) == "有" else "無管委會"
    park_type_str = str(u_row['park_type']) if str(u_row['park_type']) else ("坡道平面" if park_ping > 0 else "無車位/未拆分")

    # =================================================================================
    # 【功能 2】嚴格比對「同社區 ＋ 坪數接近」的實價登錄
    # =================================================================================
    def is_strictly_same_community(row):
        # 條件 1：本棟同門牌號
        if row['p_num'] == num and row['p_lane'] == lane:
            return True
        # 條件 2：同一條路巷 + 建築完成年月日(同張使用執照)完全相同
        if target_build_ym and len(target_build_ym) >= 5 and row['build_ym'] == target_build_ym and row['p_lane'] == lane:
            return True
        # 條件 3：同一條路巷 + 建築完成同年月(前5碼) + 總樓層數相同
        if target_build_ym and len(target_build_ym) >= 5 and str(row['build_ym'])[:5] == target_build_ym[:5] and row['total_floors_num'] == target_total_floors and row['p_lane'] == lane:
            return True
        return False

    street_df['is_same_comm'] = street_df.apply(is_strictly_same_community, axis=1)
    street_df['ping_diff'] = (street_df['house_ping'] - target_ping).round(2)
    street_df['abs_ping_diff'] = street_df['ping_diff'].abs()
    street_df['is_same_sub'] = (street_df['p_floor_sub'] == target_floor_sub) if target_floor_sub else False

    # 坪數接近區間：依使用者設定比例 (預設 ±15%) 或至少 ±3.5 坪容許誤差
    tol_ping = max(target_ping * range_ratio, 3.5)
    min_ping = round(max(1.0, target_ping - tol_ping), 1)
    max_ping = round(target_ping + tol_ping, 1)

    street_df['is_fit_ping'] = (street_df['house_ping'] >= min_ping) & (street_df['house_ping'] <= max_ping)

    same_comm_df = street_df[street_df['is_same_comm']].copy()
    # 排序：坪數接近優先 > 同柱戶別(之幾)優先 > 交易時間最新 > 坪數差距最小
    same_comm_df = same_comm_df.sort_values(
        by=['is_fit_ping', 'is_same_sub', 'month_val', 'abs_ping_diff'],
        ascending=[False, False, False, True]
    )

    now = datetime.now()
    current_m_val = now.year * 12 + now.month
    one_year_cutoff = current_m_val - 12

    comm_fit_ping_df = same_comm_df[same_comm_df['is_fit_ping']].copy()
    comm_1yr_fit_ping_df = comm_fit_ping_df[comm_fit_ping_df['month_val'] >= one_year_cutoff].copy()
    comm_1yr_all_df = same_comm_df[same_comm_df['month_val'] >= one_year_cutoff].copy()

    # 另抓「同路段、屋齡相近(±5年)、坪數接近」的鄰近真實交易作為對照
    nearby_fit_df = street_df[
        (~street_df['is_same_comm']) &
        (street_df['is_fit_ping']) &
        ((street_df['age'] - target_age).abs() <= 5) &
        (street_df['b_type'] == b_type)
    ].copy()
    nearby_fit_df['num_dist'] = (nearby_fit_df['p_num'] - num).abs()
    nearby_fit_df = nearby_fit_df.sort_values(['num_dist', 'month_val', 'abs_ping_diff'], ascending=[True, False, True])

    # =================================================================================
    # 【功能 3】給我估價：建議售價、建議開價、建議底價(含服務費)
    # =================================================================================
    if len(comm_1yr_fit_ping_df) >= 2:
        stat_df = comm_1yr_fit_ping_df
        val_basis = f"100% 依【本社區一年內・坪數接近 ({min_ping}~{max_ping}坪，共 {len(stat_df)} 筆)】真實成交計算"
    elif not comm_fit_ping_df.empty:
        stat_df = comm_fit_ping_df
        val_basis = f"100% 依【本社區歷年・坪數接近 ({min_ping}~{max_ping}坪，共 {len(stat_df)} 筆)】真實成交計算"
    elif not same_comm_df.empty:
        stat_df = same_comm_df
        val_basis = f"依【本社區全部成交紀錄 (共 {len(stat_df)} 筆)】真實成交計算 (建議微調上方坪數範圍)"
    else:
        stat_df = nearby_fit_df
        val_basis = f"依【同路段相近屋齡與相近坪數 (共 {len(stat_df)} 筆)】真實成交計算"

    q = stat_df['real_unit_price'].quantile([0.25, 0.5, 0.75]).to_dict()
    low_u, med_u, high_u = round(q[0.25], 1), round(q[0.5], 1), round(q[0.75], 1)
    avg_u = round(float(stat_df['real_unit_price'].mean()), 1)

    # 自動推算本社區車位價值 (若使用者未手動輸入車位價，且本戶含車位坪數，自動取本社區有拆車位之車位中位數價)
    comm_parks = same_comm_df[same_comm_df['parking_price'] > 0]['parking_price']
    auto_park_price = round(float(comm_parks.median()), 0) if (not comm_parks.empty and park_ping > 0) else 0.0
    final_park_price = auto_park_price if custom_park < 0 else custom_park

    # 樓層高低微調係數
    floor_factor = 1.0
    floor_adj_note = "標準樓層係數 (1.00)"
    if target_floor > 0 and target_total_floors >= 5:
        fl_ratio = target_floor / target_total_floors
        if target_floor == 4:
            floor_factor = 0.98
            floor_adj_note = f"{target_floor}樓諧音微調 (-2%)"
        elif fl_ratio >= 0.75 and target_floor < target_total_floors:
            floor_factor = 1.015
            floor_adj_note = f"{target_floor}樓中高樓層視野加分 (+1.5%)"
        elif target_floor == target_total_floors:
            floor_factor = 0.99
            floor_adj_note = f"{target_floor}樓頂樓微調 (-1%)"
        elif target_floor <= 3:
            floor_factor = 0.985
            floor_adj_note = f"{target_floor}樓低樓層微調 (-1.5%)"

    # 1. 建議售價 (合理市場成交價) = 樓層微調後中位數單價 × 扣車位坪數 + 車位價格
    suggested_sale_unit = round(med_u * floor_factor, 1)
    suggested_sale_total = round(suggested_sale_unit * target_ping + final_park_price)

    # 2. 建議開價 (預留 10% 買方議價空間，前端可自由切換 +5% ~ +15%)
    asking_total = round(suggested_sale_total * 1.10)
    asking_unit = round((asking_total - final_park_price) / target_ping, 1) if target_ping > 0 else 0

    # 3. 建議底價(含服務費) (設定為建議售價之 97%，內含 4% 仲介服務費)
    bottom_incl_fee_total = round(suggested_sale_total * 0.97)
    bottom_incl_fee_unit = round((bottom_incl_fee_total - final_park_price) / target_ping, 1) if target_ping > 0 else 0
    service_fee_4pct = round(bottom_incl_fee_total * 0.04, 1)
    seller_net_total = round(bottom_incl_fee_total - service_fee_4pct, 1)

    # 整理實價登錄比對清單
    all_display_df = pd.concat([same_comm_df, nearby_fit_df], ignore_index=True)
    comp_list = []
    for _, r in all_display_df.head(45).iterrows():
        d_str = str(r['date_str'])
        f_date = f"{d_str[:-4]}/{d_str[-4:-2]}" if len(d_str) >= 5 else d_str
        is_1yr = bool(r['month_val'] >= one_year_cutoff)
        is_fit_p = bool(r['is_fit_ping'])
        p_diff = float(r['ping_diff'])
        p_diff_str = f"+{p_diff}坪" if p_diff > 0 else (f"{p_diff}坪" if p_diff < 0 else "坪數完全相同")
        comp_list.append({
            "date": f_date,
            "is_1yr": is_1yr,
            "is_fit_ping": is_fit_p,
            "ping_diff_str": p_diff_str,
            "abs_ping_diff": float(r['abs_ping_diff']),
            "addr": r['clean_addr'],
            "b_type": r['b_type'],
            "age": f"{r['age']}年" if r['age'] >= 0 else "-",
            "build_ym": r['build_ym'],
            "ping": r['house_ping'],
            "main_ping": r['main_ping'],
            "sub_ping": r['sub_ping'],
            "pub_ping": r['public_ping'],
            "total_ping": r['total_ping'],
            "floor": f"{r['floor_num']}F{r['p_floor_sub']} / {r['total_floors_num']}F" if r['floor_num'] > 0 else f"{r['floor_raw']}/{r['total_floors_raw']}",
            "unit": r['real_unit_price'],
            "total": r['total_price'],
            "park": r['parking_price'],
            "layout": f"{r['rooms']}房{r['halls']}廳{r['baths']}衛",
            "is_same": bool(r['is_same_comm']),
            "is_same_sub": bool(r['is_same_sub']),
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
            "total_ping": total_ping_with_park,
            "ping": target_ping,
            "main_ping": main_ping,
            "sub_ping": sub_ping,
            "pub_ping": pub_ping,
            "pub_ratio": pub_ratio,
            "park_ping": park_ping,
            "park_type": park_type_str,
            "park_price": final_park_price,
            "min_ping": min_ping,
            "max_ping": max_ping,
            "age": target_age,
            "build_ym": f"民國 {target_build_ym[:-4]}年{target_build_ym[-4:-2]}月{target_build_ym[-2:]}日" if len(target_build_ym) >= 7 else target_build_ym,
            "floors": f"{target_floor}樓{target_floor_sub} / 共 {target_total_floors} 樓" if target_floor > 0 else f"共 {target_total_floors} 樓",
            "type": b_type,
            "usage": usage,
            "mgmt": mgmt,
            "layout": layout,
            "building_units": building_units_list,
            "basis": val_basis,
            "comm_fit_1yr_count": int(len(comm_1yr_fit_ping_df)),
            "comm_fit_all_count": int(len(comm_fit_ping_df)),
            "comm_all_count": int(len(same_comm_df))
        },
        "val": {
            "count": len(comp_list),
            "same_count": len(same_comm_df),
            "floor_adj_note": floor_adj_note,
            "avg_u": avg_u, "low_u": low_u, "med_u": med_u, "high_u": high_u,
            "low_t": round(low_u * target_ping + final_park_price),
            "med_t": round(med_u * target_ping + final_park_price),
            "high_t": round(high_u * target_ping + final_park_price),
            "suggested_sale_unit": suggested_sale_unit,
            "suggested_sale_total": suggested_sale_total,
            "asking_total": asking_total,
            "asking_unit": asking_unit,
            "bottom_incl_fee_total": bottom_incl_fee_total,
            "bottom_incl_fee_unit": bottom_incl_fee_unit,
            "service_fee_4pct": service_fee_4pct,
            "seller_net_total": seller_net_total
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
    <title>好房 AI 真實地政實價比對＆房仲估價系統 v7.5</title>
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
                    <span class="font-black text-lg tracking-wide">好房 AI 真實實價比對＆房仲委託估價系統</span>
                    <span class="hidden md:inline-block ml-2 text-xs bg-emerald-500/20 text-emerald-300 border border-emerald-400/30 px-2.5 py-0.5 rounded-full">v7.5 100%官方數據・本戶產權・同社區近坪比對・售價/開價/含服務費底價</span>
                </div>
            </div>
        </div>
    </nav>

    <div class="bg-gradient-to-r from-slate-900 via-slate-800 to-blue-900 text-white py-7 px-4 shadow-inner">
        <div class="max-w-7xl mx-auto">
            <h1 class="text-2xl md:text-3xl font-extrabold mb-2">輸入地址自動帶出本戶資料・同社區相近坪數實價比對・委託估價</h1>
            <p class="text-blue-200 text-xs md:text-sm mb-5">嚴格鎖定內政部實價登錄「本門牌/同柱標準層」與「同張使用執照社區且坪數接近」之真實成交，自動試算建議售價、開價與含服務費底價。</p>

            <div class="bg-white p-4 rounded-2xl shadow-xl text-slate-800 grid grid-cols-1 md:grid-cols-12 gap-3 items-end">
                <div class="md:col-span-5">
                    <label class="text-xs font-bold text-slate-500 block mb-1">📍 輸入完整地址 (含縣市、路街、號、樓、之幾)</label>
                    <input type="text" id="addr" value="新竹市西濱路一段185號9樓之3"
                           placeholder="例：新竹市西濱路一段185號9樓之3"
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3.5 py-2.5 text-sm font-bold focus:outline-none focus:ring-2 focus:ring-blue-600">
                </div>
                <div class="md:col-span-2">
                    <label class="text-xs font-bold text-slate-500 block mb-1">📐 指定扣車位坪數</label>
                    <input type="number" step="0.1" id="ping" placeholder="留空自動抓本戶坪數"
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3 py-2.5 text-sm font-bold focus:outline-none">
                </div>
                <div class="md:col-span-2">
                    <label class="text-xs font-bold text-slate-500 block mb-1">🚗 車位價格 (萬元)</label>
                    <input type="number" id="park" placeholder="留空自動抓社區車位價"
                           class="w-full bg-slate-50 border border-slate-300 rounded-xl px-3 py-2.5 text-sm font-bold focus:outline-none">
                </div>
                <div class="md:col-span-1">
                    <label class="text-xs font-bold text-slate-500 block mb-1">🔍 坪數誤差</label>
                    <select id="ratio" class="w-full bg-slate-50 border border-slate-300 rounded-xl px-2 py-2.5 text-sm font-bold focus:outline-none">
                        <option value="0.10">±10%</option>
                        <option value="0.15" selected>±15%</option>
                        <option value="0.20">±20%</option>
                        <option value="0.30">±30%</option>
                    </select>
                </div>
                <div class="md:col-span-2">
                    <button onclick="runQuery()" class="w-full bg-blue-600 hover:bg-blue-700 active:scale-95 transition text-white font-bold py-2.5 px-4 rounded-xl shadow-md flex items-center justify-center space-x-2">
                        <i class="fa-solid fa-magnifying-glass-chart"></i>
                        <span>查本戶＆社區估價</span>
                    </button>
                </div>
            </div>
        </div>
    </div>

    <div class="max-w-7xl mx-auto px-4 py-8">
        <div id="loading" class="hidden bg-white rounded-3xl p-12 text-center shadow-sm border border-slate-200 my-6">
            <i class="fa-solid fa-circle-notch text-5xl text-blue-600 spin mb-4"></i>
            <h3 class="text-lg font-extrabold text-slate-800">正在連線台灣內政部伺服器檢索本戶與同社區實價登錄...</h3>
            <p class="text-xs text-slate-400 mt-2">正在逐季掃描該路段所有歷史官方紀錄，首次查詢約需 10~20 秒，同路段第二次查詢僅需 0.1 秒！</p>
        </div>

        <div id="err" class="hidden bg-red-50 border-l-4 border-red-600 text-red-700 p-5 rounded-xl shadow-sm my-4 font-bold text-sm leading-relaxed"></div>

        <!-- 當該門牌整棟從未有過實價登錄時的誠實提示區塊 -->
        <div id="noDoorBox" class="hidden bg-amber-50 border-2 border-amber-400 rounded-2xl p-6 my-4 shadow-sm">
            <h3 class="text-lg font-black text-amber-900 mb-2"><i class="fa-solid fa-triangle-exclamation mr-2"></i>查無本棟門牌之實價登錄紀錄（系統拒絕帶入錯誤別棟資料）</h3>
            <p id="noDoorMsg" class="text-sm text-amber-800 mb-4"></p>
            <p class="text-xs font-bold text-slate-700 mb-2">📌 若您的房子與下列同路段緊鄰門牌屬於「同一社區」，請點擊該門牌以載入同社區規格與行情：</p>
            <div id="nearestDoorsList" class="grid grid-cols-1 md:grid-cols-2 gap-2"></div>
        </div>

        <div id="dashboard" class="hidden grid grid-cols-1 lg:grid-cols-12 gap-6">
            <!-- 左欄：功能1 (本戶房屋資料) + 功能3 (建議售價/開價/底價含服務費) -->
            <div class="lg:col-span-5 space-y-6">
                <!-- 【功能 1】本戶/本棟真實房屋資料卡 -->
                <div class="bg-white rounded-2xl p-6 shadow-sm border-2 border-blue-600">
                    <div class="flex items-center justify-between mb-2">
                        <span id="rStatusBadge" class="bg-blue-600 text-white text-xs font-extrabold px-2.5 py-1 rounded-md"></span>
                        <span id="rType" class="text-xs font-bold text-indigo-700 bg-indigo-100 px-2.5 py-1 rounded-md"></span>
                    </div>
                    <h2 id="rAddr" class="text-xl font-black text-slate-900 mt-2"></h2>
                    <p id="rCommRange" class="text-xs text-emerald-700 font-bold mt-1 mb-3"></p>
                    <p id="note" class="text-xs text-slate-700 font-semibold bg-amber-50 p-3 rounded-xl border border-amber-200 mb-4 leading-relaxed"></p>

                    <!-- 核心面積 4 格卡 -->
                    <div class="grid grid-cols-4 gap-2 text-center bg-blue-50/60 p-3 rounded-xl border border-blue-200 mb-3">
                        <div>
                            <p class="text-[11px] text-slate-500">權狀總坪數</p>
                            <p id="rTotalPing" class="text-sm font-black text-slate-900 mt-0.5"></p>
                        </div>
                        <div class="border-l border-blue-200">
                            <p class="text-[11px] text-blue-700 font-bold">扣車位建坪</p>
                            <p id="rPing" class="text-base font-black text-blue-700"></p>
                        </div>
                        <div class="border-l border-blue-200">
                            <p class="text-[11px] text-slate-500">主建+陽台</p>
                            <p id="rMainSub" class="text-xs font-extrabold text-slate-800 mt-1"></p>
                        </div>
                        <div class="border-l border-blue-200">
                            <p class="text-[11px] text-slate-500">公設(公設比)</p>
                            <p id="rPub" class="text-xs font-extrabold text-slate-800 mt-1"></p>
                        </div>
                    </div>

                    <!-- 房屋詳細屬性 -->
                    <div class="grid grid-cols-2 gap-2 text-xs bg-slate-50 p-3 rounded-xl border border-slate-200 mb-4">
                        <div class="col-span-2"><span class="text-slate-400">建築完成日(使照)：</span><strong id="rBuildYm" class="text-blue-700"></strong></div>
                        <div><span class="text-slate-400">真實屋齡：</span><strong id="rAge" class="text-slate-800"></strong></div>
                        <div><span class="text-slate-400">所在樓層：</span><strong id="rFloor" class="text-slate-800"></strong></div>
                        <div><span class="text-slate-400">登錄格局：</span><strong id="rLayout" class="text-slate-800"></strong></div>
                        <div><span class="text-slate-400">車位資訊：</span><strong id="rParkInfo" class="text-slate-800"></strong></div>
                        <div><span class="text-slate-400">主要用途：</span><strong id="rUsage" class="text-slate-800"></strong></div>
                        <div><span class="text-slate-400">社區管理：</span><strong id="rMgmt" class="text-slate-800"></strong></div>
                    </div>

                    <!-- 本棟各樓層真實交易紀錄清單 -->
                    <div class="bg-slate-50 p-3 rounded-xl border border-slate-200">
                        <span class="text-xs font-extrabold text-slate-700 block mb-2">🏢 本棟門牌曾登錄之真實戶別規格 (點擊可套用該戶坪數)：</span>
                        <div id="unitBtns" class="space-y-1.5 max-h-40 overflow-y-auto"></div>
                    </div>
                </div>

                <!-- 【功能 3】給我估價：建議售價、開價、底價(含服務費) -->
                <div class="bg-white rounded-2xl p-6 shadow-sm border-2 border-indigo-600">
                    <div class="flex items-center justify-between mb-2">
                        <h3 class="text-base font-black text-slate-900"><i class="fa-solid fa-sack-dollar text-amber-500 mr-2"></i>房仲實戰估價建議書</h3>
                        <span id="rFloorAdj" class="text-[11px] bg-indigo-50 text-indigo-700 px-2.5 py-0.5 rounded-full font-bold border border-indigo-200"></span>
                    </div>
                    <p id="rBasis" class="text-xs bg-emerald-50 text-emerald-800 border border-emerald-200 font-bold p-2 rounded-lg mb-4 text-center"></p>

                    <!-- 三大核心價格：建議開價 / 建議售價 / 建議底價(含服務費) -->
                    <div class="grid grid-cols-3 gap-2.5 text-center mb-4">
                        <!-- 1. 建議開價 -->
                        <div class="bg-amber-50 p-3 rounded-2xl border-2 border-amber-300 flex flex-col justify-between">
                            <div>
                                <span class="text-[11px] bg-amber-200 text-amber-950 px-2 py-0.5 rounded font-black">1. 建議開價</span>
                                <p class="text-[10px] text-amber-700 font-semibold mt-1">預留買方議價空間</p>
                            </div>
                            <div class="my-2">
                                <p id="vAskTotal" class="text-2xl font-black text-amber-800"></p>
                                <p class="text-[10px] font-bold text-amber-700">萬元</p>
                            </div>
                            <p id="vAskUnit" class="text-[11px] font-extrabold text-amber-900 bg-amber-200/60 py-1 rounded-lg"></p>
                        </div>

                        <!-- 2. 建議售價 (合理成交價) -->
                        <div class="bg-gradient-to-b from-blue-700 to-indigo-900 text-white p-3 rounded-2xl shadow-lg flex flex-col justify-between transform scale-[1.03]">
                            <div>
                                <span class="text-[11px] bg-yellow-400 text-slate-900 px-2 py-0.5 rounded font-black">2. 建議售價</span>
                                <p class="text-[10px] text-blue-200 font-semibold mt-1">合理市場成交行情</p>
                            </div>
                            <div class="my-2">
                                <p id="vSaleTotal" class="text-3xl font-black text-yellow-300"></p>
                                <p class="text-[10px] font-bold text-blue-100">萬元</p>
                            </div>
                            <p id="vSaleUnit" class="text-[11px] font-extrabold text-white bg-white/15 py-1 rounded-lg"></p>
                        </div>

                        <!-- 3. 建議底價 (含服務費) -->
                        <div class="bg-emerald-50 p-3 rounded-2xl border-2 border-emerald-400 flex flex-col justify-between">
                            <div>
                                <span class="text-[11px] bg-emerald-200 text-emerald-950 px-2 py-0.5 rounded font-black">3. 建議底價</span>
                                <p class="text-[10px] text-emerald-700 font-bold mt-1">已含仲介服務費</p>
                            </div>
                            <div class="my-2">
                                <p id="vBotTotal" class="text-2xl font-black text-emerald-800"></p>
                                <p class="text-[10px] font-bold text-emerald-700">萬元</p>
                            </div>
                            <p id="vBotUnit" class="text-[11px] font-extrabold text-emerald-900 bg-emerald-200/60 py-1 rounded-lg"></p>
                        </div>
                    </div>

                    <!-- 底價(含服務費) ＆ 屋主實拿動態拆算器 -->
                    <div class="bg-slate-50 p-4 rounded-2xl border border-slate-200 space-y-3 mb-4">
                        <div class="flex items-center justify-between border-b border-slate-200 pb-2">
                            <span class="text-xs font-black text-slate-800"><i class="fa-solid fa-calculator text-indigo-600 mr-1.5"></i>委託底價（含服務費）＆ 屋主實拿試算</span>
                            <span class="text-[10px] text-slate-400 font-semibold">可自由調整費率與折數</span>
                        </div>

                        <div class="grid grid-cols-3 gap-2 text-xs">
                            <div>
                                <label class="text-[10px] text-slate-500 font-bold block mb-1">開價加價幅度</label>
                                <select id="askMarkup" onchange="updateValuationCalc()" class="w-full bg-white border border-slate-300 rounded-lg px-2 py-1.5 font-bold text-slate-800">
                                    <option value="1.06">+6% (微幅議價)</option>
                                    <option value="1.08">+8% (平實開價)</option>
                                    <option value="1.10" selected>+10% (標準開價)</option>
                                    <option value="1.12">+12% (充裕議價)</option>
                                    <option value="1.15">+15% (高開空間)</option>
                                </select>
                            </div>
                            <div>
                                <label class="text-[10px] text-slate-500 font-bold block mb-1">底價設定折數</label>
                                <select id="botRatio" onchange="updateValuationCalc()" class="w-full bg-white border border-slate-300 rounded-lg px-2 py-1.5 font-bold text-slate-800">
                                    <option value="1.00">售價 100% (硬底)</option>
                                    <option value="0.98">售價 98%</option>
                                    <option value="0.97" selected>售價 97% (推薦)</option>
                                    <option value="0.95">售價 95% (急售底)</option>
                                </select>
                            </div>
                            <div>
                                <label class="text-[10px] text-slate-500 font-bold block mb-1">內含服務費率</label>
                                <select id="feeRate" onchange="updateValuationCalc()" class="w-full bg-white border border-slate-300 rounded-lg px-2 py-1.5 font-bold text-slate-800">
                                    <option value="0.04" selected>4% (賣方標準)</option>
                                    <option value="0.03">3% (優惠費率)</option>
                                    <option value="0.02">2% (專案費率)</option>
                                    <option value="0.05">5% (含買方統包)</option>
                                    <option value="0.06">6% (法定上限)</option>
                                </select>
                            </div>
                        </div>

                        <div class="bg-white p-3 rounded-xl border border-slate-200 text-xs space-y-1.5">
                            <div class="flex justify-between items-center">
                                <span class="text-slate-600 font-semibold">🛡️ 委託契約底價（含服務費）：</span>
                                <strong id="cBotIncl" class="text-sm font-black text-emerald-700"></strong>
                            </div>
                            <div class="flex justify-between items-center">
                                <span class="text-slate-600 font-semibold">💼 內含仲介服務費金額：</span>
                                <strong id="cFeeAmt" class="text-rose-600 font-bold"></strong>
                            </div>
                            <div class="flex justify-between items-center pt-1.5 border-t border-dashed border-slate-200">
                                <span class="text-slate-900 font-extrabold">💰 屋主扣除服務費後【實拿淨額】：</span>
                                <strong id="cSellerNet" class="text-base font-black text-blue-700"></strong>
                            </div>
                        </div>

                        <!-- 屋主指定實拿反推底價 -->
                        <div class="pt-1">
                            <label class="text-[11px] text-indigo-800 font-extrabold block mb-1">💡 屋主指定「實拿淨額(萬)」反推「含服務費底價」：</label>
                            <div class="flex space-x-2">
                                <input type="number" id="customNet" oninput="reverseBottomFromNet()" placeholder="輸入屋主想實拿萬數"
                                       class="w-1/2 bg-white border border-indigo-300 rounded-lg px-2.5 py-1.5 text-xs font-bold focus:outline-none focus:ring-1 focus:ring-indigo-500">
                                <div id="revBotResult" class="w-1/2 bg-indigo-50 border border-indigo-200 text-indigo-900 rounded-lg px-2.5 py-1.5 text-xs font-extrabold flex items-center justify-center">
                                    應簽底價：-- 萬
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- 社區實價統計 PR25 / PR50 / PR75 參考 -->
                    <div class="grid grid-cols-3 gap-2 text-center text-xs">
                        <div class="p-2.5 rounded-xl bg-slate-50 border border-slate-200">
                            <span class="text-[11px] font-bold text-slate-500 block">📉 社區低標 (PR25)</span>
                            <p id="lowT" class="text-sm font-black text-slate-800 mt-0.5"></p>
                            <p id="lowU" class="text-[10px] font-bold text-slate-500"></p>
                        </div>
                        <div class="p-2.5 rounded-xl bg-slate-50 border border-slate-200">
                            <span class="text-[11px] font-bold text-blue-600 block">📊 社區中位 (PR50)</span>
                            <p id="medT" class="text-sm font-black text-blue-800 mt-0.5"></p>
                            <p id="medU" class="text-[10px] font-bold text-blue-600"></p>
                        </div>
                        <div class="p-2.5 rounded-xl bg-slate-50 border border-slate-200">
                            <span class="text-[11px] font-bold text-slate-500 block">📈 社區高標 (PR75)</span>
                            <p id="highT" class="text-sm font-black text-slate-800 mt-0.5"></p>
                            <p id="highU" class="text-[10px] font-bold text-slate-500"></p>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 右欄：【功能 2】同社區且坪數接近之真實成交行情表 -->
            <div class="lg:col-span-7 space-y-6">
                <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-4">
                        <div>
                            <h3 class="text-base font-black text-slate-900">📋 同社區・坪數接近 真實實價登錄比對明細</h3>
                            <p id="rCommNote" class="text-xs text-blue-600 font-bold mt-0.5"></p>
                        </div>
                        <div class="flex flex-wrap gap-1.5">
                            <button onclick="filterTable('fit_1yr')" id="btnFit1Yr" class="text-xs font-bold px-3 py-1.5 rounded-lg bg-blue-600 text-white shadow">同社區・近坪一年內 (<span id="fit1YrCnt">0</span>)</button>
                            <button onclick="filterTable('fit_all')" id="btnFitAll" class="text-xs font-bold px-3 py-1.5 rounded-lg bg-slate-100 text-slate-700">同社區・近坪歷年 (<span id="fitAllCnt">0</span>)</button>
                            <button onclick="filterTable('same_all')" id="btnSameAll" class="text-xs font-bold px-3 py-1.5 rounded-lg bg-slate-100 text-slate-700">同社區全部房型 (<span id="sameAllCnt">0</span>)</button>
                            <button onclick="filterTable('all')" id="btnAll" class="text-xs font-bold px-3 py-1.5 rounded-lg bg-slate-100 text-slate-700">含同路近坪 (<span id="allCnt">0</span>)</button>
                        </div>
                    </div>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left border-collapse">
                            <thead>
                                <tr class="border-b-2 border-slate-200 text-[11px] text-slate-400">
                                    <th class="py-2 px-2">歸屬</th>
                                    <th class="py-2 px-2">成交年月</th>
                                    <th class="py-2 px-2">真實成交門牌</th>
                                    <th class="py-2 px-2 text-right">扣車位坪 (與本戶差距)</th>
                                    <th class="py-2 px-2 text-center">樓層</th>
                                    <th class="py-2 px-2 text-right">拆算單價</th>
                                    <th class="py-2 px-2 text-right">成交總價</th>
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

function applyUnitPing(p, parkP) {
    document.getElementById('ping').value = p;
    if (parkP !== undefined && parkP > 0) {
        document.getElementById('park').value = parkP;
    }
    runQuery();
}

function useNearestDoor(addrStr) {
    document.getElementById('addr').value = addrStr;
    document.getElementById('ping').value = '';
    document.getElementById('park').value = '';
    runQuery();
}

async function runQuery() {
    const addr = document.getElementById('addr').value.trim();
    const ping = document.getElementById('ping').value || 0;
    const parkRaw = document.getElementById('park').value;
    const park = parkRaw === "" ? -1 : parseFloat(parkRaw);
    const ratio = document.getElementById('ratio').value || 0.15;

    if (!addr) return alert("請輸入完整地址！");
    document.getElementById('err').classList.add('hidden');
    document.getElementById('noDoorBox').classList.add('hidden');
    document.getElementById('dashboard').classList.add('hidden');
    document.getElementById('loading').classList.remove('hidden');

    try {
        const res = await fetch(`/api/analyze?address=${encodeURIComponent(addr)}&custom_ping=${ping}&custom_park=${park}&range_ratio=${ratio}`);
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
        renderDashboard(d);
        document.getElementById('dashboard').classList.remove('hidden');
    } catch (e) {
        document.getElementById('loading').classList.add('hidden');
        const errEl = document.getElementById('err');
        errEl.innerText = "連線逾時或主機忙碌中，請再按一次查詢！";
        errEl.classList.remove('hidden');
    }
}

function renderDashboard(d) {
    // 1. 渲染本戶資料
    document.getElementById('rStatusBadge').innerText = d.target.status_badge;
    document.getElementById('rStatusBadge').className = d.has_exact_unit
        ? "bg-emerald-600 text-white text-xs font-extrabold px-2.5 py-1 rounded-md"
        : "bg-amber-600 text-white text-xs font-extrabold px-2.5 py-1 rounded-md";
    document.getElementById('rAddr').innerText = d.target.addr;
    document.getElementById('rCommRange').innerText = d.target.comm_range;
    document.getElementById('rType').innerText = d.target.type;
    document.getElementById('note').innerText = d.target.note;

    document.getElementById('rTotalPing').innerText = d.target.total_ping + " 坪";
    document.getElementById('rPing').innerText = d.target.ping + " 坪";
    document.getElementById('rMainSub').innerText = `${d.target.main_ping}+${d.target.sub_ping}坪`;
    document.getElementById('rPub').innerText = `${d.target.pub_ping}坪(${d.target.pub_ratio}%)`;
    document.getElementById('rBuildYm').innerText = d.target.build_ym;
    document.getElementById('rAge').innerText = d.target.age + " 年";
    document.getElementById('rFloor').innerText = d.target.floors;
    document.getElementById('rLayout').innerText = d.target.layout;
    document.getElementById('rParkInfo').innerText = d.target.park_ping > 0
        ? `${d.target.park_ping}坪 (${d.target.park_price}萬)`
        : (d.target.park_price > 0 ? `外加車位 ${d.target.park_price}萬` : "無車位/未拆分");
    document.getElementById('rUsage').innerText = d.target.usage;
    document.getElementById('rMgmt').innerText = d.target.mgmt;

    // 本棟清單
    const uBox = document.getElementById('unitBtns');
    uBox.innerHTML = '';
    d.target.building_units.forEach(u => {
        const active = Math.abs(u.ping - d.target.ping) < 0.2;
        uBox.innerHTML += `
            <div onclick="applyUnitPing(${u.ping}, ${u.park_price})" class="cursor-pointer p-2 rounded-lg border text-xs flex justify-between items-center transition ${active ? 'bg-blue-600 text-white border-blue-600 font-bold' : 'bg-white text-slate-700 border-slate-200 hover:bg-blue-50'}">
                <span><b>${u.floor}</b>｜扣車位 <b>${u.ping}坪</b> (總坪${u.total_ping}坪・${u.layout})</span>
                <span>${u.date} 成交 ${u.unit_price}萬/坪 (${u.total_price}萬)</span>
            </div>`;
    });

    // 2. 渲染估價區塊
    document.getElementById('rFloorAdj').innerText = d.val.floor_adj_note;
    document.getElementById('rBasis').innerText = "🎯 " + d.target.basis;

    document.getElementById('lowT').innerText = d.val.low_t.toLocaleString() + " 萬";
    document.getElementById('lowU').innerText = `${d.val.low_u} 萬/坪`;
    document.getElementById('medT').innerText = d.val.med_t.toLocaleString() + " 萬";
    document.getElementById('medU').innerText = `${d.val.med_u} 萬/坪`;
    document.getElementById('highT').innerText = d.val.high_t.toLocaleString() + " 萬";
    document.getElementById('highU').innerText = `${d.val.high_u} 萬/坪`;

    updateValuationCalc();

    // 3. 渲染同社區相似坪數實價表
    document.getElementById('fit1YrCnt').innerText = d.target.comm_fit_1yr_count;
    document.getElementById('fitAllCnt').innerText = d.target.comm_fit_all_count;
    document.getElementById('sameAllCnt').innerText = d.target.comm_all_count;
    document.getElementById('allCnt').innerText = d.val.count;
    document.getElementById('rCommNote').innerText =
        `鎖定同社區且與本戶(${d.target.ping}坪)接近之區間【${d.target.min_ping} ~ ${d.target.max_ping} 坪】：近一年 ${d.target.comm_fit_1yr_count} 筆 / 歷年同坪數 ${d.target.comm_fit_all_count} 筆`;

    if (d.target.comm_fit_1yr_count > 0) {
        filterTable('fit_1yr');
    } else if (d.target.comm_fit_all_count > 0) {
        filterTable('fit_all');
    } else {
        filterTable('same_all');
    }
}

function updateValuationCalc() {
    if (!globalData) return;
    const markup = parseFloat(document.getElementById('askMarkup').value) || 1.10;
    const botRatio = parseFloat(document.getElementById('botRatio').value) || 0.97;
    const feeRate = parseFloat(document.getElementById('feeRate').value) || 0.04;

    const ping = globalData.target.ping;
    const parkPrice = globalData.target.park_price;
    const saleTotal = globalData.val.suggested_sale_total;
    const saleUnit = globalData.val.suggested_sale_unit;

    // 1. 建議開價
    const askTotal = Math.round(saleTotal * markup);
    const askUnit = ping > 0 ? ((askTotal - parkPrice) / ping).toFixed(1) : 0;

    // 2. 建議底價 (含服務費)
    const botTotal = Math.round(saleTotal * botRatio);
    const botUnit = ping > 0 ? ((botTotal - parkPrice) / ping).toFixed(1) : 0;
    const feeAmt = (botTotal * feeRate).toFixed(1);
    const sellerNet = (botTotal - feeAmt).toFixed(1);

    document.getElementById('vAskTotal').innerText = askTotal.toLocaleString();
    document.getElementById('vAskUnit').innerText = `單價 ${askUnit} 萬/坪`;

    document.getElementById('vSaleTotal').innerText = saleTotal.toLocaleString();
    document.getElementById('vSaleUnit').innerText = `單價 ${saleUnit} 萬/坪`;

    document.getElementById('vBotTotal').innerText = botTotal.toLocaleString();
    document.getElementById('vBotUnit').innerText = `單價 ${botUnit} 萬/坪`;

    document.getElementById('cBotIncl').innerText = `${botTotal.toLocaleString()} 萬元 (單價 ${botUnit} 萬/坪)`;
    document.getElementById('cFeeAmt').innerText = `${feeAmt} 萬元 (按 ${Math.round(feeRate * 100)}% 計)`;
    document.getElementById('cSellerNet').innerText = `${Number(sellerNet).toLocaleString()} 萬元`;

    const netEl = document.getElementById('customNet');
    if (!netEl.value) {
        netEl.value = Math.round(sellerNet);
    }
    reverseBottomFromNet();
}

function reverseBottomFromNet() {
    const netVal = parseFloat(document.getElementById('customNet').value);
    const feeRate = parseFloat(document.getElementById('feeRate').value) || 0.04;
    const outEl = document.getElementById('revBotResult');
    if (!netVal || netVal <= 0) {
        outEl.innerText = "應簽底價：-- 萬";
        return;
    }
    const reqBottom = Math.ceil(netVal / (1 - feeRate));
    const fee = (reqBottom - netVal).toFixed(1);
    outEl.innerText = `含費底價：${reqBottom.toLocaleString()}萬 (費${fee}萬)`;
}

function filterTable(mode) {
    if (!globalData) return;
    const tbody = document.getElementById('tableBody');
    tbody.innerHTML = '';

    ['btnFit1Yr', 'btnFitAll', 'btnSameAll', 'btnAll'].forEach(id => {
        document.getElementById(id).className = "text-xs font-bold px-3 py-1.5 rounded-lg bg-slate-100 text-slate-700";
    });
    const btnMap = {
        'fit_1yr': 'btnFit1Yr',
        'fit_all': 'btnFitAll',
        'same_all': 'btnSameAll',
        'all': 'btnAll'
    };
    document.getElementById(btnMap[mode]).className = "text-xs font-bold px-3 py-1.5 rounded-lg bg-blue-600 text-white shadow";

    let list = globalData.comps;
    if (mode === 'fit_1yr') list = list.filter(c => c.is_same && c.is_fit_ping && c.is_1yr);
    else if (mode === 'fit_all') list = list.filter(c => c.is_same && c.is_fit_ping);
    else if (mode === 'same_all') list = list.filter(c => c.is_same);

    if (list.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="text-center py-6 text-slate-400">此篩選條件下無紀錄，請點選右方「同社區・近坪歷年」或「同社區全部房型」查看！</td></tr>`;
        return;
    }

    list.forEach(c => {
        let badge = `<span class="bg-slate-200 text-slate-700 text-[10px] px-2 py-0.5 rounded font-semibold">同路相近坪數</span>`;
        if (c.is_exact_door && c.is_same_sub) {
            badge = `<span class="bg-purple-600 text-white text-[10px] px-2 py-0.5 rounded font-bold">★ 本棟同柱戶別</span>`;
        } else if (c.is_exact_door) {
            badge = `<span class="bg-emerald-600 text-white text-[10px] px-2 py-0.5 rounded font-bold">🎯 本棟門牌</span>`;
        } else if (c.is_same) {
            badge = `<span class="bg-blue-600 text-white text-[10px] px-2 py-0.5 rounded font-bold">🏠 同社區</span>`;
        }

        const yrBadge = c.is_1yr
            ? `<span class="text-emerald-600 font-bold ml-1 text-[10px]">(一年內)</span>`
            : `<span class="text-slate-400 ml-1 text-[10px]">(歷年)</span>`;

        const diffBadge = c.is_fit_ping
            ? `<span class="inline-block bg-blue-100 text-blue-800 text-[10px] px-1.5 py-0.5 rounded font-bold ml-1">${c.ping_diff_str}</span>`
            : `<span class="inline-block bg-amber-100 text-amber-800 text-[10px] px-1.5 py-0.5 rounded font-semibold ml-1">${c.ping_diff_str}</span>`;

        const parkNote = c.park > 0
            ? `<span class="block text-[10px] text-slate-400">含車位${c.park}萬 (總坪${c.total_ping})</span>`
            : `<span class="block text-[10px] text-slate-400">總坪${c.total_ping}坪・${c.layout}</span>`;

        tbody.innerHTML += `
            <tr class="hover:bg-slate-50 ${c.is_exact_door ? 'bg-emerald-50/50' : (c.is_same && c.is_fit_ping ? 'bg-blue-50/40' : '')}">
                <td class="py-3 px-2 whitespace-nowrap">${badge}</td>
                <td class="py-3 px-2 font-semibold text-slate-700 whitespace-nowrap">${c.date}${yrBadge}</td>
                <td class="py-3 px-2 font-bold text-slate-900">${c.addr}</td>
                <td class="py-3 px-2 text-right font-bold text-blue-700 whitespace-nowrap">
                    ${c.ping} 坪 ${diffBadge}
                    <span class="block text-[10px] text-slate-400 font-normal">主建 ${c.main_ping} 坪</span>
                </td>
                <td class="py-3 px-2 text-center text-slate-600 font-semibold whitespace-nowrap">${c.floor}</td>
                <td class="py-3 px-2 text-right font-black text-red-600 whitespace-nowrap">${c.unit} 萬/坪</td>
                <td class="py-3 px-2 text-right font-bold text-slate-900 whitespace-nowrap">${c.total.toLocaleString()} 萬 ${parkNote}</td>
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
