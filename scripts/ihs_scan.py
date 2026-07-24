# -*- coding: utf-8 -*-
"""IHS（頭肩底 Inverse Head and Shoulders）全市場掃描＋Discord 通知。

資料來源：Binance 公開現貨 K 線 API（/api/v3/klines），純讀公開行情、不需要
API key、不是交易權限，符合鐵律「絕不碰真錢」。只做型態偵測與通知，不下單、
不建議進出場時機——型態候選本身不是操作建議。

偵測邏輯複用 c:\\Users\\chris\\OneDrive\\桌面\\形態學 專案的 ihs_detector/
（本檔 sibling package，逐字複製過來，兩邊之後各自演進——如果之後兩邊都要
同步改動，考慮抽成獨立套件，但目前先各自獨立避免耦合）。

推播分兩級（2026-07-24 使用者要求提早示警，不要只等完整突破）：
- **half**（左肩＋頭部初步成形，LS→N1→H）：型態才走到一半，右肩/頸線/突破
  都還沒發生，純粹是「這裡可能在形成頭肩底，先看著」，失敗率遠高於
  breakout 分級，純觀察用途。
- **breakout**（完整 LS→N1→H→N2→RS 都確認、且已突破頸線）：最嚴格、最少
  噪音的分級，原本就有的通知邏輯。
兩級都各自做「同一個 key 只通知一次」的 state-diff（跟 whale_scan.py 手法
一致），並且都有「冷啟動不推播」保護——首次執行只建立基準狀態，否則會把
K 線視窗內所有歷史候選一次性通知，等同洗版。
"""
import json
import os
import sys
import time

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ihs_detector import IHSConfig, detect_inverse_head_shoulders  # noqa: E402
from ihs_detector.swing import find_swing_points, extract_swing_points  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BREAKOUT_STATE_PATH = os.path.join(ROOT, "data", "ihs_state.json")
HALF_STATE_PATH = os.path.join(ROOT, "data", "ihs_half_state.json")

H = {"User-Agent": "Mozilla/5.0"}
SPOT_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]
TIMEOUT = 20
MIN_QUOTE_VOL = 300_000  # 跟 whale_scan.py 同門檻，24h 成交額 >= $30萬才收進宇宙
TIMEFRAMES = ["4h", "1d", "1w"]
KLINE_LIMIT = 200
MIN_PATTERN_SCORE = 60.0  # breakout 分級門檻，避免洗版
STABLE = {"USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD", "USD1", "EURI",
          "PAX", "GUSD", "USDD", "RLUSD", "USDE", "USDY", "BFUSD", "USDS"}
LEV_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")
DISCORD_SEND_DELAY = 0.4
TF_LABEL = {"4h": "4小時", "1d": "日線", "1w": "週線"}


def sget(path, params=None):
    last = None
    for host in SPOT_HOSTS:
        try:
            r = requests.get(host + path, params=params, headers=H, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"spot {path} 失敗: {last}")


def fetch_universe(min_qv=MIN_QUOTE_VOL):
    info = sget("/api/v3/exchangeInfo")
    eligible = set()
    for s in info["symbols"]:
        if (s["status"] == "TRADING" and s["quoteAsset"] == "USDT"
                and s.get("isSpotTradingAllowed", True)
                and s["baseAsset"] not in STABLE
                and not s["baseAsset"].endswith(LEV_SUFFIX)):
            eligible.add(s["symbol"])
    tick = sget("/api/v3/ticker/24hr")
    symbols = [t["symbol"] for t in tick
               if t["symbol"] in eligible and float(t["quoteVolume"]) >= min_qv]
    return sorted(symbols)


def fetch_klines_df(symbol, interval, limit=KLINE_LIMIT):
    kl = sget("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    rows = [{
        "timestamp": pd.to_datetime(k[0], unit="ms"),
        "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
        "close": float(k[4]), "volume": float(k[5]),
    } for k in kl]
    return pd.DataFrame(rows)


