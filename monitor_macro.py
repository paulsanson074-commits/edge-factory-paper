#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════╗
║  MACRO MONITOR — Edge Factory Paper Trading                             ║
║  Strategies: H-A24 (Treasury Auction → TLT)                            ║
║              H95  (FOMC Pre-Drift → TLT)                               ║
║              H-M02 (DXY 52w Breakout → USDBRL)                         ║
║                                                                         ║
║  Runs ONCE DAILY at ~21:30 UTC (after US market close) via cron.        ║
║  Calendar-driven signals — checks today against known event dates.       ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import os
import sys
import json
import requests
from datetime import datetime, timezone, timedelta, date
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trade_logger import (
    load_state, save_state, git_commit_and_push, now_utc,
    append_row, log_signal,
    get_macro_log_path, MACRO_COLUMNS
)

REPO_DIR = os.path.dirname(os.path.abspath(__file__))

# ═══════════════════════════════════════════════════════════════════════════
#  STRATEGY DEFINITIONS — FROZEN
# ═══════════════════════════════════════════════════════════════════════════

# H-A24: LONG TLT at T+4 after 10Y/30Y coupon auction, hold 5 trading days
# H95:   LONG TLT on day before FOMC announcement, exit at close of FOMC day
# H-M02: LONG USDBRL when DXY makes new 52-week high, hold ~15 days

MACRO_COST_BPS = {
    "H-A24": 5.0,    # TLT: tight spread, minimal slippage
    "H95":   5.0,
    "H-M02": 20.0,   # USDBRL: wider spread, EM premium
}

# ═══════════════════════════════════════════════════════════════════════════
#  FOMC CALENDAR — 2026 (update annually)
#  Source: federalreserve.gov/monetarypolicy/fomccalendars.htm
# ═══════════════════════════════════════════════════════════════════════════

FOMC_DATES_2026 = [
    # Announcement dates (2-day meetings: entry day before, exit on announcement)
    "2026-01-28",  # Jan meeting
    "2026-03-18",  # Mar meeting
    "2026-05-06",  # May meeting
    "2026-06-17",  # Jun meeting
    "2026-07-29",  # Jul meeting
    "2026-09-16",  # Sep meeting
    "2026-11-04",  # Nov meeting
    "2026-12-16",  # Dec meeting
]

# Add prior year dates for reference (H95 entry = day BEFORE announcement)
FOMC_DATES_2025 = [
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-17",
]

ALL_FOMC_DATES = FOMC_DATES_2025 + FOMC_DATES_2026


# ═══════════════════════════════════════════════════════════════════════════
#  DATA FETCHERS
# ═══════════════════════════════════════════════════════════════════════════

def fetch_treasury_auction_dates() -> list:
    """
    Fetch recent and upcoming Treasury auction dates from Treasury Fiscal Data API.
    Returns list of dicts with security_type, auction_date, issue_date.
    We care about 10Y and 30Y COUPON auctions (not bills).
    """
    try:
        # Fetch auctions from last 60 days
        cutoff = (date.today() - timedelta(days=60)).strftime("%Y-%m-%d")
        url = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query"
        params = {
            "filter": f"auction_date:gte:{cutoff},security_type:eq:Note,Bond",
            "sort": "-auction_date",
            "page[size]": "50",
            "fields": "security_type,security_term,auction_date,issue_date,high_yield",
        }
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", [])

        # Filter for 10Y notes and 30Y bonds
        relevant = []
        for d in data:
            term = d.get("security_term", "")
            sec_type = d.get("security_type", "")
            if ("10-Year" in term or "30-Year" in term) and sec_type in ("Note", "Bond"):
                relevant.append({
                    "security_type": f"{sec_type} ({term})",
                    "auction_date": d["auction_date"],
                    "issue_date": d.get("issue_date", ""),
                    "high_yield": d.get("high_yield", ""),
                })

        print(f"  Treasury API: {len(relevant)} relevant auctions found")
        return relevant

    except Exception as e:
        print(f"  Treasury API error: {e}")
        return []


