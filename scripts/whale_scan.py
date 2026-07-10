# -*- coding: utf-8 -*-
"""主力資金雷達：全市場資金流向評分＋候選池追蹤＋Discord 異動通知。

== 涵蓋範圍（v2，2026-07-10 擴充）==
- 主要交易所：Binance 現貨 USDT（~264 檔，24h額≥$30萬）。
- 加開 Gate.io 現貨 USDT，只收 Binance 沒有的長尾幣種（~130 檔新增，
  含使用者參考範例中的 TAG 這類極小型迷因幣；Binance 完全沒有上架 TAG）。
- Hyperliquid：只有 ~231 檔主流永續合約，**完全不含** TAG/VANRY/RIF 這類
  超小型幣，只能給主流幣一個「未平倉/日量」擁擠度參考，對長尾幣種沒有幫助。
  這是免費 API 的硬限制，不是我們的篩選限制。

== 五大子分數（決定總分，來源依交易所而異）==
- 鯨魚（大額成交淨流向）：Binance 用 aggTrades（isBuyerMaker 推斷方向）；
  Gate.io 用 trades（side 欄位直接給方向，更可靠）。都是「大單掛單行為」
  的統計近似，**不是真實錢包持倉**。
- CVD：Binance 用 K 線 taker-buy 欄位；Gate.io 沒有這個欄位，改用近期
  逐筆成交方向加總近似，方法不同、意義相近。
- OI×價：僅 Binance 有永續合約資料時才有；Gate.io 尚未整合期貨，一律缺。
- DOM 市場深度：只對本輪分數最高的前 40 檔額外抓五檔深度。
- 操縱警示：簡單啟發式（單筆佔比過高），只在成交量夠厚時評估。

== 額外顯示（不計入總分，純參考）==
- HL 未平倉擁擠度：Hyperliquid 未平倉量／當日名目量，只有該幣在 HL 上架
  才有，多數長尾幣一律顯示「缺」。

== 候選池與追蹤 ==
- 候選池＝總分達 S/A 級。首次進入會記錄「入池時間／入池價」，之後每輪更新
  期間最高／最低價；跌出候選池後這些數字會凍結，直到下次重新進入才歸零重記。
- 只有「進出候選池」「候選池內 S↔A 升降」「風險分跨越 75」才推 Discord，
  一般 B/C/D 之間的日常波動不通知（見 2026-07-10 lessons：曾經因為沒有這條
  規則在 265 檔裡洗出 69 則通知）。

== 一切分數是規則統計，不是預測，不構成投資建議 ==
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import html
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
GATE_HOST = "https://api.gateio.ws/api/v4"
HL_HOST = "https://api.hyperliquid.xyz/info"
STABLE = {"USDC", "FDUSD", "TUSD", "DAI", "EUR", "USDP", "BUSD", "USD1", "EURI",
          "XUSD", "PAX", "GUSD", "USDD", "EURT", "RLUSD", "USDE", "USDY"}
LEV_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")
WHALE_TRADE_USD = 10_000
MANIP_MIN_NOTIONAL = 50_000
GRADE_BOUNDS = [(50, "S"), (25, "A"), (0, "B"), (-25, "C")]
NOTABLE_GRADES = {"S", "A"}
_GRADE_ORDER = ["S", "A", "B", "C", "D"]
TIMEOUT = 20


def esc(x):
    return html.escape(str(x))


def clamp(v, lo=-100, hi=100):
    return max(lo, min(hi, v))


def grade_of(total):
    for th, g in GRADE_BOUNDS:
        if total >= th:
            return g
    return "D"


def grade_rank(g):
    return _GRADE_ORDER.index(g)


# ---------- HTTP helpers ----------
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


def gget(path, params=None):
    r = requests.get(GATE_HOST + path, params=params, headers=H, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# ---------- 1) 全市場流動性篩選（Binance 為主，Gate.io 補長尾）----------
def fetch_binance_universe(min_qv):
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
            if qv >= min_qv:
                rows.append({"symbol": t["symbol"], "base": t["symbol"][:-4], "exch": "binance",
                             "close": float(t["lastPrice"]), "chg24h": float(t["priceChangePercent"]),
                             "qv": qv})
    return rows, {r["base"] for r in rows}


def fetch_gate_universe(min_qv, exclude_bases):
    tick = gget("/spot/tickers")
    rows = []
    for t in tick:
        cp = t["currency_pair"]
        if not cp.endswith("_USDT"):
            continue
        base = cp[:-5]
        if base in STABLE or base in exclude_bases:
            continue
        try:
            qv = float(t["quote_volume"] or 0)
            last = float(t["last"] or 0)
            chg = float(t["change_percentage"] or 0)
        except (TypeError, ValueError):
            continue
        if qv >= min_qv and last > 0:
            rows.append({"symbol": cp, "base": base, "exch": "gate",
                         "close": last, "chg24h": chg, "qv": qv})
    return rows


def fetch_futures_symbols():
    try:
        info = fget("/fapi/v1/exchangeInfo")
        return {s["symbol"] for s in info["symbols"]
                if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING"}
    except Exception:  # noqa: BLE001
        return set()


def fetch_hl_snapshot():
    """一次性抓 Hyperliquid 全部永續合約的未平倉/日量，回傳 {base: crowd_ratio}。
    僅涵蓋 ~231 檔主流永續合約，長尾迷因幣一律不在其中（見檔頭說明）。"""
    try:
        r = requests.post(HL_HOST, json={"type": "metaAndAssetCtxs"}, headers=H, timeout=TIMEOUT)
        r.raise_for_status()
        meta, ctxs = r.json()
        out = {}
        for a, c in zip(meta["universe"], ctxs):
            try:
                oi, vol, px = float(c["openInterest"]), float(c["dayNtlVlm"]), float(c["markPx"])
                if vol > 0:
                    out[a["name"]] = round(oi * px / vol, 3)  # OI 名目值 / 日量：擁擠度
            except (KeyError, ValueError, ZeroDivisionError):
                continue
        return out
    except Exception:  # noqa: BLE001 - 這是額外參考欄，失敗就整批缺
        return {}


# ---------- 2) 單一幣種指標：Binance ----------
def whale_flow_binance(symbol):
    trades = sget("/api/v3/aggTrades", {"symbol": symbol, "limit": 500})
    buy = sell = whale_buy = whale_sell = 0.0
    max_single = 0.0
    for t in trades:
        notional = float(t["p"]) * float(t["q"])
        if t["m"]:
            sell += notional
            if notional >= WHALE_TRADE_USD:
                whale_sell += notional
        else:
            buy += notional
            if notional >= WHALE_TRADE_USD:
                whale_buy += notional
        max_single = max(max_single, notional)
    total = buy + sell
    return whale_buy - whale_sell, total, (max_single / total if total else 0), len(trades)


def cvd_and_pullback_binance(symbol):
    kl = sget("/api/v3/klines", {"symbol": symbol, "interval": "1h", "limit": 24})
    cvd6, highs = 0.0, []
    for i, k in enumerate(kl):
        qv, taker_buy_qv = float(k[7]), float(k[10])
        if i >= len(kl) - 6:
            cvd6 += 2 * taker_buy_qv - qv
        highs.append(float(k[2]))
    last_close = float(kl[-1][4])
    recent_high = max(highs[-6:]) if len(highs) >= 6 else max(highs)
    pullback_pct = (recent_high - last_close) / recent_high * 100 if recent_high else 0
    double_top = False
    if len(highs) >= 10:
        h1_idx = max(range(len(highs) - 10, len(highs) - 5), key=lambda i: highs[i])
        h2_idx = max(range(len(highs) - 5, len(highs)), key=lambda i: highs[i])
        h1, h2 = highs[h1_idx], highs[h2_idx]
        if h1 > 0 and abs(h1 - h2) / h1 < 0.015 and last_close < min(h1, h2) * 0.97:
            double_top = True
    return cvd6, pullback_pct, double_top


def oi_and_ratio_binance(symbol):
    oi = fget("/futures/data/openInterestHist", {"symbol": symbol, "period": "1h", "limit": 6})
    ls = fget("/futures/data/topLongShortAccountRatio", {"symbol": symbol, "period": "1h", "limit": 1})
    oi_chg = ((float(oi[-1]["sumOpenInterestValue"]) / float(oi[0]["sumOpenInterestValue"])) - 1) * 100 \
        if len(oi) >= 2 else None
    ratio = float(ls[0]["longShortRatio"]) if ls else None
    return oi_chg, ratio


def dom_imbalance_binance(symbol):
    book = sget("/api/v3/depth", {"symbol": symbol, "limit": 1000})
    return _dom_from_book(book["bids"], book["asks"])


# ---------- 2b) 單一幣種指標：Gate.io（長尾幣種）----------
def whale_and_cvd_gate(cp):
    """Gate.io trades 有明確 side 欄位，一次抓同時算鯨魚淨流向與短期 CVD 近似。"""
    trades = gget("/spot/trades", {"currency_pair": cp, "limit": 500})
    buy = sell = whale_buy = whale_sell = 0.0
    max_single = 0.0
    for t in trades:
        notional = float(t["price"]) * float(t["amount"])
        if t["side"] == "sell":
            sell += notional
            if notional >= WHALE_TRADE_USD:
                whale_sell += notional
        else:
            buy += notional
            if notional >= WHALE_TRADE_USD:
                whale_buy += notional
        max_single = max(max_single, notional)
    total = buy + sell
    cvd_proxy = buy - sell  # 近 500 筆的淨主動買賣力道，近似 CVD
    return whale_buy - whale_sell, total, (max_single / total if total else 0), len(trades), cvd_proxy


def pullback_gate(cp):
    kl = gget("/spot/candlesticks", {"currency_pair": cp, "interval": "1h", "limit": 24})
    highs = [float(k[3]) for k in kl]
    last_close = float(kl[-1][2])
    recent_high = max(highs[-6:]) if len(highs) >= 6 else max(highs)
    pullback_pct = (recent_high - last_close) / recent_high * 100 if recent_high else 0
    double_top = False
    if len(highs) >= 10:
        h1_idx = max(range(len(highs) - 10, len(highs) - 5), key=lambda i: highs[i])
        h2_idx = max(range(len(highs) - 5, len(highs)), key=lambda i: highs[i])
        h1, h2 = highs[h1_idx], highs[h2_idx]
        if h1 > 0 and abs(h1 - h2) / h1 < 0.015 and last_close < min(h1, h2) * 0.97:
            double_top = True
    return pullback_pct, double_top


def dom_imbalance_gate(cp):
    book = gget("/spot/order_book", {"currency_pair": cp, "limit": 100})
    bids = [(float(p), float(q)) for p, q in book["bids"]]
    asks = [(float(p), float(q)) for p, q in book["asks"]]
    return _dom_from_book(bids, asks)


def _dom_from_book(bids, asks):
    bids = [(float(p), float(q)) for p, q in bids]
    asks = [(float(p), float(q)) for p, q in asks]
    if not bids or not asks:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2
    lo, hi = mid * 0.99, mid * 1.01
    b = sum(p * q for p, q in bids if p >= lo)
    a = sum(p * q for p, q in asks if p <= hi)
    return b / (a + b) if a + b else None


# ---------- 3) 單幣綜合評分 ----------
def score_symbol(row, futures_syms, hl_map, do_dom):
    symbol, base, qv, exch = row["symbol"], row["base"], row["qv"], row["exch"]
    sub = {"whale": None, "cvd": None, "oi": None, "dom": None, "manip": 0}
    tags = []
    pullback, double_top = None, False

    try:
        if exch == "binance":
            net, total_notional, max_ratio, ntr = whale_flow_binance(symbol)
        else:
            net, total_notional, max_ratio, ntr, cvd_raw = whale_and_cvd_gate(symbol)
            if qv > 0:
                sub["cvd"] = clamp(round(cvd_raw / qv * 300))
        if total_notional > 0:
            sub["whale"] = clamp(round(net / total_notional * 100))
        if total_notional >= MANIP_MIN_NOTIONAL and max_ratio > 0.35 and ntr >= 5:
            tags.append("單筆成交佔比異常")
            sub["manip"] = -15
    except Exception:  # noqa: BLE001
        pass

    if exch == "binance":
        try:
            cvd6, pullback, double_top = cvd_and_pullback_binance(symbol)
            if qv > 0:
                sub["cvd"] = clamp(round(cvd6 / qv * 300))
            if double_top:
                tags.append("雙頂形態")
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            pullback, double_top = pullback_gate(symbol)
            if double_top:
                tags.append("雙頂形態")
        except Exception:  # noqa: BLE001
            pass

    if exch == "binance" and symbol in futures_syms:
        try:
            oi_chg, ratio = oi_and_ratio_binance(symbol)
            if oi_chg is not None:
                same_dir = (oi_chg > 0 and row["chg24h"] > 0) or (oi_chg < 0 and row["chg24h"] < 0)
                mag = min(abs(oi_chg), 20) / 20 * 40
                sub["oi"] = round(mag if same_dir else -mag) if oi_chg else 0
            if ratio is not None and ratio >= 2.5:
                tags.append("散戶多單擁擠")
        except Exception:  # noqa: BLE001
            pass

    if do_dom:
        try:
            imb = dom_imbalance_binance(symbol) if exch == "binance" else dom_imbalance_gate(symbol)
            if imb is not None:
                sub["dom"] = clamp(round((imb - 0.5) * 200))
        except Exception:  # noqa: BLE001
            pass

    hl_crowd = hl_map.get(base)  # 僅參考，不計入總分

    core = {k: v for k, v in sub.items() if k != "manip" and v is not None}
    total = round(sum(core.values()) / len(core) * (1 if len(core) >= 2 else 0.6)) if core else 0
    total = clamp(total + sub["manip"])

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
    if sub["manip"] < 0:
        risk += 15
    risk = clamp(risk, 0, 100)

    return {"symbol": symbol, "base": base, "exch": exch, "close": row["close"],
            "chg24h": row["chg24h"], "qv": qv, "sub": sub, "hl_crowd": hl_crowd,
            "total": total, "grade": grade_of(total), "risk": risk, "tags": tags}


def resonance_tags(r):
    """共振訊號：從既有子分數推導的描述性標籤，純展示用，不影響總分。"""
    tags = []
    whale, cvd, oi = r["sub"]["whale"], r["sub"]["cvd"], r["sub"]["oi"]
    if whale is not None and cvd is not None and whale > 20 and cvd > 20:
        tags.append("強動能")
    if oi is not None and oi > 20:
        tags.append("OI強勢")
    elif (whale is not None and whale > 20) or (cvd is not None and cvd > 20):
        if oi is None:
            tags.append("純動能")
    if whale is not None and 0 < whale <= 20 and abs(r["chg24h"]) < 3:
        tags.append("蓄勢")
    return tags


def action_suggestion(alert, r):
    """建議行動：規則生成的操作語句，非投資建議，僅供參考。"""
    grade, direction = r["grade"], alert.get("direction", "")
    if alert["type"] == "risk_on":
        return "風險分觸頂：建議減碼／緊止損，觀察後續是否止穩。"
    if alert["type"] == "risk_off":
        return "風險警示解除：可觀察是否重新站穩，暫不建議追價。"
    if direction == "跌出候選池":
        return "資金流轉弱：建議降低倉位或觀望，等待重新確認動能。"
    if grade == "S":
        return "升 S 級（強）：資金流最集中，若無背離可續抱，仍需設止損。"
    if grade == "A" and direction == "進入候選池":
        return "升 A 初期：可少量試倉（2-5%），止損設在入池價下方，觀察是否延續。"
    if grade == "A":
        return "候選池內續強：可維持既有倉位，追蹤是否升至 S。"
    return "評級變動，建議先觀察一輪確認方向，不建議立即動作。"


# ---------- 4) 狀態比對／候選池追蹤 ----------
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
            continue
        old_grade, new_grade = old.get("grade"), r["grade"]
        old_notable, new_notable = old_grade in NOTABLE_GRADES, new_grade in NOTABLE_GRADES
        if old_grade != new_grade and (old_notable or new_notable):
            if new_notable and not old_notable:
                direction = "進入候選池"
            elif old_notable and not new_notable:
                direction = "跌出候選池"
            else:
                direction = "升級" if grade_rank(new_grade) < grade_rank(old_grade) else "降級"
            alerts.append({"type": "grade", "direction": direction, **r, "prev_grade": old_grade})
        was_risky = old.get("risk", 0) >= 75
        if r["risk"] >= 75 and not was_risky:
            alerts.append({"type": "risk_on", **r})
        elif was_risky and r["risk"] < 75:
            alerts.append({"type": "risk_off", **r})
    return alerts


def update_tracking(results, prev_state, alerted_symbols, now_iso):
    """維護候選池追蹤：入池時間/價、期間高低、通知次數。跌出後凍結直到重新入池。"""
    new_state = {}
    for r in results:
        sym = r["symbol"]
        old = prev_state.get(sym, {})
        in_pool = r["grade"] in NOTABLE_GRADES
        was_in_pool = old.get("grade") in NOTABLE_GRADES
        entry = {"grade": r["grade"], "total": r["total"], "risk": r["risk"], "ts": now_iso}
        if in_pool and not was_in_pool:
            entry.update(pool_entry_ts=now_iso, pool_entry_price=r["close"],
                         high_since=r["close"], low_since=r["close"], alert_count=0)
        elif in_pool and was_in_pool:
            entry.update(pool_entry_ts=old.get("pool_entry_ts", now_iso),
                         pool_entry_price=old.get("pool_entry_price", r["close"]),
                         high_since=max(old.get("high_since", r["close"]), r["close"]),
                         low_since=min(old.get("low_since", r["close"]), r["close"]),
                         alert_count=old.get("alert_count", 0))
        else:
            # 跌出或本來就不在池內：凍結既有追蹤紀錄（若有），不重置
            for k in ("pool_entry_ts", "pool_entry_price", "high_since", "low_since", "alert_count"):
                if k in old:
                    entry[k] = old[k]
        if sym in alerted_symbols:
            entry["alert_count"] = entry.get("alert_count", 0) + 1
        new_state[sym] = entry
    return new_state


# ---------- 5) Discord ----------
def bar(v, width=10):
    v = clamp(v, -50, 50)
    filled = round((v + 50) / 100 * width)
    return "▓" * filled + "░" * (width - filled)


def send_discord(webhook, alerts):
    if not webhook or not alerts:
        return
    color = {"grade": 0x3E5C8A, "risk_on": 0xC7364C, "risk_off": 0x8A8F98}
    for i in range(0, len(alerts), 10):
        embeds = []
        for a in alerts[i:i + 10]:
            sub = a["sub"]
            if a["type"] == "grade":
                icon = "🟢" if a["direction"] == "進入候選池" else ("🟠" if a["direction"] == "跌出候選池" else "🟡")
                title = f'{icon} {a["base"]}．主力評分 {a["prev_grade"]}→{a["grade"]}（{a["direction"]}）'
            elif a["type"] == "risk_on":
                title = f'🔴 {a["base"]}．風險警示：評分 {a["risk"]}（高風險）'
            else:
                title = f'⚪ {a["base"]}．風險警示解除（{a["risk"]}）'
            track = ""
            pe_ts, pe_px = a.get("pool_entry_ts"), a.get("pool_entry_price")
            if pe_ts and pe_px:
                hi, lo = a.get("high_since", pe_px), a.get("low_since", pe_px)
                hi_pct, lo_pct = (hi / pe_px - 1) * 100, (lo / pe_px - 1) * 100
                track = (f'\n入池 {pe_ts[5:16]}・${pe_px:g}\n'
                        f'最高 {hi_pct:+.1f}%　最低 {lo_pct:+.1f}%　異動 {a.get("alert_count", 1)} 次')
            tag_line = f'\n標籤：{"、".join(a["tags"])}' if a.get("tags") else ""
            desc = (f'現價 ${a["close"]:g}・{a["chg24h"]:+.1f}%\n'
                    f'總分 [{bar(a["total"])}] {a["total"]:+d}\n'
                    f'鯨魚 {sub["whale"] if sub["whale"] is not None else "缺"}　'
                    f'CVD {sub["cvd"] if sub["cvd"] is not None else "缺"}　'
                    f'OI×價 {sub["oi"] if sub["oi"] is not None else "缺"}　'
                    f'DOM {sub["dom"] if sub["dom"] is not None else "缺"}\n'
                    f'風險 {a["risk"]}　交易所 {a["exch"]}'
                    + track + tag_line
                    + f'\n➡ {action_suggestion(a, a)}'
                    + "\n程式規則生成，非投資建議")
            embeds.append({"title": title, "description": desc, "color": color.get(a["type"], 0x676D76)})
        try:
            requests.post(webhook, json={"embeds": embeds}, timeout=TIMEOUT)
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] Discord 推播失敗: {e}")


# ---------- 6) 網頁 ----------
def card_html(r):
    sub = r["sub"]
    tone = "up" if r["grade"] in ("S", "A") else ("dn" if r["risk"] >= 75 else "")

    def s(v):
        return "缺" if v is None else f"{v:+d}"

    track = ""
    if r.get("pool_entry_ts") and r.get("pool_entry_price"):
        pe_px, hi, lo = r["pool_entry_price"], r.get("high_since", r["close"]), r.get("low_since", r["close"])
        track = (f'<div class="track">入池 {esc(r["pool_entry_ts"][5:16])}・${pe_px:g}　'
                 f'最高 {(hi/pe_px-1)*100:+.1f}%　最低 {(lo/pe_px-1)*100:+.1f}%　'
                 f'異動 {r.get("alert_count", 0)} 次</div>')
    tags = "、".join(resonance_tags(r) + r["tags"]) or "—"
    return (f'<div class="card {tone}"><div class="chead"><b class="sym">{esc(r["base"])}</b>'
            f'<span class="grade {tone}">{r["grade"]}</span>'
            f'<span class="px">${r["close"]:g}<span class="{"up" if r["chg24h"]>=0 else "dn"}">'
            f'{r["chg24h"]:+.1f}%</span></span></div>'
            f'<div class="bar"><div class="fill {tone}" style="width:{clamp((r["total"]+50),0,100)}%"></div>'
            f'<span>{r["total"]:+d}</span></div>'
            f'<div class="grid"><span>鯨魚 {s(sub["whale"])}</span><span>CVD {s(sub["cvd"])}</span>'
            f'<span>OI×價 {s(sub["oi"])}</span><span>DOM {s(sub["dom"])}</span>'
            f'<span>HL擁擠 {r["hl_crowd"] if r["hl_crowd"] is not None else "缺"}</span>'
            f'<span>操縱 {sub["manip"]:+d}</span></div>'
            + track +
            f'<div class="tags">{esc(tags)}</div>'
            f'<div class="exch">來源：{esc(r["exch"])}</div></div>')


def render_page(results, alerts, universe_n, min_qv, now, exch_counts):
    pool = [r for r in results if r["grade"] in NOTABLE_GRADES or r["risk"] >= 75]
    pool.sort(key=lambda r: -r["total"])
    cards = "".join(card_html(r) for r in pool) or '<p class="sub">目前無候選池成員或高風險標的。</p>'

    def sub_cell(v):
        if v is None:
            return '<td class="fl">缺</td>'
        cls = "up" if v > 10 else ("dn" if v < -10 else "")
        return f'<td class="{cls}">{v:+d}</td>'

    rows = []
    for r in sorted(results, key=lambda x: -x["total"])[:200]:
        tone = "up" if r["grade"] in ("S", "A") else ("dn" if r["grade"] in ("C", "D") else "")
        risk_tone = "dn" if r["risk"] >= 75 else ("" if r["risk"] >= 50 else "fl")
        rows.append(
            f'<tr><td class="code">{esc(r["base"])}</td><td>{esc(r["exch"])}</td>'
            f'<td class="{tone}"><b>{r["grade"]}</b>（{r["total"]:+d}）</td>'
            + sub_cell(r["sub"]["whale"]) + sub_cell(r["sub"]["cvd"])
            + sub_cell(r["sub"]["oi"]) + sub_cell(r["sub"]["dom"])
            + f'<td class="{risk_tone}">{r["risk"]}</td>'
            f'<td>{esc("、".join(r["tags"]) if r["tags"] else "—")}</td>'
            f'<td>{r["chg24h"]:+.2f}%</td></tr>')

    alert_rows = "".join(
        f'<li>{esc(a["base"])}：' + (
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
    .wrap{max-width:1160px;margin:0 auto;padding:28px 20px 60px;display:flex;flex-direction:column;gap:24px}
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
    .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}
    .card{border:1px solid var(--line);border-radius:10px;padding:14px 16px;background:var(--bg)}
    .card.up{border-color:var(--up)}.card.dn{border-color:var(--dn)}
    .chead{display:flex;align-items:baseline;gap:8px;margin-bottom:6px}
    .chead .sym{font-size:1.05rem}
    .chead .grade{font-weight:700;padding:1px 8px;border-radius:6px;background:var(--chip)}
    .chead .grade.up{color:var(--up)}.chead .grade.dn{color:var(--dn)}
    .chead .px{margin-left:auto;font-size:.82rem;font-variant-numeric:tabular-nums}
    .card .bar{position:relative;background:var(--chip);border-radius:6px;height:16px;margin:6px 0;overflow:hidden}
    .card .bar .fill{position:absolute;left:0;top:0;bottom:0;background:var(--flat)}
    .card .bar .fill.up{background:var(--up)}.card .bar .fill.dn{background:var(--dn)}
    .card .bar span{position:relative;font-size:.7rem;line-height:16px;padding-left:6px;font-variant-numeric:tabular-nums}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:2px 10px;font-size:.78rem;color:var(--muted);margin:6px 0}
    .track{font-size:.74rem;color:var(--muted);border-top:1px dashed var(--line);padding-top:6px;margin-top:6px}
    .tags{font-size:.76rem;margin-top:4px}
    .exch{font-size:.7rem;color:var(--muted);margin-top:4px}
    """
    return (f'<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>主力資金雷達 {now:%Y-%m-%d %H:%M}</title><style>{css}</style></head><body>'
            f'<div class="wrap"><header class="mast"><h1>主力資金雷達</h1>'
            f'<span class="chip">掃描 {universe_n} 檔（Binance {exch_counts.get("binance",0)}・'
            f'Gate.io {exch_counts.get("gate",0)}・24h額≥${min_qv:,.0f}）</span>'
            f'<span class="chip">更新 {now:%Y-%m-%d %H:%M} UTC</span>'
            f'<span class="chip"><a href="./index.html">← 回每日市場觀察</a></span></header>'
            f'<section><h2>候選池（S/A 級或高風險）</h2>'
            f'<p class="sub">下方卡片即時反映目前在池內的標的；入池追蹤（時間/價格/期間高低）持續累積</p>'
            f'<div class="cards">{cards}</div></section>'
            f'<section><h2>本輪評級／風險異動</h2><ul>{alert_rows}</ul></section>'
            f'<section><h2>全市場評分（顯示前 200，依總分排序）</h2>'
            f'<p class="sub">評級：總分 ≥50 S・≥25 A・≥0 B・≥-25 C・其餘 D｜「缺」＝該幣無此資料源</p>'
            f'<div class="tbl"><table><tr><th>標的</th><th>來源</th><th>評級</th><th>鯨魚</th><th>CVD</th>'
            f'<th>OI×價</th><th>DOM</th><th>風險</th><th>訊號標籤</th><th>24h</th></tr>'
            + "".join(rows) +
            '</table></div></section>'
            '<section class="appendix"><h2>公式與誠實限制</h2><ul>'
            '<li><b>涵蓋範圍</b>：Binance 現貨 USDT（主要）＋ Gate.io 現貨 USDT（只收 Binance 沒有的長尾幣，'
            '約 130 檔，含 TAG 這類極小型迷因幣）。兩者都套 24h 成交額 ≥$30萬 的流動性門檻。</li>'
            '<li><b>鯨魚（大額成交淨流向）</b>：近 500 筆逐筆成交中單筆 ≥$10,000 者的買賣淨額。'
            'Binance 用 isBuyerMaker 推斷方向；Gate.io 用其 trades 的明確 side 欄位。'
            '<b>這不是真實錢包持倉</b>，只是大單掛單行為的統計近似。</li>'
            '<li><b>CVD</b>：Binance 用近 6 小時 K 線的主動買賣量差；Gate.io 沒有這個欄位，'
            '改用近 500 筆成交的淨方向近似，兩者方法不同、意義相近。</li>'
            '<li><b>OI×價</b>：僅 Binance 有永續合約的幣才有資料；Gate.io 尚未整合期貨資料，一律缺。</li>'
            '<li><b>HL 未平倉擁擠度</b>：Hyperliquid 未平倉名目值／當日成交量，僅供參考、不計入總分。'
            'Hyperliquid 只有約 231 檔主流永續合約，<b>完全不含 TAG/VANRY/RIF 這類長尾迷因幣</b>，'
            '對這類標的這欄必定顯示「缺」——這是免費 API 的硬限制，不是我們刻意不做。</li>'
            '<li><b>DOM</b>：只對本輪分數最高的前 40 檔額外抓深度算失衡比，其餘顯示「缺」。'
            '掛單可撤可假，僅供參考。</li>'
            '<li><b>操縱警示／雙頂形態</b>：簡單啟發式規則，是提示不是證據；低流動性幣的操縱檢查會跳過。</li>'
            '<li><b>建議行動</b>：規則生成的操作語句（如「試倉 2-5%」），是既定規則輸出，'
            '<b>不是投資建議</b>，不構成任何形式的個人化建議。</li>'
            '<li>總分＝可得子分數的加權平均（子分數越少，總分越保守打折）；'
            '評級與風險分是「當下資料的規則統計」，不是預測，也未經回測驗證預測力。</li>'
            '</ul></section>'
            '</div></body></html>')


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-quote-vol", type=float, default=300_000)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dom-top", type=int, default=40)
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    b_rows, b_bases = fetch_binance_universe(args.min_quote_vol)
    try:
        g_rows = fetch_gate_universe(args.min_quote_vol, b_bases)
    except Exception as e:  # noqa: BLE001 - Gate 失敗不影響 Binance 主流程
        g_rows = []
        print(f"[WARN] Gate.io 掃描失敗，本輪只用 Binance: {e}")
    universe = sorted(b_rows + g_rows, key=lambda r: -r["qv"])
    if args.limit:
        universe = universe[:args.limit]
    exch_counts = {"binance": sum(1 for r in universe if r["exch"] == "binance"),
                   "gate": sum(1 for r in universe if r["exch"] == "gate")}

    futures_syms = fetch_futures_symbols()
    hl_map = fetch_hl_snapshot()
    dom_set = {r["symbol"] for r in universe[:args.dom_top]}

    results, errors = [], 0
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(score_symbol, r, futures_syms, hl_map, r["symbol"] in dom_set): r for r in universe}
        for fut in cf.as_completed(futs):
            try:
                results.append(fut.result())
            except Exception:  # noqa: BLE001
                errors += 1

    prev_state = load_state()
    is_cold_start = not prev_state
    alerts = build_alerts(results, prev_state, is_cold_start)
    alerted_symbols = {a["symbol"] for a in alerts}

    # 把追蹤資訊（入池時間/價、期間高低）附加回 alert 物件，供 Discord/頁面顯示
    tracking_preview = update_tracking(results, prev_state, alerted_symbols, now.isoformat())
    for a in alerts:
        a.update({k: v for k, v in tracking_preview[a["symbol"]].items()
                 if k in ("pool_entry_ts", "pool_entry_price", "high_since", "low_since", "alert_count")})
    for r in results:
        r.update({k: v for k, v in tracking_preview[r["symbol"]].items()
                 if k in ("pool_entry_ts", "pool_entry_price", "high_since", "low_since", "alert_count")})

    save_state(tracking_preview)

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not args.dry_run:
        send_discord(webhook, alerts)

    html_out = render_page(results, alerts, len(universe), args.min_quote_vol, now, exch_counts)
    os.makedirs(os.path.dirname(DOCS_PATH), exist_ok=True)
    with open(DOCS_PATH, "w", encoding="utf-8") as f:
        f.write(html_out)

    print(f"universe={len(universe)}(binance={exch_counts['binance']},gate={exch_counts['gate']}) "
          f"scored={len(results)} errors={errors} alerts={len(alerts)} cold_start={is_cold_start} "
          f"hl_symbols={len(hl_map)} discord={'on' if webhook else 'off(dry/no-secret)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
