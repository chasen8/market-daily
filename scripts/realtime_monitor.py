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
  同時記下每檔幣的 24h 成交額，再用 whale_scan.py 的候選池狀態篩到只剩
  S/A/D 級成員（見下方「收斂到候選池成員」）
- 對這份清單開 WebSocket：Binance 組合流（aggTrade）、Gate.io spot.trades
- 記憶體內維護每檔幣近 60 秒的成交，每 15 秒檢查一次淨流向是否破門檻
- 破門檻且不在冷卻時間內（同一幣 10 分鐘只警一次，避免同一波行情洗版）→ 推 Discord
- WebSocket 斷線自動重連（指數退避），單一交易所掛掉不影響另一家

== 門檻改比例制（2026-07-13，跟 whale_scan.py 的鯨魚分層是同一個教訓）==
之前門檻是齊頭式固定 $80,000——對 BTC（日均量數十億）這金額毫無意義，
對剛好卡在 $30 萬流動性下限的小幣卻可能大到永遠觸發不了，兩種都不對。
改成「淨力道 ÷ 24h 成交額」的比例，並加一個絕對金額下限（避免極小額
的幣光靠低流動性就湊出高比例）：兩個條件都要達到才觸發。

== 收斂到候選池成員（2026-07-13，比例制上線後發現雜訊還是太多）==
問題不在門檻數字，是監控範圍太大：全市場（約 290 檔）逐筆掃描本來就會撞到
大量統計雜訊。改成只對 whale_scan.py 已經算出候選池（S/A/D 級）的成員開
WebSocket——範圍縮到候選池大小，每則警報帶出等級/方向。候選池清單讀本機
data/whale_state.json（VM 每 30 分鐘 git pull 一次）。

== 60 秒單窗口警報退役，升級多窗口 CVD＋量價背離（2026-07-14）==
兩輪研究（學術：docs/orderbook-quant-research.md；實務：docs/dom-practical-research.md）
都指向同一結論：分鐘級以下的資金流訊號預測的是接下來幾分鐘，跟本專案的波段
（數天～數週）持倉週期錯配——60 秒內誰在主動買賣，對波段決策沒有資訊量，
whale_scan.py 每 5 分鐘的 CVD 子分數已涵蓋同樣資訊。改成只推兩種對波段
有意義的事件（仍限候選池成員）：
1. 持續同向累積：5分/15分/1小時三個窗口淨流全部同向，且 15 分淨流佔 24h 量
   達門檻——持續性資金行為，不是單筆大單雜訊。
2. 量價背離：15 分價格漲逾門檻但淨流明顯偏賣（出貨嫌疑），或價跌但淨流
   明顯偏買（吸籌嫌疑）——實務圈 CVD 背離的程式化版本。
