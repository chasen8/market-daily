# -*- coding: utf-8 -*-
"""
scan.py

scan_symbols：跨多個 symbol、多個 timeframe 執行 detect_inverse_head_shoulders，
彙整結果並依 spec 第十一步規則排序。
"""
from __future__ import annotations

from typing import Dict

import pandas as pd

from .config import IHSConfig
from .pattern import detect_inverse_head_shoulders

# spec 第十步「輸出欄位」，用於清理內部除錯欄位、確保輸出 schema 一致。
OUTPUT_FIELDS = [
    "symbol", "timeframe", "pattern_type", "mode", "pattern_score",
    "LS_time", "LS_price", "N1_time", "N1_price", "H_time", "H_price",
    "N2_time", "N2_price", "RS_time", "RS_price",
    "shoulder_diff", "head_depth", "neckline_angle", "neckline_slope_pct_per_bar",
    "current_close", "current_neckline_price", "distance_to_neckline",
    "breakout_detected", "breakout_time", "breakout_price", "volume_ratio",
]


def _sort_key(result: dict, config: IHSConfig):
    """
    第十一步排序規則（由高到低 pattern_score，同分時依序比較）：
    1. breakout_detected = True 優先
    2. distance_to_neckline 較小優先
    3. volume_ratio 較高優先
    4. timeframe 優先序 1w > 1d > 4h

    這裡回傳「越小越好」的 tuple，讓 sorted() 用預設遞增排序即可達成上述效果。
    """
    pattern_score = result["pattern_score"]
    breakout_rank = 0 if result["breakout_detected"] else 1
    distance = result["distance_to_neckline"]
    distance = distance if distance is not None else float("inf")
    volume_ratio = result["volume_ratio"]
    neg_volume_ratio = -volume_ratio if volume_ratio is not None else float("inf")
    neg_timeframe_rank = -config.timeframe_rank(result["timeframe"])

    return (-pattern_score, breakout_rank, distance, neg_volume_ratio, neg_timeframe_rank)


def scan_symbols(symbol_data: Dict[str, Dict[str, pd.DataFrame]], config: IHSConfig) -> list[dict]:
    """
    跨 symbol、跨 timeframe 掃描頭肩底候選。

    參數
    ----
    symbol_data : {symbol: {timeframe: df}}，例如：
        {"BTCUSDT": {"4h": df_4h, "1d": df_1d}, "ETHUSDT": {"1d": df_1d}}
    config : IHSConfig

    回傳
    ----
    list of dict，欄位為 OUTPUT_FIELDS，已依 pattern_score 等規則排序（第十一步）。
    """
    all_results: list[dict] = []

    for symbol, tf_map in symbol_data.items():
        for timeframe, df in tf_map.items():
            candidates = detect_inverse_head_shoulders(df, timeframe, config, symbol=symbol)
            for c in candidates:
                cleaned = {k: c.get(k) for k in OUTPUT_FIELDS}
                all_results.append(cleaned)

    all_results.sort(key=lambda r: _sort_key(r, config))
    return all_results
