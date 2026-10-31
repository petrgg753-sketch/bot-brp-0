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

# ---------------------------------------------------------------- подписка

SUBS = os.path.join(BASE, "subs.json")

SUB_PLANS = (
    "<b>Проверка Wi-Fi по подписке</b>\n\n"
    "Раз в месяц вы присылаете файл замеров — получаете отчёт: какие каналы забиты, "
    "кто мешает, что изменилось с прошлого раза.\n\n"
    "<b>2 900 ₽ в месяц.</b>\n\n"
    "Первый месяц — вводный: разовый аудит за 3 900 ₽ и месяц наблюдения в подарок.\n\n"
    "Оформление — кнопка ниже. Оплата после подтверждения, реквизиты пришлю в чат."
)

SUB_KEYBOARD = {
    "inline_keyboard": [[{"text": "Оформить подписку", "callback_data": "sub:new"}]],
}


def load_subs() -> dict:
    try:
        with open(SUBS, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def save_subs(data: dict) -> None:
    with open(SUBS, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def owner_id() -> int:
    return int(os.environ.get("TELEGRAM_CHAT_ID", "0") or 0)


def add_months(chat_id: int, months: int) -> str:
    from datetime import date, timedelta

    subs = load_subs()
    key = str(chat_id)
    entry = subs.get(key, {})
    today = date.today()
    start = today
    if entry.get("until"):
        try:
            parsed = date.fromisoformat(entry["until"])
            if parsed > today:
                start = parsed
        except Exception:  # noqa: BLE001
            pass
    until = start + timedelta(days=30 * months)
    entry["until"] = until.isoformat()
    entry.setdefault("since", today.isoformat())
    subs[key] = entry
    save_subs(subs)
    return until.isoformat()


def sub_line(chat_id: int) -> str:
    from datetime import date

    entry = load_subs().get(str(chat_id), {})
    if not entry.get("until"):
        return "подписки нет"
    try:
        days = (date.fromisoformat(entry["until"]) - date.today()).days
    except Exception:  # noqa: BLE001
        return "подписка активна"
    if days < 0:
        return f"истекла {entry['until']}"
    return f"активна до {entry['until']} ({days} дн.)"


def check_due() -> None:
    """Напоминает о скором окончании подписки, не чаще раза в день."""
    from datetime import date

    subs = load_subs()
    today = date.today().isoformat()
    changed = False

    for key, entry in subs.items():
        until = entry.get("until")
        if not until or entry.get("reminded") == today:
            continue
        try:
            days = (date.fromisoformat(until) - date.today()).days
        except Exception:  # noqa: BLE001
            continue
        if 0 <= days <= 3:
            try:
                send(int(key), f"Подписка заканчивается {until}. Продлить — /subscribe")
                if owner_id():
                    send(owner_id(), f"Подписка клиента {key} до {until} ({days} дн.)")
            except Exception:  # noqa: BLE001
                pass
            entry["reminded"] = today
            changed = True

    if changed:
        save_subs(subs)


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

    if name.lower().endswith(".txt"):
        send(chat_id, "Смотрю замеры Wi-Fi, собираю отчёт…")
        out_pdf = saved + "_report.pdf"
        try:
            import subprocess
            import sys as _sys

            subprocess.run(
                [_sys.executable, os.path.join(BASE, "wifi_report.py"), saved, "--out", out_pdf],
                capture_output=True, text=True, timeout=180, check=True,
            )
            send_document(chat_id, out_pdf, f"Готовый отчёт по Wi-Fi. Ваша подписка: {sub_line(chat_id)}")
            owner = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
            if owner and owner != chat_id:
                send(owner, f"Клиент прислал замеры Wi-Fi: <code>{name}</code>")
        except Exception as exc:  # noqa: BLE001
            log(f"wifi_report fail: {exc}")
            send(chat_id, "Не получилось разобрать файл. Нужен report.txt, который создаёт wifi_gather.py.")
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
        elif command == "/subscribe":
            send(chat_id, SUB_PLANS, SUB_KEYBOARD)
        elif command == "/mysub":
            send(chat_id, f"Ваша подписка: {sub_line(chat_id)}")
        elif command == "/subs":
            if chat_id != owner_id():
                send(chat_id, "Команда только для владельца.")
            else:
                subs = load_subs()
                lines = [f"<code>{key}</code> — {value.get('until', 'заявка')}"
                         for key, value in subs.items()]
                send(chat_id, "\n".join(lines) if lines else "Подписок пока нет.")
        elif command == "/paid":
            if chat_id != owner_id():
                send(chat_id, "Команда только для владельца.")
            elif len(text.split()) < 2:
                send(chat_id, "Формат: /paid &lt;chat_id&gt; [месяцев]")
            else:
                parts = text.split()
                try:
                    target = int(parts[1])
                    months = int(parts[2]) if len(parts) > 2 else 1
                except ValueError:
                    send(chat_id, "chat_id и число месяцев должны быть числами.")
                    return
                until = add_months(target, months)
                send(chat_id, f"Оплачено до {until}")
                send(target, f"Подписка активна до {until}. В конце месяца пришлите report.txt — верну отчёт.")
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
    elif data == "sub:new":
        from datetime import date

        subs = load_subs()
        key = str(chat_id)
        entry = subs.get(key, {})
        entry["requested"] = date.today().isoformat()
        subs[key] = entry
        save_subs(subs)

        send(chat_id, "Заявка принята. Подтвержу и пришлю реквизиты в этот чат.")
        if owner_id() and owner_id() != chat_id:
            username = message.get("chat", {}).get("username")
            who = f"@{username}" if username else str(chat_id)
            send(owner_id(), f"Заявка на подписку: {who} (<code>{chat_id}</code>)")
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
    last_due_check = 0.0
    while True:
        try:
            data = api("getUpdates", {
                "offset": offset,
                "timeout": 20,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            })
        except Exception as exc:  # noqa: BLE001
            log(f"poll error: {exc}")
            if "Conflict" in str(exc) or "409" in str(exc):
                # второй поллер уже слушает — это норма, выходим тихо
                log("другой экземпляр бота активен, пропускаю запуск")
                return 0
            if args.once:
                return 1
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

        if time.time() - last_due_check > 600:
            last_due_check = time.time()
            try:
                check_due()
            except Exception as exc:  # noqa: BLE001
                log(f"check_due error: {exc}")

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
