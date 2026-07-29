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
from ihs_detector import (  # noqa: E402
    IHSConfig,
    detect_inverse_head_shoulders,
    detect_head_and_shoulders_top,
)
from ihs_detector import direction as dirmod  # noqa: E402
from ihs_detector.swing import find_swing_points, extract_swing_points  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BREAKOUT_STATE_PATH = os.path.join(ROOT, "data", "ihs_state.json")
HALF_STATE_PATH = os.path.join(ROOT, "data", "ihs_half_state.json")
# 頭肩頂用獨立的 state 檔（2026-07-30 新增）。刻意不跟頭肩底共用檔案：
# 共用的話首次上線時「檔案已存在」會讓冷啟動保護失效，把歷史上所有頭肩頂
# 一次全部推播出去。獨立檔案不存在 -> 觸發冷啟動 -> 只建基準不通知。
TOP_BREAKOUT_STATE_PATH = os.path.join(ROOT, "data", "hs_top_state.json")
TOP_HALF_STATE_PATH = os.path.join(ROOT, "data", "hs_top_half_state.json")

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
    is_top = r.get("direction") == dirmod.TOP
    if is_top:
        title = f'🔴 頭肩頂跌破・{r["symbol"]}（{tf_label}）'
        struct = (f'左肩 `{r["LS_price"]:g}` → 頭部 `{r["H_price"]:g}` → '
                  f'右肩 `{r["RS_price"]:g}`（頭部高於雙肩 `{r["head_depth"] * 100:.2f}%`）')
        color = 0xE74C3C
        break_word = "跌破價"
    else:
        title = f'🟢 頭肩底突破・{r["symbol"]}（{tf_label}）'
        struct = (f'左肩 `{r["LS_price"]:g}` → 頭部 `{r["H_price"]:g}` → '
                  f'右肩 `{r["RS_price"]:g}`（頭部低於雙肩 `{r["head_depth"] * 100:.2f}%`）')
        color = 0x2ECC71
        break_word = "突破價"
    target = r.get("measured_target")
    hit = r.get("target_hit_rate")
    fail = r.get("historical_failure_rate")
    # 誠實標示：量測目標的歷史達成率（Bulkowski：頭肩底 71%、頭肩頂 51%）。
    # 不標的話目標價看起來會像承諾，但實際上頭肩頂只有約一半會走到。
    target_line = ""
    if target:
        target_line = f'\n量測目標 `{target:g}`'
        if hit:
            target_line += f'（歷史達成率約 `{hit:.0%}`'
            if fail:
                target_line += f'，失敗率約 `{fail:.0%}`'
            target_line += '）'
    desc = (
        f'pattern_score `{r["pattern_score"]:.1f}`\n'
        f'{struct}\n'
        f'肩差 `{r["shoulder_diff"] * 100:.2f}%` · 頸線角度 `{r["neckline_angle"]:.1f}°`\n'
        f'{break_word} `{r["breakout_price"]:g}` · 時間 `{fmt_tw_time(r["breakout_time"])}`'
        + (f' · 量能比 `{r["volume_ratio"]:.2f}`' if r.get("volume_ratio") else '')
        + target_line
        + '\n程式規則生成的型態候選，非投資建議'
    )
    return {"title": title, "description": desc, "color": color}


def build_half_embed(h):
    tf_label = TF_LABEL.get(h["timeframe"], h["timeframe"])
    is_top = h.get("direction") == dirmod.TOP
    kind = "頭肩頂" if is_top else "頭肩底"
    rel = "高於" if is_top else "低於"
    color = 0xE67E22 if is_top else 0xF1C40F
    title = f'⚠️ {kind}觀察（左肩＋頭部）・{h["symbol"]}（{tf_label}）'
    desc = (
        f'左肩 `{h["LS_price"]:g}` → 頭部 `{h["H_price"]:g}`（比左肩{rel} '
        f'`{h["head_depth_vs_ls"] * 100:.2f}%`）\n'
        f'頭部時間 `{fmt_tw_time(h["H_time"])}`\n'
        '> 型態才走到一半：右肩、頸線、突破都還沒發生，純觀察，'
        '失敗率遠高於「突破」通知，不是進場訊號\n'
        '程式規則生成，非投資建議'
    )
    return {"title": title, "description": desc, "color": color}


