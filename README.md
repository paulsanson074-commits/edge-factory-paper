# Edge Factory — Paper Trading Infrastructure

**Live out-of-sample signal monitoring for validated systematic trading strategies.**

This repository generates a verifiable, timestamped, tamper-evident record of every trading signal produced by the Edge Factory research framework. No capital is deployed — all positions are paper-traded with real-time market prices and actual order book spreads recorded at signal time.

---

## What This Proves

1. **Signals are generated ex-ante.** Git commits timestamp every entry before exits occur. The commit history is cryptographically chained — retroactive editing changes all downstream SHA hashes, making tampering detectable.

2. **Prices are real.** Entry and exit prices come from live Binance order book snapshots (bookTicker) and yfinance market data at the exact moment signals fire. These are verifiable against public historical data.

3. **Costs are honest.** Actual bid-ask spreads are recorded at entry and exit. Net returns deduct measured half-spread at each leg — not optimistic flat assumptions.

4. **Every signal is logged** — including signals that were skipped due to cooldown or existing open positions. The signal log is the complete record; the trade log is a subset.

---

## Strategies Monitored

### Cascade Spillover Family (Domain B, Intraday)

| ID | Instrument | Mechanism | Backtest Sharpe | Status |
|---|---|---|---|---|
| H-B42v2 | BTCUSDT perp | BTC ≥2% 15-min drop → long BTC 1h (own overshoot fade) | 0.68 | SHELVED (L9/L10 open) |
| H-B48v2 | ETHUSDT perp | BTC ≥2% 15-min drop → long ETH 1h | 1.06 | Gate 2 PASS |
| H-B49 | SOLUSDT perp | Same trigger → long SOL 1h | 1.58 | Gate 2 PASS |
| H-B66 | DOGEUSDT perp | Same trigger → long DOGE 1h | 1.01 | Gate 2 PASS |
| H-B67 | AVAXUSDT perp | Same trigger → long AVAX 1h | 1.56 | Gate 2 PASS |
| H-B74 | LINKUSDT perp | Same trigger → long LINK 1h | 0.97 | Gate 2 PASS |

**Note on H-B42v2:** Included for paper trade resolution of open Gate 2 questions. L9 (bid-ask spread) failed on OHLCV proxy artifact — paper trade captures real bookTicker data to resolve. L10 (frequency decay) flagged but μ per trade is stable. If paper trade confirms spread < 15 bps RT and positive mean over 20+ cascades → upgrades to portfolio.

**Trigger:** Any closed 15-minute Binance BTCUSDT kline with (close - open) / open ≤ -2%.
**Entry:** Immediately after trigger detection (within 5 min of bar close). Record bookTicker bid/ask for each instrument.
**Exit:** 60 minutes after entry. Record bookTicker bid/ask.
**Correlation note:** All 6 strategies share the same BTC trigger. Pairwise ρ estimated at 0.50–0.70. They are ONE allocation bucket, not six independent strategies.

### Macro Strategies (Domain A, Multi-Day)

| ID | Instrument | Mechanism | Backtest Sharpe |
|---|---|---|---|
| H-A24 | TLT | Long TLT at T+4 after 10Y/30Y auction, hold 5 days | 0.75 |
| H95 | TLT | Long TLT day before FOMC, exit on announcement day | 0.76 |
| H-M02 | USDBRL | Long USDBRL on DXY 52-week high, hold ~15 days | 0.53 |

---

## Repository Structure

```
├── monitor_cascade.py          # Runs every 5 min — cascade detection + trade mgmt
├── monitor_macro.py            # Runs daily — H-A24, H95, H-M02
├── trade_logger.py             # Shared CSV logging + git operations
├── audit.py                    # Verification tool for external review
├── requirements.txt
├── .github/workflows/
│   ├── cascade.yml             # Cron: */5 * * * *
│   └── macro.yml               # Cron: 30 21 * * 1-5
├── state/
│   └── open_positions.json     # Current open positions (updated atomically)
└── logs/
    ├── all_signals.csv         # EVERY signal detected (entered + skipped)
    ├── H-B42v2_trades.csv      # BTC cascade trades (shelved — paper resolves L9/L10)
    ├── H-B48v2_trades.csv      # ETH cascade trades
    ├── H-B49_trades.csv        # SOL cascade trades
    ├── H-B66_trades.csv        # DOGE cascade trades
    ├── H-B67_trades.csv        # AVAX cascade trades
    ├── H-B74_trades.csv        # LINK cascade trades
    ├── H-A24_trades.csv        # Treasury auction trades
    ├── H95_trades.csv          # FOMC pre-drift trades
    └── H-M02_trades.csv        # DXY→USDBRL trades
```

