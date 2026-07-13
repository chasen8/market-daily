# -*- coding: utf-8 -*-
"""通用技術指標量化評分模組（獨立、無外部依賴，只用標準庫）。

== 設計目的 ==
把常見技術指標（均線、RSI、MACD、KD、布林通道、成交量）從「定性判斷」
（例如只顯示「多頭/空頭」文字、或 RSI>75 才給一個布林值）改成**連續的量化分數**，
每個子指標輸出 -100（強空）~ +100（強多）、0 為中性，可以互相比較、加權合成。

本檔不抓資料、不管交易所是誰——只吃標準 OHLCV 序列（純數字 list），輸出分數。
這樣同一套公式可以同時餵給 build_report.py（日報）、whale_scan.py（主力雷達），
未來 Phase 1 回測策略要用也能直接複製這個檔案過去，不綁定任何專案內部結構。

== 分數怎麼定義的（每個函式的 docstring 有詳細公式）==
- 均線排列（score_ma_trend）：收盤相對每條均線的距離＋均線是否呈多頭/空頭排列
- RSI（score_rsi）：不是簡單的 >75/<30 兩段式，是連續曲線，且刻意讓「極端值」
  （RSI 接近 0 或 100）從波峰往回收，反映「過熱/超賣後的反轉風險」
- MACD（score_macd）：柱狀圖方向＋擴張/收斂
- 布林通道（score_bollinger）：%B 位置，穿出軌道一樣是「先給極端分再收斂」
- KD（score_kd）：K 值位置＋黃金/死亡交叉
- 成交量（score_volume）：量比，但爆量超過 3 倍後不再線性加分（避免單一離群值主導）
- 時間序列動能（score_tsmom，2026-07-13 新增）：自身過去 N 期報酬率的 z-score
  （Moskowitz/Ooi/Pedersen 2012 Time-Series Momentum），跟 MA 都反映趨勢但計算邏輯獨立
- 波動率狀態（score_volatility_regime，2026-07-13 新增）：ATR/Close 百分位排名，
  判斷現在是低波動蓄勢還是高波動放大期，不表方向，是現有系統原本缺的維度
- ADX 趨勢強度（adx_weight_multiplier，2026-07-13 新增）：不是獨立分數項，是「趨勢類
  指標（MA/MACD）現在可不可信」的權重濾網——盤整期（ADX 低）自動降低 MA/MACD 權重，
  避免假突破雜訊主導總分

== 誠實限制 ==
這些公式是規則統計，不是學術驗證過的最佳參數；週期（14/20/26...）用業界慣用值，
沒有針對任何市場做過最適化。分數是「當下技術面狀態的量化描述」，不是預測。
TSMOM/波動率狀態/ADX 的縮放係數與 lookback 視窗都是起點參數，尚未用本專案歷史資料
回測校準，正式使用一段時間後應該檢查分數分布是否合理。
"""


def clamp(v, lo=-100, hi=100):
    return max(lo, min(hi, v))


# ---------- 基礎指標（回傳跟輸入等長的序列，資料不足處為 None）----------

def sma_full(values, period):
    n = len(values)
    out = [None] * n
    for i in range(period - 1, n):
        out[i] = sum(values[i - period + 1:i + 1]) / period
    return out


