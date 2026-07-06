#!/usr/bin/env python3
"""
Backtest for the v2 signal engine - redesigned top-down instead of
"4 equally-weighted, highly-correlated timeframe votes".

What v1 got wrong (confirmed by its own backtest, 60-day GC=F window):
- 1,065 signals in 60 days (~18/day) - way too promiscuous, because 5m/15m/30m
  candles are highly correlated, so "multiple timeframes agree" fired
  constantly without adding real independent information.
- Risk:reward was backwards - 1.5x ATR stop vs only 1x ATR target, so even a
  60.5% win rate produced only +0.01 average R. That's not an edge, it's
  noise; real-world spread/slippage would push it negative.
- Cooldown only blocked repeating the SAME direction, so the system could
  (and did) flip BUY -> SELL -> BUY chasing short-term noise.
- Max drawdown of -27.66R against only +12.07R total gain - a fragile,
  low-quality equity curve even before costs.

v2 fixes, using standard top-down trend-following methodology:
- 4h is now a mandatory TREND GATE (EMA20/50 cross + MACD only, no RSI).
  If 4h has no clear trend, the system stands aside entirely - it does not
  trade a coin-flip market.
- 30m carries an ADX(14) trend-strength filter. ADX < 20 means "chop", and
  the system skips the bar - this directly targets the choppy-whipsaw
  failure mode from v1.
- 30m bias must also agree with the 4h trend (no counter-trend trades).
- 15m is used only for entry TIMING - it must also agree with the trend,
  triggering the actual entry bar.
- 5m is dropped entirely. It's too noisy to add real information for a
  multi-hour hold and was mostly restating what 15m already said.
- Risk:reward is flipped to 1x ATR stop / 2x ATR target (2:1), using the
  more stable 30m ATR rather than noisy 15m/5m ATR.
- A single GLOBAL cooldown (default 90 min) blocks any new trade regardless
  of direction, stopping the flip-flop pattern seen in v1.
"""

import pandas as pd
import numpy as np
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD, ADXIndicator
from ta.volatility import AverageTrueRange

SYMBOL = "GC=F"
MAX_HOLD_BARS = 96          # ~24h on the 15m decision timeline (wider target needs more room)
COOLDOWN = pd.Timedelta(minutes=90)
ADX_MIN = 20
ADX_HIGH = 25
STOP_ATR_MULT = 1.0
TP1_ATR_MULT = 2.0


def fetch(interval: str, period: str) -> pd.DataFrame:
    df = yf.Ticker(SYMBOL).history(period=period, interval=interval)
    return df[["Open", "High", "Low", "Close"]].dropna()


def compute_indicators(df: pd.DataFrame, with_adx: bool = False) -> pd.DataFrame:
    df = df.copy()
    close = df["Close"]
    df["rsi"] = RSIIndicator(close=close, window=14).rsi()
    df["ema_fast"] = EMAIndicator(close=close, window=20).ema_indicator()
    df["ema_slow"] = EMAIndicator(close=close, window=50).ema_indicator()
    macd_ind = MACD(close=close)
    df["macd"] = macd_ind.macd()
    df["macd_signal"] = macd_ind.macd_signal()
    df["atr"] = AverageTrueRange(high=df["High"], low=df["Low"], close=close, window=14).average_true_range()
    if with_adx:
        df["adx"] = ADXIndicator(high=df["High"], low=df["Low"], close=close, window=14).adx()
    return df


def trend_bias(row) -> str:
    """4h trend gate: pure structure, no RSI. Requires EMA + MACD to agree."""
    if pd.isna(row["ema_slow"]) or pd.isna(row["macd_signal"]):
        return None
    if row["ema_fast"] > row["ema_slow"] and row["macd"] > row["macd_signal"]:
        return "BULLISH"
    if row["ema_fast"] < row["ema_slow"] and row["macd"] < row["macd_signal"]:
        return "BEARISH"
    return "NEUTRAL"


