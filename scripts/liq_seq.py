# -*- coding: utf-8 -*-
"""引擎 C：歷史強平事件密度（回顧性統計，不是預測）。

**這是什麼**：把 OKX 永續合約公開的「歷史上真的發生過」的強平事件，依價格
分桶累積成密度，用來佐證（不是證明）量能牆價位的可信度——某個量能牆價位
附近，過去是否也是強平事件密集發生的地方。

**這不是什麼**：不是「預測性清算熱力圖」。四大交易所（幣安/OKX/Bybit/
Deribit）都不公開逐帳戶槓桿倍數與進場價，任何聲稱能「預測」下一波清算會
發生在哪個價位的熱力圖，本質上都是用未公開資料做的模型估算，不是真數據
（查證見 docs/project-charter.md 決策記錄 2026-07-17、2026-07-21）。本模組
只統計「已經發生過」的強平，是事後回顧，不是前瞻預測。

**跨交易所比對的取捨**：量能牆（引擎 A/B）資料來自幣安現貨/深度，這裡的
強平事件來自 OKX 永續合約。兩邊價格高度相關但不完全一致（不同交易所、
不同商品類型），密度數字只能當「參考背景」，不是同一商品的精確比對。

**資料來源查證**（2026-07-21，用 curl 直接打 API 驗證，非官方文件記載）：
- `GET https://www.okx.com/api/v5/public/liquidation-orders`
  （公開端點，不需要 API key／簽名）。
- 這個端點**目前不在 OKX 官方 v5 文件頁**（www.okx.com/docs-v5/en/ 全文
  搜尋 "liquidation-orders" 只找到 WebSocket 頻道，沒有這支 REST）。
  第三方函式庫（go-okx-v5、python-okx 的 consts.py）仍列出這個路徑，
  實測（curl）確認端點目前仍可用、回真實資料（依 symbol 價格不同、亂打
  不存在的 uly 會回 "Index doesn't exist" 錯誤，非純靜態假資料）。
  **風險**：既然官方文件已不記載，不保證長期穩定，可能哪天說關就關。
- 必要參數：`instType=SWAP`、`uly=<BASE>-USDT`（或 `instFamily`）、
  `state`（必填，只接受 `filled`/`unfilled`；我們要「已發生」的事件用
  `filled`）。`instId` 單獨給不夠，必須搭配 `uly`/`instFamily`。
- 回應欄位：`data[].details[]`，每筆含 `bkPx`（破產價，字串）、`side`
  （buy/sell）、`posSide`（long/short）、`sz`（數量，字串）、`ts`
  （毫秒字串）、`ccy`、`bkLoss`。
- **觀察到的怪異行為（未在任何文件記載，實測發現）**：`data` 陣列除了
  `data[0]`（含真正的 `details`）外，其餘元素是
  `{"$ref": "$.data[0]"}` 佔位符，且 `data[0].details` 的筆數等於
  `16 x limit`（例如 limit=100 實得 1600 筆），成因不明、疑似 OKX 後端
  的序列化怪癖。本模組**只讀有 `details` 鍵的分組、忽略 `$ref` 佔位符**，
  不依賴這個「16x」倍數（未文件化的行為不可靠，寫死倍數風險更高）。
- **視窗深度**：第三方文件宣稱「近 7 天」，但實測單次 `limit=100` 對
  BTC 這種高頻幣種只拿到約 18.5 小時的資料（強平事件太密集，還沒到 7 天
  額度就被 limit 頂到）。所以**不依賴 OKX 單次回應能給多深的歷史**，
  改用「我們自己每次排程都抓、去重後累積」的策略達到想要的視窗深度
  （與 depth_seq 的 48 小時策略同一邏輯，只是強平事件稀疏，視窗拉長到
  14 天更有意義）。
- 找不到官方文件公布的 rate limit 數字；本模組每次執行對每個幣種只呼叫
  一次，未觀察到限流問題。

無第三方相依，Python 3.12+ 標準庫即可。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

OKX_HOST = "https://www.okx.com"
LIQ_PATH = "/api/v5/public/liquidation-orders?instType=SWAP&uly={uly}&state=filled&limit=100"
TIMEOUT_SEC = 10
RETRIES = 2
RETENTION_DAYS = 14


def binance_symbol_to_okx_uly(symbol: str) -> str:
    """BTCUSDT -> BTC-USDT（OKX 的 uly/underlying 格式）。

    我們 5 個幣種都是 xxxUSDT 現貨命名，OKX 永續合約用 xxx-USDT-SWAP，
    其 uly（underlying）是 xxx-USDT。只處理 USDT 結尾，其餘格式視為不支援
    （目前專案只有 USDT 計價幣種，未來若加別的計價幣種需要擴充這個函式）。
    """
    if symbol.endswith("USDT") and len(symbol) > 4:
        return symbol[:-4] + "-USDT"
    raise ValueError(f"不支援的符號格式（只支援 xxxUSDT）: {symbol}")


def _dedup_key(symbol: str, det: dict) -> str:
    """穩定去重鍵：OKX 沒給訂單/成交 ID，用價格+方向+數量+時間戳組合。

    實測同一批 100(x16=1600) 筆記錄裡，這個組合鍵完全沒有碰撞
    （見 liq_seq.py 模組 docstring 的查證記錄），可以放心當去重依據。
    """
    return "|".join(
        [
            symbol,
            str(det.get("ts", "")),
            str(det.get("bkPx", "")),
            str(det.get("side", "")),
            str(det.get("posSide", "")),
            str(det.get("sz", "")),
        ]
    )


def normalize_record(det: dict, symbol: str, inst_id: str) -> dict | None:
    """把 OKX 原始 detail dict 轉成本專案的輕量事件格式；欄位缺漏回 None。"""
    try:
        ts_ms = int(det["ts"])
        price = float(det["bkPx"])
        sz = float(det.get("sz", 0))
    except (KeyError, TypeError, ValueError):
        return None
    ts_iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(
        timespec="seconds"
    )
    return {
        "ts_utc": ts_iso,
        "symbol": symbol,
        "inst_id": inst_id,
        "price": price,
        "side": det.get("side", ""),
        "pos_side": det.get("posSide", ""),
        "sz": sz,
        "key": _dedup_key(symbol, det),
    }


def fetch_liquidations(symbol: str, timeout: int = TIMEOUT_SEC, retries: int = RETRIES) -> list[dict]:
    """抓某幣種近期強平事件（OKX SWAP，state=filled）。失敗回空 list。"""
    try:
        uly = binance_symbol_to_okx_uly(symbol)
    except ValueError:
        return []
    url = OKX_HOST + LIQ_PATH.format(uly=uly)
    last_err: Exception | None = None
    for attempt in range(1 + retries):
        req = urllib.request.Request(url, headers={"User-Agent": "VolumeWallBot/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("code") != "0":
                last_err = RuntimeError(f"OKX code={payload.get('code')} msg={payload.get('msg')}")
                break  # 參數/邏輯錯誤重試也不會好，直接放棄這輪
            records = []
            for group in payload.get("data", []):
                if "details" not in group:
                    continue  # "$ref" 佔位符，跳過（見模組 docstring）
                inst_id = group.get("instId", uly)
                for det in group["details"]:
                    rec = normalize_record(det, symbol, inst_id)
                    if rec is not None:
                        records.append(rec)
            return records
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            if attempt < retries:
                continue
    if last_err is not None:
        print(
            f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} "
            f"WARN liq_seq {symbol} 抓取失敗: {type(last_err).__name__}: {last_err}"
        )
    return []


def dedupe_merge(existing: list[dict], new: list[dict]) -> list[dict]:
    """合併新抓到的事件與既有序列，用 key 去重（保留既有順序，新事件接在後面）。"""
    seen = {e["key"] for e in existing if "key" in e}
    merged = list(existing)
    for r in new:
        k = r.get("key")
        if k is not None and k not in seen:
            merged.append(r)
            seen.add(k)
    return merged


def prune_liq_seq(seq: list[dict], now: datetime, retention_days: int = RETENTION_DAYS) -> list[dict]:
    """只留最近 retention_days 天的事件；ts 解析不了的丟棄。"""
    cutoff = now - timedelta(days=retention_days)
    kept = []
    for r in seq:
        try:
            ts = datetime.fromisoformat(r["ts_utc"])
        except (KeyError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            kept.append(r)
    return kept


def _liq_seq_path(base_dir: Path, symbol: str) -> Path:
    return base_dir / "data" / "liq_seq" / f"{symbol}.jsonl"


def load_liq_seq(base_dir: Path, symbol: str) -> list[dict]:
    path = _liq_seq_path(base_dir, symbol)
    if not path.exists():
        return []
    seq: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                seq.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    seq.sort(key=lambda s: s.get("ts_utc", ""))
    return seq


def write_liq_seq(base_dir: Path, symbol: str, seq: list[dict]) -> None:
    path = _liq_seq_path(base_dir, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for s in seq:
            f.write(json.dumps(s, separators=(",", ":"), ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# 密度統計（仿照 DOM 引擎的 0.1% 桶邏輯；純函式，不做 IO）
# --------------------------------------------------------------------------

def bucket_liquidations(
    events: list[dict], bucket_pct: float = 0.001, reference_price: float | None = None
) -> dict[int, dict]:
    """把強平事件依價格分桶（同引擎 B 的 0.1% 絕對桶寬邏輯）。

    強平事件沒有「當下 mid」的概念（每筆事件的破產價就是它自己的價格），
    所以桶寬用 reference_price（沒給就用事件價格中位數）算，不是逐事件
    各自的 mid。回傳 {bucket_key: {"count": int, "notional_usd": float}}。
    """
    if not events:
        return {}
    if reference_price is None:
        prices = sorted(e["price"] for e in events)
        reference_price = prices[len(prices) // 2]
    if not reference_price or reference_price <= 0:
        return {}
    bucket_width = reference_price * bucket_pct
    if bucket_width <= 0:
        return {}
    buckets: dict[int, dict] = {}
    for e in events:
        key = round((e["price"] - reference_price) / bucket_width)
        b = buckets.setdefault(key, {"count": 0, "notional_usd": 0.0})
        b["count"] += 1
        b["notional_usd"] += e["price"] * e["sz"]
    return buckets


def liq_density_near_price(events: list[dict], target_price: float, band_pct: float = 0.005) -> dict:
    """某價位 ±band_pct（預設 0.5%）範圍內，歷史強平事件的筆數與總名目金額。

    用來標註量能牆旁邊的「歷史強平密度」——**回顧性統計，不是預測**。
    """
    if not events or not target_price:
        return {"count": 0, "notional_usd": 0.0}
    lo = target_price * (1 - band_pct)
    hi = target_price * (1 + band_pct)
    matched = [e for e in events if lo <= e["price"] <= hi]
    return {
        "count": len(matched),
        "notional_usd": sum(e["price"] * e["sz"] for e in matched),
    }
