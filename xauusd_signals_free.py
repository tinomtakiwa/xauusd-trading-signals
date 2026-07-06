#!/usr/bin/env python3
"""
XAUUSD Trading Signals - FREE Edition, v2 (top-down trend-gated)

Data source : yfinance (free, no API key, no billing)
Analysis    : top-down, trend-gated technical analysis - no LLM / vision API
              calls, so there is no recurring cost from this step.
Alerts      : Gmail SMTP with an app password (free)
Logging     : CSV file committed back to the repo by the GitHub Actions job

--- Why v2 exists ---
v1 treated 5m/15m/30m/4h as four equally-weighted "votes" and fired on a
simple majority. A 60-day backtest of that exact logic (see backtest.py /
FREE_TRADING_SIGNALS_PLAN.md history) showed the flaw plainly: 1,065 signals
in 60 days, a 60.5% win rate that still only produced +0.01 average R
(because the stop was wider than the target), and a max drawdown of -27.66R
against only +12.07R total gain. That is not an edge - it's noise dressed
up as confidence, because 5m/15m/30m candles are highly correlated and
"multiple timeframes agree" fired almost constantly.

v2 redesigns this the way an experienced systematic trader would:
- 4h is a mandatory TREND GATE (EMA20/50 cross + MACD only, no RSI). If the
  4h trend isn't clearly bullish or bearish, the system stands aside.
- 30m carries an ADX(14) trend-strength filter. ADX < 20 means "chop" and
  the bar is skipped - this directly targets the whipsaw failure mode.
- 30m's own bias must agree with the 4h trend (no counter-trend trades).
- 15m is used only for entry TIMING and must also agree with the trend.
- 5m is dropped entirely - too noisy to add real information for a
  multi-hour hold, and it was mostly restating what 15m already said.
- Risk:reward is flipped to 1x ATR stop / 2x-4x ATR targets (using the more
  stable 30m ATR), instead of v1's backwards 1.5x-ATR-stop / 1x-ATR-target.
- A single GLOBAL cooldown blocks any new trade regardless of direction,
  instead of only blocking repeats of the same direction.

Backtested on the same 60-day GC=F window as v1: 152 signals (vs 1,065),
46.1% win rate (lower is expected and fine - that's the trend-following
payoff shape), average R per trade of +0.382 (vs +0.01), total R of +58.0
(vs +12.07), and max drawdown of only -10.0R (vs -27.66R). Past performance
on a 60-day sample is not a guarantee of future results - treat this as a
sanity check, not proof of a durable edge.
"""

import os
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD, ADXIndicator
from ta.volatility import AverageTrueRange

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("trading.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ---- Config ----------------------------------------------------------

SYMBOL = "GC=F"  # COMEX Gold futures - tracks XAUUSD spot closely, free on yfinance

TIMEFRAMES = {
    "15m": {"interval": "15m", "period": "5d"},
    "30m": {"interval": "30m", "period": "1mo"},
    "4h":  {"interval": "1h",  "period": "3mo", "resample": "4h"},
}

ADX_MIN = 20            # below this, market is too choppy to trade
ADX_HIGH = 25           # at/above this, confidence upgrades to HIGH
STOP_ATR_MULT = 1.0
TP1_ATR_MULT = 2.0
TP2_ATR_MULT = 3.0
TP3_ATR_MULT = 4.0

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
COOLDOWN_MINUTES = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "90"))  # global, any direction

STATE_FILE = Path("last_signal_state.json")
LOG_CSV = Path("signals_log.csv")


# ---- Session check -----------------------------------------------------

def is_trading_session() -> tuple[bool, str]:
    """London/NY session check - same hours as before."""
    now = datetime.now(timezone.utc)
    day = now.weekday()  # 0 = Mon ... 6 = Sun
    hour = now.hour + now.minute / 60

    if day >= 5:
        return False, "Weekend"

    london = 6 <= hour <= 15
    ny = 12.5 <= hour <= 21

    if london and ny:
        return True, "London/NY Overlap"
    if london:
        return True, "London"
    if ny:
        return True, "NY"
    return False, "Closed"


# ---- Data + indicators --------------------------------------------------

