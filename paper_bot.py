"""
Paper trading bot — MA crossover σε BTC/EUR και ETH/EUR.
Ψεύτικα λεφτά, πραγματικές τιμές (δημόσιο API Kraken, χωρίς κλειδιά).
Δεν συνδέεται σε κανέναν λογαριασμό — δεν μπορεί να κάνει πραγματικό trade.

Χρήση:
  pip install requests
  python paper_bot.py --backtest      # τεστ στα τελευταία ~7 μέρες
  python paper_bot.py --once          # ένας έλεγχος και έξοδος (για GitHub Actions)
  python paper_bot.py                 # συνεχές live paper trading (Ctrl+C για στοπ)
  python paper_bot.py --status        # τρέχουσα εικόνα χαρτοφυλακίου
"""
import argparse, csv, json, os, time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests

# ---------------- Ρυθμίσεις ----------------
SYMBOLS      = {"BTCEUR": "XBTEUR", "ETHEUR": "ETHEUR"}   # όνομα -> pair στο Kraken
START_CASH   = 1000.0          # € συνολικά, μοιράζεται ίσα ανά νόμισμα
INTERVAL_MIN = 15              # διάρκεια κεριού σε λεπτά
FAST, SLOW   = 9, 21           # μέσοι όροι (σε κεριά)
FEE          = 0.0025          # 0,25% προμήθεια ανά συναλλαγή (ρεαλιστικό για Kraken/Revolut X)
SLIPPAGE     = 0.0005          # 0,05% χειρότερη τιμή εκτέλεσης
POLL_SEC     = 60              # κάθε πόσο ελέγχει σε συνεχή λειτουργία
STATE_FILE   = "paper_state.json"
TRADES_FILE  = "paper_trades.csv"
EQUITY_FILE  = "paper_equity.csv"
RESULTS_FILE = "RESULTS.md"
API          = "https://api.kraken.com/0/public/OHLC"
TZ           = ZoneInfo("Europe/Athens")
# --------------------------------------------


def fetch_closes(symbol):
    """Επιστρέφει [(close_time_ms, close_price)] μόνο για ΚΛΕΙΣΜΕΝΑ κεριά (έως 720)."""
    r = requests.get(API, params={"pair": SYMBOLS[symbol], "interval": INTERVAL_MIN}, timeout=15)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(j["error"])
    rows = next(v for k, v in j["result"].items() if k != "last")
    step = INTERVAL_MIN * 60
    now = time.time()
    return [((int(k[0]) + step) * 1000, float(k[4])) for k in rows if int(k[0]) + step <= now]


def sma(values, n):
    return sum(values[-n:]) / n


def signal(prices):
    """'buy' αν ο γρήγορος ΜΟ μόλις πέρασε πάνω από τον αργό, 'sell' στο αντίθετο, αλλιώς None."""
    if len(prices) < SLOW + 1:
        return None
    prev_fast, prev_slow = sma(prices[:-1], FAST), sma(prices[:-1], SLOW)
    fast, slow = sma(prices, FAST), sma(prices, SLOW)
    if prev_fast <= prev_slow and fast > slow:
        return "buy"
    if prev_fast >= prev_slow and fast < slow:
        return "sell"
    return None


def new_state():
    per = START_CASH / len(SYMBOLS)
    return {"started": None, "accounts": {s: {"cash": per, "qty": 0.0, "last_candle": 0} for s in SYMBOLS}}


def execute(acc, symbol, side, price, ts, log):
    if side == "buy" and acc["qty"] == 0 and acc["cash"] > 0:
        fill = price * (1 + SLIPPAGE)
        qty = acc["cash"] * (1 - FEE) / fill
        log(symbol, "BUY", fill, qty, acc["cash"], ts)
        acc["qty"], acc["cash"] = qty, 0.0
    elif side == "sell" and acc["qty"] > 0:
        fill = price * (1 - SLIPPAGE)
        cash = acc["qty"] * fill * (1 - FEE)
        log(symbol, "SELL", fill, acc["qty"], cash, ts)
        acc["cash"], acc["qty"] = cash, 0.0


