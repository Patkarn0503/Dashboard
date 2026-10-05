#!/usr/bin/env python3
"""
Portfolio tracker
  Google Sheet (Holdings + Cost Basis)  ->  live prices  ->  "History" tab  ->  docs/index.html

Usage
  python tracker.py            # real run (needs GOOGLE_CREDENTIALS + SHEET_ID env vars)
  python tracker.py --mock     # offline test: fake holdings/prices, no Google, writes docs/index.html
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
BKK = ZoneInfo("Asia/Bangkok")
HISTORY_TAB = "History"
HISTORY_HEADER = ["date", "gold_usd", "crypto_usd", "stablecoin_usd",
                  "stocks_usd", "cash_usd", "total_usd", "usdthb"]

# ticker -> category.  A ticker not listed here stops the run (never guess).
CATEGORY = {
    "XAUT": "gold",
    "BTC": "crypto", "BNB": "crypto", "SOL": "crypto", "KITE": "crypto", "ALLO": "crypto",
    "USDT": "stablecoin", "USDC": "stablecoin", "RWUSD": "stablecoin", "USDE": "stablecoin",
    "USD1": "stablecoin", "RLUSD": "stablecoin", "USD": "stablecoin",
    "ASML": "stocks", "TSM": "stocks", "NVDA": "stocks", "AMZN": "stocks",
    "MSFT": "stocks", "TSLA": "stocks", "SPCX": "stocks",
    "THB": "cash",
}
COINGECKO = {"BTC": "bitcoin", "BNB": "binancecoin", "SOL": "solana"}   # add ids for KITE/ALLO if you hold them
YAHOO = {"ASML": "ASML", "TSM": "TSM", "NVDA": "NVDA", "AMZN": "AMZN", "MSFT": "MSFT",
         "TSLA": "TSLA", "SPCX": "SPCX", "XAUT": "GC=F"}               # gold: 1 XAUT = 1 troy oz = gold futures price
CATS = ["gold", "crypto", "stablecoin", "stocks"]
COST_KEYS = {"Gold": "gold", "Crypto": "crypto", "Stablecoin": "stablecoin", "Stocks": "stocks"}


def num(x):
    x = str(x).replace(",", "").replace("\\", "").replace("฿", "").strip()
    if x in ("", "-"):
        return 0.0
    return float(x)


# ---------------------------------------------------------------- sheet parsing
def parse_sheet(rows):
    """rows = list of lists (like gspread get_all_values). Returns (holdings, cost_thb)."""
    holdings, cost, mode = {}, {}, None
    for row in rows:
        row = list(row) + [""] * 6
        first = row[0].strip()
        if first.startswith("TRANSACTIONS"):
            mode = None
            continue
        if first.startswith("HOLDINGS"):
            mode = "h"
            continue
        if first.startswith("COST BASIS"):
            mode = "c"
            continue
        if mode == "h":
            if first in ("", "Category"):
                continue
            ticker = row[1].strip().upper()
            if ticker:
                holdings[ticker] = holdings.get(ticker, 0.0) + num(row[2])
        elif mode == "c":
            if first in ("", "Category") or first.startswith("Category"):
                continue
            cost[first] = num(row[1])
    return holdings, cost


# ---------------------------------------------------------------- prices
def fetch_prices(tickers, overrides):
    import requests
    import yfinance as yf

    prices, failed = {}, []
    cg_ids = {t: COINGECKO[t] for t in tickers if t in COINGECKO}
    if cg_ids:
        try:
            r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                             params={"ids": ",".join(cg_ids.values()), "vs_currencies": "usd"}, timeout=30)
            r.raise_for_status()
            data = r.json()
            for t, cid in cg_ids.items():
                if cid in data and "usd" in data[cid]:
                    prices[t] = float(data[cid]["usd"])
        except Exception as e:  # noqa
            print("CoinGecko error:", e)
    for t in tickers:
        if t in prices or t not in YAHOO:
            continue
        try:
            tk = yf.Ticker(YAHOO[t])
            p = None
            try:
                p = tk.fast_info["last_price"]
            except Exception:
                p = None
            if not p or p != p:
                p = float(tk.history(period="5d")["Close"].dropna().iloc[-1])
            prices[t] = float(p)
        except Exception as e:  # noqa
            print(f"Yahoo error for {t}:", e)
    try:
        tk = yf.Ticker("THB=X")
        p = None
        try:
            p = tk.fast_info["last_price"]
        except Exception:
            p = None
        if not p or p != p:
            p = float(tk.history(period="5d")["Close"].dropna().iloc[-1])
        fx = float(p)
    except Exception as e:  # noqa
        print("FX error:", e)
        fx = None
    for t, p in overrides.items():          # manual override file wins
        if t == "USDTHB":
            fx = float(p)
        else:
            prices[t] = float(p)
    for t in tickers:
        if CATEGORY.get(t) in ("gold", "crypto", "stocks") and t not in prices:
            failed.append(t)
    return prices, fx, failed


# ---------------------------------------------------------------- maths
def compute(holdings, prices, fx):
    usd = {c: 0.0 for c in CATS + ["cash"]}
    unknown = []
    for t, q in holdings.items():
        cat = CATEGORY.get(t)
        if cat is None:
            if q:
                unknown.append(t)
            continue
        if cat == "cash":
            usd["cash"] += q / fx                    # THB cash
        elif cat == "stablecoin":
            usd["stablecoin"] += q                   # $1.00 each (incl. USD cash)
        else:
            usd[cat] += q * prices[t]
    usd["total"] = sum(usd[c] for c in CATS + ["cash"])
    return usd, unknown


def history_row(today, usd, fx):
    return [today, round(usd["gold"], 2), round(usd["crypto"], 2), round(usd["stablecoin"], 2),
            round(usd["stocks"], 2), round(usd["cash"], 2), round(usd["total"], 2), round(fx, 4)]


# ---------------------------------------------------------------- Google Sheets
def open_sheet():
    import gspread
    from google.oauth2.service_account import Credentials
    info = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(os.environ["SHEET_ID"])


def load_seed():
    p = HERE / "history_seed.csv"
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return [r for r in csv.reader(f)][1:]


def upsert_history(ws, row):
    values = ws.get_all_values()
    if not values:
        ws.append_row(HISTORY_HEADER)
        values = [HISTORY_HEADER]
    if len(values) == 1:                                   # empty History tab -> import old data once
        seed = [r for r in load_seed() if r and r[0] != row[0]]
        if seed:
            ws.append_rows(seed, value_input_option="USER_ENTERED")
            values = ws.get_all_values()
    if len(values) > 1 and values[-1][0] == row[0]:        # same day -> overwrite last row
        n = len(values)
        ws.update(range_name=f"A{n}:H{n}", values=[row], value_input_option="USER_ENTERED")
    else:
        ws.append_row(row, value_input_option="USER_ENTERED")
    return ws.get_all_values()


def rows_to_hist(values):
    out = []
    for r in values[1:]:
        if len(r) < 7 or not r[0]:
            continue
        out.append({"date": r[0], "gold": num(r[1]), "crypto": num(r[2]), "stablecoin": num(r[3]),
                    "stocks": num(r[4]), "cash": num(r[5]), "total": num(r[6])})
    out.sort(key=lambda d: d["date"])
    return out


# ---------------------------------------------------------------- render
def render(hist, cost_thb, cash_thb, fx, note):
    cb = {
        "rate": fx,
        "thb": sum(cost_thb) + cash_thb,
        "costThb": cost_thb,           # gold, crypto, stablecoin, stocks
        "cashThb": cash_thb,
    }
    html = (HERE / "template.html").read_text(encoding="utf-8")
    html = (html.replace("__HIST__", json.dumps(hist)).replace("__CB__", json.dumps(cb))
            .replace("__UPDATED__", note).replace("__RATE__", f"{fx:.2f}"))
    out = HERE / "docs" / "index.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print("wrote", out)


# ---------------------------------------------------------------- main
MOCK_HOLDINGS = {"XAUT": 0.14617164, "BTC": 0.0280524485, "BNB": 3.48696552, "SOL": 0.930394634,
                 "USDT": 8772.33, "ASML": 0.6202959, "TSM": 1.4076358, "NVDA": 2.7003489,
                 "AMZN": 2.2403772, "MSFT": 1.2456455, "TSLA": 1.1602701, "SPCX": 1.0,
                 "THB": 5400.0, "USD": 9.56}
MOCK_PRICES = {"XAUT": 4169.0, "BTC": 86438.68, "BNB": 801.7, "SOL": 121.06, "ASML": 1867.31,
               "TSM": 472.78, "NVDA": 233.95, "AMZN": 251.52, "MSFT": 517.53, "TSLA": 370.59, "SPCX": 158.96}
MOCK_COST = {"Gold": 20000, "Crypto": 179247, "Stablecoin": 282949, "Stocks": 112404}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="offline test, no Google / no internet")
    args = ap.parse_args()
    now = dt.datetime.now(BKK)
    today = now.strftime("%Y-%m-%d")

    if args.mock:
        holdings, cost, prices, fx, ws = dict(MOCK_HOLDINGS), dict(MOCK_COST), dict(MOCK_PRICES), 33.61, None
    else:
        sh = open_sheet()
        holdings, cost = parse_sheet(sh.sheet1.get_all_values())
        ov_path = HERE / "price_overrides.json"
        overrides = json.loads(ov_path.read_text()) if ov_path.exists() else {}
        prices, fx, failed = fetch_prices(list(holdings), overrides)
        if fx is None or failed:
            sys.exit(f"ABORT: missing prices {failed} / fx={fx}. History not changed. "
                     f"Add them to price_overrides.json (e.g. {{\"SPCX\": 150.0, \"USDTHB\": 33.6}}) if needed.")
        try:
            ws = sh.worksheet(HISTORY_TAB)
        except Exception:
            ws = sh.add_worksheet(HISTORY_TAB, rows=2000, cols=len(HISTORY_HEADER))

    missing = [k for k in COST_KEYS if k not in cost]
    if missing:
        sys.exit(f"ABORT: COST BASIS section of the sheet is missing {missing}")
    usd, unknown = compute(holdings, prices, fx)
    if unknown:
        sys.exit(f"ABORT: unknown tickers {unknown}. Add them to CATEGORY in tracker.py")

    row = history_row(today, usd, fx)
    if ws is not None:
        values = upsert_history(ws, row)
    else:
        seed = [HISTORY_HEADER] + [r for r in load_seed() if r[0] != today] + [row]
        values = seed
    hist = rows_to_hist(values)

    cost_thb = [cost[k] for k in ("Gold", "Crypto", "Stablecoin", "Stocks")]
    cash_thb = holdings.get("THB", 0.0)
    note = f"Updated {now.strftime('%d %b %Y %H:%M')} (Bangkok)."
    render(hist, cost_thb, cash_thb, fx, note)
    print(f"total ${usd['total']:,.2f}  =  THB {usd['total'] * fx:,.0f}")


if __name__ == "__main__":
    main()
