#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════╗
║  CASCADE MONITOR — Edge Factory Paper Trading                           ║
║  Strategies: H-B48v2 (ETH), H-B49 (SOL), H-B66 (DOGE),               ║
║              H-B67 (AVAX), H-B74 (LINK)                                ║
║                                                                         ║
║  Trigger: BTC drops ≥2% on any 15-min bar (close vs open)              ║
║  Action: LONG all 5 alts at next available price                        ║
║  Exit: 1 hour after entry (4 × 15-min bars)                            ║
║  Data: Binance public API (no key needed for reads)                     ║
║                                                                         ║
║  Designed to run every 5 minutes via GitHub Actions cron.               ║
║  State persisted in state/open_positions.json.                          ║
║  Git commits provide immutable timestamp proof.                         ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import os
import sys
import time
import json
import requests
from datetime import datetime, timezone, timedelta

# Add repo root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trade_logger import (
    load_state, save_state, git_commit_and_push, get_current_sha, now_utc,
    append_row, log_signal,
    get_cascade_log_path, CASCADE_COLUMNS
)

# ═══════════════════════════════════════════════════════════════════════════
#  STRATEGY CONFIGURATION — FROZEN
#  Do not modify after paper trading begins. Any changes = new version.
# ═══════════════════════════════════════════════════════════════════════════

CASCADE_TRIGGER_PCT = -2.0        # BTC must drop ≥2% on a 15-min bar
CASCADE_HOLD_MINUTES = 60         # 1 hour hold
CASCADE_DIRECTION = "long"        # Always LONG alts after BTC cascade

# Instruments and their hypothesis IDs
CASCADE_ALTS = {
    "BTCUSDT":  "H-B42v2",
    "ETHUSDT":  "H-B48v2",
    "SOLUSDT":  "H-B49",
    "DOGEUSDT": "H-B66",
    "AVAXUSDT": "H-B67",
    "LINKUSDT": "H-B74",
}

# Cost assumptions from Gate 2 (bps round-trip)
# These are ASSUMED costs for P&L tracking. Actual spread is measured separately.
COST_ASSUMPTIONS = {
    "H-B42v2": 8.0,   # BTC: spread 2 + commission 2 + impact 4 (L9 unresolved — paper trade resolves)
    "H-B48v2": 8.0,   # ETH: spread 3 + commission 2 + impact 3
    "H-B49":   7.0,   # SOL: spread 3 + commission 2 + impact 2
    "H-B66":  10.0,    # DOGE: spread 5 + commission 2 + impact 3
    "H-B67":  10.0,    # AVAX: spread 5 + commission 2 + impact 3
    "H-B74":  10.0,    # LINK: spread 5 + commission 2 + impact 3
}

# Binance API base — use futures API since these are perpetual strategies.
# Standard api.binance.com works from EU IPs but is blocked from US.
# fapi.binance.com (futures) has same geo-restrictions.
# If running from US-based GitHub Actions runner, use a self-hosted runner
# on a European VPS, or run the monitor directly on the VPS with cron.
BINANCE_BASE = os.environ.get("BINANCE_API_BASE", "https://fapi.binance.com")
USE_FUTURES_API = "fapi" in BINANCE_BASE

# Cooldown: don't re-trigger within 60 minutes of last cascade
CASCADE_COOLDOWN_MINUTES = 60


# ═══════════════════════════════════════════════════════════════════════════
#  BINANCE API — PUBLIC ENDPOINTS (NO KEY REQUIRED)
# ═══════════════════════════════════════════════════════════════════════════

def fetch_btc_klines(interval: str = "15m", limit: int = 4) -> list:
    """
    Fetch recent BTC 15-min klines from Binance.
    Returns list of dicts with open_time, open, high, low, close, volume.
    We fetch 4 bars = last 60 minutes of 15-min data.
    Supports both spot (/api/v3) and futures (/fapi/v1) endpoints.
    """
    if USE_FUTURES_API:
        url = f"{BINANCE_BASE}/fapi/v1/klines"
    else:
        url = f"{BINANCE_BASE}/api/v3/klines"
    params = {"symbol": "BTCUSDT", "interval": interval, "limit": limit}

    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            raw = resp.json()

            klines = []
            for k in raw:
                klines.append({
                    "open_time_ms": k[0],
                    "open_time_utc": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)
                                     .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "close_time_ms": k[6],
                    "close_time_utc": datetime.fromtimestamp(k[6] / 1000, tz=timezone.utc)
                                      .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "is_closed": k[6] < int(datetime.now(timezone.utc).timestamp() * 1000),
                })
            return klines

        except Exception as e:
            print(f"  Binance klines attempt {attempt+1} failed: {e}")
            if attempt < 2:
                time.sleep(2)
    return []