def ema_full(values, period):
    n = len(values)
    out = [None] * n
    if n < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    k = 2 / (period + 1)
    for i in range(period, n):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi_full(closes, period=14):
    """Wilder 平滑法（業界標準算法，比簡單移動平均更貼近一般看盤軟體的 RSI）。"""
    n = len(closes)
    out = [None] * n
    if n < period + 1:
        return out
    deltas = [closes[i] - closes[i - 1] for i in range(1, n)]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def macd_full(closes, fast=12, slow=26, signal=9):
    ema_fast, ema_slow = ema_full(closes, fast), ema_full(closes, slow)
    macd_line = [(a - b) if (a is not None and b is not None) else None
                 for a, b in zip(ema_fast, ema_slow)]
    valid = [v for v in macd_line if v is not None]
    signal_line = [None] * len(closes)
    if len(valid) >= signal:
        sig_partial = ema_full(valid, signal)
        first_idx = next(i for i, v in enumerate(macd_line) if v is not None)
        for i, v in enumerate(sig_partial):
            signal_line[first_idx + i] = v
    hist = [(m - s) if (m is not None and s is not None) else None
            for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, hist


def bollinger_full(closes, period=20, num_std=2):
    n = len(closes)
    mid = sma_full(closes, period)
    upper, lower, pct_b = [None] * n, [None] * n, [None] * n
    for i in range(period - 1, n):
        window = closes[i - period + 1:i + 1]
        m = mid[i]
        var = sum((x - m) ** 2 for x in window) / period
        std = var ** 0.5
        upper[i] = m + num_std * std
        lower[i] = m - num_std * std
        rng = upper[i] - lower[i]
        pct_b[i] = (closes[i] - lower[i]) / rng if rng > 0 else 0.5
    return upper, mid, lower, pct_b


def kd_full(highs, lows, closes, period=9, k_smooth=3, d_smooth=3):
    n = len(closes)
    rsv = [None] * n
    for i in range(period - 1, n):
        hh, ll = max(highs[i - period + 1:i + 1]), min(lows[i - period + 1:i + 1])
        rsv[i] = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100
    k, d = [None] * n, [None] * n
    prev_k = prev_d = 50.0
    for i in range(n):
        if rsv[i] is None:
            continue
        prev_k = (prev_k * (k_smooth - 1) + rsv[i]) / k_smooth
        k[i] = prev_k
        prev_d = (prev_d * (d_smooth - 1) + prev_k) / d_smooth
        d[i] = prev_d
    return k, d


def volume_ratio(volumes, short=5, long=20):
    if len(volumes) < long or sum(volumes[-long:]) == 0:
        return None
    return (sum(volumes[-short:]) / short) / (sum(volumes[-long:]) / long)


def true_range_full(highs, lows, closes):
    n = len(closes)
    out = [None] * n
    if n < 2:
        return out
    out[0] = highs[0] - lows[0]
    for i in range(1, n):
        out[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    return out


def atr_full(highs, lows, closes, period=14):
    """Wilder 平滑（跟 rsi_full 同一種平滑法），回傳跟輸入等長的 ATR 序列。"""
    tr = true_range_full(highs, lows, closes)
    n = len(closes)
    out = [None] * n
    valid_tr = [v for v in tr if v is not None]
    if len(valid_tr) < period:
        return out
    first_idx = period  # tr[0] 是用 high-low 湊的、不是真正的 TR，跳過從 tr[1:period+1] 取種子
    if n <= first_idx:
        return out
    seed = sum(tr[1:period + 1]) / period
    out[first_idx] = seed
    prev = seed
    for i in range(first_idx + 1, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def adx_full(highs, lows, closes, period=14):
    """Wilder DMI/ADX 原始公式。回傳跟輸入等長的 ADX 序列（0~100，只表強度不表方向）。"""
    n = len(closes)
    out = [None] * n
    if n < period * 2:
        return out
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
    tr = true_range_full(highs, lows, closes)

    def wilder_smooth(series, start):
        sm = [None] * n
        seed = sum(series[start - period + 1:start + 1])
        sm[start] = seed
        prev = seed
        for i in range(start + 1, n):
            prev = prev - prev / period + series[i]
            sm[i] = prev
        return sm

    start = period
    tr_sm = wilder_smooth(tr, start)
    plus_sm = wilder_smooth(plus_dm, start)
    minus_sm = wilder_smooth(minus_dm, start)

    dx = [None] * n
    for i in range(start, n):
        if not tr_sm[i]:
            continue
        pdi = plus_sm[i] / tr_sm[i] * 100
        mdi = minus_sm[i] / tr_sm[i] * 100
        denom = pdi + mdi
        dx[i] = abs(pdi - mdi) / denom * 100 if denom > 1e-9 else 0.0

    dx_valid_start = next((i for i in range(start, n) if dx[i] is not None), None)
    if dx_valid_start is None or n - dx_valid_start < period:
        return out
    adx_seed = sum(dx[dx_valid_start:dx_valid_start + period]) / period
    seed_idx = dx_valid_start + period - 1
    out[seed_idx] = adx_seed
    prev = adx_seed
    for i in range(seed_idx + 1, n):
        if dx[i] is None:
            continue
        prev = (prev * (period - 1) + dx[i]) / period
        out[i] = prev
    return out


def percentile_rank(value, history):
    """value 在 history 這個歷史分布裡的百分位（0~100）。history 不含 value 本身。
    相等值算一半權重（below + 0.5*equal），避免資料剛好有大量重複值時
    （例如低波動時 ATR% 幾乎不變）百分位被浮點捨入方向隨機推到 0 或 100 這種極端值。
    """
    if not history:
        return None
    below = sum(1 for v in history if v < value)
    equal = sum(1 for v in history if v == value)
    return (below + 0.5 * equal) / len(history) * 100


# ---------- 量化評分（每個都是 -100~100，0=中性）----------

def _interp(x, pts):
    """分段線性內插：pts=[(x0,y0),(x1,y1),...]，x 由小到大排列。"""
    x = clamp(x, pts[0][0], pts[-1][0])
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        if x0 <= x <= x1:
            t = (x - x0) / (x1 - x0) if x1 != x0 else 0
            return round(y0 + t * (y1 - y0))
    return 0


def score_ma_trend(close, ma_list):
    """ma_list：[(名稱, 值), ...]，由短週期到長週期排列，例如 [("MA5",v),("MA20",v),("MA60",v)]。
    每條均線：收盤在其上方給正分、下方給負分（依偏離幅度漸增，但每條均線的配額封頂，
    避免單一均線的極端偏離主導總分）；均線呈多頭排列（短>中>長）額外 +20，
    空頭排列（短<中<長）額外 -20。
    """
    valid = [(n, v) for n, v in ma_list if v is not None and v > 0]
    if not valid:
        return None
    per = 80 / len(valid)
    s = 0.0
    for _, v in valid:
        pct = (close - v) / v
        s += clamp(pct * 1000, -per, per)
    vals = [v for _, v in valid]
    if len(vals) >= 2:
        if all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1)):
            s += 20
        elif all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)):
            s -= 20
    return clamp(round(s))