def equity(accounts, last_prices):
    return sum(a["cash"] + a["qty"] * last_prices.get(s, 0) for s, a in accounts.items())


def fmt_ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=TZ).strftime("%d/%m %H:%M")


# ---------------- Backtest ----------------
def backtest(data=None):
    """data: {symbol: [(ts, close), ...]} — αν λείπει, κατεβάζει τα τελευταία ~720 κεριά."""
    data = data or {s: fetch_closes(s) for s in SYMBOLS}
    accounts, trades = new_state()["accounts"], []
    log = lambda sym, side, px, qty, eur, ts: trades.append((fmt_ts(ts), sym, side, px, qty, eur))
    for s, candles in data.items():
        closes = [c for _, c in candles]
        for i in range(SLOW + 1, len(closes) + 1):
            sig = signal(closes[:i])
            if sig:
                execute(accounts[s], s, sig, closes[i - 1], candles[i - 1][0], log)
    last = {s: c[-1][1] for s, c in data.items()}
    final = equity(accounts, last)
    hold = sum((START_CASH / len(data)) * c[-1][1] / c[SLOW][1] for c in data.values())

    print(f"\nBacktest {INTERVAL_MIN}m, MA {FAST}/{SLOW}, {len(next(iter(data.values())))} κεριά\n")
    for t in trades:
        print(f"  {t[0]}  {t[1]:7} {t[2]:4}  τιμή {t[3]:>10.2f}  ποσότητα {t[4]:.6f}  ({t[5]:.2f}€)")
    print(f"\n  Συναλλαγές: {len(trades)}")
    print(f"  Αρχικό: {START_CASH:.2f}€  →  Τελικό: {final:.2f}€  ({(final / START_CASH - 1) * 100:+.2f}%)")
    print(f"  Buy & hold ίδιας περιόδου: {hold:.2f}€  ({(hold / START_CASH - 1) * 100:+.2f}%)\n")
    return final


