#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
与 Apple TV 做 Companion 配对，产出 keys/atv_credentials.json。

PIN 由 Apple TV 显示在屏幕上，需要人工读取。

用法
----
交互式（推荐人工使用）：
    python tools/pair_atv.py --host 192.168.1.10

非交互（把 PIN 写进文件，脚本轮询等待；便于自动化）：
    python tools/pair_atv.py --host 192.168.1.10 --pin-file /tmp/pin.txt

⚠️ pyatv 0.18 的配对是两段式：pairing.pin(x) 然后 await pairing.finish()。
   老写法 await pairing.finish(pin) 会报 TypeError。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import pyatv
from pyatv.const import Protocol

BASE = Path(__file__).resolve().parent.parent


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="", help="Apple TV 的 IP；留空则全网段 mDNS 扫描")
    ap.add_argument("--out", default=str(BASE / "keys" / "atv_credentials.json"))
    ap.add_argument("--pin-file", default="", help="非交互模式：轮询该文件读取 PIN")
    ap.add_argument("--timeout", type=int, default=180, help="等待 PIN 的秒数")
    args = ap.parse_args()

    loop = asyncio.get_running_loop()
    print(f"[1/4] 扫描 Apple TV（host={args.host or '自动'}）...")
    atvs = await pyatv.scan(loop, hosts=[args.host] if args.host else None, timeout=5)
    if not atvs:
        raise SystemExit("没有扫描到任何设备。确认 ATV 已开机、与本机同网段。")

    print("      发现以下设备：")
    for i, c in enumerate(atvs):
        print(f"        [{i}] {c.name}  {c.identifier}  {c.address}")
    conf = atvs[0] if len(atvs) == 1 else atvs[
        int(input("      选择设备编号: ").strip() or "0")
    ]

    print(f"[2/4] 开始与「{conf.name}」配对，PIN 会显示在电视屏幕上 ...")
    pairing = await pyatv.pair(conf, Protocol.Companion, loop)
    await pairing.begin()

    if args.pin_file:
        pin = ""
        for _ in range(args.timeout * 2):
            p = Path(args.pin_file)
            if p.exists():
                pin = p.read_text(encoding="utf-8").strip()
                if pin:
                    break
            await asyncio.sleep(0.5)
        if not pin:
            await pairing.close()
            raise SystemExit("等待 PIN 超时")
    else:
        pin = input("      请输入电视上显示的 PIN: ").strip()

    print("[3/4] 提交 PIN ...")
    pairing.pin(pin)
    await pairing.finish()
    ok = pairing.has_paired
    creds = pairing.service.credentials
    await pairing.close()

    if not ok or not creds:
        raise SystemExit(f"配对失败（has_paired={ok}）")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "name": conf.name,
        "identifier": conf.identifier,
        "address": str(conf.address),
        "companion_credentials": creds,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[4/4] ✅ 配对成功，凭据已写入 {out}")
    print("      注意：该文件含可控制你 Apple TV 的凭据，请勿外传。")


if __name__ == "__main__":
    asyncio.run(main())
