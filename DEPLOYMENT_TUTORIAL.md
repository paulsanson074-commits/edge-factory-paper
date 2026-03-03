# Edge Factory Paper Trading — Deployment Tutorial

Step-by-step guide to get this running. Assumes you have Python 3.11+, git, and a GitHub account.

---

## PART 1: LOCAL SETUP (5 minutes)

### 1.1 Extract the archive

```bash
# Wherever you downloaded the tar.gz:
cd ~/Projects   # or wherever you keep your stuff
tar -xzf edge-factory-paper.tar.gz
cd edge-factory-paper
```

### 1.2 Test locally (from Paris — Binance works from EU IPs)

```bash
# Install dependencies
pip install -r requirements.txt

# Test cascade monitor — should connect to Binance and check recent BTC bars
python monitor_cascade.py

# Expected output (no cascade):
# ======================================================================
#   CASCADE MONITOR — 2026-02-28T...
# ======================================================================
#   Open cascade trades: 0
#   --- Checking exits ---
#   --- Checking for cascade triggers ---
#   Bar 2026-02-28T...: BTC 84500 → 84300 (-0.24%)
#   No cascade triggers
#   Done. Next check in ~5 minutes.

# Test macro monitor
python monitor_macro.py

# Test audit tool (will show empty report — no trades yet)
python audit.py
```

If both monitors run without errors, your code is good. If Binance returns 451 errors, you're hitting from a blocked IP (shouldn't happen from France).

---

## PART 2: GITHUB REPO (5 minutes)

### 2.1 Create the repo on GitHub

1. Go to https://github.com/new
2. Repository name: `edge-factory-paper`
3. **Make it PUBLIC** — this is the whole point, auditors need to see it
4. Do NOT initialize with README (we already have one)
5. Click "Create repository"

### 2.2 Push everything

```bash
cd ~/Projects/edge-factory-paper   # wherever you extracted it

# Initialize git
git init
git branch -M main

# Initial commit
git add -A
git commit -m "Initial deployment: paper trading infrastructure — 9 strategies (6 cascade + 3 macro)"

# Connect to GitHub (replace YOUR_USERNAME)
git remote add origin https://github.com/YOUR_USERNAME/edge-factory-paper.git

# Push
git push -u origin main
```

If you use SSH instead of HTTPS:
```bash
git remote add origin git@github.com:YOUR_USERNAME/edge-factory-paper.git
```

### 2.3 Verify on GitHub

Go to `https://github.com/YOUR_USERNAME/edge-factory-paper`. You should see all files, the README rendered, and the `.github/workflows/` folder.

---

## PART 3: DEPLOYMENT OPTIONS

You have two choices. Pick one.

### OPTION A: VPS (Recommended — €4/month)

GitHub Actions free-tier runners are US-based. Binance blocks US IPs. A €4/month European VPS solves this permanently and gives you more control.

#### 3A.1 Get a VPS

1. Go to https://www.hetzner.com/cloud/
2. Sign up, create a project
3. Create server:
   - Location: **Falkenstein** or **Helsinki** (EU)
   - Image: **Ubuntu 24.04**
   - Type: **CX22** (2 vCPU, 4GB RAM — overkill but cheapest, €4.51/month)
   - SSH key: add your public key (or use password)
4. Note the IP address

#### 3A.2 Set up the VPS

```bash
# SSH in
ssh root@YOUR_VPS_IP

# Update and install
apt update && apt upgrade -y
apt install -y python3-pip python3-venv git cron

# Clone your repo
git clone https://github.com/YOUR_USERNAME/edge-factory-paper.git
cd edge-factory-paper

# Install Python dependencies
pip3 install -r requirements.txt --break-system-packages

# Configure git (for automated commits)
git config user.name "Edge Factory Bot"
git config user.email "bot@edge-factory.dev"
```

#### 3A.3 Set up GitHub authentication for pushing

The bot needs to push commits. Easiest way is a Personal Access Token (PAT):

1. Go to https://github.com/settings/tokens?type=beta
2. "Generate new token" (Fine-grained)
3. Token name: `edge-factory-bot`
4. Repository access: "Only select repositories" → `edge-factory-paper`
5. Permissions: Contents → Read and write
6. Generate, copy the token

