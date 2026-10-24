"""
Paper trading bot v2: πολλές στρατηγικές MA τρέχουν παράλληλα σε BTC/EUR και ETH/EUR.
Ψεύτικα λεφτά, πραγματικές τιμές (δημόσιο API Kraken, χωρίς κλειδιά).
Δεν συνδέεται σε κανέναν λογαριασμό, άρα δεν μπορεί να κάνει πραγματικό trade.

Κάθε στρατηγική έχει δικά της ψεύτικα 1.000€ (500€ ανά νόμισμα) και τρέχει με δύο
επίπεδα προμήθειας ταυτόχρονα: 0,25% (Kraken) και 0,09% (Revolut X taker).
Τα σήματα είναι ίδια· αλλάζει μόνο πόσα «τρώει» το κόστος.

Χρήση:
  pip install requests matplotlib
  python paper_bot.py --backtest      # σύγκριση όλων των στρατηγικών στις τελευταίες 7 μέρες → BACKTEST.md
  python paper_bot.py --once          # ένας έλεγχος και έξοδος (για GitHub Actions)
  python paper_bot.py --status        # τρέχουσα εικόνα
"""
import argparse, csv, json, os, time
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

# ---------------- Ρυθμίσεις ----------------
SYMBOLS    = {"BTCEUR": "XBTEUR", "ETHEUR": "ETHEUR"}   # όνομα -> pair στο Kraken
START_CASH = 1000.0           # € ανά στρατηγική, μοιράζεται ίσα ανά νόμισμα
FEES       = {"hi": 0.0025, "lo": 0.0009}   # 0,25% (Kraken) · 0,09% (Revolut X taker)
SLIPPAGE   = 0.0005           # 0,05% χειρότερη τιμή εκτέλεσης
BACKTEST_DAYS = 7

# Κάθε στρατηγική:
#   tf     = διάρκεια κεριού σε λεπτά (15, 60, 240)
#   fast/slow = μέσοι όροι σε κεριά
#   trend  = αγοράζει μόνο αν η τιμή είναι πάνω από τον MA(trend). 0 = χωρίς φίλτρο
#   margin = ο γρήγορος MA πρέπει να ξεπεράσει τον αργό κατά τόσο (0.003 = 0,3%)
#   hold   = ελάχιστα κεριά παραμονής πριν επιτραπεί πώληση
STRATEGIES = {
    "A": dict(name="Παλιά: 15′ MA 9/21 (μέτρο σύγκρισης)", tf=15,  fast=9,  slow=21,  trend=0,   margin=0.0,   hold=0),
    "B": dict(name="1h MA 20/50 + τάση MA200 + 0,3%",     tf=60,  fast=20, slow=50,  trend=200, margin=0.003, hold=4),
    "C": dict(name="4h MA 9/21 + τάση MA200 + 0,3%",      tf=240, fast=9,  slow=21,  trend=200, margin=0.003, hold=2),
    "D": dict(name="1h MA 50/200 (golden cross)",          tf=60,  fast=50, slow=200, trend=0,   margin=0.0,   hold=0),
}
COLORS = {"A": "#9ca3af", "B": "#16a34a", "C": "#9333ea", "D": "#ea580c", "hold": "#2563eb"}

STATE_FILE, TRADES_FILE, EQUITY_FILE = "paper_state.json", "paper_trades.csv", "paper_equity.csv"
RESULTS_FILE, CHART_FILE, BACKTEST_FILE = "RESULTS.md", "results_chart.png", "BACKTEST.md"
API = "https://api.kraken.com/0/public/OHLC"
TZ  = ZoneInfo("Europe/Athens")
# --------------------------------------------

