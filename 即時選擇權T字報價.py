# -*- coding: utf-8 -*-
"""
即時選擇權T字報價.py

用 TAIFEX MIS 即時報價，產出台指選擇權 (TXO) T 字報價網頁 (CALL 紅 / PUT 綠)，
並可推播摘要到 iPhone (ntfy)。設計給 GitHub Actions 排程在雲端定時執行，
你的電腦關機時也會更新網頁與推播。

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
    now = datetime.now(TW_TZ)
    h, m = now.hour, now.minute
    if (h == 8 and m >= 45) or (9 <= h <= 12) or (h == 13 and m <= 45):
        return "日盤"
    if h >= 15 or h < 5 or (h == 5 and m == 0):
        return "夜盤"
    return "非交易"


def _num(v):
    v = (v or "").replace(",", "").strip()
    if v in ("", "-"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


# ── 1. MIS 即時選擇權報價 ─────────────────────────────────────────────────────

def fetch_mis_options(session):
    """回傳 MIS QuoteList；夜盤/非交易皆可取（非交易時為最後成交價）。"""
    ref = MIS_OPT_NIGHT if session == "夜盤" else MIS_OPT_DAY
    r = requests.post(
        MIS_QUOTE_URL,
        json={"MarketType": "1", "SymbolType": "O", "KindID": "1", "CID": "TXO",
              "ExpireMonth": "", "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"},
        headers={"Content-Type": "application/json;charset=UTF-8",
                 "Accept": "application/json, text/plain, */*",
                 "Referer": ref, "User-Agent": UA},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("RtData", {}).get("QuoteList", [])


def parse_options(quote_list):
    """
    解析 MIS SymbolID（例 TXY44300G6 = root TXY / 履約 44300 / G=7月買權 / 年碼 6）。
    以 (root, 月, 年碼) 分群 = 同一到期，回傳成交量最大的到期別資料。
    """
    groups = defaultdict(lambda: {"C": {}, "P": {}, "vol": 0, "time": ""})
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
        }
        groups[gkey]["vol"] += vol
        t = it.get("CTime", "")
        if t > groups[gkey]["time"]:
            groups[gkey]["time"] = t

    if not groups:
        raise ValueError("MIS 未回傳可解析的選擇權報價")
    gkey = max(groups, key=lambda g: groups[g]["vol"])
    return gkey, groups[gkey]


# ── 2. 即時標的價（大台指期 TXF） ─────────────────────────────────────────────

def fetch_txf_price(session):
    ref = MIS_FUT_NIGHT if session == "夜盤" else MIS_FUT_DAY
    try:
        r = requests.post(
            MIS_QUOTE_URL,
            json={"MarketType": "1", "SymbolType": "F", "KindID": "1", "CID": "",
                  "ExpireMonth": "", "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"},
            headers={"Content-Type": "application/json;charset=UTF-8",
                     "Referer": ref, "User-Agent": UA},
            timeout=12,
        )
        items = r.json().get("RtData", {}).get("QuoteList", [])
        txf = [i for i in items if i.get("SymbolID", "").startswith("TXF")
               and i.get("SymbolID", "").endswith("-M")]
        txf.sort(key=lambda i: int((_num(i.get("CTotalVolume")) or 0)), reverse=True)
        for it in txf:
            px = _num(it.get("CLastPrice"))
            if px and px > 10000:
                return px, f"TXF近月({session})"
    except Exception:
        pass
    return None, ""


# ── 3. 前一交易日未平倉 OI（支撐壓力用；MIS 盤中無 OI） ────────────────────────

def fetch_prev_oi(mon, yr):
    """從 TAIFEX 每日收盤檔取對應到期別各履約價 OI，回傳 {'C':{k:oi}, 'P':{k:oi}, 'date':d}。失敗回 None。"""
    d = datetime.now(TW_TZ).date()
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Referer": TAIFEX_DL_PAGE})
    try:
        sess.get(TAIFEX_DL_PAGE, timeout=15)
    except Exception:
        return None
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
            out = {"C": defaultdict(int), "P": defaultdict(int), "date": d}
            cnt = 0
            for row in csv.DictReader(io.StringIO(text)):
                if row.get("交易時段", "").strip() != "一般":
                    continue
                try:
                    dt = row["契約到期日"].strip()          # 例 20260724
                    if not dt:
                        continue
                    if int(dt[4:6]) != mon:                  # 只取同月到期
                        continue
                    cp = "C" if row["買賣權"].strip() == "買權" else "P"
                    k  = int(float(row["履約價"]))
                    oi = int(_num(row["未沖銷契約數"]) or 0)
                    out[cp][k] += oi
                    cnt += 1
                except Exception:
                    pass
            if cnt:
                return out
        except Exception:
            pass
        d -= timedelta(days=1)
    return None


# ── 4. 組報告資料 ─────────────────────────────────────────────────────────────

def build_report(radius=1500):
    session = current_session()
    ql = fetch_mis_options(session)
    (root, mon, yr), grp = parse_options(ql)
    calls, puts = grp["C"], grp["P"]

    # 標的價：TXF 即時優先，價平 parity 備援
    under, usrc = fetch_txf_price(session)
    common = [k for k in calls if k in puts and calls[k]["px"] and puts[k]["px"]]
    if not common:
        raise ValueError("無有效買賣權對可定價")
    atm_parity = min(common, key=lambda k: abs(calls[k]["px"] - puts[k]["px"]))
    fwd = atm_parity + calls[atm_parity]["px"] - puts[atm_parity]["px"]
    if not under:
        under, usrc = fwd, "價平parity"
    atm = min(common, key=lambda k: abs(k - under))

    oi = fetch_prev_oi(mon, yr)

    lo, hi = under - radius, under + radius
    strikes = sorted(k for k in set(calls) | set(puts) if lo <= k <= hi)

    def mk(side, k, is_call):
        d = side.get(k)
        if not d or not d["px"]:
            return None
        px, vol = d["px"], d["vol"]
        return {"K": k, "px": px, "vol": vol, "amt": int(px * 50 * vol),
                "be": (k + px) if is_call else (k - px),
                "bid": d["bid"], "ask": d["ask"]}

    crows = {k: mk(calls, k, True)  for k in strikes if mk(calls, k, True)}
    prows = {k: mk(puts,  k, False) for k in strikes if mk(puts,  k, False)}

    # MIS CTime 例 213907 → 21:39:07
    t = grp["time"]
    tstr = f"{t[:2]}:{t[2:4]}:{t[4:6]}" if len(t) == 6 else "-"

    return {
        "session": session, "root": root, "mon": mon, "yr": yr,
        "under": under, "usrc": usrc, "atm": atm, "fwd": fwd,
        "strikes": strikes, "crows": crows, "prows": prows,
        "oi": oi, "time": tstr,
        "now": datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }


# ── 5. HTML 產出 ─────────────────────────────────────────────────────────────

def heat(amt, mx, base):
    t = (amt / mx) ** 0.55 if mx else 0
    r, g, b = base
    return f"rgb({int(255+(r-255)*t)},{int(255+(g-255)*t)},{int(255+(b-255)*t)})"

CALL_BASE = (214, 52, 52)
PUT_BASE  = (30, 160, 70)


def render_html(rep):
    crows, prows = rep["crows"], rep["prows"]
    cmax = max((r["amt"] for r in crows.values()), default=1)
    pmax = max((r["amt"] for r in prows.values()), default=1)
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
            bg = heat(c["amt"], cmax, CALL_BASE)
            cc = (f'<td class="amt" style="background:{bg}">{fmt(c["amt"])}</td>'
                  f'<td class="vol">{fmt(c["vol"])}</td>'
                  f'<td class="oi">{fmt(c_oiv) if c_oiv else ""}</td>'
                  f'<td class="px">{c["px"]:g}</td>'
                  f'<td class="be">{c["be"]:,.0f}</td>')
        else:
            cc = '<td class="e"></td>'*5
        if p:
            bg = heat(p["amt"], pmax, PUT_BASE)
            pc = (f'<td class="be">{p["be"]:,.0f}</td>'
                  f'<td class="px">{p["px"]:g}</td>'
                  f'<td class="oi">{fmt(p_oiv) if p_oiv else ""}</td>'
                  f'<td class="vol">{fmt(p["vol"])}</td>'
                  f'<td class="amt" style="background:{bg}">{fmt(p["amt"])}</td>')
        else:
            pc = '<td class="e"></td>'*5
        trs.append(f'<tr class="drow{atm_cls}">{cc}<td class="strike">{k:,}</td>{pc}</tr>')
    rows_html = "\n".join(trs)

    def wk_label(mon, root):
        return f"{mon}月 週選({root})"

    live = rep["session"] != "非交易"
    dot = "#e0392b" if live else "#9a9790"
    sess_txt = f'{rep["session"]}　行情時間 {rep["time"]}' if live else f'{rep["session"]}（顯示最後成交價）'

    oi_note = ""
    if oi:
        oi_note = (f'未平倉牆（{oi_date} 收盤）：買權壓力 <b>{c_wall:,}</b>、賣權支撐 <b>{p_wall:,}</b>'
                   f'{"、Put/Call 未平倉比 <b>%.2f</b>" % pcr_oi if pcr_oi else ""}。')
    else:
        oi_note = "未平倉牆：暫無前一日 OI 資料。"

    ctop_txt = f'{c_top["K"]:,}（{c_top["vol"]:,} 口）' if c_top else "-"
    ptop_txt = f'{p_top["K"]:,}（{p_top["vol"]:,} 口）' if p_top else "-"
    pcr_oi_kpi = f"{pcr_oi:.2f}" if pcr_oi else "—"

    return f'''<title>台指選擇權即時 T 字報價</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
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
.tblwrap{{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:12px}}
table{{border-collapse:collapse;width:100%;font-size:12.5px;min-width:860px}}
thead th{{position:sticky;top:0;background:var(--panel);color:var(--muted);font-weight:600;
  font-size:10.5px;letter-spacing:.3px;padding:8px 7px;border-bottom:2px solid var(--line)}}
.grp-c{{color:var(--call)}} .grp-p{{color:var(--put)}}
.drow td{{padding:4px 7px;border-bottom:1px solid var(--hair);text-align:right;white-space:nowrap}}
.strike{{text-align:center!important;font-weight:700;background:var(--bg);
  border-left:1px solid var(--line);border-right:1px solid var(--line)}}
.be,.oi{{color:var(--muted)}} .px{{font-weight:600}}
.e{{background:transparent!important}}
.drow.atm .strike{{background:var(--atm)}}
.drow.atm td{{border-top:1px solid #d8b24a;border-bottom:1px solid #d8b24a}}
.note{{color:var(--muted);font-size:11.5px;line-height:1.75;margin-top:16px}}
.note b{{color:var(--ink)}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;font-size:11.5px;color:var(--muted);margin:10px 2px 0}}
.sw{{display:inline-block;width:24px;height:10px;border-radius:2px;vertical-align:middle;margin-right:5px}}
</style>
<div class="wrap">
<h1>台指選擇權即時 T 字報價</h1>
<div class="sub"><span class="dot"></span>{sess_txt}　·　{wk_label(rep["mon"], rep["root"])}　·　標的 {rep["under"]:,.0f}（{rep["usrc"]}）　·　產生 {rep["now"]}</div>
<div class="kpis">
  <div class="kpi call"><div class="l">CALL 成交量</div><div class="v">{c_vol:,}<small> 口</small></div></div>
  <div class="kpi put"><div class="l">PUT 成交量</div><div class="v">{p_vol:,}<small> 口</small></div></div>
  <div class="kpi"><div class="l">Put/Call 量比</div><div class="v">{pcr_v:.2f}</div></div>
  <div class="kpi"><div class="l">P/C 未平倉比</div><div class="v">{pcr_oi_kpi}</div></div>
  <div class="kpi"><div class="l">價平</div><div class="v">{rep["atm"]:,}</div></div>
</div>
<div class="tblwrap">
<table>
<thead><tr>
  <th class="grp-c">CALL 金額</th><th class="grp-c">口數</th><th class="grp-c">OI</th><th class="grp-c">權利金</th><th class="grp-c">損益兩平</th>
  <th>履約價</th>
  <th class="grp-p">損益兩平</th><th class="grp-p">權利金</th><th class="grp-p">OI</th><th class="grp-p">口數</th><th class="grp-p">PUT 金額</th>
</tr></thead>
<tbody>
{rows_html}
</tbody>
</table>
</div>
<div class="legend">
  <span><span class="sw" style="background:linear-gradient(90deg,#fff,rgb(214,52,52))"></span>買權金額</span>
  <span><span class="sw" style="background:linear-gradient(90deg,#fff,rgb(30,160,70))"></span>賣權金額</span>
  <span><span class="sw" style="background:var(--atm);border:1px solid #d8b24a"></span>價平</span>
  <span>網頁每 60 秒自動重新整理</span>
</div>
<div class="note">
  <b>即時欄位</b>（MIS）：權利金、口數、金額、損益兩平；金額 = 權利金 × 50 × 口數。<br>
  <b>今日重點</b>：買權最大量 {ctop_txt}、賣權最大量 {ptop_txt}。{oi_note}<br>
  <b>限制</b>：OI 為期交所盤後公布，盤中沿用前一日；此表為 TAIFEX MIS 約每 5 秒的準即時報價，非逐筆。
</div>
</div>'''


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


def push_ntfy(rep, page_url=None):
    topic = load_ntfy_topic()
    if not topic:
        print("  ⚠ 無 ntfy topic，略過推播")
        return
    crows, prows = rep["crows"], rep["prows"]
    c_vol = sum(r["vol"] for r in crows.values())
    p_vol = sum(r["vol"] for r in prows.values())
    pcr_v = (p_vol / c_vol) if c_vol else 0
    c_top = max(crows.values(), key=lambda r: r["vol"], default=None)
    p_top = max(prows.values(), key=lambda r: r["vol"], default=None)
    lines = [
        f"標的 {rep['under']:,.0f}　價平 {rep['atm']:,}　{rep['session']} {rep['time']}",
        f"CALL {c_vol:,}口 / PUT {p_vol:,}口　P/C量比 {pcr_v:.2f}",
    ]
    if c_top: lines.append(f"買權最大量 {c_top['K']:,}（{c_top['vol']:,}口）")
    if p_top: lines.append(f"賣權最大量 {p_top['K']:,}（{p_top['vol']:,}口）")
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
    rep = build_report(radius=args.radius)
    print(f"  時段 {rep['session']}　標的 {rep['under']:,.0f}（{rep['usrc']}）　"
          f"價平 {rep['atm']:,}　行情時間 {rep['time']}")
    print(f"  CALL {len(rep['crows'])} 檔 / PUT {len(rep['prows'])} 檔　"
          f"{'有' if rep['oi'] else '無'}前一日 OI")

    html = render_html(rep)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ 已輸出 {args.out}")

    if args.notify:
        push_ntfy(rep, page_url=args.page_url or None)


if __name__ == "__main__":
    main()