"""
import asyncio
import collections
import datetime as dt
import json
import logging
import os
import re
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
LEV_SUFFIX = ("UP", "DOWN", "BULL", "BEAR")  # Binance 槓桿代幣後綴
GATE_LEV_RE = re.compile(r"\d[LS]$")  # Gate.io 槓桿代幣（XRP3L/BTC5S 這種），實測 364 檔存在
# 這種代幣機制上本來就會有劇烈量能波動，不是真的資金流向訊號——2026-07-13 從
# 即時警報連續洗版（XRP3L/XRP5S/BTC5L...）抓到這個漏篩，whale_scan.py 同步修正。

# 候選池等級定義，跟 whale_scan.py 保持一致（複製而非 import，兩支程式部署環境
# 不同：這支跑在 GCP VM 常駐 daemon，whale_scan.py 跑在 GitHub Actions）
LONG_GRADES = {"S", "A"}
SHORT_GRADES = {"D"}
POOL_GRADES = LONG_GRADES | SHORT_GRADES
STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "whale_state.json")

MIN_QUOTE_VOL = float(os.environ.get("MIN_QUOTE_VOL", 300_000))
CVD_WINDOWS = (300, 900, 3600)  # 多窗口：5分/15分/1小時（秒）
CHECK_INTERVAL_SEC = 60         # 訊號是分鐘級的，每分鐘檢查一次就夠
SUSTAIN_RATIO = float(os.environ.get("SUSTAIN_RATIO", 0.01))        # 持續累積：15分淨流佔24h量門檻(1%)
DIVERGE_PRICE_PCT = float(os.environ.get("DIVERGE_PRICE_PCT", 0.01))  # 背離：15分價格變動門檻(1%)
DIVERGE_RATIO = float(os.environ.get("DIVERGE_RATIO", 0.005))       # 背離：15分淨流佔24h量門檻(0.5%)
ALERT_FLOOR_USD = float(os.environ.get("ALERT_FLOOR_USD", 10_000))  # 15分淨流絕對金額下限
COOLDOWN_SEC = 1800      # 同一幣同一類警報 30 分鐘只推一次（訊號本身是慢訊號）
MIN_TRADES_15M = 10      # 15分內成交筆數太少代表資料不足，不給訊號
UNIVERSE_REFRESH_SEC = 1800
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

# symbol -> deque[(ts, signed_notional, price)]；正=主動買、負=主動賣
trades: dict[str, collections.deque] = collections.defaultdict(collections.deque)
last_alert: dict[tuple, float] = {}  # (symbol, 警報類型) -> ts
universe_lock = asyncio.Lock()
binance_symbols: dict[str, float] = {}  # symbol -> 24h 成交額(USD)，比例門檻要用
gate_symbols: dict[str, float] = {}
pool_grades: dict[str, str] = {}  # symbol -> grade，只含候選池成員，警報文案用


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
        if base in STABLE or base in exclude_bases or GATE_LEV_RE.search(base):
            continue
        try:
            qv = float(t["quote_volume"] or 0)
        except (TypeError, ValueError):
            continue
        if qv >= MIN_QUOTE_VOL:
            out[cp] = qv
    return out


def fetch_pool_symbols():
    """讀 whale_scan.py 的候選池狀態，回傳 {symbol: grade}，只留 S/A/D 級。
    讀取失敗（檔案不存在/格式壞掉）回傳 None，讓呼叫端沿用舊清單，
    避免候選池暫時讀不到就把整個即時警報清空。"""
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning(f"讀取候選池狀態失敗，沿用舊清單: {e}")
        return None
    return {sym: entry.get("grade") for sym, entry in state.items()
            if entry.get("grade") in POOL_GRADES}


def _post_embed(title, desc, color):
    if not DISCORD_WEBHOOK:
        log.info(f"[DRY] {title}")
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"embeds": [{
            "title": title, "description": desc, "color": color}]}, timeout=15)
    except Exception as e:  # noqa: BLE001
        log.warning(f"Discord 推播失敗: {e}")


def _grade_line(grade):
    if not grade:
        return ""
    pool_dir = "做多" if grade in LONG_GRADES else "做空"
    return f"候選池 **{grade}**（{pool_dir}方向） · "


def send_sustain_alert(symbol, exch, grade, qv, net5, net15, net60, r15):
    kind = "吸籌" if net15 > 0 else "出貨"
    emoji = "📈" if net15 > 0 else "📉"
    title = f"{emoji} 持續{kind}．{symbol}．三窗口同向"
    desc = (f"{_grade_line(grade)}5分/15分/1時淨流 `${net5:+,.0f}` / `${net15:+,.0f}` / `${net60:+,.0f}`\n"
            f"15分淨流佔 24h 量 **{r15:+.2%}** · 24h 額 `${qv:,.0f}` · {exch}\n"
            "三個時間窗淨流同向＝持續性資金行為，不是單筆大單雜訊。\n"
            "程式規則生成，非投資建議。")
    _post_embed(title, desc, 0x2EA673 if net15 > 0 else 0xA83E54)


def send_diverge_alert(symbol, exch, grade, qv, net15, r15, chg15):
    kind = "價漲量賣（出貨嫌疑）" if chg15 > 0 else "價跌量買（吸籌嫌疑）"
    title = f"⚠️ 量價背離．{symbol}．{kind}"
    desc = (f"{_grade_line(grade)}15 分價格 **{chg15:+.2%}**，但淨流 `${net15:+,.0f}`"
            f"（佔 24h 量 **{r15:+.2%}**）方向相反\n"
            f"24h 額 `${qv:,.0f}` · {exch}\n"
            "價格與真實成交流反向＝檯面上的走勢缺乏對應資金支撐，留意反轉。\n"
            "程式規則生成，非投資建議。")
    _post_embed(title, desc, 0xD4AF37)


def record_trade(symbol, notional_signed, price):
    trades[symbol].append((time.time(), notional_signed, price))


def prune(symbol):
    dq = trades[symbol]
    cutoff = time.time() - CVD_WINDOWS[-1]
    while dq and dq[0][0] < cutoff:
        dq.popleft()


def window_stats(symbol, window, now=None):
    """回傳 (淨流, 筆數, 窗口首筆價, 窗口末筆價)。"""
    cutoff = (now or time.time()) - window
    net, n, first_price, last_price = 0.0, 0, None, None
    for ts, signed, price in trades[symbol]:
        if ts < cutoff:
            continue
        net += signed
        n += 1
        if first_price is None:
            first_price = price
        last_price = price
    return net, n, first_price, last_price


def check_symbol(symbol, now):
    """單一幣種的訊號判定（獨立函式方便測試）。回傳觸發的警報類型 list。"""
    fired = []
    qv = binance_symbols.get(symbol) or gate_symbols.get(symbol)
    if not qv:
        return fired
    net5, _, _, _ = window_stats(symbol, CVD_WINDOWS[0], now)
    net15, n15, p0, p1 = window_stats(symbol, CVD_WINDOWS[1], now)
    net60, _, _, _ = window_stats(symbol, CVD_WINDOWS[2], now)
    if n15 < MIN_TRADES_15M or abs(net15) < ALERT_FLOOR_USD:
        return fired
    r15 = net15 / qv
    exch = "binance" if symbol in binance_symbols else "gate"
    grade = pool_grades.get(symbol)

    # 1) 持續同向累積：三窗口淨流全同向 + 15分佔比達門檻
    if net5 * net15 > 0 and net15 * net60 > 0 and abs(r15) >= SUSTAIN_RATIO \
            and now - last_alert.get((symbol, "sustain"), 0) >= COOLDOWN_SEC:
        log.info(f"SUSTAIN {symbol} net15={net15:+.0f} r15={r15:+.2%} grade={grade}")
        send_sustain_alert(symbol, exch, grade, qv, net5, net15, net60, r15)
        last_alert[(symbol, "sustain")] = now
        fired.append("sustain")

    # 2) 量價背離：15分價格與淨流明顯反向
    if p0 and p1:
        chg15 = p1 / p0 - 1
        diverge = (chg15 >= DIVERGE_PRICE_PCT and r15 <= -DIVERGE_RATIO) or \
                  (chg15 <= -DIVERGE_PRICE_PCT and r15 >= DIVERGE_RATIO)
        if diverge and now - last_alert.get((symbol, "diverge"), 0) >= COOLDOWN_SEC:
            log.info(f"DIVERGE {symbol} chg15={chg15:+.2%} net15={net15:+.0f} r15={r15:+.2%} grade={grade}")
            send_diverge_alert(symbol, exch, grade, qv, net15, r15, chg15)
            last_alert[(symbol, "diverge")] = now
            fired.append("diverge")
    return fired


async def checker_loop():
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SEC)
        now = time.time()
        for symbol in list(trades.keys()):
            prune(symbol)
            check_symbol(symbol, now)


async def universe_refresh_loop():
    global binance_symbols, gate_symbols, pool_grades
    while True:
        try:
            b = fetch_binance_universe()
            b_bases = {s[:-4] for s in b}
            g = fetch_gate_universe(b_bases)
            pool = fetch_pool_symbols()
            if pool is not None:
                b = {s: qv for s, qv in b.items() if s in pool}
                g = {s: qv for s, qv in g.items() if s in pool}
            async with universe_lock:
                binance_symbols, gate_symbols = b, g
                if pool is not None:
                    pool_grades = pool
            note = f"（候選池 {len(pool)} 檔）" if pool is not None else "（候選池讀取失敗，沿用舊清單未篩選）"
            log.info(f"universe refreshed: binance={len(b)} gate={len(g)} {note}")
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
                    record_trade(sym, signed, price)
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
                    record_trade(cp, signed, float(price))
        except Exception as e:  # noqa: BLE001
            log.warning(f"Gate.io WS 斷線，{backoff}s 後重連: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)


async def main():
    log.info(f"啟動：多窗口CVD {'/'.join(str(w) for w in CVD_WINDOWS)}s，"
             f"持續累積門檻=15分佔24h量{SUSTAIN_RATIO:.1%}，"
             f"背離門檻=價{DIVERGE_PRICE_PCT:.1%}×流{DIVERGE_RATIO:.1%}，"
             f"下限=${ALERT_FLOOR_USD:,.0f}，冷卻={COOLDOWN_SEC}s，"
             f"webhook={'已設定' if DISCORD_WEBHOOK else '未設定(dry-run)'}")
    # 先同步抓一次清單，避免 WS 迴圈啟動時清單是空的
    global binance_symbols, gate_symbols, pool_grades
    b = fetch_binance_universe()
    g = fetch_gate_universe({s[:-4] for s in b})
    pool = fetch_pool_symbols()
    if pool is not None:
        b = {s: qv for s, qv in b.items() if s in pool}
        g = {s: qv for s, qv in g.items() if s in pool}
        pool_grades = pool
    binance_symbols, gate_symbols = b, g
    log.info(f"初始清單：binance={len(binance_symbols)} gate={len(gate_symbols)}"
             + (f"（候選池 {len(pool)} 檔）" if pool is not None else "（候選池讀取失敗，暫不篩選）"))

    await asyncio.gather(
        universe_refresh_loop(),
        binance_ws_loop(),
        gate_ws_loop(),
        checker_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