_cache = {}
def fetch_closes(symbol, tf):
    """[(close_time_ms, close)] μόνο για ΚΛΕΙΣΜΕΝΑ κεριά (έως 720). Cache ανά τρέξιμο."""
    key = (symbol, tf)
    if key not in _cache:
        r = requests.get(API, params={"pair": SYMBOLS[symbol], "interval": tf}, timeout=15)
        r.raise_for_status()
        j = r.json()
        if j.get("error"):
            raise RuntimeError(j["error"])
        rows = next(v for k, v in j["result"].items() if k != "last")
        step, now = tf * 60, time.time()
        _cache[key] = [((int(k[0]) + step) * 1000, float(k[4])) for k in rows if int(k[0]) + step <= now]
    return _cache[key]


def sma(v, n):
    return sum(v[-n:]) / n


def desired(cfg, closes, in_pos):
    """Θέλει να είναι μέσα (True), έξω (False) ή να μείνει όπως είναι (None);"""
    need = max(cfg["slow"], cfg["trend"]) + 1
    if len(closes) < need:
        return None
    f, s, m = sma(closes, cfg["fast"]), sma(closes, cfg["slow"]), cfg["margin"]
    if not in_pos:
        up = f > s * (1 + m)
        if cfg["trend"]:
            up = up and closes[-1] > sma(closes, cfg["trend"])
        if cfg["margin"] == 0 and cfg["trend"] == 0:      # κλασικό crossover: μόνο τη στιγμή της διασταύρωσης
            pf, ps = sma(closes[:-1], cfg["fast"]), sma(closes[:-1], cfg["slow"])
            up = pf <= ps and f > s
        return True if up else None
    down = f < s * (1 - m)
    return False if down else None


# ---------------- Λογαριασμοί ----------------
def new_account():
    per = START_CASH / len(SYMBOLS)
    return {"cash": {k: per for k in FEES}, "qty": {k: 0.0 for k in FEES},
            "last_candle": 0, "entry_ts": 0, "entry_eur": 0.0}


def new_strategy_state():
    return {"accounts": {s: new_account() for s in SYMBOLS},
            "stats": {"trades": 0, "closed": 0, "wins": 0, "fees": {k: 0.0 for k in FEES}}}


def trade(cfg, st, sym, side, price, ts, log=None):
    acc, stats = st["accounts"][sym], st["stats"]
    for k, fee in FEES.items():
        if side == "BUY":
            fill = price * (1 + SLIPPAGE)
            stats["fees"][k] += acc["cash"][k] * fee
            acc["qty"][k], acc["cash"][k] = acc["cash"][k] * (1 - fee) / fill, 0.0
        else:
            fill = price * (1 - SLIPPAGE)
            gross = acc["qty"][k] * fill
            stats["fees"][k] += gross * fee
            acc["cash"][k], acc["qty"][k] = gross * (1 - fee), 0.0
    stats["trades"] += 1
    if side == "BUY":
        acc["entry_ts"], acc["entry_eur"] = ts, (acc["qty"]["hi"] * price * (1 + SLIPPAGE)) / (1 - FEES["hi"])
    else:
        stats["closed"] += 1
        stats["wins"] += acc["cash"]["hi"] > acc["entry_eur"]
    if log:
        log(ts, sym, side, fill, acc)


def process(cfg, st, sym, candles, start_ts, log=None):
    """Τρέχει τη στρατηγική σε όλα τα κεριά μετά το start_ts."""
    acc = st["accounts"][sym]
    closes = [c for _, c in candles]
    held_min = cfg["hold"] * cfg["tf"] * 60 * 1000
    for i, (ts, px) in enumerate(candles):
        if ts <= start_ts:
            continue
        in_pos = acc["qty"]["hi"] > 0
        want = desired(cfg, closes[: i + 1], in_pos)
        if want is True and not in_pos:
            trade(cfg, st, sym, "BUY", px, ts, log)
        elif want is False and in_pos and ts - acc["entry_ts"] >= held_min:
            trade(cfg, st, sym, "SELL", px, ts, log)
        acc["last_candle"] = ts


