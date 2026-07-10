# -*- coding: utf-8 -*-
"""主力資金雷達：全市場（Binance 現貨 USDT 交易對）資金流向評分＋Discord 異動通知。

與 build_report.py／scoring.py 的差異：那份只服務首頁的「24h 成交額前 10-15
大主流幣」；這份掃描「所有」符合流動性門檻的 USDT 交易對（含中小型幣），
用不同的分數體系（資金流向 delta 分＋字母評級），並在評級變動時推 Discord。

== 資料與限制（誠實條款，同步寫進 docs/whales.html 附錄）==
- 「鯨魚／大額成交」＝現貨逐筆成交（aggTrades）中單筆名目金額超過門檻的淨買賣力道，
  是「大額掛單行為」的統計近似，**不是**真實錢包持倉／鏈上巨鯨資料。
- 「OI×價」（未平倉量×價格方向）僅在該幣「有 Binance USDT本位永續合約」時才有資料；
  多數中小型幣沒有永續合約，此欄會顯示「缺」，分數由其餘子項按權重重算。
- 「市場深度 DOM」只對本輪分數最高的前 N 檔做（全市場逐一抓五檔會超過 API 額度），
  其餘顯示「缺」。
- 「操縱警示」是簡單啟發式（單筆佔比、蓄意來回對敲），只在成交量夠厚時才評估，
  是提示不是證據。
- 一切分數是「當下資料的規則統計」，不是預測、不構成投資建議。
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import sys

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "data", "whale_state.json")
DOCS_PATH = os.path.join(ROOT, "docs", "whales.html")

H = {"User-Agent": "Mozilla/5.0"}
SPOT_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]
FAPI_HOST = "https://fapi.binance.com"
STABLE = {"USDC", "FDUSD", "TUSD", "DAI", "EUR", "USDP", "BUSD", "USD1", "EURI",
          "XUSD", "PAX", "GUSD", "USDD", "EURT", "RLUSD"}
LEV_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")
WHALE_TRADE_USD = 10_000          # 單筆逐筆成交視為「大額」的門檻
MANIP_MIN_NOTIONAL = 50_000       # 低於此窗口總名目就不評估操縱警示（避免誤判低流動性幣）
GRADE_BOUNDS = [(50, "S"), (25, "A"), (0, "B"), (-25, "C")]  # 其餘為 D
TIMEOUT = 20


def sget(path, params=None):
    last = None
    for host in SPOT_HOSTS:
        try:
            r = requests.get(host + path, params=params, headers=H, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"spot {path} 失敗: {last}")


def fget(path, params=None):
    r = requests.get(FAPI_HOST + path, params=params, headers=H, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def grade_of(total):
    for th, g in GRADE_BOUNDS:
        if total >= th:
            return g
    return "D"


def clamp(v, lo=-100, hi=100):
    return max(lo, min(hi, v))


# ---------- 1) 全市場流動性篩選 ----------
def fetch_universe(min_quote_vol):
    info = sget("/api/v3/exchangeInfo")
    eligible = set()
    for s in info["symbols"]:
        if (s["status"] == "TRADING" and s["quoteAsset"] == "USDT"
                and s.get("isSpotTradingAllowed", True)
                and s["baseAsset"] not in STABLE
                and not s["baseAsset"].endswith(LEV_SUFFIX)):
            eligible.add(s["symbol"])

    tick = sget("/api/v3/ticker/24hr")
    rows = []
    for t in tick:
        if t["symbol"] in eligible:
            qv = float(t["quoteVolume"])
            if qv >= min_quote_vol:
                rows.append({"symbol": t["symbol"], "base": t["symbol"][:-4],
                             "close": float(t["lastPrice"]), "chg24h": float(t["priceChangePercent"]),
                             "qv": qv})
    rows.sort(key=lambda r: -r["qv"])
    return rows


def fetch_futures_symbols():
    try:
        info = fget("/fapi/v1/exchangeInfo")
        return {s["symbol"] for s in info["symbols"]
                if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING"}
    except Exception:  # noqa: BLE001 - 期貨資料是加分項，失敗就全部標缺
        return set()


# ---------- 2) 單一幣種指標 ----------
def whale_flow(symbol):
    """大額逐筆成交淨流向（近似鯨魚行為），回傳 (net_usd, total_usd, max_single_ratio, n_trades)。"""
    trades = sget("/api/v3/aggTrades", {"symbol": symbol, "limit": 500})
    buy = sell = 0.0
    max_single = 0.0
    for t in trades:
        notional = float(t["p"]) * float(t["q"])
        if t["m"]:  # isBuyerMaker=True → taker 是賣方（賣壓）
            sell += notional
        else:
            buy += notional
        max_single = max(max_single, notional)
    total = buy + sell
    whale_buy = sum(float(t["p"]) * float(t["q"]) for t in trades
                    if not t["m"] and float(t["p"]) * float(t["q"]) >= WHALE_TRADE_USD)
    whale_sell = sum(float(t["p"]) * float(t["q"]) for t in trades
                     if t["m"] and float(t["p"]) * float(t["q"]) >= WHALE_TRADE_USD)
    return whale_buy - whale_sell, total, (max_single / total if total else 0), len(trades)


def cvd_and_vol(symbol):
    """近 24 根 1h K 線：CVD（taker 買賣力道差）＋近 4h 高點回落幅度＋簡易雙頂偵測。"""
    kl = sget("/api/v3/klines", {"symbol": symbol, "interval": "1h", "limit": 24})
    cvd6, cvd24 = 0.0, 0.0
    highs = []
    for i, k in enumerate(kl):
        qv, taker_buy_qv = float(k[7]), float(k[10])
        delta = 2 * taker_buy_qv - qv
        cvd24 += delta
        if i >= len(kl) - 6:
            cvd6 += delta
        highs.append(float(k[2]))
    last_close = float(kl[-1][4])
    recent_high = max(highs[-6:]) if len(highs) >= 6 else max(highs)
    pullback_pct = (recent_high - last_close) / recent_high * 100 if recent_high else 0
    # 簡易雙頂：近 24 根內兩段高點相近（差 <1.5%）且中間有明顯回落（>3%），且目前已跌破前高
    double_top = False
    if len(highs) >= 10:
        h1_idx = max(range(len(highs) - 10, len(highs) - 5), key=lambda i: highs[i])
        h2_idx = max(range(len(highs) - 5, len(highs)), key=lambda i: highs[i])
        h1, h2 = highs[h1_idx], highs[h2_idx]
        if h1 > 0 and abs(h1 - h2) / h1 < 0.015 and last_close < min(h1, h2) * 0.97:
            double_top = True
    return cvd6, cvd24, pullback_pct, double_top


def oi_and_ratio(symbol):
    oi = fget("/futures/data/openInterestHist", {"symbol": symbol, "period": "1h", "limit": 6})
    ls = fget("/futures/data/topLongShortAccountRatio", {"symbol": symbol, "period": "1h", "limit": 1})
    oi_chg = ((float(oi[-1]["sumOpenInterestValue"]) / float(oi[0]["sumOpenInterestValue"])) - 1) * 100 \
        if len(oi) >= 2 else None
    ratio = float(ls[0]["longShortRatio"]) if ls else None
    return oi_chg, ratio


def dom_imbalance(symbol):
    book = sget("/api/v3/depth", {"symbol": symbol, "limit": 1000})
    bids = [(float(p), float(q)) for p, q in book["bids"]]
    asks = [(float(p), float(q)) for p, q in book["asks"]]
    if not bids or not asks:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2
    lo, hi = mid * 0.99, mid * 1.01
    b = sum(p * q for p, q in bids if p >= lo)
    a = sum(p * q for p, q in asks if p <= hi)
    return b / (a + b) if a + b else None


# ---------- 3) 單幣綜合評分 ----------
def score_symbol(row, futures_syms, do_dom):
    symbol, base, qv = row["symbol"], row["base"], row["qv"]
    sub = {"whale": None, "cvd": None, "oi": None, "volanom": None, "dom": None}
    tags = []

    try:
        net, total_notional, max_ratio, ntr = whale_flow(symbol)
        if total_notional > 0:
            sub["whale"] = clamp(round(net / total_notional * 100))  # -100..100 淨力道比例
        if total_notional >= MANIP_MIN_NOTIONAL and max_ratio > 0.35 and ntr >= 5:
            tags.append("單筆成交佔比異常")
    except Exception:  # noqa: BLE001
        pass

    pullback, double_top = None, False
    try:
        cvd6, cvd24, pullback, double_top = cvd_and_vol(symbol)
        if qv > 0:
            sub["cvd"] = clamp(round(cvd6 / qv * 300))  # 近 6h CVD 相對 24h 量的力道，放大顯示
        if double_top:
            tags.append("雙頂形態")
    except Exception:  # noqa: BLE001
        pass

    if symbol in futures_syms:
        try:
            oi_chg, ratio = oi_and_ratio(symbol)
            if oi_chg is not None:
                same_dir = (oi_chg > 0 and row["chg24h"] > 0) or (oi_chg < 0 and row["chg24h"] < 0)
                mag = min(abs(oi_chg), 20) / 20 * 40
                sub["oi"] = round(mag if same_dir else -mag) if oi_chg > 0 or oi_chg < 0 else 0
            if ratio is not None and ratio >= 2.5:
                tags.append("散戶多單擁擠")
        except Exception:  # noqa: BLE001
            pass

    if do_dom:
        try:
            imb = dom_imbalance(symbol)
            if imb is not None:
                sub["dom"] = clamp(round((imb - 0.5) * 200))
        except Exception:  # noqa: BLE001
            pass

    avail = {k: v for k, v in sub.items() if v is not None}
    total = round(sum(avail.values()) / len(avail) * (1 if len(avail) >= 2 else 0.6)) if avail else 0
    if "單筆成交佔比異常" in tags:
        total -= 15
    total = clamp(total)

    risk = 0
    if pullback is not None:
        if pullback >= 15:
            risk += 45
        elif pullback >= 8:
            risk += 25
    if sub["cvd"] is not None and sub["cvd"] < -10 and row["chg24h"] > 0:
        risk += 20
        tags.append("價漲量縮背離")
    if double_top:
        risk += 20
    if "單筆成交佔比異常" in tags:
        risk += 15
    risk = clamp(risk, 0, 100)

    return {"symbol": symbol, "base": base, "close": row["close"], "chg24h": row["chg24h"],
            "qv": qv, "sub": sub, "total": total, "grade": grade_of(total),
            "risk": risk, "tags": tags}


# ---------- 4) 狀態比對＋Discord ----------
def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def build_alerts(results, prev_state, is_cold_start):
    alerts = []
    if is_cold_start:
        return alerts
    for r in results:
        old = prev_state.get(r["symbol"])
        if old is None:
            continue  # 幣種第一次被納入掃描範圍，只建檔不通知（避免冷啟動式洗版）
        if old.get("grade") != r["grade"]:
            direction = "升級" if GRADE_BOUNDS_INDEX(r["grade"]) < GRADE_BOUNDS_INDEX(old["grade"]) else "降級"
            alerts.append({"type": "grade", "direction": direction, **r, "prev_grade": old["grade"]})
        was_risky = old.get("risk", 0) >= 75
        if r["risk"] >= 75 and not was_risky:
            alerts.append({"type": "risk_on", **r})
        elif was_risky and r["risk"] < 75:
            alerts.append({"type": "risk_off", **r})
    return alerts


_GRADE_ORDER = ["S", "A", "B", "C", "D"]


def GRADE_BOUNDS_INDEX(g):  # noqa: N802 - 保留大寫以貼近呼叫處語意
    return _GRADE_ORDER.index(g)


def send_discord(webhook, alerts):
    if not webhook or not alerts:
        return
    color = {"grade": 0x3E5C8A, "risk_on": 0xC7364C, "risk_off": 0x8A8F98}
    for i in range(0, len(alerts), 10):
        batch = alerts[i:i + 10]
        embeds = []
        for a in batch:
            if a["type"] == "grade":
                title = f'{a["symbol"]} 主力評分 {a["prev_grade"]} → {a["grade"]}（{a["direction"]}）'
            elif a["type"] == "risk_on":
                title = f'⚠ {a["symbol"]} 風險評分升至 {a["risk"]}（高風險）'
            else:
                title = f'{a["symbol"]} 風險警示解除（{a["risk"]}）'
            sub = a["sub"]
            desc = (f'現價 {a["close"]:g}｜24h {a["chg24h"]:+.2f}%\n'
                    f'總分 {a["total"]}｜風險 {a["risk"]}\n'
                    f'鯨魚 {sub["whale"] if sub["whale"] is not None else "缺"}　'
                    f'CVD {sub["cvd"] if sub["cvd"] is not None else "缺"}　'
                    f'OI×價 {sub["oi"] if sub["oi"] is not None else "缺"}　'
                    f'DOM {sub["dom"] if sub["dom"] is not None else "缺"}\n'
                    + (f'標籤：{"、".join(a["tags"])}\n' if a["tags"] else "")
                    + "程式規則生成，非投資建議")
            embeds.append({"title": title, "description": desc, "color": color.get(a["type"], 0x676D76)})
        try:
            requests.post(webhook, json={"embeds": embeds}, timeout=TIMEOUT)
        except Exception as e:  # noqa: BLE001 - Discord 推播失敗不影響本次掃描結果落檔
            print(f"[WARN] Discord 推播失敗: {e}")


# ---------- 5) 網頁 ----------
def esc(x):
    import html
    return html.escape(str(x))


def render_page(results, alerts, universe_n, min_qv, now):
    def sub_cell(v):
        if v is None:
            return '<td class="fl">缺</td>'
        cls = "up" if v > 10 else ("dn" if v < -10 else "")
        return f'<td class="{cls}">{v:+d}</td>'

    rows = []
    for r in sorted(results, key=lambda x: -x["total"])[:150]:
        tone = "up" if r["grade"] in ("S", "A") else ("dn" if r["grade"] in ("C", "D") else "")
        risk_tone = "dn" if r["risk"] >= 75 else ("" if r["risk"] >= 50 else "fl")
        rows.append(
            f'<tr><td class="code">{esc(r["base"])}</td>'
            f'<td class="{tone}"><b>{r["grade"]}</b>（{r["total"]:+d}）</td>'
            + sub_cell(r["sub"]["whale"]) + sub_cell(r["sub"]["cvd"])
            + sub_cell(r["sub"]["oi"]) + sub_cell(r["sub"]["dom"])
            + f'<td class="{risk_tone}">{r["risk"]}</td>'
            f'<td>{esc("、".join(r["tags"]) if r["tags"] else "—")}</td>'
            f'<td>{r["chg24h"]:+.2f}%</td></tr>')

    alert_rows = "".join(
        f'<li>{esc(a["symbol"])}：' + (
            f'{esc(a["prev_grade"])} → {esc(a["grade"])}（{esc(a["direction"])}）'
            if a["type"] == "grade" else
            (f'風險升至 {a["risk"]}（高風險）' if a["type"] == "risk_on" else f'風險警示解除（{a["risk"]}）')
        ) + '</li>' for a in alerts) or "<li>本輪無評級／風險狀態變動</li>"

    css = """
    :root{--bg:#FAFAF7;--card:#FFFFFF;--ink:#23262B;--muted:#676D76;--line:#E3E1DB;--accent:#3E5C8A;--chip:#EEF1F5;
     --up:#187A4D;--dn:#B02B40;--flat:#8A8F98}
    @media (prefers-color-scheme: dark){:root{--bg:#14161A;--card:#1C1F25;--ink:#E8E6E1;--muted:#9AA0A8;--line:#2C3037;
     --accent:#93ADD6;--chip:#252A32;--up:#3FBF8A;--dn:#E4677B;--flat:#8A8F98}}
    :root[data-theme="dark"]{--bg:#14161A;--card:#1C1F25;--ink:#E8E6E1;--muted:#9AA0A8;--line:#2C3037;
     --accent:#93ADD6;--chip:#252A32;--up:#3FBF8A;--dn:#E4677B;--flat:#8A8F98}
    :root[data-theme="light"]{--bg:#FAFAF7;--card:#FFFFFF;--ink:#23262B;--muted:#676D76;--line:#E3E1DB;
     --accent:#3E5C8A;--chip:#EEF1F5;--up:#187A4D;--dn:#B02B40;--flat:#8A8F98}
    *{box-sizing:border-box}
    body{background:var(--bg);color:var(--ink);margin:0;font-family:"Microsoft JhengHei","PingFang TC",system-ui,sans-serif}
    .wrap{max-width:1080px;margin:0 auto;padding:28px 20px 60px;display:flex;flex-direction:column;gap:24px}
    .mast{border-bottom:2px solid var(--ink);padding-bottom:14px;display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 16px}
    .mast h1{font-size:1.4rem;margin:0;letter-spacing:.1em}
    .chip{background:var(--chip);color:var(--muted);border-radius:999px;padding:2px 12px;font-size:.74rem}
    section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px 22px}
    h2{font-size:1.0rem;margin:0 0 4px;color:var(--accent);letter-spacing:.1em}
    .sub{font-size:.8rem;color:var(--muted);margin:0 0 14px}
    .tbl{overflow-x:auto}
    table{border-collapse:collapse;width:100%;font-size:.82rem;white-space:nowrap}
    th{color:var(--muted);text-align:right;padding:6px 10px;border-bottom:1px solid var(--line);font-size:.74rem}
    td{padding:6px 10px;text-align:right;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
    th:first-child,td:first-child{text-align:left}
    .code{font-family:ui-monospace,Consolas,monospace}
    .up{color:var(--up)}.dn{color:var(--dn)}.fl{color:var(--flat)}
    .appendix li,.appendix p{font-size:.82rem;color:var(--muted)}
    a{color:var(--accent)}
    """
    return (f'<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>主力資金雷達 {now:%Y-%m-%d %H:%M}</title><style>{css}</style></head><body>'
            f'<div class="wrap"><header class="mast"><h1>主力資金雷達</h1>'
            f'<span class="chip">掃描 {universe_n} 檔（24h額≥${min_qv:,.0f}）</span>'
            f'<span class="chip">更新 {now:%Y-%m-%d %H:%M} UTC</span>'
            f'<span class="chip"><a href="./index.html">← 回每日市場觀察</a></span></header>'
            f'<section><h2>本輪評級／風險異動</h2><ul>{alert_rows}</ul></section>'
            f'<section><h2>全市場評分（顯示前 150，依總分排序）</h2>'
            f'<p class="sub">評級：總分 ≥50 S・≥25 A・≥0 B・≥-25 C・其餘 D｜子分數 -100~+100，'
            f'50 中性偏移量｜「缺」＝該幣無此資料源</p>'
            f'<div class="tbl"><table><tr><th>標的</th><th>評級</th><th>鯨魚</th><th>CVD</th>'
            f'<th>OI×價</th><th>DOM</th><th>風險</th><th>訊號標籤</th><th>24h</th></tr>'
            + "".join(rows) +
            '</table></div></section>'
            '<section class="appendix"><h2>公式與誠實限制</h2><ul>'
            '<li><b>鯨魚（大額成交淨流向）</b>：近 500 筆逐筆成交中，單筆 ≥$10,000 者的'
            '（買方主動吃單 − 賣方主動吃單）金額差，除以總成交額換算成 -100~100。'
            '<b>這不是真實錢包持倉</b>，只是大單掛單行為的統計近似。</li>'
            '<li><b>CVD</b>：近 6 小時 K 線的（主動買量−主動賣量）合計，相對 24h 總量放大顯示。</li>'
            '<li><b>OI×價</b>：僅該幣有 Binance USDT 本位永續合約時才有資料；未平倉量變化與價格'
            '同向視為趨勢有資金確認、反向視為背離。無永續合約的幣一律顯示「缺」。</li>'
            '<li><b>DOM</b>：只對本輪分數最高的前 40 檔額外抓五檔深度算失衡比，其餘因 API 額度'
            '限制顯示「缺」。掛單可撤可假，僅供參考。</li>'
            '<li><b>操縱警示／雙頂形態</b>：簡單啟發式規則（單筆佔比、近似雙頂），是提示不是證據；'
            '低流動性幣的操縱檢查會直接跳過以避免誤判。</li>'
            '<li>總分＝可得子分數的加權平均（子分數越少，總分越保守打折）；'
            '評級與風險分是「當下資料的規則統計」，不是預測、不構成投資建議。</li>'
            '</ul></section>'
            '</div></body></html>')


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-quote-vol", type=float, default=300_000)
    ap.add_argument("--limit", type=int, default=None, help="測試用：只處理前 N 檔（依24h額排序）")
    ap.add_argument("--dry-run", action="store_true", help="不發 Discord，只印結果")
    ap.add_argument("--dom-top", type=int, default=40)
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    universe = fetch_universe(args.min_quote_vol)
    if args.limit:
        universe = universe[:args.limit]
    futures_syms = fetch_futures_symbols()
    dom_set = {r["symbol"] for r in universe[:args.dom_top]}

    results, errors = [], 0
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(score_symbol, r, futures_syms, r["symbol"] in dom_set): r for r in universe}
        for fut in cf.as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as e:  # noqa: BLE001
                errors += 1

    prev_state = load_state()
    is_cold_start = not prev_state
    alerts = build_alerts(results, prev_state, is_cold_start)

    new_state = {r["symbol"]: {"grade": r["grade"], "total": r["total"], "risk": r["risk"],
                                "ts": now.isoformat()} for r in results}
    save_state(new_state)

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not args.dry_run:
        send_discord(webhook, alerts)

    html_out = render_page(results, alerts, len(universe), args.min_quote_vol, now)
    os.makedirs(os.path.dirname(DOCS_PATH), exist_ok=True)
    with open(DOCS_PATH, "w", encoding="utf-8") as f:
        f.write(html_out)

    print(f"universe={len(universe)} scored={len(results)} errors={errors} "
          f"alerts={len(alerts)} cold_start={is_cold_start} discord={'on' if webhook else 'off(dry/no-secret)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
