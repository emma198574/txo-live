# -*- coding: utf-8 -*-
"""
即時選擇權T字報價.py

用 TAIFEX MIS 即時報價，產出台指選擇權 (TXO) T 字報價網頁 (CALL 紅 / PUT 綠)，
並可推播摘要到 iPhone (ntfy)。設計給 GitHub Actions 排程在雲端定時執行，
你的電腦關機時也會更新網頁與推播。

網頁分成「週三結算」「週五結算」兩個分頁，各取該結算日成交量最大的到期別
（週三＝W 系列週選或月選，週五＝F 系列週選），量比、價平、▲▼ 增減都各算各的。

即時欄位（MIS，盤中/夜盤約每 5 秒更新）：權利金、成交量、成交金額、損益兩平。
盤後欄位（前一日 TAIFEX 收盤檔）：未平倉 OI → 支撐壓力牆。MIS 盤中不提供 OI。

用法：
    python3 即時選擇權T字報價.py                       # 產出 public/index.html
    python3 即時選擇權T字報價.py --notify              # 產出網頁並推播 ntfy
    python3 即時選擇權T字報價.py --out 選擇權T字報價_當日.html
    python3 即時選擇權T字報價.py --radius 1500         # 顯示價平 ±N 點（預設 1500）

雲端：設環境變數 NTFY_TOPIC（GitHub Actions Secret）即會推播。
"""

import io
import os
import re
import csv
import sys
import json
import argparse
from datetime import datetime, date, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TW_TZ    = ZoneInfo("Asia/Taipei")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MIS_QUOTE_URL  = "https://mis.taifex.com.tw/futures/api/getQuoteList"
MIS_OPT_DAY    = "https://mis.taifex.com.tw/futures/RegularSession/EquityIndices/OptionsDomestic/"
MIS_OPT_NIGHT  = "https://mis.taifex.com.tw/futures/AfterHoursSession/EquityIndices/OptionsDomestic/"
MIS_FUT_DAY    = "https://mis.taifex.com.tw/futures/RegularSession/EquityIndices/FuturesDomestic/"
MIS_FUT_NIGHT  = "https://mis.taifex.com.tw/futures/AfterHoursSession/EquityIndices/FuturesDomestic/"
TAIFEX_DL_OPT  = "https://www.taifex.com.tw/cht/3/dlOptDataDown"
TAIFEX_DL_PAGE = "https://www.taifex.com.tw/cht/3/dlOptDailyMarketView"

CALL_MON = "ABCDEFGHIJKL"      # 買權月份碼 A=1月 … L=12月
PUT_MON  = "MNOPQRSTUVWX"      # 賣權月份碼 M=1月 … X=12月
SID_PAT  = re.compile(r'^(TX[A-Z0-9])(\d{3,5})([A-Z])(\d)$')


# ── 時段判斷 ────────────────────────────────────────────────────────────────

def current_session():
    """
    回傳 (顯示用時段, MIS MarketType)。
    MarketType 才是 MIS 決定日/夜盤的參數：'0' = 一般（日盤）、'1' = 盤後（夜盤）；
    Referer 只是擺樣子，不影響回傳內容。用錯會在日盤拿到前一夜盤的殘留報價。
    非交易時段沿用剛結束那個時段的最後成交價：
      13:45~15:00 → 日盤收盤；05:00~08:45 → 前一夜盤收盤。
    """
    now = datetime.now(TW_TZ)
    h, m = now.hour, now.minute
    if (h == 8 and m >= 45) or (9 <= h <= 12) or (h == 13 and m <= 45):
        return "日盤", "0"
    if h >= 15 or h < 5 or (h == 5 and m == 0):
        return "夜盤", "1"
    return ("非交易", "0") if 13 <= h < 15 else ("非交易", "1")


