# -*- coding: utf-8 -*-
"""
export.py

export_results：把 scan_symbols / detect_inverse_head_shoulders 的結果
（list of dict）輸出成 CSV 或 JSON 檔案。
"""
from __future__ import annotations

import json
import os
from typing import Sequence

import pandas as pd


def export_results(results: Sequence[dict], output_path: str) -> str:
    """
    輸出結果到檔案，依副檔名決定格式：
    - .csv  -> 以 pandas DataFrame 輸出 CSV（utf-8-sig，Excel friendly）
    - .json -> 輸出 JSON array（utf-8，縮排 2，ensure_ascii=False 保留中文）

    參數
    ----
    results : list of dict（例如 scan_symbols 的輸出）
    output_path : 檔案路徑，副檔名須為 .csv 或 .json

    回傳
    ----
    實際寫入的檔案路徑（str）。
    """
    ext = os.path.splitext(output_path)[1].lower()

    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    if ext == ".csv":
        df = pd.DataFrame(list(results))
        df.to_csv(output_path, index=False, encoding="utf-8-sig")
    elif ext == ".json":
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(list(results), f, ensure_ascii=False, indent=2, default=str)
    else:
        raise ValueError(f"不支援的輸出格式: {ext}（僅支援 .csv / .json）")

    return output_path