# ---------------- Live paper ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            st = json.load(f)
        if "accounts" not in st:                       # παλιά μορφή αρχείου
            st = {"started": None, "accounts": st}
        return st
    return new_state()


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def csv_append(path, header, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def csv_log(sym, side, px, qty, eur, ts):
    csv_append(TRADES_FILE, ["χρόνος", "νόμισμα", "κίνηση", "τιμή", "ποσότητα", "ευρώ"],
               [fmt_ts(ts), sym, side, f"{px:.2f}", f"{qty:.8f}", f"{eur:.2f}"])
    print(f"  >>> {fmt_ts(ts)} {sym} {side} @ {px:.2f} ({eur:.2f}€)")


def step(state):
    """Επεξεργάζεται ΟΛΑ τα νέα κλεισμένα κεριά από τον τελευταίο έλεγχο. Επιστρέφει τελευταίες τιμές."""
    last = {}
    for s in SYMBOLS:
        acc = state["accounts"][s]
        try:
            candles = fetch_closes(s)
        except Exception as e:
            print(f"  ! {s}: {e}")
            continue
        last[s] = candles[-1][1]
        if acc["last_candle"] == 0:                    # πρώτη εκτέλεση: απλώς σημειώνει πού είμαστε
            acc["last_candle"] = candles[-1][0]
            continue
        closes = [c for _, c in candles]
        for i, (ts, px) in enumerate(candles):
            if ts <= acc["last_candle"]:
                continue
            sig = signal(closes[: i + 1])
            if sig:
                execute(acc, s, sig, px, ts, csv_log)
            acc["last_candle"] = ts
    if state["started"] is None and last:
        state["started"] = {"time": datetime.now(TZ).isoformat(timespec="minutes"), "prices": last}
    return last


def write_results(state, last):
    eq = equity(state["accounts"], last)
    st = state["started"] or {}
    p0 = st.get("prices", {})
    hold = sum((START_CASH / len(SYMBOLS)) * last[s] / p0[s] for s in SYMBOLS if s in p0 and s in last) \
        if all(s in p0 and s in last for s in SYMBOLS) else None
    n_trades = 0
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, encoding="utf-8") as f:
            n_trades = max(sum(1 for _ in f) - 1, 0)
    lines = [
        "# Paper trading — αποτελέσματα",
        "",
        f"Ενημέρωση: {datetime.now(TZ):%d/%m/%Y %H:%M} · Έναρξη: {st.get('time', '-')}",
        f"Στρατηγική: MA {FAST}/{SLOW} σε κεριά {INTERVAL_MIN}′ · προμήθεια {FEE*100:.2f}% · ψεύτικο κεφάλαιο {START_CASH:.0f}€",
        "",
        "| | Αξία | Απόδοση |",
        "|---|---:|---:|",
        f"| **Bot** | {eq:.2f}€ | {(eq / START_CASH - 1) * 100:+.2f}% |",
    ]
    if hold is not None:
        lines.append(f"| Buy & hold | {hold:.2f}€ | {(hold / START_CASH - 1) * 100:+.2f}% |")
    lines += ["", f"Συναλλαγές: {n_trades} — αναλυτικά στο `{TRADES_FILE}`", "", "| Νόμισμα | Θέση | Μετρητά | Αξία σε coin |", "|---|---|---:|---:|"]
    for s, a in state["accounts"].items():
        lines.append(f"| {s} | {'ΜΕΣΑ' if a['qty'] else 'έξω'} | {a['cash']:.2f}€ | {a['qty'] * last.get(s, 0):.2f}€ |")
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    csv_append(EQUITY_FILE, ["χρόνος", "bot_€", "buy_hold_€"],
               [datetime.now(TZ).strftime("%Y-%m-%d %H:%M"), f"{eq:.2f}", f"{hold:.2f}" if hold else ""])
    return eq


def once():
    state = load_state()
    last = step(state)
    save_state(state)
    if len(last) == len(SYMBOLS):
        eq = write_results(state, last)
        print(f"Αξία: {eq:.2f}€ ({(eq / START_CASH - 1) * 100:+.2f}%)")
    else:
        raise SystemExit("Αποτυχία λήψης τιμών — δες τα μηνύματα παραπάνω")


def live():
    print(f"Paper trading ξεκίνησε — {', '.join(SYMBOLS)}, MA {FAST}/{SLOW} σε {INTERVAL_MIN}m. Ctrl+C για στοπ.")
    while True:
        state = load_state()
        last = step(state)
        save_state(state)
        if last:
            eq = equity(state["accounts"], last)
            pos = " | ".join(f"{s}: {'ΜΕΣΑ' if state['accounts'][s]['qty'] else 'έξω'}" for s in SYMBOLS)
            print(f"{datetime.now(TZ):%H:%M:%S}  Αξία: {eq:.2f}€ ({(eq / START_CASH - 1) * 100:+.2f}%)  {pos}")
        time.sleep(POLL_SEC)


def status():
    state = load_state()
    last = {s: fetch_closes(s)[-1][1] for s in SYMBOLS}
    for s, a in state["accounts"].items():
        print(f"{s}: μετρητά {a['cash']:.2f}€, ποσότητα {a['qty']:.6f} (≈{a['qty'] * last[s]:.2f}€)")
    eq = equity(state["accounts"], last)
    print(f"Σύνολο: {eq:.2f}€ ({(eq / START_CASH - 1) * 100:+.2f}%)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group()
    g.add_argument("--backtest", action="store_true")
    g.add_argument("--once", action="store_true")
    g.add_argument("--status", action="store_true")
    a = p.parse_args()
    try:
        if a.backtest: backtest()
        elif a.once: once()
        elif a.status: status()
        else: live()
    except KeyboardInterrupt:
        print("\nΣταμάτησε. Η κατάσταση είναι στο", STATE_FILE)