def _num(v):
    v = (v or "").replace(",", "").strip()
    if v in ("", "-"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


# ── 1. MIS 即時選擇權報價 ─────────────────────────────────────────────────────

def fetch_mis_options(mkt):
    """回傳 MIS QuoteList；夜盤/非交易皆可取（非交易時為最後成交價）。"""
    ref = MIS_OPT_NIGHT if mkt == "1" else MIS_OPT_DAY
    r = requests.post(
        MIS_QUOTE_URL,
        json={"MarketType": mkt, "SymbolType": "O", "KindID": "1", "CID": "TXO",
              "ExpireMonth": "", "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"},
        headers={"Content-Type": "application/json;charset=UTF-8",
                 "Accept": "application/json, text/plain, */*",
                 "Referer": ref, "User-Agent": UA},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("RtData", {}).get("QuoteList", [])


def quote_key(cdate, ctime, night):
    """
    把 CDate + CTime 併成可比大小的整數，用來找出整份報價「最後更新」的那一筆。
    只比 CTime 字串會踩到夜盤跨午夜的坑：23:59 的字串大於凌晨 04:59，
    會把已經過去 5 小時的成交當成最新行情時間。
    """
    if not ctime or len(ctime) != 6:
        return -1
    try:
        d, t = int(cdate or 0), int(ctime)
    except ValueError:
        return -1
    if night and t < 80000:          # 夜盤 00:00~05:00 屬於隔天
        t += 240000
    return d * 1000000 + t


EXP_PAT = re.compile(r'\((\d{4})/(\d{2})/(\d{2})\)')


def third_wednesday(y, mon):
    """月選（TXO）到期日 = 該月第三個星期三。"""
    d = date(y, mon, 1)
    d += timedelta(days=(2 - d.weekday()) % 7)      # 該月第一個星期三
    return d + timedelta(days=14)


def parse_expiry(item, mon, yr):
    """
    取這檔合約的契約到期日（YYYYMMDD），用來跟 TAIFEX 盤後檔精準對上同一到期別。
    週選的 MIS 名稱直接帶日期，例 '臺指選W1 (2026/08/05)'、'臺指選F5 (2026/07/31)'；
    月選（TXO086）沒帶，改算該月第三個星期三。
    """
    for fld in ("DispEName", "DispCName"):
        m = EXP_PAT.search(item.get(fld, "") or "")
        if m:
            return "".join(m.groups())
    y = datetime.now(TW_TZ).year
    y = y - (y % 10) + int(yr)                      # 年碼是西元年個位數
    if y < datetime.now(TW_TZ).year - 1:            # 跨十年進位，例 2029 的 '0' → 2030
        y += 10
    return third_wednesday(y, mon).strftime("%Y%m%d")


SERIES_PAT = re.compile(r'臺指選\s*([A-Z]\d)')


def contract_label(name, mon):
    """把 MIS 中文名轉成短代號：'臺指選W1 (2026/08/05)' → W1；月選 '臺指選086' → 8月選。"""
    m = SERIES_PAT.search(name or "")
    return m.group(1) if m else f"{mon}月選"


def expiry_weekday(exp):
    """契約到期日是星期幾（0=一 … 6=日）；解析不出來回 -1。"""
    try:
        return date(int(exp[:4]), int(exp[4:6]), int(exp[6:8])).weekday()
    except Exception:
        return -1


def collect_groups(quote_list, night=False):
    """
    解析 MIS SymbolID（例 TXY44300G6 = root TXY / 履約 44300 / G=7月買權 / 年碼 6）。
    以 (root, 月, 年碼) 分群 = 同一到期別，回傳所有到期別 {gkey: grp}。
    """
    groups = defaultdict(lambda: {"C": {}, "P": {}, "vol": 0, "time": "", "date": "",
                                  "key": -1, "exp": "", "name": ""})
    for it in quote_list:
        sid = it.get("SymbolID", "").split("-")[0]
        m = SID_PAT.match(sid)
        if not m:
            continue
        root, strike, ltr, yr = m.groups()
        strike = int(strike)
        if ltr in CALL_MON:
            cp, mon = "C", CALL_MON.index(ltr) + 1
        elif ltr in PUT_MON:
            cp, mon = "P", PUT_MON.index(ltr) + 1
        else:
            continue
        gkey = (root, mon, yr)
        vol  = int(_num(it.get("CTotalVolume")) or 0)
        groups[gkey][cp][strike] = {
            "px":  _num(it.get("CLastPrice")),
            "vol": vol,
            "bid": _num(it.get("CBidPrice1")),
            "ask": _num(it.get("CAskPrice1")),
            # 今日相對昨收的權利金變動。MIS 直接給，不必自己存快照比對，
            # 而且是「今日累積」而非上一版的 5 分鐘差，訊號比 ▲▼ 強。
            "ref":  _num(it.get("CRefPrice")),
            "diff": _num(it.get("CDiff")),
            "rate": _num(it.get("CDiffRate")),
            "open": _num(it.get("COpenPrice")),
            "high": _num(it.get("CHighPrice")),
            "low":  _num(it.get("CLowPrice")),
        }
        groups[gkey]["vol"] += vol
        if not groups[gkey]["exp"]:
            groups[gkey]["exp"] = parse_expiry(it, mon, yr)
        if not groups[gkey]["name"]:
            groups[gkey]["name"] = contract_label(it.get("DispCName", ""), mon)
        t, dt = it.get("CTime", ""), it.get("CDate", "")
        kk = quote_key(dt, t, night)
        if kk > groups[gkey]["key"]:
            groups[gkey].update(key=kk, time=t, date=dt)

    if not groups:
        raise ValueError("MIS 未回傳可解析的選擇權報價")
    return dict(groups)


def pick_by_weekday(groups, weekday):
    """挑出到期日落在指定星期、且成交量最大的那個到期別；沒有就回 None。"""
    cand = {g: v for g, v in groups.items()
            if expiry_weekday(v["exp"]) == weekday and v["vol"] > 0}
    if not cand:
        return None
    gkey = max(cand, key=lambda g: cand[g]["vol"])
    return gkey, cand[gkey]


# ── 2. 即時標的價（大台指期 TXF） ─────────────────────────────────────────────

def fetch_txf_price(session, mkt):
    # 期貨的 SymbolID 後綴會跟著時段變：日盤 TXFH6-F、夜盤 TXFH6-M。
    ref  = MIS_FUT_NIGHT if mkt == "1" else MIS_FUT_DAY
    sfx  = "-M" if mkt == "1" else "-F"
    try:
        r = requests.post(
            MIS_QUOTE_URL,
            json={"MarketType": mkt, "SymbolType": "F", "KindID": "1", "CID": "",
                  "ExpireMonth": "", "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"},
            headers={"Content-Type": "application/json;charset=UTF-8",
                     "Referer": ref, "User-Agent": UA},
            timeout=12,
        )
        items = r.json().get("RtData", {}).get("QuoteList", [])
        txf = [i for i in items if i.get("SymbolID", "").startswith("TXF")
               and i.get("SymbolID", "").endswith(sfx)]
        txf.sort(key=lambda i: int((_num(i.get("CTotalVolume")) or 0)), reverse=True)
        for it in txf:
            px = _num(it.get("CLastPrice"))
            if px and px > 10000:
                return px, f"TXF近月({session})"
    except Exception:
        pass
    return None, ""


# ── 3. 前一交易日未平倉 OI（支撐壓力用；MIS 盤中無 OI） ────────────────────────

_OI_CACHE = {}          # 一次下載、多個到期別共用（週三／週五分頁都要查同一份收盤檔）


def load_oi_buckets():
    """
    下載 TAIFEX 最近一個交易日的選擇權收盤檔，照「契約到期日」分桶。
    回傳 (buckets, 資料日期)；抓不到回 (None, None)。結果快取於行程內。
    """
    if "v" in _OI_CACHE:
        return _OI_CACHE["v"]
    _OI_CACHE["v"] = (None, None)
    d = datetime.now(TW_TZ).date()
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Referer": TAIFEX_DL_PAGE})
    try:
        sess.get(TAIFEX_DL_PAGE, timeout=15)
    except Exception:
        return _OI_CACHE["v"]
    for _ in range(8):
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        try:
            r = sess.post(TAIFEX_DL_OPT, data={
                "down_type": "1", "commodity_id": "TXO", "commodity_id2": "all",
                "queryStartDate": d.strftime("%Y/%m/%d"), "queryEndDate": d.strftime("%Y/%m/%d"),
                "commodity_id2t": "",
            }, timeout=25)
            text = r.content.decode("ms950", errors="ignore")
            # 先照到期日分桶，再挑出要的那一個到期別
            buckets = defaultdict(lambda: {"C": defaultdict(int), "P": defaultdict(int)})
            for row in csv.DictReader(io.StringIO(text)):
                if row.get("交易時段", "").strip() != "一般":
                    continue
                try:
                    dt = row["契約到期日"].strip()          # 例 20260724
                    if not dt:
                        continue
                    cp = "C" if row["買賣權"].strip() == "買權" else "P"
                    k  = int(float(row["履約價"]))
                    oi = int(_num(row["未沖銷契約數"]) or 0)
                    buckets[dt][cp][k] += oi
                except Exception:
                    pass
            if not buckets:
                d -= timedelta(days=1)
                continue
            _OI_CACHE["v"] = (buckets, d)
            return _OI_CACHE["v"]
        except Exception:
            pass
        d -= timedelta(days=1)
    return _OI_CACHE["v"]


def fetch_prev_oi(expiry, mon):
    """
    從 TAIFEX 每日收盤檔取「同一契約到期日」各履約價 OI。
    以前只比對月份，同月的週選（TXZ/TX1/TXU…）與月選會被加總成同一面牆，
    支撐壓力因此失真；改用契約到期日精準比對。
    找不到該到期日時（例如遇假日調整），退回同月合計並標記 exact=False。
    回傳 {'C':{k:oi}, 'P':{k:oi}, 'date':d, 'exp':YYYYMMDD, 'exact':bool}，失敗回 None。
    """
    buckets, d = load_oi_buckets()
    if not buckets:
        return None
    if expiry in buckets:
        b = buckets[expiry]
        return {"C": b["C"], "P": b["P"], "date": d, "exp": expiry, "exact": True}
    # 對不到（假日調整或名稱解析失誤）：退回同月合計，並在畫面上標示
    same = [dt for dt in buckets if len(dt) == 8 and int(dt[4:6]) == mon]
    if same:
        out = {"C": defaultdict(int), "P": defaultdict(int),
               "date": d, "exp": "、".join(sorted(same)), "exact": False}
        for dt in same:
            for cp in ("C", "P"):
                for k, v in buckets[dt][cp].items():
                    out[cp][k] += v
        return out
    return None


# ── 4. 組報告資料 ─────────────────────────────────────────────────────────────

def group_fwd(grp):
    """用買賣權 parity 推當組的遠期價；沒有可配對的買賣權回 None。"""
    calls, puts = grp["C"], grp["P"]
    common = [k for k in calls if k in puts and calls[k]["px"] and puts[k]["px"]]
    if not common:
        return None
    atm_parity = min(common, key=lambda k: abs(calls[k]["px"] - puts[k]["px"]))
    return atm_parity + calls[atm_parity]["px"] - puts[atm_parity]["px"]


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def moneyness(k, under, is_call):
    """價內 in-the-money／價外 out-of-the-money。買權履約價低於標的為價內，賣權相反。"""
    itm = (k < under) if is_call else (k > under)
    return "itm" if itm else "otm"


MONEY_TXT = {"itm": "價內", "otm": "價外"}


def add_excess(rows, under, is_call):
    """
    算「超額漲跌」：該履約價今日漲跌% 減掉「同側、同價內外」履約價漲跌%的中位數。

    為什麼不能直接看漲跌%：指數一漲，全部 CALL 一起漲、全部 PUT 一起跌，
    那是方向 beta，不是誰在這個履約價出手。扣掉中位數之後剩下的，
    才是這一檔相對同儕的異常強弱 —— 跌得比同儕兇 = 有人在壓價收租（築牆），
    漲得比同儕兇 = 有人在追價（想突破）。跟細族群輪動要剔除大盤 beta 同一個道理。

    為什麼還要分價內／價外：兩者的 % 變動根本不是同一種東西。價內權利金主要跟著
    內含價值走（指數跌 1,000 點，價內賣權就多 1,000 點內含價值），價外動的則是
    時間價值與隱含波動率，基數又小，%天生大得多。把兩群混在一起取中位數，
    等於拿內含價值的變動去當時間價值的基準，價內檔會被系統性誤判。

    顯著與否用 |超額| 的中位數當尺度（MAD 概念），自適應盤中波動大小，不硬編閾值。
    每一組各自算自己的中位數與尺度；樣本不足 MIN_GROUP_N 檔就整組不判定 ——
    分組後樣本變少，三五檔算出來的中位數沒有代表性，寧可留白也不要給假訊號。
    """
    # 沒成交的檔位 rate 可能是前一筆的陳舊值，不能拿來定基準
    live = [r for r in rows.values() if r["rate"] is not None and r["vol"] > 0]
    for r in rows.values():
        r["tag"] = ""
        r.setdefault("excess", None)
        r.setdefault("grp", "")
    if not live:
        return

    # 量門檻取「整側總量 1%」與絕對下限的較大值。只用相對門檻會在清淡時失效：
    # 剛掛牌的週選一側可能總共才 24 口，1% = 0.24 口，於是 1 口成交就被當成大戶，
    # 判出「賣方築牆」。單一履約價要有解讀價值，先得有起碼的口數。
    vol_gate = max((sum(r["vol"] for r in live) or 1) * 0.01, MIN_TAG_VOL)

    for g in ("itm", "otm"):
        grp = [r for r in live if moneyness(r["K"], under, is_call) == g]
        # 基準只用量夠大的檔算，且判定對象就是同一批。兩者必須對齊：
        # 冷門檔的報價更新慢，跟不上最新行情，拿它們一起算中位數會把基準拖低，
        # 結果量大的檔全部呈現正超額 —— 那是基準偏掉，不是市場真的在追價。
        core = [r for r in grp if r["vol"] >= vol_gate]
        if len(core) < MIN_GROUP_N:
            continue
        med = _median([r["rate"] for r in core])
        scale = max((_median([abs(r["rate"] - med) for r in core]) or 1.0) * EXCESS_K,
                    MIN_EXCESS_PT)
        for r in grp:                      # 超額全組都算，冷門檔也看得到自己的位置
            r["excess"] = r["rate"] - med
            r["grp"] = g
        for r in core:                     # 但只有量夠大的才下買賣方判定
            if r["excess"] <= -scale:
                r["tag"] = "sell"    # 相對同儕被壓 → 賣方築牆
            elif r["excess"] >= scale:
                r["tag"] = "buy"     # 相對同儕被追 → 買方挑戰


TAG_TXT = {"sell": "賣方築牆", "buy": "買方追價", "": "中性"}

# 一側價外合計成交量低於此數，就不把口數最大的履約價當成牆
MIN_ZONE_VOL = 500

# 超額顯著門檻 = EXCESS_K × |超額|中位數。
# 1.0 太鬆：1 MAD ≈ 0.67σ，統計上約一半的履約價都會超過，標記滿版等於沒標。
# 2.0 ≈ 1.35σ，只有約兩成會亮燈，標到的才是真的異常。
EXCESS_K = 2.0

# 價內／價外分組後，一組有效樣本少於此數就整組不判定
MIN_GROUP_N = 5

# 超額門檻的絕對下限（百分點）。純自適應會在同質性高的組裡失控：
# 價外買權有時 30 檔全部同步跌 -50~-56%，MAD 只有 1pt，門檻自動縮到 ±2pt，
# 於是連報價跳動的雜訊都會被判成「買方追價」。低於這個幅度的超額沒有解讀價值。
MIN_EXCESS_PT = 5.0

# 單一履約價要被判買賣方，至少要有的成交口數（絕對下限）
MIN_TAG_VOL = 50


def build_zone(crows, prows, under, oi, lo, hi, top=3):
    """
    盤中撐壓：用「今日累積成交口數」的分布抓牆的位置。

    為什麼排序用口數而不是金額：這張卡片要跟昨日 OI 牆對照，而 OI 的單位就是口數，
    用金額排就是拿兩把尺在比。更實際的問題是金額 = 權利金×50×口數，權利金隨著
    接近價平而變大，用金額排幾乎必然選出離標的最近的那一檔 —— 那是價平的定義，
    不是市場押注的位置。口數才是部位規模，也才是「牆有多高」。
    金額仍然並列顯示，看的是資金投入強度，兩個問題分兩欄回答。

    範圍限制在畫面的 ±radius 視窗內，順便壓掉深價外樂透票對口數的灌水。
    再用 add_excess 的 tag 標記那道牆是賣方在築（硬）還是買方在打（可能破）。
    """
    def pick(rows, keep):
        cand = [r for r in rows.values() if keep(r["K"]) and r["vol"] > 0]
        cand.sort(key=lambda r: r["vol"], reverse=True)
        return cand[:top], sum(r["vol"] for r in cand)

    res, res_vol = pick(crows, lambda k: k >= under)      # 壓力：價外買權
    sup, sup_vol = pick(prows, lambda k: k <= under)      # 支撐：價外賣權

    # 量能門檻：剛掛牌的週選一側常只有個位數口成交，那種「牆」是雜訊。
    # 寧可標成量能不足也不要給一個看起來很篤定的價位。
    res_thin = res_vol < MIN_ZONE_VOL
    sup_thin = sup_vol < MIN_ZONE_VOL

    # OI 牆要跟盤中重心放在同一個尺上比，否則會憑空生出「移動了三千點」的假訊號：
    #   1. 限制在標的同一側
    #   2. 限制在畫面顯示的 ±radius 視窗內 —— 台指深價外買權長年有賣方遠期收租的
    #      巨量存量 OI（例如標的 42,300 時最大 OI 在 46,500），那不是今天的壓力。
    def wall_of(d, keep):
        c = {k: v for k, v in d.items() if lo <= k <= hi and keep(k) and v > 0}
        return max(c, key=lambda k: c[k]) if c else None

    c_wall = wall_of(oi["C"], lambda k: k >= under) if oi else None
    p_wall = wall_of(oi["P"], lambda k: k <= under) if oi else None
    return {
        "res": res, "sup": sup,
        "res_vol": res_vol, "sup_vol": sup_vol,
        "res_thin": res_thin, "sup_thin": sup_thin,
        "res_k": res[0]["K"] if (res and not res_thin) else None,
        "sup_k": sup[0]["K"] if (sup and not sup_thin) else None,
        "c_wall": c_wall, "p_wall": p_wall,
        # 盤中金額第一名 vs 昨日同側 OI 牆差幾點；差距大代表牆在移動
        "res_shift": (res[0]["K"] - c_wall) if (res and c_wall and not res_thin) else None,
        "sup_shift": (sup[0]["K"] - p_wall) if (sup and p_wall and not sup_thin) else None,
    }


def build_report(gkey, grp, session, under, usrc, tab_id, tab_name, radius=1500):
    """把單一到期別（一個分頁）的資料整理成畫面要的形狀。"""
    root, mon, yr = gkey
    calls, puts = grp["C"], grp["P"]

    fwd = group_fwd(grp)
    common = [k for k in calls if k in puts and calls[k]["px"] and puts[k]["px"]]
    if not common:
        raise ValueError(f"{tab_name}：無有效買賣權對可定價")
    atm = min(common, key=lambda k: abs(k - under))

    expiry = grp["exp"]
    oi = fetch_prev_oi(expiry, mon)

    lo, hi = under - radius, under + radius
    strikes = sorted(k for k in set(calls) | set(puts) if lo <= k <= hi)

    def mk(side, k, is_call):
        d = side.get(k)
        if not d or not d["px"]:
            return None
        px, vol = d["px"], d["vol"]
        return {"K": k, "px": px, "vol": vol, "amt": int(px * 50 * vol),
                "be": (k + px) if is_call else (k - px),
                "bid": d["bid"], "ask": d["ask"],
                "rate": d["rate"], "diff": d["diff"], "ref": d["ref"]}

    crows = {k: mk(calls, k, True)  for k in strikes if mk(calls, k, True)}
    prows = {k: mk(puts,  k, False) for k in strikes if mk(puts,  k, False)}

    add_excess(crows, under, True)
    add_excess(prows, under, False)
    zone = build_zone(crows, prows, under, oi, lo, hi)

    # MIS CTime 例 213907 → 21:39:07；非今日的資料把日期一起標出來，
    # 免得像 07/31 早上那次：產生時間是今天早上、行情時間卻是昨晚而看不出來。
    t, dt = grp["time"], grp["date"]
    tstr = f"{t[:2]}:{t[2:4]}:{t[4:6]}" if len(t) == 6 else "-"
    today = datetime.now(TW_TZ).strftime("%Y%m%d")
    stale = bool(dt) and dt != today
    if stale:
        tstr = f"{dt[4:6]}/{dt[6:8]} {tstr}"

    return {
        "id": tab_id, "tab": tab_name, "series": grp["name"],
        "session": session, "root": root, "mon": mon, "yr": yr,
        "under": under, "usrc": usrc, "atm": atm, "fwd": fwd,
        "strikes": strikes, "crows": crows, "prows": prows,
        "oi": oi, "time": tstr, "stale": stale, "expiry": expiry,
        "vol": grp["vol"], "zone": zone,
    }


# 分頁定義：(分頁 id, 到期日星期, 分頁標題)。月選也是星期三到期，會併進週三那頁。
TABS = [("wed", 2, "週三結算"), ("fri", 4, "週五結算")]


def build_page(radius=1500):
    """抓一次 MIS，拆出週三／週五兩個到期別，組成整頁資料。"""
    session, mkt = current_session()
    ql = fetch_mis_options(mkt)
    groups = collect_groups(ql, night=(mkt == "1"))

    picks = [(tid, name) + (pick_by_weekday(groups, wd) or (None, None))
             for tid, wd, name in TABS]
    picks = [(tid, name, g, v) for tid, name, g, v in picks if g]
    if not picks:
        raise ValueError("MIS 未回傳週三／週五到期的選擇權報價")

    # 標的價：TXF 即時優先；抓不到就用成交量最大那組的 parity 推算
    under, usrc = fetch_txf_price(session, mkt)
    if not under:
        top = max(picks, key=lambda p: p[3]["vol"])
        under, usrc = group_fwd(top[3]), "價平parity"
        if not under:
            raise ValueError("無標的價可用（TXF 與 parity 皆失敗）")

    reps = [build_report(g, v, session, under, usrc, tid, name, radius=radius)
            for tid, name, g, v in picks]
    return {
        "session": session, "under": under, "usrc": usrc, "reps": reps,
        "now": datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }


# ── 5. HTML 產出 ─────────────────────────────────────────────────────────────

def heat(amt, mx, base):
    """金額格背景：熱度（資金強度），開 0.55 次方壓縮讓中小值也看得見。"""
    t = (amt / mx) ** 0.55 if mx else 0
    r, g, b = base
    return f"rgb({int(255+(r-255)*t)},{int(255+(g-255)*t)},{int(255+(b-255)*t)})"


def bar(vol, mx, base, to_left):
    """
    口數格背景畫成成交口數分布條：CALL 由右往左長、PUT 由左往右長，
    以履約價欄為中軸，整張表就是一張左右對開的盤中部位分布圖。
    刻意用線性比例（不像熱度圖壓縮），分布圖要能直接目測倍數關係 ——
    這欄回答「牆有多高」，隔壁金額欄回答「押了多少錢」。
    """
    t = (vol / mx) if mx else 0
    pct = max(0.0, min(100.0, t * 100))
    r, g, b = base
    d = "left" if to_left else "right"
    return (f"background:linear-gradient(to {d},"
            f"rgba({r},{g},{b},.42) 0 {pct:.1f}%,transparent {pct:.1f}% 100%)")

CALL_BASE = (214, 52, 52)
PUT_BASE  = (30, 160, 70)

# 在瀏覽器端用 localStorage 記住上一版（上一個「產生時間」）的金額與權利金，
# 每次載入就地算出各履約價相對上一版的增減（▲紅=增加、▼綠=減少）。
# 金額 ▲▼ 搭配權利金 ▲▼ 可判斷主導方：
#   金額增 + 權利金漲 → 買方（BC/BP）追價；金額增 + 權利金跌 → 賣方（SC/SP）壓價收租。
# 雲端每 5 分鐘換一版新資料、網頁每 60 秒重整；同一版重整不會洗掉差額。
DELTA_JS = """
<script>
(function(){
  var wrap = document.querySelector('.wrap');
  if(!wrap) return;
  var GEN = wrap.getAttribute('data-gen') || '';
  // 金額格與權利金格一起記；key = 分頁:側別:履約價:a(金額)/p(權利金)
  // 分頁要進 key，否則週三／週五同一履約價會互相蓋掉基準。
  var cells = [].slice.call(document.querySelectorAll('td[data-amt],td[data-px]'));
  function keyOf(td){
    return td.getAttribute('data-tab') + ':' + td.getAttribute('data-side') + ':' +
           td.getAttribute('data-k') + (td.hasAttribute('data-amt') ? ':a' : ':p');
  }
  var cur = {};
  cells.forEach(function(td){
    cur[keyOf(td)] = +(td.getAttribute('data-amt') || td.getAttribute('data-px'));
  });
  var prev = null;
  try { prev = JSON.parse(localStorage.getItem('txo_snap2') || 'null'); } catch(e){}
  var base = null;
  if(!prev){
    localStorage.setItem('txo_snap2', JSON.stringify({gen:GEN, cur:cur, base:cur}));
  } else if(prev.gen === GEN){
    base = prev.base;                        // 同一版重整：沿用既有基準
  } else {
    base = prev.cur;                         // 換新版：上一版數字成為新基準
    localStorage.setItem('txo_snap2', JSON.stringify({gen:GEN, cur:cur, base:prev.cur}));
  }
  if(!base) return;                          // 第一次看：尚無可比較的基準
  function fmtAmt(n){ return n.toLocaleString('en-US'); }
  function fmtPx(n){ return String(Math.round(n * 100) / 100); }   // 權利金保留小數
  cells.forEach(function(td){
    var key = keyOf(td);
    if(!(key in base)) return;
    var d = cur[key] - base[key];
    if(Math.abs(d) < 1e-9) return;
    var s = td.querySelector('.delta');
    if(!s) return;
    var isAmt = td.hasAttribute('data-amt');
    s.textContent = (d > 0 ? '▲ ' : '▼ ') + (isAmt ? fmtAmt(Math.abs(d)) : fmtPx(Math.abs(d)));
    s.className = 'delta' + (isAmt ? '' : ' plain') + (d > 0 ? ' up' : ' down');
    s.style.display = 'inline-block';
  });
})();
// 每 60 秒帶時間戳重新載入：繞過 iPhone 主畫面 App 與 CDN 的快取，永遠抓最新那版。
setTimeout(function(){
  location.replace(location.pathname + '?t=' + Date.now());
}, 60000);
</script>
"""


def money(a):
    """金額用億／萬顯示；盤中動輒上億，逐位數字反而讀不出量級。"""
    return f"{a/1e8:.2f} 億" if a >= 1e8 else f"{a/1e4:,.0f} 萬"


def chg_td(r):
    """今日漲跌%（相對昨收）。底色標的是『超額』而非漲跌本身：
    綠＝相對同側同儕被壓（賣方築牆）、紅＝被追價（買方挑戰）。"""
    rate = r.get("rate")
    if rate is None:
        return '<td class="chg"></td>'
    tag = r.get("tag", "")
    ex  = r.get("excess")
    g   = MONEY_TXT.get(r.get("grp", ""), "")
    tip = (f'今日 {rate:+.0f}%，對照{g}同儕超額 {ex:+.0f}pt → {TAG_TXT[tag]}'
           if ex is not None else f'今日 {rate:+.0f}%（樣本不足或無成交，不判定）')
    return f'<td class="chg {tag}" title="{tip}">{rate:+.0f}%</td>'


def render_zone(rep):
    """盤中撐壓卡片：今日成交金額分布抓出來的牆，並與昨日 OI 牆對照。"""
    z = rep["zone"]
    if not z:
        return ""

    def rows(items, thin, vol):
        if thin:
            return (f'<div class="zrow thin"><span class="zm">量能不足（此側價外合計 {vol:,} 口），'
                    f'不做撐壓判讀</span></div>')
        out = []
        for r in items:
            tag = r.get("tag", "")
            rt  = f'{r["rate"]:+.0f}%' if r.get("rate") is not None else "—"
            out.append(
                f'<div class="zrow"><b>{r["K"]:,}</b>'
                f'<span class="zm">{r["vol"]:,} 口</span>'
                f'<span class="zv">{money(r["amt"])}</span>'
                f'<span class="zr {tag}">{rt}</span>'
                f'<span class="zt {tag}">{TAG_TXT[tag]}</span></div>')
        return "\n".join(out) or '<div class="zrow"><span class="zm">此側無成交</span></div>'

    def note(shift, wall, label):
        # 這裡的 OI 牆已限定在標的同一側，跟盤中重心是可比的範圍
        if wall is None:
            return f'昨日 {label} OI 牆：視窗內無資料'
        if shift is None:
            return f'昨日 {label} OI 牆 {wall:,}'
        if abs(shift) <= 100:
            return f'昨日 {label} OI 牆 {wall:,}　·　盤中重心與其一致'
        d = "上移" if shift > 0 else "下移"
        return f'昨日 {label} OI 牆 {wall:,}　·　盤中重心{d} {abs(shift):,} 點'

    return f'''<div class="zones">
  <div class="zone res">
    <div class="zh">盤中壓力區<small>標的之上・買權今日成交口數</small></div>
    {rows(z["res"], z["res_thin"], z["res_vol"])}
    <div class="zn">{note(z["res_shift"], z["c_wall"], "買權")}</div>
  </div>
  <div class="zone sup">
    <div class="zh">盤中支撐區<small>標的之下・賣權今日成交口數</small></div>
    {rows(z["sup"], z["sup_thin"], z["sup_vol"])}
    <div class="zn">{note(z["sup_shift"], z["p_wall"], "賣權")}</div>
  </div>
</div>'''


def render_panel(rep):
    """單一到期別（一個分頁）的 KPI + T 字表 + 說明。"""
    tid = rep["id"]
    crows, prows = rep["crows"], rep["prows"]
    cmax  = max((r["amt"] for r in crows.values()), default=1)
    pmax  = max((r["amt"] for r in prows.values()), default=1)
    cvmax = max((r["vol"] for r in crows.values()), default=1)
    pvmax = max((r["vol"] for r in prows.values()), default=1)
    c_vol = sum(r["vol"] for r in crows.values())
    p_vol = sum(r["vol"] for r in prows.values())
    c_amt = sum(r["amt"] for r in crows.values())
    p_amt = sum(r["amt"] for r in prows.values())
    pcr_v = (p_vol / c_vol) if c_vol else 0
    c_top = max(crows.values(), key=lambda r: r["vol"], default=None)
    p_top = max(prows.values(), key=lambda r: r["vol"], default=None)

    oi = rep["oi"]
    c_wall = p_wall = pcr_oi = oi_date = None
    if oi:
        c_oi_all = oi["C"]; p_oi_all = oi["P"]
        if c_oi_all: c_wall = max(c_oi_all, key=lambda k: c_oi_all[k])
        if p_oi_all: p_wall = max(p_oi_all, key=lambda k: p_oi_all[k])
        tc = sum(c_oi_all.values()); tp = sum(p_oi_all.values())
        pcr_oi = (tp / tc) if tc else None
        oi_date = oi["date"].strftime("%m/%d")

    def fmt(n): return f"{n:,}"

    trs = []
    for k in rep["strikes"]:
        c = crows.get(k); p = prows.get(k)
        atm_cls = " atm" if k == rep["atm"] else ""
        c_oiv = oi["C"].get(k) if oi else None
        p_oiv = oi["P"].get(k) if oi else None
        if c:
            cc = (f'<td class="amt" data-tab="{tid}" data-side="C" data-k="{k}" data-amt="{c["amt"]}" '
                  f'style="background:{heat(c["amt"], cmax, CALL_BASE)}">'
                  f'<span class="amtnum">{fmt(c["amt"])}</span><span class="delta"></span></td>'
                  f'<td class="vol" style="{bar(c["vol"], cvmax, CALL_BASE, True)}">{fmt(c["vol"])}</td>'
                  f'<td class="oi">{fmt(c_oiv) if c_oiv else ""}</td>'
                  f'{chg_td(c)}'
                  f'<td class="px" data-tab="{tid}" data-side="C" data-k="{k}" data-px="{c["px"]:g}">'
                  f'<span class="pxnum">{c["px"]:g}</span><span class="delta plain"></span></td>'
                  f'<td class="be">{c["be"]:,.0f}</td>')
        else:
            cc = '<td class="e"></td>'*6
        if p:
            pc = (f'<td class="be">{p["be"]:,.0f}</td>'
                  f'<td class="px" data-tab="{tid}" data-side="P" data-k="{k}" data-px="{p["px"]:g}">'
                  f'<span class="pxnum">{p["px"]:g}</span><span class="delta plain"></span></td>'
                  f'{chg_td(p)}'
                  f'<td class="oi">{fmt(p_oiv) if p_oiv else ""}</td>'
                  f'<td class="vol" style="{bar(p["vol"], pvmax, PUT_BASE, False)}">{fmt(p["vol"])}</td>'
                  f'<td class="amt" data-tab="{tid}" data-side="P" data-k="{k}" data-amt="{p["amt"]}" '
                  f'style="background:{heat(p["amt"], pmax, PUT_BASE)}">'
                  f'<span class="amtnum">{fmt(p["amt"])}</span><span class="delta"></span></td>')
        else:
            pc = '<td class="e"></td>'*6
        trs.append(f'<tr class="drow{atm_cls}">{cc}<td class="strike">{k:,}</td>{pc}</tr>')
    rows_html = "\n".join(trs)

    e = rep["expiry"]
    exp_txt = f'{e[4:6]}/{e[6:8]} 到期（{rep["series"]}）'
    live = rep["session"] != "非交易" and not rep["stale"]
    time_txt = f'行情時間 {rep["time"]}' if live else f'最後成交 {rep["time"]}'
    if rep["stale"]:
        time_txt = f'⚠ 非今日行情　最後成交 {rep["time"]}'

    oi_note = ""
    if oi:
        # 標出 OI 是哪一個到期別，免得跟畫面上的週選搞混
        if oi["exact"]:
            oe = oi["exp"]
            scope = f'{oe[4:6]}/{oe[6:8]} 到期'
        else:
            scope = '⚠ 同月各週合計（對不到單一到期日）'
        oi_note = (f'未平倉牆（{oi_date} 收盤・{scope}）：買權壓力 <b>{c_wall:,}</b>、'
                   f'賣權支撐 <b>{p_wall:,}</b>'
                   f'{"、Put/Call 未平倉比 <b>%.2f</b>" % pcr_oi if pcr_oi else ""}。')
    else:
        oi_note = "未平倉牆：暫無前一日 OI 資料。"

    ctop_txt = f'{c_top["K"]:,}（{c_top["vol"]:,} 口）' if c_top else "-"
    ptop_txt = f'{p_top["K"]:,}（{p_top["vol"]:,} 口）' if p_top else "-"
    pcr_oi_kpi = f"{pcr_oi:.2f}" if pcr_oi else "—"

    return f'''<section class="panel" data-panel="{tid}">
<div class="sub sub-tab">{exp_txt}　·　{time_txt}　·　本到期別成交 {rep["vol"]:,} 口</div>
<div class="kpis">
  <div class="kpi call"><div class="l">CALL 成交量</div><div class="v">{c_vol:,}<small> 口</small></div></div>
  <div class="kpi put"><div class="l">PUT 成交量</div><div class="v">{p_vol:,}<small> 口</small></div></div>
  <div class="kpi"><div class="l">Put/Call 量比</div><div class="v">{pcr_v:.2f}</div></div>
  <div class="kpi"><div class="l">P/C 未平倉比</div><div class="v">{pcr_oi_kpi}</div></div>
  <div class="kpi"><div class="l">價平</div><div class="v">{rep["atm"]:,}</div></div>
</div>
{render_zone(rep)}
<div class="tblwrap">
<table>
<thead><tr>
  <th class="grp-c">CALL 金額</th><th class="grp-c">口數</th><th class="grp-c">OI</th><th class="grp-c">今日</th><th class="grp-c">權利金</th><th class="grp-c">損益兩平</th>
  <th>履約價</th>
  <th class="grp-p">損益兩平</th><th class="grp-p">權利金</th><th class="grp-p">今日</th><th class="grp-p">OI</th><th class="grp-p">口數</th><th class="grp-p">PUT 金額</th>
</tr></thead>
<tbody>
{rows_html}
</tbody>
</table>
</div>
<div class="note">
  <b>本頁重點</b>：買權最大量 {ctop_txt}、賣權最大量 {ptop_txt}。{oi_note}
</div>
</section>'''


# 分頁切換：按鈕控制哪一個 panel 顯示，選擇存 localStorage，
# 這樣每 60 秒自動重整回來時還停在原本看的那一頁。
TAB_JS = """
<script>
(function(){
  var btns = [].slice.call(document.querySelectorAll('.tab'));
  if(!btns.length) return;
  var panels = [].slice.call(document.querySelectorAll('.panel'));
  function show(id){
    btns.forEach(function(b){ b.classList.toggle('on', b.getAttribute('data-tab') === id); });
    panels.forEach(function(p){ p.classList.toggle('on', p.getAttribute('data-panel') === id); });
    try { localStorage.setItem('txo_tab', id); } catch(e){}
  }
  var ids = btns.map(function(b){ return b.getAttribute('data-tab'); });
  var saved = null;
  try { saved = localStorage.getItem('txo_tab'); } catch(e){}
  show(ids.indexOf(saved) >= 0 ? saved : ids[0]);
  btns.forEach(function(b){
    b.addEventListener('click', function(){ show(b.getAttribute('data-tab')); });
  });
})();
</script>
"""


def render_html(page):
    """整頁：共用表頭 + 週三／週五分頁。"""
    reps = page["reps"]
    live = page["session"] != "非交易" and not any(r["stale"] for r in reps)
    dot = "#e0392b" if live else "#9a9790"
    sess_txt = page["session"] if live else f'{page["session"]}（顯示最後成交價）'

    tabs = []
    for r in reps:
        e = r["expiry"]
        tabs.append(f'<button class="tab" data-tab="{r["id"]}">{r["tab"]}'
                    f'<small>{e[4:6]}/{e[6:8]} {r["series"]}</small></button>')
    tabs_html   = "\n  ".join(tabs)
    panels_html = "\n".join(render_panel(r) for r in reps)

    return f'''<meta charset="utf-8">
<title>台指選擇權即時 T 字報價</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<style>
:root{{--bg:#f7f6f3;--panel:#fff;--ink:#1c1b19;--muted:#6b6862;--line:#e7e4dd;
  --call:#c0392b;--put:#1e7a3c;--atm:#fff6d8;--hair:#efece5;}}
@media(prefers-color-scheme:dark){{:root{{--bg:#17181a;--panel:#1f2124;--ink:#ececec;
  --muted:#9a9790;--line:#2e3033;--call:#ff6b5c;--put:#54c777;--atm:#3a3418;--hair:#26282b;}}}}
:root[data-theme=dark]{{--bg:#17181a;--panel:#1f2124;--ink:#ececec;--muted:#9a9790;
  --line:#2e3033;--call:#ff6b5c;--put:#54c777;--atm:#3a3418;--hair:#26282b;}}
:root[data-theme=light]{{--bg:#f7f6f3;--panel:#fff;--ink:#1c1b19;--muted:#6b6862;
  --line:#e7e4dd;--call:#c0392b;--put:#1e7a3c;--atm:#fff6d8;--hair:#efece5;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,"PingFang TC","Helvetica Neue",Arial,sans-serif;
  font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased;}}
.wrap{{max-width:1080px;margin:0 auto;padding:24px 14px 60px;}}
h1{{font-size:20px;margin:0 0 4px;font-weight:700;letter-spacing:.3px}}
.sub{{color:var(--muted);font-size:12.5px;margin-bottom:6px;line-height:1.6}}
.dot{{display:inline-block;width:8px;height:8px;border-radius:50%;background:{dot};margin-right:6px;
  vertical-align:middle;animation:pulse 1.6s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.35}}}}
@media(prefers-reduced-motion:reduce){{.dot{{animation:none}}}}
.kpis{{display:grid;grid-template-columns:repeat(5,1fr);gap:9px;margin:14px 0}}
@media(max-width:640px){{.kpis{{grid-template-columns:repeat(2,1fr)}}}}
.kpi{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px}}
.kpi .l{{font-size:10.5px;color:var(--muted);letter-spacing:.4px;margin-bottom:4px}}
.kpi .v{{font-size:18px;font-weight:700}} .kpi .v small{{font-size:11px;font-weight:500;color:var(--muted)}}
.kpi.call .v{{color:var(--call)}} .kpi.put .v{{color:var(--put)}}
.zones{{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin:0 0 14px}}
@media(max-width:640px){{.zones{{grid-template-columns:1fr}}}}
.zone{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 13px}}
.zone.res{{border-left:3px solid var(--call)}} .zone.sup{{border-left:3px solid var(--put)}}
.zh{{font-size:12px;font-weight:700;margin-bottom:7px}}
.zh small{{font-weight:500;color:var(--muted);font-size:10.5px;margin-left:7px}}
.zrow{{display:flex;align-items:baseline;gap:8px;font-size:12px;padding:3px 0;flex-wrap:wrap}}
.zrow b{{font-size:14px;min-width:58px}}
.zm{{color:var(--ink);min-width:56px}} .zv,.zr,.zt{{color:var(--muted);font-size:11px}}
.zr.sell,.zt.sell{{color:var(--put)}} .zr.buy,.zt.buy{{color:var(--call)}}
.zt{{margin-left:auto;font-weight:600}}
.zrow.thin .zm{{color:var(--muted);font-size:11.5px}}
.zn{{color:var(--muted);font-size:10.5px;margin-top:7px;padding-top:7px;border-top:1px solid var(--hair)}}
.tblwrap{{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:12px}}
table{{border-collapse:collapse;width:100%;font-size:12.5px;min-width:960px}}
thead th{{position:sticky;top:0;background:var(--panel);color:var(--muted);font-weight:600;
  font-size:10.5px;letter-spacing:.3px;padding:8px 7px;border-bottom:2px solid var(--line)}}
.grp-c{{color:var(--call)}} .grp-p{{color:var(--put)}}
.drow td{{padding:4px 7px;border-bottom:1px solid var(--hair);text-align:right;white-space:nowrap}}
.strike{{text-align:center!important;font-weight:700;background:var(--bg);
  border-left:1px solid var(--line);border-right:1px solid var(--line)}}
.be,.oi{{color:var(--muted)}} .px{{font-weight:600}}
.chg{{font-size:11px;color:var(--muted)}}
.chg.sell{{color:var(--put);font-weight:700;background:rgba(30,160,70,.13)}}
.chg.buy{{color:var(--call);font-weight:700;background:rgba(214,52,52,.13)}}
.amt .amtnum{{display:block}} .px .pxnum{{display:block}}
.delta{{display:none;font-size:9.5px;font-weight:700;line-height:1.4;margin-top:1px;
  padding:0 4px;border-radius:3px;background:rgba(0,0,0,.34);letter-spacing:.2px}}
.delta.plain{{background:transparent;padding:0}}
.delta.plain.up{{color:var(--call)}} .delta.plain.down{{color:var(--put)}}
.delta.up{{color:#ff6a5c}} .delta.down{{color:#37d67a}}
.e{{background:transparent!important}}
.drow.atm .strike{{background:var(--atm)}}
.drow.atm td{{border-top:1px solid #d8b24a;border-bottom:1px solid #d8b24a}}
.note{{color:var(--muted);font-size:11.5px;line-height:1.75;margin-top:16px}}
.note b{{color:var(--ink)}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;font-size:11.5px;color:var(--muted);margin:10px 2px 0}}
.sw{{display:inline-block;width:24px;height:10px;border-radius:2px;vertical-align:middle;margin-right:5px}}
.tabs{{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 10px}}
.tab{{appearance:none;font:inherit;font-size:13px;font-weight:700;cursor:pointer;
  background:var(--panel);color:var(--muted);border:1px solid var(--line);
  border-radius:999px;padding:8px 16px;line-height:1.3}}
.tab small{{display:block;font-size:10.5px;font-weight:500;opacity:.8;margin-top:2px}}
.tab.on{{background:var(--ink);color:var(--bg);border-color:var(--ink)}}
.sub-tab{{margin:0 2px 4px}}
.panel{{display:none}} .panel.on{{display:block}}
</style>
<div class="wrap" data-gen="{page["now"]}">
<h1>台指選擇權即時 T 字報價</h1>
<div class="sub"><span class="dot"></span>{sess_txt}　·　標的 {page["under"]:,.0f}（{page["usrc"]}）　·　產生 {page["now"]}</div>
<div class="tabs">
  {tabs_html}
</div>
{panels_html}
<div class="legend">
  <span><span class="sw" style="background:linear-gradient(90deg,transparent,rgba(214,52,52,.42))"></span>買權口數分布（由右往左）</span>
  <span><span class="sw" style="background:linear-gradient(90deg,rgba(30,160,70,.42),transparent)"></span>賣權口數分布（由左往右）</span>
  <span><span class="sw" style="background:linear-gradient(90deg,#fff,rgb(214,52,52))"></span>金額熱度</span>
  <span><span class="sw" style="background:var(--atm);border:1px solid #d8b24a"></span>價平</span>
  <span><b style="color:#ff6a5c">▲</b> 較上一版增加　<b style="color:#37d67a">▼</b> 較上一版減少（金額與權利金皆有）</span>
  <span>網頁每 60 秒自動重新整理</span>
</div>
<div class="note">
  <b>即時欄位</b>（MIS）：權利金、口數、金額、今日漲跌、損益兩平；金額 = 權利金 × 50 × 口數，為今日累積。<br>
  <b>盤中撐壓怎麼來的</b>：OI 盤中不更新，所以牆的位置改用<b>今日累積成交口數</b>抓 ——
  壓力取標的之上口數最大的買權、支撐取標的之下口數最大的賣權，範圍限在畫面的 ±1,500 點視窗內。
  用口數而非金額，是因為要跟 OI 牆對照，而 OI 的單位就是口數；而且金額 = 權利金×50×口數，
  權利金隨著接近價平而變大，用金額排幾乎必然選出離標的最近那一檔，那是價平的定義而非市場押注的位置。
  口數欄的分布條回答「牆有多高」，金額欄的熱度回答「押了多少錢」。
  一側價外合計不足 500 口時直接標示量能不足，不硬給價位。<br>
  <b>賣方築牆／買方追價</b>：直接看漲跌%會誤判 —— 指數一漲，全部買權一起漲、全部賣權一起跌，那是方向 beta。
  本表扣掉<b>同側所有履約價漲跌%的中位數</b>，剩下的「超額」才是這一檔相對同儕的異常強弱：
  跌得比同儕兇（綠）＝有人壓價收租，牆較硬；漲得比同儕兇（紅）＝有人追價，該價位可能被挑戰。
  顯著門檻用超額絕對值的中位數自適應，不是固定值。<br>
  <b>牆在移動</b>：卡片下緣比對盤中金額重心與昨日 OI 牆。兩者背離超過 100 點，代表今天的資金押在別的價位，
  昨日那道支撐壓力已經不是同一個位置。<br>
  <b>分頁</b>：兩個分頁是不同結算日的合約（週三＝W 系列或月選、週五＝F 系列），
  各自獨立計算量比、價平與 ▲▼ 增減；切換後的選擇會記住，自動重整不會跳回去。<br>
  <b>限制</b>：OI 為期交所盤後公布，盤中沿用前一日；成交金額只知道成交，不知道那一口是新倉還是平倉，
  所以「築牆」是傾向推論而非事實。此表為 TAIFEX MIS 約每 5 秒的準即時報價，非逐筆。
</div>
</div>''' + TAB_JS + DELTA_JS


# ── 6. ntfy 推播 ─────────────────────────────────────────────────────────────

def load_ntfy_topic():
    t = os.environ.get("NTFY_TOPIC", "")
    if t:
        return t
    p = os.path.join(BASE_DIR, "ntfy_config.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("topic", "")
    return ""


def push_ntfy(page, page_url=None):
    topic = load_ntfy_topic()
    if not topic:
        print("  ⚠ 無 ntfy topic，略過推播")
        return
    reps = page["reps"]
    lines = [f"標的 {page['under']:,.0f}　{page['session']} {reps[0]['time']}"
             + ("　⚠ 非今日行情" if any(r["stale"] for r in reps) else "")]
    # 推播沒有「上一版」可比，做不到網頁的 ▲▼；改帶絕對數字，
    # 讓手機上不開網頁也看得出牆在哪個履約價、押了多重。
    def wall(label, r):
        # 帶上今日漲跌%與買賣方研判，手機上不開網頁也看得出這道牆硬不硬
        rt = f"　今日 {r['rate']:+.0f}%（{TAG_TXT.get(r.get('tag',''),'')}）" if r.get("rate") is not None else ""
        return (f"{label} {r['K']:,}（{r['vol']:,}口）"
                f"　權利金 {r['px']:g}　金額 {money(r['amt'])}{rt}")
    for rep in reps:                      # 週三／週五各一段
        crows, prows = rep["crows"], rep["prows"]
        c_vol = sum(r["vol"] for r in crows.values())
        p_vol = sum(r["vol"] for r in prows.values())
        pcr_v = (p_vol / c_vol) if c_vol else 0
        e = rep["expiry"]
        z = rep["zone"]
        lines.append(f"── {rep['tab']} {e[4:6]}/{e[6:8]}（{rep['series']}）　價平 {rep['atm']:,}")
        lines.append(f"CALL {c_vol:,}口 / PUT {p_vol:,}口　P/C量比 {pcr_v:.2f}")
        # 只推盤中撐壓。以前另外推的「買權／賣權最大量」現在跟撐壓同樣以口數排序，
        # 幾乎必然是同一檔，徒增重複；更糟的是量能不足時撐壓已被擋掉，
        # 那兩行卻會把只有個位數口的雜訊從後門推出來。
        if z and (z["sup_k"] or z["res_k"]):
            sup = f"{z['sup_k']:,}" if z["sup_k"] else "—"
            res = f"{z['res_k']:,}" if z["res_k"] else "—"
            lines.append(f"盤中撐壓　支撐 {sup}　壓力 {res}")
        if z and z["res"] and not z["res_thin"]:
            lines.append(wall("盤中壓力", z["res"][0]))
        elif z and z["res_thin"]:
            lines.append(f"盤中壓力　量能不足（價外合計 {z['res_vol']:,} 口）")
        if z and z["sup"] and not z["sup_thin"]:
            lines.append(wall("盤中支撐", z["sup"][0]))
        elif z and z["sup_thin"]:
            lines.append(f"盤中支撐　量能不足（價外合計 {z['sup_vol']:,} 口）")
    body = "\n".join(lines)
    headers = {"Title": "選擇權即時 T 字報價".encode("utf-8"), "Tags": "chart_with_upwards_trend"}
    if page_url:
        headers["Click"] = page_url
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers, timeout=10)
        print("  ✓ ntfy 推播成功")
    except Exception as e:
        print(f"  ⚠ ntfy 推播失敗：{e}")


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(BASE_DIR, "public", "index.html"),
                    help="HTML 輸出路徑（預設 public/index.html）")
    ap.add_argument("--radius", type=int, default=1500, help="顯示價平 ±N 點（預設 1500）")
    ap.add_argument("--notify", action="store_true", help="推播摘要到 ntfy")
    ap.add_argument("--page-url", default=os.environ.get("PAGE_URL", ""),
                    help="推播點擊要開的網頁網址（GitHub Pages 網址）")
    args = ap.parse_args()

    print(f"[{datetime.now(TW_TZ):%H:%M:%S}] 抓取 MIS 即時報價…")
    page = build_page(radius=args.radius)
    print(f"  時段 {page['session']}　標的 {page['under']:,.0f}（{page['usrc']}）")
    for rep in page["reps"]:
        oi = rep["oi"]
        oi_txt = "無前一日 OI" if not oi else (
            f"OI {oi['date']:%m/%d} 收盤・到期 {oi['exp']}"
            f"{'' if oi['exact'] else '（⚠ 同月合計，非單一到期日）'}")
        print(f"  [{rep['tab']}] {rep['series']}　到期 {rep['expiry']}　價平 {rep['atm']:,}　"
              f"行情時間 {rep['time']}{'　⚠ 非今日行情' if rep['stale'] else ''}")
        print(f"      CALL {len(rep['crows'])} 檔 / PUT {len(rep['prows'])} 檔　{oi_txt}")

    html = render_html(page)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ 已輸出 {args.out}")

    if args.notify:
        push_ntfy(page, page_url=args.page_url or None)


if __name__ == "__main__":
    main()