def fetch_candles(interval: str, period: str, resample: str = None) -> pd.DataFrame:
    df = yf.Ticker(SYMBOL).history(period=period, interval=interval)
    if df.empty:
        raise ValueError(f"No data returned for interval={interval} period={period}")
    if resample:
        df = df.resample(resample).agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
        ).dropna()
    return df


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
    """4h trend gate: pure structure (EMA + MACD only, no RSI)."""
    if pd.isna(row["ema_slow"]) or pd.isna(row["macd_signal"]):
        return None
    if row["ema_fast"] > row["ema_slow"] and row["macd"] > row["macd_signal"]:
        return "BULLISH"
    if row["ema_fast"] < row["ema_slow"] and row["macd"] < row["macd_signal"]:
        return "BEARISH"
    return "NEUTRAL"


def confluence_bias(row) -> str:
    """30m/15m bias: 2-of-3 vote (EMA cross, MACD cross, RSI band)."""
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
    tf_data = {}
    for tf, cfg in TIMEFRAMES.items():
        df = fetch_candles(cfg["interval"], cfg["period"], cfg.get("resample"))
        with_adx = (tf == "30m")
        df = compute_indicators(df, with_adx=with_adx)
        tf_data[tf] = df
    return tf_data


def build_signal(tf_data: dict) -> dict:
    """Top-down: 4h = trend gate, 30m = strength + direction filter,
    15m = entry timing. All three must agree, and 30m ADX must show a
    real trend, or the system stands aside (returns HOLD)."""
    row_4h = tf_data["4h"].iloc[-1]
    row_30m = tf_data["30m"].iloc[-1]
    row_15m = tf_data["15m"].iloc[-1]

    bias_4h = trend_bias(row_4h)
    bias_30m = confluence_bias(row_30m)
    bias_15m = confluence_bias(row_15m)
    adx_30m = row_30m.get("adx")

    reasoning_bits = [
        f"4h={bias_4h}", f"30m={bias_30m} (ADX={adx_30m:.1f})" if pd.notna(adx_30m) else f"30m={bias_30m} (ADX=n/a)",
        f"15m={bias_15m}",
    ]

    stand_aside = {
        "signal": "HOLD", "confidence": "LOW",
        "entry_price": round(float(row_15m["Close"]), 2),
        "stop_loss": None, "TP1": None, "TP2": None, "TP3": None,
        "alignment": "no trade - filters not met",
        "reasoning": "; ".join(reasoning_bits),
        "timeframe_detail": {"4h": bias_4h, "30m": bias_30m, "15m": bias_15m, "adx_30m": adx_30m},
    }

    if bias_4h not in ("BULLISH", "BEARISH"):
        stand_aside["reasoning"] = "4h trend unclear, standing aside; " + stand_aside["reasoning"]
        return stand_aside
    if pd.isna(adx_30m) or adx_30m < ADX_MIN:
        stand_aside["reasoning"] = f"30m ADX below {ADX_MIN} (chop filter), standing aside; " + stand_aside["reasoning"]
        return stand_aside
    if bias_30m != bias_4h:
        stand_aside["reasoning"] = "30m does not confirm 4h trend, standing aside; " + stand_aside["reasoning"]
        return stand_aside
    if bias_15m != bias_4h:
        stand_aside["reasoning"] = "15m entry timing not aligned yet, standing aside; " + stand_aside["reasoning"]
        return stand_aside

    direction = "BUY" if bias_4h == "BULLISH" else "SELL"
    confidence = "HIGH" if adx_30m >= ADX_HIGH else "MEDIUM"

    atr = row_30m["atr"]
    entry = float(row_15m["Close"])

    if pd.isna(atr) or atr <= 0:
        stand_aside["reasoning"] = "ATR unavailable, standing aside; " + stand_aside["reasoning"]
        return stand_aside

    if direction == "BUY":
        stop = entry - STOP_ATR_MULT * atr
        tp1 = entry + TP1_ATR_MULT * atr
        tp2 = entry + TP2_ATR_MULT * atr
        tp3 = entry + TP3_ATR_MULT * atr
    else:
        stop = entry + STOP_ATR_MULT * atr
        tp1 = entry - TP1_ATR_MULT * atr
        tp2 = entry - TP2_ATR_MULT * atr
        tp3 = entry - TP3_ATR_MULT * atr

    return {
        "signal": direction,
        "confidence": confidence,
        "entry_price": round(entry, 2),
        "stop_loss": round(stop, 2),
        "TP1": round(tp1, 2),
        "TP2": round(tp2, 2),
        "TP3": round(tp3, 2),
        "alignment": f"4h/30m/15m all {bias_4h}, ADX={adx_30m:.1f}",
        "reasoning": (
            f"Top-down trend-gated: {', '.join(reasoning_bits)}. "
            f"2:1 reward:risk (stop={STOP_ATR_MULT}x ATR, TP1={TP1_ATR_MULT}x ATR)."
        ),
        "timeframe_detail": {"4h": bias_4h, "30m": bias_30m, "15m": bias_15m, "adx_30m": round(float(adx_30m), 1)},
    }


