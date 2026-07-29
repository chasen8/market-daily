# -*- coding: utf-8 -*-
"""
indicators.py

型態偵測用得到的通用技術指標。目前只有 ATR。

為什麼需要 ATR（2026-07-30）：
原本的門檻（肩差 5%、頭部幅度 3%）是固定百分比，但同樣的百分比在不同標的
與不同週期上意義天差地遠——BTC 4 小時的 3% 是家常便飯，台積電日線的 3%
是大事。用「幾倍 ATR」表達門檻可以讓同一組參數在任何標的/週期上都保持
相同的「相對於自身波動」的嚴格程度。

依據：MQL5「Structured Head and Shoulders Scanner」一文指出自動化偵測的
難點之一就是「最小尺寸應相對於波動率定義」。Bulkowski 與 QuantInsti 都
明講頭肩型態沒有公認的數值門檻，所以與其硬編百分比，不如綁定波動率。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def true_range(df: pd.DataFrame) -> np.ndarray:
    """TR = max(high-low, |high-prev_close|, |low-prev_close|)"""
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    prev_close = np.concatenate(([close[0]], close[:-1]))
    return np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])


def atr(df: pd.DataFrame, period: int = 14) -> np.ndarray:
    """
    Wilder ATR（RMA 平滑）。回傳與 df 等長的陣列，前 period-1 根為 NaN。

    用 Wilder 的遞迴平滑而不是簡單移動平均，跟 TradingView 的 ta.atr() 一致，
    這樣 Python 版與 Pine 版的門檻才會對得起來。
    """
    tr = true_range(df)
    n = len(tr)
    out = np.full(n, np.nan, dtype=float)
    if n < period or period < 1:
        return out
    seed = tr[:period].mean()
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def atr_at(atr_values: np.ndarray, index: int) -> float | None:
    """取某根 K 線的 ATR；若該處還沒有值（暖身期）則往前找最近的有效值。"""
    if atr_values is None or len(atr_values) == 0:
        return None
    i = min(max(index, 0), len(atr_values) - 1)
    while i >= 0:
        v = atr_values[i]
        if not np.isnan(v) and v > 0:
            return float(v)
        i -= 1
    return None
