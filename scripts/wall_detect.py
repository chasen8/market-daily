# -*- coding: utf-8 -*-
"""量能牆偵測核心邏輯（純函式，不做任何 IO）。

規格來源：docs/project-charter.md 的「訊號規格 v1」一節（唯一權威來源，
本模組門檻數字全部照抄該節，不自行發明）。

引擎 A：detect_volume_walls(klines)
    - 資料為幣安 15m K 線（收盤棒），偵測「爆量牆」與「十字吸籌牆」。
引擎 B：detect_dom_walls(snapshot)
    - 資料為單一時間點的訂單簿深度快照，偵測「掛單牆」。
track_persistence(snapshot_walls)
    - 對一串按時間排序的 detect_dom_walls() 結果做持續性追蹤與消失分類。

資料形狀（呼叫端負責把幣安原始回應轉成這個形狀，本模組不碰網路/檔案）：

klines: list[dict]，依 open_time 升冪排列，每個 dict 至少含：
    open_time (int, ms)、open/high/low/close (float)、volume (float，底幣量)、
    quote_volume (float，USD 成交額；缺省則用 volume*typical_price 近似)。

snapshot: dict，至少含：
    bids: [[price, qty], ...]（price/qty 可為 str 或 float）
    asks: [[price, qty], ...]
"""
from __future__ import annotations

from typing import Any


# --------------------------------------------------------------------------
# 共用小工具
# --------------------------------------------------------------------------

def _percentile_rank(values: list[float], x: float) -> float:
    """回傳 x 在 values 中的百分位（<=x 的比例 * 100，含自身）。"""
    if not values:
        return 0.0
    n = len(values)
    count_le = sum(1 for v in values if v <= x)
    return count_le / n * 100.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def atr_series(klines: list[dict], period: int = 14) -> list[float]:
    """回傳與 klines 等長的 ATR(period) 序列（簡單移動平均版 True Range）。

    資料不足 period 根時，用當下可取得的 TR 做簡單移動平均（非 0），
    只有完全沒有資料時才會是 0。
    """
    trs: list[float] = []
    prev_close = None
    for k in klines:
        if prev_close is None:
            tr = k["high"] - k["low"]
        else:
            tr = max(
                k["high"] - k["low"],
                abs(k["high"] - prev_close),
                abs(k["low"] - prev_close),
            )
        trs.append(tr)
        prev_close = k["close"]

    atrs: list[float] = []
    for i in range(len(trs)):
        window = trs[max(0, i - period + 1): i + 1]
        atrs.append(sum(window) / len(window) if window else 0.0)
    return atrs


# --------------------------------------------------------------------------
# 引擎 A：量能牆（K 線成交量）
# --------------------------------------------------------------------------

def detect_volume_walls(
    klines: list[dict],
    lookback: int = 200,
    max_walls: int = 30,
) -> list[dict]:
    """偵測爆量牆與十字吸籌牆（charter 訊號規格 v1 § 引擎 A）。

    - 爆量牆：該棒 volume 在最近 200 根（含自身，資料不足則用現有全部）
      的百分位 >= 97。
    - 十字吸籌牆：實體/振幅 <= 0.3 且 volume >= 1.5x 近 200 根中位數
      且振幅（high-low） >= 0.5 x ATR(14)。
    - 牆價位 = 該棒 typical price (H+L+C)/3；牆量 = 該棒成交額（USD，
      quote_volume；若 klines 未提供則用 volume*typical_price 近似）。
    - FIFO 修剪：兩種類型各自最多保留最近 30 道（依建立順序，超過時
      淘汰最舊的）。
    - 強度 = 該牆 volume_usd 在「FIFO 修剪後、最終保留」的同類型牆群組中
      的百分位（0-100）。

    回傳：list[dict]，依 open_time 升冪，每個：
        {type: 'big_volume' | 'doji', open_time, price, volume_usd, strength}
    """
    n = len(klines)
    if n == 0:
        return []

    atrs = atr_series(klines, 14)
    fifo: dict[str, list[dict]] = {"big_volume": [], "doji": []}

    for i, k in enumerate(klines):
        window = klines[max(0, i - lookback + 1): i + 1]
        vols = [w["volume"] for w in window]
        vol_pctl = _percentile_rank(vols, k["volume"])
        median_vol = _median(vols)

        rng = k["high"] - k["low"]
        body = abs(k["close"] - k["open"])
        atr14 = atrs[i]
        typical_price = (k["high"] + k["low"] + k["close"]) / 3.0
        volume_usd = k.get("quote_volume")
        if volume_usd is None:
            volume_usd = k["volume"] * typical_price

        is_big = vol_pctl >= 97.0

        is_doji = False
        if rng > 0:
            body_ratio = body / rng
            is_doji = (
                body_ratio <= 0.3
                and k["volume"] >= 1.5 * median_vol
                and rng >= 0.5 * atr14
            )

        if is_big:
            wall = {
                "type": "big_volume",
                "open_time": k["open_time"],
                "price": typical_price,
                "volume_usd": volume_usd,
            }
            fifo["big_volume"].append(wall)
            if len(fifo["big_volume"]) > max_walls:
                fifo["big_volume"].pop(0)

        if is_doji:
            wall = {
                "type": "doji",
                "open_time": k["open_time"],
                "price": typical_price,
                "volume_usd": volume_usd,
            }
            fifo["doji"].append(wall)
            if len(fifo["doji"]) > max_walls:
                fifo["doji"].pop(0)

    walls: list[dict] = []
    for group in fifo.values():
        usds = [w["volume_usd"] for w in group]
        for w in group:
            w["strength"] = _percentile_rank(usds, w["volume_usd"])
            walls.append(w)

    walls.sort(key=lambda w: w["open_time"])
    return walls


