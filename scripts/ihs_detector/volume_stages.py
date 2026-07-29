# -*- coding: utf-8 -*-
"""
volume_stages.py

五段式量能檢查（2026-07-30 新增，研究來源 StockCharts ChartSchool）。

教科書描述的量能節奏（以頭肩頂為例，頭肩底方向相反但量能規則相同）：
1. 左肩推進量 > 頭部推進量（頭部創新高卻量縮，是第一個警訊）
2. 頭部回落時放量
3. 右肩推進時縮量
4. 右肩回落時放量
5. 跌破頸線時放量確認

本模組只檢查其中三段——那是能從我們既有的五個關鍵點嚴格劃分出來的部分：
- (a) 頭部推進量 < 左肩區量      對應第 1 條
- (b) 頭部回落量 > 頭部推進量    對應第 2 條
- (c) 右肩推進量 < 頭部回落量    對應第 3 條
第 5 條（突破放量）已經由既有的 volume_ratio 處理，不重複計分。
第 4 條需要「右肩之後的回落段」，在型態剛完成時通常還不存在，故不納入。

誠實定位：這是規則統計，不是預測；量能規則在教科書上本來就寫「ideally,
but not always」，所以本模組的輸出是 0~1 的品質係數，只當加分項，
不當淘汰條件。
"""
from __future__ import annotations

from typing import Optional

import numpy as np


def _segment_mean(volume: np.ndarray, i1: int, i2: int) -> Optional[float]:
    lo, hi = (i1, i2) if i1 <= i2 else (i2, i1)
    seg = volume[lo:hi + 1]
    if len(seg) == 0:
        return None
    m = float(seg.mean())
    return m if m > 0 else None


def evaluate(volume: np.ndarray, ls_index: int, n1_index: int, h_index: int,
             n2_index: int, rs_index: int) -> dict:
    """
    完整三段檢查。回傳：
        {"quality": float|None, "passed": int, "checked": int, "stages": {...}}
    """
    ls_seg = _segment_mean(volume, ls_index, n1_index)
    head_adv = _segment_mean(volume, n1_index, h_index)
    head_ret = _segment_mean(volume, h_index, n2_index)
    rs_adv = _segment_mean(volume, n2_index, rs_index)

    stages: dict[str, Optional[bool]] = {}
    # (a) 頭部推進量應該小於左肩區量
    stages["head_advance_lighter"] = (head_adv < ls_seg) if (ls_seg and head_adv) else None
    # (b) 頭部回落應該放量
    stages["head_retrace_heavier"] = (head_ret > head_adv) if (head_adv and head_ret) else None
    # (c) 右肩推進應該縮量
    stages["right_shoulder_lighter"] = (rs_adv < head_ret) if (head_ret and rs_adv) else None

    decided = [v for v in stages.values() if v is not None]
    if not decided:
        return {"quality": None, "passed": 0, "checked": 0, "stages": stages}
    passed = sum(1 for v in decided if v)
    return {"quality": passed / len(decided), "passed": passed,
            "checked": len(decided), "stages": stages}
