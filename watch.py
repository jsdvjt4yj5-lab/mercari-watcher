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
    """Try several strategies since the raw HTML from a plain HTTP request
    doesn't always match what a browser renders."""
    # Strategy 1: find the label, then look for a date pattern in the text
    # that follows it (don't require "no digits" between them — there may
    # be other digits, like an id, sitting between label and value).
    idx = html.find("終了予定時刻")
    if idx != -1:
        window = html[idx : idx + 300]
        m = re.search(r"(\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2})", window)
        if m:
            return m.group(1)

    # Strategy 2: same date pattern, anywhere on the page (last resort —
    # only used if strategy 1 fails to find the labelled version).
    m = re.search(r"(\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2})", html)
    if m:
        return m.group(1)

    return None


def parse_end_time_iso(html: str) -> datetime | None:
    """Fallback: look for an ISO-8601 timestamp under a plausible key name
    in any embedded JSON (e.g. Next.js hydration data), for pages where
    the Japanese-label text isn't present in the raw HTML."""
    m = re.search(
        r'"(?:auctionEndAt|endAt|closeAt|closedAt|expireAt|expiresAt|biddingEndAt|saleEndAt)"'
        r'\s*:\s*"([0-9T:\-\+\.Z]+)"',
        html,
    )
    if not m:
        return None
    raw = m.group(1).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
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


def format_remaining(remaining: timedelta) -> str:
    """Format a timedelta like '1d 6h 17m' (matching the site's DD:HH:MM style)."""
    total_seconds = int(remaining.total_seconds())
    if total_seconds <= 0:
        return "ended"
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts) + " left"


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

    end_dt = parse_end_datetime(end_time)
    if end_dt is None:
        # label-based parse failed — try the JSON/ISO fallback
        end_dt = parse_end_time_iso(html)
        if end_dt is not None:
            # produce a matching display string in JST for consistency
            end_time = end_dt.astimezone(JST).strftime("%Y年%-m月%-d日 %H:%M")

    if end_dt is None:
        print("Could not find an end time on the page (neither label nor JSON match).")

    remaining = (end_dt - datetime.now(timezone.utc)) if end_dt else None
    timer_str = format_remaining(remaining) if remaining is not None else "unknown"

    print(f"Current price: ¥{price} | last known: {last_price} | end: {end_time} | {timer_str}")

    if last_price is None:
        # first run — just record it, no alert
        send_telegram(
            f"👀 Now watching:\n<b>{title}</b>\nCurrent price: ¥{price}\n"
            f"Ends: {end_time or 'unknown'} ({timer_str})\n{ITEM_URL}"
        )
    elif price != last_price:
        direction = "📈 went UP" if price > last_price else "📉 went DOWN"
        send_telegram(
            f"{direction}: ¥{last_price} → ¥{price}\n"
            f"<b>{title}</b>\n"
            f"Ends: {end_time or 'unknown'} ({timer_str})\n{ITEM_URL}"
        )

    # --- closing-soon alert ---
    # reset the "already alerted" flag if the end time changed (e.g. relisted/extended)
    closing_alert_sent = state.get("closing_alert_sent", False)
    if end_time != state.get("end_time"):
        closing_alert_sent = False

    if remaining is not None and not closing_alert_sent:
        if timedelta(0) < remaining <= timedelta(minutes=CLOSING_ALERT_MINUTES):
            send_telegram(
                f"⏰ Closing soon! {timer_str}\n"
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
