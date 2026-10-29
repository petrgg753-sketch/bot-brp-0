#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
decode.py — разбор CSV от прошивки rf_probe (sniffer).

Формат входа: CSV с колонками ts_ms,channel,payload_hex (лишние колонки игнорируются).
Выход: карта байтов, разбивка на "нажатия", проверка CRC8, таблица команд.

    python decode.py capture.csv
    python decode.py --self-test
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
from collections import Counter

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FRAME_LEN = 5
SYNC = 0xA5
CRC_POLY = 0x07
CMDS = {1: "UP", 2: "DOWN", 3: "STOP", 4: "PAIR"}


def crc8(data: bytes, poly: int = CRC_POLY, init: int = 0x00) -> int:
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def read_capture(path: str) -> list[tuple[int, bytes]]:
    frames: list[tuple[int, bytes]] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(2048)
        fh.seek(0)
        if "payload" in sample.lower():
            reader = csv.DictReader(fh)
            for row in reader:
                payload = None
                for key, value in row.items():
                    if key and "payload" in key.lower():
                        payload = value
                        break
                if not payload:
                    continue
                try:
                    ts = int(str(row.get("ts_ms") or 0).strip() or 0)
                except ValueError:
                    ts = 0
                frames.append((ts, bytes.fromhex(payload.replace(" ", "").strip())))
        else:
            reader = csv.reader(fh)
            for row in reader:
                if len(row) < 2:
                    continue
                try:
                    ts = int(float(row[0]))
                except ValueError:
                    continue
                frames.append((ts, bytes.fromhex(row[-1].replace(" ", "").strip())))
    return frames


def split_bursts(frames, gap_ms: int = 150):
    bursts, current = [], []
    for ts, payload in frames:
        if current and ts - current[-1][0] > gap_ms:
            bursts.append(current)
            current = []
        current.append((ts, payload))
    if current:
        bursts.append(current)
    return bursts


def byte_map(frames) -> None:
    width = Counter(len(p) for _, p in frames).most_common(1)[0][0]
    rows = [p for _, p in frames if len(p) == width]
    print(f"\nБАЙТОВАЯ КАРТА (ширина {width}, пакетов {len(rows)})")
    print("  поз | уник | min  max  | тип")
    for idx in range(width):
        values = [row[idx] for row in rows]
        distinct = sorted(set(values))
        kind = "CONST" if len(distinct) == 1 else "VAR"
        print(f"   {idx:>2} | {len(distinct):>4} | {min(values):>3}  {max(values):>3}  | {kind}")


def analyse(frames) -> dict:
    if not frames:
        print("Пусто: пакетов нет.")
        return {}

    print(f"Пакетов: {len(frames)}  |  длина: {Counter(len(p) for _, p in frames)}")
    byte_map(frames)

    valid = [(ts, p) for ts, p in frames
             if len(p) == FRAME_LEN and p[0] == SYNC and crc8(p[:4]) == p[4]]
    print(f"\nВалидных кадров (sync 0x{ SYNC:02X} + CRC8): {len(valid)} из {len(frames)}")

    if not valid:
        print("CRC не сошёлся: либо другой формат, либо шум. Нужен дамп с известным кадром.")
        return {"frames": len(frames), "valid": 0}

    bursts = split_bursts(valid)
    print(f"Нажатий (бёрстов): {len(bursts)}  |  повторов в бёрсте: "
          f"{Counter(len(b) for b in bursts)}")

    print("\n  # | cmd        | seq | повторов | кадр")
    for idx, burst in enumerate(bursts, 1):
        _, payload = burst[0]
        cmd = CMDS.get(payload[2], f"0x{payload[2]:02X}")
        print(f" {idx:>2} | {cmd:<10} | {payload[3]:>3} | {len(burst):>8} | {payload.hex(' ').upper()}")

    seqs = [b[0][1][3] for b in bursts]
    deltas = [(b - a) % 256 for a, b in zip(seqs, seqs[1:])]
    print(f"\nСчётчик seq: старт {seqs[0]}, шаг {Counter(deltas).most_common(1)[0][0] if deltas else '-'}")
    return {"frames": len(frames), "valid": len(valid), "bursts": len(bursts)}


def self_test() -> int:
    rows, seq = [], 0
    base = 1000
    for cmd in (1, 2, 3, 4):
        for i in range(6):
            frame = bytes([SYNC, 0x21, cmd, seq & 0xFF, 0])
            frame = frame[:4] + bytes([crc8(frame[:4])])
            rows.append((base + i * 20, frame))
            seq += 1
        base += 600
    rows.append((5000, bytes([0x11, 0x22, 0x33, 0x44, 0x55])))  # шум
    rows.append((5040, bytes([SYNC, 0x21, 0x09, 0x00, 0x00])))  # битый CRC

    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                     encoding="utf-8", newline="") as fh:
        path = fh.name
        fh.write("ts_ms,channel,payload_hex\n")
        for ts, payload in rows:
            fh.write(f"{ts},76,{payload.hex().upper()}\n")

    try:
        print("=== SELF-TEST: синтетический захват ===")
        result = analyse(read_capture(path))
        ok = result.get("valid") == 24 and result.get("bursts") == 4
        print("\nSELF-TEST:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        os.unlink(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Разбор захвата nRF24")
    parser.add_argument("capture", nargs="?", help="CSV от прошивки rf_probe")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.capture:
        parser.print_help()
        return 2
    analyse(read_capture(args.capture))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
