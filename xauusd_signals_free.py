#!/usr/bin/env python3
"""
XAUUSD Trading Signals - FREE Edition

Data source : yfinance (free, no API key, no billing)
Analysis    : rule-based technical indicators (RSI, EMA, MACD, ATR) via the
              open-source `ta` library - no LLM / vision API calls, so there
              is no recurring cost from this step.
Alerts      : Gmail SMTP with an app password (free)
Logging     : CSV file committed back to the repo by the GitHub Actions job

This script generates signals only. It never places trades and is not
financial advice - treat output as one input among many.
"""

import os
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
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
    "5m":  {"interval": "5m",  "period": "5d"},
    "15m": {"interval": "15m", "period": "5d"},
    "30m": {"interval": "30m", "period": "1mo"},
    "4h":  {"interval": "1h",  "period": "3mo", "resample": "4h"},
}

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
COOLDOWN_MINUTES = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "60"))

STATE_FILE = Path("last_signal_state.json")
LOG_CSV = Path("signals_log.csv")


# ---- Session check -----------------------------------------------------

def is_trading_session() -> tuple[bool, str]:
    """London/NY session check - same hours as the original design."""
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
            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        ).dropna()
    return df


def analyze_timeframe(df: pd.DataFrame) -> dict:
    close = df["Close"]

    rsi = RSIIndicator(close=close, window=14).rsi()
    ema_fast = EMAIndicator(close=close, window=20).ema_indicator()
    ema_slow = EMAIndicator(close=close, window=50).ema_indicator()
    macd_ind = MACD(close=close)
    macd_line = macd_ind.macd()
    macd_signal = macd_ind.macd_signal()
    atr = AverageTrueRange(high=df["High"], low=df["Low"], close=close, window=14).average_true_range()

    rsi_v = float(rsi.iloc[-1])
    ema_fast_v = float(ema_fast.iloc[-1])
    ema_slow_v = float(ema_slow.iloc[-1])
    macd_v = float(macd_line.iloc[-1])
    macd_sig_v = float(macd_signal.iloc[-1])
    atr_v = float(atr.iloc[-1]) if not pd.isna(atr.iloc[-1]) else 0.0
    price = float(close.iloc[-1])

    bullish_votes = sum([
        ema_fast_v > ema_slow_v,
        macd_v > macd_sig_v,
        40 <= rsi_v <= 70,
    ])
    bearish_votes = sum([
        ema_fast_v < ema_slow_v,
        macd_v < macd_sig_v,
        30 <= rsi_v <= 60,
    ])

    if bullish_votes >= 2:
        bias = "BULLISH"
    elif bearish_votes >= 2:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    return {
        "bias": bias,
        "price": round(price, 2),
        "rsi": round(rsi_v, 1),
        "atr": round(atr_v, 2),
    }


def build_signal(tf_results: dict) -> dict:
    biases = [r["bias"] for r in tf_results.values()]
    bullish = biases.count("BULLISH")
    bearish = biases.count("BEARISH")
    total = len(biases)

    if bullish >= 3:
        signal, confidence = "BUY", "HIGH"
    elif bullish == 2:
        signal, confidence = "BUY", "MEDIUM"
    elif bearish >= 3:
        signal, confidence = "SELL", "HIGH"
    elif bearish == 2:
        signal, confidence = "SELL", "MEDIUM"
    else:
        signal, confidence = "HOLD", "LOW"

    primary = tf_results.get("15m") or next(iter(tf_results.values()))
    entry = primary["price"]
    atr = primary["atr"] or 1.0

    stop_loss = tp1 = tp2 = tp3 = None
    if signal == "BUY":
        stop_loss = round(entry - 1.5 * atr, 2)
        tp1, tp2, tp3 = (round(entry + m * atr, 2) for m in (1, 2, 3))
    elif signal == "SELL":
        stop_loss = round(entry + 1.5 * atr, 2)
        tp1, tp2, tp3 = (round(entry - m * atr, 2) for m in (1, 2, 3))

    dominant = max(bullish, bearish)
    neutral = total - bullish - bearish

    return {
        "signal": signal,
        "confidence": confidence,
        "entry_price": entry,
        "stop_loss": stop_loss,
        "TP1": tp1,
        "TP2": tp2,
        "TP3": tp3,
        "alignment": f"{dominant}/{total} timeframes agree",
        "reasoning": (
            f"{bullish} bullish / {bearish} bearish / {neutral} neutral across "
            f"{', '.join(tf_results.keys())} (RSI + EMA20/50 + MACD confluence, rule-based)"
        ),
        "timeframe_detail": tf_results,
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
    """Only email on BUY/SELL with HIGH/MEDIUM confidence, and avoid
    re-sending the same signal more often than COOLDOWN_MINUTES."""
    if analysis["signal"] not in ("BUY", "SELL"):
        return False
    if analysis["confidence"] not in ("HIGH", "MEDIUM"):
        return False

    last_signal = state.get("signal")
    last_sent_at = state.get("sent_at")

    if analysis["signal"] != last_signal:
        return True

    if last_sent_at:
        elapsed_min = (datetime.now(timezone.utc) - datetime.fromisoformat(last_sent_at)).total_seconds() / 60
        return elapsed_min >= COOLDOWN_MINUTES

    return True


def send_email(analysis: dict, session: str):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        logger.info("Gmail not configured - skipping email")
        return

    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    subject = f"XAUUSD {analysis['signal']} - {analysis['confidence']} ({session})"
    body = f"""XAUUSD SIGNAL - free, rule-based edition (not financial advice)
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
    logger.info("XAUUSD Trading Signals - FREE Edition")
    logger.info("=" * 60)

    is_trading, session = is_trading_session()
    logger.info(f"Session: {session}")
    if not is_trading:
        logger.info("Outside trading hours - skipping")
        return

    tf_results = {}
    for tf, cfg in TIMEFRAMES.items():
        try:
            df = fetch_candles(cfg["interval"], cfg["period"], cfg.get("resample"))
            tf_results[tf] = analyze_timeframe(df)
            r = tf_results[tf]
            logger.info(f"{tf}: {r['bias']} (price={r['price']}, rsi={r['rsi']})")
        except Exception as e:
            logger.error(f"{tf} failed: {e}")

    if not tf_results:
        logger.error("No timeframe data available - aborting")
        return

    analysis = build_signal(tf_results)
    logger.info(f"Signal: {analysis['signal']} ({analysis['confidence']}) - {analysis['alignment']}")

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
