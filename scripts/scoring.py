# -*- coding: utf-8 -*-
"""加密貨幣五面向評分：技術面、市場深度、籌碼面、基本面、新聞情緒。

每面向 0–100 分、50 為中性；公式全部明碼寫在本檔並同步顯示於網頁附錄。
任一面向資料源失敗 → 該面向記 None（頁面顯示「缺」），總分由其餘面向
權重重新正規化計算，絕不用假數字充數。

權重：技術 30、深度 20、籌碼 20、基本 15、新聞情緒 15。
"""
import datetime as dt
import xml.etree.ElementTree as ET

import requests

import ta_scoring as ta

TIMEOUT = 25
H = {"User-Agent": "Mozilla/5.0"}
WEIGHTS = {"tech": 30, "depth": 20, "chips": 20, "fund": 15, "news": 15}

# CoinGecko id 對照（前 15 大常客；不在表內的幣基本面記「缺」）
CG_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin",
    "XRP": "ripple", "DOGE": "dogecoin", "ADA": "cardano", "TRX": "tron",
    "LINK": "chainlink", "AVAX": "avalanche-2", "SUI": "sui", "NEAR": "near",
    "LTC": "litecoin", "DOT": "polkadot", "ZEC": "zcash", "XLM": "stellar",
    "BCH": "bitcoin-cash", "UNI": "uniswap", "SHIB": "shiba-inu", "PEPE": "pepe",
}


def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


# ---------- 技術面（輸入：build_report 的 crypto_scan 一列） ----------
def score_tech(r):
    """委派給共用模組 ta_scoring.py（均線/RSI/MACD/KD/布林/量比六指標量化合成），
    不再是這裡自己的 vs20/vs60/RSI 三段式土砲公式。ta_scoring 用 -100~100、0中性，
    這裡轉回本頁沿用的 0~100、50中性慣例（50 + total/2），維持頁面顯示不變。
    """
    ohlcv = r.get("_ohlcv")
    if not ohlcv:
        return None
    result = ta.analyze(*ohlcv)
    if result["total"] is None:
        return None
    return clamp(round(50 + result["total"] / 2))


# ---------- 市場深度（輸入：dom_latest 的一列；無 DOM 資料 → None） ----------
def score_depth(d):
    if d is None:
        return None
    s = 50
    imb = d.get("imb_10")
    if imb is not None:
        s += clamp((imb - 0.5) * 200, -25, 25)             # 當下 ±1% 失衡
    m24 = d.get("imb10_mean24h")
    if m24 is not None:
        s += clamp((m24 - 0.5) * 100, -10, 10)             # 24h 平均失衡
    sp = d.get("spread_bp")
    if sp is not None:
        s += 10 if sp < 1 else (5 if sp < 5 else (-10 if sp > 20 else 0))
    tot = (d.get("bid_usd_10") or 0) + (d.get("ask_usd_10") or 0)
    s += 5 if tot > 2e7 else (-5 if tot < 2e6 else 0)      # 簿深規模（流動性）
    return round(clamp(s))


# ---------- 籌碼面：Binance 合約 →（美國主機被擋時）OKX 備援 ----------
def _chips_binance(base):
    sym = base + "USDT"
    fr = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                      params={"symbol": sym}, headers=H, timeout=TIMEOUT).json()
    funding = float(fr["lastFundingRate"]) * 100          # % / 8h
    ls = requests.get("https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                      params={"symbol": sym, "period": "1h", "limit": 1},
                      headers=H, timeout=TIMEOUT).json()
    ratio = float(ls[0]["longShortRatio"]) if ls else None
    oi = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
                      params={"symbol": sym, "period": "1h", "limit": 25},
                      headers=H, timeout=TIMEOUT).json()
    oi_chg = ((float(oi[-1]["sumOpenInterestValue"]) / float(oi[0]["sumOpenInterestValue"])) - 1) * 100 \
        if len(oi) >= 2 else None
    return funding, ratio, oi_chg


def _chips_okx(base):
    fr = requests.get("https://www.okx.com/api/v5/public/funding-rate",
                      params={"instId": f"{base}-USDT-SWAP"}, headers=H, timeout=TIMEOUT).json()
    funding = float(fr["data"][0]["fundingRate"]) * 100
    ls = requests.get("https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio",
                      params={"ccy": base, "period": "1H"}, headers=H, timeout=TIMEOUT).json()
    ratio = float(ls["data"][0][1]) if ls.get("data") else None
    oi = requests.get("https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume",
                      params={"ccy": base, "period": "1H"}, headers=H, timeout=TIMEOUT).json()
    rows = oi.get("data") or []
    oi_chg = ((float(rows[0][1]) / float(rows[-1][1])) - 1) * 100 if len(rows) >= 2 else None
    return funding, ratio, oi_chg


def score_chips(base, chg7):
    try:
        try:
            funding, ratio, oi_chg = _chips_binance(base)
        except Exception:  # noqa: BLE001 - 美國主機常被擋，換 OKX
            funding, ratio, oi_chg = _chips_okx(base)
    except Exception:  # noqa: BLE001 - 兩邊都失敗
        return None
    s = 50
    # 資金費率（% / 8h）：小幅正值健康；過高＝多頭擁擠；深負＝空頭擁擠（反向加分）
    if funding >= 0.05:
        s -= 15
    elif funding >= 0.02:
        s -= 8
    elif funding <= -0.02:
        s += 8
    elif abs(funding) <= 0.01:
        s += 5
    # 散戶多空帳戶比：極端偏多＝反指標
    if ratio is not None:
        if ratio >= 2.5:
            s -= 10
        elif ratio <= 0.8:
            s += 8
    # 未平倉量 24h 變化 × 價格方向：同向＝趨勢有資金確認
    if oi_chg is not None:
        if oi_chg > 3:
            s += 8 if chg7 > 0 else -8
        elif oi_chg < -5:
            s -= 3
    return clamp(s)


