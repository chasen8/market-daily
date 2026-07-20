# -*- coding: utf-8 -*-
"""量能牆報表主程式（量能牆專案，GitHub Actions 雲端自足模式）。

流程（CI 每 5 分鐘一跑，working directory = market-daily repo 根目錄）：
  1. 抓幣安 15m K 線（最近 500 根 x 5 幣種），跑引擎 A（detect_volume_walls）。
  2. 每幣種自己抓當下逐檔深度（REST /api/v3/depth limit=1000），跑
     detect_dom_walls 後把「輕量偵測結果」追加到 data/depth_seq/{SYMBOL}.jsonl
     （純文字 JSONL，一行一快照：ts_utc、mid、bucket_width、walls；
     追加後修剪只留最近 48 小時）。track_persistence 吃整個序列。
  3. 引擎 C：每幣種抓 OKX 永續合約近期強平事件，去重後追加到
     data/liq_seq/{SYMBOL}.jsonl（純文字 JSONL，修剪只留最近 14 天），
     算出量能牆附近的歷史強平密度——**回顧性統計，不是預測**，也是
     跨交易所比對（OKX 永續 vs 幣安現貨），見 liq_seq.py 模組說明。
  4. 產出自含 SVG/HTML 報表 docs/walls.html（無外部 CDN、無 fetch）。
  5. 若環境變數 DISCORD_WEBHOOK_URL 存在，對「新持續牆在現價 +-2xATR 內」與
     「持續牆消失」發警報；不存在就 log 一行 skip。webhook URL 絕不落檔、
     絕不印出、絕不寫進任何檔案內容。

路徑相對 repo 根目錄；本機開發用環境變數 WALLS_DATA_DIR 指到任一暫存目錄。
無第三方相依，Python 3.12+ 標準庫即可。
"""
from __future__ import annotations

import html as html_lib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wall_detect import (  # noqa: E402
    atr_series,
    detect_dom_walls,
    detect_volume_walls,
    track_persistence,
)
from liq_seq import (  # noqa: E402
    RETENTION_DAYS as LIQ_RETENTION_DAYS,
    dedupe_merge as liq_dedupe_merge,
    fetch_liquidations,
    liq_density_near_price,
    load_liq_seq,
    prune_liq_seq as prune_liq_seq_events,
    write_liq_seq,
)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
# GitHub Actions 的美國 runner 會被 api.binance.com 以 HTTP 451 geo-block，
# 依序退到 data-api.binance.vision（幣安官方公開行情鏡像；同 whale_scan.py 做法）
SPOT_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]
KLINES_PATH = "/api/v3/klines?symbol={symbol}&interval=15m&limit=500"
DEPTH_PATH = "/api/v3/depth?symbol={symbol}&limit=1000"
# 路徑相對 repo 根（CI 的 working directory）；本機開發用 WALLS_DATA_DIR 覆寫
BASE_DIR = Path(os.environ.get("WALLS_DATA_DIR", "."))
SEQ_DIR = BASE_DIR / "data" / "depth_seq"
OUTPUT_FILE = BASE_DIR / "docs" / "walls.html"
SEQ_RETENTION_HOURS = 48
TIMEOUT_SEC = 10
DEPTH_RETRIES = 2
DISPLAY_BARS = 150  # 圖表顯示的最新棒數（偵測仍用完整 500 根）
CHART_W = 900
CHART_H = 360
DOM_W = 220
MARGIN = {"top": 24, "right": 16, "bottom": 28, "left": 64}


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}"
    print(line)


# --------------------------------------------------------------------------
# 資料抓取 / 讀取
# --------------------------------------------------------------------------

def _get_json(path: str):
    """對 SPOT_HOSTS 依序嘗試 GET，回傳解析後 JSON；全部失敗拋最後的例外。"""
    last_err: Exception | None = None
    for host in SPOT_HOSTS:
        url = host + path
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT_SEC) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            log(f"WARN {host} 失敗（{type(e).__name__}: {e}），試下一個 host")
    assert last_err is not None
    raise last_err


def fetch_klines(symbol: str) -> list[dict]:
    raw = _get_json(KLINES_PATH.format(symbol=symbol))
    klines = []
    for row in raw:
        klines.append(
            {
                "open_time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "quote_volume": float(row[7]),
            }
        )
    return klines


