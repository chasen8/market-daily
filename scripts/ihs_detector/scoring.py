# -*- coding: utf-8 -*-
"""
scoring.py

Pattern Score（0-100）。

== 2026-07-30 重大修正：對稱性不再等於高分 ==
原本的配分是 shoulder 30 / head 25 / neckline 25 / breakout 20，其中
shoulder_score「越對稱分數越高」。查證後發現這跟實證統計相反：

  Bulkowski（thepatternsite.com）：
  「頭肩頂看起來越不對稱，表現越好」
  「左肩延伸的型態表現較佳；右肩延伸的表現較差」

也就是說——**對稱性是「這是不是頭肩型態」的辨識標準，不是「這個型態會不會
走得好」的績效標準**。舊配分把兩件事混在一起，等於在獎勵表現較差的型態。

現在預設把 shoulder_diff 降級成純粹的辨識門檻（在 pattern.py 擋掉超標的），
不再計分，30 分重新分配給三個真正有意義的維度：

  預設 "gate"：head_depth 35 / neckline 30 / breakout 35
  相容模式 "reward_symmetry"：維持舊配分（30/25/25/20）
  實驗模式 "reward_asymmetry"：照 Bulkowski 統計，不對稱給分

breakout 拿到最高權重的理由：ChartSchool 明講「型態在頸線被突破之前不算完成」，
突破是唯一有確認意義的事件，其餘都是「長得像」而已。

純代數公式，不含任何主觀判斷。
"""
from __future__ import annotations

from typing import Optional

from .config import IHSConfig

# 各模式的配分表（總和必為 100）
_WEIGHTS = {
    "gate": {"shoulder": 0.0, "head_depth": 35.0, "neckline": 30.0, "breakout": 35.0},
    "reward_symmetry": {"shoulder": 30.0, "head_depth": 25.0, "neckline": 25.0, "breakout": 20.0},
    "reward_asymmetry": {"shoulder": 30.0, "head_depth": 25.0, "neckline": 25.0, "breakout": 20.0},
}


def calculate_pattern_score(shoulder_diff: float, head_depth: float, neckline_angle: float,
                             breakout_detected: bool, distance_to_neckline: Optional[float],
                             config: IHSConfig, timeframe: str) -> dict:
    """
    計算 pattern_score 與各分量。回傳 dict：
        {shoulder_score, head_depth_score, neckline_score, breakout_score,
         pattern_score, weights_mode}
    """
    mode = config.shoulder_symmetry_scoring
    w = _WEIGHTS.get(mode, _WEIGHTS["gate"])

    max_allowed_shoulder_diff = config.shoulder_diff_limit(timeframe)
    if w["shoulder"] <= 0:
        # gate 模式：對稱性只在 pattern.py 當門檻，這裡不給分
        shoulder_score = 0.0
    elif mode == "reward_asymmetry":
        # Bulkowski：越不對稱表現越好。以門檻為滿分刻度，
        # 完全對稱得 0 分、貼近門檻上限得滿分。
        ratio = min(shoulder_diff / max_allowed_shoulder_diff, 1.0) if max_allowed_shoulder_diff else 0.0
        shoulder_score = ratio * w["shoulder"]
    else:
        # 舊行為：越對稱分數越高
        shoulder_score = max(0.0, 1 - shoulder_diff / max_allowed_shoulder_diff) * w["shoulder"]

    head_depth_score = min(head_depth / config.ideal_head_depth, 1.0) * w["head_depth"]

    max_neckline_angle = config.max_neckline_angle
    neckline_score = max(0.0, 1 - abs(neckline_angle) / max_neckline_angle) * w["neckline"]

    if breakout_detected:
        breakout_score = w["breakout"]
    else:
        d = distance_to_neckline if distance_to_neckline is not None else 1.0
        breakout_score = max(0.0, 1 - d / config.distance_to_neckline_scale) * w["breakout"]

    pattern_score = shoulder_score + head_depth_score + neckline_score + breakout_score
    # 數值保護：各分量本來就已 clip 在自己的滿分區間，加總必落在 [0, 100]，
    # 這裡再夾一次純粹是防浮點誤差溢出。
    pattern_score = max(0.0, min(100.0, pattern_score))

    return {
        "shoulder_score": shoulder_score,
        "head_depth_score": head_depth_score,
        "neckline_score": neckline_score,
        "breakout_score": breakout_score,
        "pattern_score": pattern_score,
        "weights_mode": mode,
    }