def _best_neckline_in_range(pivots, lo_index, hi_index, direction):
    """半形態用的頸線點：底部取區間最高，頂部取區間最低。"""
    best = None
    for p in pivots:
        if p["index"] <= lo_index or p["index"] >= hi_index:
            continue
        if best is None:
            best = p
        elif direction == dirmod.BOTTOM and p["price"] > best["price"]:
            best = p
        elif direction == dirmod.TOP and p["price"] < best["price"]:
            best = p
    return best


def detect_half_pattern(df, timeframe, config, symbol=None, direction=dirmod.BOTTOM):
    """LS -> N1 -> H 只到頭部的早期示警（2026-07-24 新增，使用者要求提早通知）。

    只取「最新一個肩/頭 pivot」當頭部候選 H，往回找頭部最極端的 LS/N1 組合——
    同一時間只追蹤一組「目前正在形成」的候選，避免歷史上每個可能的頭部都各報
    一次造成洗版。回傳 None 表示目前沒有符合條件的候選。

    2026-07-30：支援頭肩頂（direction="top"，肩/頭改用 swing high、頸線改用 swing low）。
    """
    swings_df = find_swing_points(df, config.pivot_window(timeframe))
    swing_lows, swing_highs = extract_swing_points(swings_df)
    shoulder_pool = dirmod.shoulder_pivots(swing_lows, swing_highs, direction)
    neckline_pool = dirmod.neckline_pivots(swing_lows, swing_highs, direction)
    if len(shoulder_pool) < 2:
        return None
    Hp = shoulder_pool[-1]
    min_head_depth = config.min_head_depth(timeframe)
    best = None
    for LS in shoulder_pool:
        if LS["index"] >= Hp["index"]:
            continue
        if not dirmod.head_is_beyond(Hp["price"], LS["price"], direction):
            continue
        N1 = _best_neckline_in_range(neckline_pool, LS["index"], Hp["index"], direction)
        if N1 is None:
            continue
        head_depth_vs_ls = abs(LS["price"] - Hp["price"]) / LS["price"]
        if head_depth_vs_ls < min_head_depth:
            continue
        if best is None or head_depth_vs_ls > best["head_depth_vs_ls"]:
            best = {
                "symbol": symbol, "timeframe": timeframe, "direction": direction,
                "LS_time": LS["time"], "LS_price": LS["price"],
                "N1_time": N1["time"], "N1_price": N1["price"],
                "H_time": Hp["time"], "H_price": Hp["price"],
                "head_depth_vs_ls": head_depth_vs_ls,
            }
    return best


