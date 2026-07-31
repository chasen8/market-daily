# -*- coding: utf-8 -*-
"""
pattern.py

第二～八步：組合候選頭肩結構、計算頸線、判斷 candidate / breakout 模式、
成交量篩選。核心函式：
- calculate_neckline(N1, N2, angle_scale)
- detect_head_shoulders(df, timeframe, config, direction, symbol=None)
- detect_inverse_head_shoulders(...)  頭肩底（direction="bottom"）的相容包裝
- detect_head_and_shoulders_top(...)  頭肩頂（direction="top"）

2026-07-30：同一份主流程同時支援頭肩底與頭肩頂，兩者的方向差異全部集中在
direction.py，避免複製兩份程式碼造成漂移。

全部為純數學規則（pivot 比較、代數公式），不使用 LLM／圖像辨識／主觀判斷。
"""
from __future__ import annotations

import math
from typing import Callable, Optional

import numpy as np
import pandas as pd

from . import direction as dirmod
from . import volume_stages
from .config import IHSConfig, BULKOWSKI_STATS
from .indicators import atr, atr_at
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


def _neckline_candidates(neckline_pivots: list[dict], lo_index: int, hi_index: int,
                          prominence_ratio: float, direction: str) -> list[dict]:
    """
    2026-07-28 使用者要求「頸線斜率越接近零越優先」後新增；2026-07-30 支援雙方向。

    回傳 (lo_index, hi_index) 開區間內「夠顯著」的頸線點候選。只回傳最極端那一點
    （舊行為）會強迫頸線通過兩個未必等高的點、斜率無從選擇；但若完全不設門檻，
    演算法會為了追求水平而選到區間裡不顯著的小高/低點，畫出一條沒有意義的
    「平頸線」。這個門檻是兩者的折衷：先篩掉不夠顯著的，再由呼叫端在剩下的
    組合裡挑最平的。方向差異見 direction.pick_neckline_candidates。
    """
    return dirmod.pick_neckline_candidates(
        neckline_pivots, lo_index, hi_index, prominence_ratio, direction)


def _neckline_untouched(df: pd.DataFrame, neckline: "Neckline", ls_index: int,
                         rs_index: int, direction: str) -> bool:
    """
    2026-07-24 使用者依實際圖表案例（BASUSDT）新增的驗證規則：頸線區間內，
    除了左右肩本身，任何一根 K 線都不能穿越當時的頸線內插價，否則整組候選作廢。

    頭肩底：頸線是天花板，high 不得超過。
    頭肩頂：頸線是地板，low 不得跌破（2026-07-30 依使用者要求補上鏡像規則）。

    N1、N2 本身在頸線上，內插價剛好等於自己的價格，天生不會觸發；
    只需排除 LS、RS 兩根本身。
    """
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    for i in range(ls_index, rs_index + 1):
        if i == ls_index or i == rs_index:
            continue
        if dirmod.neckline_pierced(high[i], low[i], neckline.price_at(i), direction):
            return False
    return True


def _average_volume(volume: np.ndarray, upto_index_exclusive: int, window: int) -> Optional[float]:
    """average_volume_20：往前取 window 根（不含 upto_index_exclusive 當根）的平均量。"""
    lo = max(0, upto_index_exclusive - window)
    seg = volume[lo:upto_index_exclusive]
    if len(seg) == 0:
        return None
    return float(seg.mean())


def _find_breakout(df: pd.DataFrame, neckline: Neckline, rs_index: int,
                    breakout_buffer: float, enable_volume_filter: bool,
                    min_volume_ratio: float, volume_avg_window: int,
                    direction: str = dirmod.BOTTOM):
    """
    第七、八步：在 RS 之後尋找第一根滿足突破（且若啟用則同時滿足量能）條件的 K 線。
    頭肩底要收盤站上頸線，頭肩頂要收盤跌破頸線（見 direction.is_broken_out）。

    回傳 (breakout_time, breakout_price, breakout_index, volume_ratio) 或
    (None, None, None, None) 表示未找到有效突破。
    """
    close = df["close"].to_numpy(dtype=float)
    volume = df["volume"].to_numpy(dtype=float) if "volume" in df.columns else None
    time_col = "timestamp" if "timestamp" in df.columns else None
    n = len(df)

    for i in range(rs_index + 1, n):
        neckline_price = neckline.price_at(i)
        if dirmod.is_broken_out(close[i], neckline_price, breakout_buffer, direction):
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


