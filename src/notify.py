"""Telegram notification delivery. See README for bot setup."""
from __future__ import annotations

import html
import os
import time

import requests

MAX_MESSAGE_LEN = 3500  # Telegram's hard limit is 4096; leave headroom for markup


def _send(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    resp = requests.post(
        url,
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    resp.raise_for_status()


def send(text: str) -> None:
    """Send, splitting on message boundaries if too long for one Telegram message."""
    if len(text) <= MAX_MESSAGE_LEN:
        _send(text)
        return

    # Split on blank lines so we never cut a job entry in half.
    blocks = text.split("\n\n")
    buf = ""
    for block in blocks:
        if len(buf) + len(block) + 2 > MAX_MESSAGE_LEN:
            if buf:
                _send(buf)
                time.sleep(0.4)  # Telegram rate-limits ~30 msg/sec; be gentle
            buf = block
        else:
            buf = f"{buf}\n\n{block}" if buf else block
    if buf:
        _send(buf)


def format_jobs(jobs: list) -> str:
    """Build the alert message. Newest/most relevant first is the caller's job."""
    e = html.escape
    header = f"\U0001F6A8 <b>{len(jobs)} new posting{'s' if len(jobs) != 1 else ''}</b>"
    blocks = [header]

    for job in jobs:
        parts = [f"<b>{e(job.company)}</b>"]
        parts.append(f"{e(job.title)}")
        if job.location:
            parts.append(f"\U0001F4CD {e(job.location)}")
        if job.flags:
            parts.append(" ".join(job.flags))
        parts.append(f'<a href="{e(job.url)}">Apply</a>')
        blocks.append("\n".join(parts))

    return "\n\n".join(blocks)


def format_health_alert(warnings: list[str]) -> str:
    e = html.escape
    lines = ["\u26A0\uFE0F <b>Scraper health warnings</b>", ""]
    lines += [f"\u2022 {e(w)}" for w in warnings]
    lines.append("")
    lines.append("<i>Fix these — a silently broken scraper means missed postings.</i>")
    return "\n".join(lines)
