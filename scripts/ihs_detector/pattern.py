# -*- coding: utf-8 -*-
"""
pattern.py

第二～八步：組合候選頭肩底結構、計算頸線、判斷 candidate / breakout 模式、
成交量篩選。核心函式：
- calculate_neckline(N1, N2, angle_scale)
- detect_inverse_head_shoulders(df, timeframe, config, symbol=None)

全部為純數學規則（pivot 比較、代數公式），不使用 LLM／圖像辨識／主觀判斷。
"""
from __future__ import annotations

import math
from typing import Callable, Optional

import numpy as np
import pandas as pd

from .config import IHSConfig
from .swing import find_swing_points, extract_swing_points
from .scoring import calculate_pattern_score


class Neckline:
    """頸線：由 N1（左頸線高點）與 N2（右頸線高點）決定的線性內插/外插線。"""

    __slots__ = ("N1", "N2", "bars_between", "slope_pct_per_bar", "angle_deg", "price_at")

    def __init__(self, N1: dict, N2: dict, bars_between: int,
                 slope_pct_per_bar: float, angle_deg: float,
                 price_at: Callable[[int], float]):
        self.N1 = N1
        self.N2 = N2
        self.bars_between = bars_between
        self.slope_pct_per_bar = slope_pct_per_bar
        self.angle_deg = angle_deg
        self.price_at = price_at


def calculate_neckline(N1: dict, N2: dict, angle_scale: float = 100.0) -> Neckline:
    """
    第五、六步：頸線角度與頸線價格函式。

    N1, N2: {"index": int, "price": float, ...}  -- price 為該根 K 線的 high。

    公式（spec 第五、六步，逐字對應）：
        bars_between = N2.index - N1.index
        neckline_slope_pct_per_bar = ((N2.high - N1.high) / N1.high) / bars_between
        neckline_angle = atan(neckline_slope_pct_per_bar * angle_scale) * 180 / pi
        neckline_price_at(i) = N1.high + (N2.high - N1.high) * ((i - N1.index) / (N2.index - N1.index))
    """
    bars_between = N2["index"] - N1["index"]
    if bars_between <= 0:
        raise ValueError("N2.index 必須大於 N1.index")

    n1_high = N1["price"]
    n2_high = N2["price"]

    slope_pct_per_bar = ((n2_high - n1_high) / n1_high) / bars_between
    angle_deg = math.atan(slope_pct_per_bar * angle_scale) * 180.0 / math.pi

    def price_at(i: int) -> float:
        return n1_high + (n2_high - n1_high) * ((i - N1["index"]) / bars_between)

    return Neckline(N1, N2, bars_between, slope_pct_per_bar, angle_deg, price_at)


def _best_swing_high_in_range(swing_highs: list[dict], lo_index: int, hi_index: int) -> Optional[dict]:
    """
    在 (lo_index, hi_index) 開區間內，取價格（high）最高的 swing high 當頸線點。

    設計決策（見 docs/project-charter.md）：規格第二步沒有明講 N1/N2 是否要限定
    「LS-H 之間 / H-RS 之間唯一一個代表點」還是任意 swing high 都能組出一個候選。
    實測發現「任意組合」在真實資料上會讓同一組 (LS,H,RS) 骨架因為 N1/N2 換不同
    swing high 而重複產生大量本質相同的候選（AAPL 300 根日線曾一次跑出 1070 筆，
    去重後只有 69 組不同骨架）。改為「該區間內最高的那個 swing high」後，
    每組 (LS,H,RS) 只會產生一筆候選，符合「頸線＝兩肩之間最明顯高點」的直覺定義，
    同時把複雜度從 O(lows^3 * highs^2) 降到 O(lows^3)。
    """
    best = None
    for sh in swing_highs:
        if sh["index"] <= lo_index or sh["index"] >= hi_index:
            continue
        if best is None or sh["price"] > best["price"]:
            best = sh
    return best


def _average_volume(volume: np.ndarray, upto_index_exclusive: int, window: int) -> Optional[float]:
    """average_volume_20：往前取 window 根（不含 upto_index_exclusive 當根）的平均量。"""
    lo = max(0, upto_index_exclusive - window)
    seg = volume[lo:upto_index_exclusive]
    if len(seg) == 0:
        return None
    return float(seg.mean())


def _find_breakout(df: pd.DataFrame, neckline: Neckline, rs_index: int,
                    breakout_buffer: float, enable_volume_filter: bool,
                    min_volume_ratio: float, volume_avg_window: int):
    """
    第七、八步：在 RS 之後尋找第一根滿足突破（且若啟用則同時滿足量能）條件的 K 線。

    回傳 (breakout_time, breakout_price, breakout_index, volume_ratio) 或
    (None, None, None, None) 表示未找到有效突破。
    """
    close = df["close"].to_numpy(dtype=float)
    volume = df["volume"].to_numpy(dtype=float) if "volume" in df.columns else None
    time_col = "timestamp" if "timestamp" in df.columns else None
    n = len(df)

    for i in range(rs_index + 1, n):
        neckline_price = neckline.price_at(i)
        if close[i] > neckline_price * (1 + breakout_buffer):
            volume_ratio = None
            if enable_volume_filter:
                avg_vol = _average_volume(volume, i, volume_avg_window) if volume is not None else None
                if avg_vol is None or avg_vol == 0:
                    continue  # 資料不足以判斷量能，跳過這根，繼續找下一根
                volume_ratio = float(volume[i]) / avg_vol
                if volume_ratio < min_volume_ratio:
                    continue  # 突破但量能不足，不算有效突破，繼續往後找
            breakout_time = df[time_col].iloc[i] if time_col else i
            return breakout_time, float(close[i]), i, volume_ratio

    return None, None, None, None