# --------------------------------------------------------------------------
# 引擎 B：DOM 掛單牆（單一快照）
# --------------------------------------------------------------------------

def detect_dom_walls(
    snapshot: dict,
    bucket_pct: float = 0.001,
    range_pct: float = 0.05,
    threshold_multiplier: float = 8.0,
    min_usd: float = 500_000.0,
) -> dict:
    """偵測單一深度快照中的掛單牆（charter 訊號規格 v1 § 引擎 B）。

    - 取 mid +-5% 範圍內的檔位，逐檔聚成 0.1% 價格桶（絕對寬度 =
      mid * 0.1%，桶中心價格 = mid + bucket_key * bucket_width）。
    - 桶 USD = sum(price * qty)，bids/asks 一起累加進同一組桶
      （兩者價格區間天然不重疊，只有極窄範圍可能相鄰）。
    - 牆 = 桶 USD >= 8x 全部桶中位數 且 >= $500k（雙門檻，兩者都要滿足）。

    回傳：
        {"mid": float | None, "bucket_width": float,
         "walls": [{"bucket_key": int, "price": float, "usd": float,
                     "side": "bid"|"ask"}, ...]}
    walls 依 bucket_key 升冪排列；side 取該桶中金額較大的一側，僅供顯示用。
    """
    bids = [(float(p), float(q)) for p, q in snapshot.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in snapshot.get("asks", [])]
    if not bids or not asks:
        return {"mid": None, "bucket_width": 0.0, "walls": []}

    best_bid = max(p for p, _ in bids)
    best_ask = min(p for p, _ in asks)
    mid = (best_bid + best_ask) / 2.0
    bucket_width = mid * bucket_pct
    if bucket_width <= 0:
        return {"mid": mid, "bucket_width": 0.0, "walls": []}

    lo = mid * (1 - range_pct)
    hi = mid * (1 + range_pct)

    buckets: dict[int, dict[str, float]] = {}

    def add(price: float, qty: float, side: str) -> None:
        if price < lo or price > hi:
            return
        key = round((price - mid) / bucket_width)
        usd = price * qty
        b = buckets.setdefault(key, {"usd": 0.0, "bid_usd": 0.0, "ask_usd": 0.0})
        b["usd"] += usd
        b[f"{side}_usd"] += usd

    for p, q in bids:
        add(p, q, "bid")
    for p, q in asks:
        add(p, q, "ask")

    if not buckets:
        return {"mid": mid, "bucket_width": bucket_width, "walls": []}

    all_usd = [b["usd"] for b in buckets.values()]
    med = _median(all_usd)
    threshold = max(threshold_multiplier * med, min_usd)

    walls = []
    for key in sorted(buckets):
        b = buckets[key]
        if b["usd"] >= threshold and b["usd"] >= min_usd:
            side = "bid" if b["bid_usd"] >= b["ask_usd"] else "ask"
            price = mid + key * bucket_width
            walls.append(
                {"bucket_key": key, "price": price, "usd": b["usd"], "side": side}
            )
    return {"mid": mid, "bucket_width": bucket_width, "walls": walls}


# --------------------------------------------------------------------------
# 持續性追蹤與消失分類
# --------------------------------------------------------------------------

