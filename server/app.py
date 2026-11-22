#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
888ai.me（阿童木 P4-CAN 套件）支付后端（支付宝 + 微信支付）。

设计：
- 核心逻辑与 Web 框架无关；运行时优先用 Flask，环境没有 Flask 时
  自动退回标准库 http.server（ThreadingHTTPServer），行为一致。
- 订单存 SQLite 单文件；金额一律由服务端按档位查表，绝不信任前端。
- 支付宝 RSA2 签名/验签、微信 APIv3 签名/验签与 AES-256-GCM 解密
  用 cryptography 库；商户参数全部走环境变量，
  缺失时支付接口返回 503 配置缺失，不崩溃。

接口：
  GET  /api/health
  POST /api/order                 建单
  POST /api/pay/alipay            生成支付宝支付 URL（page/wap 按 UA 选）
  POST /api/pay/notify/alipay     支付宝异步回调（验签后置 PAID）
  POST /api/pay/wechat            微信下单（Native 扫码 / H5 按 UA 选）
  POST /api/pay/notify/wechat     微信异步回调（验签+解密后置 PAID）
  GET  /api/order/<order_id>      查单（手机号脱敏）

环境变量见 README.md。
"""

import base64
import json
import logging
import os
import re
import secrets
import sqlite3
import string
import threading
import time
from datetime import datetime
from decimal import Decimal
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ---------------------------------------------------------------- 日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("pay")

# ---------------------------------------------------------------- 档位与价格（服务端唯一价格源，单位：分）
TIERS = {
    "legacy_basic": {"name": "老款基础", "amount_cents": 39900},
    "legacy_eap":   {"name": "老款 EAP",  "amount_cents": 49900},
    "legacy_fsd":   {"name": "老款 FSD",  "amount_cents": 59900},
    "new_basic":    {"name": "新款基础", "amount_cents": 49900},
    "new_eap":      {"name": "新款 EAP",  "amount_cents": 59900},
    "new_fsd":      {"name": "新款 FSD",  "amount_cents": 69900},
}

SUBJECT_PREFIX = "阿童木 P4-CAN 套件 "

# ---------------------------------------------------------------- 配置（环境变量）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("ORDERS_DB_PATH", os.path.join(BASE_DIR, "orders.db"))
HOST = os.environ.get("PAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("PAY_PORT", "9000"))

ALIPAY_GATEWAY_DEFAULT = "https://openapi.alipay.com/gateway.do"


def alipay_config():
    """读取支付宝商户配置；返回 (cfg, missing_list)。"""
    cfg = {
        "app_id": os.environ.get("ALIPAY_APP_ID", "").strip(),
        "app_private_key_path": os.environ.get("ALIPAY_APP_PRIVATE_KEY_PATH", "").strip(),
        "alipay_public_key_path": os.environ.get("ALIPAY_PUBLIC_KEY_PATH", "").strip(),
        "gateway": os.environ.get("ALIPAY_GATEWAY", ALIPAY_GATEWAY_DEFAULT).strip()
        or ALIPAY_GATEWAY_DEFAULT,
        "notify_url": os.environ.get("ALIPAY_NOTIFY_URL", "").strip(),
        "return_url": os.environ.get("ALIPAY_RETURN_URL", "").strip(),
    }
    missing = [
        name
        for name, key in (
            ("ALIPAY_APP_ID", "app_id"),
            ("ALIPAY_APP_PRIVATE_KEY_PATH", "app_private_key_path"),
            ("ALIPAY_PUBLIC_KEY_PATH", "alipay_public_key_path"),
            ("ALIPAY_NOTIFY_URL", "notify_url"),
        )
        if not cfg[key]
    ]
    return cfg, missing


WECHAT_API_BASE = "https://api.mch.weixin.qq.com"


def wechat_config():
    """读取微信商户配置；返回 (cfg, missing_list)。"""
    cfg = {
        "mchid": os.environ.get("WECHAT_MCHID", "").strip(),
        "appid": os.environ.get("WECHAT_APPID", "").strip(),
        "cert_serial": os.environ.get("WECHAT_CERT_SERIAL", "").strip(),
        "private_key_path": os.environ.get("WECHAT_PRIVATE_KEY_PATH", "").strip(),
        "api_v3_key": os.environ.get("WECHAT_API_V3_KEY", "").strip(),
        "pub_key_id": os.environ.get("WECHAT_PUB_KEY_ID", "").strip(),
        "pub_key_path": os.environ.get("WECHAT_PUB_KEY_PATH", "").strip(),
        "notify_url": os.environ.get("WECHAT_NOTIFY_URL", "").strip(),
    }
    missing = [
        name
        for name, key in (
            ("WECHAT_MCHID", "mchid"),
            ("WECHAT_APPID", "appid"),
            ("WECHAT_CERT_SERIAL", "cert_serial"),
            ("WECHAT_PRIVATE_KEY_PATH", "private_key_path"),
            ("WECHAT_API_V3_KEY", "api_v3_key"),
            ("WECHAT_PUB_KEY_ID", "pub_key_id"),
            ("WECHAT_PUB_KEY_PATH", "pub_key_path"),
            ("WECHAT_NOTIFY_URL", "notify_url"),
        )
        if not cfg[key]
    ]
    return cfg, missing


# ---------------------------------------------------------------- 数据库
_db_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    tier            TEXT NOT NULL,
    tier_name       TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL,
    phone           TEXT NOT NULL,
    wechat          TEXT DEFAULT '',
    car             TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'PENDING',
    alipay_trade_no TEXT DEFAULT '',
    wechat_transaction_id TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    paid_at         TEXT DEFAULT ''
);
"""


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with _db_lock, db_connect() as conn:
        conn.execute(SCHEMA)
        # 老库迁移：早期订单表没有微信交易号列，补上
        cols = [row[1] for row in conn.execute("PRAGMA table_info(orders)")]
        if "wechat_transaction_id" not in cols:
            conn.execute(
                "ALTER TABLE orders ADD COLUMN wechat_transaction_id TEXT DEFAULT ''"
            )


