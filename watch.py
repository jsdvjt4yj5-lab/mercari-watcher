#!/usr/bin/env python3
"""
Mercari auction watcher.

Fetches a Mercari item page, extracts the current price and auction end
time, compares against the last known state (stored in state.json), and
sends a Telegram message if the price has changed. Also sends a one-time
"closing soon" alert once the auction is within CLOSING_ALERT_MINUTES of
its end time.

Environment variables required:
  ITEM_URL              - the Mercari item URL to watch
  TELEGRAM_BOT_TOKEN     - bot token from @BotFather
  TELEGRAM_CHAT_ID       - your chat id (from @userinfobot or getUpdates)

Optional:
  CLOSING_ALERT_MINUTES  - minutes before close to alert (default: 30)
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

STATE_FILE = Path(__file__).parent / "state.json"

ITEM_URL = os.environ["ITEM_URL"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# minutes before auction close to send a "closing soon" alert
CLOSING_ALERT_MINUTES = int(os.environ.get("CLOSING_ALERT_MINUTES", "30"))

JST = timezone(timedelta(hours=9))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def fetch_page(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def parse_price(html: str) -> int | None:
    # og/product meta tag is the most reliable source: <meta property="product:price:amount" content="700">
    m = re.search(r'product:price:amount["\']\s*content=["\'](\d+)', html)
    if m:
        return int(m.group(1))
    # fallback: og:price:amount variant
    m = re.search(r'"price"\s*:\s*"?(\d+)"?', html)
    if m:
        return int(m.group(1))
    # fallback: visible "現在 ¥700" text on the page
    m = re.search(r"現在\s*¥\s*([\d,]+)", html)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def parse_end_time(html: str) -> str | None:
    m = re.search(r"終了予定時刻[^\d]*(\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2})", html)
    if m:
        return m.group(1)
    return None


def parse_end_datetime(end_time_str: str | None) -> datetime | None:
    """Parse '2026年9月10日 11:57' (JST, as shown on Mercari) into an aware datetime."""
    if not end_time_str:
        return None
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})", end_time_str)
    if not m:
        return None
    year, month, day, hour, minute = (int(g) for g in m.groups())
    return datetime(year, month, day, hour, minute, tzinfo=JST)


def parse_title(html: str) -> str:
    m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
    if m:
        return m.group(1)
    return ITEM_URL


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def send_telegram(message: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"},
        timeout=20,
    )
    resp.raise_for_status()


def main() -> None:
    html = fetch_page(ITEM_URL)

    price = parse_price(html)
    end_time = parse_end_time(html)
    title = parse_title(html)

    if price is None:
        print("Could not parse price from page — item may be sold/removed.")
        state = load_state()
        if not state.get("gone_alert_sent"):
            send_telegram(
                f"⚠️ Could not find a price on the watched item.\n"
                f"It may have sold or been removed.\n{ITEM_URL}"
            )
            state["gone_alert_sent"] = True
            save_state(state)
        sys.exit(0)

    state = load_state()
    last_price = state.get("price")

    print(f"Current price: ¥{price} | last known: {last_price} | end: {end_time}")

    if last_price is None:
        # first run — just record it, no alert
        send_telegram(
            f"👀 Now watching:\n<b>{title}</b>\nCurrent price: ¥{price}\n"
            f"Ends: {end_time or 'unknown'}\n{ITEM_URL}"
        )
    elif price != last_price:
        direction = "📈 went UP" if price > last_price else "📉 went DOWN"
        send_telegram(
            f"{direction}: ¥{last_price} → ¥{price}\n"
            f"<b>{title}</b>\n"
            f"Ends: {end_time or 'unknown'}\n{ITEM_URL}"
        )

    # --- closing-soon alert ---
    # reset the "already alerted" flag if the end time changed (e.g. relisted/extended)
    closing_alert_sent = state.get("closing_alert_sent", False)
    if end_time != state.get("end_time"):
        closing_alert_sent = False

    end_dt = parse_end_datetime(end_time)
    if end_dt and not closing_alert_sent:
        remaining = end_dt - datetime.now(timezone.utc)
        if timedelta(0) < remaining <= timedelta(minutes=CLOSING_ALERT_MINUTES):
            mins_left = int(remaining.total_seconds() // 60)
            send_telegram(
                f"⏰ Closing soon! ~{mins_left} min left\n"
                f"<b>{title}</b>\nCurrent price: ¥{price}\n"
                f"Ends: {end_time}\n{ITEM_URL}"
            )
            closing_alert_sent = True

    state.update(
        {
            "price": price,
            "end_time": end_time,
            "title": title,
            "gone_alert_sent": False,
            "closing_alert_sent": closing_alert_sent,
        }
    )
    save_state(state)


if __name__ == "__main__":
    main()