def track_persistence(
    snapshot_walls: list[dict],
    tolerance: int = 1,
    min_snapshots: int = 3,
    bucket_pct: float = 0.001,
) -> dict:
    """對一串依時間升冪排列的 detect_dom_walls() 結果做持續性追蹤。

    snapshot_walls: list[dict]，每個至少含：
        {"mid": float, "walls": [{"bucket_key": int, "price": float,
                                    "usd": float, "side": str}, ...]}
      （可額外帶 "ts_utc"、"bucket_width" 之類欄位；bucket_width 若存在
      會用作容差基準，否則用 mid * bucket_pct 推算）。

    規則（charter）：
        - 持續牆 = 同一桶（**絕對價格**差 <= tolerance x 桶寬）連續
          >= min_snapshots 個快照都出現的牆。
          注意：牆的身分比對用「絕對價格」錨定，不用 bucket_key ——
          bucket_key 是相對當下 mid 的桶號，mid 漂移會讓同一道實體牆
          （絕對價位不變）的桶號跳動，導致追蹤誤判斷裂（假消失＋假新牆）。
        - 快照數量 < min_snapshots 時，不做持續性判定，只回傳「當下牆」
          （最後一個快照的 walls）。
        - 持續牆消失時：期間內 mid 價格若穿過牆價位 => 判定「被吃」
          (eaten)；沒穿過 => 判定「撤單」(cancelled，疑似幌騙)。

    回傳 dict：
        {
          "current": [...],        # 最後一個快照的原始 walls
          "persistent": [...],     # 目前仍存在、streak>=min_snapshots 的牆
          "disappeared": [...],    # 曾經持續、後來消失的牆（含 reason）
          "newly_persistent": [...] # 在最後一個快照「剛好」達到
                                     # min_snapshots 門檻的牆（可用來觸發警報）
        }
    """
    n = len(snapshot_walls)
    current = snapshot_walls[-1]["walls"] if n else []

    if n < min_snapshots:
        return {
            "current": current,
            "persistent": [],
            "disappeared": [],
            "newly_persistent": [],
        }

    active_tracks: list[dict] = []
    disappeared: list[dict] = []
    newly_persistent: list[dict] = []

    for idx, snap in enumerate(snapshot_walls):
        # 容差的絕對寬度：優先用快照自帶的 bucket_width，
        # 否則用 mid * bucket_pct 推算；都拿不到就退回逐牆用自身價格推算。
        snap_bw = snap.get("bucket_width")
        if not snap_bw:
            mid = snap.get("mid")
            snap_bw = mid * bucket_pct if mid else None

        # 牆 <-> track 的配對：全域按距離排序後貪婪指派，而不是逐牆搶配。
        # 逐牆搶配會讓「先出現的小鄰居牆」搶走長壽 track（例如同快照出現
        # 78.16 與 78.24 兩道牆時，78.16 先把 78.2 的老 track 搶走，
        # 真正的主牆 78.24 被迫開新 track，老 track 下一輪就假死）。
        candidates: list[tuple[float, int, int]] = []
        for wi, w in enumerate(snap["walls"]):
            bw = snap_bw if snap_bw else w["price"] * bucket_pct
            tol_abs = tolerance * bw
            for ti, t in enumerate(active_tracks):
                if not t["alive"]:
                    continue
                dist = abs(t["last_price"] - w["price"])
                if dist <= tol_abs:
                    candidates.append((dist, wi, ti))
        candidates.sort(key=lambda c: c[0])

        matched_wall_idx: set[int] = set()
        matched_track_ids: set[int] = set()
        for dist, wi, ti in candidates:
            t = active_tracks[ti]
            if wi in matched_wall_idx or id(t) in matched_track_ids:
                continue
            w = snap["walls"][wi]
            t["streak"] += 1
            t["bucket_key"] = w["bucket_key"]
            t["last_idx"] = idx
            t["last_price"] = w["price"]
            t["last_usd"] = w["usd"]
            matched_wall_idx.add(wi)
            matched_track_ids.add(id(t))
            if t["streak"] == min_snapshots:
                newly_persistent.append(
                    {
                        "bucket_key": t["bucket_key"],
                        "price": t["last_price"],
                        "side": t["side"],
                        "usd": t["last_usd"],
                        "streak": t["streak"],
                        "idx": idx,
                    }
                )

        for wi, w in enumerate(snap["walls"]):
            if wi in matched_wall_idx:
                continue
            active_tracks.append(
                {
                    "bucket_key": w["bucket_key"],
                    "side": w["side"],
                    "streak": 1,
                    "first_idx": idx,
                    "last_idx": idx,
                    "last_price": w["price"],
                    "last_usd": w["usd"],
                    "alive": True,
                }
            )

        # 這一輪沒被任何 wall 對到的存活 track，視為在這個快照消失
        # （first_idx == idx 的是本輪剛建立的新 track，不在檢查範圍）
        for t in active_tracks:
            if t["alive"] and t["first_idx"] != idx and id(t) not in matched_track_ids:
                t["alive"] = False
                if t["streak"] >= min_snapshots:
                    mid_before = snapshot_walls[t["last_idx"]]["mid"]
                    mid_after = snap["mid"]
                    wall_price = t["last_price"]
                    crossed = False
                    if mid_before is not None and mid_after is not None:
                        crossed = (mid_before - wall_price) * (mid_after - wall_price) < 0
                    disappeared.append(
                        {
                            "bucket_key": t["bucket_key"],
                            "price": wall_price,
                            "side": t["side"],
                            "streak": t["streak"],
                            "reason": "eaten" if crossed else "cancelled",
                            "first_idx": t["first_idx"],
                            "last_idx": t["last_idx"],
                            "disappeared_at_idx": idx,
                        }
                    )

    persistent = [
        {
            "bucket_key": t["bucket_key"],
            "price": t["last_price"],
            "side": t["side"],
            "usd": t["last_usd"],
            "streak": t["streak"],
            "first_idx": t["first_idx"],
            "last_idx": t["last_idx"],
        }
        for t in active_tracks
        if t["alive"] and t["streak"] >= min_snapshots
    ]

    return {
        "current": current,
        "persistent": persistent,
        "disappeared": disappeared,
        "newly_persistent": newly_persistent,
    }