```bash
# On your VPS, set the remote URL with the token embedded:
cd /root/edge-factory-paper
git remote set-url origin https://YOUR_USERNAME:YOUR_TOKEN@github.com/YOUR_USERNAME/edge-factory-paper.git

# Test it works:
git pull   # should succeed without password prompt
```

#### 3A.4 Set up cron jobs

```bash
# Open crontab
crontab -e
# (choose nano or vim when prompted)
```

Paste these two lines at the bottom:

```cron
# Cascade monitor: every 5 minutes, 24/7
*/5 * * * * cd /root/edge-factory-paper && /usr/bin/python3 monitor_cascade.py >> /var/log/cascade.log 2>&1

# Macro monitor: daily at 21:30 UTC, weekdays only
30 21 * * 1-5 cd /root/edge-factory-paper && /usr/bin/python3 monitor_macro.py >> /var/log/macro.log 2>&1
```

Save and exit. Cron starts immediately.

#### 3A.5 Add auto-push after each monitor run

The monitors already call `git_commit_and_push()` internally when trades happen. But for state updates (even when nothing triggers), add a push wrapper:

```bash
# Create a wrapper script on the VPS
cat > /root/run_cascade.sh << 'EOF'
#!/bin/bash
cd /root/edge-factory-paper
git pull --rebase --quiet 2>/dev/null
python3 monitor_cascade.py
git add -A
git diff --cached --quiet || git commit -m "cascade run $(date -u +%Y-%m-%dT%H:%M:%SZ)"
git push --quiet 2>/dev/null
EOF

cat > /root/run_macro.sh << 'EOF'
#!/bin/bash
cd /root/edge-factory-paper
git pull --rebase --quiet 2>/dev/null
python3 monitor_macro.py
git add -A
git diff --cached --quiet || git commit -m "macro run $(date -u +%Y-%m-%dT%H:%M:%SZ)"
git push --quiet 2>/dev/null
EOF

chmod +x /root/run_cascade.sh /root/run_macro.sh
```

Update crontab to use the wrappers:

```cron
*/5 * * * * /root/run_cascade.sh >> /var/log/cascade.log 2>&1
30 21 * * 1-5 /root/run_macro.sh >> /var/log/macro.log 2>&1
```

#### 3A.6 Verify it's working

```bash
# Wait 5 minutes, then check:
tail -30 /var/log/cascade.log

# Check git log:
cd /root/edge-factory-paper
git log --oneline -5

# Check GitHub — you should see commits appearing
```

---

### OPTION B: GitHub Actions Only

