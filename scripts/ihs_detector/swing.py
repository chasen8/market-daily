# -*- coding: utf-8 -*-
"""
swing.py

第一步：找 swing high / swing low（pivot window 方法）。

純數學規則，不使用任何 LLM / 圖像辨識 / 主觀判斷。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def find_swing_points(df: pd.DataFrame, pivot_window: int) -> pd.DataFrame:
    """
    找出 swing low / swing high。

    定義（spec 第一步）：
    - swing low：某根 K 線的 low 必須小於前後 N 根 K 線的 low
    - swing high：某根 K 線的 high 必須大於前後 N 根 K 線的 high

    參數
    ----
    df : DataFrame，至少包含 'high', 'low' 欄位，依時間排序、整數位置索引（0..n-1）。
    pivot_window : N，前後各檢查幾根 K 線。

    回傳
    ----
    df 的複本，新增兩個布林欄位：
    - is_swing_low
    - is_swing_high
    """
    if pivot_window < 1:
        raise ValueError("pivot_window 必須 >= 1")

    n = len(df)
    low = df["low"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)

    is_low = np.zeros(n, dtype=bool)
    is_high = np.zeros(n, dtype=bool)

    N = pivot_window
    for i in range(N, n - N):
        before_low = low[i - N:i]
        after_low = low[i + 1:i + N + 1]
        if low[i] < before_low.min() and low[i] < after_low.min():
            is_low[i] = True

        before_high = high[i - N:i]
        after_high = high[i + 1:i + N + 1]
        if high[i] > before_high.max() and high[i] > after_high.max():
            is_high[i] = True

    out = df.copy()
    out["is_swing_low"] = is_low
    out["is_swing_high"] = is_high
    return out


def extract_swing_points(df_with_swings: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """
    把 find_swing_points 產生的布林欄位轉換成方便組合候選結構的點清單。

    回傳 (swing_lows, swing_highs)，每個元素為：
        {"index": int, "time": <timestamp>, "price": float}
    swing_lows 用 low 價格；swing_highs 用 high 價格。皆依 index 由小到大排序。
    """
    if "is_swing_low" not in df_with_swings.columns or "is_swing_high" not in df_with_swings.columns:
        raise ValueError("df 必須先經過 find_swing_points 處理")

    time_col = "timestamp" if "timestamp" in df_with_swings.columns else None

    swing_lows = []
    swing_highs = []
    for i, row in df_with_swings.iterrows():
        t = row[time_col] if time_col else i
        if bool(row["is_swing_low"]):
            swing_lows.append({"index": int(i), "time": t, "price": float(row["low"])})
        if bool(row["is_swing_high"]):
            swing_highs.append({"index": int(i), "time": t, "price": float(row["high"])})

    return swing_lows, swing_highs
