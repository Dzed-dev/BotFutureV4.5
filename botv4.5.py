# ================== BOT FUTURES V4.5 ==================
# REAL ORDER | BINANCE FUTURES TESTNET
# FULL FEATURE + TP BERTAHAP REAL + REAL TRAILING STOP
# =====================================================

import requests, time, pandas as pd, hmac, hashlib, urllib.parse, math, json, os
from datetime import datetime, date, timezone
from colorama import Fore, Style, init

JOURNAL_FILE = "trade_journal.csv"

if not os.path.exists(JOURNAL_FILE):
    with open(JOURNAL_FILE, "w") as f:
        f.write(
           "date,entry_time,exit_time,side,entry_price,exit_price,position_size,pnl_usd,R_multiple,duration_min\n"
        )

init(autoreset=True)

# ================== API ==================
API_KEY = "Y3PPjbS4EtYwqCuft5JAd02q7cYPtGYMUueIp7WWb336EtRctnVkqO4jYKX7ChVF"
API_SECRET = "MOO3XgFh8e5XmxstlICAI3Yy8HGc1ATu09upTO4KqnjMfG7owvGrDTXCwziLBGM8"
BASE_URL = "https://testnet.binancefuture.com"
SYMBOL = "BTCUSDT"
LEVERAGE = 5

HEADERS = {"X-MBX-APIKEY": API_KEY}

# ================== CONFIG ==================
RISK_PER_TRADE = 0.01
MAX_NOTIONAL_PCT = 0.10

DAILY_LOSS_LIMIT = 0.02
MAX_TRADES_PER_DAY = 7

SMART_PAUSE_AFTER_LOSS = 3
SMART_PAUSE_MINUTES = 60

ATR_PERIOD = 14
ATR_MULT = 1.5

P1_R = 0.5
P2_R = 1.0
FINAL_R = 1.5
PARTIAL_PCT = 0.30

# ===== VOLATILITY REGIME =====
LOW_VOL_THRESHOLD  = 0.0008   # < 0.08% = market mati
HIGH_VOL_THRESHOLD = 0.0030   # > 0.30% = market liar

BASE_RISK = RISK_PER_TRADE

# ===== CHOP FILTER =====
MIN_ATR_PCT = 0.001      # 0.1% dari harga
MIN_EMA_GAP_PCT = 0.0005  # 0.05% dari harga

# ================== STATE ==================
STATE_FILE = "trade_state.json"
state = {
    "initial_size": 0.0,
    "tp1_done": False,
    "tp2_done": False,
    "stop_order_id": None,
    "highest": None,
    "lowest": None,
    "initial_stop_set": False,
    "last_stop_price": None
}

ENTRY_BALANCE = 0.0
ENTRY_TIME = None
LAST_CANDLE_TIME = 0
ORDERS_CLEANED = False
WIN = 0
LOSE = 0
REALIZED_PNL = 0.0
LAST_POSITION_SIZE = 0.0
LOSS_STREAK = 0
TRADES_TODAY = 0
SMART_PAUSE_UNTIL = None
CURRENT_DAY = date.today()
DAY_START_BALANCE = 0.0

# ====== DD & RR ======
PEAK_BALANCE = 0.0
MAX_DRAWDOWN = 0.0

TOTAL_RISK = 0.0
TOTAL_REWARD = 0.0

# ================== UTIL ==================
def server_time():
    return requests.get(BASE_URL + "/fapi/v1/time").json()["serverTime"]

def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state.update(json.load(f))

def sign(params):
    q = urllib.parse.urlencode(params)
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    return q + "&signature=" + sig

def signed_req(method, path, params=None):
    if params is None:
        params = {}
    params["timestamp"] = server_time()
    params["recvWindow"] = 10000
    query = urllib.parse.urlencode(params)
    signature = hmac.new(
        API_SECRET.encode(),
        query.encode(),
        hashlib.sha256
    ).hexdigest()

    url = BASE_URL + path + "?" + query + "&signature=" + signature
    r = requests.request(method, url, headers=HEADERS, timeout=10)
    if r.status_code != 200:
        print("BINANCE NGOMEL:", r.text)  
        r.raise_for_status()

    return r.json()

def round_step(v, step):
    return math.floor(v / step) * step

