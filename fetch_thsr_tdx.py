#!/usr/bin/env python3
"""
從 TDX 抓高鐵「每日時刻表」，轉成高鐵里程工具（index.html）使用的列格式，存成 tdx/YYYY-MM-DD.json。

使用方法
  本機測試：
    export TDX_CLIENT_ID=你的ClientId
    export TDX_CLIENT_SECRET=你的ClientSecret
    python3 fetch_thsr_tdx.py
  正式使用：由 .github/workflows/tdx-timetable.yml 每天自動執行，金鑰放在 GitHub Secrets，不寫進程式。

輸出格式（每天一檔）：
  {"date": "2026-11-19", "updated": "2026-11-19 00:20",
   "S": ["803;;0615,0626,...", ...],   # 南下，欄位順序 南港→左營
   "N": ["802;;0615,0629,...", ...]}   # 北上，欄位順序 左營→南港
  每列與 index.html 內建班表同格式：車次;行駛日(留空);12 欄 hhmm。
  空白 = 不在該車路線範圍，"-" = 通過不停。
  中間站取「發車時間」、終點站取「到站時間」，與 index.html 的 TT_IS_DEPARTURE=true 假設一致。

另外輸出（供轉乘看板使用）：
  tdx/thsr-full/YYYY-MM-DD.json  高鐵每車各站 [站碼, 到站, 發車]（Nagang-trans 用）
  tdx/metro.json                 捷運／輕軌指定車站的平日、假日發車時刻（Ankang-trans、Nagang-trans 用）

只用 Python 標準函式庫，不需安裝任何套件。
"""
import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOKEN_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
# 高鐵每日時刻表（依日期）。路徑如有異動，請到 TDX Swagger 的 THSR 分類核對。
DAILY_URL = "https://tdx.transportdata.tw/api/basic/v2/Rail/THSR/DailyTimetable/TrainDate/{date}?$format=JSON"

TPE_TZ = timezone(timedelta(hours=8))
OUT_DIR = Path(__file__).resolve().parent / "tdx"
DAYS_AHEAD = [0, 1, 2]   # 抓今天、明天、後天；昨天的檔案前一天已抓好
KEEP_DAYS = 3            # 刪除 3 天前的舊檔

# 南下順序（與 index.html 的 STATIONS 一致）
S_ORDER = ["NAG", "TPE", "BAQ", "TAY", "HSC", "MIL", "TAC", "CHA", "YUL", "CHY", "TNN", "ZUY"]
N_ORDER = list(reversed(S_ORDER))

# TDX 高鐵車站代碼 → 工具內代碼；若代碼對不上，改用中文站名比對（見 station_code）
ID_MAP = {"0990": "NAG", "1000": "TPE", "1010": "BAQ", "1020": "TAY", "1030": "HSC", "1035": "MIL",
          "1040": "TAC", "1043": "CHA", "1047": "YUL", "1050": "CHY", "1060": "TNN", "1070": "ZUY"}
NAME_MAP = {"南港": "NAG", "台北": "TPE", "板橋": "BAQ", "桃園": "TAY", "新竹": "HSC", "苗栗": "MIL",
            "台中": "TAC", "彰化": "CHA", "雲林": "YUL", "嘉義": "CHY", "台南": "TNN", "左營": "ZUY"}


def http_json(req, tries=4):
    """呼叫 API；遇到 429（呼叫太頻繁）就等 5、10、20 秒後重試。"""
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == tries - 1:
                try:
                    body = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    body = ""
                raise RuntimeError(f"HTTP {e.code} {e.reason}；TDX 回應：{body}") from None
            wait = 5 * 2 ** i
            print(f"TDX 回應 429（太頻繁），{wait} 秒後重試", file=sys.stderr)
            time.sleep(wait)


def get_token(cid, secret):
    data = urllib.parse.urlencode({"grant_type": "client_credentials",
                                   "client_id": cid, "client_secret": secret}).encode()
    req = urllib.request.Request(TOKEN_URL, data=data,
                                 headers={"content-type": "application/x-www-form-urlencoded"})
    return http_json(req)["access_token"]


def station_code(stop):
    sid = str(stop.get("StationID", ""))
    if sid in ID_MAP:
        return ID_MAP[sid]
    name = (stop.get("StationName") or {}).get("Zh_tw", "").replace("臺", "台").replace("站", "")
    if name in NAME_MAP:
        return NAME_MAP[name]
    raise ValueError(f"無法辨識車站：{sid} {name}")


def hhmm(t):
    """'06:25' → '0625'"""
    return t.replace(":", "")[:4] if t else ""