---

## Trade Log Schema

### Cascade Trades (CSV)

| Column | Description |
|---|---|
| signal_id | Unique: CASCADE-YYYYMMDD-HHMM |
| hypothesis_id | H-B48v2, H-B49, H-B66, H-B67, or H-B74 |
| instrument | ETHUSDT, SOLUSDT, DOGEUSDT, AVAXUSDT, LINKUSDT |
| direction | Always "long" |
| trigger_datetime_utc | When BTC 15-min bar closed |
| trigger_bar_open | BTC price at bar open |
| trigger_bar_close | BTC price at bar close |
| btc_return_pct | (close-open)/open × 100 |
| entry_datetime_utc | When entry was logged |
| entry_mid_price | (bid+ask)/2 from Binance bookTicker |
| entry_bid | Best bid at entry |
| entry_ask | Best ask at entry |
| entry_spread_bps | (ask-bid)/mid × 10000 |
| exit_datetime_utc | Entry + ~60 minutes |
| exit_mid_price | (bid+ask)/2 at exit |
| exit_bid | Best bid at exit |
| exit_ask | Best ask at exit |
| exit_spread_bps | Spread at exit |
| return_gross_bps | (exit_mid - entry_mid) / entry_mid × 10000 |
| cost_bps | entry_half_spread + exit_half_spread |
| return_net_bps | gross - cost |
| hold_minutes | Actual hold duration |
| status | OPEN or CLOSED |
| git_sha_entry | Commit SHA when entry was recorded |
| git_sha_exit | Commit SHA when exit was recorded |

### Macro Trades (CSV)

Same structure adapted for daily-frequency strategies, with `entry_price` / `exit_price` (close prices) instead of bid/ask snapshots, and `hold_days` instead of `hold_minutes`.

---

## Verification Guide for Auditors

### Step 1: Clone and inspect git history

```bash
git clone https://github.com/[user]/edge-factory-paper.git
cd edge-factory-paper
git log --oneline --all | head -50
```

Every trade entry and exit has a corresponding git commit. The commit timestamps are set by GitHub Actions runners (UTC) and cannot be retroactively modified without breaking the SHA chain.

### Step 2: Run the audit tool

```bash
pip install requests numpy pandas
python audit.py
```

This verifies: git integrity (no force-push/rebase), timestamp consistency, P&L arithmetic, and generates per-strategy performance summaries.

### Step 3: Cross-check against public data

For any cascade trade, you can verify the BTC trigger against Binance historical klines:

```python
import requests
# Example: verify CASCADE-20260301-1430
url = "https://api.binance.com/api/v3/klines"
params = {
    "symbol": "BTCUSDT",
    "interval": "15m",
    "startTime": 1740837600000,  # 2026-03-01 14:00 UTC in ms
    "limit": 5
}
data = requests.get(url, params=params).json()
for k in data:
    print(f"Open: {k[1]}, Close: {k[4]}, Return: {(float(k[4])-float(k[1]))/float(k[1])*100:.2f}%")
```

### Step 4: Verify no cherry-picking

The `all_signals.csv` log records EVERY signal detection — including those skipped due to cooldown or existing positions. Compare `all_signals.csv` (total detections) against trade logs (entries taken). The skip rate should be explainable by the documented cooldown rules.

### Step 5: Check for strategy parameter changes

```bash
git log --all -p -- monitor_cascade.py | grep "CASCADE_TRIGGER_PCT\|CASCADE_HOLD_MINUTES"
```