# ================== BINANCE ==================
def get_symbol_filters():
    info = requests.get(BASE_URL + "/fapi/v1/exchangeInfo").json()
    for s in info["symbols"]:
        if s["symbol"] == SYMBOL:
            f = {i["filterType"]: i for i in s["filters"]}
            return float(f["PRICE_FILTER"]["tickSize"]), float(f["LOT_SIZE"]["stepSize"])
    return 0.1, 0.001

def set_leverage():
    try:
        signed_req("POST", "/fapi/v1/leverage", {
            "symbol": SYMBOL,
            "leverage": LEVERAGE
        })
        print(Fore.GREEN + f"[{ts()}] Leverage set {LEVERAGE}x")
    except:
        print(Fore.YELLOW + f"[{ts()}] Leverage skipped")

def get_account():
    return signed_req("GET", "/fapi/v2/account")

def get_open_orders():
    return signed_req("GET", "/fapi/v1/openOrders", {
        "symbol": SYMBOL
    })

def cancel_all_orders():
    orders = get_open_orders()
    for o in orders:
        signed_req("DELETE", "/fapi/v1/order", {
            "symbol": SYMBOL,
            "orderId": o["orderId"]
        })

def get_position():
    for p in signed_req("GET", "/fapi/v2/positionRisk"):
        if p["symbol"] == SYMBOL:
            return p
    return None

def cancel_order(order_id):
    if order_id:
        signed_req("DELETE", "/fapi/v1/order", {
            "symbol": SYMBOL,
            "orderId": order_id
        })

def place_market(side, qty, reduce=False):
    qty = round_step(qty, STEP)
    if qty <= 0: return
    signed_req("POST", "/fapi/v1/order", {
        "symbol": SYMBOL,
        "side": side,
        "type": "MARKET",
        "quantity": qty,
        "reduceOnly": reduce
    })

def place_limit(side, qty, price, reduce=False):
    qty = round_step(qty, STEP)
    price = round_step(price, TICK)
    if qty <= 0 or price <= 0:
        return None

    try:
        r = signed_req("POST", "/fapi/v1/order", {
            "symbol": SYMBOL,
            "side": side,
            "type": "LIMIT",
            "quantity": qty,
            "price": price,
            "timeInForce": "GTC",
            "reduceOnly": reduce
        })
        return r
    except:
        return None

def place_stop(side, qty, stop_price):
    qty = round_step(qty, STEP)
    stop_price = round_step(stop_price, TICK)
    if qty <= 0 or stop_price <= 0: return None
    r = signed_req("POST", "/fapi/v1/order", {
        "symbol": SYMBOL,
        "side": side,
        "type": "STOP_MARKET",
        "stopPrice": stop_price,
        "quantity": qty,
        "reduceOnly": True
    })
    return r.get("orderId")

def get_realized_pnl(symbol, start_time):
    incomes = signed_req("GET", "/fapi/v1/income", {
        "symbol": symbol,
        "incomeType": "REALIZED_PNL",
        "startTime": start_time
    })

    pnl = 0.0
    for i in incomes:
        pnl += float(i["income"])

    return pnl

# ================== MARKET ==================
def get_ohlc(tf, limit=300):
    r = requests.get(BASE_URL + "/fapi/v1/klines",
        params={"symbol": SYMBOL, "interval": tf, "limit": limit})
    df = pd.DataFrame(r.json(), columns=[
        "ot","o","h","l","c","v","ct","q","n","tb","tq","i"
    ])
    df[["o","h","l","c"]] = df[["o","h","l","c"]].astype(float)
    return df.rename(columns={"o":"open","h":"high","l":"low","c":"close"})

def rsi(s, p=14):
    d = s.diff()
    g = d.where(d > 0, 0).rolling(p).mean()
    l = -d.where(d < 0, 0).rolling(p).mean()
    return 100 - (100 / (1 + g / l))