def to_row(train):
    """把一筆 TDX 列車資料轉成 ('S'|'N', '車次;;12欄')"""
    no = str(train["DailyTrainInfo"]["TrainNo"]).lstrip("0") or "0"
    stops = sorted(train["StopTimes"], key=lambda s: s["StopSequence"])
    codes = [station_code(s) for s in stops]
    # 方向由起訖站在南下序列中的先後判斷，不依賴 Direction 欄位的編碼
    d = "S" if S_ORDER.index(codes[0]) < S_ORDER.index(codes[-1]) else "N"
    order = S_ORDER if d == "S" else N_ORDER
    times = {}
    for i, (c, s) in enumerate(zip(codes, stops)):
        last = i == len(stops) - 1
        times[c] = hhmm(s.get("ArrivalTime") if last else (s.get("DepartureTime") or s.get("ArrivalTime")))
    a, b = order.index(codes[0]), order.index(codes[-1])
    cols = []
    for j, c in enumerate(order):
        if c in times:
            cols.append(times[c])
        elif a < j < b:
            cols.append("-")      # 路線範圍內但不停
        else:
            cols.append("")       # 不在路線範圍
    return d, f"{no};;{','.join(cols)}", stops[0].get("DepartureTime", "")


# ---------------- 捷運／輕軌車站時刻表 ----------------
METRO_URL = "https://tdx.transportdata.tw/api/basic/v2/Rail/Metro/StationTimeTable/{op}?{q}$format=JSON"


def metro_get(op, token):
    """先用 $filter 只抓需要的車站；若 TDX 不接受篩選（400）再改抓全部。"""
    names = sorted({stn for (o, stn, _) in METRO_WANT.values() if o == op})
    flt = " or ".join(f"StationName/Zh_tw eq '{n}'" for n in names)
    hdr = {"authorization": f"Bearer {token}", "accept": "application/json"}
    try:
        q = "$filter=" + urllib.parse.quote(flt) + "&"
        data = http_json(urllib.request.Request(METRO_URL.format(op=op, q=q), headers=hdr))
        if data:
            return data
        print(f"捷運 {op}：篩選後沒有資料，改抓全部", file=sys.stderr)
    except RuntimeError as ex:
        if "HTTP 400" not in str(ex):
            raise
        print(f"捷運 {op}：篩選不被接受，改抓全部", file=sys.stderr)
    time.sleep(2)
    return http_json(urllib.request.Request(METRO_URL.format(op=op, q=""), headers=hdr))
# 各線車站順序（用來判斷某筆時刻的終點站是在「哪個方向」）
LINE_ORDER = {
    "NTALRT": ["雙城", "玫瑰中國城", "台北小城", "耕莘安康院區", "景文科大", "安康", "陽光運動公園", "新和國小", "十四張"],
    "NTMC": ["大坪林", "十四張", "秀朗橋", "景平", "景安", "中和", "橋和", "中原", "板新", "板橋",
             "新埔民生", "頭前庄", "幸福", "新北產業園區"],
}
# 需要的時刻：鍵 → (營運者, 車站, 方向)。方向 +1 = 往車站順序後面，-1 = 往前面，0 = 不分（起點站）
METRO_WANT = {
    "BL23_W": ("TRTC", "南港展覽館", 0),      # 板南線南港展覽館發車（起點站，全部往頂埔／亞東醫院）
    "K_ANKANG": ("NTALRT", "安康", +1),        # 安康 → 十四張
    "K_SSZ": ("NTALRT", "十四張", -1),        # 十四張 → 雙城
    "Y_SZ_F": ("NTMC", "十四張", +1),         # 十四張 → 新北產業園區
    "Y_SZ_B": ("NTMC", "十四張", -1),         # 十四張 → 大坪林
    "Y_BQ_F": ("NTMC", "板橋", +1),           # 板橋 → 新北產業園區
    "Y_BQ_B": ("NTMC", "板橋", -1),           # 板橋 → 大坪林
}


def zh(o):
    return ((o or {}).get("Zh_tw") or "").replace("臺", "台").replace("站", "")