def score_rsi(rsi_value):
    """連續曲線，關鍵點：RSI 50=中性(0)；50→70 漸強、70→85 動能區加碼到滿分；
    超過 85 開始從波峰回落（過熱、追高風險升高，見誠實限制）；
    下半部對稱：50→30 漸弱、30→15 加碼看空，低於 15 從谷底回升（超賣反彈風險）。
    """
    if rsi_value is None:
        return None
    pts = [(0, -40), (15, -100), (30, -60), (50, 0), (70, 60), (85, 100), (100, 40)]
    return _interp(rsi_value, pts)


def score_macd(hist, hist_prev, price_ref=None):
    """柱狀圖方向給基礎分（正=多頭動能、負=空頭動能），
    相對前一根擴張＝動能增強再加碼、收斂＝動能減弱先扣一點（但不翻方向）。
    price_ref 用來判斷「柱狀圖是不是其實接近 0」——完美等速趨勢下柱狀圖會收斂到
    接近 0 但因浮點運算可能出現 -1e-15 這種雜訊值，不加容忍區間會被誤判成有方向。
    """
    if hist is None:
        return None
    eps = abs(price_ref) * 1e-8 if price_ref else 1e-9
    if abs(hist) < eps:
        return 0
    base = 60 if hist > 0 else -60
    if hist_prev is not None and abs(hist_prev) >= eps:
        expanding = abs(hist) > abs(hist_prev)
        base += (20 if expanding else -20) * (1 if hist > 0 else -1)
    return clamp(round(base))