def detect_inverse_head_shoulders(df: pd.DataFrame, timeframe: str, config: IHSConfig,
                                   symbol: Optional[str] = None) -> list[dict]:
    """
    第二～九步主流程：從單一 symbol/timeframe 的 OHLCV DataFrame 找出所有符合
    條件的頭肩底候選（candidate 或 breakout 模式），並算出 pattern_score。

    df 需含欄位：timestamp, open, high, low, close, volume，並以時間排序、
    重設為連續整數索引（0..n-1），否則 index 比較會不正確。

    回傳：list of dict，欄位對應 spec 第十步「輸出欄位」。
    """
    if timeframe not in config.pivot_window_by_timeframe:
        raise ValueError(f"不支援的 timeframe: {timeframe}")

    df = df.reset_index(drop=True)
    swings_df = find_swing_points(df, config.pivot_window(timeframe))
    swing_lows, swing_highs = extract_swing_points(swings_df)

    shoulder_diff_limit = config.shoulder_diff_limit(timeframe)
    min_head_depth = config.min_head_depth(timeframe)
    max_angle = config.max_neckline_angle
    breakout_buffer = config.breakout_buffer(timeframe)

    close = df["close"].to_numpy(dtype=float)
    last_index = len(df) - 1
    time_col = "timestamp" if "timestamp" in df.columns else None

    results: list[dict] = []

    # 第二步：組合候選結構 LS -> H -> RS（依 index 時間序）。
    # N1、N2 不再窮舉所有 swing high 組合，而是分別取 LS-H 之間、H-RS 之間
    # 「最高」的那個 swing high 當頸線點（見 _best_swing_high_in_range 說明），
    # 讓每組 (LS, H, RS) 骨架只產生一筆候選，避免重複爆炸。
    for LS in swing_lows:
        for H in swing_lows:
            if H["index"] <= LS["index"]:
                continue
            if not (H["price"] < LS["price"]):
                continue  # 條件：H.low < LS.low

            N1 = _best_swing_high_in_range(swing_highs, LS["index"], H["index"])
            if N1 is None:
                continue

            for RS in swing_lows:
                if RS["index"] <= H["index"]:
                    continue
                if not (H["price"] < RS["price"]):
                    continue  # 條件：H.low < RS.low

                N2 = _best_swing_high_in_range(swing_highs, H["index"], RS["index"])
                if N2 is None:
                    continue

                # 第三步：左右肩差異率
                shoulder_diff = abs(LS["price"] - RS["price"]) / ((LS["price"] + RS["price"]) / 2)
                if shoulder_diff > shoulder_diff_limit:
                    continue

                # 第四步：頭部深度
                shoulder_avg = (LS["price"] + RS["price"]) / 2
                head_depth = (shoulder_avg - H["price"]) / shoulder_avg
                if not (head_depth > 0 and head_depth >= min_head_depth):
                    continue

                # 第五步：頸線角度
                neckline = calculate_neckline(N1, N2, config.angle_scale)
                if abs(neckline.angle_deg) > max_angle:
                    continue

                # ---- 通過 Mode A candidate 全部條件 ----
                current_close = close[last_index]
                current_neckline_price = neckline.price_at(last_index)
                distance_to_neckline = abs(current_close - current_neckline_price) / current_neckline_price

                mode = "candidate"
                breakout_detected = False
                breakout_time = None
                breakout_price = None
                volume_ratio = None

                if config.enable_breakout_filter:
                    b_time, b_price, b_index, v_ratio = _find_breakout(
                        df, neckline, RS["index"], breakout_buffer,
                        config.enable_volume_filter, config.min_volume_ratio,
                        config.volume_avg_window,
                    )
                    if b_time is not None:
                        mode = "breakout"
                        breakout_detected = True
                        breakout_time = b_time
                        breakout_price = b_price
                        volume_ratio = v_ratio

                score = calculate_pattern_score(
                    shoulder_diff=shoulder_diff,
                    head_depth=head_depth,
                    neckline_angle=neckline.angle_deg,
                    breakout_detected=breakout_detected,
                    distance_to_neckline=distance_to_neckline,
                    config=config,
                    timeframe=timeframe,
                )
                pattern_score = score["pattern_score"]

                if pattern_score < config.min_pattern_score:
                    continue

                results.append({
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "pattern_type": "inverse_head_and_shoulders",
                    "mode": mode,
                    "pattern_score": pattern_score,
                    "LS_time": LS["time"], "LS_price": LS["price"],
                    "N1_time": N1["time"], "N1_price": N1["price"],
                    "H_time": H["time"], "H_price": H["price"],
                    "N2_time": N2["time"], "N2_price": N2["price"],
                    "RS_time": RS["time"], "RS_price": RS["price"],
                    "shoulder_diff": shoulder_diff,
                    "head_depth": head_depth,
                    "neckline_angle": neckline.angle_deg,
                    "neckline_slope_pct_per_bar": neckline.slope_pct_per_bar,
                    "current_close": current_close,
                    "current_neckline_price": current_neckline_price,
                    "distance_to_neckline": distance_to_neckline,
                    "breakout_detected": breakout_detected,
                    "breakout_time": breakout_time,
                    "breakout_price": breakout_price,
                    "volume_ratio": volume_ratio,
                    # 附加除錯/可追溯欄位（非 spec 必要欄位，但不影響驗收）
                    "_score_components": score,
                })

    return results
