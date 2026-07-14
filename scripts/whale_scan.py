# -*- coding: utf-8 -*-
"""主力資金雷達：全市場資金流向評分＋候選池追蹤＋Discord 異動通知。

== 涵蓋範圍（v2，2026-07-10 擴充）==
- 主要交易所：Binance 現貨 USDT（~264 檔，24h額≥$30萬）。
- 加開 Gate.io 現貨 USDT，只收 Binance 沒有的長尾幣種（~130 檔新增，
  含使用者參考範例中的 TAG 這類極小型迷因幣；Binance 完全沒有上架 TAG）。
- Hyperliquid：只有 ~231 檔主流永續合約，**完全不含** TAG/VANRY/RIF 這類
  超小型幣，只能給主流幣一個「未平倉/日量」擁擠度參考，對長尾幣種沒有幫助。
  這是免費 API 的硬限制，不是我們的篩選限制。

== 七大子分數（決定總分，來源依交易所而異；v4 新增「技術」，2026-07-13 新增「資金費率」）==
- 鯨魚（大額成交淨流向）：Binance 用 aggTrades（isBuyerMaker 推斷方向）；
  Gate.io 用 trades（side 欄位直接給方向，更可靠）。都是「大單掛單行為」
  的統計近似，**不是真實錢包持倉**。門檻依 24h 成交額分層（BTC/ETH 等
  高流動性幣門檻遠高於長尾小幣，見 whale_threshold()），不是齊頭式固定值。
- CVD：Binance 用 K 線 taker-buy 欄位；Gate.io 沒有這個欄位，改用近期
  逐筆成交方向加總近似，方法不同、意義相近。
- OI×價：僅 Binance 有永續合約資料時才有；Gate.io 尚未整合期貨，一律缺。
- DOM 市場深度：只對本輪分數最高的前 40 檔抓訂單簿，分數乘上「簿深佔 24h 量
  比例」信心係數（見 score_dom()）。2026-07-14 起加三層防偽（文獻依據見
  docs/orderbook-quant-research.md）：(a) 一輪 3 張快照間隔 4 秒取中位數，
  殺瞬時假單與報價抖動；(b) 簿失衡方向與 taker 成交流（cvd）矛盾時 ×0.4——
  掛單可撤可假、成交不可逆；(c) 方向與上一輪相反時 ×0.5——真實簿壓持續數輪，
  spoofing 掛單活不久。誠實定位：靜態簿失衡的預測力在文獻上集中於 tick 級，
  這裡只當「流動性/確認變數」，權重不高於其他子分數。
- 技術：均線/RSI/MACD/KD/布林/量比六個常見指標的量化合成分，2026-07-13 新增
  時間序列動能(TSMOM)、波動率狀態(ATR百分位)兩項，並用 ADX 當「趨勢類指標
  (MA/MACD) 現在可不可信」的動態權重濾網（ta_scoring.py），跟前面幾項是完全
  不同的資料來源（K線 vs 逐筆成交/合約），互為獨立驗證。
- 資金費率（2026-07-13 新增，僅 Binance 永續合約幣種）：用該幣自己過去約 20 天
  的資金費率分布做 z-score，費率極端偏多（多頭擁擠）視為反轉風險給負分，極端
  偏空反之（funding_scoring.py）。這是「逆向」訊號，跟前面幾項「同向」動能類
  分數方向定義相反，只是加進同一個簡單平均，正負號已經對齊過。
- 操縱警示：簡單啟發式（單筆佔比過高），只在成交量夠厚時評估。
- 指標共振加成（2026-07-14 新增）：≥3 個獨立來源子分數同方向達門檻時，
  總分額外 ±6~18（見 resonance_bonus()）——簡單平均會稀釋「多源同向」的證據力，
  共振加成把這個資訊補回來，但上限壓低、只做臨門一腳。

== 額外顯示（不計入總分，純參考）==
- HL 未平倉擁擠度：Hyperliquid 未平倉量／當日名目量，只有該幣在 HL 上架
  才有，多數長尾幣一律顯示「缺」。

== 候選池與追蹤（v5 新增空方，對稱多方）==
- 多方候選池＝總分達 S/A 級（建議做多）；空方候選池＝總分 < -25 的 D 級
  （建議做空，2026-07-12 新增，之前只有多方）。首次進入記錄「入池時間／入池價／
  方向」，之後每輪更新期間最高／最低價；跌出候選池後這些數字會凍結，直到
  下次重新進入才歸零重記；多空方向切換視為新一輪（重置追蹤數字，不沿用）。
- Discord 通知分三類：**機會**＝首次進入任一候選池（唯一會明講「建議做多／
  做空」的類型）；**異動**＝候選池內升降級、跌出候選池（純狀態追蹤）；
  **風險**＝反轉/崩跌警示（風險分跨越 75），跟候選池是平行系統。
  一般 B/C 之間的日常波動不通知（見 2026-07-10 lessons：曾經因為沒有這條
  規則在 265 檔裡洗出 69 則通知）。

== 推送紀錄與績效（v4 新增，data/signal_log.json）==
- 每次進入候選池開一筆歷史紀錄，跌出時結案，記錄推送價/結案價/期間高低/
  推送次數，事後對帳「上漲有效」還是「反向走跌」。跟上面的候選池追蹤是
  同一組進出條件，但這裡是 append-only 歷史，不會被覆蓋。
- **這是唯一能誠實驗證整套評分系統有沒有預測力的地方**：長期勝率若接近或
  低於 50%，代表系統沒用，該檢討公式，不是繼續加指標（見附錄）。

== 一切分數是規則統計，不是預測，不構成投資建議 ==
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import html
import json
import os
import re
import statistics
import sys
import time

import requests

import ta_scoring as ta
import funding_scoring as fs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "data", "whale_state.json")
MAJORS_PATH = os.path.join(ROOT, "data", "majors_history.json")
MAJORS = ["BTC", "ETH", "SOL", "BNB", "XRP"]  # 主流幣資金強度專區固定追蹤這幾檔
MAJORS_HISTORY_LEN = 24  # 5 分鐘一輪，24 筆＝近 2 小時基準
SIGNAL_LOG_PATH = os.path.join(ROOT, "data", "signal_log.json")
DIGEST_STATE_PATH = os.path.join(ROOT, "data", "digest_state.json")
DIGEST_INTERVAL_HOURS = float(os.environ.get("DIGEST_INTERVAL_HOURS", 4))
SIGNAL_LOG_MAX = 300  # 超過上限先丟最舊的「已結案」紀錄，開放中的不丟
DOCS_PATH = os.path.join(ROOT, "docs", "whales.html")

H = {"User-Agent": "Mozilla/5.0"}
SPOT_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]
FAPI_HOST = "https://fapi.binance.com"
GATE_HOST = "https://api.gateio.ws/api/v4"
HL_HOST = "https://api.hyperliquid.xyz/info"
STABLE = {"USDC", "FDUSD", "TUSD", "DAI", "EUR", "USDP", "BUSD", "USD1", "EURI",
          "XUSD", "PAX", "GUSD", "USDD", "EURT", "RLUSD", "USDE", "USDY",
          "BFUSD", "USDS", "U"}
LEV_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")  # Binance 槓桿代幣後綴
GATE_LEV_RE = re.compile(r"\d[LS]$")  # Gate.io 槓桿代幣命名（如 XRP3L/BTC5S），364 檔實測存在
# 這種代幣機制上本來就會有劇烈量能波動（槓桿再平衡），不是真的資金流向訊號，
# 之前漏篩導致主力雷達跟即時警報都被洗版（2026-07-13 從即時警報噪音抓到）。
# 鯨魚門檻改成流動性分層：固定 $10,000 對 BTC（日均量數十億）根本是雜訊，
# 對一個 $30 萬量的長尾幣卻可能大到永遠觸發不了。門檻依 24h 成交額分層，
# 讓「大額成交」在每個流動性量級都代表真正的異常，不是齊頭式假平等。
WHALE_TIERS = [(500_000_000, 200_000), (100_000_000, 75_000),
              (20_000_000, 25_000), (2_000_000, 8_000)]
WHALE_TRADE_FLOOR = 3_000


def whale_threshold(qv_24h):
    for qv_min, th in WHALE_TIERS:
        if qv_24h >= qv_min:
            return th
    return WHALE_TRADE_FLOOR


MANIP_MIN_NOTIONAL = 50_000
DOM_CONF_FULL_RATIO = 0.02  # ±1% 簿深佔 24h 成交額達此比例，DOM 分數才給滿信心權重
GRADE_BOUNDS = [(50, "S"), (25, "A"), (0, "B"), (-25, "C")]
LONG_GRADES = {"S", "A"}   # 多方機會候選池（做多）
SHORT_GRADES = {"D"}       # 空方機會候選池（做空）——跟多方對稱新增，之前完全沒有
POOL_GRADES = LONG_GRADES | SHORT_GRADES
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


def direction_of(grade):
    """做多／做空／None（不在任何機會候選池）。空方只有 D 一級，沒有多方 S/A
    那樣的兩段分級——GRADE_BOUNDS 本來就沒有對稱切出「強空/普通空」兩層，
    這是誠實的限制，不是假裝對稱（見附錄）。"""
    if grade in LONG_GRADES:
        return "long"
    if grade in SHORT_GRADES:
        return "short"
    return None


def grade_class(g):
    """S/A/B/C/D 各自一個 CSS class（"g"+字母），用同一個 --up/--dn 色相但不同飽和度/
    粗細做出五級視覺層次——S 跟 A 才不會長得一樣分不出來。"""
    return f"g{g}" if g in _GRADE_ORDER else ""


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
        if base in STABLE or base in exclude_bases or GATE_LEV_RE.search(base):
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


def fetch_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", headers=H, timeout=TIMEOUT)
        r.raise_for_status()
        return int(r.json()["data"][0]["value"])
    except Exception:  # noqa: BLE001
        return None


# ---------- 2) 單一幣種指標：Binance ----------
def whale_flow_binance(symbol, threshold):
    trades = sget("/api/v3/aggTrades", {"symbol": symbol, "limit": 500})
    buy = sell = whale_buy = whale_sell = 0.0
    max_single = 0.0
    for t in trades:
        notional = float(t["p"]) * float(t["q"])
        if t["m"]:
            sell += notional
            if notional >= threshold:
                whale_sell += notional
        else:
            buy += notional
            if notional >= threshold:
                whale_buy += notional
        max_single = max(max_single, notional)
    total = buy + sell
    return whale_buy - whale_sell, total, (max_single / total if total else 0), len(trades)


def binance_kline_metrics(symbol):
    """一次抓 K 線，同時供 CVD/雙頂偵測與 ta_scoring 技術面分析使用（避免重複打 API）。
    130 根才夠 ta_scoring 新增的 TSMOM/波動率狀態（需要 n=20+lookback=100+1=121 根）；
    舊的 MA60/MACD(26+9)/布林(20) 需求遠低於這個數字，一起滿足。"""
    kl = sget("/api/v3/klines", {"symbol": symbol, "interval": "1h", "limit": 130})
    highs = [float(k[2]) for k in kl]
    lows = [float(k[3]) for k in kl]
    closes = [float(k[4]) for k in kl]
    volumes = [float(k[5]) for k in kl]
    cvd6 = 0.0
    for i, k in enumerate(kl):
        qv, taker_buy_qv = float(k[7]), float(k[10])
        if i >= len(kl) - 6:
            cvd6 += 2 * taker_buy_qv - qv
    last_close = closes[-1]
    recent_high = max(highs[-6:]) if len(highs) >= 6 else max(highs)
    pullback_pct = (recent_high - last_close) / recent_high * 100 if recent_high else 0
    double_top = False
    if len(highs) >= 10:
        h1_idx = max(range(len(highs) - 10, len(highs) - 5), key=lambda i: highs[i])
        h2_idx = max(range(len(highs) - 5, len(highs)), key=lambda i: highs[i])
        h1, h2 = highs[h1_idx], highs[h2_idx]
        if h1 > 0 and abs(h1 - h2) / h1 < 0.015 and last_close < min(h1, h2) * 0.97:
            double_top = True
    return cvd6, pullback_pct, double_top, (highs, lows, closes, volumes)


def oi_and_ratio_binance(symbol):
    oi = fget("/futures/data/openInterestHist", {"symbol": symbol, "period": "1h", "limit": 6})
    ls = fget("/futures/data/topLongShortAccountRatio", {"symbol": symbol, "period": "1h", "limit": 1})
    oi_chg = ((float(oi[-1]["sumOpenInterestValue"]) / float(oi[0]["sumOpenInterestValue"])) - 1) * 100 \
        if len(oi) >= 2 else None
    ratio = float(ls[0]["longShortRatio"]) if ls else None
    return oi_chg, ratio


def fetch_funding_history(symbol, limit=60):
    """資金費率結算約每 8 小時一次，limit=60 約 20 天歷史，供 funding_scoring.py
    的 z-score 當基準分布用。回傳 (最新一筆費率, 不含最新那筆的歷史 list)。"""
    data = fget("/fapi/v1/fundingRate", {"symbol": symbol, "limit": limit})
    if not data:
        return None, []
    rates = [float(d["fundingRate"]) for d in data]
    return rates[-1], rates[:-1]


DOM_SNAPSHOTS = 3        # 一輪抓幾張快照取中位數（殺瞬時假單與做市商報價抖動）
DOM_SNAPSHOT_GAP = 4.0   # 快照間隔秒數（假單要活過整個採樣窗才騙得到中位數）


def dom_imbalance_binance(symbol):
    # limit 從 1000 降到 500：多快照讓每檔 API 呼叫 ×3，用較低的 limit 抵掉大部分
    # 請求權重（Binance depth limit=500 權重是 1000 的一半）；±1% 帶內 500 檔位
    # 對絕大多數幣綽綽有餘
    book = sget("/api/v3/depth", {"symbol": symbol, "limit": 500})
    return _dom_from_book(book["bids"], book["asks"])


# ---------- 2b) 單一幣種指標：Gate.io（長尾幣種）----------
def whale_and_cvd_gate(cp, threshold):
    """Gate.io trades 有明確 side 欄位，一次抓同時算鯨魚淨流向與短期 CVD 近似。"""
    trades = gget("/spot/trades", {"currency_pair": cp, "limit": 500})
    buy = sell = whale_buy = whale_sell = 0.0
    max_single = 0.0
    for t in trades:
        notional = float(t["price"]) * float(t["amount"])
        if t["side"] == "sell":
            sell += notional
            if notional >= threshold:
                whale_sell += notional
        else:
            buy += notional
            if notional >= threshold:
                whale_buy += notional
        max_single = max(max_single, notional)
    total = buy + sell
    cvd_proxy = buy - sell  # 近 500 筆的淨主動買賣力道，近似 CVD
    return whale_buy - whale_sell, total, (max_single / total if total else 0), len(trades), cvd_proxy


def gate_kline_metrics(cp):
    """同 binance_kline_metrics：一次抓 130 根 1h K 線供雙頂偵測與 ta_scoring 共用。
    Gate 格式：[timestamp, quote_vol, close, high, low, open, base_vol, ...]。"""
    kl = gget("/spot/candlesticks", {"currency_pair": cp, "interval": "1h", "limit": 130})
    highs = [float(k[3]) for k in kl]
    lows = [float(k[4]) for k in kl]
    closes = [float(k[2]) for k in kl]
    volumes = [float(k[6]) for k in kl]
    last_close = closes[-1]
    recent_high = max(highs[-6:]) if len(highs) >= 6 else max(highs)
    pullback_pct = (recent_high - last_close) / recent_high * 100 if recent_high else 0
    double_top = False
    if len(highs) >= 10:
        h1_idx = max(range(len(highs) - 10, len(highs) - 5), key=lambda i: highs[i])
        h2_idx = max(range(len(highs) - 5, len(highs)), key=lambda i: highs[i])
        h1, h2 = highs[h1_idx], highs[h2_idx]
        if h1 > 0 and abs(h1 - h2) / h1 < 0.015 and last_close < min(h1, h2) * 0.97:
            double_top = True
    return pullback_pct, double_top, (highs, lows, closes, volumes)


def dom_imbalance_gate(cp):
    book = gget("/spot/order_book", {"currency_pair": cp, "limit": 100})
    bids = [(float(p), float(q)) for p, q in book["bids"]]
    asks = [(float(p), float(q)) for p, q in book["asks"]]
    return _dom_from_book(bids, asks)


def _dom_from_book(bids, asks):
    """回傳 (失衡比, ±1% 內總簿深金額)。總簿深要一起回傳，
    因為信心係數需要拿它跟 24h 成交額比（見 score_symbol 的 DOM 段落）。"""
    bids = [(float(p), float(q)) for p, q in bids]
    asks = [(float(p), float(q)) for p, q in asks]
    if not bids or not asks:
        return None, None
    mid = (bids[0][0] + asks[0][0]) / 2
    lo, hi = mid * 0.99, mid * 1.01
    b = sum(p * q for p, q in bids if p >= lo)
    a = sum(p * q for p, q in asks if p <= hi)
    total = a + b
    return (b / total if total else None), total


def dom_multi_snapshot(fetch_once):
    """一輪抓 DOM_SNAPSHOTS 張快照、間隔 DOM_SNAPSHOT_GAP 秒，失衡比與簿深各取中位數。
    單張快照等於從做市商高頻改單的雜訊裡隨機抽一個點，且對 spoofing（掛假單、價格
    靠近就撤）零防禦——中位數要騙就得讓假單活過整個採樣窗，成本高得多
    （文獻依據見 docs/orderbook-quant-research.md 缺陷 1/2 節）。
    部分快照失敗就用剩下的算，全失敗回傳 (None, None)。"""
    imbs, depths = [], []
    for i in range(DOM_SNAPSHOTS):
        if i:
            time.sleep(DOM_SNAPSHOT_GAP)
        try:
            imb, depth = fetch_once()
        except Exception:  # noqa: BLE001
            continue
        if imb is not None and depth is not None:
            imbs.append(imb)
            depths.append(depth)
    if not imbs:
        return None, None
    return statistics.median(imbs), statistics.median(depths)


def score_dom(imb, depth_usd, qv_24h):
    """DOM 失衡分數乘上「信心係數」（簿深佔 24h 成交額的比例）：BTC/ETH 這種幣
    ±1% 內的絕對簿深金額雖然龐大，但相對於自己動輒數十億的日成交量，佔比其實很小，
    容易被做市商瞬時報價雜訊主導，直接照失衡比給滿分會對大幣過度判斷；
    比例越低分數依比例打折，比例達 DOM_CONF_FULL_RATIO（2%）以上才給滿權重。
    """
    if imb is None or not depth_usd or not qv_24h:
        return None
    raw = clamp(round((imb - 0.5) * 200))
    confidence = clamp(depth_usd / qv_24h / DOM_CONF_FULL_RATIO, 0, 1)
    return round(raw * confidence)


# ---------- 3) 單幣綜合評分 ----------
def score_symbol(row, futures_syms, hl_map, do_dom):
    symbol, base, qv, exch = row["symbol"], row["base"], row["qv"], row["exch"]
    sub = {"whale": None, "cvd": None, "oi": None, "dom": None, "ta": None, "manip": 0, "funding": None}
    tags = []
    pullback, double_top = None, False
    threshold = whale_threshold(qv)

    try:
        if exch == "binance":
            net, total_notional, max_ratio, ntr = whale_flow_binance(symbol, threshold)
        else:
            net, total_notional, max_ratio, ntr, cvd_raw = whale_and_cvd_gate(symbol, threshold)
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
            cvd6, pullback, double_top, ohlcv = binance_kline_metrics(symbol)
            if qv > 0:
                sub["cvd"] = clamp(round(cvd6 / qv * 300))
            if double_top:
                tags.append("雙頂形態")
            ta_result = ta.analyze(*ohlcv)
            sub["ta"] = ta_result["total"]
        except Exception:  # noqa: BLE001
            pass
    else:
        try:
            pullback, double_top, ohlcv = gate_kline_metrics(symbol)
            if double_top:
                tags.append("雙頂形態")
            ta_result = ta.analyze(*ohlcv)
            sub["ta"] = ta_result["total"]
        except Exception:  # noqa: BLE001
            pass

    ratio, oi_chg, funding_now = None, None, None
    if exch == "binance" and symbol in futures_syms:
        try:
            oi_chg, ratio = oi_and_ratio_binance(symbol)
            if oi_chg is not None:
                same_dir = (oi_chg > 0 and row["chg24h"] > 0) or (oi_chg < 0 and row["chg24h"] < 0)
                mag = min(abs(oi_chg), 20) / 20 * 40
                sub["oi"] = round(mag if same_dir else -mag) if oi_chg else 0
            if ratio is not None and ratio >= 2.5:
                tags.append("散戶多單擁擠")
            elif ratio is not None and ratio <= 0.5 and (
                    (sub["whale"] or 0) > 0 or (sub["cvd"] or 0) > 0):
                tags.append("軋空候選")
        except Exception:  # noqa: BLE001
            pass
        try:
            funding_now, funding_hist = fetch_funding_history(symbol)
            sub["funding"] = fs.score_funding_zscore(funding_now, funding_hist)
        except Exception:  # noqa: BLE001
            pass

    if do_dom:
        try:
            fetch = (lambda: dom_imbalance_binance(symbol)) if exch == "binance" \
                else (lambda: dom_imbalance_gate(symbol))
            imb, depth_usd = dom_multi_snapshot(fetch)
            sub["dom"] = score_dom(imb, depth_usd, qv)
            # 成交流交叉確認（Silantyev 2019：成交過的失衡比掛在簿上的可信——
            # 掛單可撤可假，成交不可逆）：簿失衡方向與 taker 成交流（cvd）明確矛盾
            # 時大打折，同向才給全額，天然的 spoof 過濾器
            if sub["dom"] is not None and sub["cvd"] is not None \
                    and sub["dom"] * sub["cvd"] < 0 and abs(sub["dom"]) >= 10 and abs(sub["cvd"]) >= 10:
                sub["dom"] = round(sub["dom"] * 0.4)
            # 跨輪持續性過濾：方向跟上一輪（5 分鐘前）相反就打對折——真實的簿壓
            # 通常持續數輪，spoofing 掛單活不久；上一輪沒有 DOM 資料就不動
            prev_dom = row.get("prev_dom")
            if sub["dom"] is not None and prev_dom is not None and sub["dom"] * prev_dom < 0:
                sub["dom"] = round(sub["dom"] * 0.5)
        except Exception:  # noqa: BLE001
            pass

    hl_crowd = hl_map.get(base)  # 僅參考，不計入總分

    core = {k: v for k, v in sub.items() if k != "manip" and v is not None}
    total = round(sum(core.values()) / len(core) * (1 if len(core) >= 2 else 0.6)) if core else 0
    reso, reso_n = resonance_bonus(sub)
    if reso:
        tags.append(f"{'多' if reso > 0 else '空'}方共振×{reso_n}")
    total = clamp(total + reso + sub["manip"])

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
    has_futures = exch == "binance" and symbol in futures_syms

    return {"symbol": symbol, "base": base, "exch": exch, "close": row["close"],
            "chg24h": row["chg24h"], "qv": qv, "sub": sub, "hl_crowd": hl_crowd,
            "total": total, "grade": grade_of(total), "risk": risk, "tags": tags,
            "raw": {"pullback": pullback, "double_top": double_top, "ratio": ratio,
                    "oi_chg": oi_chg, "has_futures": has_futures, "funding_now": funding_now}}


RESO_STEP = 6       # 每多一個共振源的加成分數
RESO_CAP = 18       # 共振加成上限（避免共振主導總分，總分主體仍是子分數平均）
RESO_THRESHOLD = 25  # 子分數絕對值達此值才算「有明確方向」的共振源


def resonance_bonus(sub):
    """指標共振加成（2026-07-14 起計入總分）：whale/cvd/oi/dom/ta/funding 六個
    來源獨立的子分數中，同方向達 RESO_THRESHOLD 的數量 ≥3 個才給加成，
    每多一個 +RESO_STEP、上限 RESO_CAP。理由：簡單平均會稀釋「多個獨立資料源
    同時指向同方向」的資訊（三個 +30 和一個 +90 兩個 0 平均一樣，但前者證據力
    更強）；同時要求「明確多於反方向數量」，多空訊號拉鋸時不給加成。
    加成刻意壓在總分量級的零頭（≤18），讓它只做臨門一腳，不喧賓奪主。
    回傳 (加成分數, 共振源數量)——源數量給標籤用，不能從被封頂的分數反推。"""
    keys = ("whale", "cvd", "oi", "dom", "ta", "funding")
    pos = sum(1 for k in keys if sub.get(k) is not None and sub[k] >= RESO_THRESHOLD)
    neg = sum(1 for k in keys if sub.get(k) is not None and sub[k] <= -RESO_THRESHOLD)
    if pos >= 3 and pos > neg:
        return min((pos - 2) * RESO_STEP, RESO_CAP), pos
    if neg >= 3 and neg > pos:
        return -min((neg - 2) * RESO_STEP, RESO_CAP), neg
    return 0, 0


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


def risk_label(risk):
    """風險分文字分級，純展示用（數字才是真正的判準）。"""
    if risk >= 90:
        return "即將崩跌"
    if risk >= 75:
        return "高風險"
    if risk >= 50:
        return "看跌觀察"
    if risk >= 25:
        return "留意"
    return "觀望"


def quality_score(r):
    """品質分（長期體質，非動能）：流動性層級＋是否有永續合約＋近期有無結構性破壞。
    這不是基本面分析（沒有團隊/營收等資料可查），只是「流動性與結構穩健度」的規則統計，
    刻意跟主力評分（動能）分開，動能會變、這個相對穩定。"""
    s = 50
    qv = r["qv"]
    s += 25 if qv >= 10_000_000 else (15 if qv >= 1_000_000 else (5 if qv >= 300_000 else -10))
    if r["raw"]["has_futures"]:
        s += 15
    if r["raw"]["double_top"]:
        s -= 20
    if r["raw"]["pullback"] is not None and r["raw"]["pullback"] >= 15:
        s -= 15
    if any(t in r["tags"] for t in ("單筆成交佔比異常",)):
        s -= 15
    return clamp(round(s), 0, 100)


def flow_score(r):
    """FLOW（先行指標）：只看鯨魚／CVD 這兩個「快」訊號，且刻意在價格還沒大動時
    給滿權重——目的是搶在主力評分（需要更多子分數同時到位）觸發之前先標記「正在蓄積」
    的候選，價格已經大動的就打折（不再是「先行」，價值降低）。"""
    whale, cvd = r["sub"]["whale"], r["sub"]["cvd"]
    vals = [v for v in (whale, cvd) if v is not None and v > 0]
    if not vals:
        return 0
    raw = sum(vals) / len(vals)
    discount = 1.0 if abs(r["chg24h"]) < 5 else (0.5 if abs(r["chg24h"]) < 10 else 0.2)
    return round(raw * discount)


def behavior_label(r):
    """行為標籤：優先序規則，每檔只取一個最顯著的描述。"""
    if r["risk"] >= 90:
        return "即將崩跌"
    if "軋空候選" in r["tags"]:
        return "軋空候選"
    if r["risk"] >= 75:
        return "看跌警示"
    if r["grade"] in LONG_GRADES:
        return "動能強（做多）"
    if r["grade"] in SHORT_GRADES:
        return "動能強（做空）"
    if flow_score(r) >= 15 and r["grade"] not in POOL_GRADES:
        return "蓄勢"
    return "觀望"


def action_suggestion(alert, r):
    """建議行動：規則生成的操作語句，非投資建議，僅供參考。"""
    grade = r["grade"]
    atype = alert["type"]
    if atype == "risk_on":
        return "風險分觸頂：建議減碼／緊止損，觀察後續是否止穩。"
    if atype == "risk_off":
        return "風險警示解除：可觀察是否重新站穩，暫不建議追價。"
    if atype == "opportunity":
        if alert["pool_direction"] == "long":
            if grade == "S":
                return "建議：做多。S 級，資金流最集中，可試倉，止損設在入池價下方，留意背離。"
            return "建議：做多。A 級初期，可少量試倉（2-5%），止損設在入池價下方，觀察是否延續。"
        return "建議：做空。D 級，多項指標同向偏空，可試倉，止損設在入池價上方，留意反彈背離。"
    # movement
    change = alert.get("change", "")
    if change == "跌出候選池":
        pool_dir = "多方" if alert.get("pool_direction") == "long" else "空方"
        return f"資金流轉弱：跌出{pool_dir}候選池，建議降低倉位或觀望，等待重新確認動能。"
    if change == "升級":
        return "候選池內續強：可維持既有倉位／部位，追蹤是否進一步強化。"
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
    """三分類通知：
    - 機會（opportunity）：首次進入多方或空方候選池，是唯一會明確講「建議做多／做空」的類型。
    - 異動（movement）：候選池內的升降級、跌出候選池——單純狀態變化，不重複給方向建議。
    - 風險（risk_on/risk_off）：既有部位的反轉/崩跌警示，跟機會候選池是平行、獨立的系統
      （風險分是雙頂/回落/操縱等結構性訊號，不是「有沒有進出候選池」）。
    """
    alerts = []
    if is_cold_start:
        return alerts
    for r in results:
        old = prev_state.get(r["symbol"])
        if old is None:
            continue
        old_grade, new_grade = old.get("grade"), r["grade"]
        old_dir, new_dir = direction_of(old_grade), direction_of(new_grade)
        if old_grade != new_grade:
            if new_dir and not old_dir:
                alerts.append({"type": "opportunity", "pool_direction": new_dir, **r, "prev_grade": old_grade})
            elif old_dir and not new_dir:
                alerts.append({"type": "movement", "change": "跌出候選池",
                              "pool_direction": old_dir, **r, "prev_grade": old_grade})
            elif old_dir and new_dir and old_dir != new_dir:
                # 直接從多方池跳到空方池（或反過來）：先記一筆跌出、再記一筆新機會
                alerts.append({"type": "movement", "change": "跌出候選池",
                              "pool_direction": old_dir, **r, "prev_grade": old_grade})
                alerts.append({"type": "opportunity", "pool_direction": new_dir, **r, "prev_grade": old_grade})
            elif old_dir and new_dir:
                change = "升級" if grade_rank(new_grade) < grade_rank(old_grade) else "降級"
                alerts.append({"type": "movement", "change": change,
                              "pool_direction": new_dir, **r, "prev_grade": old_grade})
        was_risky = old.get("risk", 0) >= 75
        if r["risk"] >= 75 and not was_risky:
            alerts.append({"type": "risk_on", **r})
        elif was_risky and r["risk"] < 75:
            alerts.append({"type": "risk_off", **r})
    return alerts


def build_all_transitions(results, prev_state, is_cold_start):
    """頁面用的完整事件摘要：所有評級變動（含 B/C/D 之間的日常波動），
    不像 build_alerts 只挑候選池/風險開關——這個給人看，Discord 不用（避免洗版）。"""
    out = []
    if is_cold_start:
        return out
    for r in results:
        old = prev_state.get(r["symbol"])
        if old is None or old.get("grade") == r["grade"]:
            continue
        direction = "升級" if grade_rank(r["grade"]) < grade_rank(old.get("grade", "D")) else "降級"
        out.append({"base": r["base"], "prev_grade": old.get("grade"), "grade": r["grade"],
                    "direction": direction, "total": r["total"]})
    return out


def market_overview(results, all_transitions, fg):
    """大盤總覽：整體風向、廣度、山寨強弱 vs BTC。全部從已算好的 results 彙總，不额外打 API。"""
    btc = next((r for r in results if r["base"] == "BTC"), None)
    eth = next((r for r in results if r["base"] == "ETH"), None)
    avg_total = round(sum(r["total"] for r in results) / len(results), 1) if results else 0
    up = sum(1 for t in all_transitions if t["direction"] == "升級")
    down = sum(1 for t in all_transitions if t["direction"] == "降級")
    if up > down * 1.5:
        bias = "多頭強勢"
    elif down > up * 1.5:
        bias = "空頭強勢"
    else:
        bias = "中性"
    alts = [r for r in results if r["base"] not in ("BTC",)]
    if alts:
        alt_med = sorted(r["chg24h"] for r in alts)[len(alts) // 2]
        excess = alt_med - (btc["chg24h"] if btc else 0)
    else:
        alt_med, excess = None, None
    return {"btc": btc, "eth": eth, "avg_total": avg_total, "up": up, "down": down,
            "bias": bias, "fg": fg, "alt_median_chg": alt_med, "alt_excess_vs_btc": excess}


def update_tracking(results, prev_state, alerted_symbols, now_iso):
    """維護候選池追蹤：入池時間/價/方向、期間高低、通知次數。跌出後凍結直到重新入池。
    多方→空方（或反過來）直接切換視為新一輪episode（重置入池價/高低），
    不會沿用舊方向的追蹤數字。"""
    new_state = {}
    for r in results:
        sym = r["symbol"]
        old = prev_state.get(sym, {})
        direction = direction_of(r["grade"])
        old_direction = direction_of(old.get("grade"))
        in_pool = direction is not None
        same_episode = in_pool and old_direction == direction
        entry = {"grade": r["grade"], "total": r["total"], "risk": r["risk"], "ts": now_iso}
        if r["sub"]["dom"] is not None:
            entry["dom"] = r["sub"]["dom"]  # 下一輪的 DOM 跨輪持續性過濾要用
        if in_pool and not same_episode:
            entry.update(pool_direction=direction, pool_entry_ts=now_iso, pool_entry_price=r["close"],
                         high_since=r["close"], low_since=r["close"], alert_count=0)
        elif same_episode:
            entry.update(pool_direction=direction,
                         pool_entry_ts=old.get("pool_entry_ts", now_iso),
                         pool_entry_price=old.get("pool_entry_price", r["close"]),
                         high_since=max(old.get("high_since", r["close"]), r["close"]),
                         low_since=min(old.get("low_since", r["close"]), r["close"]),
                         alert_count=old.get("alert_count", 0))
        else:
            # 跌出或本來就不在池內：凍結既有追蹤紀錄（若有），不重置
            for k in ("pool_direction", "pool_entry_ts", "pool_entry_price", "high_since", "low_since", "alert_count"):
                if k in old:
                    entry[k] = old[k]
        if sym in alerted_symbols:
            entry["alert_count"] = entry.get("alert_count", 0) + 1
        new_state[sym] = entry
    return new_state


# ---------- 4a2) 推送紀錄與績效（每次進出候選池留一筆歷史紀錄）----------
def load_signal_log():
    if os.path.exists(SIGNAL_LOG_PATH):
        with open(SIGNAL_LOG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_signal_log(log):
    os.makedirs(os.path.dirname(SIGNAL_LOG_PATH), exist_ok=True)
    closed = [r for r in log if r["status"] == "closed"]
    open_ = [r for r in log if r["status"] == "open"]
    if len(closed) + len(open_) > SIGNAL_LOG_MAX:
        closed = closed[-(SIGNAL_LOG_MAX - len(open_)):] if SIGNAL_LOG_MAX > len(open_) else []
    with open(SIGNAL_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(open_ + closed, f, ensure_ascii=False, indent=1)


def update_signal_log(results, prev_state, log, now_iso):
    """每次候選池進出留一筆歷史紀錄，是「推送紀錄與績效」表的資料來源。
    跟 update_tracking() 判斷同一組進出池條件，但這裡是 append-only 歷史，
    update_tracking() 的 whale_state.json 只留最新一次的狀態（會被覆蓋）。
    result 判定：以「目前價（開放中）／結案當下價（已結案）」對比推送價，
    是最直接的「這次訊號後來準不準」量化——不是用最高點硬湊出來的好看數字。
    """
    open_idx = {r["symbol"]: i for i, r in enumerate(log) if r["status"] == "open"}
    for r in results:
        sym = r["symbol"]
        old = prev_state.get(sym, {})
        direction = direction_of(r["grade"])
        old_direction = direction_of(old.get("grade"))
        in_pool = direction is not None
        same_episode = in_pool and old_direction == direction and sym in open_idx

        if in_pool and not same_episode:
            log.append({
                "symbol": sym, "base": r["base"], "exch": r["exch"], "direction": direction,
                "entry_ts": now_iso, "entry_price": r["close"], "entry_grade": r["grade"],
                "last_ts": now_iso, "last_price": r["close"],
                "high_since": r["close"], "low_since": r["close"],
                "push_count": 1, "status": "open",
            })
            open_idx[sym] = len(log) - 1
        elif same_episode:
            rec = log[open_idx[sym]]
            rec["last_ts"] = now_iso
            rec["last_price"] = r["close"]
            rec["high_since"] = max(rec["high_since"], r["close"])
            rec["low_since"] = min(rec["low_since"], r["close"])
            rec["push_count"] += 1
        elif not in_pool and sym in open_idx:
            rec = log[open_idx[sym]]
            rec["status"] = "closed"
            rec["closed_ts"] = now_iso
            rec["last_ts"] = now_iso
            rec["last_price"] = r["close"]
            rec["high_since"] = max(rec["high_since"], r["close"])
            rec["low_since"] = min(rec["low_since"], r["close"])
    return log


def signal_result(rec):
    """回傳 (中文結果字串, 是否成功)。做多＝現價≥推送價才算成功；
    做空方向相反＝現價≤推送價才算成功——這是空方候選池新增後，結果判定
    一定要對稱處理的地方，不能整批沿用做多的判準。"""
    direction = rec.get("direction", "long")
    if direction == "short":
        ok = rec["last_price"] <= rec["entry_price"]
        return ("下跌有效" if ok else "反向走升"), ok
    ok = rec["last_price"] >= rec["entry_price"]
    return ("上漲有效" if ok else "反向走跌"), ok


def signal_stats(log):
    """整份紀錄的勝率統計——這是驗證整套評分系統有沒有用的唯一誠實方法：
    真實訊號事後追蹤，不是回測、不是自吹自擂。多空合併計算總勝率。"""
    closed = [r for r in log if r["status"] == "closed"]
    if not closed:
        return {"n": 0, "win_rate": None, "avg_gain": None, "avg_drawdown": None}
    wins = sum(1 for r in closed if signal_result(r)[1])
    avg_gain = sum((r["high_since"] / r["entry_price"] - 1) * 100 for r in closed) / len(closed)
    avg_dd = sum((r["low_since"] / r["entry_price"] - 1) * 100 for r in closed) / len(closed)
    return {"n": len(closed), "win_rate": round(wins / len(closed) * 100, 1),
            "avg_gain": round(avg_gain, 2), "avg_drawdown": round(avg_dd, 2)}


# ---------- 4b) 主流幣資金強度（相對自己歷史基準，不是絕對分數）----------
def load_majors_history():
    if os.path.exists(MAJORS_PATH):
        with open(MAJORS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("series", {}), d.get("last_flows", {})
    return {}, {}


def save_majors_history(series, last_flows):
    os.makedirs(os.path.dirname(MAJORS_PATH), exist_ok=True)
    with open(MAJORS_PATH, "w", encoding="utf-8") as f:
        json.dump({"series": series, "last_flows": last_flows}, f, ensure_ascii=False, indent=1)


def strength_label(delta):
    if delta >= 30:
        return "資金流入顯著放大"
    if delta >= 15:
        return "資金流入轉強"
    if delta <= -30:
        return "資金流出顯著放大"
    if delta <= -15:
        return "資金流出轉強"
    return "正常波動範圍"


def compute_major_flows(results, prev_hist):
    """BTC/SOL 這種高流動性幣，固定 $10,000 門檻的鯨魚分數本身雖然已經改成分層
    （見 whale_threshold），但單一快照的分數還是不足以判斷「現在是不是真的資金
    大量流入」——BTC 隨時都有大單進出，重點是「跟它自己平常比是不是明顯放大」。
    這裡對每檔主流幣維護近 2 小時（24 筆×5分鐘）的鯨魚分數歷史，用「當前值 減去
    扣掉當前這筆之後的歷史平均」當作相對強度，而不是拿不同幣的絕對分數互相比較。
    """
    by_base = {r["base"]: r for r in results}
    new_hist, flows = {}, []
    for base in MAJORS:
        r = by_base.get(base)
        if r is None:
            continue
        series = list(prev_hist.get(base, []))
        baseline = round(sum(series) / len(series), 1) if series else None
        current = r["sub"]["whale"] if r["sub"]["whale"] is not None else 0
        delta = round(current - baseline, 1) if baseline is not None else None
        series.append(current)
        new_hist[base] = series[-MAJORS_HISTORY_LEN:]
        flows.append({"base": base, "close": r["close"], "chg24h": r["chg24h"],
                      "whale_now": current, "baseline": baseline, "delta": delta,
                      "label": strength_label(delta) if delta is not None else "累積基準中",
                      "samples": len(series)})
    return flows, new_hist


def build_major_alerts(flows, prev_last_flows):
    """主流幣資金強度異常放大/縮小時單獨推 Discord，跟候選池/風險開關是平行的第三種觸發源。
    只在「進入顯著放大狀態」的瞬間推播一次（用上一輪記錄的 label 判斷邊界），
    停留在顯著放大狀態的後續輪次不重複通知，離開後才可能再次觸發。
    """
    alerts = []
    for f in flows:
        if f["delta"] is None:
            continue
        was_notable = prev_last_flows.get(f["base"]) in (
            "資金流入顯著放大", "資金流出顯著放大")
        now_notable = f["label"] in ("資金流入顯著放大", "資金流出顯著放大")
        if now_notable and not was_notable:
            alerts.append(dict(f))
    return alerts


# ---------- 5) Discord ----------
# 用 embed（不是純文字 content）：Discord 對同一機器人短時間內連發的純文字訊息
# 會自動合併、不重複顯示大頭貼也沒有卡片分隔線，連續好幾則會糊成一坨
# （2026-07-13 實測抓到，改回 embed 才有參考截圖那種「每則獨立色條卡片」的效果）。
# 內文排版沿用 markdown 慣例：反引號包數字、粗體百分比、反引號文字進度條、
# > 引言區塊放備註。
GRADE_DOT = {"S": "🟡", "A": "🟢", "B": "⚪", "C": "🟠", "D": "🔴"}  # S 用金/黃圈區隔頂級
EMBED_COLOR = {"opportunity_long": 0xD4AF37, "opportunity_short": 0xA83E54,
              "movement": 0x8A8172, "risk_on": 0xA83E54, "risk_off": 0x8A8172,
              "major": 0xD4AF37}
DISCORD_SEND_DELAY = 0.4  # 逐則發送間隔，避免撞 webhook 速率限制


def bar(v, width=12):
    """-100~100 置中進度條，供總分（機會/異動）使用。"""
    v = clamp(v, -100, 100)
    filled = round((v + 100) / 200 * width)
    return "▓" * filled + "░" * (width - filled)


def bar100(v, width=12):
    """0~100 進度條，供品質分/風險分這種非置中量表使用。"""
    v = clamp(v, 0, 100)
    filled = round(v / 100 * width)
    return "▓" * filled + "░" * (width - filled)


def trend_emoji(chg):
    return "📈" if chg >= 0 else "📉"


def _post_discord_embeds(webhook, embeds):
    """每批最多 10 個 embed（Discord API 上限），批次之間留間隔避免撞速率限制。"""
    for i in range(0, len(embeds), 10):
        batch = embeds[i:i + 10]
        try:
            requests.post(webhook, json={"embeds": batch}, timeout=TIMEOUT)
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] Discord 推播失敗: {e}")
        time.sleep(DISCORD_SEND_DELAY)


def should_send_digest(now):
    """定時戰報節流：距上次發送滿 DIGEST_INTERVAL_HOURS 才發。時間戳存獨立小檔，
    不塞進 whale_state.json（那個檔每輪被 update_tracking 整個重建，塞進去會被洗掉）。"""
    try:
        with open(DIGEST_STATE_PATH, encoding="utf-8") as f:
            last = dt.datetime.fromisoformat(json.load(f)["last_ts"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return True  # 沒發過（或檔案壞掉）→ 直接發
    return (now - last).total_seconds() >= DIGEST_INTERVAL_HOURS * 3600


def save_digest_ts(now):
    with open(DIGEST_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"last_ts": now.isoformat()}, f)


def digest_line(r):
    return (f'{GRADE_DOT.get(r["grade"], "⚪")}**{r["base"]}** `{r["total"]:+d}` · '
            f'24h {r["chg24h"]:+.1f}% · 品質 `{r["quality"]}`')


def send_digest_discord(webhook, results, overview, stats, universe_n, now):
    """定時戰報（完整評分摘要）：跟事件型通知（機會/異動/風險）不同，這是固定間隔
    的全景快照——大盤狀態＋兩個候選池＋蓄勢觀察＋推送紀錄勝率。使用者要求：
    「完整評分也要在 discord 給我」。單一 embed，金色框。"""
    if not webhook:
        return
    longs = sorted((r for r in results if r["grade"] in LONG_GRADES), key=lambda r: -r["total"])
    shorts = sorted((r for r in results if r["grade"] in SHORT_GRADES), key=lambda r: r["total"])
    brewing = sorted((r for r in results if r["grade"] not in POOL_GRADES and r["flow"] >= 15),
                     key=lambda r: -r["flow"])[:3]
    lines = [f'掃描 `{universe_n}` 檔 · 大盤 **{overview["bias"]}** · '
             f'漲 `{overview["up"]}`/跌 `{overview["down"]}`'
             + (f' · 恐慌貪婪 `{overview["fg"]}`' if overview.get("fg") is not None else "")]
    for key, label in (("btc", "BTC"), ("eth", "ETH")):
        m = overview.get(key)
        if m:
            lines.append(f'{label} `{m["total"]:+d}` {GRADE_DOT.get(m["grade"], "⚪")}{m["grade"]} '
                         f'({m["chg24h"]:+.1f}%)')
    lines.append("")
    lines.append(f'**🟢 多方候選池（{len(longs)}）**')
    lines += [digest_line(r) for r in longs[:8]] or ["（無）"]
    if len(longs) > 8:
        lines.append(f'…另有 {len(longs) - 8} 檔，完整清單見網頁')
    lines.append("")
    lines.append(f'**🔴 空方候選池（{len(shorts)}）**')
    lines += [digest_line(r) for r in shorts[:8]] or ["（無）"]
    if len(shorts) > 8:
        lines.append(f'…另有 {len(shorts) - 8} 檔，完整清單見網頁')
    if brewing:
        lines.append("")
        lines.append('**⏳ 蓄勢觀察（未入池但資金先行）**')
        lines += [f'⚪**{r["base"]}** FLOW `{r["flow"]}` · 24h {r["chg24h"]:+.1f}%' for r in brewing]
    lines.append("")
    win_txt = f'{stats["win_rate"]}%（{stats["n"]} 筆結案）' if stats["win_rate"] is not None else f'累積中（{stats["n"]} 筆結案）'
    lines.append(f'推送紀錄勝率 **{win_txt}**')
    lines.append("程式規則生成，非投資建議")
    embed = {"title": f'📊 定時戰報 · {now:%m-%d %H:%M} UTC',
             "description": "\n".join(lines), "color": 0xD4AF37}
    _post_discord_embeds(webhook, [embed])


def send_major_flow_discord(webhook, major_alerts):
    if not webhook or not major_alerts:
        return
    embeds = []
    for f in major_alerts:
        inflow = "流入" in f["label"]
        head = "▲" if inflow else "▼"
        title = f'{head} 主流強度・{f["base"]}・{"🟢" if inflow else "🔴"} {f["label"]}：{trend_emoji(f["chg24h"])}'
        desc = (f'現價 `${f["close"]:g}` · 24h **{f["chg24h"]:+.1f}%**\n'
                f'鯨魚分 `{f["whale_now"]:+.0f}` · 近2h基準 `{f["baseline"]:+.1f}` · '
                f'相對強度 **{f["delta"]:+.1f}**\n'
                '> 跟自己平常比，不是跟其他幣比較\n程式規則生成，非投資建議')
        embeds.append({"title": title, "description": desc,
                       "color": EMBED_COLOR["major"] if inflow else 0xA83E54})
    _post_discord_embeds(webhook, embeds)


SUB_LABELS = (("whale", "鯨魚"), ("cvd", "CVD"), ("oi", "OI"), ("dom", "簿深"),
              ("ta", "技術"), ("funding", "費率"))


def sub_breakdown(a):
    """子分數明細列：機會/異動訊息顯示「是哪幾個指標在推動這個訊號」。
    只列有資料的項目；技術分本身已是 ta_scoring.py 十一個指標的合成分。"""
    sub = a.get("sub") or {}
    parts = [f"{name} `{sub[k]:+d}`" for k, name in SUB_LABELS if sub.get(k) is not None]
    return " · ".join(parts)


def send_discord(webhook, alerts):
    if not webhook or not alerts:
        return
    embeds = []
    for a in alerts:
        tags_text = "、".join(a.get("tags", []))
        ckey = a["type"]
        if a["type"] == "opportunity":
            is_long = a["pool_direction"] == "long"
            ckey = "opportunity_long" if is_long else "opportunity_short"
            head = "▲" if is_long else "▼"
            call = "🟢 做多點" if is_long else "🔴 做空點"
            quote = f'{tags_text}｜{action_suggestion(a, a)}' if tags_text else action_suggestion(a, a)
            title = f'{head} 機會・{a["base"]}・{call}：{trend_emoji(a["chg24h"])}'
            breakdown = sub_breakdown(a)
            desc = (f'進場價 `${a["close"]:g}` · 24h **{a["chg24h"]:+.1f}%**\n'
                    f'總分 `{bar(a["total"])}` **{a["total"]:+d}** · '
                    f'品質 `{a["quality"]}` · {GRADE_DOT.get(a["grade"],"⚪")}**{a["grade"]}**\n'
                    + (f'{breakdown}\n' if breakdown else '')
                    + f'> {quote}')
        elif a["type"] == "movement":
            icon = "🟠" if a["change"] == "跌出候選池" else "⚡"
            quote = tags_text or action_suggestion(a, a)
            title = f'{icon} 異動・{a["base"]}・{a["prev_grade"]}→{a["grade"]}（{a["change"]}）：{trend_emoji(a["chg24h"])}'
            breakdown = sub_breakdown(a)
            desc = (f'現價 `${a["close"]:g}` · **{a["chg24h"]:+.1f}%** · 篩選分 `{a["quality"]}` · '
                    f'{GRADE_DOT.get(a["grade"],"⚪")}**{a["grade"]}** `{a["total"]:+d}`\n'
                    + (f'{breakdown}\n' if breakdown else '')
                    + f'> {quote}')
        elif a["type"] == "risk_on":
            title = f'▼ 風險・{a["base"]}・🔴 {risk_label(a["risk"])}：{trend_emoji(a["chg24h"])}'
            desc = (f'現價 `${a["close"]:g}` · **{a["chg24h"]:+.1f}%**\n'
                    f'風險評分 `{bar100(a["risk"])}` **{a["risk"]}** · {risk_label(a["risk"])}\n'
                    f'> {action_suggestion(a, a)}')
        else:
            title = f'▲ 風險・{a["base"]}・⚪ 警示解除：{trend_emoji(a["chg24h"])}'
            desc = (f'現價 `${a["close"]:g}` · **{a["chg24h"]:+.1f}%**\n'
                    f'風險評分 `{bar100(a["risk"])}` **{a["risk"]}**\n'
                    f'> {action_suggestion(a, a)}')
        pe_ts, pe_px = a.get("pool_entry_ts"), a.get("pool_entry_price")
        if pe_ts and pe_px and a["type"] in ("opportunity", "movement"):
            hi, lo = a.get("high_since", pe_px), a.get("low_since", pe_px)
            desc += (f'\n入池 `{pe_ts[5:16]}` · `${pe_px:g}` · '
                    f'最高 **{(hi/pe_px-1)*100:+.1f}%** · 最低 **{(lo/pe_px-1)*100:+.1f}%** · '
                    f'異動 `{a.get("alert_count", 1)}` 次')
        desc += "\n程式規則生成，非投資建議"
        embeds.append({"title": title, "description": desc, "color": EMBED_COLOR.get(ckey, 0x8A8172)})
    _post_discord_embeds(webhook, embeds)


# ---------- 6) 網頁 ----------
def card_html(r):
    sub = r["sub"]
    direction = direction_of(r["grade"])
    if direction == "long":
        tone = "up"
    elif direction == "short":
        tone = "dn"
    else:
        tone = "dn" if r["risk"] >= 75 else ""

    def s(v):
        return "缺" if v is None else f"{v:+d}"

    track = ""
    if r.get("pool_entry_ts") and r.get("pool_entry_price"):
        pe_px, hi, lo = r["pool_entry_price"], r.get("high_since", r["close"]), r.get("low_since", r["close"])
        track = (f'<div class="track">入池 {esc(r["pool_entry_ts"][5:16])}・${pe_px:g}（MFE/MAE）　'
                 f'最高 {(hi/pe_px-1)*100:+.1f}%　最低 {(lo/pe_px-1)*100:+.1f}%　'
                 f'異動 {r.get("alert_count", 0)} 次</div>')
    tags = "、".join(resonance_tags(r) + r["tags"]) or "—"
    risk_line = f'{r["risk"]}・{esc(risk_label(r["risk"]))}' if r["risk"] >= 25 else f'{r["risk"]}'
    return (f'<div class="card {tone}"><div class="chead"><b class="sym">{esc(r["base"])}</b>'
            f'<span class="grade {grade_class(r["grade"])}">{r["grade"]}</span>'
            f'<span class="beh">{esc(r["behavior"])}</span>'
            f'<span class="px">${r["close"]:g}<span class="{"up" if r["chg24h"]>=0 else "dn"}">'
            f'{r["chg24h"]:+.1f}%</span></span></div>'
            f'<div class="bar"><div class="fill {tone}" style="width:{clamp((r["total"]+50),0,100)}%"></div>'
            f'<span>{r["total"]:+d}</span></div>'
            f'<div class="grid"><span>鯨魚 {s(sub["whale"])}</span><span>CVD {s(sub["cvd"])}</span>'
            f'<span>OI×價 {s(sub["oi"])}</span><span>DOM {s(sub["dom"])}</span>'
            f'<span>技術 {s(sub["ta"])}</span>'
            f'<span>HL擁擠 {r["hl_crowd"] if r["hl_crowd"] is not None else "缺"}</span>'
            f'<span>操縱 {sub["manip"]:+d}</span>'
            f'<span>品質 {r["quality"]}</span><span>FLOW {r["flow"]}</span>'
            f'<span>風險 {risk_line}</span></div>'
            + track +
            f'<div class="tags">{esc(tags)}</div>'
            f'<div class="exch">來源：{esc(r["exch"])}</div></div>')


def fmt_duration(start_iso, end_iso):
    try:
        start = dt.datetime.fromisoformat(start_iso)
        end = dt.datetime.fromisoformat(end_iso)
        mins = (end - start).total_seconds() / 60
    except Exception:  # noqa: BLE001
        return "—"
    if mins < 60:
        return f"{round(mins)}min"
    if mins < 60 * 24:
        return f"{mins/60:.1f}h"
    return f"{mins/1440:.1f}d"


def signal_row(rec):
    result, ok = signal_result(rec)
    direction = rec.get("direction", "long")
    dir_txt = "做多" if direction == "long" else "做空"
    dir_cls = "up" if direction == "long" else "dn"
    gain_pct = (rec["high_since"] / rec["entry_price"] - 1) * 100
    dd_pct = (rec["low_since"] / rec["entry_price"] - 1) * 100
    dur = fmt_duration(rec["entry_ts"], rec["last_ts"])
    status_txt = "開放中" if rec["status"] == "open" else "已結案"
    badge_cls = "win" if ok else "loss"
    badge_icon = "✓" if ok else "✗"
    return (
        '<tr>'
        f'<td><div class="logsym"><b>{esc(rec["base"])}</b><span>{esc(rec["symbol"])}</span></div></td>'
        f'<td><span class="{dir_cls}">{dir_txt}</span></td>'
        f'<td><span class="grade {grade_class(rec["entry_grade"])}">{esc(rec["entry_grade"])}</span></td>'
        f'<td><span class="badge {badge_cls}">{badge_icon} {esc(result)}</span></td>'
        f'<td><div class="timecell">{rec["entry_ts"][5:16]} → {rec["last_ts"][5:16]}'
        f'<span>歷時 {dur}・{status_txt}</span></div></td>'
        f'<td class="mono">${rec["entry_price"]:g}</td>'
        f'<td class="mono">${rec["last_price"]:g}</td>'
        f'<td class="mono up">+{gain_pct:.2f}%</td>'
        f'<td class="mono dn">{dd_pct:.2f}%</td>'
        f'<td class="mono">{rec["push_count"]}</td>'
        f'<td><span class="chip" style="font-size:.68rem">{esc(rec["exch"])}</span></td>'
        '</tr>')


def render_page(results, alerts, all_transitions, overview, universe_n, min_qv, now, exch_counts,
                major_flows, signal_log, stats):
    pool = [r for r in results if r["grade"] in POOL_GRADES or r["risk"] >= 75]
    pool.sort(key=lambda r: -r["total"])
    cards = "".join(card_html(r) for r in pool) or '<p class="sub">目前無候選池成員或高風險標的。</p>'

    signal_rows_sorted = sorted(signal_log, key=lambda r: r["last_ts"], reverse=True)[:100]
    signal_rows_html = "".join(signal_row(r) for r in signal_rows_sorted) or '<tr><td colspan="10">尚無推送紀錄。</td></tr>'
    win_rate_txt = f'{stats["win_rate"]}%' if stats["win_rate"] is not None else "累積中"
    avg_gain_txt = f'+{stats["avg_gain"]}%' if stats["avg_gain"] is not None else "—"
    avg_dd_txt = f'{stats["avg_drawdown"]}%' if stats["avg_drawdown"] is not None else "—"

    def major_row(f):
        tone = "up" if (f["delta"] or 0) > 0 else ("dn" if (f["delta"] or 0) < 0 else "")
        base_txt = f'{f["baseline"]:+.1f}' if f["baseline"] is not None else "累積中"
        delta_txt = f'{f["delta"]:+.1f}' if f["delta"] is not None else "—"
        return (f'<tr><td class="code">{esc(f["base"])}</td><td>${f["close"]:g}</td>'
                f'<td>{f["chg24h"]:+.2f}%</td><td>{f["whale_now"]:+.0f}</td>'
                f'<td>{base_txt}</td><td class="{tone}"><b>{delta_txt}</b></td>'
                f'<td>{esc(f["label"])}</td><td>{f["samples"]}</td></tr>')
    major_rows = "".join(major_row(f) for f in major_flows) or "<tr><td colspan=8>—</td></tr>"

    flow_candidates = sorted(
        (r for r in results if r["grade"] not in POOL_GRADES and r["flow"] >= 10),
        key=lambda r: -r["flow"])[:10]
    flow_rows = "".join(
        f'<li><b>{esc(r["base"])}</b>・FLOW {r["flow"]}・{esc(r["behavior"])}'
        f'（現價 ${r["close"]:g}，24h {r["chg24h"]:+.1f}%）</li>' for r in flow_candidates
    ) or "<li>目前沒有明顯的先行蓄積訊號。</li>"

    quality_top = sorted(results, key=lambda r: -r["quality"])[:10]
    quality_rows = "".join(
        f'<li><b>{esc(r["base"])}</b>・品質 {r["quality"]}・{esc(r["behavior"])}</li>'
        for r in quality_top) or "<li>—</li>"

    def sub_cell(v):
        if v is None:
            return '<td class="fl">缺</td>'
        cls = "up" if v > 10 else ("dn" if v < -10 else "")
        return f'<td class="{cls}">{v:+d}</td>'

    rows = []
    for r in sorted(results, key=lambda x: -x["total"])[:200]:
        risk_tone = "dn" if r["risk"] >= 75 else ("" if r["risk"] >= 50 else "fl")
        rows.append(
            f'<tr><td class="code">{esc(r["base"])}</td><td>{esc(r["exch"])}</td>'
            f'<td><span class="grade {grade_class(r["grade"])}"><b>{r["grade"]}</b></span>'
            f'（{r["total"]:+d}）</td>'
            + sub_cell(r["sub"]["whale"]) + sub_cell(r["sub"]["cvd"])
            + sub_cell(r["sub"]["oi"]) + sub_cell(r["sub"]["dom"]) + sub_cell(r["sub"]["ta"])
            + f'<td class="{risk_tone}">{r["risk"]}・{esc(risk_label(r["risk"]))}</td>'
            f'<td>{r["quality"]}</td><td>{r["flow"]}</td><td>{esc(r["behavior"])}</td>'
            f'<td>{esc("、".join(r["tags"]) if r["tags"] else "—")}</td>'
            f'<td>{r["chg24h"]:+.2f}%</td></tr>')

    # Discord 有推播的異動（機會／異動／風險三分類）
    def alert_line(a):
        if a["type"] == "opportunity":
            call = "做多" if a["pool_direction"] == "long" else "做空"
            return f'🎯 機會・{esc(a["base"])}：{esc(a["prev_grade"])}→{esc(a["grade"])}，建議{call}'
        if a["type"] == "movement":
            return f'⚡ 異動・{esc(a["base"])}：{esc(a["prev_grade"])}→{esc(a["grade"])}（{esc(a["change"])}）'
        if a["type"] == "risk_on":
            return f'🔴 風險・{esc(a["base"])}：升至 {a["risk"]}（{esc(risk_label(a["risk"]))}）'
        return f'⚪ 風險解除・{esc(a["base"])}：{a["risk"]}'
    alert_rows = "".join(f'<li>{alert_line(a)}</li>' for a in alerts) \
        or "<li>本輪無機會／異動／風險開關（Discord 不會推播）</li>"

    # 全市場所有評級變動（含日常 B/C/D 波動，只在頁面顯示、不推 Discord，避免洗版）
    trans_rows = "".join(
        f'<li>{"🔻" if t["direction"]=="降級" else "🔺"} {esc(t["base"])}　'
        f'{esc(t["prev_grade"])}→{esc(t["grade"])}（{esc(t["direction"])}，{t["total"]:+d}）</li>'
        for t in sorted(all_transitions, key=lambda t: t["base"])[:60]
    ) or "<li>本輪全市場無評級變動。</li>"

    # 黑金主題：刻意固定一套視覺（不跟隨系統亮暗），2026-07-13 應使用者要求改版。
    # 金色只用在版面裝飾/強調（標題、邊框、S 級徽章），漲跌語意色維持獨立的
    # 綠/紅（已過色盲驗證 ΔE 14.1），不會拿金色混充漲跌，避免語意衝突。
    css = """
    :root{--bg:#0B0A08;--card:#17140F;--card2:#1D1811;--ink:#ECE4D3;--muted:#A79A7E;
     --line:rgba(212,175,55,.22);--gold:#D4AF37;--gold-soft:#E8CD6B;--chip:#221D14;
     --up:#2EA673;--dn:#A83E54;--flat:#8A8172}
    *{box-sizing:border-box}
    body{background:var(--bg);color:var(--ink);margin:0;
     font-family:"Microsoft JhengHei","PingFang TC",system-ui,sans-serif}
    .wrap{max-width:1160px;margin:0 auto;padding:32px 20px 64px;display:flex;flex-direction:column;gap:22px}
    .mast{border-bottom:1px solid var(--gold);padding-bottom:16px;display:flex;flex-wrap:wrap;
     align-items:baseline;gap:8px 16px}
    .mast h1{font-size:1.5rem;margin:0;letter-spacing:.16em;color:var(--gold);font-weight:700}
    .chip{background:var(--chip);color:var(--muted);border:1px solid var(--line);
     border-radius:999px;padding:3px 13px;font-size:.72rem}
    section{background:linear-gradient(180deg,var(--card),var(--card) 60%,var(--card2));
     border:1px solid var(--line);border-radius:12px;padding:22px 24px;
     box-shadow:0 1px 0 rgba(212,175,55,.06) inset}
    h2{font-size:.92rem;margin:0 0 6px;color:var(--gold-soft);letter-spacing:.16em;
     font-weight:700;text-transform:uppercase;padding-left:12px;
     border-left:3px solid var(--gold)}
    .sub{font-size:.8rem;color:var(--muted);margin:0 0 16px;line-height:1.6}
    .tbl{overflow-x:auto}
    table{border-collapse:collapse;width:100%;font-size:.82rem;white-space:nowrap}
    th{color:var(--gold-soft);text-align:right;padding:7px 11px;border-bottom:1px solid var(--line);
     font-size:.7rem;letter-spacing:.06em;font-weight:600;text-transform:uppercase}
    td{padding:7px 11px;text-align:right;border-bottom:1px solid var(--line);
     font-variant-numeric:tabular-nums}
    th:first-child,td:first-child{text-align:left}
    tbody tr:hover td{background:rgba(212,175,55,.05)}
    .code{font-family:ui-monospace,Consolas,monospace}
    .up{color:var(--up)}.dn{color:var(--dn)}.fl{color:var(--flat)}
    .appendix li,.appendix p{font-size:.82rem;color:var(--muted);line-height:1.7}
    a{color:var(--gold-soft)}
    .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}
    .card{border:1px solid var(--line);border-radius:12px;padding:16px 18px;background:var(--card2);
     transition:border-color .15s}
    .card.up{border-color:var(--up)}.card.dn{border-color:var(--dn)}
    .chead{display:flex;align-items:baseline;gap:8px;margin-bottom:8px}
    .chead .sym{font-size:1.08rem;font-weight:700;letter-spacing:.02em}
    .grade{font-weight:700;padding:2px 10px;border-radius:6px;background:var(--chip);
     display:inline-block;font-size:.8rem;line-height:1.5}
    .grade.gS{background:var(--gold);color:#0B0A08}
    .grade.gA{background:color-mix(in srgb, var(--up) 26%, var(--chip));color:var(--up)}
    .grade.gB{background:var(--chip);color:var(--ink)}
    .grade.gC{background:color-mix(in srgb, var(--dn) 26%, var(--chip));color:var(--dn)}
    .grade.gD{background:var(--dn);color:#fff}
    .chead .px{margin-left:auto;font-size:.82rem;font-variant-numeric:tabular-nums}
    .card .bar{position:relative;background:var(--chip);border-radius:6px;height:15px;margin:8px 0;overflow:hidden}
    .card .bar .fill{position:absolute;left:0;top:0;bottom:0;background:var(--flat)}
    .card .bar .fill.up{background:var(--up)}.card .bar .fill.dn{background:var(--dn)}
    .card .bar span{position:relative;font-size:.68rem;line-height:15px;padding-left:6px;
     font-variant-numeric:tabular-nums;color:#fff;font-weight:700;
     text-shadow:0 1px 2px rgba(0,0,0,.75),0 0 1px rgba(0,0,0,.9)}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:3px 10px;font-size:.78rem;color:var(--muted);margin:8px 0}
    .track{font-size:.74rem;color:var(--muted);border-top:1px dashed var(--line);padding-top:8px;margin-top:8px}
    .tags{font-size:.76rem;margin-top:4px;color:var(--gold-soft)}
    .exch{font-size:.68rem;color:var(--muted);margin-top:6px}
    .chead .beh{font-size:.68rem;color:var(--gold-soft);background:var(--chip);
     border-radius:6px;padding:2px 8px;border:1px solid var(--line)}
    .ov{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
    .ov .kpi{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--card2)}
    .ov .kpi .l{font-size:.7rem;color:var(--muted);letter-spacing:.03em}
    .ov .kpi .v{font-size:1.2rem;font-weight:700;font-variant-numeric:tabular-nums;color:var(--gold-soft)}
    .cols{columns:2;column-gap:24px}
    .cols li{break-inside:avoid}
    .muted{color:var(--muted)}
    .logtbl{border-collapse:separate;border-spacing:0 5px}
    .logtbl td{background:var(--card2);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
    .logtbl td:first-child{border-left:1px solid var(--line);border-radius:8px 0 0 8px}
    .logtbl td:last-child{border-right:1px solid var(--line);border-radius:0 8px 8px 0}
    .logsym{display:flex;flex-direction:column;line-height:1.25}
    .logsym b{font-size:.9rem}
    .logsym span{font-size:.66rem;color:var(--muted);font-family:ui-monospace,Consolas,monospace}
    .timecell{display:flex;flex-direction:column;line-height:1.4;font-size:.78rem}
    .timecell span{font-size:.68rem;color:var(--muted)}
    .badge{display:inline-flex;align-items:center;gap:4px;padding:3px 11px;border-radius:999px;
     font-size:.74rem;font-weight:700;white-space:nowrap}
    .badge.win{background:rgba(46,166,115,.18);color:var(--up)}
    .badge.loss{background:rgba(168,62,84,.18);color:var(--dn)}
    td.mono{font-family:ui-monospace,Consolas,monospace}
    """
    return (f'<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>加密訊息 {now:%Y-%m-%d %H:%M}</title><style>{css}</style></head><body>'
            f'<div class="wrap"><header class="mast"><h1>加密訊息</h1>'
            f'<span class="chip">掃描 {universe_n} 檔（Binance {exch_counts.get("binance",0)}・'
            f'Gate.io {exch_counts.get("gate",0)}・24h額≥${min_qv:,.0f}）</span>'
            f'<span class="chip">更新 {now:%Y-%m-%d %H:%M} UTC</span>'
            f'<span class="chip"><a href="./index.html">← 回每日市場觀察</a></span></header>'
            f'<section><h2>大盤總覽</h2>'
            f'<p class="sub">全部從本輪已算好的 {universe_n} 檔彙總，不額外打 API</p>'
            f'<div class="ov">'
            f'<div class="kpi"><div class="l">整體風向（升/降家數）</div>'
            f'<div class="v">{esc(overview["bias"])}</div>'
            f'<div class="l">升 {overview["up"]}・降 {overview["down"]}</div></div>'
            f'<div class="kpi"><div class="l">全市場平均主力分</div><div class="v">{overview["avg_total"]:+.1f}</div></div>'
            f'<div class="kpi"><div class="l">Fear &amp; Greed</div>'
            f'<div class="v">{overview["fg"] if overview["fg"] is not None else "缺"}</div></div>'
            f'<div class="kpi"><div class="l">BTC / ETH 24h</div>'
            f'<div class="v">{(str(round(overview["btc"]["chg24h"],1))+"%") if overview["btc"] else "缺"} / '
            f'{(str(round(overview["eth"]["chg24h"],1))+"%") if overview["eth"] else "缺"}</div></div>'
            f'<div class="kpi"><div class="l">山寨中位數 24h（超額 vs BTC）</div>'
            f'<div class="v">{(str(round(overview["alt_median_chg"],1))+"%") if overview["alt_median_chg"] is not None else "缺"}'
            f'（{overview["alt_excess_vs_btc"]:+.1f}pp）</div></div></div></section>'
            f'<section><h2>推送紀錄與績效（誠實對帳，不是回測）</h2>'
            f'<p class="sub">每次進入候選池都留一筆紀錄，事後追蹤實際漲跌——這是驗證整套評分'
            f'系統有沒有用的唯一誠實方法。「結果」＝目前價（開放中）或結案當下價（已結案）'
            f'對比推送價，不是挑最高點硬湊好看數字（見附錄）</p>'
            f'<div class="ov">'
            f'<div class="kpi"><div class="l">已結案訊號數</div><div class="v">{stats["n"]}</div></div>'
            f'<div class="kpi"><div class="l">上漲有效率</div>'
            f'<div class="v">{win_rate_txt}</div></div>'
            f'<div class="kpi"><div class="l">平均最大漲幅</div>'
            f'<div class="v">{avg_gain_txt}</div></div>'
            f'<div class="kpi"><div class="l">平均最大回落</div>'
            f'<div class="v">{avg_dd_txt}</div></div>'
            f'</div>'
            f'<div class="tbl" style="margin-top:14px"><table class="logtbl"><tr><th>幣種</th><th>方向</th><th>評級</th><th>結果</th>'
            f'<th>首推→最新</th><th>推送價</th><th>現價/結案價</th><th>漲幅(高點)</th>'
            f'<th>跌幅(低點)</th><th>推送次數</th><th>來源</th></tr>'
            + signal_rows_html + '</table></div></section>'
            f'<section><h2>主流幣資金強度（相對自己基準，不是跟其他幣比）</h2>'
            f'<p class="sub">固定幣種{" / ".join(MAJORS)}，鯨魚門檻本就依流動性分層更高；'
            f'這裡再拿「現在」跟「這檔幣自己近 2 小時的平均」比，抓真正異常放大的資金流，'
            f'不是絕對分數高低（見附錄）</p>'
            f'<div class="tbl"><table><tr><th>幣種</th><th>現價</th><th>24h</th>'
            f'<th>當前鯨魚分</th><th>近2h基準</th><th>相對強度</th><th>判讀</th><th>樣本數</th></tr>'
            + major_rows + '</table></div></section>'
            f'<section><h2>機會候選池（多方 S/A・空方 D・或高風險）</h2>'
            f'<p class="sub">綠框＝多方機會（建議做多）、紅框＝空方機會（建議做空）或高風險警示；'
            f'入池追蹤（時間/價格/期間高低＝MFE/MAE）持續累積</p>'
            f'<div class="cards">{cards}</div></section>'
            f'<section><h2>先行・點火前資金流（FLOW，未進候選池）</h2>'
            f'<p class="sub">只看鯨魚／CVD 這兩個快訊號，且價格還沒大動時給滿權重——目的是搶在主力評分'
            f'觸發前先標記「正在蓄積」的候選，不保證會真的觸發</p><ul class="cols">{flow_rows}</ul></section>'
            f'<section><h2>品質分排行（長期體質，非動能）</h2>'
            f'<p class="sub">流動性層級＋有無永續合約＋近期有無結構性破壞的規則統計，跟主力評分分開看</p>'
            f'<ul class="cols">{quality_rows}</ul></section>'
            f'<section><h2>機會／異動／風險（Discord 有推播）</h2><ul>{alert_rows}</ul></section>'
            f'<section><h2>全市場評級異動摘要（僅頁面顯示，含日常 B/C/D 波動）</h2>'
            f'<ul class="cols">{trans_rows}</ul></section>'
            f'<section><h2>全市場評分（顯示前 200，依總分排序）</h2>'
            f'<p class="sub">評級：總分 ≥50 S・≥25 A・≥0 B・≥-25 C・其餘 D｜「缺」＝該幣無此資料源</p>'
            f'<div class="tbl"><table><tr><th>標的</th><th>來源</th><th>評級</th><th>鯨魚</th><th>CVD</th>'
            f'<th>OI×價</th><th>DOM</th><th>技術</th><th>風險</th><th>品質</th><th>FLOW</th><th>行為</th>'
            f'<th>訊號標籤</th><th>24h</th></tr>'
            + "".join(rows) +
            '</table></div></section>'
            '<section class="appendix"><h2>公式與誠實限制</h2><ul>'
            '<li><b>涵蓋範圍</b>：Binance 現貨 USDT（主要）＋ Gate.io 現貨 USDT（只收 Binance 沒有的長尾幣，'
            '約 130 檔，含 TAG 這類極小型迷因幣）。兩者都套 24h 成交額 ≥$30萬 的流動性門檻。</li>'
            '<li><b>鯨魚（大額成交淨流向）</b>：近 500 筆逐筆成交中「單筆金額 ≥ 門檻」者的買賣淨額。'
            '<b>門檻依 24h 成交額分層</b>，不是齊頭式固定 $10,000——固定門檻對 BTC/ETH 這種'
            '日均量數十億的幣毫無意義（$10,000 只是雜訊），對長尾小幣卻可能大到永遠觸發不了。'
            '分層：24h額≥$5億→門檻$200,000；≥$1億→$75,000；≥$2000萬→$25,000；'
            '≥$200萬→$8,000；其餘→$3,000。Binance 用 isBuyerMaker 推斷方向；'
            'Gate.io 用其 trades 的明確 side 欄位。<b>這不是真實錢包持倉</b>，'
            '只是大單掛單行為的統計近似。</li>'
            '<li><b>技術</b>：均線排列／RSI／MACD／KD／布林通道／成交量比六個常見技術指標'
            '各自量化成 -100~100 連續分數後合成（獨立模組 ta_scoring.py，同時供日報頁面使用，'
            '公式細節見該檔案 docstring）。跟「鯨魚/CVD/OI/DOM」是完全不同的資料來源'
            '（K 線 vs 逐筆成交/合約資料），互相獨立、互為驗證。</li>'
            '<li><b>CVD</b>：Binance 用近 6 小時 K 線的主動買賣量差；Gate.io 沒有這個欄位，'
            '改用近 500 筆成交的淨方向近似，兩者方法不同、意義相近。</li>'
            '<li><b>OI×價</b>：僅 Binance 有永續合約的幣才有資料；Gate.io 尚未整合期貨資料，一律缺。</li>'
            '<li><b>HL 未平倉擁擠度</b>：Hyperliquid 未平倉名目值／當日成交量，僅供參考、不計入總分。'
            'Hyperliquid 只有約 231 檔主流永續合約，<b>完全不含 TAG/VANRY/RIF 這類長尾迷因幣</b>，'
            '對這類標的這欄必定顯示「缺」——這是免費 API 的硬限制，不是我們刻意不做。</li>'
            '<li><b>DOM</b>：只對本輪分數最高的前 40 檔額外抓深度算失衡比，其餘顯示「缺」。'
            '分數會再乘上「信心係數」＝±1% 簿深金額 ÷ 24h 成交額，比例 &lt;2% 依比例打折——'
            'BTC/ETH 這種幣簿深絕對金額雖大，佔自己日成交量的比例其實很小，容易被做市商'
            '瞬時報價雜訊主導，不打折會對大幣的 DOM 訊號過度判斷。'
            '掛單可撤可假（spoofing），另有三層防偽：一輪 3 張快照取中位數、'
            '與 taker 成交流方向矛盾時 ×0.4、與上一輪方向相反時 ×0.5。'
            '學術文獻上簿失衡的預測力集中在 tick 級，此處僅作流動性/確認變數。</li>'
            '<li><b>操縱警示／雙頂形態</b>：簡單啟發式規則，是提示不是證據；低流動性幣的操縱檢查會跳過。</li>'
            '<li><b>品質分</b>：50 起跳，流動性層級 -10~+25、有 Binance 永續合約 +15、'
            '近期雙頂 -20、近期高點回落 ≥15% -15、有操縱警示 -15。<b>這不是基本面分析</b>'
            '（沒有團隊/營收/白皮書等資料可查），只是流動性與結構穩健度的規則統計，'
            '跟主力評分（動能，會快速變化）刻意分開看。</li>'
            '<li><b>FLOW（先行指標）</b>：只取鯨魚／CVD 兩個「反應快」的子分數平均，'
            '24h 漲跌在 ±5% 內給滿權重、±5~10% 打五折、超過 ±10% 只算兩成——'
            '概念是「動作還沒被價格證實前」的早期蓄積訊號，<b>不保證會真的觸發主力評分</b>，'
            '很多 FLOW 訊號最後不了了之。</li>'
            '<li><b>行為標籤／風險文字分級</b>：純展示用的規則對照（如風險≥90＝「即將崩跌」、'
            '多空比≤0.5 且資金淨流入＝「軋空候選」），數字本身才是判準，文字只是好讀。</li>'
            '<li><b>大盤總覽</b>：全部彙總自本輪已算好的 results，不另外打 API；「升/降家數」'
            '統計全市場（不限候選池）本輪評級變動方向；「山寨超額」＝山寨 24h 漲跌中位數－BTC 24h。</li>'
            f'<li><b>主流幣資金強度</b>：固定追蹤 {"/".join(MAJORS)}。鯨魚門檻已依流動性分層'
            '（見上方鯨魚說明），但 BTC/ETH 這種幣隨時都有大單進出，單一快照的鯨魚分數'
            '無法判斷「現在算不算異常」。這裡額外維護每檔幣近 2 小時（24 筆×5分鐘）的'
            '鯨魚分數歷史，用「當前值－扣除當前這筆後的歷史平均」當相對強度：'
            '≥+30＝資金流入顯著放大、≥+15＝轉強，對稱定義流出；未達門檻＝正常波動範圍。'
            '只在「進入顯著放大」的瞬間推 Discord，停留在該狀態不重複通知。'
            '<b>樣本數 &lt; 24 代表歷史還在累積中，基準尚不穩定，判讀僅供參考。</b></li>'
            '<li><b>機會／異動／風險三分類（Discord）</b>：<b>機會</b>＝首次進入多方（S/A）或空方（D）'
            '候選池，是唯一會明確講「建議做多／做空」的通知；<b>異動</b>＝候選池內的升降級、跌出候選池，'
            '純狀態追蹤不重複給方向建議；<b>風險</b>＝既有的反轉/崩跌警示（雙頂、大幅回落、操縱嫌疑），'
            '跟機會候選池是兩套獨立系統——風險警示不代表「機會」消失，是另一個維度的訊號。</li>'
            '<li><b>空方候選池（做空機會）</b>：2026-07-12 新增，對稱多方候選池——總分 &lt;-25（D 級）'
            '首次觸發視為做空機會。<b>誠實限制</b>：多方有 S（&gt;=50）/A（&gt;=25~50）兩層分級，'
            '空方目前只有 D 一層（&lt;-25，範圍比多方任一層都寬），沒有對稱切出「強空/普通空」——'
            '這是評分公式原本的門檻設計就不對稱，不是假裝對稱又藏起這個落差。</li>'
            '<li><b>推送紀錄與績效</b>：每次進入機會候選池（多方或空方）開一筆紀錄，跌出時結案。'
            '「結果」依方向判定：做多＝現價（開放中）或結案價（已結案）≥推送價才算「上漲有效」；'
            '做空則相反，現價≤推送價才算「下跌有效」——<b>兩個方向的成功定義互為鏡像，不能只套用'
            '做多的判準</b>。「漲幅(高點)」「跌幅(低點)」不分方向，都是這段期間最高/最低價對比推送價的'
            '客觀紀錄，<b>不是進場就能拿到高點</b>。上漲/下跌有效率統計只計已結案訊號，開放中的不計入'
            '（避免拿還沒有結果的訊號灌水勝率）。<b>這是唯一能誠實驗證整套評分系統有沒有用的地方</b>：'
            '如果長期勝率接近或低於 50%，代表評分系統沒有真正的預測力，該重新檢討公式，不是繼續加指標。</li>'
            '<li><b>尚未做（誠實列出）</b>：板塊輪動（DeFi/GameFi 等資金板塊分析）需要幣種分類資料源，'
            '目前免費 API 沒有整合，屬於下一階段的候選功能，還沒做。</li>'
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
    fg = fetch_fear_greed()
    dom_set = {r["symbol"] for r in universe[:args.dom_top]}

    # prev_state 提前到評分之前載入：DOM 跨輪持續性過濾需要把上一輪的 dom 分數
    # 塞進 row 給 score_symbol 用（見該函式 do_dom 段落）
    prev_state = load_state()
    is_cold_start = not prev_state
    for r in universe:
        r["prev_dom"] = prev_state.get(r["symbol"], {}).get("dom")

    results, errors = [], 0
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(score_symbol, r, futures_syms, hl_map, r["symbol"] in dom_set): r for r in universe}
        for fut in cf.as_completed(futs):
            try:
                results.append(fut.result())
            except Exception:  # noqa: BLE001
                errors += 1

    for r in results:
        r["quality"] = quality_score(r)
        r["flow"] = flow_score(r)
        r["behavior"] = behavior_label(r)
    alerts = build_alerts(results, prev_state, is_cold_start)
    alerted_symbols = {a["symbol"] for a in alerts}
    all_transitions = build_all_transitions(results, prev_state, is_cold_start)
    overview = market_overview(results, all_transitions, fg)

    prev_series, prev_last_flows = load_majors_history()
    major_flows, new_majors_series = compute_major_flows(results, prev_series)
    major_alerts = [] if is_cold_start else build_major_alerts(major_flows, prev_last_flows)
    save_majors_history(new_majors_series, {f["base"]: f["label"] for f in major_flows})

    # 把追蹤資訊（入池時間/價、期間高低）附加回 alert 物件，供 Discord/頁面顯示
    tracking_preview = update_tracking(results, prev_state, alerted_symbols, now.isoformat())
    for a in alerts:
        a.update({k: v for k, v in tracking_preview[a["symbol"]].items()
                 if k in ("pool_entry_ts", "pool_entry_price", "high_since", "low_since", "alert_count")})
    for r in results:
        r.update({k: v for k, v in tracking_preview[r["symbol"]].items()
                 if k in ("pool_entry_ts", "pool_entry_price", "high_since", "low_since", "alert_count")})

    save_state(tracking_preview)

    signal_log = update_signal_log(results, prev_state, load_signal_log(), now.isoformat())
    save_signal_log(signal_log)
    stats = signal_stats(signal_log)

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not args.dry_run:
        send_discord(webhook, alerts)
        send_major_flow_discord(webhook, major_alerts)
        if webhook and should_send_digest(now):
            send_digest_discord(webhook, results, overview, stats, len(universe), now)
            save_digest_ts(now)

    html_out = render_page(results, alerts, all_transitions, overview, len(universe),
                           args.min_quote_vol, now, exch_counts, major_flows, signal_log, stats)
    os.makedirs(os.path.dirname(DOCS_PATH), exist_ok=True)
    with open(DOCS_PATH, "w", encoding="utf-8") as f:
        f.write(html_out)

    print(f"universe={len(universe)}(binance={exch_counts['binance']},gate={exch_counts['gate']}) "
          f"scored={len(results)} errors={errors} alerts={len(alerts)} major_alerts={len(major_alerts)} "
          f"cold_start={is_cold_start} hl_symbols={len(hl_map)} "
          f"discord={'on' if webhook else 'off(dry/no-secret)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
