#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wifi_report.py — превращает report.txt клиента в PDF-отчёт.

    python wifi_report.py report.txt
    python wifi_report.py report.txt --out отчёт_кафе.pdf

Разбирает вывод netsh (русский и английский), считает загрузку каналов,
находит помехи и собирает план.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ACCENT = colors.HexColor("#0f766e")
DARK = colors.HexColor("#111827")
MUTED = colors.HexColor("#6b7280")


def register_fonts() -> str:
    candidates = [
        ("DejaVu", r"C:\Windows\Fonts\DejaVuSans.ttf"),
        ("Arial", r"C:\Windows\Fonts\arial.ttf"),
    ]
    for name, path in candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont(name, path))
                return name
            except Exception:  # noqa: BLE001
                continue
    return "Helvetica"


def parse_networks(text: str) -> list[dict]:
    networks: list[dict] = []
    current: dict | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        ssid = re.match(r"SSID\s+\d+\s*:\s*(.*)$", line)
        if ssid:
            current = {"ssid": ssid.group(1).strip() or "<скрыта>",
                       "channel": None, "signal": None, "auth": ""}
            networks.append(current)
            continue

        if current is None:
            continue

        if re.search(r"Authentication|Проверка подлинности", line):
            value = line.split(":", 1)[-1].strip()
            current["auth"] = "открыта" if "Open" in value or "Открыт" in value else "защищена"

        channel = re.search(r"Channel\s*:\s*(\d+)", line) or re.search(r"Канал\s*:\s*(\d+)", line)
        if channel:
            current["channel"] = int(channel.group(1))

        signal = re.search(r"Signal\s*:\s*(\d+)%", line) or re.search(r"Сигнал\s*:\s*(\d+)%", line)
        if signal:
            current["signal"] = int(signal.group(1))

    return [n for n in networks if n["channel"] is not None]


def parse_ping(text: str) -> dict:
    stats = {"avg": None, "loss": None}
    avg = re.search(r"(?:Average|Среднее)\s*=\s*(\d+)\s*(?:ms|мс)", text)
    if avg:
        stats["avg"] = int(avg.group(1))
    loss = re.search(r"\((\d+)%\s*(?:loss|потерь)\)", text)
    if loss:
        stats["loss"] = int(loss.group(1))
    return stats


def channel_usage(networks: list[dict]) -> dict[int, int]:
    usage = {ch: 0 for ch in range(1, 14)}
    for net in networks:
        ch = net["channel"]
        if ch in usage:
            usage[ch] += 1
    return usage


def build_story(networks, ping, usage, font, source_name) -> list:
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontName=font, fontSize=20,
                        textColor=DARK, spaceAfter=2)
    sub = ParagraphStyle("sub", parent=styles["Normal"], fontName=font, fontSize=9,
                         textColor=MUTED, spaceAfter=14)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontName=font, fontSize=13,
                        textColor=ACCENT, spaceBefore=14, spaceAfter=6)
    body = ParagraphStyle("body", parent=styles["Normal"], fontName=font, fontSize=10.5,
                          leading=15, textColor=DARK)

    story = [
        Paragraph("Отчёт по Wi-Fi", h1),
        Paragraph(f"Источник: {source_name} · {datetime.now():%d.%m.%Y}", sub),
    ]

    total = len(networks)
    busy = [ch for ch, count in usage.items() if count]
    best = min(range(1, 14), key=lambda ch: sum(usage[(ch + d - 1) % 13 + 1]
                                                for d in (-1, 0, 1)))

    story.append(Paragraph("Коротко", h2))
    summary = [
        ["Сетей найдено", str(total)],
        ["Занятых каналов", f"{len(busy)} из 13"],
        ["Самый свободный канал", str(best)],
        ["Средний пинг", f"{ping['avg']} мс" if ping["avg"] is not None else "—"],
        ["Потери пакетов", f"{ping['loss']}%" if ping["loss"] is not None else "—"],
    ]
    table = Table(summary, colWidths=[70 * mm, 60 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (0, -1), MUTED),
        ("TEXTCOLOR", (1, 0), (1, -1), DARK),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor("#e5e7eb")),
    ]))
    story.append(table)

    story.append(Paragraph("Загрузка каналов 2,4 ГГц", h2))
    drawing = Drawing(400, 150)
    chart = VerticalBarChart()
    chart.x = 35
    chart.y = 25
    chart.height = 110
    chart.width = 350
    chart.data = [[usage[ch] for ch in range(1, 14)]]
    chart.categoryAxis.categoryNames = [str(ch) for ch in range(1, 14)]
    chart.categoryAxis.labels.fontName = font
    chart.categoryAxis.labels.fontSize = 8
    chart.valueAxis.labels.fontName = font
    chart.valueAxis.labels.fontSize = 8
    chart.valueAxis.valueMin = 0
    chart.valueAxis.valueMax = max(1, max(usage.values()) + 1)
    chart.bars[0].fillColor = ACCENT
    chart.bars[0].strokeColor = None
    drawing.add(chart)
    story.append(drawing)

    story.append(Paragraph("Сети", h2))
    rows = [["SSID", "Канал", "Сигнал", "Защита"]]
    for net in sorted(networks, key=lambda n: (-(n["signal"] or 0)))[:15]:
        rows.append([net["ssid"][:28], str(net["channel"]),
                     f"{net['signal']}%" if net["signal"] is not None else "—",
                     net["auth"] or "—"])
    if len(rows) == 1:
        rows.append(["данных нет", "—", "—", "—"])

    net_table = Table(rows, colWidths=[75 * mm, 20 * mm, 25 * mm, 30 * mm], hAlign="LEFT")
    net_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
        ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(net_table)

    story.append(Paragraph("Что делать", h2))
    plan = []
    if usage[best] == 0 or usage[best] == 1:
        plan.append(f"Поставить роутер на канал <b>{best}</b> — там сейчас почти пусто.")
    if ping["loss"] and ping["loss"] >= 3:
        plan.append("Есть потери пакетов: проверить кабели и расстояние до роутера.")
    if ping["avg"] and ping["avg"] > 60:
        plan.append("Высокий пинг: проверить, не качают ли что-то в фоне и не забит ли канал.")
    if total >= 10:
        plan.append("Сетей много: включить 5 ГГц для устройств, которым нужна скорость.")
    if not plan:
        plan.append("Критичных проблем нет. Повторить замер через месяц для сравнения.")

    for index, item in enumerate(plan, 1):
        story.append(Paragraph(f"{index}. {item}", body))

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "Отчёт описывает радиообстановку по данным, которые клиент собрал сам. "
        "Мы не подключались к сети и ничего не меняли.", sub))

    return story


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", nargs="?", default="report.txt")
    parser.add_argument("--out")
    args = parser.parse_args()

    if not os.path.exists(args.source):
        print(f"нет файла {args.source} — сначала запусти wifi_gather.py")
        return 1

    with open(args.source, encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    networks = parse_networks(text)
    ping = parse_ping(text)
    usage = channel_usage(networks)

    font = register_fonts()
    out_path = args.out or f"wifi_report_{datetime.now():%Y%m%d_%H%M}.pdf"

    doc = SimpleDocTemplate(out_path, pagesize=A4,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title="Отчёт по Wi-Fi")
    story = build_story(networks, ping, usage, font, os.path.basename(args.source))
    doc.build(story)

    print(f"сетей: {len(networks)}")
    print(f"пинг: {ping['avg']} мс, потери {ping['loss']}%")
    print(f"отчёт: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