def fetch_yfinance_price(ticker: str) -> dict:
    """Fetch latest close price for a ticker using yfinance."""
    try:
        import yfinance as yf
        data = yf.download(ticker, period="5d", progress=False, auto_adjust=True)
        if isinstance(data.columns, __import__('pandas').MultiIndex):
            data.columns = [c[0] for c in data.columns]
        if len(data) == 0:
            return {"price": 0, "date": "", "error": "no data"}
        last = data.iloc[-1]
        return {
            "price": float(last["Close"]),
            "date": str(data.index[-1].date()),
            "error": None,
        }
    except Exception as e:
        return {"price": 0, "date": "", "error": str(e)}


def fetch_dxy_52w_high() -> dict:
    """Check if DXY made a new 52-week high today."""
    try:
        import yfinance as yf
        data = yf.download("DX-Y.NYB", period="1y", progress=False, auto_adjust=True)
        if isinstance(data.columns, __import__('pandas').MultiIndex):
            data.columns = [c[0] for c in data.columns]
        if len(data) < 20:
            return {"is_breakout": False, "error": "insufficient data"}

        today_close = float(data["Close"].iloc[-1])
        prior_high = float(data["Close"].iloc[:-1].max())
        is_breakout = today_close > prior_high

        return {
            "is_breakout": is_breakout,
            "today_close": today_close,
            "prior_52w_high": prior_high,
            "date": str(data.index[-1].date()),
            "error": None,
        }
    except Exception as e:
        return {"is_breakout": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
#  BUSINESS DAY HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def add_business_days(start_date: date, n: int) -> date:
    """Add n business days to a date (ignores holidays — good enough for monitoring)."""
    current = start_date
    added = 0
    while added < n:
        current += timedelta(days=1)
        if current.weekday() < 5:  # Mon-Fri
            added += 1
    return current


def get_business_days_ago(d: date, n: int) -> date:
    """Go back n business days."""
    current = d
    removed = 0
    while removed < n:
        current -= timedelta(days=1)
        if current.weekday() < 5:
            removed += 1
    return current


# ═══════════════════════════════════════════════════════════════════════════
#  H-A24: TREASURY AUCTION DEALER INVENTORY
# ═══════════════════════════════════════════════════════════════════════════

def check_h_a24(state: dict, today: date):
    """
    H-A24: LONG TLT at T+4 (4 business days after auction), hold 5 business days.
    Check if today = T+4 for any recent 10Y/30Y auction.
    """
    hyp_id = "H-A24"
    print(f"\n  --- {hyp_id}: Treasury Auction Check ---")

    auctions = fetch_treasury_auction_dates()
    if not auctions:
        print(f"  No auction data available")
        return

    for auction in auctions:
        auction_date = date.fromisoformat(auction["auction_date"])
        entry_date = add_business_days(auction_date, 4)   # T+4
        exit_date = add_business_days(entry_date, 5)       # Hold 5 days

        if entry_date == today:
            print(f"  SIGNAL: {auction['security_type']} auctioned {auction_date}, "
                  f"T+4 entry today")

            # Check if already in this trade
            trade_key = f"HA24-{auction_date.strftime('%Y%m%d')}"
            if trade_key in state.get("macro", {}):
                print(f"  Already in trade {trade_key}")
                log_signal(hyp_id, "AUCTION", auction["security_type"],
                           "SKIPPED_ALREADY_OPEN", trade_key)
                continue

            # Fetch TLT price
            tlt = fetch_yfinance_price("TLT")
            if tlt["error"]:
                print(f"  TLT fetch error: {tlt['error']}")
                continue

            signal_id = trade_key
            entry_time = now_utc()

            # Log entry
            row = {
                "signal_id": signal_id,
                "hypothesis_id": hyp_id,
                "instrument": "TLT",
                "direction": "long",
                "trigger_datetime_utc": entry_time,
                "trigger_description": f"{auction['security_type']} auction on {auction_date}, T+4 entry",
                "trigger_value": auction.get("high_yield", ""),
                "entry_datetime_utc": entry_time,
                "entry_price": tlt["price"],
                "exit_datetime_utc": "",
                "exit_price": "",
                "return_gross_bps": "",
                "cost_bps": MACRO_COST_BPS[hyp_id],
                "return_net_bps": "",
                "hold_days": "",
                "status": "OPEN",
                "git_sha_entry": "",
                "git_sha_exit": "",
            }
            append_row(get_macro_log_path(hyp_id), MACRO_COLUMNS, row)
            log_signal(hyp_id, "AUCTION", auction["security_type"],
                       "ENTERED", f"TLT={tlt['price']:.2f}")

            # Track in state
            state.setdefault("macro", {})[signal_id] = {
                "hypothesis_id": hyp_id,
                "instrument": "TLT",
                "entry_time": entry_time,
                "entry_price": tlt["price"],
                "exit_date": exit_date.isoformat(),
                "trigger": auction["security_type"],
            }

            sha = git_commit_and_push(f"H-A24 ENTRY: {signal_id} | TLT LONG @ {tlt['price']:.2f}")
            print(f"  Entry committed: {sha}")


def check_h_a24_exits(state: dict, today: date):
    """Close H-A24 positions that have reached their exit date."""
    for signal_id, pos in list(state.get("macro", {}).items()):
        if pos["hypothesis_id"] != "H-A24":
            continue
        exit_date = date.fromisoformat(pos["exit_date"])
        if today >= exit_date:
            _close_macro_position(state, signal_id, pos)


# ═══════════════════════════════════════════════════════════════════════════
#  H95: FOMC PRE-DRIFT
# ═══════════════════════════════════════════════════════════════════════════

def check_h95(state: dict, today: date):
    """
    H95: LONG TLT on the day BEFORE FOMC announcement.
    Exit at close of FOMC announcement day.
    """
    hyp_id = "H95"
    print(f"\n  --- {hyp_id}: FOMC Pre-Drift Check ---")

    tomorrow = today + timedelta(days=1)
    # Handle weekends: if today is Friday, tomorrow (Saturday) won't be FOMC
    tomorrow_str = tomorrow.strftime("%Y-%m-%d")

    if tomorrow_str in ALL_FOMC_DATES:
        print(f"  SIGNAL: FOMC announcement tomorrow ({tomorrow_str})")

        trade_key = f"H95-{tomorrow_str}"
        if trade_key in state.get("macro", {}):
            print(f"  Already in trade {trade_key}")
            return

        tlt = fetch_yfinance_price("TLT")
        if tlt["error"]:
            print(f"  TLT fetch error: {tlt['error']}")
            return

        signal_id = trade_key
        entry_time = now_utc()

        row = {
            "signal_id": signal_id,
            "hypothesis_id": hyp_id,
            "instrument": "TLT",
            "direction": "long",
            "trigger_datetime_utc": entry_time,
            "trigger_description": f"FOMC announcement {tomorrow_str}, pre-drift entry",
            "trigger_value": f"FOMC {tomorrow_str}",
            "entry_datetime_utc": entry_time,
            "entry_price": tlt["price"],
            "exit_datetime_utc": "",
            "exit_price": "",
            "return_gross_bps": "",
            "cost_bps": MACRO_COST_BPS[hyp_id],
            "return_net_bps": "",
            "hold_days": "",
            "status": "OPEN",
            "git_sha_entry": "",
            "git_sha_exit": "",
        }
        append_row(get_macro_log_path(hyp_id), MACRO_COLUMNS, row)
        log_signal(hyp_id, "FOMC", tomorrow_str, "ENTERED", f"TLT={tlt['price']:.2f}")

        state.setdefault("macro", {})[signal_id] = {
            "hypothesis_id": hyp_id,
            "instrument": "TLT",
            "entry_time": entry_time,
            "entry_price": tlt["price"],
            "exit_date": tomorrow_str,  # Exit on FOMC day
            "trigger": f"FOMC {tomorrow_str}",
        }

        sha = git_commit_and_push(f"H95 ENTRY: {signal_id} | TLT LONG @ {tlt['price']:.2f}")
        print(f"  Entry committed: {sha}")
    else:
        print(f"  No FOMC tomorrow")


def check_h95_exits(state: dict, today: date):
    """Close H95 positions on FOMC day."""
    today_str = today.strftime("%Y-%m-%d")
    for signal_id, pos in list(state.get("macro", {}).items()):
        if pos["hypothesis_id"] != "H95":
            continue
        if pos["exit_date"] == today_str or today > date.fromisoformat(pos["exit_date"]):
            _close_macro_position(state, signal_id, pos)


# ═══════════════════════════════════════════════════════════════════════════
#  H-M02: DXY 52-WEEK BREAKOUT → USDBRL
# ═══════════════════════════════════════════════════════════════════════════

def check_h_m02(state: dict, today: date):
    """
    H-M02: When DXY closes at a new 52-week high, go LONG USDBRL.
    Hold ~15 trading days.
    """
    hyp_id = "H-M02"
    print(f"\n  --- {hyp_id}: DXY 52w Breakout Check ---")

    dxy = fetch_dxy_52w_high()
    if dxy.get("error"):
        print(f"  DXY fetch error: {dxy['error']}")
        return

    print(f"  DXY today: {dxy.get('today_close', 'N/A'):.2f} | "
          f"52w high: {dxy.get('prior_52w_high', 'N/A'):.2f} | "
          f"Breakout: {dxy.get('is_breakout', False)}")

    if not dxy["is_breakout"]:
        return

    # Check cooldown — don't enter if already in an M02 trade
    for sig_id, pos in state.get("macro", {}).items():
        if pos["hypothesis_id"] == hyp_id:
            print(f"  Already in H-M02 trade: {sig_id}")
            log_signal(hyp_id, "DXY_BREAKOUT", f"DXY={dxy['today_close']:.2f}",
                       "SKIPPED_ALREADY_OPEN", sig_id)
            return

    # Fetch USDBRL
    brl = fetch_yfinance_price("BRL=X")
    if brl["error"]:
        print(f"  USDBRL fetch error: {brl['error']}")
        return

    signal_id = f"HM02-{today.strftime('%Y%m%d')}"
    entry_time = now_utc()
    exit_date = add_business_days(today, 15)

    row = {
        "signal_id": signal_id,
        "hypothesis_id": hyp_id,
        "instrument": "USDBRL",
        "direction": "long",
        "trigger_datetime_utc": entry_time,
        "trigger_description": f"DXY new 52w high: {dxy['today_close']:.2f} > {dxy['prior_52w_high']:.2f}",
        "trigger_value": f"DXY={dxy['today_close']:.2f}",
        "entry_datetime_utc": entry_time,
        "entry_price": brl["price"],
        "exit_datetime_utc": "",
        "exit_price": "",
        "return_gross_bps": "",
        "cost_bps": MACRO_COST_BPS[hyp_id],
        "return_net_bps": "",
        "hold_days": "",
        "status": "OPEN",
        "git_sha_entry": "",
        "git_sha_exit": "",
    }
    append_row(get_macro_log_path(hyp_id), MACRO_COLUMNS, row)
    log_signal(hyp_id, "DXY_BREAKOUT", f"DXY={dxy['today_close']:.2f}",
               "ENTERED", f"USDBRL={brl['price']:.4f}")

    state.setdefault("macro", {})[signal_id] = {
        "hypothesis_id": hyp_id,
        "instrument": "USDBRL",
        "entry_time": entry_time,
        "entry_price": brl["price"],
        "exit_date": exit_date.isoformat(),
        "trigger": f"DXY {dxy['today_close']:.2f}",
    }

    sha = git_commit_and_push(f"H-M02 ENTRY: {signal_id} | USDBRL LONG @ {brl['price']:.4f}")
    print(f"  Entry committed: {sha}")


def check_h_m02_exits(state: dict, today: date):
    """Close H-M02 positions at exit date."""
    for signal_id, pos in list(state.get("macro", {}).items()):
        if pos["hypothesis_id"] != "H-M02":
            continue
        exit_date = date.fromisoformat(pos["exit_date"])
        if today >= exit_date:
            _close_macro_position(state, signal_id, pos)


# ═══════════════════════════════════════════════════════════════════════════
#  SHARED EXIT LOGIC
# ═══════════════════════════════════════════════════════════════════════════

def _close_macro_position(state: dict, signal_id: str, pos: dict):
    """Close a macro position and update logs."""
    hyp_id = pos["hypothesis_id"]
    instrument = pos["instrument"]

    # Fetch exit price
    if instrument == "USDBRL":
        ticker = "BRL=X"
    else:
        ticker = instrument
    price_data = fetch_yfinance_price(ticker)

    if price_data["error"]:
        print(f"  Exit price fetch error for {instrument}: {price_data['error']}")
        return

    exit_price = price_data["price"]
    entry_price = pos["entry_price"]
    exit_time = now_utc()

    if entry_price > 0:
        return_gross = (exit_price - entry_price) / entry_price * 10000
    else:
        return_gross = 0.0

    cost = MACRO_COST_BPS[hyp_id]
    return_net = return_gross - cost

    entry_dt = date.fromisoformat(pos["entry_time"][:10])
    today = date.today()
    hold_days = (today - entry_dt).days

    print(f"\n  CLOSING {signal_id}: {instrument} "
          f"entry={entry_price:.4f} exit={exit_price:.4f} "
          f"gross={return_gross:+.1f} net={return_net:+.1f} bps ({hold_days}d)")

    # Update CSV
    _close_macro_in_log(
        get_macro_log_path(hyp_id),
        signal_id,
        {
            "exit_datetime_utc": exit_time,
            "exit_price": exit_price,
            "return_gross_bps": round(return_gross, 2),
            "return_net_bps": round(return_net, 2),
            "hold_days": hold_days,
            "status": "CLOSED",
        }
    )

    del state["macro"][signal_id]
    sha = git_commit_and_push(
        f"{hyp_id} EXIT: {signal_id} | {instrument} net={return_net:+.1f} bps"
    )
    print(f"  Exit committed: {sha}")


def _close_macro_in_log(log_path: str, signal_id: str, exit_data: dict):
    """Update OPEN → CLOSED in macro trade log."""
    import csv
    if not os.path.exists(log_path):
        return
    rows = []
    with open(log_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    for row in rows:
        if row["signal_id"] == signal_id and row["status"] == "OPEN":
            row.update(exit_data)
            break
    with open(log_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN — DAILY CRON
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print(f"{'='*70}")
    print(f"  MACRO MONITOR — {now_utc()}")
    print(f"{'='*70}")

    state = load_state()
    today = date.today()

    n_open = len(state.get("macro", {}))
    print(f"  Open macro trades: {n_open}")
    for sig_id, pos in state.get("macro", {}).items():
        print(f"    {sig_id}: {pos['instrument']} @ {pos['entry_price']:.4f}, "
              f"exit {pos['exit_date']}")

    # Check exits first (priority)
    print(f"\n  --- Checking exits ---")
    check_h_a24_exits(state, today)
    check_h95_exits(state, today)
    check_h_m02_exits(state, today)

    # Check new signals
    check_h_a24(state, today)
    check_h95(state, today)
    check_h_m02(state, today)

    # Save state
    save_state(state)

    print(f"\n  Done. Next check tomorrow ~21:30 UTC.")


if __name__ == "__main__":
    main()