def value(st, last, k="hi"):
    return sum(a["cash"][k] + a["qty"][k] * last[s] for s, a in st["accounts"].items())


def fmt_ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=TZ).strftime("%d/%m %H:%M")


def pct(v):
    return f"{(v / START_CASH - 1) * 100:+.2f}%"


# ---------------- Backtest ----------------
def backtest(write=True):
    start = int((time.time() - BACKTEST_DAYS * 86400) * 1000)
    rows, last = [], {}
    for sid, cfg in STRATEGIES.items():
        st = new_strategy_state()
        for s in SYMBOLS:
            c = fetch_closes(s, cfg["tf"])
            last[s] = c[-1][1]
            process(cfg, st, s, c, start)
        rows.append((sid, cfg, st))
    hold = 0.0
    for s in SYMBOLS:
        c = fetch_closes(s, 15)
        p0 = next(px for ts, px in c if ts > start)
        hold += (START_CASH / len(SYMBOLS)) * last[s] / p0
    out = [f"# Backtest: τελευταίες {BACKTEST_DAYS} μέρες", "",
           f"Τρέξιμο: {datetime.now(TZ):%d/%m/%Y %H:%M} · κεφάλαιο {START_CASH:.0f}€ ανά στρατηγική", "",
           "| Στρατηγική | Αξία (0,25%) | Αξία (0,09%) | Trades | Κερδοφόρα |", "|---|---:|---:|---:|---:|"]
    for sid, cfg, st in rows:
        v, vl, s = value(st, last), value(st, last, "lo"), st["stats"]
        win = f"{s['wins']}/{s['closed']}" if s["closed"] else "-"
        out.append(f"| **{sid}** {cfg['name']} | {v:.2f}€ ({pct(v)}) | {vl:.2f}€ ({pct(vl)}) | {s['trades']} | {win} |")
    out += [f"| Buy & hold | {hold:.2f}€ ({pct(hold)}) | | 0 | |", "",
            "⚠️ Μία εβδομάδα ιστορικού λέει ελάχιστα. Δείχνει μόνο αν κάτι είναι εντελώς λάθος."]
    text = "\n".join(out) + "\n"
    print(text)
    if write:
        with open(BACKTEST_FILE, "w", encoding="utf-8") as f:
            f.write(text)


# ---------------- Live paper ----------------
def archive_v1(st):
    """Μετακινεί τα αρχεία του παλιού γύρου (v1) στο archive/ και ξεκινά καθαρά."""
    day = (st.get("started") or {}).get("time", "")[:10] or datetime.now(TZ).strftime("%Y-%m-%d")
    folder = os.path.join("archive", f"v1_{day}")
    os.makedirs(folder, exist_ok=True)
    for f in (STATE_FILE, TRADES_FILE, EQUITY_FILE, RESULTS_FILE):
        if os.path.exists(f):
            os.replace(f, os.path.join(folder, f))
    print(f"Ο παλιός γύρος αρχειοθετήθηκε στο {folder}/")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            st = json.load(f)
        if st.get("version") != 2:
            archive_v1(st)
        else:
            for sid in STRATEGIES:                   # νέα στρατηγική που προστέθηκε αργότερα
                st["strategies"].setdefault(sid, new_strategy_state())
            return st
    return {"version": 2, "started": None, "strategies": {sid: new_strategy_state() for sid in STRATEGIES}}


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