def score_bollinger(pct_b):
    """%B：0=觸及下軌、0.5=中軌、1=觸及上軌，線性映射到 -100~100；
    穿出軌道外（%B<0 或 >1）代表波動加劇，用跟 RSI 一樣的「先給極端分、深度突破後回落」
    處理，反映單邊噴出後的均值回歸風險。
    """
    if pct_b is None:
        return None
    pts = [(-0.3, -20), (0.0, -100), (0.5, 0), (1.0, 100), (1.3, 20)]
    return _interp(pct_b, pts)


def score_kd(k, d):
    """K 值本身線性映射（0~100 對應 -100~100，50 為中性）；
    K 在 D 之上（偏黃金交叉狀態）加 15、K 在 D 之下（偏死亡交叉狀態）減 15。
    """
    if k is None:
        return None
    s = (k - 50) * 2
    if d is not None:
        s += 15 if k > d else -15
    return clamp(round(s))


def score_volume(vol_ratio_value):
    """量比＝近 5 期均量 ÷ 近 20 期均量，1.0 為中性。1~3 倍線性加分到滿分；
    超過 3 倍不再隨倍數線性增加（避免單筆爆量的極端值主導分數）；
    低於 1 倍等比例扣分，量趨近於 0 時逼近 -100。
    """
    if vol_ratio_value is None:
        return None
    r = vol_ratio_value
    if r >= 3.0:
        return 60
    if r >= 1.0:
        return round((r - 1.0) / 2.0 * 100)
    return round(clamp((r - 1.0) * 100))


def score_tsmom(closes, n=20, lookback=100):
    """時間序列動能（Time-Series Momentum，Moskowitz/Ooi/Pedersen 2012）：
    自身過去 N 期報酬率，用該幣種自己過去 lookback 期的「N 期報酬率分布」做 z-score，
    而非固定百分比門檻——同一個 5% 漲幅對低波動幣是極端事件、對高波動幣只是日常雜訊，
    用自身歷史分布相對化才公平（跟本專案 DOM/鯨魚門檻比例化是同一個教訓）。
    """
    need = n + lookback + 1
    if len(closes) < need:
        return None
    returns_hist = []
    for i in range(len(closes) - lookback, len(closes)):
        base = closes[i - n]
        if base:
            returns_hist.append(closes[i] / base - 1)
    if len(returns_hist) < 2:
        return None
    r_now = returns_hist[-1]
    hist = returns_hist[:-1]
    mean = sum(hist) / len(hist)
    var = sum((x - mean) ** 2 for x in hist) / len(hist)
    std = var ** 0.5
    if std < 1e-12:
        return 0
    z = (r_now - mean) / std
    return clamp(round(z * 35))


def score_volatility_regime(highs, lows, closes, period=14, lookback=100):
    """波動率狀態（ATR 百分位排名）：用 ATR/Close（比例化，避免高價幣天然 ATR
    絕對值大而被誤判為高波動）在自身近 lookback 期分布中的百分位，>50 代表波動放大、
    <50 代表波動收斂。這不是方向分數，只是「現在該不該信任趨勢類指標」的狀態描述，
    分數本身跟漲跌方向無關，正負只表示「波動偏高/偏低」。
    """
    atr = atr_full(highs, lows, closes, period)
    n = len(closes)
    atr_pct = [(atr[i] / closes[i]) if (atr[i] is not None and closes[i]) else None for i in range(n)]
    valid_idx = [i for i in range(n) if atr_pct[i] is not None]
    if len(valid_idx) < lookback + 1:
        return None
    window = valid_idx[-lookback - 1:]
    now_i = window[-1]
    hist = [atr_pct[i] for i in window[:-1]]
    pct = percentile_rank(atr_pct[now_i], hist)
    if pct is None:
        return None
    return clamp(round((pct - 50) * 2))


