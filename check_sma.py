#!/usr/bin/env python3
"""
SMA 5/10/20 alert checker — runs on a schedule (GitHub Actions) and sends a
push notification when a ticker's price crosses ABOVE all three simple moving
averages (5, 10, 20 daily closes).

Data source : Yahoo Finance chart API (no key needed)
Notification: ntfy.sh (free push to your phone — just pick a topic name)
              optional Telegram bot as a second channel
State       : state.json (committed back to the repo by the workflow so the
              script only alerts on the *crossover*, not on every run)

Environment variables:
  TICKERS            comma-separated, e.g. "SOXL,NVDA,TSLA"   (default: SOXL)
  NTFY_TOPIC         your ntfy topic name, e.g. "marks-sma-alerts-x7q2"
  TELEGRAM_BOT_TOKEN optional
  TELEGRAM_CHAT_ID   optional
  ALERT_ON_DROP      "1" to also get notified when price falls back below
                     any of the SMAs (default: off)

No third-party packages required — Python 3.8+ standard library only.
"""

import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone

STATE_FILE = "state.json"
USER_AGENT = "Mozilla/5.0 (sma-alert-bot; +https://github.com)"


# ----------------------------- data ------------------------------------- #

def fetch_yahoo(ticker: str):
    """Return (closes_oldest_to_newest, live_price_or_None)."""
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?range=3mo&interval=1d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)

    result = data["chart"]["result"][0]
    raw_closes = result["indicators"]["quote"][0]["close"]
    closes = [c for c in raw_closes if c is not None]
    live = result.get("meta", {}).get("regularMarketPrice")
    if len(closes) < 20:
        raise ValueError(f"{ticker}: only {len(closes)} closes returned")
    return closes, live


def sma(closes, n):
    return sum(closes[-n:]) / n


# ----------------------------- state ------------------------------------ #

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


# --------------------------- notifications ------------------------------ #

def notify_ntfy(title: str, body: str, priority: str = "high"):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    req = urllib.request.Request(
        "https://ntfy.sh/" + urllib.parse.quote(topic),
        data=body.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": "bell,chart_with_upwards_trend",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30):
        pass
    return True


def notify_telegram(text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30):
        pass
    return True


def send_alert(title: str, body: str):
    sent = False
    for fn, arg in ((notify_ntfy, (title, body)), (notify_telegram, (f"{title}\n{body}",))):
        try:
            if fn(*arg):
                sent = True
        except Exception as exc:  # noqa: BLE001 — keep going if one channel fails
            print(f"  ! {fn.__name__} failed: {exc}", file=sys.stderr)
    if not sent:
        print("  ! No notification channel configured (set NTFY_TOPIC "
              "and/or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID).", file=sys.stderr)


# ------------------------------ main ------------------------------------ #

def check_ticker(ticker: str, state: dict, alert_on_drop: bool):
    closes, live = fetch_yahoo(ticker)
    price = live if live is not None else closes[-1]
    s5, s10, s20 = sma(closes, 5), sma(closes, 10), sma(closes, 20)
    above_all = price > s5 and price > s10 and price > s20

    prev = state.get(ticker, {}).get("above_all")  # True / False / None (first run)
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    summary = (f"price ${price:.2f} | SMA5 ${s5:.2f} | "
               f"SMA10 ${s10:.2f} | SMA20 ${s20:.2f}")
    print(f"{ticker}: {summary} -> above_all={above_all} (prev={prev})")

    if above_all and prev is not True:
        send_alert(
            f"🔔 {ticker} above SMA 5/10/20",
            f"{ticker} ${price:.2f} crossed above all three moving averages.\n"
            f"SMA5 ${s5:.2f} · SMA10 ${s10:.2f} · SMA20 ${s20:.2f}\n{now_utc}",
        )
    elif alert_on_drop and not above_all and prev is True:
        send_alert(
            f"{ticker} dropped below an SMA",
            f"{ticker} ${price:.2f} is no longer above all three averages.\n"
            f"SMA5 ${s5:.2f} · SMA10 ${s10:.2f} · SMA20 ${s20:.2f}\n{now_utc}",
        )

    state[ticker] = {
        "above_all": above_all,
        "price": round(price, 4),
        "sma5": round(s5, 4),
        "sma10": round(s10, 4),
        "sma20": round(s20, 4),
        "checked_at": now_utc,
    }


def main():
    tickers = [t.strip().upper() for t in
               os.environ.get("TICKERS", "SOXL").split(",") if t.strip()]
    alert_on_drop = os.environ.get("ALERT_ON_DROP", "0") == "1"

    state = load_state()
    failures = 0
    for ticker in tickers:
        try:
            check_ticker(ticker, state, alert_on_drop)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"{ticker}: ERROR {exc}", file=sys.stderr)

    save_state(state)
    # Exit 0 even on partial failure so the workflow still commits state;
    # exit 1 only if *everything* failed (e.g. Yahoo fully unreachable).
    sys.exit(1 if failures == len(tickers) else 0)


if __name__ == "__main__":
    main()