def fetch_depth(symbol: str) -> dict | None:
    """抓當下逐檔深度快照（同 depth_collector.py 的做法，含重試與退避）。"""
    path = DEPTH_PATH.format(symbol=symbol)
    for attempt in range(1 + DEPTH_RETRIES):
        try:
            data = _get_json(path)
            return {
                "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "symbol": symbol,
                "bids": data.get("bids", []),
                "asks": data.get("asks", []),
            }
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            log(f"WARN {symbol} depth attempt {attempt + 1} failed: {e}")
            if attempt < DEPTH_RETRIES:
                time.sleep(2 * (attempt + 1))
    log(f"ERROR {symbol} depth 全部重試失敗，本輪不新增快照")
    return None


def _seq_path(symbol: str) -> Path:
    return SEQ_DIR / f"{symbol}.jsonl"


def load_seq(symbol: str) -> list[dict]:
    """讀某幣種的輕量偵測序列（不存在回空 list，壞行跳過）。"""
    path = _seq_path(symbol)
    if not path.exists():
        return []
    seq: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                seq.append(json.loads(line))
            except json.JSONDecodeError:
                log(f"WARN {path.name}:{line_no} JSON 解析失敗，跳過")
    seq.sort(key=lambda s: s.get("ts_utc", ""))
    return seq