def db_get_order(order_id):
    with _db_lock, db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
    return dict(row) if row else None


def db_insert_order(order):
    with _db_lock, db_connect() as conn:
        conn.execute(
            "INSERT INTO orders (order_id, tier, tier_name, amount_cents, phone,"
            " wechat, car, status, created_at) VALUES (?,?,?,?,?,?,?,'PENDING',?)",
            (
                order["order_id"],
                order["tier"],
                order["tier_name"],
                order["amount_cents"],
                order["phone"],
                order["wechat"],
                order["car"],
                order["created_at"],
            ),
        )


def db_mark_paid(order_id, trade_no):
    with _db_lock, db_connect() as conn:
        conn.execute(
            "UPDATE orders SET status='PAID', alipay_trade_no=?, paid_at=?"
            " WHERE order_id=?",
            (trade_no, datetime.now().isoformat(timespec="seconds"), order_id),
        )


def db_mark_paid_wechat(order_id, transaction_id):
    with _db_lock, db_connect() as conn:
        conn.execute(
            "UPDATE orders SET status='PAID', wechat_transaction_id=?, paid_at=?"
            " WHERE order_id=?",
            (transaction_id, datetime.now().isoformat(timespec="seconds"), order_id),
        )


# ---------------------------------------------------------------- 工具
def cents_to_yuan_str(cents):
    return str((Decimal(cents) / Decimal(100)).quantize(Decimal("0.00")))


def mask_phone(phone):
    if len(phone) >= 11:
        return phone[:3] + "****" + phone[-4:]
    if len(phone) > 4:
        return phone[:2] + "****" + phone[-2:]
    return "****"


def new_order_id():
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    rand = "".join(secrets.choice(string.digits) for _ in range(6))
    return "AT" + ts + rand