def fetch_book_ticker(symbol: str) -> dict:
    """
    Fetch top-of-book bid/ask for a symbol.
    Returns dict with bid_price, ask_price, mid_price, spread_bps.
    """
    if USE_FUTURES_API:
        url = f"{BINANCE_BASE}/fapi/v1/ticker/bookTicker"
    else:
        url = f"{BINANCE_BASE}/api/v3/ticker/bookTicker"
    params = {"symbol": symbol}

    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            bid = float(data["bidPrice"])
            ask = float(data["askPrice"])
            mid = (bid + ask) / 2
            spread_bps = (ask - bid) / mid * 10000 if mid > 0 else 0

            return {
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread_bps": round(spread_bps, 2),
                "timestamp": now_utc(),
            }

        except Exception as e:
            print(f"  bookTicker {symbol} attempt {attempt+1} failed: {e}")
            if attempt < 2:
                time.sleep(1)

    return {"bid": 0, "ask": 0, "mid": 0, "spread_bps": 0, "timestamp": now_utc()}


def fetch_btc_price() -> float:
    """Quick BTC price fetch for logging."""
    try:
        if USE_FUTURES_API:
            url = f"{BINANCE_BASE}/fapi/v1/ticker/price"
        else:
            url = f"{BINANCE_BASE}/api/v3/ticker/price"
        resp = requests.get(url, params={"symbol": "BTCUSDT"}, timeout=5)
        return float(resp.json()["price"])
    except:
        return 0.0


# ═══════════════════════════════════════════════════════════════════════════
#  CORE LOGIC
# ═══════════════════════════════════════════════════════════════════════════

def check_for_cascade(state: dict) -> list:
    """
    Check if any recent CLOSED 15-min BTC bar had a ≥2% decline.
    Returns list of trigger bars (usually 0 or 1).

    Deduplication: skip bars we've already processed (tracked in state).
    Cooldown: skip if a cascade was triggered within the last 60 minutes.
    """
    klines = fetch_btc_klines(interval="15m", limit=4)
    if not klines:
        print("  No kline data received")
        return []

    last_processed = state.get("last_processed_bar_ms", 0)
    last_cascade_time = state.get("last_cascade_time", "")
    triggers = []

    for k in klines:
        # Only check CLOSED bars
        if not k["is_closed"]:
            continue

        # Skip already-processed bars
        if k["open_time_ms"] <= last_processed:
            continue

        # Compute return
        ret_pct = (k["close"] - k["open"]) / k["open"] * 100

        print(f"  Bar {k['open_time_utc']}: BTC {k['open']:.0f} → {k['close']:.0f} "
              f"({ret_pct:+.2f}%)")

        # Update last processed regardless of trigger
        state["last_processed_bar_ms"] = k["open_time_ms"]

        # Check trigger
        if ret_pct <= CASCADE_TRIGGER_PCT:
            # Cooldown check
            if last_cascade_time:
                last_dt = datetime.fromisoformat(last_cascade_time.replace("Z", "+00:00"))
                bar_dt = datetime.fromtimestamp(k["open_time_ms"] / 1000, tz=timezone.utc)
                if (bar_dt - last_dt).total_seconds() < CASCADE_COOLDOWN_MINUTES * 60:
                    print(f"  CASCADE DETECTED but within cooldown "
                          f"(last: {last_cascade_time}). Skipping.")
                    log_signal("CASCADE", "CASCADE",
                               f"BTC {ret_pct:.2f}%",
                               "SKIPPED_COOLDOWN",
                               f"Bar {k['open_time_utc']}, within {CASCADE_COOLDOWN_MINUTES}m cooldown")
                    continue

            print(f"\n  *** CASCADE DETECTED: BTC {ret_pct:+.2f}% ***")
            triggers.append({
                "bar": k,
                "btc_return_pct": round(ret_pct, 4),
            })

    return triggers


