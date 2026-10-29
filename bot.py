#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot.py — приём заявок и автоматический разбор логов.

    python -u bot.py          # long polling
    python -u bot.py --once   # обработать очередь и выйти

Что умеет:
  /start    — оффер и кнопки
  /price    — прайс
  /sample   — как снять лог
  /status   — свободен ли слот
  /report   — пример отчёта (PDF)
  /leads    — сводка по трекеру (только владельцу)
  /id       — chat_id
  документ  — CSV разбирается сразу и приходит отчёт
  фото      — сохраняется, бот просит макроснимок чипа
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import threading
import time
import urllib.request
import uuid
from contextlib import redirect_stdout

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import decode as decoder  # noqa: E402
from tg_notify import api, load_env  # noqa: E402

INBOX = os.path.join(BASE, "inbox.jsonl")
FILES_DIR = os.path.join(BASE, "inbox_files")
PDF_SAMPLE = os.path.join(BASE, "demo", "RF_Decode_Sample.pdf")
TRACKER = os.path.join(BASE, "sales_tracker.csv")

PRICE = (
    "<b>Цены</b>\n"
    "Разбор лога — 4 900 ₽, 24 часа.\n"
    "Спецификация + декодер — 24 000 ₽, 3 дня.\n"
    "Пакет с эмулятором и тест-планом — от 49 000 ₽, 5 дней.\n"
    "White-label для сервисных компаний — от 60 000 ₽.\n\n"
    "Первый шаг всегда один: лог. Если по нему видно, что протокол не берётся — "
    "дальше не идём, платите только за разбор."
)

SAMPLE = (
    "<b>Как снять лог</b>\n"
    "1. ESP32 + nRF24L01, прошивка rf_probe (пришлю по запросу).\n"
    "2. Монитор порта 115200, дождаться <code>#RF_PROBE_READY</code>.\n"
    "3. Зажать кнопку на устройстве на 2 секунды, отпустить. Повторить для каждой.\n"
    "4. Скопировать строки <code>12345,76,A52101008C</code> в файл CSV.\n"
    "5. Прислать файл сюда.\n\n"
    "Нет платы — пришлите hex-дамп из любого сниффера или SDR."
)

STATUS = (
    "<b>Статус</b>\n"
    "Свободен. Ближайший слот — сегодня.\n"
    "Очередь: 0 заказов.\n"
    "Ответ на заявку — в течение часа."
)

START = (
    "<b>RF Decode Desk</b>\n"
    "Разбираю проприетарные 2,4 ГГц протоколы: пульты, датчики, беспроводные "
    "клавиатуры и сканеры.\n\n"
    "На выходе: карта кадра, таблица команд, Python-декодер, прошивка-эмулятор "
    "для ESP32 и тест-план приёмки.\n\n"
    "Железо не пересылается: вы снимаете лог, я разбираю."
)

PHOTO_REPLY = (
    "Фото получил.\n\n"
    "Чтобы дать вердикт, нужен макроснимок платы: маркировка на чипе и подписи "
    "возле антенны. Свет сбоку, телефон на упор, без вспышки в упор.\n\n"
    "И модель устройства — по ней видно, что внутри."
)

DOC_UNKNOWN = (
    "Файл получил. Разбираю вручную, вернусь в течение часа.\n"
    "Если это CSV с логом — пришлите именно его, разбор будет автоматическим."
)

KEYBOARD = json.dumps({
    "inline_keyboard": [
        [{"text": "Прайс", "callback_data": "price"},
         {"text": "Пример отчёта", "callback_data": "report"}],
        [{"text": "Как снять лог", "callback_data": "sample"},
         {"text": "Свободен?", "callback_data": "status"}],
    ]
}, ensure_ascii=False)


# ---------------------------------------------------------------- helpers