def prune_seq(seq: list[dict], now: datetime) -> list[dict]:
    """只留最近 SEQ_RETENTION_HOURS 小時的快照；ts 解析不了的丟棄。"""
    cutoff = now - timedelta(hours=SEQ_RETENTION_HOURS)
    kept = []
    for s in seq:
        try:
            ts = datetime.fromisoformat(s["ts_utc"])
        except (KeyError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            kept.append(s)
    return kept


def write_seq(symbol: str, seq: list[dict]) -> None:
    """整檔重寫（追加＋修剪後的結果）。純文字 JSONL、UTF-8、無 BOM。"""
    SEQ_DIR.mkdir(parents=True, exist_ok=True)
    path = _seq_path(symbol)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for s in seq:
            f.write(json.dumps(s, separators=(",", ":"), ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# Discord 警報（webhook URL 只存在於環境變數，絕不落檔/印出）
# --------------------------------------------------------------------------

def send_discord_alert(webhook_url: str, content: str) -> bool:
    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        # Discord 前的 Cloudflare 會 403 擋 Python-urllib 預設 UA，必須自訂
        headers={"Content-Type": "application/json", "User-Agent": "VolumeWallBot/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, TimeoutError) as e:
        log(f"WARN discord 警報發送失敗（不含 URL）: {type(e).__name__}")
        return False


def build_alerts(
    symbol: str,
    persistence: dict,
    current_price: float,
    atr_now: float,
    latest_idx: int,
) -> list[str]:
    """組出這個幣種要發的 Discord 訊息內容（純文字，不含任何 secret）。

    只對「發生在最新一個快照」的事件發警報（newly_persistent 的 idx、
    disappeared 的 disappeared_at_idx 要等於 latest_idx），避免腳本每次
    重新執行時，把整天歷史裡早就發生過的形成/消失事件重複洗版式重發。
    """
    msgs = []
    for w in persistence.get("newly_persistent", []):
        if w.get("idx") != latest_idx:
            continue
        if abs(current_price - w["price"]) <= 2 * atr_now:
            msgs.append(
                f"[{symbol}] 新持續牆形成於 {w['price']:.4f}（{w['side']}，"
                f"約 ${w['usd']:,.0f}），現價 {current_price:.4f} 在 2xATR 範圍內"
            )
    for w in persistence.get("disappeared", []):
        if w.get("disappeared_at_idx") != latest_idx:
            continue
        reason = "被吃" if w["reason"] == "eaten" else "撤單（疑似幌騙）"
        msgs.append(
            f"[{symbol}] 持續牆消失於 {w['price']:.4f}（{w['side']}，"
            f"連續 {w['streak']} 次快照）：{reason}"
        )
    return msgs


# --------------------------------------------------------------------------
# TradingView 貼上字串（Pine 指標輸入欄用，格式與 Pine 端解析器約定，勿改）
# --------------------------------------------------------------------------

def _fmt_tv_price(p: float) -> str:
    """牆價原值、十進位表示、絕不出現科學記號；去掉無意義的尾零。"""
    s = f"{p:.8f}".rstrip("0").rstrip(".")
    return s if s else "0"


def build_tv_string(persistence: dict, max_items: int = 12) -> str:
    """組出單一幣種的 TradingView 貼上字串。

    格式（Pine 端解析器照此實作，必須完全一致）：
        單項 = 價格|側|美元百萬   例：78.21|B|0.92（B=bid 買牆、A=ask 賣牆；
        美元百萬固定 2 位小數）；多項用 ';' 相接，不含空白、不含換行。
    內容 = 所有持續牆 + 未達持續的現況牆，依美元由大到小，上限 max_items。
    沒有任何牆時回傳空字串。
    """
    entries: list[dict] = []
    persistent = persistence.get("persistent", [])
    for w in persistent:
        entries.append({"price": w["price"], "side": w["side"], "usd": w["usd"]})
    # 現況牆中，排除已被持續牆涵蓋者（存活 track 的 last_price 即取自
    # 最新快照中被配對到的那道現況牆，價格值相同；用小 epsilon 防浮點誤差）
    for w in persistence.get("current", []):
        covered = any(
            abs(p["price"] - w["price"]) <= max(abs(w["price"]) * 1e-9, 1e-12)
            for p in entries
        )
        if not covered:
            entries.append({"price": w["price"], "side": w["side"], "usd": w["usd"]})

    entries.sort(key=lambda e: e["usd"], reverse=True)
    entries = entries[:max_items]
    return ";".join(
        f"{_fmt_tv_price(e['price'])}|{'B' if e['side'] == 'bid' else 'A'}|{e['usd'] / 1e6:.2f}"
        for e in entries
    )


# --------------------------------------------------------------------------
# SVG / HTML 產出（見 dataviz skill：色彩最後決定、暗色可讀、圖例齊全）
# --------------------------------------------------------------------------

def _fmt_price(p: float) -> str:
    if p >= 1000:
        return f"{p:,.1f}"
    if p >= 1:
        return f"{p:,.3f}"
    return f"{p:.5f}"


def render_symbol_svg(
    symbol: str,
    klines: list[dict],
    volume_walls: list[dict],
    dom_current: list[dict],
    persistence: dict,
) -> str:
    display = klines[-DISPLAY_BARS:] if len(klines) > DISPLAY_BARS else klines
    if not display:
        return "<p>（無 K 線資料）</p>"

    lows = [k["low"] for k in display]
    highs = [k["high"] for k in display]
    price_min = min(lows)
    price_max = max(highs)

    # 把「顯示範圍內」的量能牆與目前 DOM 牆價位一起納入 y 軸範圍，
    # 避免牆帶超出圖表看不到；但排除離現價太遠（>15%）的舊牆，
    # 避免單一極端值把整張圖壓扁。
    current_price = display[-1]["close"]
    relevant_wall_prices = [
        w["price"]
        for w in volume_walls
        if abs(w["price"] - current_price) <= current_price * 0.15
    ]
    relevant_wall_prices += [
        w["price"]
        for w in dom_current
        if abs(w["price"] - current_price) <= current_price * 0.15
    ]
    if relevant_wall_prices:
        price_min = min(price_min, min(relevant_wall_prices))
        price_max = max(price_max, max(relevant_wall_prices))
    if price_max <= price_min:
        price_max = price_min + max(price_min * 0.01, 1e-9)
    pad = (price_max - price_min) * 0.06
    price_min -= pad
    price_max += pad

    plot_w = CHART_W - MARGIN["left"] - MARGIN["right"]
    plot_h = CHART_H - MARGIN["top"] - MARGIN["bottom"]
    n = len(display)
    slot_w = plot_w / n
    candle_w = max(1.5, min(10.0, slot_w * 0.6))

    def y(price: float) -> float:
        frac = (price - price_min) / (price_max - price_min)
        return MARGIN["top"] + plot_h * (1 - frac)

    def x(i: int) -> float:
        return MARGIN["left"] + slot_w * i + slot_w / 2

    parts: list[str] = []
    parts.append(
        f'<svg class="wall-chart" viewBox="0 0 {CHART_W + DOM_W} {CHART_H}" '
        f'role="img" aria-label="{symbol} K 線與量能牆圖">'
    )

    # 背景
    parts.append(
        f'<rect x="0" y="0" width="{CHART_W + DOM_W}" height="{CHART_H}" '
        f'fill="var(--surface-1)"/>'
    )

    # 水平格線 + y 軸價格標籤（5 條）
    for i in range(5):
        frac = i / 4
        price = price_min + (price_max - price_min) * frac
        yy = MARGIN["top"] + plot_h * (1 - frac)
        parts.append(
            f'<line x1="{MARGIN["left"]}" y1="{yy:.1f}" x2="{CHART_W - MARGIN["right"]:.1f}" '
            f'y2="{yy:.1f}" stroke="var(--gridline)" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{MARGIN["left"] - 8}" y="{yy + 3:.1f}" text-anchor="end" '
            f'class="axis-label">{_fmt_price(price)}</text>'
        )

    # 量能牆水平帶（先畫，墊在 K 棒下面）：顏色分類型，不透明度=強度
    for w in volume_walls:
        if w["price"] < price_min or w["price"] > price_max:
            continue
        yy = y(w["price"])
        color_var = "--wall-big" if w["type"] == "big_volume" else "--wall-doji"
        opacity = max(0.12, min(0.85, w["strength"] / 100.0))
        parts.append(
            f'<rect x="{MARGIN["left"]}" y="{yy - 1.5:.1f}" width="{plot_w:.1f}" '
            f'height="3" fill="var({color_var})" opacity="{opacity:.2f}">'
            f'<title>{"爆量牆" if w["type"] == "big_volume" else "十字吸籌牆"} '
            f'@ {_fmt_price(w["price"])}　強度 {w["strength"]:.0f}%　'
            f'約 ${w["volume_usd"]:,.0f}</title></rect>'
        )

    # 持續牆標記（DOM 引擎）：violet 三角形貼在右緣
    for w in persistence.get("persistent", []):
        if w["price"] < price_min or w["price"] > price_max:
            continue
        yy = y(w["price"])
        xx = CHART_W - MARGIN["right"]
        parts.append(
            f'<path d="M {xx-9:.1f} {yy-6:.1f} L {xx:.1f} {yy:.1f} L {xx-9:.1f} {yy+6:.1f} Z" '
            f'fill="var(--wall-persistent)">'
            f'<title>持續掛單牆 @ {_fmt_price(w["price"])}（連續 {w["streak"]} 次快照，'
            f'約 ${w["usd"]:,.0f}）</title></path>'
        )

    # K 棒
    for i, k in enumerate(display):
        cx = x(i)
        up = k["close"] >= k["open"]
        color_var = "--candle-up" if up else "--candle-down"
        y_high = y(k["high"])
        y_low = y(k["low"])
        y_open = y(k["open"])
        y_close = y(k["close"])
        body_top = min(y_open, y_close)
        body_h = max(1.0, abs(y_close - y_open))
        parts.append(
            f'<line x1="{cx:.1f}" y1="{y_high:.1f}" x2="{cx:.1f}" y2="{y_low:.1f}" '
            f'stroke="var({color_var})" stroke-width="1.2"/>'
        )
        parts.append(
            f'<rect x="{cx - candle_w/2:.1f}" y="{body_top:.1f}" width="{candle_w:.1f}" '
            f'height="{body_h:.1f}" fill="var({color_var})">'
            f'<title>{datetime.fromtimestamp(k["open_time"]/1000, tz=timezone.utc).strftime("%m-%d %H:%M")} UTC\n'
            f'開 {_fmt_price(k["open"])} 高 {_fmt_price(k["high"])} 低 {_fmt_price(k["low"])} '
            f'收 {_fmt_price(k["close"])}</title></rect>'
        )

    # x 軸時間標籤（頭尾與中間）
    for i in (0, n // 2, n - 1):
        cx = x(i)
        t = datetime.fromtimestamp(display[i]["open_time"] / 1000, tz=timezone.utc)
        parts.append(
            f'<text x="{cx:.1f}" y="{CHART_H - MARGIN["bottom"] + 16}" '
            f'text-anchor="middle" class="axis-label">{t.strftime("%m/%d %H:%M")}</text>'
        )

    parts.append(
        f'<line x1="{MARGIN["left"]}" y1="{MARGIN["top"] + plot_h:.1f}" '
        f'x2="{CHART_W - MARGIN["right"]:.1f}" y2="{MARGIN["top"] + plot_h:.1f}" '
        f'stroke="var(--baseline)" stroke-width="1"/>'
    )

    # 右側：DOM 深度直方（bid 綠 / ask 紅），共用同一組 y 座標
    dom_x0 = CHART_W + 12
    dom_plot_w = DOM_W - 24
    if dom_current:
        max_usd = max(w["usd"] for w in dom_current)
    else:
        max_usd = 1.0
    parts.append(
        f'<text x="{dom_x0}" y="{MARGIN["top"] - 6}" class="axis-label">DOM 掛單牆（現況）</text>'
    )
    for w in dom_current:
        if w["price"] < price_min or w["price"] > price_max:
            continue
        yy = y(w["price"])
        bar_w = max(2.0, dom_plot_w * (w["usd"] / max_usd) if max_usd > 0 else 0)
        color_var = "--candle-up" if w["side"] == "bid" else "--candle-down"
        w_bucket = w.get("bucket_key", -(10 ** 9))
        is_persist = any(
            abs(pw["bucket_key"] - w_bucket) <= 1
            for pw in persistence.get("persistent", [])
        )
        parts.append(
            f'<rect x="{dom_x0:.1f}" y="{yy-3:.1f}" width="{bar_w:.1f}" height="6" '
            f'fill="var({color_var})" opacity="{0.85 if is_persist else 0.55}">'
            f'<title>{"買盤" if w["side"] == "bid" else "賣盤"}掛單牆 @ {_fmt_price(w["price"])}　'
            f'約 ${w["usd"]:,.0f}</title></rect>'
        )
    parts.append(
        f'<line x1="{dom_x0:.1f}" y1="{MARGIN["top"]}" x2="{dom_x0:.1f}" '
        f'y2="{MARGIN["top"] + plot_h:.1f}" stroke="var(--baseline)" stroke-width="1"/>'
    )

    parts.append("</svg>")
    return "".join(parts)


def render_table(volume_walls: list[dict], dom_current: list[dict], persistence: dict) -> str:
    rows = []
    for w in volume_walls[-15:]:
        t = datetime.fromtimestamp(w["open_time"] / 1000, tz=timezone.utc)
        rows.append(
            "<tr><td>量能牆</td><td>{}</td><td>{}</td><td>{}</td><td>{:.0f}%</td></tr>".format(
                html_lib.escape("爆量" if w["type"] == "big_volume" else "十字吸籌"),
                t.strftime("%m-%d %H:%M UTC"),
                _fmt_price(w["price"]),
                w["strength"],
            )
        )
    for w in dom_current:
        rows.append(
            "<tr><td>DOM 牆</td><td>{}</td><td>-</td><td>{}</td><td>${:,.0f}</td></tr>".format(
                "買盤" if w["side"] == "bid" else "賣盤",
                _fmt_price(w["price"]),
                w["usd"],
            )
        )
    for w in persistence.get("disappeared", []):
        rows.append(
            "<tr><td>持續牆消失</td><td>{}</td><td>-</td><td>{}</td><td>{}</td></tr>".format(
                "買盤" if w["side"] == "bid" else "賣盤",
                _fmt_price(w["price"]),
                "被吃" if w["reason"] == "eaten" else "撤單",
            )
        )
    if not rows:
        rows.append('<tr><td colspan="5">（本期無資料）</td></tr>')
    return (
        "<table class=\"data-table\"><thead><tr>"
        "<th>類型</th><th>子類型</th><th>時間</th><th>價位</th><th>強度/金額/結果</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def render_liq_row(liq_seq: list[dict], persistence: dict, current_price: float) -> str:
    """引擎 C 顯示列：歷史強平密度，回顧性統計、非預測，文案要講清楚界線。

    資料來源是 OKX 永續合約（見 liq_seq.py），跟本頁其餘引擎（幣安現貨/
    深度）是跨交易所比對，價格高度相關但不完全一致，這裡也要提醒一次。
    """
    if not liq_seq:
        return (
            '<div class="liq-row"><span class="liq-label">歷史強平密度（引擎C）</span>'
            '<span class="liq-empty">近 {days} 天無 OKX 永續強平記錄（或抓取失敗）</span></div>'
        ).format(days=LIQ_RETENTION_DAYS)

    near_current = liq_density_near_price(liq_seq, current_price, band_pct=0.005)
    parts = [
        f"近 {LIQ_RETENTION_DAYS} 天共 {len(liq_seq)} 筆　現價 ±0.5% 內 "
        f"{near_current['count']} 筆（約 ${near_current['notional_usd']:,.0f}）"
    ]
    wall_bits = []
    for w in persistence.get("persistent", [])[:5]:
        d = liq_density_near_price(liq_seq, w["price"], band_pct=0.005)
        wall_bits.append(f"{_fmt_price(w['price'])} 附近 {d['count']} 筆")
    if wall_bits:
        parts.append("持續牆附近：" + "、".join(wall_bits))

    return (
        '<div class="liq-row"><span class="liq-label">歷史強平密度（引擎C）</span>'
        f'<span class="liq-body">{html_lib.escape("　".join(parts))}</span></div>'
        '<div class="liq-disclaimer">回顧性統計，不是預測；資料來源 OKX 永續合約'
        '（非本頁其餘引擎使用的幣安現貨/深度），跨交易所比對，價格高度相關但不完全一致，'
        '僅供佐證量能牆可信度參考，不構成任何預測。</div>'
    )


PAGE_CSS = """
:root { color-scheme: light; }
.viz-root {
  --surface-1: #fcfcfb; --page-plane: #f9f9f7;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #898781;
  --gridline: #e1e0d9; --baseline: #c3c2b7; --border: rgba(11,11,11,0.10);
  --candle-up: #0ca30c; --candle-down: #d03b3b;
  --wall-big: #eb6834; --wall-doji: #2a78d6; --wall-persistent: #4a3aa7;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    --surface-1: #1a1a19; --page-plane: #0d0d0d;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
    --gridline: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
    --candle-up: #0ca30c; --candle-down: #e66767;
    --wall-big: #d95926; --wall-doji: #3987e5; --wall-persistent: #9085e9;
  }
}
:root[data-theme="dark"] .viz-root {
  --surface-1: #1a1a19; --page-plane: #0d0d0d;
  --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
  --gridline: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
  --candle-up: #0ca30c; --candle-down: #e66767;
  --wall-big: #d95926; --wall-doji: #3987e5; --wall-persistent: #9085e9;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px; background: var(--page-plane); color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}
h1 { font-size: 22px; margin: 0 0 4px; }
.meta { color: var(--text-secondary); font-size: 13px; margin-bottom: 20px; }
.legend { display: flex; flex-wrap: wrap; gap: 16px; font-size: 13px; color: var(--text-secondary);
  margin-bottom: 20px; padding: 10px 14px; background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 8px; }
.legend .swatch { display: inline-block; width: 12px; height: 12px; border-radius: 3px; margin-right: 6px; vertical-align: -1px; }
section.symbol { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
  padding: 16px 18px; margin-bottom: 22px; }
section.symbol h2 { margin: 0 0 6px; font-size: 18px; }
.summary-line { color: var(--text-secondary); font-size: 13px; margin-bottom: 10px; }
.summary-line b { color: var(--text-primary); }
.wall-chart { width: 100%; height: auto; display: block; }
.axis-label { fill: var(--text-muted); font-size: 10px; }
.alerts { font-size: 12px; color: var(--text-secondary); margin-top: 8px; }
.alerts.skipped { font-style: italic; }
details.table-view { margin-top: 10px; }
details.table-view summary { cursor: pointer; color: var(--text-secondary); font-size: 13px; }
table.data-table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px; }
table.data-table th, table.data-table td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--gridline); }
table.data-table th { color: var(--text-muted); font-weight: 600; }
footer { color: var(--text-muted); font-size: 12px; margin-top: 12px; }
.tv-row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 10px; font-size: 12px; }
.tv-row .tv-label { color: var(--text-secondary); white-space: nowrap; }
.tv-row code { background: var(--page-plane); border: 1px solid var(--border); border-radius: 6px;
  padding: 3px 8px; font-size: 11px; color: var(--text-primary); overflow-x: auto;
  max-width: 100%; white-space: nowrap; flex: 1 1 auto; min-width: 0; }
.tv-row .tv-empty { color: var(--text-muted); font-style: italic; }
.tv-row button { font: inherit; font-size: 12px; padding: 3px 12px; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--border); background: var(--surface-1); color: var(--text-primary); }
.tv-row button:hover { background: var(--page-plane); }
.liq-row { display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px; margin-top: 8px; font-size: 12px; }
.liq-row .liq-label { color: var(--text-secondary); white-space: nowrap; font-weight: 600; }
.liq-row .liq-body { color: var(--text-primary); }
.liq-row .liq-empty { color: var(--text-muted); font-style: italic; }
.liq-disclaimer { font-size: 11px; color: var(--text-muted); font-style: italic; margin-top: 2px; }
"""

COPY_JS = """
function copyTv(id, btn) {
  var el = document.getElementById(id);
  if (!el) { return; }
  var txt = el.textContent;
  function done() {
    var old = btn.textContent;
    btn.textContent = '已複製';
    setTimeout(function () { btn.textContent = old; }, 1500);
  }
  function fallback() {
    var range = document.createRange();
    range.selectNodeContents(el);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    btn.textContent = '請手動複製';
    setTimeout(function () { btn.textContent = '複製'; }, 2000);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(txt).then(done, fallback);
  } else {
    fallback();
  }
}
"""


def build_html(symbol_blocks: list[str], generated_at: str, discord_status: str) -> str:
    legend = """
    <div class="legend">
      <span><span class="swatch" style="background:var(--candle-up)"></span>收漲 / 買盤掛單牆</span>
      <span><span class="swatch" style="background:var(--candle-down)"></span>收跌 / 賣盤掛單牆</span>
      <span><span class="swatch" style="background:var(--wall-big)"></span>爆量牆（引擎A）</span>
      <span><span class="swatch" style="background:var(--wall-doji)"></span>十字吸籌牆（引擎A）</span>
      <span><span class="swatch" style="background:var(--wall-persistent)"></span>持續掛單牆標記（引擎B，右緣三角形）</span>
      <span>水平帶不透明度＝牆強度百分位</span>
      <span>歷史強平密度（引擎C）：OKX 永續合約，回顧性統計，非預測，非幣安資料</span>
    </div>
    """
    body = f"""
    <div class="viz-root">
      <h1>量能牆報表</h1>
      <div class="meta">產出時間：{generated_at}（UTC）　資料：幣安 15m K 線（引擎A）＋逐檔深度快照（引擎B）
      Discord 警報：{discord_status}</div>
      {legend}
      {''.join(symbol_blocks)}
      <footer>純靜態頁面，無外部資源／CDN。門檻詳見 docs/project-charter.md 訊號規格 v1（拍腦袋 v1 值，待回測校準）。</footer>
    </div>
    """
    return f"<!doctype html><html lang=\"zh-Hant\"><head><meta charset=\"utf-8\"/>" \
           f"<title>量能牆報表</title><style>{PAGE_CSS}</style>" \
           f"<script>{COPY_JS}</script></head><body>{body}</body></html>"


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if webhook_url:
        log("INFO DISCORD_WEBHOOK_URL 已設定，將發送警報")
        discord_status_label = "已啟用"
    else:
        log("INFO DISCORD_WEBHOOK_URL 未設定，skip 警報發送")
        discord_status_label = "未設定（skip）"

    symbol_blocks = []
    summary_lines = []
    all_alert_msgs = []

    for symbol in SYMBOLS:
        try:
            klines = fetch_klines(symbol)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as e:
            log(f"ERROR 抓 {symbol} K 線失敗，跳過此幣種: {e}")
            continue
        if not klines:
            log(f"WARN {symbol} K 線為空，跳過")
            continue
        # 最後一根可能是尚未收盤的當根，只用已收盤棒做偵測（bar-close confirmation）。
        closed_klines = klines[:-1] if len(klines) > 1 else klines
        volume_walls = detect_volume_walls(closed_klines)
        atr_now = atr_series(closed_klines, 14)[-1] if closed_klines else 0.0
        current_price = closed_klines[-1]["close"]

        # 引擎 B：自己抓當下深度 -> 偵測 -> 追加到輕量序列 -> 修剪 48h
        now = datetime.now(timezone.utc)
        dom_seq = load_seq(symbol)
        snapshot = fetch_depth(symbol)
        appended = False
        if snapshot is not None:
            det = detect_dom_walls(snapshot)
            det["ts_utc"] = snapshot["ts_utc"]
            dom_seq.append(det)
            appended = True
        dom_seq = prune_seq(dom_seq, now)
        write_seq(symbol, dom_seq)

        persistence = track_persistence(dom_seq)
        dom_current = persistence["current"]

        # 引擎 C：歷史強平事件密度（回顧性統計，非預測；OKX 永續合約，
        # 見 liq_seq.py 模組 docstring 的 API 查證與跨交易所比對限制說明）
        liq_seq = load_liq_seq(BASE_DIR, symbol)
        new_liq = fetch_liquidations(symbol)
        liq_seq = liq_dedupe_merge(liq_seq, new_liq)
        liq_seq = prune_liq_seq_events(liq_seq, now, LIQ_RETENTION_DAYS)
        write_liq_seq(BASE_DIR, symbol, liq_seq)

        gone = persistence["disappeared"]
        if len(gone) > 5 and all(w["reason"] == "cancelled" for w in gone):
            log(
                f"WARNING {symbol} 消失牆 {len(gone)} 道且 100% 為 cancelled，"
                f"疑似追蹤異常（如 mid 漂移導致身分比對斷裂）而非市場行為，請檢查"
            )

        n_big = sum(1 for w in volume_walls if w["type"] == "big_volume")
        n_doji = sum(1 for w in volume_walls if w["type"] == "doji")
        n_persist = len(persistence["persistent"])
        n_gone = len(persistence["disappeared"])
        summary_lines.append(
            f"{symbol}: 爆量牆 {n_big}　十字牆 {n_doji}　DOM 現況牆 {len(dom_current)}　"
            f"持續牆 {n_persist}　消失牆 {n_gone}（快照數 {len(dom_seq)}）　"
            f"歷史強平 {len(liq_seq)} 筆（{LIQ_RETENTION_DAYS}天窗，OKX）"
        )

        # 只在本輪確實新增了快照時發警報：CI 每 5 分鐘一跑、每跑恰好新增
        # 一個快照，latest-snapshot 過濾語意正確；若本輪抓深度失敗，
        # 最新快照是上一輪的，重發它的事件會重複洗版，所以跳過。
        alerts = (
            build_alerts(symbol, persistence, current_price, atr_now, len(dom_seq) - 1)
            if appended
            else []
        )
        all_alert_msgs.extend(alerts)

        tv_string = build_tv_string(persistence)
        if tv_string:
            tv_html = (
                f'<div class="tv-row"><span class="tv-label">TradingView 貼上字串</span>'
                f'<code id="tv-{symbol}">{html_lib.escape(tv_string)}</code>'
                f'<button type="button" onclick="copyTv(\'tv-{symbol}\', this)">複製</button></div>'
            )
        else:
            tv_html = (
                '<div class="tv-row"><span class="tv-label">TradingView 貼上字串</span>'
                '<span class="tv-empty">目前無 DOM 牆</span></div>'
            )

        svg = render_symbol_svg(symbol, closed_klines, volume_walls, dom_current, persistence)
        table = render_table(volume_walls, dom_current, persistence)
        liq_html = render_liq_row(liq_seq, persistence, current_price)
        alert_html = ""
        if alerts:
            alert_html = "<div class=\"alerts\">" + "<br/>".join(
                html_lib.escape(a) for a in alerts
            ) + "</div>"

        symbol_blocks.append(f"""
        <section class="symbol">
          <h2>{symbol}</h2>
          <div class="summary-line">現價 <b>{_fmt_price(current_price)}</b>
            爆量牆 <b>{n_big}</b>　十字吸籌牆 <b>{n_doji}</b>
            DOM 現況牆 <b>{len(dom_current)}</b>　持續牆 <b>{n_persist}</b>
            消失牆 <b>{n_gone}</b>（深度快照數 {len(dom_seq)}）</div>
          {svg}
          {tv_html}
          {liq_html}
          {alert_html}
          <details class="table-view"><summary>資料表格（最新 15 筆量能牆／現況 DOM 牆／消失牆）</summary>
            {table}
          </details>
        </section>
        """)
        time.sleep(0.3)  # 幣安 weight 限流保險（每幣種 klines+depth 兩個請求）

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    html_out = build_html(symbol_blocks, generated_at, discord_status_label)
    OUTPUT_FILE.write_text(html_out, encoding="utf-8")
    log(f"INFO 已寫出 {OUTPUT_FILE} ({OUTPUT_FILE.stat().st_size} bytes)")

    for line in summary_lines:
        log(f"INFO {line}")

    if webhook_url and all_alert_msgs:
        ok = 0
        for msg in all_alert_msgs:
            if send_discord_alert(webhook_url, msg):
                ok += 1
        log(f"INFO discord 警報送出 {ok}/{len(all_alert_msgs)} 則")
    elif webhook_url:
        log("INFO 本次無警報事件，未發送 discord 訊息")
    else:
        log(f"INFO skip 警報發送（DISCORD_WEBHOOK_URL 未設定），本次共有 {len(all_alert_msgs)} 則潛在警報未送出")

    return 0


if __name__ == "__main__":
    sys.exit(main())
