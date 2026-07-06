# Free Edition — Setup Guide

No Anthropic key, no Browserless key, no billing anywhere. The only credential needed is a Gmail app password (also free).

## 1. Create a repo

- Go to https://github.com/new
- Public repo → unlimited free Actions minutes, any schedule frequency.
- Private repo → also fine, just keep the schedule at 15 min or slower (already set) to stay under the 2,000 free minutes/month.

## 2. Upload 3 files to the repo root

- `xauusd_signals_free.py`
- `requirements_free.txt`
- `github_actions_workflow_free.yml` → rename to `.github/workflows/trading.yml` when you add it (GitHub only picks up workflows from that exact folder)

## 3. Get a Gmail app password (2 min, free)

1. Go to https://myaccount.google.com/apppasswords (requires 2-Step Verification turned on)
2. Create an app password for "Mail"
3. Copy the 16-character password (remove spaces)

## 4. Add repo secrets

**Settings → Secrets and variables → Actions → New repository secret**

- `GMAIL_USER` → your Gmail address
- `GMAIL_APP_PASSWORD` → the 16-char app password from step 3

That's the entire secrets list — nothing else required.

## 5. Test it

1. **Actions** tab → select the workflow → **Run workflow** → **Run workflow**
2. Wait ~30 seconds, click the run to see logs
3. You should see each timeframe's bias (BULLISH/BEARISH/NEUTRAL), the combined signal, and either "Email sent" or "skipping" depending on confidence

## 6. Let it run

Once confirmed working, the schedule in the workflow file runs it automatically every 15 minutes during London/NY trading hours, Monday–Friday. Every run appends a row to `signals_log.csv` in the repo (so you get a running history you can open anytime), and updates `last_signal_state.json` so you don't get the same alert spammed every cycle — only on a new signal or after a 60-minute cooldown.

## What you will and won't get

- You'll get an email only when the multi-timeframe RSI/EMA/MACD confluence produces a BUY or SELL with MEDIUM or HIGH confidence.
- HOLD / LOW-confidence cycles are logged to the CSV but don't email you — check the sheet if you want to see every cycle, not just the alerts.
- This is directional, rule-based signal generation — not a backtested strategy and not financial advice. Treat it as one input, not a system to trade on blindly.

## Cost check

| Service | Plan used | Cost |
|---|---|---|
| GitHub Actions | Free tier | $0 |
| yfinance (price data) | No account needed | $0 |
| Gmail SMTP | Free app password | $0 |
| CSV logging | Committed to repo | $0 |

**Total: $0/month, no expiring trial, no credit card.**
