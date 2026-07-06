# XAUUSD Trading Signal Bot — Free-Tier Project Plan

## 1. What you're trying to build

An automated system that watches XAUUSD (gold) price action across multiple timeframes (5m, 15m, 30m, 4h), generates a BUY / SELL / HOLD signal with an entry, stop-loss, and take-profit levels, and emails you the alert — running unattended, on a schedule, at **zero ongoing cost**. It does not place trades; it only generates and delivers signals for you to act on manually.

## 2. Why the original design wasn't actually free

The version already in this folder (`xauusd_scheduler_github.py` + `github_actions_workflow.yml`) uses:

| Component | Original approach | Problem |
|---|---|---|
| Chart data | Screenshot TradingView via Browserless, every 5 min | ~720 screenshots/day — Browserless's free tier (50/month) is exceeded by ~300x within the first day |
| Analysis | Claude Opus vision model reads the screenshots | Vision calls on a premium model, 180x/day — real ongoing API spend, not covered by any free tier |
| Schedule | Every 5 minutes, 6am–9pm UTC, weekdays | On a *private* GitHub repo this burns through the 2,000 free Actions minutes/month in about 1–2 weeks |

None of this is free at the volume the workflow runs. To get to **actually $0/month**, the data source and analysis method both need to change — you can't keep the screenshot+vision approach and also avoid paying.

## 3. Redesigned architecture (target: $0/month, forever)

```
GitHub Actions (scheduled)
    │
    ▼
Fetch OHLC price data — yfinance (free, no API key, no rate-limit billing)
    │
    ▼
Compute indicators in pure Python — RSI, EMA cross, MACD (via the free `ta` library)
    │
    ▼
Rule-based signal engine — combines timeframes into BUY/SELL/HOLD + confidence
    │
    ▼
Email alert — Gmail SMTP with an app password (free)
    │
    ▼
Log row — appended to a CSV committed back to the repo (free, no Google Cloud setup needed)
```

Key changes from the original design and why each one removes a cost:

- **No Browserless.** Instead of screenshotting charts, pull raw OHLC candles directly with `yfinance` (ticker `GC=F` for gold futures, which tracks spot gold closely). No signup, no API key, no per-call billing.
- **No Claude/vision API calls.** Signal logic runs as deterministic, rule-based technical analysis (RSI thresholds, EMA crossovers, MACD histogram, multi-timeframe agreement) using the open-source `ta` Python package. This is the piece that makes "$0 forever" actually true — LLM APIs require a funded, billable account, so keeping one out of the loop is what removes the last recurring cost.
- **No Google Cloud project.** Instead of Google Sheets (which requires setting up a service account), log each run as a row appended to `signals_log.csv` in the repo. Simpler, and free by construction.
- **Public repo, OR reduced frequency on a private repo.** GitHub Actions is unlimited/free on public repos. If you'd rather keep it private, running every 15–30 minutes instead of every 5 keeps you comfortably inside the 2,000 free minutes/month.

## 4. Trade-off you should know about

Rule-based indicators (RSI/EMA/MACD) are more mechanical and less "context-aware" than an AI reading a chart — they won't notice things like chart patterns, news context, or support/resistance zones the way a vision model attempting real analysis might. In exchange, they're deterministic, free, fast, and don't depend on any third-party paid service staying available. This is the standard trade-off for a genuinely $0 setup.

## 5. Cost summary

| Item | Service | Cost |
|---|---|---|
| Price data | yfinance | $0 |
| Indicator computation | `ta` (Python library) | $0 |
| Compute/scheduling | GitHub Actions | $0 (public repo, or private repo at ≤30 min frequency) |
| Email alerts | Gmail SMTP + app password | $0 |
| Logging | CSV committed to repo | $0 |
| **Total** | | **$0/month** |

## 6. Setup checklist

1. Create a GitHub repo (public, for unlimited free Actions minutes — or private if you're okay with a lower run frequency).
2. Add the redesigned script (`fetch data → compute indicators → decide signal → email → log`) and a `requirements.txt` with `yfinance`, `ta`, `pandas`.
3. Add a Gmail App Password as a repo secret (`GMAIL_USER`, `GMAIL_APP_PASSWORD`) — no other secrets required.
4. Add the GitHub Actions workflow file with a cron schedule matched to your chosen frequency.
5. Trigger it manually once (`workflow_dispatch`) to confirm it runs end-to-end, then let the schedule take over.

## 7. Important caveats

- This is a signal generator, not a trading system — it does not size positions, manage risk, or place orders.
- Rule-based technical signals are not guaranteed to be profitable and are not financial advice; treat alerts as one input among many.
- Free data sources like yfinance can occasionally be delayed, rate-limited, or briefly unavailable — there's no SLA the way there would be with a paid data feed.
- If you later want AI-assisted reasoning back in the loop, it can be added, but that reintroduces a real (if small) recurring cost — there's no way around that for any hosted LLM.

---

Next step: if this direction looks right, I can write the actual replacement script and workflow file that implement section 3.