def load_state(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def fmt_tw_time(ts):
    """K 線時間戳（Binance 回傳、pandas 解析後為 naive UTC）轉台灣時間顯示，
    並明確標註時區，避免使用者誤判成 UTC 或當地電腦時區（2026-07-24 使用者
    要求訊息要標明時區）。台灣不實施日光節約時間，固定 +8 小時沒有 DST 誤差。"""
    return (pd.Timestamp(ts) + pd.Timedelta(hours=8)).strftime("%Y-%m-%d %H:%M") + " (UTC+8 台灣)"


def _post_discord_embeds(webhook, embeds):
    """跟 whale_scan.py 同一套發送方式：webhook 可逗號分隔多個網址，
    每批最多 10 個 embed（Discord 上限），批次間留間隔避免撞速率限制。"""
    hooks = [h.strip() for h in (webhook or "").split(",") if h.strip()]
    for i in range(0, len(embeds), 10):
        batch = embeds[i:i + 10]
        for h in hooks:
            try:
                requests.post(h, json={"embeds": batch}, timeout=TIMEOUT)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] Discord 推播失敗: {e}")
        time.sleep(DISCORD_SEND_DELAY)


def build_breakout_embed(r):
    tf_label = TF_LABEL.get(r["timeframe"], r["timeframe"])
    title = f'🟢 頭肩底突破・{r["symbol"]}（{tf_label}）'
    desc = (
        f'pattern_score `{r["pattern_score"]:.1f}`\n'
        f'左肩 `{r["LS_price"]:g}` → 頭部 `{r["H_price"]:g}` → 右肩 `{r["RS_price"]:g}`\n'
        f'肩差 `{r["shoulder_diff"] * 100:.2f}%` · 頭深 `{r["head_depth"] * 100:.2f}%` · '
        f'頸線角度 `{r["neckline_angle"]:.1f}°`\n'
        f'突破價 `{r["breakout_price"]:g}` · 時間 `{fmt_tw_time(r["breakout_time"])}`'
        + (f' · 量能比 `{r["volume_ratio"]:.2f}`' if r.get("volume_ratio") else '')
        + '\n程式規則生成的型態候選，非投資建議'
    )
    return {"title": title, "description": desc, "color": 0x2ECC71}


def build_half_embed(h):
    tf_label = TF_LABEL.get(h["timeframe"], h["timeframe"])
    title = f'⚠️ 型態觀察（左肩＋頭部）・{h["symbol"]}（{tf_label}）'
    desc = (
        f'左肩 `{h["LS_price"]:g}` → 頭部 `{h["H_price"]:g}`（比左肩低 '
        f'`{h["head_depth_vs_ls"] * 100:.2f}%`）\n'
        f'頭部時間 `{fmt_tw_time(h["H_time"])}`\n'
        '> 型態才走到一半：右肩、頸線、突破都還沒發生，純觀察，'
        '失敗率遠高於「突破」通知，不是進場訊號\n'
        '程式規則生成，非投資建議'
    )
    return {"title": title, "description": desc, "color": 0xF1C40F}


def _best_swing_high_in_range(swing_highs, lo_index, hi_index):
    best = None
    for sh in swing_highs:
        if sh["index"] <= lo_index or sh["index"] >= hi_index:
            continue
        if best is None or sh["price"] > best["price"]:
            best = sh
    return best


def detect_half_pattern(df, timeframe, config, symbol=None):
    """LS -> N1 -> H 只到頭部的早期示警（2026-07-24 新增，使用者要求提早通知）。

    只取「最新一個 swing low」當頭部候選 H，往回找分數最高（頭部比左肩低最多）
    的 LS/N1 組合——同一時間只追蹤一組「目前正在形成」的候選，避免歷史上
    每個可能的頭部都各報一次造成洗版。回傳 None 表示目前沒有符合條件的候選。
    """
    swings_df = find_swing_points(df, config.pivot_window(timeframe))
    swing_lows, swing_highs = extract_swing_points(swings_df)
    if len(swing_lows) < 2:
        return None
    Hp = swing_lows[-1]
    min_head_depth = config.min_head_depth(timeframe)
    best = None
    for LS in swing_lows:
        if LS["index"] >= Hp["index"]:
            continue
        if not (Hp["price"] < LS["price"]):
            continue
        N1 = _best_swing_high_in_range(swing_highs, LS["index"], Hp["index"])
        if N1 is None:
            continue
        head_depth_vs_ls = (LS["price"] - Hp["price"]) / LS["price"]
        if head_depth_vs_ls < min_head_depth:
            continue
        if best is None or head_depth_vs_ls > best["head_depth_vs_ls"]:
            best = {
                "symbol": symbol, "timeframe": timeframe,
                "LS_time": LS["time"], "LS_price": LS["price"],
                "N1_time": N1["time"], "N1_price": N1["price"],
                "H_time": Hp["time"], "H_price": Hp["price"],
                "head_depth_vs_ls": head_depth_vs_ls,
            }
    return best