def json_response(status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return status, "application/json; charset=utf-8", body


def text_response(status, text):
    return status, "text/plain; charset=utf-8", text.encode("utf-8")


# ---------------------------------------------------------------- RSA2（支付宝）
def _load_pem(path, kind):
    """读取 PEM 密钥文件；容忍只有 base64 正文、没有 PEM 头尾的文件。"""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if "-----BEGIN" not in content:
        if kind == "private":
            header = "-----BEGIN PRIVATE KEY-----"
            footer = "-----END PRIVATE KEY-----"
        else:
            header = "-----BEGIN PUBLIC KEY-----"
            footer = "-----END PUBLIC KEY-----"
        body = re.sub(r"\s+", "", content)
        wrapped = "\n".join(
            [header] + [body[i : i + 64] for i in range(0, len(body), 64)] + [footer]
        )
        content = wrapped
    return content.encode("utf-8")


def build_sign_content(params):
    """支付宝签名串：剔除 sign / sign_type（验签时）与空值，key 排序后 k=v& 拼接。

    注意：构造请求时 sign_type 需要参与签名，验签回调时 sign 与 sign_type 都剔除，
    因此由调用方决定传入前是否保留 sign_type。
    """
    items = [
        (k, v)
        for k, v in params.items()
        if k != "sign" and v is not None and str(v) != ""
    ]
    items.sort(key=lambda kv: kv[0])
    return "&".join("%s=%s" % (k, v) for k, v in items)


def rsa2_sign(content, private_key_path):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    private_key = serialization.load_pem_private_key(
        _load_pem(private_key_path, "private"), password=None
    )
    signature = private_key.sign(
        content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256()
    )
    return base64.b64encode(signature).decode("utf-8")


def rsa2_verify(content, signature_b64, public_key_path):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    public_key = serialization.load_pem_public_key(
        _load_pem(public_key_path, "public")
    )
    try:
        public_key.verify(
            base64.b64decode(signature_b64),
            content.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except InvalidSignature:
        return False
    except Exception as exc:  # 密钥格式等问题按验签失败处理并记录
        log.error("verify error: %s", exc)
        return False


# ---------------------------------------------------------------- 业务处理（框架无关）
def handle_health():
    return json_response(200, {"ok": True, "service": "p4can-pay"})


def handle_create_order(data):
    if not isinstance(data, dict):
        return json_response(400, {"error": "invalid_json"})
    tier = str(data.get("tier", "")).strip()
    phone = str(data.get("phone", "")).strip()
    wechat = str(data.get("wechat", "") or "").strip()
    car = str(data.get("car", "") or "").strip()

    if tier not in TIERS:
        return json_response(
            400, {"error": "invalid_tier", "allowed": sorted(TIERS.keys())}
        )
    if not re.fullmatch(r"1\d{10}", phone):
        return json_response(400, {"error": "invalid_phone"})

    info = TIERS[tier]
    order = {
        "order_id": new_order_id(),
        "tier": tier,
        "tier_name": info["name"],
        "amount_cents": info["amount_cents"],
        "phone": phone,
        "wechat": wechat,
        "car": car,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    db_insert_order(order)
    log.info("order created: %s tier=%s amount=%s", order["order_id"], tier, info["amount_cents"])
    return json_response(
        201,
        {
            "order_id": order["order_id"],
            "tier": tier,
            "tier_name": info["name"],
            "amount": cents_to_yuan_str(info["amount_cents"]),
            "status": "PENDING",
        },
    )


def handle_get_order(order_id):
    order = db_get_order(order_id)
    if not order:
        return json_response(404, {"error": "order_not_found"})
    return json_response(
        200,
        {
            "order_id": order["order_id"],
            "tier": order["tier"],
            "tier_name": order["tier_name"],
            "amount": cents_to_yuan_str(order["amount_cents"]),
            "status": order["status"],
            "phone": mask_phone(order["phone"]),
            "created_at": order["created_at"],
            "paid_at": order["paid_at"] or None,
        },
    )


def handle_alipay_pay(data, user_agent):
    if not isinstance(data, dict):
        return json_response(400, {"error": "invalid_json"})
    order_id = str(data.get("order_id", "")).strip()
    if not order_id:
        return json_response(400, {"error": "order_id_required"})

    cfg, missing = alipay_config()
    if missing:
        return json_response(
            503,
            {
                "error": "alipay_config_missing",
                "message": "支付宝商户参数未配置，暂不能支付",
                "missing": missing,
            },
        )

    order = db_get_order(order_id)
    if not order:
        return json_response(404, {"error": "order_not_found"})
    if order["status"] == "PAID":
        return json_response(409, {"error": "order_already_paid"})

    ua = (user_agent or "").lower()
    is_mobile = any(k in ua for k in ("mobile", "android", "iphone", "ipad"))
    if is_mobile:
        method = "alipay.trade.wap.pay"
        product_code = "QUICK_WAP_WAY"
    else:
        method = "alipay.trade.page.pay"
        product_code = "FAST_INSTANT_TRADE_PAY"

    biz_content = {
        "out_trade_no": order["order_id"],
        "total_amount": cents_to_yuan_str(order["amount_cents"]),
        "subject": SUBJECT_PREFIX + order["tier_name"],
        "product_code": product_code,
    }
    params = {
        "app_id": cfg["app_id"],
        "method": method,
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "notify_url": cfg["notify_url"],
        "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":")),
    }
    if cfg["return_url"]:
        params["return_url"] = cfg["return_url"]

    try:
        sign = rsa2_sign(
            build_sign_content(params), cfg["app_private_key_path"]
        )
    except ImportError:
        return json_response(
            503,
            {
                "error": "crypto_dependency_missing",
                "message": "服务器缺少 cryptography 库，无法签名（pip install cryptography）",
            },
        )
    except Exception as exc:
        log.error("sign failed: %s", exc)
        return json_response(500, {"error": "sign_failed"})

    params["sign"] = sign
    pay_url = cfg["gateway"] + "?" + urlencode(params)
    return json_response(
        200,
        {
            "order_id": order["order_id"],
            "amount": cents_to_yuan_str(order["amount_cents"]),
            "method": method,
            "pay_url": pay_url,
        },
    )


def handle_alipay_notify(form):
    """支付宝异步通知：form 为 {key: value} 的普通 dict。返回纯文本 success/failure。"""
    cfg, missing = alipay_config()
    if missing:
        log.error("notify received but alipay config missing: %s", missing)
        return text_response(200, "failure")

    params = dict(form)
    sign = params.pop("sign", "")
    sign_type = params.pop("sign_type", None)
    if not sign:
        log.warning("notify without sign: %s", params.get("out_trade_no"))
        return text_response(200, "failure")

    # 官方文档口径：验签串剔除 sign 与 sign_type；个别实现保留 sign_type，
    # 两种串都试一次（仍需合法签名，不降低安全性）。
    candidates = [build_sign_content(params)]
    if sign_type:
        with_type = dict(params)
        with_type["sign_type"] = sign_type
        candidates.append(build_sign_content(with_type))
    if not any(
        rsa2_verify(c, sign, cfg["alipay_public_key_path"]) for c in candidates
    ):
        log.warning("notify verify FAILED: out_trade_no=%s", params.get("out_trade_no"))
        return text_response(200, "failure")

    # 业务校验
    if params.get("app_id") != cfg["app_id"]:
        log.warning("notify app_id mismatch: %s", params.get("app_id"))
        return text_response(200, "failure")

    order_id = params.get("out_trade_no", "")
    order = db_get_order(order_id)
    if not order:
        log.warning("notify unknown order: %s", order_id)
        return text_response(200, "failure")

    try:
        notify_amount = Decimal(params.get("total_amount", ""))
    except Exception:
        log.warning("notify bad total_amount: %s", params.get("total_amount"))
        return text_response(200, "failure")
    if notify_amount != (Decimal(order["amount_cents"]) / Decimal(100)):
        log.warning(
            "notify amount mismatch: order=%s expected=%s got=%s",
            order_id,
            cents_to_yuan_str(order["amount_cents"]),
            params.get("total_amount"),
        )
        return text_response(200, "failure")

    trade_status = params.get("trade_status", "")
    if trade_status in ("TRADE_SUCCESS", "TRADE_FINISHED"):
        if order["status"] == "PAID":
            log.info("notify duplicate for paid order: %s", order_id)
            return text_response(200, "success")
        db_mark_paid(order_id, params.get("trade_no", ""))
        log.info(
            "order PAID: %s trade_no=%s amount=%s",
            order_id,
            params.get("trade_no"),
            params.get("total_amount"),
        )
        return text_response(200, "success")

    log.info("notify ignored status %s for order %s", trade_status, order_id)
    return text_response(200, "success")


# ---------------------------------------------------------------- 微信支付（APIv3）
def wechat_verify_signature(cfg, timestamp, nonce, body_text, signature_b64, serial):
    """微信支付公钥模式验签：serial 须等于公钥 ID，签名串为 timestamp\\nnonce\\nbody\\n。

    与支付宝同为 RSA PKCS1v15 + SHA256，直接复用 rsa2_verify。
    """
    if serial != cfg["pub_key_id"]:
        log.warning("wechat serial mismatch: %s", serial)
        return False
    message = "%s\n%s\n%s\n" % (timestamp, nonce, body_text)
    return rsa2_verify(message, signature_b64, cfg["pub_key_path"])


def wechat_api_post(cfg, path, payload):
    """带商户证书签名的 APIv3 POST；返回 (status, resp_headers, resp_body_bytes)。

    签名串：POST\\n<path>\\n<timestamp>\\n<nonce>\\n<body>\\n，
    用商户证书私钥 RSA-SHA256 签名（与支付宝同算法，复用 rsa2_sign）。
    """
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    sign_message = "POST\n%s\n%s\n%s\n%s\n" % (
        path,
        timestamp,
        nonce,
        body.decode("utf-8"),
    )
    signature = rsa2_sign(sign_message, cfg["private_key_path"])
    authorization = (
        'WECHATPAY2-SHA256-RSA2048 mchid="%s",nonce_str="%s",timestamp="%s",'
        'serial_no="%s",signature="%s"'
        % (cfg["mchid"], nonce, timestamp, cfg["cert_serial"], signature)
    )
    req = Request(
        WECHAT_API_BASE + path,
        data=body,
        method="POST",
        headers={
            "Authorization": authorization,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "888ai-pay/1.0",
        },
    )
    try:
        with urlopen(req, timeout=15) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, headers, resp.read()
    except HTTPError as exc:
        headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return exc.code, headers, exc.read()


def handle_wechat_pay(data, user_agent, client_ip):
    if not isinstance(data, dict):
        return json_response(400, {"error": "invalid_json"})
    order_id = str(data.get("order_id", "")).strip()
    if not order_id:
        return json_response(400, {"error": "order_id_required"})

    cfg, missing = wechat_config()
    if missing:
        return json_response(
            503,
            {
                "error": "wechat_config_missing",
                "message": "微信商户参数未配置，暂不能支付",
                "missing": missing,
            },
        )

    order = db_get_order(order_id)
    if not order:
        return json_response(404, {"error": "order_not_found"})
    if order["status"] == "PAID":
        return json_response(409, {"error": "order_already_paid"})

    ua = (user_agent or "").lower()
    is_mobile = any(k in ua for k in ("mobile", "android", "iphone", "ipad"))
    payload = {
        "mchid": cfg["mchid"],
        "appid": cfg["appid"],
        "description": SUBJECT_PREFIX + order["tier_name"],
        "out_trade_no": order["order_id"],
        "notify_url": cfg["notify_url"],
        "amount": {"total": order["amount_cents"], "currency": "CNY"},
    }
    if is_mobile:
        path = "/v3/pay/transactions/h5"
        payload["scene_info"] = {
            "payer_client_ip": client_ip or "127.0.0.1",
            "h5_info": {"type": "Wap"},
        }
    else:
        path = "/v3/pay/transactions/native"

    try:
        status, resp_headers, resp_body = wechat_api_post(cfg, path, payload)
    except ImportError:
        return json_response(
            503,
            {
                "error": "crypto_dependency_missing",
                "message": "服务器缺少 cryptography 库，无法签名（pip install cryptography）",
            },
        )
    except Exception as exc:
        log.error("wechat api request failed: %s", exc)
        return json_response(502, {"error": "wechat_api_unreachable"})

    body_text = resp_body.decode("utf-8", "replace")
    if status != 200:
        wx_code, wx_msg = "", body_text[:200]
        try:
            err = json.loads(body_text)
            wx_code = err.get("code", "")
            wx_msg = err.get("message", wx_msg)
        except Exception:
            pass
        log.error("wechat api error: http=%s code=%s msg=%s", status, wx_code, wx_msg)
        return json_response(
            502, {"error": "wechat_api_error", "wechat_code": wx_code, "message": wx_msg}
        )

    # 应答验签（微信支付公钥模式）：验不过视为不可信应答，不把链接交给前端
    if not wechat_verify_signature(
        cfg,
        resp_headers.get("wechatpay-timestamp", ""),
        resp_headers.get("wechatpay-nonce", ""),
        body_text,
        resp_headers.get("wechatpay-signature", ""),
        resp_headers.get("wechatpay-serial", ""),
    ):
        log.error("wechat response verify FAILED for order %s", order_id)
        return json_response(502, {"error": "wechat_response_verify_failed"})

    try:
        result = json.loads(body_text)
    except Exception:
        return json_response(502, {"error": "wechat_api_bad_response"})
    amount_str = cents_to_yuan_str(order["amount_cents"])
    if is_mobile:
        h5_url = result.get("h5_url", "")
        if not h5_url:
            return json_response(502, {"error": "wechat_api_bad_response"})
        return json_response(
            200,
            {
                "order_id": order["order_id"],
                "amount": amount_str,
                "pay_type": "h5",
                "h5_url": h5_url,
            },
        )
    code_url = result.get("code_url", "")
    if not code_url:
        return json_response(502, {"error": "wechat_api_bad_response"})
    return json_response(
        200,
        {
            "order_id": order["order_id"],
            "amount": amount_str,
            "pay_type": "native",
            "code_url": code_url,
        },
    )


def wechat_fail(status, message):
    return json_response(status, {"code": "FAIL", "message": message})


def handle_wechat_notify(raw_body, headers):
    """微信支付异步通知：headers 为小写 key 的 dict，raw_body 为请求体原文。

    流程：微信支付公钥验签 → APIv3 密钥 AES-256-GCM 解密 resource →
    校验商户号/订单/金额/trade_state → 置 PAID（幂等）。
    """
    cfg, missing = wechat_config()
    if missing:
        log.error("wechat notify received but config missing: %s", missing)
        return wechat_fail(500, "微信支付配置缺失")

    timestamp = headers.get("wechatpay-timestamp", "")
    nonce = headers.get("wechatpay-nonce", "")
    signature = headers.get("wechatpay-signature", "")
    serial = headers.get("wechatpay-serial", "")
    if not (timestamp and nonce and signature and serial):
        log.warning("wechat notify missing signature headers")
        return wechat_fail(401, "签名头缺失")
    try:
        if abs(int(time.time()) - int(timestamp)) > 300:
            log.warning("wechat notify timestamp too old: %s", timestamp)
            return wechat_fail(401, "通知已过期")
    except ValueError:
        return wechat_fail(401, "时间戳非法")

    try:
        body_text = raw_body.decode("utf-8")
    except Exception:
        return wechat_fail(400, "请求体非法")
    if not wechat_verify_signature(cfg, timestamp, nonce, body_text, signature, serial):
        log.warning("wechat notify verify FAILED")
        return wechat_fail(401, "签名验证失败")

    try:
        resource = json.loads(body_text)["resource"]
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = cfg["api_v3_key"].encode("utf-8")
        if len(key) != 32:
            log.error("WECHAT_API_V3_KEY is not 32 bytes")
            return wechat_fail(500, "APIv3 密钥配置错误")
        plaintext = AESGCM(key).decrypt(
            resource["nonce"].encode("utf-8"),
            base64.b64decode(resource["ciphertext"]),
            (resource.get("associated_data") or "").encode("utf-8"),
        )
        plain = json.loads(plaintext.decode("utf-8"))
    except ImportError:
        return wechat_fail(500, "服务器缺少 cryptography 库")
    except Exception as exc:
        log.warning("wechat notify decrypt/parse failed: %s", exc)
        return wechat_fail(400, "通知解密失败")

    if plain.get("mchid") != cfg["mchid"]:
        log.warning("wechat notify mchid mismatch: %s", plain.get("mchid"))
        return wechat_fail(400, "商户号不符")

    order_id = plain.get("out_trade_no", "")
    order = db_get_order(order_id)
    if not order:
        log.warning("wechat notify unknown order: %s", order_id)
        return wechat_fail(400, "订单不存在")

    total = (plain.get("amount") or {}).get("total")
    if total != order["amount_cents"]:
        log.warning(
            "wechat notify amount mismatch: order=%s expected=%s got=%s",
            order_id,
            order["amount_cents"],
            total,
        )
        return wechat_fail(400, "金额不符")

    if plain.get("trade_state") != "SUCCESS":
        log.info(
            "wechat notify ignored state %s for order %s",
            plain.get("trade_state"),
            order_id,
        )
        return json_response(200, {"code": "SUCCESS", "message": "成功"})

    if order["status"] == "PAID":
        log.info("wechat notify duplicate for paid order: %s", order_id)
        return json_response(200, {"code": "SUCCESS", "message": "成功"})
    db_mark_paid_wechat(order_id, plain.get("transaction_id", ""))
    log.info(
        "order PAID via wechat: %s transaction_id=%s amount=%s",
        order_id,
        plain.get("transaction_id"),
        total,
    )
    return json_response(200, {"code": "SUCCESS", "message": "成功"})


# ---------------------------------------------------------------- 路由分发（框架无关）
def dispatch(
    method,
    path,
    json_body=None,
    form=None,
    user_agent="",
    headers=None,
    raw_body=b"",
    remote_addr="",
):
    """返回 (status, content_type, body_bytes)。headers 统一转小写 key。"""
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    client_ip = headers.get("x-forwarded-for", "").split(",")[0].strip() or remote_addr
    if method == "GET" and path == "/api/health":
        return handle_health()
    if method == "POST" and path == "/api/order":
        return handle_create_order(json_body)
    if method == "POST" and path == "/api/pay/alipay":
        return handle_alipay_pay(json_body, user_agent)
    if method == "POST" and path == "/api/pay/notify/alipay":
        return handle_alipay_notify(form or {})
    if method == "POST" and path == "/api/pay/wechat":
        return handle_wechat_pay(json_body, user_agent, client_ip)
    if method == "POST" and path == "/api/pay/notify/wechat":
        return handle_wechat_notify(raw_body or b"", headers)
    m = re.fullmatch(r"/api/order/([A-Za-z0-9_-]+)", path or "")
    if method == "GET" and m:
        return handle_get_order(m.group(1))
    return json_response(404, {"error": "not_found", "path": path})


# ---------------------------------------------------------------- Flask 适配（优先）
def run_flask():
    from flask import Flask, request

    app = Flask(__name__)

    def _handle(**_view_args):
        raw_body = request.get_data(cache=True) or b""
        json_body = None
        if request.is_json:
            json_body = request.get_json(silent=True)
        form = request.form.to_dict() if request.form else None
        status, ctype, body = dispatch(
            request.method,
            request.path,
            json_body=json_body,
            form=form,
            user_agent=request.headers.get("User-Agent", ""),
            headers=dict(request.headers),
            raw_body=raw_body,
            remote_addr=request.remote_addr or "",
        )
        return app.response_class(body, status=status, content_type=ctype)

    app.add_url_rule("/api/health", view_func=_handle, methods=["GET"])
    app.add_url_rule("/api/order", view_func=_handle, methods=["POST"])
    app.add_url_rule("/api/order/<order_id>", view_func=_handle, methods=["GET"])
    app.add_url_rule("/api/pay/alipay", view_func=_handle, methods=["POST"])
    app.add_url_rule("/api/pay/notify/alipay", view_func=_handle, methods=["POST"])
    app.add_url_rule("/api/pay/wechat", view_func=_handle, methods=["POST"])
    app.add_url_rule("/api/pay/notify/wechat", view_func=_handle, methods=["POST"])

    log.info("starting Flask mode on %s:%s (db=%s)", HOST, PORT, DB_PATH)
    app.run(host=HOST, port=PORT, threaded=True)


# ---------------------------------------------------------------- 标准库适配（回退）
def run_stdlib():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 走 logging，别刷 stderr
            log.info("%s - %s", self.address_string(), fmt % args)

        def _respond(self, status, ctype, body):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length > 0 else b""

        def _handle(self):
            parsed = urlparse(self.path)
            raw = self._read_body()
            ctype = self.headers.get("Content-Type", "")
            json_body, form = None, None
            if raw:
                if "application/json" in ctype:
                    try:
                        json_body = json.loads(raw.decode("utf-8"))
                    except Exception:
                        json_body = None
                elif "application/x-www-form-urlencoded" in ctype:
                    qs = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
                    form = {k: v[0] for k, v in qs.items()}
            status, resp_ctype, body = dispatch(
                self.command,
                parsed.path,
                json_body=json_body,
                form=form,
                user_agent=self.headers.get("User-Agent", ""),
                headers=dict(self.headers.items()),
                raw_body=raw,
                remote_addr=self.client_address[0],
            )
            self._respond(status, resp_ctype, body)

        do_GET = _handle
        do_POST = _handle

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("starting stdlib http.server mode on %s:%s (db=%s)", HOST, PORT, DB_PATH)
    server.serve_forever()


def main():
    db_init()
    try:
        import flask  # noqa: F401
        run_flask()
    except ImportError:
        log.warning("Flask not available, falling back to stdlib http.server")
        run_stdlib()


if __name__ == "__main__":
    main()