def scan(symbols, config):
    """對每個 symbol x timeframe 只抓一次 K 線，同時跑四種偵測（避免重複打 API）：
    頭肩底突破／頭肩底半形態／頭肩頂跌破／頭肩頂半形態。

    回傳 (bottom_breakouts, bottom_halves, top_breakouts, top_halves)。
    """
    b_breaks, b_halves, t_breaks, t_halves = [], [], [], []
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
                b_breaks.extend(r for r in results if r["breakout_detected"])
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] {symbol} {tf} 頭肩底偵測失敗: {e}")
            try:
                results = detect_head_and_shoulders_top(df, tf, config, symbol=symbol)
                t_breaks.extend(r for r in results if r["breakout_detected"])
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] {symbol} {tf} 頭肩頂偵測失敗: {e}")
            try:
                half = detect_half_pattern(df, tf, config, symbol=symbol,
                                           direction=dirmod.BOTTOM)
                if half is not None:
                    b_halves.append(half)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] {symbol} {tf} 頭肩底半形態偵測失敗: {e}")
            try:
                half = detect_half_pattern(df, tf, config, symbol=symbol,
                                           direction=dirmod.TOP)
                if half is not None:
                    t_halves.append(half)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] {symbol} {tf} 頭肩頂半形態偵測失敗: {e}")
    return b_breaks, b_halves, t_breaks, t_halves


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

    # 四類各自獨立的冷啟動判定與 state 檔
    paths = {
        "bottom_breakout": BREAKOUT_STATE_PATH,
        "bottom_half": HALF_STATE_PATH,
        "top_breakout": TOP_BREAKOUT_STATE_PATH,
        "top_half": TOP_HALF_STATE_PATH,
    }
    cold = {k: not os.path.exists(p) for k, p in paths.items()}

    t0 = time.time()
    symbols = fetch_universe()
    b_breaks, b_halves, t_breaks, t_halves = scan(symbols, config)
    elapsed = time.time() - t0

    break_key = lambda r: f'{r["symbol"]}|{r["timeframe"]}|{r["breakout_time"]}'  # noqa: E731
    half_key = lambda h: f'{h["symbol"]}|{h["timeframe"]}|{h["H_time"]}'          # noqa: E731
    break_score = lambda r: r["pattern_score"]                                     # noqa: E731
    half_score = lambda h: h["head_depth_vs_ls"]                                   # noqa: E731

    jobs = [
        ("bottom_breakout", b_breaks, break_key, break_score),
        ("bottom_half", b_halves, half_key, half_score),
        ("top_breakout", t_breaks, break_key, break_score),
        ("top_half", t_halves, half_key, half_score),
    ]

    new_by_kind, keys_by_kind = {}, {}
    for kind, items, kf, sf in jobs:
        new_items, keys = _dedupe_new(items, kf, sf, load_state(paths[kind]), cold[kind])
        new_by_kind[kind] = new_items
        keys_by_kind[kind] = keys

    # 半形態（觀察）排前面，突破（確認）排後面，讓最重要的訊息在最下方最顯眼
    embeds = (
        [build_half_embed(h) for h in new_by_kind["bottom_half"]]
        + [build_half_embed(h) for h in new_by_kind["top_half"]]
        + [build_breakout_embed(r) for r in new_by_kind["bottom_breakout"]]
        + [build_breakout_embed(r) for r in new_by_kind["top_breakout"]]
    )
    if embeds and webhook and not dry_run:
        _post_discord_embeds(webhook, embeds)

    for kind, path in paths.items():
        save_state(path, {k: True for k in keys_by_kind[kind]})

    seen_counts = {"bottom_breakout": len(b_breaks), "bottom_half": len(b_halves),
                   "top_breakout": len(t_breaks), "top_half": len(t_halves)}
    print(f"[ihs_scan] universe={len(symbols)} elapsed={elapsed:.1f}s "
          f"discord={'on' if webhook else 'off(dry/no-secret)'}{' DRY_RUN' if dry_run else ''}")
    for kind, _, _, _ in jobs:
        print(f"  {kind:16s} seen={seen_counts[kind]:5d} unique={len(keys_by_kind[kind]):5d} "
              f"new={len(new_by_kind[kind]):4d} cold_start={cold[kind]}")
    for kind in ("bottom_breakout", "top_breakout"):
        for r in new_by_kind[kind][:10]:
            print(f'  NEW {kind} {r["symbol"]} {r["timeframe"]} '
                  f'score={r["pattern_score"]:.1f} time={r["breakout_time"]}')
    for kind in ("bottom_half", "top_half"):
        for h in new_by_kind[kind][:10]:
            print(f'  NEW {kind} {h["symbol"]} {h["timeframe"]} '
                  f'depth={h["head_depth_vs_ls"] * 100:.2f}% H_time={h["H_time"]}')


if __name__ == "__main__":
    main()
