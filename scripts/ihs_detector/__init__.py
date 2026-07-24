# -*- coding: utf-8 -*-
"""
ihs_detector

純數學（無 LLM、無圖像辨識、無主觀判斷）Inverse Head and Shoulders（頭肩底）
型態偵測器。完整規格見 docs/spec-ihs.md。

公開 API：
    IHSConfig, find_swing_points, calculate_neckline,
    detect_inverse_head_shoulders, calculate_pattern_score,
    scan_symbols, export_results
"""
from .config import IHSConfig, TIMEFRAMES
from .swing import find_swing_points, extract_swing_points
from .pattern import calculate_neckline, detect_inverse_head_shoulders, Neckline
from .scoring import calculate_pattern_score
from .scan import scan_symbols, OUTPUT_FIELDS
from .export import export_results

__all__ = [
    "IHSConfig",
    "TIMEFRAMES",
    "find_swing_points",
    "extract_swing_points",
    "calculate_neckline",
    "Neckline",
    "detect_inverse_head_shoulders",
    "calculate_pattern_score",
    "scan_symbols",
    "OUTPUT_FIELDS",
    "export_results",
]
