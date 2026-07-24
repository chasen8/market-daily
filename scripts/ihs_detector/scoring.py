# -*- coding: utf-8 -*-
"""
scoring.py

第九步：Pattern Score（0-100），由四個加權分量組成：
- shoulder_score      滿分 30
- head_depth_score    滿分 25
- neckline_score      滿分 25
- breakout_score      滿分 20

純代數公式，不含任何主觀判斷。
"""
from __future__ import annotations

from typing import Optional

from .config import IHSConfig


def calculate_pattern_score(shoulder_diff: float, head_depth: float, neckline_angle: float,
                             breakout_detected: bool, distance_to_neckline: Optional[float],
                             config: IHSConfig, timeframe: str) -> dict:
    """
    計算 pattern_score 與各分量，公式逐字對應 spec 第九步。

    回傳 dict：
        {shoulder_score, head_depth_score, neckline_score, breakout_score, pattern_score}
    """
    max_allowed_shoulder_diff = config.shoulder_diff_limit(timeframe)
    shoulder_score = max(0.0, 1 - shoulder_diff / max_allowed_shoulder_diff) * 30

    ideal_head_depth = config.ideal_head_depth
    head_depth_score = min(head_depth / ideal_head_depth, 1.0) * 25

    max_neckline_angle = config.max_neckline_angle
    neckline_score = max(0.0, 1 - abs(neckline_angle) / max_neckline_angle) * 25

    if breakout_detected:
        breakout_score = 20.0
    else:
        d = distance_to_neckline if distance_to_neckline is not None else 1.0
        breakout_score = max(0.0, 1 - d / config.distance_to_neckline_scale) * 20

    pattern_score = shoulder_score + head_depth_score + neckline_score + breakout_score
    # 數值保護：理論上四個分量各自已經 clip 到自己的滿分區間，加總必落在 [0, 100]，
    # 這裡再夾一次純粹是防禦性寫法，避免浮點誤差極端情況溢出。
    pattern_score = max(0.0, min(100.0, pattern_score))

    return {
        "shoulder_score": shoulder_score,
        "head_depth_score": head_depth_score,
        "neckline_score": neckline_score,
        "breakout_score": breakout_score,
        "pattern_score": pattern_score,
    }
