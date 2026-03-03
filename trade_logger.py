#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════╗
║  TRADE LOGGER — Edge Factory Paper Trading                              ║
║  Standardized CSV logging for auditable trade records                    ║
║  Every field verifiable against public Binance/yfinance data            ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import os
import csv
import json
import subprocess
from datetime import datetime, timezone

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(REPO_DIR, "logs")
STATE_DIR = os.path.join(REPO_DIR, "state")

os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════════
#  TRADE LOG CSV SCHEMA
# ═══════════════════════════════════════════════════════════════════════════

CASCADE_COLUMNS = [
    "signal_id",               # Unique: CASCADE-YYYYMMDD-HHMM
    "hypothesis_id",           # H-B48v2, H-B49, H-B66, H-B67, H-B74
    "instrument",              # ETHUSDT, SOLUSDT, etc.
    "direction",               # long
    # -- Trigger context --
    "trigger_datetime_utc",    # When BTC 15-min bar closed
    "trigger_bar_open",        # BTC bar open price
    "trigger_bar_close",       # BTC bar close price
    "btc_return_pct",          # (close-open)/open * 100
    # -- Entry --
    "entry_datetime_utc",      # When entry was logged
    "entry_mid_price",         # (bid+ask)/2
    "entry_bid",               # bookTicker bid
    "entry_ask",               # bookTicker ask
    "entry_spread_bps",        # (ask-bid)/mid * 10000
    # -- Exit --
    "exit_datetime_utc",       # entry + 1 hour
    "exit_mid_price",
    "exit_bid",
    "exit_ask",
    "exit_spread_bps",
    # -- Returns --
    "return_gross_bps",        # (exit_mid - entry_mid) / entry_mid * 10000
    "cost_bps",                # entry_half_spread + exit_half_spread
    "return_net_bps",          # gross - cost
    "hold_minutes",            # Actual hold duration
    # -- Metadata --
    "status",                  # OPEN / CLOSED
    "git_sha_entry",           # Commit SHA when entry logged
    "git_sha_exit",            # Commit SHA when exit logged
]

MACRO_COLUMNS = [
    "signal_id",               # e.g., HA24-YYYYMMDD, H95-YYYYMMDD
    "hypothesis_id",           # H-A24, H95, H-M02
    "instrument",              # TLT, USDBRL
    "direction",               # long
    # -- Trigger context --
    "trigger_datetime_utc",    # When signal condition met
    "trigger_description",     # e.g., "10Y auction on 2026-02-25, T+4 entry"
    "trigger_value",           # e.g., auction tail, DXY level
    # -- Entry --
    "entry_datetime_utc",
    "entry_price",             # Close price (daily resolution)
    # -- Exit --
    "exit_datetime_utc",
    "exit_price",
    # -- Returns --
    "return_gross_bps",
    "cost_bps",                # Flat estimate for daily instruments
    "return_net_bps",
    "hold_days",
    # -- Metadata --
    "status",                  # OPEN / CLOSED
    "git_sha_entry",
    "git_sha_exit",
]

SIGNAL_LOG_COLUMNS = [
    "datetime_utc",            # When signal was detected
    "hypothesis_id",           # Which strategy
    "signal_type",             # CASCADE / FOMC / AUCTION / DXY_BREAKOUT
    "trigger_value",           # BTC return %, DXY level, etc.
    "action_taken",            # ENTERED / SKIPPED_ALREADY_OPEN / SKIPPED_ERROR
    "details",                 # Any additional context
]


def _ensure_csv(filepath: str, columns: list):
    """Create CSV with header if it doesn't exist."""
    if not os.path.exists(filepath):
        with open(filepath, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()


def append_row(filepath: str, columns: list, row: dict):
    """Append a single row to a CSV file. Creates file with header if needed."""
    _ensure_csv(filepath, columns)
    with open(filepath, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writerow(row)


def get_cascade_log_path(hyp_id: str) -> str:
    """Get trade log path for a cascade strategy."""
    return os.path.join(LOGS_DIR, f"{hyp_id}_trades.csv")


def get_macro_log_path(hyp_id: str) -> str:
    """Get trade log path for a macro strategy."""
    return os.path.join(LOGS_DIR, f"{hyp_id}_trades.csv")


def get_signal_log_path() -> str:
    """Get the unified signal detection log."""
    return os.path.join(LOGS_DIR, "all_signals.csv")


def log_signal(hyp_id: str, signal_type: str, trigger_value: str,
               action: str, details: str = ""):
    """Log every signal detection — even if not traded."""
    path = get_signal_log_path()
    row = {
        "datetime_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hypothesis_id": hyp_id,
        "signal_type": signal_type,
        "trigger_value": trigger_value,
        "action_taken": action,
        "details": details,
    }
    append_row(path, SIGNAL_LOG_COLUMNS, row)


# ═══════════════════════════════════════════════════════════════════════════
#  STATE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════

STATE_FILE = os.path.join(STATE_DIR, "open_positions.json")


def load_state() -> dict:
    """Load current open positions. Returns dict with 'cascade' and 'macro' keys."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"cascade": {}, "macro": {}, "last_processed_bar": None}


def save_state(state: dict):
    """Save state to JSON."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════════
#  GIT OPERATIONS — IMMUTABLE TIMESTAMPS
# ═══════════════════════════════════════════════════════════════════════════

def git_commit_and_push(message: str) -> str:
    """
    Stage all changes, commit, push. Returns commit SHA.
    The git history IS the proof of timing. Every commit timestamp
    is immutable — retroactive editing changes all downstream SHAs.
    """
    try:
        subprocess.run(["git", "-C", REPO_DIR, "add", "-A"],
                       capture_output=True, check=True)

        result = subprocess.run(
            ["git", "-C", REPO_DIR, "commit", "-m", message,
             "--author", "Edge Factory Bot <bot@edge-factory.dev>"],
            capture_output=True, text=True
        )

        if result.returncode != 0:
            # Nothing to commit
            if "nothing to commit" in result.stdout + result.stderr:
                return get_current_sha()
            print(f"  Git commit warning: {result.stderr}")
            return "COMMIT_FAILED"

        # Push
        push_result = subprocess.run(
            ["git", "-C", REPO_DIR, "push"],
            capture_output=True, text=True
        )
        if push_result.returncode != 0:
            print(f"  Git push warning: {push_result.stderr}")

        return get_current_sha()

    except Exception as e:
        print(f"  Git error: {e}")
        return "GIT_ERROR"


def get_current_sha() -> str:
    """Get current HEAD commit SHA."""
    try:
        result = subprocess.run(
            ["git", "-C", REPO_DIR, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True
        )
        return result.stdout.strip()[:12]
    except Exception:
        return "NO_GIT"


def now_utc() -> str:
    """Current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
