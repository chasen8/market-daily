# -*- coding: utf-8 -*-
"""
config.py

集中管理所有可調參數，對應 docs/spec-ihs.md 第十二步「config 需要包含」清單。

所有預設值均取自 spec-ihs.md：
- pivot_window：4h/1d 建議 3~5（本檔取中間值 4 作為預設，可調），1w 建議 2~3（取 3）
- shoulder_diff_limit：4h 0.05；1d/1w 0.10
- min_head_depth：4h 0.03；1d/1w 0.05
- max_neckline_angle：30（度）
- angle_scale：100
- breakout_buffer：4h 0.005；1d/1w 0.01
- min_volume_ratio：1.2
- shoulder_amplitude_tolerance：0.30（右肩跌幅相對左肩跌幅的容忍度，2026-07-28 由 0.15 放寬）
- require_neckline_untouched：True（頸線區間不得被其他K棒穿越）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

TIMEFRAMES = ("4h", "1d", "1w")


@dataclass
class IHSConfig:
    # --- 第十二步「config 需要包含」逐項對應 ---
    pivot_window_by_timeframe: Dict[str, int] = field(
        default_factory=lambda: {"4h": 4, "1d": 4, "1w": 3}
    )
    shoulder_diff_limit_by_timeframe: Dict[str, float] = field(
        default_factory=lambda: {"4h": 0.05, "1d": 0.10, "1w": 0.10}
    )
    min_head_depth_by_timeframe: Dict[str, float] = field(
        default_factory=lambda: {"4h": 0.03, "1d": 0.05, "1w": 0.05}
    )
    max_neckline_angle: float = 30.0
    angle_scale: float = 100.0
    breakout_buffer_by_timeframe: Dict[str, float] = field(
        default_factory=lambda: {"4h": 0.005, "1d": 0.01, "1w": 0.01}
    )
    enable_breakout_filter: bool = False
    enable_volume_filter: bool = False
    min_volume_ratio: float = 1.2
    min_pattern_score: float = 0.0
    # 2026-07-24 使用者依實際圖表案例（BASUSDT）新增的兩條結構驗證規則：
    shoulder_amplitude_tolerance: float = 0.30  # 右肩跌幅需落在左肩跌幅 ±30% 內
    require_neckline_untouched: bool = True     # 頸線區間內（除左右肩本身）不得被其他K棒高點穿越
    # 2026-07-28：頸線點不再只取「區間最高」，改成在「夠高的候選」裡挑斜率最平的組合。
    # prominence_ratio 決定什麼叫「夠高」：價格 >= 區間最高價 × 本值 才列入候選。
    # 設 1.0 等同回到舊行為（只有最高點入選）；設太低會讓演算法為了求平而選到不顯著的小高點。
    neckline_prominence_ratio: float = 0.90

    # --- 額外輔助參數（spec 公式中出現的常數，集中放這裡方便調整） ---
    ideal_head_depth: float = 0.10          # 第九步 head_depth_score 用
    distance_to_neckline_scale: float = 0.10  # 第九步 breakout_score（candidate）用
    volume_avg_window: int = 20             # average_volume_20 的視窗長度
    # 第十一步排序用的 timeframe 優先序：1w > 1d > 4h（數字越大優先權越高）
    timeframe_priority: Dict[str, int] = field(
        default_factory=lambda: {"1w": 3, "1d": 2, "4h": 1}
    )

    # --- 便利存取器：找不到對應 timeframe 直接丟錯，避免默默用錯參數 ---
    def pivot_window(self, timeframe: str) -> int:
        return self.pivot_window_by_timeframe[timeframe]

    def shoulder_diff_limit(self, timeframe: str) -> float:
        return self.shoulder_diff_limit_by_timeframe[timeframe]

    def min_head_depth(self, timeframe: str) -> float:
        return self.min_head_depth_by_timeframe[timeframe]

    def breakout_buffer(self, timeframe: str) -> float:
        return self.breakout_buffer_by_timeframe[timeframe]

    def timeframe_rank(self, timeframe: str) -> int:
        return self.timeframe_priority.get(timeframe, 0)
