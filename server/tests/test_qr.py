#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QR 编码器交叉校验：把 app.py 的纯标准库实现与参考库 qrcode（pip）逐模块比对。

运行（需要装了 qrcode 库的 Python，仅作测试预言机，运行时不依赖）：
    /path/to/venv/bin/python tests/test_qr.py

校验内容：
- 格式信息位（ECC M, mask 0）与版本信息位（V7）等于公开常数；
- 多个载荷在相同 version + mask 下，矩阵与参考库逐格一致；
- 参考库自动选的 mask 与自研选 mask 打印对比（仅提示，不作硬断言）；
- qr_png 输出为合法 PNG（魔数 + IHDR 尺寸）。
"""
import importlib.util
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ORDERS_DB_PATH", "/tmp/qr-test-orders.db")

spec = importlib.util.spec_from_file_location(
    "payapp", os.path.join(os.path.dirname(__file__), "..", "app.py")
)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

import qrcode
from qrcode.constants import ERROR_CORRECT_M
from qrcode.util import MODE_8BIT_BYTE, QRData

failures = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + extra) if extra else ""))
    if not cond:
        failures.append(name)


# 格式信息：ECC M / mask 0 时数据位为 0，BCH 余数亦 0，结果即掩码常数 0x5412
# 本身；权威校验是下方与参考库的逐格矩阵比对（格式区包含在内）。
check("format bits M/mask0", app._qr_format_bits(0) == 0b101010000010010,
      bin(app._qr_format_bits(0)))
check("version bits V7", app._qr_version_bits(7) == 0b000111110010010100,
      bin(app._qr_version_bits(7)))
check("version bits V10", app._qr_version_bits(10) == 0b001010010011010011,
      bin(app._qr_version_bits(10)))

PAYLOADS = [
    "HELLO WORLD",
    "weixin://wxpay/bizpayurl?pr=AbCdEfGh1234",
    "https://888ai.me/",
    "A" * 60,
    "Order AT20261122220524057043 amount 399.00 CNY",
    "x" * 120,
    "y" * 200,
]

for text in PAYLOADS:
    version, mask, matrix = app.qr_encode(text)
    ref = qrcode.QRCode(
        version=version,
        error_correction=ERROR_CORRECT_M,
        mask_pattern=mask,
        box_size=1,
        border=0,
    )
    ref.add_data(QRData(text, mode=MODE_8BIT_BYTE))
    ref.make(fit=False)
    ref_matrix = [[1 if cell else 0 for cell in row] for row in ref.modules]
    same = ref_matrix == [list(row) for row in matrix]
    check("matrix v%d mask%d len=%d" % (version, mask, len(text)), same)
    # 参考库自动选版/选掩码（软对比）
    auto = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=1, border=0)
    auto.add_data(QRData(text, mode=MODE_8BIT_BYTE))
    auto.make(fit=True)
    print(
        "     info: mine v%d/mask%d vs lib v%s/mask%s"
        % (version, mask, auto.version, auto.mask_pattern)
    )

png = app.qr_png("weixin://wxpay/bizpayurl?pr=AbCdEfGh1234")
check("png magic", png[:8] == b"\x89PNG\r\n\x1a\n")
w, h = struct.unpack(">II", png[16:24])
check("png square", w == h and w > 100, "%dx%d" % (w, h))

try:
    app.qr_encode("z" * 300)
    check("too long raises", False)
except ValueError:
    check("too long raises", True)

print("\n%d failures" % len(failures))
sys.exit(1 if failures else 0)