def atr(df, p=14):
    tr = pd.concat([
        df["high"]-df["low"],
        (df["high"]-df["close"].shift()).abs(),
        (df["low"]-df["close"].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(p).mean()

# ================== INIT ==================
TICK, STEP = get_symbol_filters()
set_leverage()
load_state()
DAY_START_BALANCE = float(get_account()["totalWalletBalance"])

ERROR_COUNT = 0

# ================== MAIN LOOP ==================
while True:
    try:
        ERROR_COUNT = 0
        acc = get_account()
        pos = get_position()

        BAL = float(acc["totalWalletBalance"])
        if date.today() != CURRENT_DAY:
           CURRENT_DAY = date.today()
           TRADES_TODAY = 0
           LOSS_STREAK = 0
           PEAK_BALANCE = BAL
           DAY_START_BALANCE = BAL 
           print(Fore.CYAN + f"[{ts()}] DAILY RESET")

        if PEAK_BALANCE == 0.0:
           PEAK_BALANCE = BAL
        if BAL > PEAK_BALANCE:
           PEAK_BALANCE = BAL

        dd_now = (PEAK_BALANCE - BAL) / PEAK_BALANCE * 100
        MAX_DRAWDOWN = max(MAX_DRAWDOWN, dd_now)

        # ===== HARD DAILY LOSS LIMIT =====
        if DAY_START_BALANCE > 0:
           daily_dd = (DAY_START_BALANCE - BAL) / DAY_START_BALANCE
           if daily_dd >= DAILY_LOSS_LIMIT:
              print(Fore.RED + f"[{ts()}] HARD STOP — DAILY LOSS LIMIT HIT")
              time.sleep(60 * 60)
              continue


        EQ = float(acc["totalMarginBalance"])

        amt = float(pos["positionAmt"])
        entry = float(pos["entryPrice"])
        upnl = float(pos["unRealizedProfit"])

        POSITION = "LONG" if amt > 0 else "SHORT" if amt < 0 else None
        SIZE = abs(amt)

        # ====== DETEKSI CLOSE TRADE ======
        if LAST_POSITION_SIZE > 0 and SIZE == 0 and ENTRY_TIME is not None:
           pnl = get_realized_pnl(SYMBOL, ENTRY_TIME)
           REALIZED_PNL += pnl
           TRADES_TODAY += 1

           risk = ENTRY_BALANCE * RISK_PER_TRADE

           if pnl > 0:
              WIN += 1
              LOSS_STREAK = 0
              TOTAL_REWARD += pnl
           else:
              LOSE += 1
              LOSS_STREAK += 1

           TOTAL_RISK += risk

           print(Fore.GREEN + f"[{ts()}] TRADE CLOSED | REALIZED PNL: {pnl:.2f}")
           exit_time = int(time.time() * 1000)
           exit_price = price
           duration_min = (exit_time - ENTRY_TIME) / 60000

           R_mult = pnl / (ENTRY_BALANCE * RISK_PER_TRADE) if ENTRY_BALANCE > 0 else 0

           trade_date = datetime.now().strftime("%Y-%m-%d")
           entry_time_str = datetime.fromtimestamp(ENTRY_TIME/1000).strftime("%H:%M:%S")
           exit_time_str = datetime.fromtimestamp(exit_time/1000).strftime("%H:%M:%S")

           with open(JOURNAL_FILE, "a") as f:
              f.write(
                 f"{trade_date},"
                 f"{entry_time_str},"
                 f"{exit_time_str},"
                 f"{state['side']},"
                 f"{entry:.2f},"
                 f"{exit_price:.2f},"
                 f"{state['initial_size']:.4f},"
                 f"{pnl:.2f},"
                 f"{R_mult:.2f},"
                 f"{duration_min:.1f}\n"
                )

           ENTRY_TIME = None   # 👈 RESET
           ORDERS_CLEANED = False


        LAST_POSITION_SIZE = SIZE


        df5 = get_ohlc("5m")
        df15 = get_ohlc("15m")

        last_candle_time = df5["ot"].iloc[-1]

        for df in (df5, df15):
            df["ema9"] = df["close"].ewm(span=9).mean()
            df["ema21"] = df["close"].ewm(span=21).mean()

        df5["rsi"] = rsi(df5["close"])
        df5["atr"] = atr(df5)

        price = df5["close"].iloc[-1]
        atr_v = df5["atr"].iloc[-1]
        rsi_v = df5["rsi"].iloc[-1]

        # ===== VOLATILITY REGIME DETECTION =====
        atr_norm = atr_v / price

        VOL_MODE = "NORMAL"
        RISK_PER_TRADE = BASE_RISK

        # ===== EQUITY PROTECTION MODE =====
        if MAX_DRAWDOWN > 10:
           RISK_PER_TRADE = BASE_RISK * 0.5
           print(Fore.YELLOW + f"[{ts()}] EQUITY PROTECTION MODE AKTIF")

        if atr_norm < LOW_VOL_THRESHOLD:
            VOL_MODE = "LOW"

        elif atr_norm > HIGH_VOL_THRESHOLD:
            VOL_MODE = "HIGH"
            RISK_PER_TRADE = BASE_RISK * 0.5   # risk dipotong saat market liar

        # ===== CHOP FILTER =====
        if atr_v is None or atr_v <= 0:
           continue
        atr_pct = atr_v / price
        ema_gap = abs(df5["ema9"].iloc[-1] - df5["ema21"].iloc[-1]) / price

        if atr_pct < MIN_ATR_PCT:
           print(Fore.YELLOW + f"[{ts()}] CHOP: ATR terlalu kecil ({atr_pct:.5f})")
           time.sleep(5)
           continue

        if ema_gap < MIN_EMA_GAP_PCT:
           print(Fore.YELLOW + f"[{ts()}] CHOP: EMA terlalu rapat ({ema_gap:.5f})")
           time.sleep(5)
           continue

        trend5 = df5["ema9"].iloc[-1] > df5["ema21"].iloc[-1]
        trend15 = df15["ema9"].iloc[-1] > df15["ema21"].iloc[-1]

        STOP_DIST = atr_v * (ATR_MULT if VOL_MODE == "NORMAL" else ATR_MULT * 1.3)

        if LOSS_STREAK >= SMART_PAUSE_AFTER_LOSS:
           print(Fore.YELLOW + f"[{ts()}] STOP: LOSS STREAK {LOSS_STREAK}")
           time.sleep(SMART_PAUSE_MINUTES * 60)
           LOSS_STREAK = 0
           continue

        if TRADES_TODAY >= MAX_TRADES_PER_DAY:
           print(Fore.YELLOW + f"[{ts()}] STOP: MAX TRADES HIT")
           time.sleep(60 * 60)
           continue

        # ===== WAIT CANDLE CLOSE (OPSIONAL 1 - FIX) =====
        if last_candle_time == LAST_CANDLE_TIME:
           continue


        # ===== ENTRY =====
        if POSITION is None and trend5 == trend15:
             if VOL_MODE == "LOW":
                print(Fore.YELLOW + f"[{ts()}] SKIP ENTRY — LOW VOL ({atr_norm:.5f})")
                continue
             if ORDERS_CLEANED == False:
                open_orders = get_open_orders()
                if open_orders:
                   print(Fore.YELLOW + f"[{ts()}] ADA ORDER LAMA → HAPUS SEMUA")
                   cancel_all_orders()
                   time.sleep(1)

                ORDERS_CLEANED = True
             side = None
             if trend5 and 45 < rsi_v < 70:
                side = "BUY"
             elif not trend5 and 30 < rsi_v < 55:
                side = "SELL"

             if side:
                ENTRY_BALANCE = BAL
                LAST_CANDLE_TIME = last_candle_time

                risk_usd = BAL * RISK_PER_TRADE
                qty_risk = risk_usd / STOP_DIST
                qty_cap = (BAL * MAX_NOTIONAL_PCT) / price
                qty = min(qty_risk, qty_cap)

                limit_price = price * (0.999 if side=="BUY" else 1.001)

                order = place_limit(side, qty, limit_price)

                if order is None:
                   print("❌ ORDER GAGAL")
                   continue

                time.sleep(2)  # tunggu fill

                pos_check = get_position()
                real_size = abs(float(pos_check["positionAmt"]))

                if real_size <= 0:
                   print("⚠️ ORDER TIDAK TERISI")
                   continue
                ENTRY_TIME = int(time.time() * 1000)
                print(f"✅ ORDER TERISI: {real_size}")

                state["initial_size"] = real_size
                state["highest"] = price
                state["lowest"] = price
                state["entry_price"] = price
                state["side"] = "LONG" if side == "BUY" else "SHORT"
                save_state()

        # ===== MANAGEMENT =====
        if POSITION:
            if not state["initial_stop_set"] and ENTRY_TIME is not None:
                if POSITION == "LONG":
                   stop_price = entry - STOP_DIST
                   stop_side = "SELL"
                else:
                   stop_price = entry + STOP_DIST
                   stop_side = "BUY"

                state["stop_order_id"] = place_stop(
                stop_side,
                SIZE,
                stop_price
                )

                state["initial_stop_set"] = True
                save_state()

                print("🛑 STOP LOSS AWAL TERPASANG")

            if state["highest"] is None:
               state["highest"] = entry
            if state["lowest"] is None:
               state["lowest"] = entry

            if POSITION == "LONG":
                state["highest"] = max(state["highest"], price)
                R_now = (price-entry)/STOP_DIST
            else:
                state["lowest"] = min(state["lowest"], price)
                R_now = (entry-price)/STOP_DIST

            # TP1
            if not state["tp1_done"] and R_now >= P1_R:
                place_limit(
                    "SELL" if POSITION=="LONG" else "BUY",
                    state["initial_size"] * PARTIAL_PCT,
                    price,
                    True
                )
                state["tp1_done"] = True
                save_state()

            # AUTO BREAKEVEN SETELAH TP1 (HANYA SEKALI)
            if state["tp1_done"] and abs((state["last_stop_price"] or 0) - entry) > TICK:
                breakeven_price = entry
                cancel_order(state["stop_order_id"])
                state["stop_order_id"] = place_stop(
                   "SELL" if POSITION=="LONG" else "BUY",
                    SIZE,
                    breakeven_price
                )
                state["last_stop_price"] = breakeven_price
                save_state()
                print("🟢 STOP PINDAH KE BREAKEVEN")

            # TP2
            if state["tp1_done"] and not state["tp2_done"] and R_now >= P2_R:
                place_limit(
                    "SELL" if POSITION=="LONG" else "BUY",
                    state["initial_size"] * PARTIAL_PCT,
                    price,
                    True
                )
                state["tp2_done"] = True
                save_state()

            # REAL TRAILING STOP
            if state["tp1_done"]:
                new_stop = (
                   state["highest"] - atr_v * ATR_MULT
                   if POSITION == "LONG"
                   else state["lowest"] + atr_v * ATR_MULT
                 )
                # stop hanya diupdate kalau geser cukup jauh
                if (
                   state["last_stop_price"] is None or
                   abs(new_stop - state["last_stop_price"]) > atr_v * 0.2
                ):
                   cancel_order(state["stop_order_id"])
                   state["stop_order_id"] = place_stop(
                       "SELL" if POSITION=="LONG" else "BUY",
                       SIZE,
                       new_stop
                    )
                   state["last_stop_price"] = new_stop
                   save_state()
                   print("🔁 TRAILING STOP DIPERBARUI")

        else:
            state.update({
                "initial_size": 0.0,
                "tp1_done": False,
                "tp2_done": False,
                "stop_order_id": None,
                "highest": None,
                "lowest": None,
                "initial_stop_set": False,
                "last_stop_price": None
            })
            save_state()

        TOTAL_TRADES = WIN + LOSE
        WINRATE = (WIN / TOTAL_TRADES * 100) if TOTAL_TRADES > 0 else 0
        RR = (TOTAL_REWARD / TOTAL_RISK) if TOTAL_RISK > 0 else 0

        print(Fore.CYAN + f"[{ts()}] PRICE:{price:.2f} POSITION:{POSITION or '-'}")
        print(f"BALANCE:{BAL:.2f} EQUITY:{EQ:.2f}")
        print(Style.DIM + "="*120)
        print(
              f"RSI:{rsi_v:.2f} | ATR:{atr_v:.2f} | "
              f"UNREALIZED PNL:{upnl:.2f} | REALIZED:{REALIZED_PNL:.2f}"
        )
        print(
             f"WIN:{WIN} | LOSE:{LOSE} | "
             f"WINRATE:{WINRATE:.2f}% | "
             f"BAL:{BAL:.2f} | EQ:{EQ:.2f}"
        )
        print(
             f"DD:{MAX_DRAWDOWN:.2f}% | "
             f"RR:{RR:.2f}"
        )

        print(Style.DIM + "="*120)

        profit_factor = (
           TOTAL_REWARD / abs(TOTAL_RISK - TOTAL_REWARD)
           if TOTAL_RISK > TOTAL_REWARD and TOTAL_RISK > 0
           else 0
        )
        print(f"PROFIT FACTOR:{profit_factor:.2f}")

        print(f"VOL:{VOL_MODE} | ATR%:{atr_norm:.5f}")
        time.sleep(20)

    except Exception as e:
        ERROR_COUNT += 1
        print(Fore.RED + f"[{ts()}] ERROR: {e}")

        if ERROR_COUNT >= 5:
           print(Fore.RED + "TOO MANY ERRORS — BOT STOPPED")
           break

        time.sleep(30)