def adx_weight_multiplier(adx_value):
    """ADX 不是獨立分數項，是「趨勢類指標(ma/macd)可不可信」的權重調節濾網
    （Wilder 原始建議：ADX<20 判定無明顯趨勢，>25 判定有趨勢）。回傳 0.5~1.5，
    ADX 缺值時回傳 1.0（不調整，等同沒有這個濾網時的行為，向後相容）。
    """
    if adx_value is None:
        return 1.0
    return clamp(adx_value / 25, 0.5, 1.5)


WEIGHTS = {"ma": 30, "rsi": 20, "macd": 20, "boll": 15, "kd": 10, "volume": 5,
           "tsmom": 15, "vol_regime": 10}


def composite_score(subs, weight_multipliers=None):
    """子分數的加權平均；可得子分數越少，總分越保守打折（跟本專案其他評分模組同一套慣例）。
    weight_multipliers：可選的 {key: multiplier}，用來讓 ADX 這類「濾網型」指標調整
    ma/macd 的實際權重，而不是直接把 ADX 當成第 7 個獨立分數項疊加（見 adx_weight_multiplier）。
    """
    avail = {k: v for k, v in subs.items() if v is not None}
    if not avail:
        return None
    weights = dict(WEIGHTS)
    if weight_multipliers:
        for k, m in weight_multipliers.items():
            if k in weights:
                weights[k] = weights[k] * m
    w = sum(weights[k] for k in avail)
    raw = sum(v * weights[k] for k, v in avail.items()) / w
    discount = 1.0 if len(avail) >= 4 else (0.8 if len(avail) >= 2 else 0.5)
    return clamp(round(raw * discount))


def grade_of(total):
    if total is None:
        return "—"
    if total >= 50:
        return "強多"
    if total >= 20:
        return "偏多"
    if total > -20:
        return "中性"
    if total > -50:
        return "偏空"
    return "強空"


def analyze(highs, lows, closes, volumes, ma_periods=(5, 20, 60)):
    """主入口：輸入完整 OHLCV 序列（由舊到新排序），回傳最新一根的完整技術分析結果。
    至少需要約 max(ma_periods)+10 根資料才會有完整分數，資料不足的子項回傳 None。
    TSMOM/波動率狀態需要更長歷史（n+lookback+1，預設約 121 根），資料不足時這兩項
    直接回傳 None、不影響其餘子項，composite_score 會照可得子項數量自動打折。
    """
    mas = [(f"MA{p}", sma_full(closes, p)[-1]) for p in ma_periods]
    rsi_series = rsi_full(closes)
    macd_line, signal_line, hist = macd_full(closes)
    _, _, _, pct_b_series = bollinger_full(closes)
    k_series, d_series = kd_full(highs, lows, closes)
    vr = volume_ratio(volumes)
    adx_series = adx_full(highs, lows, closes)

    hist_now = hist[-1]
    hist_prev = hist[-2] if len(hist) >= 2 else None
    adx_now = adx_series[-1]

    subs = {
        "ma": score_ma_trend(closes[-1], mas),
        "rsi": score_rsi(rsi_series[-1]),
        "macd": score_macd(hist_now, hist_prev, price_ref=closes[-1]),
        "boll": score_bollinger(pct_b_series[-1]),
        "kd": score_kd(k_series[-1], d_series[-1]),
        "volume": score_volume(vr),
        "tsmom": score_tsmom(closes),
        "vol_regime": score_volatility_regime(highs, lows, closes),
    }
    trend_mult = adx_weight_multiplier(adx_now)
    total = composite_score(subs, weight_multipliers={"ma": trend_mult, "macd": trend_mult})
    return {
        "sub": subs,
        "total": total,
        "grade": grade_of(total),
        "raw": {"rsi": rsi_series[-1], "macd_hist": hist_now, "pct_b": pct_b_series[-1],
                "k": k_series[-1], "d": d_series[-1], "vol_ratio": vr, "adx": adx_now,
                **{name: v for name, v in mas}},
    }
