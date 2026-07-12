# -*- coding: utf-8 -*-
"""每日市場觀察 — 靜態日報產生器。

產出 docs/index.html（完整 HTML 文件，GitHub Pages 直接服務）。
GitHub Actions 版：每小時快照＋重建。

資料來源（全部免費、無金鑰）：
- 加密貨幣行情：Binance /api/v3/ticker/24hr、/api/v3/klines（備援 data-api.binance.vision）
- DOM 深度：C:\\trading-data\\dom\\dom_history.csv（由 src/dom/dom_snapshot.py 累積）
- 台股個股：TWSE openapi STOCK_DAY_ALL（最近交易日）＋ BWIBBU_ALL（PE/殖利率/PB）
- 台股大盤：TWSE openapi FMTQIK（每日市場成交統計）
- 籌碼：TWSE rwd T86 三大法人買賣超，近 5 個交易日加總

誠實規則：每個區塊獨立抓取，失敗就在頁面顯示「取得失敗」，不編造數字。
"""
import csv
import datetime as dt
import html
import os
import sys
import time

import requests

import scoring

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "docs")
DOM_CSV = os.path.join(ROOT, "data", "dom_history.csv")
H = {"User-Agent": "Mozilla/5.0"}
BINANCE_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]
STABLE = {"USDC", "FDUSD", "TUSD", "DAI", "EUR", "USDP", "BUSD", "USD1", "EURI", "XUSD", "RLUSD"}
TPE = dt.timezone(dt.timedelta(hours=8))


def esc(x):
    return html.escape(str(x))


def fnum(x):
    try:
        return float(str(x).replace(",", ""))
    except Exception:
        return None


def bget(path, params=None):
    last = None
    for host in BINANCE_HOSTS:
        try:
            r = requests.get(host + path, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"Binance 取得失敗: {last}")


# ---------- 1) 加密貨幣技術面 ----------
def crypto_scan():
    tick = bget("/api/v3/ticker/24hr")
    pairs = []
    for t in tick:
        s = t["symbol"]
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if base in STABLE or base.endswith(("UP", "DOWN")):
            continue
        pairs.append((s, float(t["quoteVolume"])))
    top = sorted(pairs, key=lambda x: -x[1])[:15]
    rows = []
    for sym, qv in top:
        kl = bget("/api/v3/klines", {"symbol": sym, "interval": "1d", "limit": 120})
        closes = [float(k[4]) for k in kl]
        highs = [float(k[2]) for k in kl]
        lows = [float(k[3]) for k in kl]
        vols = [float(k[5]) for k in kl]
        if len(closes) < 61:
            continue
        last = closes[-1]
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60
        deltas = [closes[i] - closes[i - 1] for i in range(len(closes) - 14, len(closes))]
        gain = sum(d for d in deltas if d > 0) / 14
        loss = sum(-d for d in deltas if d < 0) / 14
        rsi = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
        volr = (sum(vols[-5:]) / 5) / (sum(vols[-20:]) / 20) if sum(vols[-20:]) else None
        trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
               for i in range(len(closes) - 14, len(closes))]
        rows.append({"sym": sym[:-4], "close": last, "vs20": (last / ma20 - 1) * 100,
                     "vs60": (last / ma60 - 1) * 100, "bull": ma20 > ma60, "rsi": rsi,
                     "volr": volr, "atr": sum(trs) / 14, "chg7": (last / closes[-8] - 1) * 100,
                     "qv_m": qv / 1e6, "_ohlcv": (highs, lows, closes, vols)})
        time.sleep(0.12)
    return rows


