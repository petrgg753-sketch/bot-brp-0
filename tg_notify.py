#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tg_notify.py — отправка сообщений в Telegram через Bot API (stdlib, без зависимостей).

Настройка: файл .env в этой папке
    TELEGRAM_BOT_TOKEN=123456:AA...
    TELEGRAM_CHAT_ID=123456789

Использование:
    python tg_notify.py --text "привет"
    python tg_notify.py --file leads.csv --top 5
    python tg_notify.py --text "лид" --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def api(method: str, payload: dict) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан (см. .env.example)")
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Telegram {method} HTTP {exc.code}: {body}") from exc


def send(text: str, chat_id: str | None = None, dry_run: bool = False) -> None:
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        raise SystemExit("TELEGRAM_CHAT_ID не задан (см. .env.example)")
    if dry_run:
        print(f"[dry-run] -> {chat_id}\n{text}")
        return
    result = api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    })
    if not result.get("ok"):
        raise SystemExit(f"Telegram error: {result}")


def digest(csv_path: str, top: int) -> str:
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))[:top]
    lines = [f"<b>Лиды: {len(rows)}</b>"]
    for row in rows:
        lines.append(
            f"\n<b>{row.get('score', '?')}</b> | {row.get('source', '')}\n"
            f"{row.get('title', '')[:120]}\n{row.get('url', '')}")
    return "\n".join(lines)


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--text")
    parser.add_argument("--file")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--chat-id")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.file:
        text = digest(args.file, args.top)
    elif args.text:
        text = args.text
    else:
        text = sys.stdin.read().strip()
    if not text:
        raise SystemExit("Пустой текст")
    send(text, args.chat_id, args.dry_run)
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
