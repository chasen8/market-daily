# -*- coding: utf-8 -*-
"""量化市場深度（DOM）快照器。

抓 Binance 現貨 order book，把「盤口厚度」量化成可跨時間比較的指標，
每次執行對每個 symbol 追加一列到 repo 的 data/dom_history.csv（GitHub Actions 版）。

指標定義（附錄同步寫在日報）：
- mid：(最佳買價+最佳賣價)/2
- spread_bp：買賣價差，單位基點（萬分之一）
- bid_usd_X / ask_usd_X：mid 往下/往上 X% 範圍內掛單總價值（USD）
- imb_X：失衡比 = bid/(bid+ask)，0.5=平衡，>0.55 買方厚，<0.45 賣方厚
排程建議：每 15 分鐘一次（Windows 工作排程器），累積後才有趨勢可看。
"""
import csv
import datetime as dt
import os
import sys

import requests

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
BANDS = [0.005, 0.01, 0.02]  # ±0.5% / 1% / 2%
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
CSV_PATH = os.path.join(DATA_DIR, "dom_history.csv")
# Binance 主站被擋時的公開行情鏡像（僅行情、無交易功能）
HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]

FIELDS = (["ts_utc", "symbol", "mid", "spread_bp", "cover_bid_pct", "cover_ask_pct"]
          + [f"{side}_usd_{int(b*1000)}" for b in BANDS for side in ("bid", "ask")]
          + [f"imb_{int(b*1000)}" for b in BANDS])


def fetch_depth(symbol: str) -> dict:
    last_err = None
    for host in HOSTS:
        try:
            r = requests.get(f"{host}/api/v3/depth",
                             params={"symbol": symbol, "limit": 5000}, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001 - 換備援主機
            last_err = e
    raise RuntimeError(f"{symbol} 兩個主機都失敗: {last_err}")


def snapshot(symbol: str) -> dict:
    book = fetch_depth(symbol)
    bids = [(float(p), float(q)) for p, q in book["bids"]]
    asks = [(float(p), float(q)) for p, q in book["asks"]]
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    row = {"ts_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "symbol": symbol, "mid": round(mid, 6),
           "spread_bp": round((best_ask - best_bid) / mid * 1e4, 3),
           # 訂單簿實際覆蓋範圍（%）；若 < 帶寬，該帶寬的數字已飽和不可信
           "cover_bid_pct": round((mid - bids[-1][0]) / mid * 100, 3),
           "cover_ask_pct": round((asks[-1][0] - mid) / mid * 100, 3)}
    for b in BANDS:
        lo, hi = mid * (1 - b), mid * (1 + b)
        bid_usd = sum(p * q for p, q in bids if p >= lo)
        ask_usd = sum(p * q for p, q in asks if p <= hi)
        k = int(b * 1000)
        row[f"bid_usd_{k}"] = round(bid_usd)
        row[f"ask_usd_{k}"] = round(ask_usd)
        row[f"imb_{k}"] = round(bid_usd / (bid_usd + ask_usd), 4) if bid_usd + ask_usd else None
    return row


def main() -> int:
    os.makedirs(DATA_DIR, exist_ok=True)
    new_file = not os.path.exists(CSV_PATH)
    ok, fail = 0, []
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for sym in SYMBOLS:
            try:
                w.writerow(snapshot(sym))
                ok += 1
            except Exception as e:  # noqa: BLE001 - 單一 symbol 失敗不中斷其他
                fail.append(f"{sym}: {e}")
    print(f"DOM snapshot: {ok}/{len(SYMBOLS)} ok -> {CSV_PATH}")
    for msg in fail:
        print("FAIL", msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