# ---------- 2) DOM ----------
def dom_latest():
    if not os.path.exists(DOM_CSV):
        raise RuntimeError("尚無 DOM 歷史檔，請先跑 src/dom/dom_snapshot.py")
    with open(DOM_CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise RuntimeError("DOM 歷史檔是空的")
    latest, hist24 = {}, {}
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
    for r in rows:
        latest[r["symbol"]] = r
        ts = dt.datetime.strptime(r["ts_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        if ts >= cutoff and r.get("imb_10"):
            hist24.setdefault(r["symbol"], []).append(float(r["imb_10"]))
    out = []
    for sym, r in latest.items():
        m = {k: fnum(r[k]) for k in r if k not in ("ts_utc", "symbol")}
        m["symbol"] = sym
        m["ts"] = r["ts_utc"]
        vals = hist24.get(sym, [])
        m["imb10_mean24h"] = sum(vals) / len(vals) if vals else None
        m["n24h"] = len(vals)
        out.append(m)
    return out


# ---------- 3) 台股 ----------
def tw_market():
    j = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/FMTQIK", headers=H, timeout=60).json()
    return j[-5:]


def tw_stocks():
    da = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", headers=H, timeout=60).json()
    bw = {r["Code"]: r for r in requests.get(
        "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL", headers=H, timeout=60).json()}
    rows = []
    for r in da:
        code = r["Code"]
        if code.startswith("00") or len(code) != 4:
            continue
        tv, close, chg = fnum(r["TradeValue"]), fnum(r["ClosingPrice"]), fnum(r["Change"])
        if not tv or close is None:
            continue
        prev = close - chg if chg is not None else None
        b = bw.get(code, {})
        rows.append({"code": code, "name": r["Name"], "close": close,
                     "chg_pct": (chg / prev * 100) if (chg is not None and prev) else None,
                     "tv_e": tv / 1e8, "pe": fnum(b.get("PEratio")),
                     "yld": fnum(b.get("DividendYield")), "pb": fnum(b.get("PBratio"))})
    rows = sorted(rows, key=lambda x: -x["tv_e"])[:15]
    return rows


def tw_t86_5d(codes):
    net, dates, d, got = {c: 0.0 for c in codes}, [], dt.datetime.now(TPE).date(), 0
    while got < 5 and (dt.datetime.now(TPE).date() - d).days < 15:
        if d.weekday() < 5:
            url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={d:%Y%m%d}&selectType=ALLBUT0999&response=json"
            try:
                j = requests.get(url, headers=H, timeout=30).json()
                if j.get("stat") == "OK" and j.get("data"):
                    idx_code, idx_net = 0, len(j["fields"]) - 1
                    for row in j["data"]:
                        c = str(row[idx_code]).strip()
                        if c in net:
                            v = fnum(row[idx_net])
                            if v is not None:
                                net[c] += v
                    dates.append(str(d))
                    got += 1
            except Exception:  # noqa: BLE001 - 單日失敗跳過
                pass
            time.sleep(1.0)
        d -= dt.timedelta(days=1)
    return net, dates


# ---------- HTML ----------
CSS = """
:root{
 --bg:#FAFAF7;--card:#FFFFFF;--ink:#23262B;--muted:#676D76;--line:#E3E1DB;
 --accent:#3E5C8A;--chip:#EEF1F5;
 --buy:#C7364C;--sell:#1E8E5A;--buy-tx:#B02B40;--sell-tx:#187A4D;--flat:#8A8F98;
}
@media (prefers-color-scheme: dark){:root{
 --bg:#14161A;--card:#1C1F25;--ink:#E8E6E1;--muted:#9AA0A8;--line:#2C3037;
 --accent:#93ADD6;--chip:#252A32;
 --buy:#A83E54;--sell:#2EA673;--buy-tx:#E4677B;--sell-tx:#3FBF8A;--flat:#8A8F98;}}
:root[data-theme="dark"]{
 --bg:#14161A;--card:#1C1F25;--ink:#E8E6E1;--muted:#9AA0A8;--line:#2C3037;
 --accent:#93ADD6;--chip:#252A32;
 --buy:#A83E54;--sell:#2EA673;--buy-tx:#E4677B;--sell-tx:#3FBF8A;--flat:#8A8F98;}
:root[data-theme="light"]{
 --bg:#FAFAF7;--card:#FFFFFF;--ink:#23262B;--muted:#676D76;--line:#E3E1DB;
 --accent:#3E5C8A;--chip:#EEF1F5;
 --buy:#C7364C;--sell:#1E8E5A;--buy-tx:#B02B40;--sell-tx:#187A4D;--flat:#8A8F98;}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);margin:0;
 font-family:"Microsoft JhengHei","PingFang TC","Noto Sans TC",system-ui,sans-serif;line-height:1.65}
.wrap{max-width:980px;margin:0 auto;padding:28px 20px 60px;display:flex;flex-direction:column;gap:28px}
.mast{border-bottom:2px solid var(--ink);padding-bottom:14px;display:flex;flex-wrap:wrap;
 align-items:baseline;gap:8px 16px}
.mast h1{font-size:1.55rem;margin:0;letter-spacing:.12em}
.mast .date{font-size:1.05rem;color:var(--accent);font-weight:700}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-left:auto}
.chip{background:var(--chip);color:var(--muted);border-radius:999px;padding:2px 12px;
 font-size:.74rem;letter-spacing:.05em}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px 22px}
h2{font-size:1.02rem;margin:0 0 4px;color:var(--accent);letter-spacing:.14em}
.sub{font-size:.8rem;color:var(--muted);margin:0 0 14px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px}
.kpi .l{font-size:.72rem;color:var(--muted);letter-spacing:.08em}
.kpi .v{font-size:1.3rem;font-weight:700;font-variant-numeric:tabular-nums}
.kpi .d{font-size:.8rem;font-variant-numeric:tabular-nums}
.up{color:var(--buy-tx)}.dn{color:var(--sell-tx)}.fl{color:var(--flat)}
.tbl{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.84rem;white-space:nowrap}
th{color:var(--muted);font-weight:600;text-align:right;padding:6px 10px;border-bottom:1px solid var(--line);
 font-size:.76rem;letter-spacing:.04em}
td{padding:6px 10px;text-align:right;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}
tr:last-child td{border-bottom:none}
.code{font-family:ui-monospace,Consolas,monospace;font-size:.82rem}
.domrow{display:grid;grid-template-columns:110px 1fr;gap:6px 14px;align-items:center;margin:10px 0}
.domsym{font-weight:700;font-variant-numeric:tabular-nums}
.dombar{display:flex;align-items:center;gap:2px}
.dombar .side{flex:1;display:flex;align-items:center;min-height:14px}
.dombar .side.b{justify-content:flex-end}
.bar{height:12px;border-radius:4px;min-width:2px}
.bar.b{background:var(--buy)}
.bar.s{background:var(--sell)}
.dombar .lab{font-size:.74rem;color:var(--muted);padding:0 8px;font-variant-numeric:tabular-nums;white-space:nowrap}
.axis{width:2px;align-self:stretch;background:var(--line)}
.legend{display:flex;gap:18px;font-size:.76rem;color:var(--muted);margin:4px 0 10px}
.legend .sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.notes li{margin:6px 0;font-size:.9rem}
.fail{color:var(--buy-tx);background:var(--chip);border-radius:8px;padding:10px 14px;font-size:.86rem}
.appendix p,.appendix li{font-size:.82rem;color:var(--muted)}
.disc{font-size:.78rem;color:var(--muted);border-top:1px solid var(--line);padding-top:14px}
footer{font-size:.74rem;color:var(--muted);text-align:center}
"""


def pct_cell(v, digits=2):
    if v is None:
        return '<td class="fl">—</td>'
    cls = "up" if v > 0 else ("dn" if v < 0 else "fl")
    arrow = "▲" if v > 0 else ("▼" if v < 0 else "–")
    return f'<td class="{cls}">{arrow} {abs(v):.{digits}f}%</td>'


def num(v, digits=2, dash="—"):
    return dash if v is None else f"{v:,.{digits}f}"


def build():
    now = dt.datetime.now(TPE)
    parts, notes, fails = [], [], []

    try:
        cs = crypto_scan()
    except Exception as e:  # noqa: BLE001
        cs, _ = [], fails.append(f"加密貨幣行情：{e}")
    try:
        dom = dom_latest()
    except Exception as e:  # noqa: BLE001
        dom, _ = [], fails.append(f"DOM 深度：{e}")
    try:
        twm = tw_market()
    except Exception as e:  # noqa: BLE001
        twm, _ = [], fails.append(f"台股大盤：{e}")
    try:
        tws = tw_stocks()
        t86, t86_dates = tw_t86_5d([r["code"] for r in tws]) if tws else ({}, [])
    except Exception as e:  # noqa: BLE001
        tws, t86, t86_dates = [], {}, []
        fails.append(f"台股個股：{e}")

    try:
        scores, fg_val = scoring.score_all(cs, dom) if cs else ([], None)
    except Exception as e:  # noqa: BLE001
        scores, fg_val = [], None
        fails.append(f"綜合評分：{e}")

    # ---- KPI 列 ----
    kpi = []
    cmap = {r["sym"]: r for r in cs}
    for sym in ("BTC", "ETH"):
        r = cmap.get(sym)
        if r:
            cls = "up" if r["chg7"] > 0 else "dn"
            arrow = "▲" if r["chg7"] > 0 else "▼"
            kpi.append(f'<div class="kpi"><div class="l">{sym}/USDT</div>'
                       f'<div class="v">{num(r["close"], 2 if r["close"] < 100 else 0)}</div>'
                       f'<div class="d {cls}">{arrow} {abs(r["chg7"]):.2f}%（7日）</div></div>')
    if twm:
        last = twm[-1]
        taiex, chg = fnum(last.get("TAIEX")), fnum(last.get("Change"))
        prev = taiex - chg if (taiex and chg is not None) else None
        p = chg / prev * 100 if prev else None
        cls = "up" if (p or 0) > 0 else ("dn" if (p or 0) < 0 else "fl")
        arrow = "▲" if (p or 0) > 0 else ("▼" if (p or 0) < 0 else "–")
        kpi.append(f'<div class="kpi"><div class="l">加權指數（{esc(last.get("Date", ""))}）</div>'
                   f'<div class="v">{num(taiex, 2)}</div>'
                   f'<div class="d {cls}">{arrow} {num(abs(p) if p is not None else None)}%</div></div>')
    if dom:
        tot_b = sum(d["bid_usd_10"] or 0 for d in dom)
        tot_a = sum(d["ask_usd_10"] or 0 for d in dom)
        comp = tot_b / (tot_b + tot_a) if tot_b + tot_a else None
        cls = "up" if (comp or 0.5) > 0.55 else ("dn" if (comp or 0.5) < 0.45 else "fl")
        kpi.append(f'<div class="kpi"><div class="l">DOM 綜合失衡（5 幣 ±1%）</div>'
                   f'<div class="v {cls}">{num(comp, 3)}</div>'
                   f'<div class="d">0.5＝買賣平衡</div></div>')
    parts.append(f'<div class="kpis">{"".join(kpi)}</div>')

    # ---- 綜合評分 ----
    if scores:
        def _cell(v):
            if v is None:
                return '<td class="fl">缺</td>'
            cls = "up" if v >= 60 else ("dn" if v <= 40 else "")
            return f'<td class="{cls}">{v}</td>'
        rows = []
        for sc in scores:
            tone = "up" if (sc["total"] or 50) >= 60 else ("dn" if (sc["total"] or 50) <= 45 else "")
            rows.append(
                f'<tr><td class="code">{esc(sc["sym"])}</td>'
                f'<td class="{tone}"><b>{sc["total"] if sc["total"] is not None else "—"}</b></td>'
                f'<td>{esc(sc["grade"])}</td>'
                + _cell(sc["sub"]["tech"]) + _cell(sc["sub"]["depth"]) + _cell(sc["sub"]["chips"])
                + _cell(sc["sub"]["fund"]) + _cell(sc["sub"]["news"])
                + pct_cell(sc["chg7"])
                + f'<td>{sc["heat"] if sc["heat"] is not None else "—"}</td></tr>')
        w = scoring.WEIGHTS
        fg_txt = f"｜Fear &amp; Greed 指數：{fg_val}" if fg_val is not None else "｜Fear &amp; Greed 取得失敗"
        parts.append(
            '<section><h2>加密貨幣綜合評分（24h 成交額前 10）</h2>'
            f'<p class="sub">五面向 0–100 分、50 中性｜權重：技術 {w["tech"]}%・深度 {w["depth"]}%・'
            f'籌碼 {w["chips"]}%・基本 {w["fund"]}%・新聞情緒 {w["news"]}%｜'
            f'「缺」＝該面向資料源失敗，總分由其餘面向按權重重算{fg_txt}｜公式與限制見附錄</p>'
            '<div class="tbl"><table><tr><th>標的</th><th>總分</th><th>評級</th><th>技術</th><th>深度</th>'
            '<th>籌碼</th><th>基本</th><th>新聞</th><th>7 日</th><th>24h 新聞則數</th></tr>'
            + "".join(rows) + '</table></div></section>')

    # ---- DOM 區 ----
    if dom:
        rows_html, tbl = [], []
        maxv = max(max(d["bid_usd_10"] or 0, d["ask_usd_10"] or 0) for d in dom) or 1
        for d in sorted(dom, key=lambda x: -(x["bid_usd_10"] or 0) - (x["ask_usd_10"] or 0)):
            b, a = d["bid_usd_10"] or 0, d["ask_usd_10"] or 0
            bw_, aw = int(b / maxv * 100), int(a / maxv * 100)
            imb = d["imb_10"]
            if imb is not None and (imb < 0.4 or imb > 0.6):
                side = "買方" if imb > 0.6 else "賣方"
                notes.append(f"<b>{esc(d['symbol'])}</b> ±1% 內{side}掛單明顯較厚"
                             f"（失衡比 {imb:.3f}，買 {b/1e6:.1f}M vs 賣 {a/1e6:.1f}M USD）。")
            sat = (d["cover_bid_pct"] or 99) < 2 or (d["cover_ask_pct"] or 99) < 2
            rows_html.append(
                f'<div class="domrow"><div class="domsym code">{esc(d["symbol"])}</div>'
                f'<div class="dombar" title="±1% 內：買 {b:,.0f} / 賣 {a:,.0f} USD">'
                f'<div class="side b"><span class="lab">買 {b/1e6:,.1f}M</span>'
                f'<div class="bar b" style="width:{bw_}%"></div></div><div class="axis"></div>'
                f'<div class="side"><div class="bar s" style="width:{aw}%"></div>'
                f'<span class="lab">賣 {a/1e6:,.1f}M</span></div></div></div>')
            tbl.append(f'<tr><td class="code">{esc(d["symbol"])}</td><td>{num(d["mid"], 4 if (d["mid"] or 0) < 10 else 2)}</td>'
                       f'<td>{num(d["spread_bp"], 3)}</td>'
                       f'<td>{num(d["imb_5"], 3)}</td><td>{num(d["imb_10"], 3)}</td><td>{num(d["imb_20"], 3)}</td>'
                       f'<td>{num(d["imb10_mean24h"], 3)}（{d["n24h"]} 筆）</td>'
                       f'<td>{"±" + num(min(d["cover_bid_pct"], d["cover_ask_pct"]), 1) + "%" }{"（±2% 帶已飽和）" if sat else ""}</td></tr>')
        ts = esc(dom[0]["ts"])
        parts.append(
            '<section><h2>市場深度 DOM 指標</h2>'
            f'<p class="sub">Binance 現貨訂單簿（5000 檔）量化快照｜最新快照 {ts}（UTC）｜'
            '失衡比＝買掛單值 ÷（買＋賣），0.5 為平衡</p>'
            '<div class="legend"><span><span class="sw" style="background:var(--buy)"></span>買盤（bid，左）</span>'
            '<span><span class="sw" style="background:var(--sell)"></span>賣盤（ask，右）</span>'
            '<span>量條＝±1% 內掛單價值</span></div>'
            + "".join(rows_html) +
            '<div class="tbl"><table><tr><th>標的</th><th>中價</th><th>價差 bp</th><th>失衡 ±0.5%</th>'
            '<th>失衡 ±1%</th><th>失衡 ±2%</th><th>24h 均值（樣本）</th><th>簿深覆蓋</th></tr>'
            + "".join(tbl) + '</table></div></section>')

    # ---- 加密貨幣技術面 ----
    if cs:
        rows = []
        for r in sorted(cs, key=lambda x: -x["qv_m"]):
            if r["rsi"] > 75:
                notes.append(f"<b>{esc(r['sym'])}</b> RSI(14) {r['rsi']:.0f} 過熱，追高風險大。")
            rows.append(f'<tr><td class="code">{esc(r["sym"])}</td><td>{num(r["close"], 4 if r["close"] < 10 else 2)}</td>'
                        + pct_cell(r["chg7"]) + pct_cell(r["vs20"]) + pct_cell(r["vs60"])
                        + f'<td>{"多頭" if r["bull"] else "空頭"}</td><td>{num(r["rsi"], 0)}</td>'
                        f'<td>{num(r["volr"], 2)}</td><td>{num(r["qv_m"], 0)}</td></tr>')
        parts.append(
            '<section><h2>加密貨幣技術面（24h 成交額前 15）</h2>'
            '<p class="sub">日線指標｜「排列」＝MA20 與 MA60 相對位置｜量比＝5 日均量 ÷ 20 日均量</p>'
            '<div class="tbl"><table><tr><th>標的</th><th>收盤</th><th>7 日</th><th>vs MA20</th>'
            '<th>vs MA60</th><th>排列</th><th>RSI14</th><th>量比</th><th>24h 額(M)</th></tr>'
            + "".join(rows) + "</table></div></section>")

    # ---- 台股 ----
    tw_parts = []
    if twm:
        rows = "".join(
            f'<tr><td>{esc(r.get("Date", ""))}</td><td>{num(fnum(r.get("TAIEX")), 2)}</td>'
            + pct_cell((fnum(r.get("Change")) / (fnum(r.get("TAIEX")) - fnum(r.get("Change"))) * 100)
                       if fnum(r.get("TAIEX")) and fnum(r.get("Change")) is not None else None)
            + f'<td>{num((fnum(r.get("TradeValue")) or 0) / 1e8, 0)}</td></tr>'
            for r in twm)
        tw_parts.append('<h2>台股大盤（近 5 個交易日）</h2>'
                        '<p class="sub">來源：證交所每日市場成交統計</p>'
                        '<div class="tbl"><table><tr><th>日期</th><th>加權指數</th><th>漲跌</th>'
                        '<th>成交值（億）</th></tr>' + rows + "</table></div>")
    if tws:
        buys = sorted(((c, v) for c, v in t86.items() if v > 0), key=lambda x: -x[1])[:3]
        sells = sorted(((c, v) for c, v in t86.items() if v < 0), key=lambda x: x[1])[:3]
        name = {r["code"]: r["name"] for r in tws}
        if buys:
            notes.append("台股法人 5 日買超（TOP15 內）：" + "、".join(
                f"<b>{esc(name[c])}</b>（{v/1000:,.0f} 張）" for c, v in buys) + "。")
        if sells:
            notes.append("台股法人 5 日賣超（TOP15 內）：" + "、".join(
                f"<b>{esc(name[c])}</b>（{v/1000:,.0f} 張）" for c, v in sells) + "。")
        rows = "".join(
            f'<tr><td><span class="code">{esc(r["code"])}</span> {esc(r["name"])}</td>'
            f'<td>{num(r["close"], 2)}</td>' + pct_cell(r["chg_pct"])
            + f'<td>{num(r["tv_e"], 1)}</td><td>{num(r["pe"], 1)}</td><td>{num(r["yld"], 2)}</td>'
            f'<td>{num(r["pb"], 2)}</td><td>{num((t86.get(r["code"]) or 0) / 1000, 0)}</td></tr>'
            for r in tws)
        d_note = f"｜法人統計日：{t86_dates[-1]}～{t86_dates[0]}" if t86_dates else "｜法人資料取得失敗"
        tw_parts.append('<h2 style="margin-top:22px">台股成交值 TOP 15（個股，不含 ETF）</h2>'
                        f'<p class="sub">最近交易日收盤｜基本面：證交所 BWIBBU{d_note}</p>'
                        '<div class="tbl"><table><tr><th>個股</th><th>收盤</th><th>漲跌</th><th>成交值(億)</th>'
                        '<th>本益比</th><th>殖利率%</th><th>淨值比</th><th>法人5日(張)</th></tr>'
                        + rows + "</table></div>")
    if tw_parts:
        parts.append("<section>" + "".join(tw_parts) + "</section>")

    # ---- 觀察筆記 ----
    if notes:
        parts.append('<section><h2>觀察筆記（規則自動生成）</h2><ul class="notes">'
                     + "".join(f"<li>{n}</li>" for n in notes) + "</ul></section>")
    if fails:
        parts.append('<section><h2>取得失敗的區塊</h2>'
                     + "".join(f'<div class="fail">{esc(f)}</div>' for f in fails) + "</section>")

    # ---- 附錄＋免責 ----
    parts.append(
        '<section class="appendix"><h2>附錄：指標定義與資料來源</h2><ul>'
        '<li><b>綜合評分公式</b>：技術＝均線排列/RSI/MACD/KD/布林通道/成交量比六個常見指標'
        '各自量化成 -100~100 分後合成，再換算回本頁 0~100 慣例（獨立模組 ta_scoring.py，'
        '同一套公式也用在主力資金雷達頁面，公式細節見該檔案）；'
        '深度＝50±（±1% 失衡 ±25、24h 均值 ±10、價差 +10/−10、簿深規模 ±5）；'
        '籌碼＝50±（資金費率健康 +5／擁擠 −8～−15／深負 +8、多空帳戶比極端 ±8～10、未平倉量變化×價格方向 ±8，Binance 合約、備援 OKX）；'
        '基本＝50±（市值排名 +5～15/−10、量/市值比 ±5、流通/最大供給 ±5、離 ATH 距離 ±5，CoinGecko）；'
        '新聞情緒＝50±（Fear&amp;Greed ≥80 → −10、≤20 → +10、40–70 → +5；Google News 24h 熱度 ≥15 則 → 順 7 日方向 ±8）。'
        '<b>誠實限制</b>：無金鑰新聞源只能量化熱度，無法判讀單則新聞利多利空；'
        '評分是「當下狀態描述」，不是買賣訊號，也未經回測驗證預測力。</li>'
        '<li><b>DOM 失衡比</b>＝mid ± X% 範圍內 買掛單價值 ÷（買＋賣掛單價值）。>0.55 買方厚、<0.45 賣方厚。'
        '掛單可撤可假（spoofing），單一快照僅供參考，趨勢（24h 均值）比單點可信。</li>'
        '<li><b>簿深覆蓋</b>：Binance 單次最多回傳 5000 檔掛單；若覆蓋範圍小於帶寬，該帶寬數字已飽和（偏低估）。</li>'
        '<li><b>顏色慣例</b>：本頁採台灣慣例——紅＝上漲/買方，綠＝下跌/賣方（與國際相反）。</li>'
        '<li><b>台股五檔深度</b>：無免費公開來源，需券商 API（Phase 2 接 Shioaji 後補上）。</li>'
        '<li>來源：Binance 公開 API、台灣證交所 OpenAPI/T86。加密行情為產生當下即時值；台股為最近交易日收盤。</li>'
        '</ul>'
        f'<p class="disc"><b>免責聲明</b>：本頁為程式自動彙整的技術面統計，不構成投資建議；'
        '歷史統計與掛單狀態不代表未來表現，交易決定與風險由使用者自行承擔。</p></section>')

    body = (f'<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>每日市場觀察 {now:%Y-%m-%d}</title><style>{CSS}</style></head><body>'
            f'<div class="wrap"><header class="mast"><h1>每日市場觀察</h1>'
            f'<span class="date">{now:%Y-%m-%d（%a）}</span>'
            f'<div class="chips"><span class="chip">產生 {now:%H:%M} 台北</span>'
            f'<span class="chip">加密＝即時</span><span class="chip">台股＝最近交易日</span>'
            f'<span class="chip">紅漲綠跌</span>'
            f'<span class="chip"><a href="./whales.html" style="color:inherit">加密訊息 →</a></span>'
            f'</div></header>'
            + "".join(parts) +
            '<footer>由交易機器人專案自動產生｜資料抓取失敗的區塊會如實標示，不以舊資料充數</footer></div>'
            '</body></html>')

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "index.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"OK -> {out}  ({len(body):,} bytes)")
    print(f"sections: crypto={len(cs)} dom={len(dom)} tw={len(tws)} fails={len(fails)}")
    return 0


if __name__ == "__main__":
    sys.exit(build())