def confluence_bias(row) -> str:
    """Used for 30m/15m: same 2-of-3 vote style as v1, kept for entry timing."""
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
        with_adx = (tf == "30m")
        df = compute_indicators(df, with_adx=with_adx)
        if tf == "4h":
            df["bias"] = df.apply(trend_bias, axis=1)
        else:
            df["bias"] = df.apply(confluence_bias, axis=1)
        tf_data[tf] = df
    return tf_data


def latest_row(df: pd.DataFrame, ts):
    sub = df[df.index <= ts]
    if len(sub) == 0:
        return None
    return sub.iloc[-1]


def run_backtest(tf_data: dict) -> pd.DataFrame:
    decision = tf_data["15m"].dropna(subset=["bias"])
    trades = []
    last_trade_time = None

    for ts, row15 in decision.iterrows():
        row4h = latest_row(tf_data["4h"], ts)
        row30m = latest_row(tf_data["30m"], ts)

        if row4h is None or row30m is None:
            continue
        if row4h["bias"] not in ("BULLISH", "BEARISH"):
            continue
        if pd.isna(row30m.get("adx")) or row30m["adx"] < ADX_MIN:
            continue
        if row30m["bias"] != row4h["bias"]:
            continue
        if row15["bias"] != row4h["bias"]:
            continue

        # Global cooldown - blocks ANY new trade, not just same-direction repeats
        if last_trade_time is not None and (ts - last_trade_time) < COOLDOWN:
            continue

        direction = "BUY" if row4h["bias"] == "BULLISH" else "SELL"
        confidence = "HIGH" if row30m["adx"] >= ADX_HIGH else "MEDIUM"

        atr = row30m["atr"]
        if pd.isna(atr) or atr <= 0:
            continue

        entry = row15["Close"]
        if direction == "BUY":
            stop = entry - STOP_ATR_MULT * atr
            tp1 = entry + TP1_ATR_MULT * atr
        else:
            stop = entry + STOP_ATR_MULT * atr
            tp1 = entry - TP1_ATR_MULT * atr

        future = decision[decision.index > ts].head(MAX_HOLD_BARS)
        outcome = "TIMEOUT"
        exit_price = None
        for _, frow in future.iterrows():
            if direction == "BUY":
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
        r_multiple = ((exit_price - entry) / risk if direction == "BUY"
                      else (entry - exit_price) / risk)

        trades.append({
            "time": ts, "signal": direction, "confidence": confidence,
            "adx_30m": round(row30m["adx"], 1),
            "entry": round(entry, 2), "stop": round(stop, 2), "tp1": round(tp1, 2),
            "outcome": outcome, "exit": round(exit_price, 2), "r": round(r_multiple, 3),
        })

        last_trade_time = ts

    return pd.DataFrame(trades)


def summarize(trades_df: pd.DataFrame):
    print("=" * 60)
    print("BACKTEST V2 RESULTS - top-down trend-gated confluence, GC=F")
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
    drawdown = cum - running_max
    max_dd = drawdown.min()

    print(f"Total signals triggered: {n}")
    print(f"Win rate (R > 0): {win_rate:.1%}")
    print(f"Average R per trade: {avg_r:.3f}")
    print(f"Total R (sum of all trades): {total_r:.2f}")
    print(f"Max drawdown (R): {max_dd:.2f}")
    print()
    print("Outcome breakdown:")
    print(trades_df["outcome"].value_counts().to_string())
    print()
    print("By signal direction:")
    print(trades_df.groupby("signal")["r"].agg(["count", "mean", "sum"]).to_string())
    print()
    print("By confidence:")
    print(trades_df.groupby("confidence")["r"].agg(["count", "mean", "sum"]).to_string())
    print()
    print("First 15 trades:")
    print(trades_df.head(15).to_string(index=False))

    trades_df.to_csv("backtest_v2_trades.csv", index=False)
    print()
    print("Full trade list written to backtest_v2_trades.csv")


if __name__ == "__main__":
    tf_data = load_timeframes()
    trades_df = run_backtest(tf_data)
    summarize(trades_df)
