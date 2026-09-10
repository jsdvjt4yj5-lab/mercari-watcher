#!/usr/bin/env python3
"""
Listens for a /refresh command sent to the Telegram bot and, if found,
triggers an immediate check of the watched item (reusing watch.py's logic).

Runs on a short schedule (e.g. every 5 minutes) separately from the main
watch.py schedule, so you don't have to wait for the full 15-min cycle to
get an on-demand status update.
"""

import json
import os

import requests

from watch import run_check, load_state, save_state, BOT_TOKEN

REFRESH_COMMANDS = {"/refresh", "/status", "/check"}


def get_updates(offset: int | None) -> list[dict]:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json().get("result", [])


def main() -> None:
    state = load_state()
    last_offset = state.get("telegram_offset")

    updates = get_updates(last_offset)
    if not updates:
        print("No new Telegram updates.")
        return

    triggered = False
    highest_update_id = last_offset or 0

    for update in updates:
        highest_update_id = max(highest_update_id, update["update_id"])
        text = (update.get("message", {}).get("text") or "").strip().lower()
        if text in REFRESH_COMMANDS:
            triggered = True

    # mark all seen updates as processed, regardless of whether one matched,
    # so we never reprocess the same message twice
    state["telegram_offset"] = highest_update_id + 1
    save_state(state)

    if triggered:
        print("Refresh command detected — running check now.")
        run_check(force_status=True)
    else:
        print("No refresh command in this batch.")


if __name__ == "__main__":
    main()
