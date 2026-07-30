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

# Bulkowski 實證統計（thepatternsite.com，2026-07-30 查證）。
# 用途：誠實標示量測目標的達成機率，避免把目標價講得像承諾。
# 樣本：頭肩底 3,197 筆「perfect trades」。
BULKOWSKI_STATS = {
    "bottom": {"target_hit_rate": 0.71, "failure_rate": 0.11,
               "avg_move": 0.45, "throwback_rate": 0.65},
    "top": {"target_hit_rate": 0.51, "failure_rate": 0.19,
            "avg_move": -0.16, "pullback_rate": 0.68},
}


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

    # --- 2026-07-30 頭肩頂支援（研究來源：StockCharts ChartSchool）---
    # 肩部對稱性模式：
    #   "strict"   = 沿用頭肩底的嚴格門檻（shoulder_diff + 跌幅比都要過），少而精
    #   "textbook" = 照教科書「symmetry preferred but not required」，只要求右肩沒有
    #                超過頭部、且肩差在放寬後的門檻內，抓得多但雜訊也多
    shoulder_symmetry_mode: str = "strict"
    textbook_shoulder_diff_limit: float = 0.20   # textbook 模式用的放寬門檻
    # 頭肩頂：教科書說「下斜頸線比上斜更看空」，所以頸線往下傾斜時給看空加分。
    # 注意這只影響「分數」，不影響「選哪組頸線」——選點仍然偏好接近水平，
    # 避免為了追求斜率而選到怪異的頸線點。
    top_downward_neckline_bonus: float = 10.0
    # 五段式量能檢查（頭部推進縮量 / 頭部回落放量 / 右肩推進縮量）
    # Bulkowski：肩部量能下降只有 61~65% 的時候成立，所以只加分不淘汰
    enable_volume_stage_score: bool = False
    volume_stage_weight: float = 15.0

    # --- 2026-07-30 研究後修正（來源：Bulkowski / QuantInsti，見 charter）---
    # ATR 相對門檻：固定百分比在不同週期與不同標的上意義差很多
    # （BTC 的 3% 跟台積電的 3% 完全不是同一回事），改用「幾倍 ATR」
    # 讓門檻自動適應波動。關掉則退回原本的固定百分比。
    use_atr_thresholds: bool = True
    atr_period: int = 14
    min_head_atr: float = 1.5          # 頭部要比雙肩平均更極端至少 N 倍 ATR
    max_shoulder_diff_atr: float = 1.5  # 左右肩價差不得超過 N 倍 ATR

    # 對稱性計分模式。Bulkowski 的實證統計顯示「頭肩頂越不對稱表現越好」，
    # 跟「越對稱分數越高」的直覺相反——對稱是「辨識」標準，不等於「績效」標準。
    #   "gate"             = 只當辨識門檻不計分（預設，最保守）
    #   "reward_symmetry"  = 舊行為，越對稱分數越高
    #   "reward_asymmetry" = 照 Bulkowski 統計，不對稱給分
    shoulder_symmetry_scoring: str = "gate"

    # --- 2026-07-30 使用者以 POWERUSDT 4h 實例指出的比例 bug ---
    # 對稱性要用哪個判準？
    #   "amplitude"（預設）= 只比「各自從頸線端點往下/往上的幅度百分比」
    #   "absolute"        = 舊行為，比兩肩的絕對價位差
    #   "both"            = 兩者都要通過（最嚴格）
    #
    # 為什麼預設改成 amplitude：兩者在頸線傾斜時會直接矛盾。實例——
    # POWERUSDT 4h 頭肩底，頸線從 0.12391 下斜到 0.11125（-10.2%），
    # 左肩跌幅 27.13%、右肩跌幅 27.08%（比值 0.998，近乎完美對稱），
    # 但兩肩絕對價位差 10.70%，被 4h 的 5% 門檻整組砍掉。
    # 頸線既然下斜 10.2%，兩肩要等比例就「必然」差約 10.2% —— 用絕對價位
    # 當門檻等於宣告「斜頸線的頭肩型態一律不算」，跟允許斜頸線自相矛盾。
    shoulder_symmetry_basis: str = "amplitude"

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