def fetch_metro(token, now):
    """抓各營運者的車站時刻表，依 METRO_WANT 整理成 tdx/metro.json。
    任何一項失敗只記錄、不中斷；沒抓到的鍵沿用舊檔，網頁再退回內建時刻。"""
    path = OUT_DIR / "metro.json"
    try:
        out = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        out = {}
    cache = {}
    for key, (op, stn, way) in METRO_WANT.items():
        try:
            if op not in cache:
                cache[op] = None                       # 先標記，失敗時同一營運者不再重複呼叫
                time.sleep(2)                          # 間隔一下，避免短時間連續呼叫被擋
                cache[op] = metro_get(op, token)
                print(f"捷運 {op}：{len(cache[op])} 筆車站時刻資料")
            if cache[op] is None:
                raise RuntimeError(f"{op} 資料未取得")
            wd, we, dests = set(), set(), set()
            order = LINE_ORDER.get(op)
            for e in cache[op]:
                if zh(e.get("StationName")) != stn:
                    continue
                dest = zh(e.get("DestinationStationName"))
                dests.add(dest)
                if dest == stn:
                    continue
                if way and order:
                    if dest not in order or stn not in order:
                        continue
                    if (order.index(dest) - order.index(stn)) * way <= 0:
                        continue
                sd = e.get("ServiceDay") or {}
                times = [t.get("DepartureTime") or t.get("ArrivalTime") for t in (e.get("Timetables") or [])]
                times = [t[:5] for t in times if t]
                if sd.get("Monday") or sd.get("Tuesday") or sd.get("Wednesday"):
                    wd.update(times)
                if sd.get("Saturday") or sd.get("Sunday"):
                    we.update(times)
            # 00:00–02:59 視為前一營運日的末段，排序時放最後
            k = lambda t: (int(t[:2]) + (24 if int(t[:2]) < 3 else 0), t[3:])
            if wd or we:
                out[key] = {"wd": " ".join(sorted(wd, key=k)), "we": " ".join(sorted(we, key=k))}
                print(f"捷運 {key}：平日 {len(wd)} 班、假日 {len(we)} 班")
            else:
                print(f"捷運 {key}：找不到資料（{stn} 的終點站有：{'、'.join(sorted(dests)) or '無'}）")
        except Exception as ex:
            print(f"捷運 {key}：抓取失敗 {ex}", file=sys.stderr)
    out["updated"] = now.strftime("%Y-%m-%d %H:%M")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")


def main():
    cid, secret = os.environ.get("TDX_CLIENT_ID"), os.environ.get("TDX_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("缺少環境變數 TDX_CLIENT_ID / TDX_CLIENT_SECRET")
    token = get_token(cid, secret)
    OUT_DIR.mkdir(exist_ok=True)
    now = datetime.now(TPE_TZ)
    ok = 0
    for d in DAYS_AHEAD:
        date = (now + timedelta(days=d)).strftime("%Y-%m-%d")
        req = urllib.request.Request(DAILY_URL.format(date=date),
                                     headers={"authorization": f"Bearer {token}", "accept": "application/json"})
        try:
            data = http_json(req)
        except Exception as e:
            print(f"{date}：抓取失敗 {e}", file=sys.stderr)
            continue
        if not data:
            print(f"{date}：TDX 尚無資料，保留舊檔")
            continue
        full = []
        for tr in data:
            st = sorted(tr["StopTimes"], key=lambda s: s["StopSequence"])
            full.append([str(tr["DailyTrainInfo"]["TrainNo"]).lstrip("0"),
                         [[str(s.get("StationID")), s.get("ArrivalTime", ""), s.get("DepartureTime", "")] for s in st]])
        (OUT_DIR / "thsr-full").mkdir(exist_ok=True)
        (OUT_DIR / "thsr-full" / f"{date}.json").write_text(
            json.dumps({"date": date, "updated": now.strftime("%Y-%m-%d %H:%M"), "trains": full}, ensure_ascii=False),
            encoding="utf-8")
        rows = {"S": [], "N": []}
        for tr in data:
            d_, row, dep = to_row(tr)
            rows[d_].append((dep, row))
        out = {"date": date, "updated": now.strftime("%Y-%m-%d %H:%M"),
               "S": [r for _, r in sorted(rows["S"])], "N": [r for _, r in sorted(rows["N"])]}
        (OUT_DIR / f"{date}.json").write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")
        print(f"{date}：南下 {len(out['S'])} 班、北上 {len(out['N'])} 班")
        ok += 1
    # 清除舊檔
    cutoff = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    for f in list(OUT_DIR.glob("*.json")) + list(OUT_DIR.glob("thsr-full/*.json")):
        if f.stem[:2] == "20" and f.stem < cutoff:
            f.unlink()
    fetch_metro(token, now)
    if ok == 0:
        sys.exit("沒有任何一天抓取成功")   # 讓 GitHub Actions 標示失敗並寄信通知


if __name__ == "__main__":
    main()