def step(state):
    last = {}
    for sid, cfg in STRATEGIES.items():
        st = state["strategies"][sid]
        def log(ts, sym, side, fill, acc, sid=sid):
            eur = acc["cash"]["hi"] if side == "SELL" else acc["qty"]["hi"] * fill / (1 - FEES["hi"])
            csv_append(TRADES_FILE, ["χρόνος", "στρατηγική", "νόμισμα", "κίνηση", "τιμή", "ευρώ"],
                       [fmt_ts(ts), sid, sym, side, f"{fill:.2f}", f"{eur:.2f}"])
            print(f"  >>> {sid} {fmt_ts(ts)} {sym} {side} @ {fill:.2f}")
        for s in SYMBOLS:
            acc = st["accounts"][s]
            try:
                candles = fetch_closes(s, cfg["tf"])
            except Exception as e:
                print(f"  ! {sid} {s}: {e}")
                continue
            if acc["last_candle"] == 0:                  # πρώτη εκτέλεση: απλώς σημειώνει πού είμαστε
                acc["last_candle"] = candles[-1][0]
                continue
            process(cfg, st, s, candles, acc["last_candle"], log)
    for s in SYMBOLS:                                    # πιο φρέσκια τιμή = κερί 15′
        try:
            last[s] = fetch_closes(s, 15)[-1][1]
        except Exception as e:
            print(f"  ! {s}: {e}")
    if state["started"] is None and len(last) == len(SYMBOLS):
        state["started"] = {"time": datetime.now(TZ).isoformat(timespec="minutes"), "prices": dict(last)}
    return last


def read_equity():
    if not os.path.exists(EQUITY_FILE):
        return []
    with open(EQUITY_FILE, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def max_dd(series):
    peak, dd = series[0] if series else 0, 0.0
    for v in series:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return dd * 100


def draw_chart(rows):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt, matplotlib.dates as md
    except ImportError:
        return False
    if len(rows) < 2:
        return False
    t = [datetime.strptime(r["χρόνος"], "%Y-%m-%d %H:%M") for r in rows]
    fig, ax = plt.subplots(figsize=(10, 5.2), dpi=130)
    ax.plot(t, [float(r["buy_hold"]) for r in rows], color=COLORS["hold"], lw=2.4, label="Buy & hold")
    for sid, cfg in STRATEGIES.items():
        if sid in rows[-1] and rows[-1][sid]:
            ys = [float(r[sid]) if r.get(sid) else None for r in rows]
            ax.plot(t, ys, color=COLORS[sid], lw=1.8, ls="--" if sid == "A" else "-", label=f"{sid}: {cfg['name']}")
    ax.axhline(START_CASH, color="#d1d5db", lw=1, ls=":")
    ax.set_title(f"Αξία ανά στρατηγική (προμήθεια 0,25%) · αρχικό {START_CASH:.0f}€", loc="left", fontsize=12)
    ax.set_ylabel("€")
    ax.xaxis.set_major_formatter(md.DateFormatter("%d/%m"))
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="y", alpha=.3)
    ax.legend(frameon=False, fontsize=8.5, loc="best")
    fig.tight_layout()
    fig.savefig(CHART_FILE)
    plt.close(fig)
    return True


