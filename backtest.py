#!/usr/bin/env python3
"""
Quick backtest of the rule-based signal engine used in xauusd_signals_free.py.

Limitation up front: yfinance only serves ~60 days of history for 5m/15m/30m
intraday candles, so this is a short-sample backtest, not a multi-year study.
Treat the numbers as a sanity check, not proof of an edge.

Method:
- Fetch 5m/15m/30m/4h (resampled from 1h) candles for GC=F over the same
  60-day window.
- Compute RSI/EMA20/EMA50/MACD/ATR causally (these indicators only look
  backward by construction, so no lookahead bias from vectorizing them).
- Walk forward bar-by-bar on the 15m timeline (matches the live script's
  "primary" timeframe). At each bar, look up the most recently CLOSED bar
  on each timeframe (<= current time) and combine into a signal using the
  same vote logic as the live script.
- When a BUY/SELL signal fires (HIGH/MEDIUM confidence, respecting the same
  60-min cooldown as the live script), simulate the trade forward using
  subsequent 15m highs/lows against a 1.5x-ATR stop and a 1x-ATR first
  target, for up to 48 bars (~12h).
- Report win rate, average R, total R, and max drawdown in R.
"""

import pandas as pd
import numpy as np
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange

SYMBOL = "GC=F"
MAX_HOLD_BARS = 48  # ~12 hours on the 15m timeline
COOLDOWN = pd.Timedelta(minutes=60)


def fetch(interval: str, period: str) -> pd.DataFrame:
    df = yf.Ticker(SYMBOL).history(period=period, interval=interval)
    df = df[["Open", "High", "Low", "Close"]].dropna()
    return df


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    close = df["Close"]
    df["rsi"] = RSIIndicator(close=close, window=14).rsi()
    df["ema_fast"] = EMAIndicator(close=close, window=20).ema_indicator()
    df["ema_slow"] = EMAIndicator(close=close, window=50).ema_indicator()
    macd_ind = MACD(close=close)
    df["macd"] = macd_ind.macd()
    df["macd_signal"] = macd_ind.macd_signal()
    df["atr"] = AverageTrueRange(high=df["High"], low=df["Low"], close=close, window=14).average_true_range()
    return df


def bias_row(row) -> str:
    if pd.isna(row["rsi"]) or pd.isna(row["ema_slow"]) or pd.isna(row["macd_signal"]):
        return None
    bullish = sum([
        row["ema_fast"] > row["ema_slow"],
        row["macd"] > row["macd_signal"],
        40 <= row["rsi"] <= 70,
    ])
    bearish = sum([
        row["ema_fast"] < row["ema_slow"],
        row["macd"] < row["macd_signal"],
        30 <= row["rsi"] <= 60,
    ])
    if bullish >= 2:
        return "BULLISH"
    if bearish >= 2:
        return "BEARISH"
    return "NEUTRAL"