Strategy parameters are frozen at deployment. Any changes should be documented as a new version with a clear rationale.

---

## Research Framework Context

These strategies were discovered and validated through a systematic research process documented in the Edge Factory framework (Checklist V4.5). Key validation metrics:

- **>1,500 hypotheses tested**, >99% kill rate
- **8-layer Gate 1** validation (direction shuffle, bootstrap CI, walk-forward, year-by-year, cost survival, Bonferroni, sample size, timing shuffle)
- **18-layer Gate 2** deep validation (bid-ask verification, parameter robustness, structural break detection, portfolio correlation)
- **Timing shuffle test (L8)** as mandatory anti-confound filter — catches beta exposure that traditional backtests miss
- **Holm-Bonferroni correction** across all tested hypotheses in each batch

The full framework documentation, kill log, and validation scripts are available on request.

---

## Limitations

- **Paper, not live.** No real capital deployed. Entry/exit prices are mid-market snapshots, not actual fills. Slippage at real execution could differ.
- **GitHub Actions latency.** Cron jobs can be delayed 1-5 minutes. Cascade entries may occur 1-20 minutes after the BTC trigger bar closes, not immediately.
- **Cascade correlation.** The 5 cascade strategies are NOT independent. A bad cascade month affects all 5 simultaneously.
- **Macro data quality.** yfinance daily close prices for TLT and USDBRL are end-of-day approximations, not real-time fills.
- **No live spread data for macro.** TLT and USDBRL use flat cost assumptions, not measured spreads.

---

## Deployment

### Option A: VPS (Recommended for cascade monitor)

GitHub Actions standard runners are US-based, and Binance blocks US IPs. The cascade monitor requires a European VPS. The macro monitor (yfinance only) works fine on GitHub Actions.

```bash
# 1. Get a VPS — Hetzner Cloud CX22, €4/month, Frankfurt datacenter
# 2. SSH in and set up:

sudo apt update && sudo apt install -y python3-pip git cron
pip3 install requests yfinance numpy pandas

git clone https://github.com/[user]/edge-factory-paper.git
cd edge-factory-paper

# Configure git for commits
git config user.name "Edge Factory Bot"
git config user.email "bot@edge-factory.dev"

# 3. Add cron jobs (crontab -e):
# Cascade: every 5 minutes
*/5 * * * * cd /root/edge-factory-paper && python3 monitor_cascade.py >> /var/log/cascade.log 2>&1 && git add -A && git diff --cached --quiet || (git commit -m "cascade $(date -u +\%Y\%m\%dT\%H\%M)" && git push)

# Macro: daily at 21:30 UTC (weekdays)
30 21 * * 1-5 cd /root/edge-factory-paper && python3 monitor_macro.py >> /var/log/macro.log 2>&1 && git add -A && git diff --cached --quiet || (git commit -m "macro $(date -u +\%Y\%m\%d)" && git push)
```

### Option B: GitHub Actions with self-hosted runner

If you prefer GitHub Actions, set up a self-hosted runner on your VPS:
```bash
# Follow: https://docs.github.com/en/actions/hosting-your-own-runners
# Then the .github/workflows/*.yml files will run on your VPS automatically
```

### Option C: GitHub Actions only (macro strategies only)

If you only want to paper trade the macro strategies (H-A24, H95, H-M02), standard GitHub Actions works. The cascade monitor requires Binance API access from a non-US IP.

```bash
# 1. Create repo and push
git init edge-factory-paper && cd edge-factory-paper
# Copy all files
git add -A && git commit -m "Initial deployment"
git remote add origin https://github.com/[user]/edge-factory-paper.git
git push -u origin main

# 2. Enable Actions: Settings → Actions → General → Allow all actions
```

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| BINANCE_API_BASE | https://fapi.binance.com | Binance API endpoint. Use fapi for futures, api for spot. |

### Verify deployment

After first cron run, check:
```bash
# On VPS
tail -20 /var/log/cascade.log

# On GitHub
# Check Actions tab → cascade workflow runs
```

---

*Edge Factory — systematic alpha discovery with institutional validation standards.*