def enter_cascade_trades(trigger: dict, state: dict) -> str:
    """
    Enter LONG positions on all 5 cascade alts.
    Fetches bookTicker for each, logs entry, updates state.
    Returns signal_id.
    """
    bar = trigger["bar"]
    btc_ret = trigger["btc_return_pct"]
    ts = datetime.fromtimestamp(bar["open_time_ms"] / 1000, tz=timezone.utc)
    signal_id = f"CASCADE-{ts.strftime('%Y%m%d-%H%M')}"
    entry_time = now_utc()

    print(f"\n  Signal: {signal_id}")
    print(f"  BTC bar: {bar['open_time_utc']} | {bar['open']:.0f} → {bar['close']:.0f} ({btc_ret:+.2f}%)")
    print(f"  Entering LONG on 6 instruments...")

    entries = {}

    for symbol, hyp_id in CASCADE_ALTS.items():
        book = fetch_book_ticker(symbol)
        print(f"    {symbol:12} bid={book['bid']:.6f}  ask={book['ask']:.6f}  "
              f"spread={book['spread_bps']:.1f} bps")

        # Build trade entry row
        row = {
            "signal_id": signal_id,
            "hypothesis_id": hyp_id,
            "instrument": symbol,
            "direction": CASCADE_DIRECTION,
            "trigger_datetime_utc": bar["open_time_utc"],
            "trigger_bar_open": bar["open"],
            "trigger_bar_close": bar["close"],
            "btc_return_pct": btc_ret,
            "entry_datetime_utc": entry_time,
            "entry_mid_price": book["mid"],
            "entry_bid": book["bid"],
            "entry_ask": book["ask"],
            "entry_spread_bps": book["spread_bps"],
            # Exit fields empty until close
            "exit_datetime_utc": "",
            "exit_mid_price": "",
            "exit_bid": "",
            "exit_ask": "",
            "exit_spread_bps": "",
            "return_gross_bps": "",
            "cost_bps": "",
            "return_net_bps": "",
            "hold_minutes": "",
            "status": "OPEN",
            "git_sha_entry": "",  # Filled after commit
            "git_sha_exit": "",
        }

        # Log entry to CSV
        log_path = get_cascade_log_path(hyp_id)
        append_row(log_path, CASCADE_COLUMNS, row)

        # Log to unified signal log
        log_signal(hyp_id, "CASCADE",
                   f"BTC {btc_ret:.2f}%, {symbol} mid={book['mid']:.6f}",
                   "ENTERED", f"signal={signal_id}")

        # Track in state for exit
        entries[symbol] = {
            "signal_id": signal_id,
            "hypothesis_id": hyp_id,
            "entry_time": entry_time,
            "entry_mid": book["mid"],
            "entry_bid": book["bid"],
            "entry_ask": book["ask"],
            "entry_spread_bps": book["spread_bps"],
            "exit_due": (datetime.now(timezone.utc) +
                         timedelta(minutes=CASCADE_HOLD_MINUTES)
                         ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    # Update state
    state["cascade"][signal_id] = entries
    state["last_cascade_time"] = entry_time

    # Git commit — THIS IS THE IMMUTABLE TIMESTAMP
    sha = git_commit_and_push(f"CASCADE ENTRY: {signal_id} | BTC {btc_ret:+.2f}% | 6 instruments LONG")

    # Update SHA in the log files (re-read last line and update)
    for symbol, hyp_id in CASCADE_ALTS.items():
        _update_last_row_sha(get_cascade_log_path(hyp_id), "git_sha_entry", sha)

    print(f"\n  Entry committed: {sha}")
    return signal_id


def check_and_close_positions(state: dict) -> list:
    """
    Check if any open cascade positions have reached their 1-hour exit time.
    Returns list of closed signal_ids.
    """
    now = datetime.now(timezone.utc)
    closed = []

    # Iterate over a copy since we'll modify during iteration
    for signal_id, positions in list(state.get("cascade", {}).items()):
        # Check exit time of first position (all share same exit time)
        first_pos = next(iter(positions.values()))
        exit_due = datetime.fromisoformat(first_pos["exit_due"].replace("Z", "+00:00"))

        if now < exit_due:
            remaining = (exit_due - now).total_seconds() / 60
            print(f"  {signal_id}: {remaining:.0f} min remaining")
            continue

        print(f"\n  Closing {signal_id} (hold period complete)")
        exit_time = now_utc()

        for symbol, pos in positions.items():
            hyp_id = pos["hypothesis_id"]
            book = fetch_book_ticker(symbol)

            entry_mid = pos["entry_mid"]
            exit_mid = book["mid"]

            # Compute returns
            if entry_mid > 0:
                return_gross = (exit_mid - entry_mid) / entry_mid * 10000
            else:
                return_gross = 0.0

            # Cost = actual half-spread at entry + actual half-spread at exit
            cost = pos["entry_spread_bps"] / 2 + book["spread_bps"] / 2
            return_net = return_gross - cost

            hold_min = CASCADE_HOLD_MINUTES
            actual_entry = datetime.fromisoformat(pos["entry_time"].replace("Z", "+00:00"))
            actual_hold = (now - actual_entry).total_seconds() / 60

            print(f"    {symbol:12} entry={entry_mid:.6f} exit={exit_mid:.6f} "
                  f"gross={return_gross:+.1f} cost={cost:.1f} net={return_net:+.1f} bps")

            # Update the OPEN row in CSV to CLOSED with exit data
            _close_trade_in_log(
                log_path=get_cascade_log_path(hyp_id),
                signal_id=signal_id,
                instrument=symbol,
                exit_data={
                    "exit_datetime_utc": exit_time,
                    "exit_mid_price": book["mid"],
                    "exit_bid": book["bid"],
                    "exit_ask": book["ask"],
                    "exit_spread_bps": book["spread_bps"],
                    "return_gross_bps": round(return_gross, 2),
                    "cost_bps": round(cost, 2),
                    "return_net_bps": round(return_net, 2),
                    "hold_minutes": round(actual_hold, 1),
                    "status": "CLOSED",
                }
            )

        # Remove from state
        del state["cascade"][signal_id]
        closed.append(signal_id)

        # Git commit for exit
        sha = git_commit_and_push(f"CASCADE EXIT: {signal_id} | {len(positions)} positions closed")

        # Update exit SHA
        for symbol, pos in positions.items():
            _update_closed_row_sha(
                get_cascade_log_path(pos["hypothesis_id"]),
                signal_id, symbol, sha
            )

        print(f"  Exit committed: {sha}")

    return closed


# ═══════════════════════════════════════════════════════════════════════════
#  CSV UPDATE HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _update_last_row_sha(filepath: str, sha_field: str, sha: str):
    """Update the SHA field in the last row of a CSV."""
    import csv
    if not os.path.exists(filepath):
        return
    rows = []
    with open(filepath, "r", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    if rows:
        rows[-1][sha_field] = sha
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _close_trade_in_log(log_path: str, signal_id: str, instrument: str,
                        exit_data: dict):
    """Find the OPEN row matching signal_id + instrument and update with exit data."""
    import csv
    if not os.path.exists(log_path):
        return
    rows = []
    with open(log_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    for row in rows:
        if (row["signal_id"] == signal_id and
            row["instrument"] == instrument and
            row["status"] == "OPEN"):
            row.update(exit_data)
            break

    with open(log_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _update_closed_row_sha(filepath: str, signal_id: str, instrument: str,
                           sha: str):
    """Update git_sha_exit for a specific closed trade."""
    import csv
    if not os.path.exists(filepath):
        return
    rows = []
    with open(filepath, "r", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    for row in rows:
        if (row["signal_id"] == signal_id and
            row["instrument"] == instrument and
            row["status"] == "CLOSED"):
            row["git_sha_exit"] = sha
            break

    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN — RUN EVERY 5 MINUTES VIA GITHUB ACTIONS
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print(f"{'='*70}")
    print(f"  CASCADE MONITOR — {now_utc()}")
    print(f"{'='*70}")

    state = load_state()

    # Count open positions
    n_open = sum(len(v) for v in state.get("cascade", {}).values())
    print(f"\n  Open cascade trades: {n_open}")
    print(f"  Last processed bar: {state.get('last_processed_bar_ms', 'none')}")
    print(f"  Last cascade: {state.get('last_cascade_time', 'none')}")

    # --- PHASE 1: Check for exits first (priority) ---
    print(f"\n  --- Checking exits ---")
    closed = check_and_close_positions(state)
    if closed:
        print(f"  Closed: {closed}")

    # --- PHASE 2: Check for new cascades ---
    print(f"\n  --- Checking for cascade triggers ---")
    triggers = check_for_cascade(state)

    if triggers:
        for trigger in triggers:
            # Only enter if no cascade positions currently open
            # (prevents overlapping cascades within cooldown)
            if state.get("cascade", {}):
                print(f"  Cascade detected but positions already open. Logging only.")
                log_signal("CASCADE", "CASCADE",
                           f"BTC {trigger['btc_return_pct']:.2f}%",
                           "SKIPPED_ALREADY_OPEN",
                           f"Bar {trigger['bar']['open_time_utc']}")
            else:
                enter_cascade_trades(trigger, state)
    else:
        print(f"  No cascade triggers")

    # Save state (always, even if nothing changed — updates last_processed_bar)
    save_state(state)

    # Commit state update if anything changed
    if triggers or closed:
        git_commit_and_push(f"STATE UPDATE: {now_utc()}")

    print(f"\n  Done. Next check in ~5 minutes.")


if __name__ == "__main__":
    main()
