#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""商城 v1 全链路自测（本地，临时 SQLite + 假商户参数，不连任何真实支付接口）。

运行（测试脚本本身需要 cryptography 构造模拟回调签名）：
    /path/to/venv/bin/python tests/test_store.py

Server A：venv Python（Flask 模式）+ 假支付宝/微信参数 + ADMIN_TOKEN
Server B：系统 python3（标准库模式）+ 无任何商户参数 / 无 ADMIN_TOKEN
"""
import base64
import http.cookiejar
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SERVER_DIR = os.path.join(os.path.dirname(__file__), "..")
KEY_DIR = "/tmp/store-test-keys"
PORT_A, PORT_B = 19095, 19096
ADMIN_TOKEN = "test-admin-token-xyz"
BUYER_PHONE = "13812348000"
GUEST_PHONE = "13955556666"

results = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if extra else ""))
    results.append((name, bool(cond)))


def opener(with_cookies=False):
    if with_cookies:
        cj = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    return urllib.request.build_opener()


def call(op, method, url, json_body=None, form=None, headers=None):
    data = None
    hdrs = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with op.open(req, timeout=10) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def call_json(op, method, url, json_body=None, headers=None):
    status, _h, body = call(op, method, url, json_body=json_body, headers=headers)
    try:
        return status, json.loads(body.decode("utf-8"))
    except Exception:
        return status, {"_raw": body.decode("utf-8", "replace")}


def wait_health(port, proc):
    op = opener()
    for _ in range(60):
        if proc.poll() is not None:
            raise RuntimeError("server exited early rc=%s" % proc.returncode)
        try:
            status, _ = call_json(op, "GET", "http://127.0.0.1:%d/api/health" % port)
            if status == 200:
                return
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError("server on %d did not come up" % port)


def gen_keys():
    os.makedirs(KEY_DIR, exist_ok=True)
    pairs = ["app", "ali", "wxmch", "wxpay"]
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


def sign_rsa2(content, priv_path):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    with open(priv_path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    sig = key.sign(content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode("utf-8")


def alipay_notify(op, base, order_id, total_amount, ali_priv):
    params = {
        "app_id": "2021000000000000",
        "out_trade_no": order_id,
        "trade_no": "2026112200000001",
        "trade_status": "TRADE_SUCCESS",
        "total_amount": total_amount,
        "sign_type": "RSA2",
        "notify_time": "2026-11-22 23:00:00",
    }
    content = "&".join("%s=%s" % (k, params[k]) for k in sorted(params) if k != "sign_type")
    params["sign"] = sign_rsa2(content, ali_priv)
    status, _h, body = call(op, "POST", base + "/api/pay/notify/alipay", form=params)
    return status, body.decode("utf-8")


def wechat_notify(op, base, order_id, total_cents, wxpay_priv, api_v3_key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    plain = {
        "mchid": "1900000101",
        "appid": "wx0000000000000000",
        "out_trade_no": order_id,
        "transaction_id": "4200000000000000001",
        "trade_state": "SUCCESS",
        "amount": {"total": total_cents, "currency": "CNY"},
    }
    nonce_gcm = "123456789012"
    ct = AESGCM(api_v3_key.encode("utf-8")).encrypt(
        nonce_gcm.encode("utf-8"),
        json.dumps(plain).encode("utf-8"),
        b"transaction",
    )
    body = json.dumps(
        {
            "id": "EVT-TEST-1",
            "create_time": "2026-11-22T23:00:00+08:00",
            "event_type": "TRANSACTION.SUCCESS",
            "resource_type": "encrypt-resource",
            "resource": {
                "algorithm": "AEAD_AES_256_GCM",
                "ciphertext": base64.b64encode(ct).decode("utf-8"),
                "nonce": nonce_gcm,
                "associated_data": "transaction",
            },
        }
    )
    ts = str(int(time.time()))
    nonce = "testnonce123"
    sig = sign_rsa2("%s\n%s\n%s\n" % (ts, nonce, body), wxpay_priv)
    req = urllib.request.Request(
        base + "/api/pay/notify/wechat",
        data=body.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Wechatpay-Timestamp": ts,
            "Wechatpay-Nonce": nonce,
            "Wechatpay-Signature": sig,
            "Wechatpay-Serial": "PUB_KEY_ID_TEST",
        },
    )
    try:
        with op.open(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def main():
    privs, pubs = gen_keys()
    for db in ("/tmp/store-test-a.db", "/tmp/store-test-b.db"):
        if os.path.exists(db):
            os.remove(db)

    env_a = dict(os.environ)
    env_a.update(
        {
            "ORDERS_DB_PATH": "/tmp/store-test-a.db",
            "PAY_HOST": "127.0.0.1",
            "PAY_PORT": str(PORT_A),
            "ADMIN_TOKEN": ADMIN_TOKEN,
            "ALIPAY_APP_ID": "2021000000000000",
            "ALIPAY_APP_PRIVATE_KEY_PATH": privs["app"],
            "ALIPAY_PUBLIC_KEY_PATH": pubs["ali"],
            "ALIPAY_NOTIFY_URL": "https://888ai.me/api/pay/notify/alipay",
            "ALIPAY_RETURN_URL": "https://888ai.me/",
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
    env_b = dict(os.environ)
    env_b.update(
        {
            "ORDERS_DB_PATH": "/tmp/store-test-b.db",
            "PAY_HOST": "127.0.0.1",
            "PAY_PORT": str(PORT_B),
        }
    )
    for k in list(env_b):
        if k.startswith(("ALIPAY_", "WECHAT_", "ADMIN_")):
            del env_b[k]

    proc_a = subprocess.Popen(
        [sys.executable, "app.py"], cwd=SERVER_DIR, env=env_a,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    proc_b = subprocess.Popen(
        ["/usr/bin/python3", "app.py"], cwd=SERVER_DIR, env=env_b,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        wait_health(PORT_A, proc_a)
        wait_health(PORT_B, proc_b)
        A = "http://127.0.0.1:%d" % PORT_A
        B = "http://127.0.0.1:%d" % PORT_B
        guest = opener()
        buyer = opener(with_cookies=True)
        admin = opener(with_cookies=True)

        # ---------- 账号 ----------
        s, j = call_json(guest, "POST", A + "/api/register",
                         {"phone": "12345", "password": "abcdef"})
        check("register bad phone 400", s == 400 and j.get("error") == "invalid_phone")
        s, j = call_json(guest, "POST", A + "/api/register",
                         {"phone": BUYER_PHONE, "password": "123"})
        check("register short password 400", s == 400 and j.get("error") == "password_too_short")
        s, j = call_json(buyer, "POST", A + "/api/register",
                         {"phone": BUYER_PHONE, "password": "secret6"})
        check("register 201", s == 201 and j["user"]["phone"] == BUYER_PHONE, s)
        s, j = call_json(guest, "POST", A + "/api/register",
                         {"phone": BUYER_PHONE, "password": "secret6"})
        check("register duplicate 409", s == 409)
        s, j = call_json(buyer, "GET", A + "/api/me")
        check("me after register", s == 200 and j["user"]["phone"] == BUYER_PHONE)
        s, j = call_json(guest, "POST", A + "/api/login",
                         {"phone": BUYER_PHONE, "password": "wrong-pass"})
        check("login wrong password 401", s == 401)
        s, j = call_json(guest, "GET", A + "/api/my/orders")
        check("my/orders requires login 401", s == 401)

        # ---------- 建单 ----------
        s, j = call_json(guest, "POST", A + "/api/order",
                         {"tier": "legacy_basic", "phone": GUEST_PHONE})
        check("order without address 400", s == 400 and j.get("error") == "address_required"
              and "recipient" in j.get("missing", []) and "address_detail" in j.get("missing", []), j)
        s, j = call_json(guest, "POST", A + "/api/order",
                         {"tier": "legacy_basic", "phone": GUEST_PHONE,
                          "recipient": "李四", "province": "浙江省", "city": "台州市",
                          "district": "仙居县", "address_detail": "某街道 1 号",
                          "car": "2020 Model 3"})
        check("guest order 201", s == 201 and j.get("fulfillment_status") == "AWAITING_PAYMENT", j)
        order_g = j["order_id"]
        s, j = call_json(buyer, "POST", A + "/api/order",
                         {"tier": "legacy_eap", "phone": BUYER_PHONE,
                          "recipient": "张三", "province": "浙江省", "city": "杭州市",
                          "district": "西湖区", "address_detail": "文三路 100 号",
                          "wechat": "zhangsan_wx", "car": "2023 Model 3"})
        check("user order 201 amount 499", s == 201 and j.get("amount") == "499.00", j)
        order_u = j["order_id"]

        s, j = call_json(buyer, "GET", A + "/api/my/orders")
        ids = [o["order_id"] for o in j.get("orders", [])]
        check("my/orders contains own only", order_u in ids and order_g not in ids, ids)
        own = next(o for o in j["orders"] if o["order_id"] == order_u)
        check("my/orders fulfillment", own["fulfillment_status"] == "AWAITING_PAYMENT"
              and own["recipient"] == "张三" and own["address_detail"] == "文三路 100 号")

        # ---------- 地址簿 ----------
        s, j = call_json(buyer, "GET", A + "/api/my/addresses")
        addrs = j.get("addresses", [])
        check("order address auto-saved as default",
              len(addrs) == 1 and addrs[0]["is_default"] and addrs[0]["city"] == "杭州市", addrs)
        s, j = call_json(buyer, "POST", A + "/api/my/addresses",
                         {"recipient": "张三丰", "phone": BUYER_PHONE, "province": "浙江省",
                          "city": "台州市", "district": "椒江区", "address_detail": "机场路 2 号"})
        check("add address 201", s == 201 and j["address"]["district"] == "椒江区")
        addr2 = j["address"]["id"]
        s, j = call_json(buyer, "POST", A + "/api/my/addresses/%d/default" % addr2)
        check("set default 200", s == 200)
        s, j = call_json(buyer, "GET", A + "/api/my/addresses")
        dflt = [a for a in j["addresses"] if a["is_default"]]
        check("default switched", len(dflt) == 1 and dflt[0]["id"] == addr2)
        s, j = call_json(buyer, "DELETE", A + "/api/my/addresses/%d" % addr2)
        check("delete address 200", s == 200)
        s, j = call_json(buyer, "GET", A + "/api/my/addresses")
        check("address count back to 1", len(j.get("addresses", [])) == 1)

        # ---------- 公开查单脱敏 ----------
        s, _h, body = call(guest, "GET", A + "/api/order/" + order_g)
        text = body.decode("utf-8")
        j = json.loads(text)
        check("public order masked", s == 200 and j["phone"] == "139****6666"
              and "address_detail" not in text and "recipient" not in text
              and "某街道" not in text and j["fulfillment_status"] == "AWAITING_PAYMENT", text[:200])

        # ---------- 游客追踪 ----------
        s, j = call_json(guest, "GET", A + "/api/track/" + order_g)
        check("track needs phone 400", s == 400)
        s, j = call_json(guest, "GET", A + "/api/track/%s?phone=0000" % order_g)
        check("track wrong phone 403", s == 403)
        s, j = call_json(guest, "GET", A + "/api/track/%s?phone=6666" % order_g)
        check("track last4 200 city-level only",
              s == 200 and j["city"] == "台州市" and "address_detail" not in j
              and j["timeline"][0]["current"] is True, j)
        s, j = call_json(guest, "GET", A + "/api/track/%s?phone=%s" % (order_g, GUEST_PHONE))
        check("track full phone 200", s == 200)

        # ---------- 回调联动履约 ----------
        s, txt = alipay_notify(guest, A, order_g, "399.00", privs["ali"])
        check("alipay notify success", s == 200 and txt == "success", txt)
        s, j = call_json(guest, "GET", A + "/api/track/%s?phone=6666" % order_g)
        paid_node = [n for n in j["timeline"] if n["key"] == "PAID"]
        check("alipay PAID links fulfillment",
              j["fulfillment_status"] == "PAID" and j["status"] == "PAID"
              and paid_node and paid_node[0]["current"] and paid_node[0]["time"], j["fulfillment_status"])
        s, code = wechat_notify(guest, A, order_u, 49900, privs["wxpay"],
                                "0123456789abcdef0123456789abcdef")
        check("wechat notify SUCCESS", s == 200 and code.get("code") == "SUCCESS", code)
        s, j = call_json(buyer, "GET", A + "/api/my/orders")
        own = next(o for o in j["orders"] if o["order_id"] == order_u)
        check("wechat PAID links fulfillment", own["status"] == "PAID"
              and own["fulfillment_status"] == "PAID")

        # ---------- 后台 ----------
        s, j = call_json(guest, "GET", A + "/api/admin/orders")
        check("admin orders requires auth 401", s == 401)
        s, j = call_json(admin, "POST", A + "/api/admin/login", {"token": "wrong"})
        check("admin login wrong token 401", s == 401)
        s, j = call_json(admin, "POST", A + "/api/admin/login", {"token": ADMIN_TOKEN})
        check("admin login 200", s == 200 and j.get("admin") is True)
        s, j = call_json(admin, "GET", A + "/api/admin/orders")
        all_ids = [o["order_id"] for o in j.get("orders", [])]
        og = next((o for o in j["orders"] if o["order_id"] == order_g), {})
        check("admin sees full phone+address",
              order_g in all_ids and og.get("phone") == GUEST_PHONE
              and og.get("address_detail") == "某街道 1 号" and og.get("wechat") is not None, og.get("phone"))
        s, j = call_json(admin, "POST", A + "/api/admin/order/%s/fulfillment" % order_g,
                         {"fulfillment_status": "BOGUS"})
        check("admin invalid status 400", s == 400)
        s, j = call_json(admin, "POST", A + "/api/admin/order/%s/fulfillment" % order_g,
                         {"fulfillment_status": "PREPARING"})
        check("admin set PREPARING", s == 200 and j["order"]["fulfillment_status"] == "PREPARING")
        s, j = call_json(admin, "POST", A + "/api/admin/order/%s/fulfillment" % order_g,
                         {"fulfillment_status": "SHIPPED", "courier": "顺丰速运",
                          "tracking_no": "SF123456789"})
        check("admin set SHIPPED + tracking", s == 200
              and j["order"]["tracking_no"] == "SF123456789")
        s, j = call_json(guest, "GET", A + "/api/track/%s?phone=6666" % order_g)
        ship_node = [n for n in j["timeline"] if n["key"] == "SHIPPED"]
        check("track reflects SHIPPED", j["fulfillment_status"] == "SHIPPED"
              and j["courier"] == "顺丰速运" and j["tracking_no"] == "SF123456789"
              and ship_node and ship_node[0]["current"])
        s, j = call_json(admin, "GET", A + "/api/admin/orders?status=SHIPPED")
        check("admin filter SHIPPED", order_g in [o["order_id"] for o in j.get("orders", [])])
        s, j = call_json(admin, "GET", A + "/api/admin/orders?status=PREPARING")
        check("admin filter PREPARING excludes", order_g not in [o["order_id"] for o in j.get("orders", [])])
        s, j = call_json(guest, "GET", A + "/api/admin/orders",
                         headers={"Authorization": "Bearer " + ADMIN_TOKEN})
        check("admin Bearer auth", s == 200)

        # ---------- 二维码接口 ----------
        s, h, body = call(guest, "GET", A + "/api/qr?text=" +
                          urllib.parse.quote("weixin://wxpay/bizpayurl?pr=AbCdEfGh", safe=""))
        check("qr endpoint png", s == 200 and h.get_content_type() == "image/png"
              and body[:8] == b"\x89PNG\r\n\x1a\n")
        s, j = call_json(guest, "GET", A + "/api/qr")
        check("qr missing text 400", s == 400)

        # ---------- 退出 ----------
        s, j = call_json(buyer, "POST", A + "/api/logout")
        check("logout 200", s == 200)
        s, j = call_json(buyer, "GET", A + "/api/me")
        check("me after logout 401", s == 401)

        # ---------- Server B：无配置回归 ----------
        s, j = call_json(guest, "POST", B + "/api/admin/login", {"token": "x"})
        check("B admin not configured 503", s == 503 and j.get("error") == "admin_not_configured")
        s, j = call_json(guest, "GET", B + "/api/admin/orders")
        check("B admin orders 503", s == 503)
        s, j = call_json(guest, "POST", B + "/api/order",
                         {"tier": "new_fsd", "phone": GUEST_PHONE, "recipient": "王五",
                          "address_detail": "某路 3 号"})
        check("B stdlib order 201", s == 201 and j.get("amount") == "699.00", j)
        oid_b = j["order_id"]
        s, j = call_json(guest, "POST", B + "/api/pay/alipay", {"order_id": oid_b})
        check("B alipay config missing 503", s == 503 and j.get("error") == "alipay_config_missing")
        s, j = call_json(guest, "POST", B + "/api/order", {"tier": "bad", "phone": GUEST_PHONE})
        check("B invalid tier 400", s == 400)
        buyer_b = opener(with_cookies=True)
        s, j = call_json(buyer_b, "POST", B + "/api/register",
                         {"phone": BUYER_PHONE, "password": "secret6"})
        check("B stdlib register 201", s == 201)
        s, j = call_json(buyer_b, "GET", B + "/api/me")
        check("B stdlib cookie session works", s == 200)
        s, h, body = call(guest, "GET", B + "/api/qr?text=hello")
        check("B stdlib qr png", s == 200 and body[:8] == b"\x89PNG\r\n\x1a\n")
    finally:
        proc_a.terminate()
        proc_b.terminate()
        try:
            out_a = proc_a.communicate(timeout=5)[0].decode("utf-8", "replace")
        except Exception:
            out_a = ""
        try:
            proc_b.communicate(timeout=5)
        except Exception:
            pass
        failed = [n for n, ok in results if not ok]
        print("\n==== %d/%d passed ====" % (len(results) - len(failed), len(results)))
        if failed:
            print("FAILED:", failed)
            print("---- server A log tail ----")
            print("\n".join(out_a.splitlines()[-30:]))
    sys.exit(1 if any(not ok for _n, ok in results) else 0)


if __name__ == "__main__":
    main()