def write_results(state, last):
    st0 = state["started"] or {}
    p0 = st0.get("prices", {})
    hold = sum((START_CASH / len(SYMBOLS)) * last[s] / p0[s] for s in SYMBOLS)
    now = datetime.now(TZ)
    vals = {sid: value(state["strategies"][sid], last) for sid in STRATEGIES}
    csv_header = ["χρόνος", "buy_hold"] + list(STRATEGIES)
    rows = read_equity()
    if rows and list(rows[0].keys()) != csv_header:        # προστέθηκε στρατηγική → ξαναγράφει την κεφαλίδα
        with open(EQUITY_FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, csv_header, restval="")
            w.writeheader(); w.writerows(rows)
    csv_append(EQUITY_FILE, csv_header, [now.strftime("%Y-%m-%d %H:%M"), f"{hold:.2f}"] + [f"{vals[s]:.2f}" for s in STRATEGIES])
    rows = read_equity()
    chart = draw_chart(rows)

    best = max(vals, key=vals.get)
    lines = [
        "# 📈 Paper trading: αποτελέσματα", "",
        f"**Ενημέρωση:** {now:%d/%m/%Y %H:%M} · **Έναρξη:** {st0.get('time', '-')[:16].replace('T', ' ')} · "
        f"**Κεφάλαιο:** {START_CASH:.0f}€ ψεύτικα ανά στρατηγική (μισά BTC, μισά ETH)", "",
        f"> Buy & hold: **{hold:.2f}€ ({pct(hold)})** · Καλύτερη στρατηγική τώρα: **{best}** "
        f"με {vals[best]:.2f}€ ({pct(vals[best])}), {'πάνω' if vals[best] > hold else 'κάτω'} από το buy & hold", "",
    ]
    if chart:
        lines += [f"![Γράφημα]({CHART_FILE})", ""]
    lines += ["| | Στρατηγική | Αξία @0,25% | Αξία @0,09% | Trades | Κερδοφόρα | Προμήθειες @0,25% | Max DD | Θέση BTC / ETH |",
              "|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for sid, cfg in STRATEGIES.items():
        st = state["strategies"][sid]
        s = st["stats"]
        vl = value(st, last, "lo")
        series = [float(r[sid]) for r in rows if r.get(sid)]
        win = f"{s['wins']}/{s['closed']} ({s['wins'] / s['closed'] * 100:.0f}%)" if s["closed"] else "-"
        pos = " / ".join("🟢 μέσα" if st["accounts"][x]["qty"]["hi"] else "⚪ έξω" for x in SYMBOLS)
        mark = "🏆 " if sid == best else ""
        lines.append(f"| **{sid}** | {mark}{cfg['name']} | {vals[sid]:.2f}€ ({pct(vals[sid])}) | {vl:.2f}€ ({pct(vl)}) | "
                     f"{s['trades']} | {win} | {s['fees']['hi']:.2f}€ | {max_dd(series):.1f}% | {pos} |")
    hs = [float(r["buy_hold"]) for r in rows if r.get("buy_hold")]
    lines += [f"| | Buy & hold | {hold:.2f}€ ({pct(hold)}) | | 0 | | 0€ | {max_dd(hs):.1f}% | 🟢 / 🟢 |", "",
              "**Πώς διαβάζεται:** «@0,25%» = με προμήθεια Kraken, «@0,09%» = με προμήθεια Revolut X (taker). "
              "Τα σήματα είναι ίδια, αλλάζει μόνο το κόστος. Max DD = μεγαλύτερη πτώση από κορυφή.", "",
              f"Συναλλαγές: `{TRADES_FILE}` · Αξία στον χρόνο: `{EQUITY_FILE}` · Backtest: `{BACKTEST_FILE}` · "
              "Προηγούμενος γύρος: `archive/`", "",
              "⚠️ Ψεύτικα λεφτά, μικρό δείγμα. Δεν είναι επενδυτική συμβουλή."]
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return vals, hold


def once():
    state = load_state()
    last = step(state)
    save_state(state)
    if len(last) != len(SYMBOLS):
        raise SystemExit("Αποτυχία λήψης τιμών, δες τα μηνύματα παραπάνω")
    vals, hold = write_results(state, last)
    for sid, v in vals.items():
        print(f"{sid}: {v:.2f}€ ({pct(v)})")
    print(f"Buy & hold: {hold:.2f}€ ({pct(hold)})")
    if not os.path.exists(BACKTEST_FILE):               # την πρώτη φορά φτιάχνει και το backtest
        try:
            backtest()
        except Exception as e:
            print(f"  ! backtest: {e}")


def status():
    state = load_state()
    last = {s: fetch_closes(s, 15)[-1][1] for s in SYMBOLS}
    for sid, cfg in STRATEGIES.items():
        v = value(state["strategies"][sid], last)
        print(f"{sid} {cfg['name']}: {v:.2f}€ ({pct(v)})")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--backtest", action="store_true")
    g.add_argument("--once", action="store_true")
    g.add_argument("--status", action="store_true")
    a = p.parse_args()
    if a.backtest: backtest()
    elif a.once: once()
    else: status()