# ---------- 基本面：CoinGecko（一次抓全部） ----------
def fetch_fundamentals(bases):
    ids = ",".join(CG_IDS[b] for b in bases if b in CG_IDS)
    if not ids:
        return {}
    j = requests.get("https://api.coingecko.com/api/v3/coins/markets",
                     params={"vs_currency": "usd", "ids": ids}, headers=H, timeout=TIMEOUT).json()
    by_id = {c["id"]: c for c in j if isinstance(c, dict) and "id" in c}
    return {b: by_id.get(CG_IDS[b]) for b in bases if b in CG_IDS}


def score_fund(c):
    if not c:
        return None
    s = 50
    rank = c.get("market_cap_rank") or 999
    s += 15 if rank <= 2 else (10 if rank <= 10 else (5 if rank <= 20 else (-10 if rank > 50 else 0)))
    mcap, vol = c.get("market_cap"), c.get("total_volume")
    if mcap and vol:
        r = vol / mcap
        s += 5 if 0.02 <= r <= 0.3 else -5     # 量/市值比：過低＝乏人問津，過高＝投機換手
    circ, mx = c.get("circulating_supply"), c.get("max_supply")
    if circ and mx:
        s += 5 if circ / mx >= 0.85 else (-5 if circ / mx < 0.5 else 0)   # 未來解鎖壓力
    ath_off = c.get("ath_change_percentage")
    if ath_off is not None:
        s += 5 if ath_off > -15 else (-5 if ath_off < -70 else 0)
    return clamp(s)


# ---------- 新聞情緒：Fear & Greed（全市場）＋ Google News 熱度 × 價格方向 ----------
def fetch_fear_greed():
    j = requests.get("https://api.alternative.me/fng/?limit=1", headers=H, timeout=TIMEOUT).json()
    return int(j["data"][0]["value"])


def news_heat(query):
    r = requests.get("https://news.google.com/rss/search",
                     params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
                     headers=H, timeout=TIMEOUT)
    root = ET.fromstring(r.content)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
    n = 0
    for item in root.iter("item"):
        pub = item.findtext("pubDate")
        try:
            ts = dt.datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=dt.timezone.utc)
            if ts >= cutoff:
                n += 1
        except Exception:  # noqa: BLE001
            continue
    return n


def score_news(fg, heat, chg7):
    if fg is None and heat is None:
        return None
    s = 50
    if fg is not None:
        if fg >= 80:
            s -= 10                            # 極度貪婪＝反指標
        elif fg <= 20:
            s += 10                            # 極度恐懼＝反指標
        elif 40 <= fg <= 70:
            s += 5                             # 情緒健康
    if heat is not None and heat >= 15:
        s += 8 if chg7 > 0 else -8             # 新聞熱度只放大現有方向，不判讀好壞
    return clamp(s)


# ---------- 總分 ----------
def total_score(sub):
    """sub: {"tech": int|None, ...}；None 的面向剔除並將權重重新正規化。"""
    avail = {k: v for k, v in sub.items() if v is not None}
    if not avail:
        return None
    w = sum(WEIGHTS[k] for k in avail)
    return round(sum(v * WEIGHTS[k] for k, v in avail.items()) / w)


def grade(total):
    if total is None:
        return "—"
    if total >= 70:
        return "偏多（強）"
    if total >= 60:
        return "偏多"
    if total > 45:
        return "中性"
    if total > 35:
        return "偏空"
    return "偏空（強）"


def score_all(crypto_rows, dom_rows, top_n=10):
    """主入口。crypto_rows：build_report.crypto_scan() 輸出；dom_rows：dom_latest() 輸出。"""
    dom_by_sym = {d["symbol"].replace("USDT", ""): d for d in dom_rows}
    rows = sorted(crypto_rows, key=lambda x: -x["qv_m"])[:top_n]
    bases = [r["sym"] for r in rows]

    try:
        fund_map = fetch_fundamentals(bases)
    except Exception:  # noqa: BLE001
        fund_map = {}
    try:
        fg = fetch_fear_greed()
    except Exception:  # noqa: BLE001
        fg = None

    out = []
    for r in rows:
        b = r["sym"]
        try:
            heat = news_heat(f"{CG_IDS.get(b, b)} crypto")
        except Exception:  # noqa: BLE001
            heat = None
        sub = {"tech": score_tech(r),
               "depth": score_depth(dom_by_sym.get(b)),
               "chips": score_chips(b, r["chg7"]),
               "fund": score_fund(fund_map.get(b)),
               "news": score_news(fg, heat, r["chg7"])}
        t = total_score(sub)
        out.append({"sym": b, "sub": sub, "total": t, "grade": grade(t),
                    "heat": heat, "close": r["close"], "chg7": r["chg7"]})
    out.sort(key=lambda x: -(x["total"] if x["total"] is not None else -1))
    return out, fg