This works for the **macro monitor** (yfinance doesn't block US IPs). The cascade monitor will fail on Binance calls from US-based runners. If you want cascade monitoring too, use Option A.

#### 3B.1 Enable GitHub Actions

1. Go to your repo on GitHub
2. Click **Settings** → **Actions** → **General**
3. Under "Actions permissions": select **Allow all actions**
4. Under "Workflow permissions": select **Read and write permissions**
5. Check **Allow GitHub Actions to create and approve pull requests**
6. Click **Save**

#### 3B.2 Verify workflows exist

Go to the **Actions** tab. You should see two workflows:
- "Cascade Monitor (5-min)" — will run but Binance calls will fail from US runners
- "Macro Monitor (Daily)" — will work

#### 3B.3 Test with manual trigger

1. Click on "Macro Monitor (Daily)"
2. Click **Run workflow** → **Run workflow**
3. Watch the run — it should complete in ~30 seconds
4. Check the commit history — you should see a new commit from the bot

---

## PART 4: VERIFY EVERYTHING IS RUNNING (next day)

### Check cascade monitor (VPS only)

```bash
# On VPS — check logs
tail -50 /var/log/cascade.log

# Should see entries every 5 minutes like:
# ======================================================================
#   CASCADE MONITOR — 2026-03-01T08:05:12Z
# ======================================================================
#   Open cascade trades: 0
#   Bar 2026-03-01T07:45:00Z: BTC 84100 → 83900 (-0.24%)
#   Bar 2026-03-01T08:00:00Z: BTC 83900 → 83850 (-0.06%)
#   No cascade triggers
```

### Check macro monitor

On GitHub, go to **Actions** tab → "Macro Monitor (Daily)" → check the last run completed.

### Check for commits

```bash
git log --oneline -20
# Should see periodic commits from the bot
```

### Simulate a cascade (optional stress test)

You can't force BTC to drop 2%, but you can test the entry/exit flow:

```bash
# On VPS, temporarily lower the threshold for testing
cd /root/edge-factory-paper

# Edit monitor_cascade.py, change CASCADE_TRIGGER_PCT from -2.0 to -0.1
# This will trigger on any 0.1% BTC drop (very frequent)
nano monitor_cascade.py

# Run manually
python3 monitor_cascade.py
# Should detect a "cascade" and enter 6 positions

# Wait 60+ minutes, run again
python3 monitor_cascade.py
# Should close the positions and log P&L

# CHECK THE LOGS
cat logs/H-B48v2_trades.csv
cat logs/all_signals.csv

# IMPORTANT: revert the threshold back to -2.0 after testing!
# And reset the state:
echo '{"cascade": {}, "macro": {}, "last_processed_bar_ms": 0, "last_cascade_time": ""}' > state/open_positions.json

# Commit the revert
git add -A
git commit -m "TEST COMPLETE: reverted threshold to -2.0, cleared test trades"
git push
```

---

## PART 5: ONGOING MAINTENANCE

### Update FOMC dates annually

The `monitor_macro.py` file has hardcoded FOMC dates. Update `FOMC_DATES_2027` when the Fed publishes their calendar (usually in June of the prior year).

```bash
# On VPS or locally:
cd edge-factory-paper
nano monitor_macro.py
# Add FOMC_DATES_2027 list
# Update ALL_FOMC_DATES to include 2027
git add -A
git commit -m "Update FOMC calendar for 2027"
git push
```

### Monitor VPS health

```bash
# Check cron is running
systemctl status cron

# Check disk space (logs grow slowly but check quarterly)
df -h

# Check cascade log isn't growing too large
ls -lh /var/log/cascade.log
# If >100MB, rotate:
> /var/log/cascade.log
```

### Run audit periodically

```bash
python3 audit.py
# Or for JSON output you can share:
python3 audit.py --json > audit_report.json
```

---

## TROUBLESHOOTING

**Binance returns 451 error:**
Your IP is blocked (US or sanctioned country). Use a European VPS.

**Git push fails with "authentication required":**
Your PAT expired or was revoked. Generate a new one and update the remote URL.

**Cron job not running:**
```bash
# Check cron daemon
systemctl status cron
# Check cron logs
grep CRON /var/log/syslog | tail -20
# Make sure scripts are executable
chmod +x /root/run_cascade.sh /root/run_macro.sh
```

**yfinance returns empty data for TLT:**
Market is closed (weekend/holiday). The macro monitor only runs on weekdays so this shouldn't happen. If it does, the monitor logs the error and skips — no bad data enters the trade log.

**Cascade monitor missed a cascade:**
GitHub Actions cron can delay up to 15 minutes under high load. VPS cron is more reliable (±1 second). The 5-minute polling interval means worst case you detect a cascade 5 minutes after the bar closes. With a 60-minute hold period, this delay is negligible for price execution but the entry timestamp in the CSV reflects when you actually observed it, not when the bar closed — which is the honest thing to log.

**Two cascade triggers overlap:**
The monitor skips new entries if positions are already open (logged as `SKIPPED_ALREADY_OPEN` in the signal log). The 60-minute cooldown also prevents re-triggering within an hour of the last cascade. Both are documented so auditors understand why some signals show "skipped."

---

## COST SUMMARY

| Item | Cost | Note |
|---|---|---|
| GitHub repo | Free | Public repo, unlimited Actions minutes |
| Hetzner VPS CX22 | €4.51/month | Optional but recommended for Binance access |
| **Total** | **€0–4.51/month** | |

---

## WHAT SUCCESS LOOKS LIKE

After 3 months, you should have:
- A public GitHub repo with 100+ days of automated commits
- ~5-15 cascade events logged with full bookTicker data
- ~2-3 FOMC trades, ~15-20 Treasury auction trades
- Any DXY breakout trades that occurred
- A clean `python audit.py` report showing P&L, spreads, and git integrity
- A verifiable, tamper-evident track record that any quant can audit

That's the artifact you show Julien, Darwinex, or any allocator.
