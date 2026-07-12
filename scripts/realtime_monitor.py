# -*- coding: utf-8 -*-
"""即時大額成交監控（跑在 GCP e2-micro 常駐主機，不是 GitHub Actions）。

== 這是什麼、不是什麼（誠實條款，開工前先講清楚）==
這不是把 whale_scan.py 的完整五面向評分搬來即時跑——OI／DOM／品質分那些都需要
REST 輪詢，幾百檔幣同時做會撞 API 額度，不適合秒級頻率。這支程式只做一件事：
**持續監聽 Binance／Gate.io 的逐筆成交 WebSocket，抓「短時間內異常大額的淨買賣力道」，
達門檻立刻推 Discord**。跟每 5 分鐘一次的完整評分（whale_scan.py，GitHub Actions）
是互補關係：
- 完整評分＝準、慢（最多 5-10 分鐘延遲），決定候選池進出、算品質分/FLOW/大盤總覽
- 這支程式＝快、窄（秒級延遲），只抓「現在正在發生的大額成交异动」，不算等級

Discord 通知會清楚標示「⚡即時警報」跟 whale_scan.py 的「主力評分異動」分開，
不要混為一談。

== 運作方式 ==
- 每 30 分鐘用 REST 重新抓一次流動性夠的幣種清單（跟 whale_scan.py 同樣門檻），
  同時記下每檔幣的 24h 成交額
- 對這份清單開 WebSocket：Binance 組合流（aggTrade）、Gate.io spot.trades
- 記憶體內維護每檔幣近 60 秒的成交，每 15 秒檢查一次淨流向是否破門檻
- 破門檻且不在冷卻時間內（同一幣 10 分鐘只警一次，避免同一波行情洗版）→ 推 Discord
- WebSocket 斷線自動重連（指數退避），單一交易所掛掉不影響另一家

== 門檻改比例制（2026-07-13，跟 whale_scan.py 的鯨魚分層是同一個教訓）==
之前門檻是齊頭式固定 $80,000——對 BTC（日均量數十億）這金額毫無意義，
對剛好卡在 $30 萬流動性下限的小幣卻可能大到永遠觸發不了，兩種都不對。
改成「60 秒淨力道 ÷ 24h 成交額」的比例，並加一個絕對金額下限（避免極小額
的幣光靠低流動性就湊出高比例）：兩個條件都要達到才觸發。
"""
import asyncio
import collections
import datetime as dt
import json
import logging
import os
import time

import requests
import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("realtime")

H = {"User-Agent": "Mozilla/5.0"}
SPOT_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]
GATE_HOST = "https://api.gateio.ws/api/v4"
STABLE = {"USDC", "FDUSD", "TUSD", "DAI", "EUR", "USDP", "BUSD", "USD1", "EURI",
          "XUSD", "PAX", "GUSD", "USDD", "EURT", "RLUSD", "USDE", "USDY",
          "BFUSD", "USDS", "U"}
LEV_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")

MIN_QUOTE_VOL = float(os.environ.get("MIN_QUOTE_VOL", 300_000))
WINDOW_SEC = 60          # 檢查近 N 秒的成交
CHECK_INTERVAL_SEC = 15  # 每隔多久檢查一次門檻
ALERT_RATIO = float(os.environ.get("ALERT_RATIO", 0.004))     # 60秒淨力道佔24h量比例門檻(0.4%)
ALERT_FLOOR_USD = float(os.environ.get("ALERT_FLOOR_USD", 5_000))  # 絕對金額下限
COOLDOWN_SEC = 600       # 同一幣 10 分鐘只警一次
UNIVERSE_REFRESH_SEC = 1800
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

# symbol -> deque[(ts, signed_notional)]；正=主動買、負=主動賣
trades: dict[str, collections.deque] = collections.defaultdict(collections.deque)
last_alert: dict[str, float] = {}
universe_lock = asyncio.Lock()
binance_symbols: dict[str, float] = {}  # symbol -> 24h 成交額(USD)，比例門檻要用
gate_symbols: dict[str, float] = {}