def load_timeframes() -> dict:
    specs = [
        ("5m", "5m", "60d", None),
        ("15m", "15m", "60d", None),
        ("30m", "30m", "60d", None),
        ("4h", "1h", "60d", "4h"),
    ]
    tf_data = {}
    for tf, interval, period, resample in specs:
        df = fetch(interval, period)
        if resample:
            df = df.resample(resample).agg(
                {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
            ).dropna()
        df = compute_indicators(df)
        df["bias"] = df.apply(bias_row, axis=1)
        tf_data[tf] = df
    return tf_data


def latest_bias(df: pd.DataFrame, ts) -> str:
    sub = df[df.index <= ts]
    if len(sub) == 0:
        return None
    b = sub.iloc[-1]["bias"]
    return b if b is not None and not (isinstance(b, float) and pd.isna(b)) else None


def combine(biases: dict):
    bullish = sum(1 for v in biases.values() if v == "BULLISH")
    bearish = sum(1 for v in biases.values() if v == "BEARISH")
    if bullish >= 3:
        return "BUY", "HIGH"
    if bullish == 2:
        return "BUY", "MEDIUM"
    if bearish >= 3:
        return "SELL", "HIGH"
    if bearish == 2:
        return "SELL", "MEDIUM"
    return "HOLD", "LOW"


def run_backtest(tf_data: dict) -> pd.DataFrame:
    decision = tf_data["15m"].dropna(subset=["bias"])
    trades = []
    last_signal_time = None
    last_signal_type = None

    for ts, row in decision.iterrows():
        biases = {tf: latest_bias(df, ts) for tf, df in tf_data.items()}
        if any(v is None for v in biases.values()):
            continue

        signal, confidence = combine(biases)
        if signal == "HOLD" or confidence == "LOW":
            continue

        if (last_signal_time is not None and signal == last_signal_type
                and (ts - last_signal_time) < COOLDOWN):
            continue

        atr = row["atr"]
        if pd.isna(atr) or atr <= 0:
            continue

        entry = row["Close"]
        if signal == "BUY":
            stop = entry - 1.5 * atr
            tp1 = entry + 1.0 * atr
        else:
            stop = entry + 1.5 * atr
            tp1 = entry - 1.0 * atr

        future = decision[decision.index > ts].head(MAX_HOLD_BARS)
        outcome = "TIMEOUT"
        exit_price = None
        for _, frow in future.iterrows():
            if signal == "BUY":
                if frow["Low"] <= stop:
                    outcome, exit_price = "SL", stop
                    break
                if frow["High"] >= tp1:
                    outcome, exit_price = "TP1", tp1
                    break
            else:
                if frow["High"] >= stop:
                    outcome, exit_price = "SL", stop
                    break
                if frow["Low"] <= tp1:
                    outcome, exit_price = "TP1", tp1
                    break

        if exit_price is None:
            exit_price = future.iloc[-1]["Close"] if len(future) else entry

        risk = abs(entry - stop)
        r_multiple = ((exit_price - entry) / risk if signal == "BUY"
                      else (entry - exit_price) / risk)

        trades.append({
            "time": ts, "signal": signal, "confidence": confidence,
            "entry": round(entry, 2), "stop": round(stop, 2), "tp1": round(tp1, 2),
            "outcome": outcome, "exit": round(exit_price, 2), "r": round(r_multiple, 3),
        })

        last_signal_time = ts
        last_signal_type = signal

    return pd.DataFrame(trades)


def summarize(trades_df: pd.DataFrame):
    print("=" * 60)
    print("BACKTEST RESULTS - rule-based confluence signal, GC=F")
    print("=" * 60)

    if trades_df.empty:
        print("No signals triggered in this window - nothing to evaluate.")
        return

    n = len(trades_df)
    win_rate = (trades_df["r"] > 0).mean()
    avg_r = trades_df["r"].mean()
    total_r = trades_df["r"].sum()

    cum = trades_df["r"].cumsum()
    running_max = cum.cummax()
    drawdown = (cum - running_max)
    max_dd = drawdown.min()

    print(f"Total signals triggered: {n}")
    print(f"Win rate (R > 0): {win_rate:.1%}")
    print(f"Average R per trade: {avg_r:.2f}")
    print(f"Total R (sum of all trades): {total_r:.2f}")
    print(f"Max drawdown (R): {max_dd:.2f}")
    print()
    print("Outcome breakdown:")
    print(trades_df["outcome"].value_counts().to_string())
    print()
    print("By signal direction:")
    print(trades_df.groupby("signal")["r"].agg(["count", "mean", "sum"]).to_string())
    print()
    print("First 10 trades:")
    print(trades_df.head(10).to_string(index=False))

    trades_df.to_csv("backtest_trades.csv", index=False)
    print()
    print("Full trade list written to backtest_trades.csv")


if __name__ == "__main__":
    tf_data = load_timeframes()
    trades_df = run_backtest(tf_data)
    summarize(trades_df)