def scan(symbols, config):
    """對每個 symbol x timeframe 抓一次 K 線，同時跑 breakout 偵測與 half-pattern
    偵測（避免重複打 API）。回傳 (breakouts, half_patterns) 兩個 list。"""
    all_breakouts, all_halves = [], []
    for symbol in symbols:
        for tf in TIMEFRAMES:
            try:
                df = fetch_klines_df(symbol, tf)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] {symbol} {tf} 抓資料失敗: {e}")
                continue
            if len(df) < 30:
                continue
            try:
                results = detect_inverse_head_shoulders(df, tf, config, symbol=symbol)
                all_breakouts.extend(r for r in results if r["breakout_detected"])
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] {symbol} {tf} breakout 偵測失敗: {e}")
            try:
                half = detect_half_pattern(df, tf, config, symbol=symbol)
                if half is not None:
                    all_halves.append(half)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] {symbol} {tf} half-pattern 偵測失敗: {e}")
    return all_breakouts, all_halves


def _dedupe_new(items, key_fn, score_fn, state, is_cold_start):
    """共用的 state-diff 去重：同一個 key 只留分數最高一筆，state 裡已通知過的
    key 跳過，冷啟動整批不通知（只建立基準）。回傳 (new_alerts, seen_keys)。"""
    seen_keys = set()
    best_by_key = {}
    for r in items:
        key = key_fn(r)
        seen_keys.add(key)
        if key in state:
            continue
        if key not in best_by_key or score_fn(r) > score_fn(best_by_key[key]):
            best_by_key[key] = r
    new_alerts = [] if is_cold_start else sorted(best_by_key.values(), key=lambda r: -score_fn(r))
    return new_alerts, seen_keys


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL_IHS", "").strip()
    dry_run = os.environ.get("IHS_DRY_RUN", "").strip() == "1"

    config = IHSConfig()
    config.enable_breakout_filter = True
    config.enable_volume_filter = False
    config.min_pattern_score = MIN_PATTERN_SCORE

    breakout_cold_start = not os.path.exists(BREAKOUT_STATE_PATH)
    half_cold_start = not os.path.exists(HALF_STATE_PATH)

    t0 = time.time()
    symbols = fetch_universe()
    breakouts, halves = scan(symbols, config)
    elapsed = time.time() - t0

    breakout_state = load_state(BREAKOUT_STATE_PATH)
    half_state = load_state(HALF_STATE_PATH)

    new_breakouts, breakout_keys = _dedupe_new(
        breakouts,
        key_fn=lambda r: f'{r["symbol"]}|{r["timeframe"]}|{r["breakout_time"]}',
        score_fn=lambda r: r["pattern_score"],
        state=breakout_state, is_cold_start=breakout_cold_start,
    )
    new_halves, half_keys = _dedupe_new(
        halves,
        key_fn=lambda h: f'{h["symbol"]}|{h["timeframe"]}|{h["H_time"]}',
        score_fn=lambda h: h["head_depth_vs_ls"],
        state=half_state, is_cold_start=half_cold_start,
    )

    embeds = [build_half_embed(h) for h in new_halves] + [build_breakout_embed(r) for r in new_breakouts]
    if embeds and webhook and not dry_run:
        _post_discord_embeds(webhook, embeds)

    save_state(BREAKOUT_STATE_PATH, {k: True for k in breakout_keys})
    save_state(HALF_STATE_PATH, {k: True for k in half_keys})

    print(f"[ihs_scan] universe={len(symbols)} elapsed={elapsed:.1f}s "
          f"discord={'on' if webhook else 'off(dry/no-secret)'}{' DRY_RUN' if dry_run else ''}\n"
          f"  breakout: seen={len(breakouts)} unique={len(breakout_keys)} "
          f"new={len(new_breakouts)} cold_start={breakout_cold_start}\n"
          f"  half:     seen={len(halves)} unique={len(half_keys)} "
          f"new={len(new_halves)} cold_start={half_cold_start}")
    for r in new_breakouts[:20]:
        print(f'  NEW breakout {r["symbol"]} {r["timeframe"]} score={r["pattern_score"]:.1f} '
              f'breakout_time={r["breakout_time"]}')
    for h in new_halves[:20]:
        print(f'  NEW half {h["symbol"]} {h["timeframe"]} '
              f'head_depth_vs_ls={h["head_depth_vs_ls"] * 100:.2f}% H_time={h["H_time"]}')


if __name__ == "__main__":
    main()
