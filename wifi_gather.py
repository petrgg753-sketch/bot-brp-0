#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wifi_gather.py — собрать данные о Wi-Fi на компьютере клиента.

Клиент запускает файл, получает report.txt и присылает его нам.
Ничего не устанавливает, ничего не меняет — только читает.

    python wifi_gather.py
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def run(args: list[str]) -> str:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=60,
                              encoding="cp866", errors="replace")
        return (done.stdout or "") + (done.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return f"ОШИБКА: {exc}"


def main() -> int:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [
        "ОТЧЁТ О WI-FI",
        f"Дата: {stamp}",
        f"Компьютер: {platform.node()}",
        f"Система: {platform.system()} {platform.release()}",
        "",
        "=== 1. СЕТИ ВОКРУГ ===",
        run(["netsh", "wlan", "show", "networks", "mode=bssid"]),
        "",
        "=== 2. ТЕКУЩЕЕ ПОДКЛЮЧЕНИЕ ===",
        run(["netsh", "wlan", "show", "interfaces"]),
        "",
        "=== 3. ПРОВЕРКА СВЯЗИ ===",
        run(["ping", "-n", "8", "8.8.8.8"]),
        "",
        "=== 4. МАРШРУТ ===",
        run(["tracert", "-h", "5", "-w", "500", "8.8.8.8"]),
    ]

    text = "\n".join(str(p) for p in parts)
    with open("report.txt", "w", encoding="utf-8") as fh:
        fh.write(text)

    print("Готово. Файл report.txt лежит рядом со скриптом.")
    print("Пришлите его в ответ на письмо — разберём и вернёмся с отчётом.")
    if "Служба геолокации" in text or "location" in text.lower():
        print()
        print("Если список сетей пустой — включите геолокацию:")
        print("Параметры → Конфиденциальность → Геолокация → Вкл.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