def sget(path, params=None):
    last = None
    for host in SPOT_HOSTS:
        try:
            r = requests.get(host + path, params=params, headers=H, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"spot {path} 失敗: {last}")


def fetch_binance_universe():
    """回傳 {symbol: 24h成交額}，成交額要留著給比例門檻用，不是只拿來篩選門檻。"""
    info = sget("/api/v3/exchangeInfo")
    eligible = set()
    for s in info["symbols"]:
        if (s["status"] == "TRADING" and s["quoteAsset"] == "USDT"
                and s.get("isSpotTradingAllowed", True)
                and s["baseAsset"] not in STABLE
                and not s["baseAsset"].endswith(LEV_SUFFIX)):
            eligible.add(s["symbol"])
    tick = sget("/api/v3/ticker/24hr")
    out = {}
    for t in tick:
        if t["symbol"] in eligible:
            qv = float(t["quoteVolume"])
            if qv >= MIN_QUOTE_VOL:
                out[t["symbol"]] = qv
    return out


def fetch_gate_universe(exclude_bases):
    r = requests.get(f"{GATE_HOST}/spot/tickers", headers=H, timeout=20)
    r.raise_for_status()
    out = {}
    for t in r.json():
        cp = t["currency_pair"]
        if not cp.endswith("_USDT"):
            continue
        base = cp[:-5]
        if base in STABLE or base in exclude_bases:
            continue
        try:
            qv = float(t["quote_volume"] or 0)
        except (TypeError, ValueError):
            continue
        if qv >= MIN_QUOTE_VOL:
            out[cp] = qv
    return out


def send_discord_alert(symbol, exch, net, window_sec, qv, ratio):
    if not DISCORD_WEBHOOK:
        log.info(f"[DRY] would alert {symbol} net={net:+.0f} ratio={ratio:.2%} (no webhook set)")
        return
    direction = "買方主動" if net > 0 else "賣方主動"
    title = f"⚡ 即時警報．{symbol}．{window_sec}秒內{direction}湧入"
    desc = (f"近 {window_sec} 秒大額成交淨力道 `${net:+,.0f}` · 佔 24h 量 **{ratio:+.2%}**\n"
            f"24h 成交額 `${qv:,.0f}` · 交易所：{exch}\n"
            "這是逐筆成交量的快速訊號，**不是完整主力評分**（沒有OI/深度/結構判斷）。\n"
            "完整評分與候選池請看每日市場觀察→主力資金雷達頁面。\n"
            "程式規則生成，非投資建議。")
    try:
        requests.post(DISCORD_WEBHOOK, json={"embeds": [{
            "title": title, "description": desc,
            "color": 0x3E5C8A if net > 0 else 0xC7364C}]}, timeout=15)
    except Exception as e:  # noqa: BLE001
        log.warning(f"Discord 推播失敗: {e}")


def record_trade(symbol, notional_signed):
    trades[symbol].append((time.time(), notional_signed))


def prune_and_sum(symbol):
    dq = trades[symbol]
    cutoff = time.time() - WINDOW_SEC
    while dq and dq[0][0] < cutoff:
        dq.popleft()
    return sum(v for _, v in dq)


async def checker_loop():
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SEC)
        now = time.time()
        for symbol in list(trades.keys()):
            net = prune_and_sum(symbol)
            if abs(net) < ALERT_FLOOR_USD:
                continue
            qv = binance_symbols.get(symbol) or gate_symbols.get(symbol)
            if not qv:
                continue  # 清單剛好還沒刷新到這檔幣的成交額，這輪先跳過
            ratio = net / qv
            if abs(ratio) < ALERT_RATIO:
                continue
            if now - last_alert.get(symbol, 0) < COOLDOWN_SEC:
                continue
            exch = "binance" if symbol in binance_symbols else "gate"
            log.info(f"ALERT {symbol} net={net:+.0f} ratio={ratio:+.2%} exch={exch}")
            send_discord_alert(symbol, exch, net, WINDOW_SEC, qv, ratio)
            last_alert[symbol] = now