def detect_head_shoulders(df: pd.DataFrame, timeframe: str, config: IHSConfig,
                           direction: str = dirmod.BOTTOM,
                           symbol: Optional[str] = None) -> list[dict]:
    """
    第二～九步主流程：從單一 symbol/timeframe 的 OHLCV DataFrame 找出所有符合
    條件的頭肩型態候選（candidate 或 breakout 模式），並算出 pattern_score。

    direction="bottom" -> 頭肩底（肩/頭是低點，頸線在上方，站上頸線為突破）
    direction="top"    -> 頭肩頂（肩/頭是高點，頸線在下方，跌破頸線為突破）

    df 需含欄位：timestamp, open, high, low, close, volume，並以時間排序、
    重設為連續整數索引（0..n-1），否則 index 比較會不正確。

    回傳：list of dict，欄位對應 spec 第十步「輸出欄位」。
    """
    dirmod.validate(direction)
    if timeframe not in config.pivot_window_by_timeframe:
        raise ValueError(f"不支援的 timeframe: {timeframe}")

    df = df.reset_index(drop=True)
    swings_df = find_swing_points(df, config.pivot_window(timeframe))
    swing_lows, swing_highs = extract_swing_points(swings_df)

    # 肩/頭與頸線各自從哪一組 pivot 挑，由方向決定
    shoulder_pool = dirmod.shoulder_pivots(swing_lows, swing_highs, direction)
    neckline_pool = dirmod.neckline_pivots(swing_lows, swing_highs, direction)

    # 肩部對稱性模式：strict 沿用既有嚴格門檻；textbook 依 ChartSchool
    # 「symmetry preferred but not required」放寬（見 config 說明）
    textbook = config.shoulder_symmetry_mode == "textbook"
    shoulder_diff_limit = (config.textbook_shoulder_diff_limit if textbook
                           else config.shoulder_diff_limit(timeframe))
    min_head_depth = config.min_head_depth(timeframe)
    max_angle = config.max_neckline_angle
    breakout_buffer = config.breakout_buffer(timeframe)

    close = df["close"].to_numpy(dtype=float)
    volume_arr = df["volume"].to_numpy(dtype=float) if "volume" in df.columns else None
    last_index = len(df) - 1
    time_col = "timestamp" if "timestamp" in df.columns else None

    # ATR 相對門檻（2026-07-30）：固定百分比在不同標的/週期上意義差很多，
    # 改用「幾倍 ATR」讓門檻自動適應波動。詳見 indicators.py 檔頭。
    atr_values = atr(df, config.atr_period) if config.use_atr_thresholds else None

    results: list[dict] = []

    # 第二步：組合候選結構 LS -> H -> RS（依 index 時間序）。
    # N1、N2 從「夠顯著」的頸線點候選裡挑斜率最接近水平的一組，
    # 讓每組 (LS, H, RS) 骨架只產生一筆候選，避免重複爆炸。
    for LS in shoulder_pool:
        for H in shoulder_pool:
            if H["index"] <= LS["index"]:
                continue
            if not dirmod.head_is_beyond(H["price"], LS["price"], direction):
                continue  # 頭部要比左肩更極端

            n1_candidates = _neckline_candidates(
                neckline_pool, LS["index"], H["index"],
                config.neckline_prominence_ratio, direction)
            if not n1_candidates:
                continue

            for RS in shoulder_pool:
                if RS["index"] <= H["index"]:
                    continue
                if not dirmod.head_is_beyond(H["price"], RS["price"], direction):
                    continue  # 頭部要比右肩更極端

                n2_candidates = _neckline_candidates(
                    neckline_pool, H["index"], RS["index"],
                    config.neckline_prominence_ratio, direction)
                if not n2_candidates:
                    continue

                # 第三步：左右肩差異。
                # 2026-07-30 修正：預設**不再**用「兩肩絕對價位差」當門檻。
                # 頸線傾斜時，兩肩要維持等比例幅度就必然有價差，用絕對價位當門檻
                # 等於把所有斜頸線的頭肩型態砍光（POWERUSDT 實例見 config 說明）。
                # 真正的對稱判準移到下方頸線迴圈裡的「幅度比」（ls_amp vs rs_amp）。
                shoulder_diff = abs(LS["price"] - RS["price"]) / ((LS["price"] + RS["price"]) / 2)
                shoulder_avg = (LS["price"] + RS["price"]) / 2
                head_depth = dirmod.head_depth(shoulder_avg, H["price"], direction)
                check_absolute = config.shoulder_symmetry_basis in ("absolute", "both")

                # ATR 模式：門檻改用「幾倍 ATR」；ATR 取不到（暖身期）時退回百分比
                bar_atr = atr_at(atr_values, RS["index"]) if atr_values is not None else None
                if bar_atr:
                    if check_absolute and abs(LS["price"] - RS["price"]) > config.max_shoulder_diff_atr * bar_atr:
                        continue
                    if abs(H["price"] - shoulder_avg) < config.min_head_atr * bar_atr:
                        continue
                    if head_depth <= 0:
                        continue
                else:
                    if check_absolute and shoulder_diff > shoulder_diff_limit:
                        continue
                    if not (head_depth > 0 and head_depth >= min_head_depth):
                        continue

                # 第五步：從候選頸線點組合中挑出「斜率最接近水平」且通過全部檢查的一組
                # （2026-07-28 使用者要求：頸線斜率越接近零越優先）。
                # 注意：頭肩頂雖然「下斜頸線更看空」，但那是**分數**上的加分，
                # 選點仍然偏好接近水平，避免為了追斜率選到怪異的頸線點。
                tol = config.shoulder_amplitude_tolerance
                N1 = None
                N2 = None
                neckline = None
                ls_amplitude = None
                rs_amplitude = None
                for c1 in n1_candidates:
                    for c2 in n2_candidates:
                        # 右肩幅度需落在左肩幅度 ±tol 內（相對容忍，見 charter 決策記錄）。
                        # 幅度是相對各自的頸線點算的，所以要在頸線組合迴圈裡逐組驗證。
                        # textbook 模式明講「symmetry not required」，故跳過這項。
                        ls_amp = dirmod.shoulder_amplitude(c1["price"], LS["price"], direction)
                        rs_amp = dirmod.shoulder_amplitude(c2["price"], RS["price"], direction)
                        if ls_amp <= 0 or rs_amp <= 0:
                            continue
                        if not textbook:
                            if not (ls_amp * (1 - tol) <= rs_amp <= ls_amp * (1 + tol)):
                                continue
                        nl = calculate_neckline(c1, c2, config.angle_scale)
                        if abs(nl.angle_deg) > max_angle:
                            continue
                        if config.require_neckline_untouched and not _neckline_untouched(
                                df, nl, LS["index"], RS["index"], direction):
                            continue
                        if neckline is None or abs(nl.angle_deg) < abs(neckline.angle_deg):
                            N1, N2, neckline = c1, c2, nl
                            ls_amplitude, rs_amplitude = ls_amp, rs_amp
                if neckline is None:
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
                        config.volume_avg_window, direction,
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

                # 頭肩頂加分：ChartSchool「a downward slope is more bearish than
                # an upward slope」。只影響分數排序，不影響是否入選。
                if direction == dirmod.TOP and neckline.angle_deg < 0 and config.top_downward_neckline_bonus:
                    steepness = min(abs(neckline.angle_deg) / max_angle, 1.0) if max_angle else 0.0
                    bearish_bonus = steepness * config.top_downward_neckline_bonus
                    score["downward_neckline_bonus"] = bearish_bonus
                    pattern_score += bearish_bonus

                # 五段式量能檢查（教科書節奏），純加分不淘汰
                volume_stage = None
                if config.enable_volume_stage_score and volume_arr is not None:
                    volume_stage = volume_stages.evaluate(
                        volume_arr, LS["index"], N1["index"], H["index"],
                        N2["index"], RS["index"])
                    if volume_stage["quality"] is not None:
                        vs_bonus = volume_stage["quality"] * config.volume_stage_weight
                        score["volume_stage_bonus"] = vs_bonus

                        pattern_score += vs_bonus

                # 2026-07-30 修正：scoring.py 內部雖然已把四個分量夾在 [0,100]，
                # 但上面兩項加分是在夾完之後才加的，理論最高會到 125
                # （下斜頸線 +10、五段量能 +15），超出對外宣稱的 0~100 範圍。
                # 這裡再夾一次，讓 pattern_score 的定義域跟文件一致。
                # 副作用：接近滿分的型態，加分會被上限吸收——這是可接受的，
                # 因為加分本來就設計成「臨門一腳」，不該讓分數突破量表。
                pattern_score = max(0.0, min(100.0, pattern_score))
                score["pattern_score"] = pattern_score

                if pattern_score < config.min_pattern_score:
                    continue

                results.append({
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "pattern_type": dirmod.PATTERN_TYPE[direction],
                    "direction": direction,
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
                    # 量測目標（ChartSchool 量測法則）：底部往上加、頂部往下減
                    "measured_target": dirmod.measured_target(
                        current_neckline_price, H["price"], direction),
                    # Bulkowski 實證：這個目標的歷史達成率（底 71% / 頂 51%）。
                    # 一併輸出是為了讓下游顯示時能誠實標示機率，不要讓目標價
                    # 看起來像承諾。
                    "target_hit_rate": BULKOWSKI_STATS[direction]["target_hit_rate"],
                    "historical_failure_rate": BULKOWSKI_STATS[direction]["failure_rate"],
                    "atr_at_rs": bar_atr,
                    # 附加除錯/可追溯欄位（非 spec 必要欄位，但不影響驗收）
                    "_score_components": score,
                    "_volume_stage": volume_stage,
                })

    return results


def detect_inverse_head_shoulders(df: pd.DataFrame, timeframe: str, config: IHSConfig,
                                   symbol: Optional[str] = None) -> list[dict]:
    """頭肩底（相容包裝，維持既有呼叫端不用改）。"""
    return detect_head_shoulders(df, timeframe, config, dirmod.BOTTOM, symbol)


def detect_head_and_shoulders_top(df: pd.DataFrame, timeframe: str, config: IHSConfig,
                                   symbol: Optional[str] = None) -> list[dict]:
    """頭肩頂（2026-07-30 新增）。"""
    return detect_head_shoulders(df, timeframe, config, dirmod.TOP, symbol)