def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} {message}"
    print(line, flush=True)
    try:
        with open(os.path.join(BASE, "bot.log"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def send(chat_id: int, text: str, markup: str | None = None) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if markup:
        payload["reply_markup"] = markup
    try:
        api("sendMessage", payload)
    except Exception as exc:  # noqa: BLE001
        log(f"send fail {chat_id}: {exc}")


def send_document(chat_id: int, path: str, caption: str = "") -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    boundary = "----doc" + uuid.uuid4().hex
    with open(path, "rb") as fh:
        payload = fh.read()

    body = bytearray()

    def field(name: str, value: str) -> None:
        body.extend(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8"))

    field("chat_id", str(chat_id))
    if caption:
        field("caption", caption)
        field("parse_mode", "HTML")
    body.extend(
        f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
        f'filename="{os.path.basename(path)}"\r\n'
        f"Content-Type: application/pdf\r\n\r\n".encode("utf-8"))
    body.extend(payload)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read().decode())


def download(file_id: str, prefix: str) -> str | None:
    info = api("getFile", {"file_id": file_id})
    if not info.get("ok"):
        return None
    file_path = info["result"]["file_path"]
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    os.makedirs(FILES_DIR, exist_ok=True)
    dest = os.path.join(FILES_DIR, f"{prefix}_{os.path.basename(file_path)}")
    urllib.request.urlretrieve(url, dest)
    return dest


def append_jsonl(record: dict) -> None:
    with open(INBOX, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def decode_csv(path: str) -> str:
    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            result = decoder.analyse(decoder.read_capture(path))
    except Exception as exc:  # noqa: BLE001
        return f"Не смог разобрать файл: <code>{exc}</code>"

    text = buffer.getvalue().strip()
    if not result.get("valid"):
        return (
            "<b>Разбор не сошёлся</b>\n"
            "В файле нет валидных кадров по нашей разметке. Пришлите сырой CSV "
            "из монитора порта, без правок.\n\n"
            f"<pre>{text[:1200]}</pre>"
        )

    summary = (
        f"<b>Разбор готов</b>\n"
        f"Пакетов: {result['frames']}, валидных: {result['valid']}, "
        f"нажатий: {result['bursts']}"
    )
    return summary + "\n\n<pre>" + text[:3000] + "</pre>"


def leads_digest() -> str:
    if not os.path.exists(TRACKER):
        return "Трекер пуст."
    with open(TRACKER, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    lines = [f"<b>Трекер: {len(rows)} строк</b>"]
    for row in rows[-8:]:
        lines.append(
            f"{row.get('target','')} — {row.get('status','')} — {row.get('next_step','')}")
    return "\n".join(lines)


# ---------------------------------------------------------------- handlers

def handle_document(message: dict, update_id: int) -> None:
    chat_id = message["chat"]["id"]
    document = message["document"]
    name = document.get("file_name", "file")
    saved = download(document["file_id"], str(update_id))
    if not saved:
        send(chat_id, "Файл не скачался, пришлите ещё раз.")
        return

    log(f"doc {name} -> {saved}")
    if name.lower().endswith(".csv"):
        report = decode_csv(saved)
        send(chat_id, report)
        owner = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
        if owner and owner != chat_id:
            send(owner, f"Клиент прислал лог: <code>{name}</code>")
        return

    send(chat_id, DOC_UNKNOWN)


def handle_message(message: dict, update_id: int) -> None:
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    if not chat_id:
        return

    text = (message.get("text") or "").strip()
    document = message.get("document")
    photos = message.get("photo")

    append_jsonl({
        "update_id": update_id,
        "ts": message.get("date"),
        "chat_id": chat_id,
        "username": chat.get("username"),
        "name": " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])),
        "text": text,
        "has_document": bool(document),
        "has_photo": bool(photos),
    })

    if text.startswith("/"):
        command = text.split()[0].lower().split("@")[0]
        if command in ("/start", "/help", "/menu"):
            send(chat_id, START, KEYBOARD)
        elif command == "/price":
            send(chat_id, PRICE, KEYBOARD)
        elif command == "/sample":
            send(chat_id, SAMPLE)
        elif command == "/status":
            send(chat_id, STATUS)
        elif command == "/report":
            if os.path.exists(PDF_SAMPLE):
                send_document(chat_id, PDF_SAMPLE, "Пример отчёта")
            else:
                send(chat_id, "Файл отчёта не найден.")
        elif command == "/id":
            send(chat_id, f"chat_id: <code>{chat_id}</code>")
        elif command == "/leads":
            owner = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
            if chat_id == owner:
                send(chat_id, leads_digest())
            else:
                send(chat_id, "Команда только для владельца.")
        else:
            send(chat_id, "Не знаю такую команду. /start")
        return

    if document:
        # тяжёлая обработка — в отдельном потоке, чтобы бот не замирал
        threading.Thread(
            target=handle_document, args=(message, update_id), daemon=True).start()
        return

    if photos:
        download(photos[-1]["file_id"], str(update_id))
        send(chat_id, PHOTO_REPLY)
        return

    send(chat_id,
         "Принял. Ответ в течение часа.\n"
         "Если есть лог — пришлите CSV, разбор начну сразу.")


def handle_callback(callback: dict) -> None:
    data = callback.get("data", "")
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    callback_id = callback.get("id")
    if callback_id:
        try:
            api("answerCallbackQuery", {"callback_query_id": callback_id})
        except Exception:  # noqa: BLE001
            pass
    if not chat_id:
        return

    if data == "price":
        send(chat_id, PRICE)
    elif data == "sample":
        send(chat_id, SAMPLE)
    elif data == "status":
        send(chat_id, STATUS)
    elif data == "report":
        if os.path.exists(PDF_SAMPLE):
            send_document(chat_id, PDF_SAMPLE, "Пример отчёта")
    else:
        send(chat_id, "Не понял кнопку. /start")


# ---------------------------------------------------------------- loop

def main() -> int:
    load_env(os.path.join(BASE, ".env"))
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    offset = 0
    if os.path.exists(INBOX):
        with open(INBOX, encoding="utf-8") as fh:
            for line in fh:
                try:
                    offset = max(offset, int(json.loads(line)["update_id"]) + 1)
                except Exception:  # noqa: BLE001
                    pass

    log(f"bot started, offset={offset}")
    while True:
        try:
            data = api("getUpdates", {
                "offset": offset,
                "timeout": 20,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            })
        except Exception as exc:  # noqa: BLE001
            log(f"poll error: {exc}")
            time.sleep(3)
            continue

        updates = data.get("result", [])
        for update in updates:
            offset = int(update["update_id"]) + 1
            try:
                if "callback_query" in update:
                    handle_callback(update["callback_query"])
                else:
                    handle_message(
                        update.get("message") or update.get("edited_message") or {},
                        update["update_id"])
                log(f"ok {update['update_id']}")
            except Exception as exc:  # noqa: BLE001
                log(f"handle error {update['update_id']}: {exc}")
                continue

        if args.once:
            # подтверждаем смещение, чтобы Telegram не отдал эти же сообщения снова
            try:
                api("getUpdates", {"offset": offset, "timeout": 0})
            except Exception:  # noqa: BLE001
                pass
            break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
