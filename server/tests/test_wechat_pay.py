#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信支付通道选择自测（本地，进程内调用 handle_wechat_pay，不连真实支付接口）。

假网关：在 app.wechat_api_post 这一层替换，记录被调用的网关 path/payload，
并返回用测试密钥真实 RSA 签名的应答（应答验签路径与生产一致）。
商户参数、订单库均为临时假数据。

覆盖 POST /api/pay/wechat 的 pay_type 逻辑：
- 手机 UA + pay_type=native → 强制 Native，返回 pay_type=native 与 code_url
- 手机 UA 不带 pay_type     → 仍走 H5 路径，返回 h5_url
- 桌面 UA 不带 pay_type     → Native（行为不变）
- 手机 UA + pay_type 非法值 → 回落默认（H5）
- 桌面 UA + pay_type=h5     → 强制 H5

运行（需要 cryptography 做签名/验签）：
    /path/to/venv/bin/python tests/test_wechat_pay.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

SERVER_DIR = os.path.join(os.path.dirname(__file__), "..")
KEY_DIR = "/tmp/wechat-pay-test-keys"
DB_FILE = "/tmp/wechat-pay-test.db"

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
             "Mobile/15E148 Safari/604.1")
DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")

results = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if extra else ""))
    results.append((name, bool(cond)))


def gen_keys():
    os.makedirs(KEY_DIR, exist_ok=True)
    pairs = ["wxmch", "wxpay"]
    for name in pairs:
        priv = os.path.join(KEY_DIR, name + "_priv.pem")
        pub = os.path.join(KEY_DIR, name + "_pub.pem")
        if not os.path.exists(priv):
            subprocess.run(
                ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
                 "rsa_keygen_bits:2048", "-out", priv],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["openssl", "rsa", "-in", priv, "-pubout", "-out", pub],
                check=True, capture_output=True,
            )
    return {n: os.path.join(KEY_DIR, n + "_priv.pem") for n in pairs}, {
        n: os.path.join(KEY_DIR, n + "_pub.pem") for n in pairs
    }


def main():
    privs, pubs = gen_keys()
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)
    os.environ.update(
        {
            "ORDERS_DB_PATH": DB_FILE,
            "WECHAT_MCHID": "1900000101",
            "WECHAT_APPID": "wx0000000000000000",
            "WECHAT_CERT_SERIAL": "TESTSERIAL123",
            "WECHAT_PRIVATE_KEY_PATH": privs["wxmch"],
            "WECHAT_API_V3_KEY": "0123456789abcdef0123456789abcdef",
            "WECHAT_PUB_KEY_ID": "PUB_KEY_ID_TEST",
            "WECHAT_PUB_KEY_PATH": pubs["wxpay"],
            "WECHAT_NOTIFY_URL": "https://888ai.me/api/pay/notify/wechat",
        }
    )

    spec = importlib.util.spec_from_file_location(
        "pay_app", os.path.join(SERVER_DIR, "app.py")
    )
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    app.db_init()

    # 建一笔待付订单（金额以订单为准，legacy_basic = 39900 分）
    status, _ct, body = app.handle_create_order(
        {
            "tier": "legacy_basic",
            "phone": "13955556666",
            "recipient": "王五",
            "province": "浙江省",
            "city": "台州市",
            "district": "仙居县",
            "address_detail": "某街道 2 号",
        }
    )
    order = json.loads(body.decode("utf-8"))
    check("create order 201", status == 201 and order.get("order_id"), status)
    order_id = order["order_id"]

    # ---- 假微信网关：记录调用、按 path 返回对应字段、应答带真实签名 ----
    gateway_calls = []

    def fake_wechat_api_post(cfg, path, payload):
        gateway_calls.append((path, payload))
        if path.endswith("/native"):
            result = {"code_url": "weixin://wxpay/bizpayurl?pr=TESTCODE123"}
        else:
            result = {
                "h5_url": "https://wx.tenpay.com/cgi-bin/mmpayweb-bin/checkmweb?prepay_id=TEST"
            }
        resp_body = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        ts = str(int(time.time()))
        nonce = "testnonce0123456789"
        sig = app.rsa2_sign("%s\n%s\n%s\n" % (ts, nonce, resp_body), privs["wxpay"])
        headers = {
            "wechatpay-timestamp": ts,
            "wechatpay-nonce": nonce,
            "wechatpay-serial": "PUB_KEY_ID_TEST",
            "wechatpay-signature": sig,
        }
        return 200, headers, resp_body.encode("utf-8")

    app.wechat_api_post = fake_wechat_api_post

    def pay(data, ua):
        gateway_calls.clear()
        status, _ct, body = app.handle_wechat_pay(data, ua, "1.2.3.4")
        return status, json.loads(body.decode("utf-8"))

    # 1) 手机 UA + pay_type=native → 强制 Native
    s, j = pay({"order_id": order_id, "pay_type": "native"}, MOBILE_UA)
    check("mobile+native returns native code_url",
          s == 200 and j.get("pay_type") == "native"
          and j.get("code_url", "").startswith("weixin://"), j)
    check("mobile+native hits native gateway without scene_info",
          gateway_calls and gateway_calls[0][0] == "/v3/pay/transactions/native"
          and "scene_info" not in gateway_calls[0][1], gateway_calls)

    # 2) 手机 UA 不带 pay_type → 默认 H5
    s, j = pay({"order_id": order_id}, MOBILE_UA)
    check("mobile default returns h5_url",
          s == 200 and j.get("pay_type") == "h5" and bool(j.get("h5_url")), j)
    check("mobile default hits h5 gateway with scene_info",
          gateway_calls and gateway_calls[0][0] == "/v3/pay/transactions/h5"
          and "scene_info" in gateway_calls[0][1], gateway_calls)

    # 3) 桌面 UA 不带 pay_type → Native 不变
    s, j = pay({"order_id": order_id}, DESKTOP_UA)
    check("desktop default returns native code_url",
          s == 200 and j.get("pay_type") == "native"
          and j.get("code_url", "").startswith("weixin://"), j)
    check("desktop default hits native gateway",
          gateway_calls and gateway_calls[0][0] == "/v3/pay/transactions/native",
          gateway_calls)

    # 4) 手机 UA + pay_type 非法值 → 回落默认（H5）
    s, j = pay({"order_id": order_id, "pay_type": "scan"}, MOBILE_UA)
    check("mobile+invalid pay_type falls back to h5",
          s == 200 and j.get("pay_type") == "h5" and bool(j.get("h5_url")), j)
    check("mobile+invalid pay_type hits h5 gateway",
          gateway_calls and gateway_calls[0][0] == "/v3/pay/transactions/h5",
          gateway_calls)

    # 5) 桌面 UA + pay_type=h5 → 强制 H5
    s, j = pay({"order_id": order_id, "pay_type": "h5"}, DESKTOP_UA)
    check("desktop+h5 returns h5_url",
          s == 200 and j.get("pay_type") == "h5" and bool(j.get("h5_url")), j)
    check("desktop+h5 hits h5 gateway",
          gateway_calls and gateway_calls[0][0] == "/v3/pay/transactions/h5",
          gateway_calls)

    failed = [n for n, ok in results if not ok]
    print("\n==== %d/%d passed ====" % (len(results) - len(failed), len(results)))
    if failed:
        print("FAILED:", failed)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