# ---- State + alerting --------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def should_send(analysis: dict, state: dict) -> bool:
    """Only email on BUY/SELL with HIGH/MEDIUM confidence, and enforce a
    GLOBAL cooldown - blocks ANY new alert (regardless of direction) within
    COOLDOWN_MINUTES of the last one, to stop flip-flop spam."""
    if analysis["signal"] not in ("BUY", "SELL"):
        return False
    if analysis["confidence"] not in ("HIGH", "MEDIUM"):
        return False

    last_sent_at = state.get("sent_at")
    if last_sent_at:
        elapsed_min = (datetime.now(timezone.utc) - datetime.fromisoformat(last_sent_at)).total_seconds() / 60
        if elapsed_min < COOLDOWN_MINUTES:
            return False

    return True


def send_email(analysis: dict, session: str):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        logger.info("Gmail not configured - skipping email")
        return

    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    subject = f"XAUUSD {analysis['signal']} - {analysis['confidence']} ({session})"
    body = f"""XAUUSD SIGNAL - free, top-down trend-gated edition (not financial advice)
{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}

Signal: {analysis['signal']}
Confidence: {analysis['confidence']}
Session: {session}

Entry: {analysis['entry_price']}
Stop Loss: {analysis['stop_loss']}
TP1: {analysis['TP1']}
TP2: {analysis['TP2']}
TP3: {analysis['TP3']}

{analysis['reasoning']}
Alignment: {analysis['alignment']}
"""

    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = GMAIL_USER
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.send_message(msg)

    logger.info(f"Email sent: {subject}")


def log_csv(analysis: dict, session: str, sent: bool):
    is_new = not LOG_CSV.exists()
    with LOG_CSV.open("a") as f:
        if is_new:
            f.write("timestamp,session,signal,confidence,entry,stop_loss,tp1,tp2,tp3,alignment,sent\n")
        f.write(
            f"{datetime.now(timezone.utc).isoformat()},{session},{analysis['signal']},"
            f"{analysis['confidence']},{analysis['entry_price']},{analysis['stop_loss']},"
            f"{analysis['TP1']},{analysis['TP2']},{analysis['TP3']},"
            f"\"{analysis['alignment']}\",{'yes' if sent else 'no'}\n"
        )


# ---- Main ----------------------------------------------------------------

def run():
    logger.info("=" * 60)
    logger.info("XAUUSD Trading Signals - FREE Edition v2 (top-down trend-gated)")
    logger.info("=" * 60)

    is_trading, session = is_trading_session()
    logger.info(f"Session: {session}")
    if not is_trading:
        logger.info("Outside trading hours - skipping")
        return

    try:
        tf_data = load_timeframes()
    except Exception as e:
        logger.error(f"Data fetch failed: {e}")
        return

    analysis = build_signal(tf_data)
    logger.info(f"Signal: {analysis['signal']} ({analysis['confidence']}) - {analysis['reasoning']}")

    state = load_state()
    sent = should_send(analysis, state)

    if sent:
        try:
            send_email(analysis, session)
            state = {"signal": analysis["signal"], "sent_at": datetime.now(timezone.utc).isoformat()}
            save_state(state)
        except Exception as e:
            logger.error(f"Email failed: {e}")
            sent = False

    log_csv(analysis, session, sent)
    logger.info("Complete\n")


if __name__ == "__main__":
    run()