async def universe_refresh_loop():
    global binance_symbols, gate_symbols
    while True:
        try:
            b = fetch_binance_universe()
            b_bases = {s[:-4] for s in b}
            g = fetch_gate_universe(b_bases)
            async with universe_lock:
                binance_symbols, gate_symbols = b, g
            log.info(f"universe refreshed: binance={len(b)} gate={len(g)}")
        except Exception as e:  # noqa: BLE001
            log.warning(f"universe refresh 失敗，沿用舊清單: {e}")
        await asyncio.sleep(UNIVERSE_REFRESH_SEC)


async def binance_ws_loop():
    backoff = 5
    while True:
        async with universe_lock:
            syms = sorted(binance_symbols)
        if not syms:
            await asyncio.sleep(5)
            continue
        streams = "/".join(f"{s.lower()}@aggTrade" for s in syms)
        # 注意：wss://stream.binance.com 在美國 IP（含 GCP us-* Always Free 區域）會被
        # Binance 以 HTTP 451 法規封鎖；data-stream.binance.vision 是官方市場資料鏡像，
        # 不受此限制，已實測組合流格式相容（見 lessons.md 2026-07-12）。
        url = f"wss://data-stream.binance.vision/stream?streams={streams}"
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                          max_size=2**20) as ws:
                log.info(f"Binance WS connected ({len(syms)} symbols)")
                backoff = 5
                async for raw in ws:
                    msg = json.loads(raw)
                    d = msg.get("data", {})
                    if d.get("e") != "aggTrade":
                        continue
                    sym, price, qty = d["s"], float(d["p"]), float(d["q"])
                    notional = price * qty
                    signed = notional if not d["m"] else -notional  # m=True→賣方主動
                    record_trade(sym, signed)
        except Exception as e:  # noqa: BLE001
            log.warning(f"Binance WS 斷線，{backoff}s 後重連: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)


async def gate_ws_loop():
    backoff = 5
    while True:
        async with universe_lock:
            syms = sorted(gate_symbols)
        if not syms:
            await asyncio.sleep(5)
            continue
        try:
            async with websockets.connect("wss://api.gateio.ws/ws/v4/", ping_interval=20,
                                          ping_timeout=20, max_size=2**20) as ws:
                # Gate 官方建議每次訂閱不要塞太多 pair，分批送出
                batch = 50
                for i in range(0, len(syms), batch):
                    payload = {"time": int(time.time()), "channel": "spot.trades",
                              "event": "subscribe", "payload": syms[i:i + batch]}
                    await ws.send(json.dumps(payload))
                    await asyncio.sleep(0.2)
                log.info(f"Gate.io WS connected ({len(syms)} symbols)")
                backoff = 5
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("event") != "update" or msg.get("channel") != "spot.trades":
                        continue
                    r = msg.get("result")
                    if not isinstance(r, dict):
                        continue
                    cp, price, amount, side = r.get("currency_pair"), r.get("price"), r.get("amount"), r.get("side")
                    if not (cp and price and amount and side):
                        continue
                    notional = float(price) * float(amount)
                    signed = notional if side == "buy" else -notional
                    record_trade(cp, signed)
        except Exception as e:  # noqa: BLE001
            log.warning(f"Gate.io WS 斷線，{backoff}s 後重連: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)


async def main():
    log.info(f"啟動：門檻=佔24h量{ALERT_RATIO:.2%}（且≥${ALERT_FLOOR_USD:,.0f}）/{WINDOW_SEC}s，"
             f"冷卻={COOLDOWN_SEC}s，webhook={'已設定' if DISCORD_WEBHOOK else '未設定(dry-run)'}")
    # 先同步抓一次清單，避免 WS 迴圈啟動時清單是空的
    global binance_symbols, gate_symbols
    binance_symbols = fetch_binance_universe()
    gate_symbols = fetch_gate_universe({s[:-4] for s in binance_symbols})
    log.info(f"初始清單：binance={len(binance_symbols)} gate={len(gate_symbols)}")

    await asyncio.gather(
        universe_refresh_loop(),
        binance_ws_loop(),
        gate_ws_loop(),
        checker_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
