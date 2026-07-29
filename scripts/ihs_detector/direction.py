# -*- coding: utf-8 -*-
"""
direction.py

頭肩底（bottom）與頭肩頂（top）的方向差異，全部集中在這個檔案，
讓 pattern.py 的主流程只有一份、不需要為了兩種型態複製一遍。

研究來源（2026-07-30）：StockCharts ChartSchool「Head and Shoulders Top」
- 頸線由「兩個低點」連成（頭肩底則是兩個高點）
- 突破 = 收盤「跌破」頸線（頭肩底是站上）
- 目標價 = 頸線 −（頭部到頸線的距離）（頭肩底是加）
- 右肩「lower than the head，usually in line with the left shoulder」，
  且明講 symmetry preferred but not required（見 config.shoulder_symmetry_mode）

兩種型態互為鏡像，但**不能**直接用「價格取負號」實作：本專案的公式（肩差、
頭部深度、頸線斜率）全都是百分比，取負號會讓分母變號、比例失真。所以這裡
用明確的方向旗標，而不是數值鏡射。
"""
from __future__ import annotations

BOTTOM = "bottom"
TOP = "top"

PATTERN_TYPE = {
    BOTTOM: "inverse_head_and_shoulders",
    TOP: "head_and_shoulders_top",
}


def validate(direction: str) -> str:
    if direction not in (BOTTOM, TOP):
        raise ValueError(f"direction 必須是 {BOTTOM!r} 或 {TOP!r}，收到 {direction!r}")
    return direction


def shoulder_pivots(swing_lows: list[dict], swing_highs: list[dict], direction: str) -> list[dict]:
    """左肩／頭／右肩要從哪一組 pivot 挑：底部用低點，頂部用高點。"""
    return swing_lows if direction == BOTTOM else swing_highs


def neckline_pivots(swing_lows: list[dict], swing_highs: list[dict], direction: str) -> list[dict]:
    """頸線的兩個端點要從哪一組 pivot 挑：底部用高點，頂部用低點。"""
    return swing_highs if direction == BOTTOM else swing_lows


def head_is_beyond(head_price: float, shoulder_price: float, direction: str) -> bool:
    """頭部是否比肩部更極端：底部要更低，頂部要更高。"""
    return head_price < shoulder_price if direction == BOTTOM else head_price > shoulder_price


def head_depth(shoulder_avg: float, head_price: float, direction: str) -> float:
    """頭部相對雙肩平均的幅度（永遠回傳正值代表「更極端」）。"""
    if direction == BOTTOM:
        return (shoulder_avg - head_price) / shoulder_avg
    return (head_price - shoulder_avg) / shoulder_avg


def shoulder_amplitude(neckline_price: float, shoulder_price: float, direction: str) -> float:
    """從頸線點到肩部的幅度，相對頸線點計算（正值代表往型態方向走）。"""
    if direction == BOTTOM:
        return (neckline_price - shoulder_price) / neckline_price
    return (shoulder_price - neckline_price) / neckline_price


def is_broken_out(close_price: float, neckline_price: float, buffer: float, direction: str) -> bool:
    """突破判定：底部要站上頸線，頂部要跌破頸線。"""
    if direction == BOTTOM:
        return close_price > neckline_price * (1 + buffer)
    return close_price < neckline_price * (1 - buffer)


def neckline_pierced(bar_high: float, bar_low: float, neckline_price: float, direction: str) -> bool:
    """
    頸線是否被這根 K 棒穿越。
    底部：頸線是天花板，high 不得超過；頂部：頸線是地板，low 不得跌破。
    """
    if direction == BOTTOM:
        return bar_high > neckline_price
    return bar_low < neckline_price


def measured_target(neckline_price: float, head_price: float, direction: str, mult: float = 1.0) -> float:
    """量測目標：底部往上加、頂部往下減（ChartSchool 的量測法則）。"""
    height = abs(neckline_price - head_price) * mult
    return neckline_price + height if direction == BOTTOM else neckline_price - height


def pick_neckline_candidates(pivots: list[dict], lo_index: int, hi_index: int,
                              prominence_ratio: float, direction: str) -> list[dict]:
    """
    區間內「夠顯著」的頸線點候選。
    底部：頸線點是高點，取接近區間最高的那些（>= 最高 × ratio）。
    頂部：頸線點是低點，取接近區間最低的那些（<= 最低 ÷ ratio）。
    """
    in_range = [p for p in pivots if lo_index < p["index"] < hi_index]
    if not in_range:
        return []
    if direction == BOTTOM:
        top = max(p["price"] for p in in_range)
        if top <= 0:
            return in_range
        return [p for p in in_range if p["price"] >= top * prominence_ratio]
    bottom = min(p["price"] for p in in_range)
    if bottom <= 0 or prominence_ratio <= 0:
        return in_range
    return [p for p in in_range if p["price"] <= bottom / prominence_ratio]
