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
  POST /api/order                 建单（必填收货人 + 详细地址 + 手机号）
  POST /api/pay/alipay            生成支付宝支付 URL（page/wap 按 UA 选）
  POST /api/pay/notify/alipay     支付宝异步回调（验签后置 PAID）
  POST /api/pay/wechat            微信下单（默认按 UA 选 Native/H5；body.pay_type 可强制 native/h5）
  POST /api/pay/notify/wechat     微信异步回调（验签+解密后置 PAID）
  GET  /api/order/<order_id>      查单（手机号脱敏）
  POST /api/register              买家注册（手机号 + 密码，会话 Cookie）
  POST /api/login                 买家登录
  POST /api/logout                买家退出
  GET  /api/me                    当前买家
  GET  /api/my/orders             我的订单（需登录，含履约状态与快递单号）
  GET  /api/my/addresses          我的地址簿（需登录）
  POST /api/my/addresses          新增地址（需登录）
  POST /api/my/addresses/<id>/default  设默认地址（需登录）
  DELETE /api/my/addresses/<id>   删除地址（需登录）
  GET  /api/track/<order_id>?phone=  游客订单追踪（订单号 + 手机号后四位）
  POST /api/admin/login           卖家后台登录（ADMIN_TOKEN 换会话 Cookie）
  POST /api/admin/logout          卖家后台退出
  GET  /api/admin/session         后台会话状态
  GET  /api/admin/orders?status=  全部订单（需后台鉴权，含完整地址）
  POST /api/admin/order/<id>/fulfillment  改履约状态 / 快递（需后台鉴权）
  GET  /api/qr?text=              离线生成 QR 二维码 PNG（微信扫码支付用）
  POST /api/chat/send             站内客服：客户发消息（命中关键词自动秒回）
  GET  /api/chat/poll             站内客服：增量拉取会话消息（after_id）
  GET  /api/admin/chat/inbox      客服收件箱：会话列表 + 未读数（需后台鉴权）
  GET  /api/admin/chat/thread/<session_id>  会话完整线程（打开即标记已读）
  POST /api/admin/chat/reply      客服回复（需后台鉴权）

环境变量见 README.md。
"""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import string
import struct
import threading
import time
import zlib
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

# ---------------------------------------------------------------- 商城（账号 / 后台 / 履约）
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").strip().lower() in (
    "1",
    "true",
    "yes",
)
USER_COOKIE = "p4session"
ADMIN_COOKIE = "p4admin"
SESSION_TTL_SECONDS = 30 * 86400  # 买家会话 30 天
ADMIN_SESSION_TTL_SECONDS = 12 * 3600  # 后台会话 12 小时

FULFILLMENT_STATUSES = [
    "AWAITING_PAYMENT",
    "PAID",
    "PREPARING",
    "SHIPPED",
    "IN_TRANSIT",
    "DELIVERED",
    "COMPLETED",
    "AFTER_SALE",
]
FULFILLMENT_LABELS = {
    "AWAITING_PAYMENT": "待付款",
    "PAID": "付款成功（待发货）",
    "PREPARING": "备货中",
    "SHIPPED": "已发货",
    "IN_TRANSIT": "运输中",
    "DELIVERED": "已签收",
    "COMPLETED": "已完成",
    "AFTER_SALE": "售后中",
}
# 时间线主线顺序（AFTER_SALE 为旁路状态，不进主线）
FULFILLMENT_FLOW = [
    "AWAITING_PAYMENT",
    "PAID",
    "PREPARING",
    "SHIPPED",
    "IN_TRANSIT",
    "DELIVERED",
    "COMPLETED",
]

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

SCHEMA_STORE = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    phone         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER,
    kind       TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS addresses (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    recipient      TEXT NOT NULL DEFAULT '',
    phone          TEXT NOT NULL DEFAULT '',
    province       TEXT NOT NULL DEFAULT '',
    city           TEXT NOT NULL DEFAULT '',
    district       TEXT NOT NULL DEFAULT '',
    address_detail TEXT NOT NULL DEFAULT '',
    is_default     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id        TEXT PRIMARY KEY,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    customer_note     TEXT DEFAULT '',
    agent_last_read_id INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    sender     TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'human',
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session
    ON chat_messages (session_id, id);
"""

# 订单表新增列（与 wechat_transaction_id 相同的 ALTER TABLE 迁移手法）
ORDER_NEW_COLUMNS = {
    "user_id": "INTEGER",
    "recipient": "TEXT DEFAULT ''",
    "province": "TEXT DEFAULT ''",
    "city": "TEXT DEFAULT ''",
    "district": "TEXT DEFAULT ''",
    "address_detail": "TEXT DEFAULT ''",
    "fulfillment_status": "TEXT DEFAULT 'AWAITING_PAYMENT'",
    "courier": "TEXT DEFAULT ''",
    "tracking_no": "TEXT DEFAULT ''",
    "status_updated_at": "TEXT DEFAULT ''",
}


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with _db_lock, db_connect() as conn:
        conn.execute(SCHEMA)
        conn.executescript(SCHEMA_STORE)
        # 老库迁移：早期订单表没有微信交易号列，补上
        cols = [row[1] for row in conn.execute("PRAGMA table_info(orders)")]
        if "wechat_transaction_id" not in cols:
            conn.execute(
                "ALTER TABLE orders ADD COLUMN wechat_transaction_id TEXT DEFAULT ''"
            )
            cols.append("wechat_transaction_id")
        fulfillment_just_added = "fulfillment_status" not in cols
        for col, ddl in ORDER_NEW_COLUMNS.items():
            if col not in cols:
                conn.execute("ALTER TABLE orders ADD COLUMN %s %s" % (col, ddl))
        if fulfillment_just_added:
            # 存量订单回填：已付款的老订单履约状态对齐到 PAID
            conn.execute(
                "UPDATE orders SET fulfillment_status='PAID'"
                " WHERE status='PAID' AND fulfillment_status='AWAITING_PAYMENT'"
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
            " wechat, car, status, created_at, user_id, recipient, province, city,"
            " district, address_detail, fulfillment_status, status_updated_at)"
            " VALUES (?,?,?,?,?,?,?,'PENDING',?,?,?,?,?,?,?,'AWAITING_PAYMENT',?)",
            (
                order["order_id"],
                order["tier"],
                order["tier_name"],
                order["amount_cents"],
                order["phone"],
                order["wechat"],
                order["car"],
                order["created_at"],
                order.get("user_id"),
                order.get("recipient", ""),
                order.get("province", ""),
                order.get("city", ""),
                order.get("district", ""),
                order.get("address_detail", ""),
                order["created_at"],
            ),
        )


def db_mark_paid(order_id, trade_no):
    now = datetime.now().isoformat(timespec="seconds")
    with _db_lock, db_connect() as conn:
        conn.execute(
            "UPDATE orders SET status='PAID', alipay_trade_no=?, paid_at=?,"
            " fulfillment_status=CASE WHEN fulfillment_status='AWAITING_PAYMENT'"
            " THEN 'PAID' ELSE fulfillment_status END, status_updated_at=?"
            " WHERE order_id=?",
            (trade_no, now, now, order_id),
        )


def db_mark_paid_wechat(order_id, transaction_id):
    now = datetime.now().isoformat(timespec="seconds")
    with _db_lock, db_connect() as conn:
        conn.execute(
            "UPDATE orders SET status='PAID', wechat_transaction_id=?, paid_at=?,"
            " fulfillment_status=CASE WHEN fulfillment_status='AWAITING_PAYMENT'"
            " THEN 'PAID' ELSE fulfillment_status END, status_updated_at=?"
            " WHERE order_id=?",
            (transaction_id, now, now, order_id),
        )


def db_list_orders_by_user(user_id):
    with _db_lock, db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def db_list_all_orders(fulfillment_status=None):
    sql = "SELECT * FROM orders"
    args = ()
    if fulfillment_status:
        sql += " WHERE fulfillment_status = ?"
        args = (fulfillment_status,)
    sql += " ORDER BY created_at DESC"
    with _db_lock, db_connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def db_update_fulfillment(order_id, updates):
    """updates 可含 fulfillment_status / courier / tracking_no；同步刷新 status_updated_at。"""
    allowed = ("fulfillment_status", "courier", "tracking_no")
    fields = {k: v for k, v in updates.items() if k in allowed}
    if not fields:
        return False
    fields["status_updated_at"] = datetime.now().isoformat(timespec="seconds")
    set_clause = ", ".join("%s=?" % k for k in fields)
    with _db_lock, db_connect() as conn:
        cur = conn.execute(
            "UPDATE orders SET %s WHERE order_id=?" % set_clause,
            tuple(fields.values()) + (order_id,),
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------- 账号 / 会话
def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return "pbkdf2_sha256$200000$%s$%s" % (salt.hex(), dk.hex())


def verify_password(password, stored):
    try:
        _algo, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def db_create_user(phone, password_hash):
    try:
        with _db_lock, db_connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (phone, password_hash, created_at) VALUES (?,?,?)",
                (phone, password_hash, datetime.now().isoformat(timespec="seconds")),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def db_get_user_by_phone(phone):
    with _db_lock, db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE phone = ?", (phone,)
        ).fetchone()
    return dict(row) if row else None


def db_get_user_by_id(user_id):
    with _db_lock, db_connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def db_create_session(user_id, kind, ttl_seconds):
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    from datetime import timedelta

    with _db_lock, db_connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, kind, created_at, expires_at)"
            " VALUES (?,?,?,?,?)",
            (
                token,
                user_id,
                kind,
                now.isoformat(timespec="seconds"),
                (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds"),
            ),
        )
    return token


def db_get_session(token):
    if not token:
        return None
    with _db_lock, db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE token = ?", (token,)
        ).fetchone()
        if not row:
            return None
        sess = dict(row)
        if sess["expires_at"] <= datetime.now().isoformat(timespec="seconds"):
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None
    return sess


def db_delete_session(token):
    if not token:
        return
    with _db_lock, db_connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


# ---------------------------------------------------------------- 地址簿
def db_list_addresses(user_id):
    with _db_lock, db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM addresses WHERE user_id = ?"
            " ORDER BY is_default DESC, id ASC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def db_insert_address(user_id, addr, make_default=False):
    with _db_lock, db_connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM addresses WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        is_default = 1 if (make_default or count == 0) else 0
        if is_default:
            conn.execute(
                "UPDATE addresses SET is_default = 0 WHERE user_id = ?", (user_id,)
            )
        cur = conn.execute(
            "INSERT INTO addresses (user_id, recipient, phone, province, city,"
            " district, address_detail, is_default, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                user_id,
                addr.get("recipient", ""),
                addr.get("phone", ""),
                addr.get("province", ""),
                addr.get("city", ""),
                addr.get("district", ""),
                addr.get("address_detail", ""),
                is_default,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        return cur.lastrowid


def db_save_order_address(user_id, addr):
    """下单时把收货地址存进地址簿：完全相同的地址不重复存。"""
    existing = db_list_addresses(user_id)
    keys = ("recipient", "phone", "province", "city", "district", "address_detail")
    for row in existing:
        if all((row.get(k) or "") == addr.get(k, "") for k in keys):
            return row["id"]
    return db_insert_address(user_id, addr)


def db_set_default_address(user_id, address_id):
    with _db_lock, db_connect() as conn:
        cur = conn.execute(
            "SELECT id FROM addresses WHERE id = ? AND user_id = ?",
            (address_id, user_id),
        ).fetchone()
        if not cur:
            return False
        conn.execute(
            "UPDATE addresses SET is_default = 0 WHERE user_id = ?", (user_id,)
        )
        conn.execute(
            "UPDATE addresses SET is_default = 1 WHERE id = ?", (address_id,)
        )
        return True


def db_delete_address(user_id, address_id):
    with _db_lock, db_connect() as conn:
        cur = conn.execute(
            "DELETE FROM addresses WHERE id = ? AND user_id = ?",
            (address_id, user_id),
        )
        return cur.rowcount > 0


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


def binary_response(status, content_type, body, extra_headers=None):
    headers = {"Cache-Control": "no-store"}
    if extra_headers:
        headers.update(extra_headers)
    return status, content_type, body, headers


def with_cookie(resp, name, value, max_age):
    """给 (status, ctype, body) 三元组挂上 Set-Cookie，返回四元组。"""
    status, ctype, body = resp[0], resp[1], resp[2]
    cookie = "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (
        name,
        value,
        max_age,
    )
    if COOKIE_SECURE:
        cookie += "; Secure"
    return status, ctype, body, {"Set-Cookie": cookie}


def parse_cookies(headers):
    raw = (headers or {}).get("cookie", "")
    out = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def current_user(headers):
    """从买家会话 Cookie 解析当前用户；未登录返回 None。"""
    token = parse_cookies(headers).get(USER_COOKIE, "")
    sess = db_get_session(token)
    if not sess or sess.get("kind") != "user" or not sess.get("user_id"):
        return None
    return db_get_user_by_id(sess["user_id"])


def is_admin_request(headers):
    """后台鉴权：Bearer ADMIN_TOKEN 直通，或后台会话 Cookie。"""
    if not ADMIN_TOKEN:
        return False
    auth = (headers or {}).get("authorization", "")
    if auth.startswith("Bearer "):
        if hmac.compare_digest(auth[len("Bearer ") :].strip(), ADMIN_TOKEN):
            return True
    token = parse_cookies(headers).get(ADMIN_COOKIE, "")
    sess = db_get_session(token)
    return bool(sess and sess.get("kind") == "admin")


def public_user(user):
    return {"id": user["id"], "phone": user["phone"], "created_at": user["created_at"]}


def build_timeline(order):
    """履约时间线：主线节点 + 是否到达 / 是否当前。中间节点无独立时间戳，只给
    下单（created_at）、付款（paid_at）与当前状态（status_updated_at）。"""
    current = order.get("fulfillment_status") or "AWAITING_PAYMENT"
    times = {
        "AWAITING_PAYMENT": order.get("created_at"),
        "PAID": order.get("paid_at") or None,
    }
    if current in FULFILLMENT_FLOW:
        times[current] = order.get("status_updated_at") or times.get(current)
    nodes = []
    if current == "AFTER_SALE":
        for key in FULFILLMENT_FLOW:
            nodes.append(
                {
                    "key": key,
                    "label": FULFILLMENT_LABELS[key],
                    "time": times.get(key),
                    "done": False,
                    "current": False,
                }
            )
        nodes.append(
            {
                "key": "AFTER_SALE",
                "label": FULFILLMENT_LABELS["AFTER_SALE"],
                "time": order.get("status_updated_at") or None,
                "done": False,
                "current": True,
            }
        )
        return nodes
    idx = FULFILLMENT_FLOW.index(current)
    for i, key in enumerate(FULFILLMENT_FLOW):
        nodes.append(
            {
                "key": key,
                "label": FULFILLMENT_LABELS[key],
                "time": times.get(key),
                "done": i < idx,
                "current": i == idx,
            }
        )
    return nodes


def order_self_view(order):
    """买家本人视图：完整收货地址、履约与快递信息，手机号脱敏。"""
    return {
        "order_id": order["order_id"],
        "tier": order["tier"],
        "tier_name": order["tier_name"],
        "amount": cents_to_yuan_str(order["amount_cents"]),
        "status": order["status"],
        "fulfillment_status": order.get("fulfillment_status") or "AWAITING_PAYMENT",
        "fulfillment_label": FULFILLMENT_LABELS.get(
            order.get("fulfillment_status") or "AWAITING_PAYMENT", ""
        ),
        "phone": mask_phone(order["phone"]),
        "wechat": order.get("wechat") or "",
        "car": order.get("car") or "",
        "recipient": order.get("recipient") or "",
        "province": order.get("province") or "",
        "city": order.get("city") or "",
        "district": order.get("district") or "",
        "address_detail": order.get("address_detail") or "",
        "courier": order.get("courier") or "",
        "tracking_no": order.get("tracking_no") or "",
        "created_at": order["created_at"],
        "paid_at": order["paid_at"] or None,
        "status_updated_at": order.get("status_updated_at") or None,
        "timeline": build_timeline(order),
    }


def order_admin_view(order):
    """卖家后台视图：完整电话与地址（仅后台鉴权后可见）。"""
    view = order_self_view(order)
    view["phone"] = order["phone"]
    view["alipay_trade_no"] = order.get("alipay_trade_no") or ""
    view["wechat_transaction_id"] = order.get("wechat_transaction_id") or ""
    view["user_id"] = order.get("user_id")
    return view


def address_view(addr):
    return {
        "id": addr["id"],
        "recipient": addr.get("recipient") or "",
        "phone": addr.get("phone") or "",
        "province": addr.get("province") or "",
        "city": addr.get("city") or "",
        "district": addr.get("district") or "",
        "address_detail": addr.get("address_detail") or "",
        "is_default": bool(addr.get("is_default")),
        "created_at": addr.get("created_at"),
    }


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


# ---------------------------------------------------------------- QR 二维码（纯标准库）
# 字节模式 + ECC 等级 M，版本 1–10 自动选小。微信 Native 的 code_url 约 40 字符，
# V4-M 即可容纳。实现按 ISO/IEC 18004：GF(256) Reed-Solomon、交织、掩码罚分选优。
_QR_ECC_M = {
    # version: (每块 ECC 码字数, [(块数, 每块数据码字数), ...])
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
    7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
_QR_ALIGN_CENTERS = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}
_QR_REMAINDER_BITS = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0, 9: 0, 10: 0}

_QR_GF_EXP = [0] * 512
_QR_GF_LOG = [0] * 256
_x = 1
for _i in range(255):
    _QR_GF_EXP[_i] = _x
    _QR_GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _QR_GF_EXP[_i] = _QR_GF_EXP[_i - 255]


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _QR_GF_EXP[_QR_GF_LOG[a] + _QR_GF_LOG[b]]


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(len(result)):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < len(result):
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _poly_mod(value, gen):
    deg = gen.bit_length() - 1
    v = value
    while v.bit_length() - 1 >= deg:
        v ^= gen << ((v.bit_length() - 1) - deg)
    return v


def _qr_format_bits(mask):
    data = (0b00 << 3) | mask  # ECC 等级 M 的指示位为 00
    rem = _poly_mod(data << 10, 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _qr_version_bits(version):
    rem = _poly_mod(version << 12, 0x1F25)
    return (version << 12) | rem


def _qr_data_codewords(data_bytes, version):
    """数据码字 → 分块纠错 → 交织，返回完整码字序列。"""
    ecc_len, block_spec = _QR_ECC_M[version]
    total_data = sum(n * c for n, c in block_spec)
    capacity_bits = total_data * 8
    bits = []

    def append(val, length):
        for i in range(length - 1, -1, -1):
            bits.append((val >> i) & 1)

    append(0b0100, 4)  # 字节模式
    append(len(data_bytes), 8 if version <= 9 else 16)
    for b in data_bytes:
        append(b, 8)
    append(0, min(4, capacity_bits - len(bits)))  # 终止符
    append(0, (-len(bits)) % 8)  # 对齐到字节
    pad = 0xEC
    while len(bits) < capacity_bits:
        append(pad, 8)
        pad ^= 0xEC ^ 0x11
    codewords = [
        sum(bits[i + j] << (7 - j) for j in range(8))
        for i in range(0, capacity_bits, 8)
    ]
    blocks, eccs, k = [], [], 0
    divisor = _rs_divisor(ecc_len)
    for num, dcw in block_spec:
        for _ in range(num):
            block = codewords[k : k + dcw]
            k += dcw
            blocks.append(block)
            eccs.append(_rs_remainder(block, divisor))
    result = []
    for i in range(max(len(b) for b in blocks)):
        for b in blocks:
            if i < len(b):
                result.append(b[i])
    for i in range(ecc_len):
        for e in eccs:
            result.append(e[i])
    return result


def _qr_base_matrix(version):
    """画功能图形（定位/分隔/校正/定时/版本信息），返回 (modules, reserved)。"""
    size = version * 4 + 17
    modules = [[0] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    def setf(r, c, v):
        if 0 <= r < size and 0 <= c < size:
            modules[r][c] = v
            reserved[r][c] = True

    for r0, c0 in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                if 0 <= dr <= 6 and 0 <= dc <= 6:
                    v = (
                        1
                        if (
                            dr in (0, 6)
                            or dc in (0, 6)
                            or (2 <= dr <= 4 and 2 <= dc <= 4)
                        )
                        else 0
                    )
                else:
                    v = 0  # 分隔符（白边）
                setf(r0 + dr, c0 + dc, v)
    for i in range(8, size - 8):  # 定时图形
        v = 1 if i % 2 == 0 else 0
        setf(6, i, v)
        setf(i, 6, v)
    centers = _QR_ALIGN_CENTERS[version]  # 校正图形：只跳过与定位图形重叠的三角组合
    for r in centers:
        for c in centers:
            if (
                (r == centers[0] and c == centers[0])
                or (r == centers[0] and c == centers[-1])
                or (r == centers[-1] and c == centers[0])
            ):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    v = 1 if (max(abs(dr), abs(dc)) == 2 or (dr == 0 and dc == 0)) else 0
                    setf(r + dr, c + dc, v)
    for i in range(9):  # 保留格式信息区
        if i != 6:
            reserved[8][i] = True
            reserved[i][8] = True
    for i in range(8):
        reserved[8][size - 1 - i] = True
        reserved[size - 1 - i][8] = True
    modules[size - 8][8] = 1  # 暗模块
    reserved[size - 8][8] = True
    if version >= 7:  # 版本信息
        bits = _qr_version_bits(version)
        for i in range(18):
            v = (bits >> i) & 1
            r1, c1 = i // 3, size - 11 + i % 3
            r2, c2 = size - 11 + i % 3, i // 3
            modules[r1][c1] = v
            reserved[r1][c1] = True
            modules[r2][c2] = v
            reserved[r2][c2] = True
    return modules, reserved


def _qr_mask_bit(mask, r, c):
    if mask == 0:
        return (r + c) % 2 == 0
    if mask == 1:
        return r % 2 == 0
    if mask == 2:
        return c % 3 == 0
    if mask == 3:
        return (r + c) % 3 == 0
    if mask == 4:
        return (r // 2 + c // 3) % 2 == 0
    if mask == 5:
        return (r * c) % 2 + (r * c) % 3 == 0
    if mask == 6:
        return ((r * c) % 2 + (r * c) % 3) % 2 == 0
    return ((r + c) % 2 + (r * c) % 3) % 2 == 0  # mask 7


def _qr_set_format(modules, mask):
    size = len(modules)
    bits = _qr_format_bits(mask)
    for i in range(6):
        modules[i][8] = (bits >> i) & 1
    modules[7][8] = (bits >> 6) & 1
    modules[8][8] = (bits >> 7) & 1
    modules[8][7] = (bits >> 8) & 1
    for i in range(9, 15):
        modules[8][14 - i] = (bits >> i) & 1
    for i in range(8):
        modules[8][size - 1 - i] = (bits >> i) & 1
    for i in range(8, 15):
        modules[size - 15 + i][8] = (bits >> i) & 1
    modules[size - 8][8] = 1


def _qr_penalty(modules):
    size = len(modules)
    penalty = 0
    lines = list(modules) + [[modules[r][c] for r in range(size)] for c in range(size)]
    for line in lines:  # 规则 1：同色游程 ≥5
        run_color, run_len = line[0], 1
        for v in line[1:]:
            if v == run_color:
                run_len += 1
            else:
                if run_len >= 5:
                    penalty += run_len - 2
                run_color, run_len = v, 1
        if run_len >= 5:
            penalty += run_len - 2
    for r in range(size - 1):  # 规则 2：2x2 同色块
        for c in range(size - 1):
            v = modules[r][c]
            if v == modules[r][c + 1] == modules[r + 1][c] == modules[r + 1][c + 1]:
                penalty += 3
    for line in lines:  # 规则 3：1011101 且一侧有 4 个白模块
        bits = 0
        for idx, v in enumerate(line):
            bits = ((bits << 1) & 0x7FF) | v
            if idx >= 10 and bits in (0b10111010000, 0b00001011101):
                penalty += 40
    dark = sum(sum(row) for row in modules)  # 规则 4：黑白比例偏离 50%
    total = size * size
    k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
    penalty += k * 10
    return penalty


def qr_encode(text):
    """返回 (version, mask, matrix)；matrix 为 0/1 二维列表。"""
    data = text.encode("utf-8")
    version = None
    for v in range(1, 11):
        _ecc, spec = _QR_ECC_M[v]
        total_data = sum(n * c for n, c in spec)
        len_bits = 8 if v <= 9 else 16
        if 4 + len_bits + 8 * len(data) <= total_data * 8:
            version = v
            break
    if version is None:
        raise ValueError("qr_text_too_long")
    codewords = _qr_data_codewords(data, version)
    bits = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)
    bits += [0] * _QR_REMAINDER_BITS[version]
    base, reserved = _qr_base_matrix(version)
    size = len(base)
    best = None
    for mask in range(8):
        modules = [row[:] for row in base]
        i = 0
        col = size - 1
        upward = True
        while col > 0:
            if col == 6:
                col -= 1
            rows = range(size - 1, -1, -1) if upward else range(size)
            for r in rows:
                for c in (col, col - 1):
                    if not reserved[r][c]:
                        bit = bits[i] if i < len(bits) else 0
                        if _qr_mask_bit(mask, r, c):
                            bit ^= 1
                        modules[r][c] = bit
                        i += 1
            col -= 2
            upward = not upward
        _qr_set_format(modules, mask)
        score = _qr_penalty(modules)
        if best is None or score < best[0]:
            best = (score, mask, modules)
    return version, best[1], best[2]


def _png_grayscale(width, height, rows):
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(typ, payload):
        return (
            struct.pack(">I", len(payload))
            + typ
            + payload
            + struct.pack(">I", binascii.crc32(typ + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def qr_png(text, scale=6, border=4):
    _version, _mask, matrix = qr_encode(text)
    n = len(matrix)
    size = (n + border * 2) * scale
    rows = []
    for y in range(size):
        my = y // scale - border
        row = bytearray()
        for x in range(size):
            mx = x // scale - border
            dark = 0 <= my < n and 0 <= mx < n and matrix[my][mx] == 1
            row.append(0 if dark else 255)
        rows.append(bytes(row))
    return _png_grayscale(size, size, rows)


def handle_qr(query):
    text = (query or {}).get("text", "")
    if not text:
        return json_response(400, {"error": "text_required"})
    if len(text.encode("utf-8")) > 213:
        return json_response(400, {"error": "qr_text_too_long"})
    try:
        png = qr_png(text)
    except ValueError:
        return json_response(400, {"error": "qr_text_too_long"})
    return binary_response(200, "image/png", png)


# ---------------------------------------------------------------- 站内客服：关键词秒回规则
# 与原前端智能问答库逐字一致（从 index.html 的 QA 数组搬移），顺序即匹配优先级：
# 客户消息转小写后按顺序做子串匹配，命中第一条即以 agent/auto 身份秒回；未命中不自动回复。
# 规则可写三元组 (关键词, 文案, 语境词)：关键词命中之外还须命中任一语境词才触发——
# OTA 条用它防裸「升级/更新」抢走车机版本类问题（问车机要不要升级 ≠ 问模块固件升级）。
CHAT_QA = [
    (['满血'], '满血与否取决于车自身的 FSD 版本和特斯拉的推送进度（市面说的「工厂版」指保留了特定出厂软件的车），和本模块无关。模块对已购 FSD 的车只做红绿灯识别激活和地区限制解锁，不提供 FSD 本体激活，也不承诺满血。'),
    (['价格', '多少钱', '多少元', '价位'], '组装套件价格（空白硬件，固件免费下载自刷，OTA 永久免费）：\n老款 Model 3/Y：基础 ¥399 / EAP ¥499 / FSD ¥599\n新款焕新版 / Juniper：基础 ¥499 / EAP ¥599 / FSD ¥699\n硬件是同一套，按车已购的订阅选档。点页面价格区的「立即购买」就能在线下单付款。'),
    (['rev', '选包', '版本包', '刷不上'], '固件分 REV1.X 和 REV3.X 两个包，看主板 ESP32-P4 芯片版本选，不通用。买成品按卖家标注；自己买板看芯片丝印，或问板卡卖家。'),
    (['首刷', '第一次', '刷机', '怎么刷', 'fullflash', '整刷'], '第一次刷机用 FULLFLASH 整刷包：电脑 Chrome 打开在线刷机工具（esp.huhn.me 或 adafruit WebSerial ESPTool），主板 USB 连电脑，选 merged 整刷包从地址 0x0 写入。下载和校验值都在本站下载区。'),
    (['ota', '升级', '更新'], '后续升级只能用 OTA 包：在模块网页仪表盘的固件更新入口上传，过程不要断电。本站 OTA 升级永久免费。注意：千万别拿 FULLFLASH 整刷包当更新包刷，那是已知的模块重启故障原因。', ['固件', '模块', 'ota', '更新包', '升级包', '刷机包', '仪表盘']),
    (['重启', '闪退', '断开', '不稳定'], '模块自己重启先查两件事：①是不是把整刷包当更新包刷了——重刷对应版本的 OTA 包纠正；②供电是否稳定——12V 转 5V 降压模块输出不足也会这样。还不行就在下方留手机号，客服联系你排查。'),
    (['eap', 'fsd', '没买', '订阅'], '没买 EAP/FSD 也能用基础功能（脱手提醒解除、持续 AP、实验变道、电池预热等）。但召唤、红绿灯识别（TLSSC）需要车本身已订阅 EAP/FSD 才生效——模块只解锁地区与距离限制，不破解付费功能。'),
    (['召唤', '红绿灯', 'tlssc'], '召唤距离放开（±85m）需要车已购 EAP/FSD；红绿灯识别（TLSSC）需要车已购 FSD，模块负责激活和地区解锁，不提供功能本体。'),
    (['接线', '破线', '安装', 'x179', '针脚'], '走后排 X179 诊断接头对插，不用破线。但接线前必须先进车辆服务模式的 CAN Port 页面，逐脚核对针脚对应的总线名称——不同年份线束布局至少有 4 种，不能只按年份猜。详细接线表在本站「安装要点」区。'),
    (['保修', '质保'], '先说结论：整车还在特斯拉基本保修期内（4 年或 8 万公里，以先到为准）的，建议先别装，等出保后再考虑。安装涉及车辆 CAN 总线操作，特斯拉有可能据此拒绝相关部件甚至整车保修；已出保的车辆也请自行评估风险。'),
    (['版本', '2026', '能用', '适配', '车型', 'model', '车机', '升级系统', '系统升级', '能装'], '适用于 Model 3 / Y，车机软件需要 ≥ 2026.20。低于 2026.20 功能不正常、不能用，请先升级车机。下单前先在车机里确认版本号。'),
    (['下单', '购买', '支付', '付款', '发货', '怎么买'], '下单方式：在本站价格区点对应档位的「立即购买」，填收货地址后用微信扫码或支付宝付款；付款成功后卖家备货发货，进度和快递单号在本站「我的订单」页随时可查。不确定适配先在这里问我，确认了再下单。'),
    (['电话', '微信', '联系', '人工', '真人'], '店主在线（8:00–23:00）时就在这个对话里直接回复你，约 1 分钟，不用跳出去。想留个联系方式慢慢聊，也可以在下方表单留手机号或微信号，店主会加你。'),
    (['变道', '匝道', 'noa'], '高速上自己变道下匝道是 NOA 的活，得车本身买了 EAP 才有——模块只解锁限制，不给功能本体。模块自带的实验变道是基础 AP 下打转向灯辅助变道，定位是非高速场景，别当 NOA 用。'),
    (['饰板', '放哪', '藏哪', '装哪'], '可以放饰板里，不用露在外面：P4 方案接后排中央扶手后方的 X179 口，那个口本身就在饰板内；老款车也可以走副驾脚窝中控台侧饰板后的对插口（和加装仪表同一个位置），空腔更大、更好藏。都不用破线。'),
    (['t2', '一体板', 't-2can', 'lilygo'], '有，T2CAN 是同作者的一体板方案：一块 LilyGO T-2CAN 板集成双路 CAN 和电源，螺丝端子免焊接、12V 直供，比 P4 散件好装好藏。固件 v3.30.3 中文版已在本站固件区免费下载（只适配标准版 H784，FD 版不行）。套件实测中、即将上架，可先在下方表单留言登记。注意：标准 Model 3/Y 只接两路总线，杀手模式和高级 EAP 按接法二选一；它还独有 Model S/X（MCU2）支持。'),
    (['车规', '集成', '工业级'], '分方案说：T2CAN 是一体板，一块板集成双路 CAN 和电源；P4 是散件组装（主板+收发模块+降压）。实话说，两个都不是车规级认证产品——T2 官方定位是工业级 CAN 方案（12–24V 宽压、带隔离），P4 用开发板+工业收发模块，都没拿 AEC-Q100 这类车规认证。车上 12V 环境正常用没问题。'),
    (['咸鱼', '转转', '担保', '平台交易'], '闲鱼不走了，走转转平台担保交易，一样有平台担保、验货后再放款。留个手机号或微信，确认车型、车机版本（≥2026.20）和订阅档后，客服帮你安排转转担保下单。'),
    (['你好', '您好', '在吗', 'hi', 'hello'], '你好，我是阿童木智能客服。价格、适配、刷机、保修都可以直接问我。'),
]


def chat_auto_answer(text):
    """命中关键词返回自动回复文案，未命中返回 None。

    三元组规则带语境词：关键词命中后，还须至少命中一个语境词才触发，
    否则继续往下匹配后面的规则。
    """
    s = (text or "").lower()
    for rule in CHAT_QA:
        keywords, answer = rule[0], rule[1]
        context = rule[2] if len(rule) > 2 else None
        if context is not None and not any(c in s for c in context):
            continue
        for kw in keywords:
            if kw in s:
                return answer
    return None
# ---------------------------------------------------------------- 站内客服：数据层
CHAT_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
CHAT_TEXT_MAX = 2000
CHAT_RATE_LIMIT = 20  # 同一会话每分钟最多 20 条客户消息
CHAT_RATE_WINDOW_SECONDS = 60


def _chat_now():
    return datetime.now().isoformat(timespec="seconds")


def chat_message_view(row):
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "sender": row["sender"],
        "kind": row["kind"],
        "text": row["text"],
        "created_at": row["created_at"],
    }


def db_chat_touch_session(session_id):
    now = _chat_now()
    with _db_lock, db_connect() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (session_id, first_seen, last_seen)"
            " VALUES (?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET last_seen = excluded.last_seen",
            (session_id, now, now),
        )


def db_chat_add_message(session_id, sender, kind, text):
    now = _chat_now()
    with _db_lock, db_connect() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (session_id, first_seen, last_seen)"
            " VALUES (?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET last_seen = excluded.last_seen",
            (session_id, now, now),
        )
        cur = conn.execute(
            "INSERT INTO chat_messages (session_id, sender, kind, text, created_at)"
            " VALUES (?,?,?,?,?)",
            (session_id, sender, kind, text, now),
        )
        row = conn.execute(
            "SELECT * FROM chat_messages WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


def db_chat_poll(session_id, after_id):
    with _db_lock, db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE session_id = ? AND id > ?"
            " ORDER BY id ASC",
            (session_id, after_id),
        ).fetchall()
    return [dict(r) for r in rows]


def db_chat_recent_customer_count(session_id):
    from datetime import timedelta

    cutoff = (datetime.now() - timedelta(seconds=CHAT_RATE_WINDOW_SECONDS)).isoformat(
        timespec="seconds"
    )
    with _db_lock, db_connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM chat_messages"
            " WHERE session_id = ? AND sender = 'customer' AND created_at >= ?",
            (session_id, cutoff),
        ).fetchone()[0]


def db_chat_inbox():
    """会话列表：最后一条消息预览 + 未读数（id 大于 agent_last_read_id 的客户消息）。"""
    with _db_lock, db_connect() as conn:
        sessions = conn.execute(
            "SELECT * FROM chat_sessions ORDER BY last_seen DESC"
        ).fetchall()
        out = []
        for sess in sessions:
            sid = sess["session_id"]
            last = conn.execute(
                "SELECT * FROM chat_messages WHERE session_id = ?"
                " ORDER BY id DESC LIMIT 1",
                (sid,),
            ).fetchone()
            unread = conn.execute(
                "SELECT COUNT(*) FROM chat_messages"
                " WHERE session_id = ? AND sender = 'customer' AND id > ?",
                (sid, sess["agent_last_read_id"] or 0),
            ).fetchone()[0]
            out.append(
                {
                    "session_id": sid,
                    "first_seen": sess["first_seen"],
                    "last_seen": sess["last_seen"],
                    "customer_note": sess["customer_note"] or "",
                    "unread": unread,
                    "last_message": (
                        {
                            "id": last["id"],
                            "sender": last["sender"],
                            "kind": last["kind"],
                            "text": last["text"],
                            "created_at": last["created_at"],
                        }
                        if last
                        else None
                    ),
                }
            )
    return out


def db_chat_thread(session_id):
    """完整线程；同时把 agent_last_read_id 推进到当前最大客户消息 id。会话不存在返回 None。"""
    with _db_lock, db_connect() as conn:
        sess = conn.execute(
            "SELECT * FROM chat_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not sess:
            return None
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
        max_customer = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM chat_messages"
            " WHERE session_id = ? AND sender = 'customer'",
            (session_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE chat_sessions SET agent_last_read_id = ? WHERE session_id = ?",
            (max_customer, session_id),
        )
    return [dict(r) for r in rows]


def db_chat_session_exists(session_id):
    with _db_lock, db_connect() as conn:
        row = conn.execute(
            "SELECT session_id FROM chat_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------- 站内客服：处理函数
def _valid_chat_session_id(session_id):
    return bool(CHAT_SESSION_ID_RE.fullmatch(session_id or ""))


def _extract_chat_text(data):
    text = str((data or {}).get("text", "") or "").strip()
    if not text:
        return None, json_response(
            400, {"error": "text_required", "message": "消息内容不能为空"}
        )
    if len(text) > CHAT_TEXT_MAX:
        return None, json_response(
            400,
            {
                "error": "text_too_long",
                "message": "单条消息最多 %d 字" % CHAT_TEXT_MAX,
            },
        )
    return text, None


def handle_chat_send(data):
    if not isinstance(data, dict):
        return json_response(400, {"error": "invalid_json"})
    session_id = str(data.get("session_id", "") or "").strip()
    if not _valid_chat_session_id(session_id):
        return json_response(
            400, {"error": "invalid_session_id", "message": "会话 ID 不合法"}
        )
    text, err = _extract_chat_text(data)
    if err:
        return err
    if db_chat_recent_customer_count(session_id) >= CHAT_RATE_LIMIT:
        return json_response(
            429,
            {"error": "rate_limited", "message": "发送太频繁了，请稍等一分钟再发"},
        )
    msg = db_chat_add_message(session_id, "customer", "human", text)
    auto = None
    answer = chat_auto_answer(text)
    if answer:
        auto_row = db_chat_add_message(session_id, "agent", "auto", answer)
        auto = chat_message_view(auto_row)
    log.info("chat msg session=%s id=%s auto=%s", session_id, msg["id"], bool(auto))
    return json_response(
        201, {"message": chat_message_view(msg), "auto_reply": auto}
    )


def handle_chat_poll(query):
    session_id = str((query or {}).get("session_id", "") or "").strip()
    if not _valid_chat_session_id(session_id):
        return json_response(400, {"error": "invalid_session_id"})
    try:
        after_id = int((query or {}).get("after_id", 0) or 0)
    except (TypeError, ValueError):
        after_id = 0
    msgs = db_chat_poll(session_id, after_id)
    last_id = msgs[-1]["id"] if msgs else after_id
    return json_response(
        200,
        {"messages": [chat_message_view(m) for m in msgs], "last_id": last_id},
    )


def handle_admin_chat_inbox(headers):
    guard = _admin_guard(headers)
    if guard:
        return guard
    return json_response(200, {"sessions": db_chat_inbox()})


def handle_admin_chat_thread(headers, session_id):
    guard = _admin_guard(headers)
    if guard:
        return guard
    if not _valid_chat_session_id(session_id):
        return json_response(400, {"error": "invalid_session_id"})
    rows = db_chat_thread(session_id)
    if rows is None:
        return json_response(404, {"error": "session_not_found"})
    return json_response(
        200,
        {
            "session_id": session_id,
            "messages": [chat_message_view(r) for r in rows],
        },
    )


def handle_admin_chat_reply(data, headers):
    guard = _admin_guard(headers)
    if guard:
        return guard
    if not isinstance(data, dict):
        return json_response(400, {"error": "invalid_json"})
    session_id = str(data.get("session_id", "") or "").strip()
    if not _valid_chat_session_id(session_id):
        return json_response(400, {"error": "invalid_session_id"})
    text, err = _extract_chat_text(data)
    if err:
        return err
    if not db_chat_session_exists(session_id):
        return json_response(404, {"error": "session_not_found"})
    msg = db_chat_add_message(session_id, "agent", "human", text)
    log.info("chat reply session=%s id=%s", session_id, msg["id"])
    return json_response(201, {"message": chat_message_view(msg)})



# ---------------------------------------------------------------- 业务处理（框架无关）
def handle_health():
    return json_response(200, {"ok": True, "service": "p4can-pay"})


def _extract_address(data):
    return {
        k: str(data.get(k, "") or "").strip()
        for k in ("recipient", "province", "city", "district", "address_detail")
    }


def handle_create_order(data, headers=None):
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

    addr = _extract_address(data)
    missing = [k for k in ("recipient", "address_detail") if not addr[k]]
    if missing:
        return json_response(
            400,
            {
                "error": "address_required",
                "message": "下单需填写收货人与详细收货地址",
                "missing": missing,
            },
        )

    user = current_user(headers) if headers else None
    info = TIERS[tier]
    order = {
        "order_id": new_order_id(),
        "tier": tier,
        "tier_name": info["name"],
        "amount_cents": info["amount_cents"],
        "phone": phone,
        "wechat": wechat,
        "car": car,
        "user_id": user["id"] if user else None,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        **addr,
    }
    db_insert_order(order)
    if user:
        book_addr = dict(addr)
        book_addr["phone"] = phone
        db_save_order_address(user["id"], book_addr)
    log.info("order created: %s tier=%s amount=%s", order["order_id"], tier, info["amount_cents"])
    return json_response(
        201,
        {
            "order_id": order["order_id"],
            "tier": tier,
            "tier_name": info["name"],
            "amount": cents_to_yuan_str(info["amount_cents"]),
            "status": "PENDING",
            "fulfillment_status": "AWAITING_PAYMENT",
        },
    )


def handle_get_order(order_id):
    order = db_get_order(order_id)
    if not order:
        return json_response(404, {"error": "order_not_found"})
    fulfillment = order.get("fulfillment_status") or "AWAITING_PAYMENT"
    return json_response(
        200,
        {
            "order_id": order["order_id"],
            "tier": order["tier"],
            "tier_name": order["tier_name"],
            "amount": cents_to_yuan_str(order["amount_cents"]),
            "status": order["status"],
            "fulfillment_status": fulfillment,
            "fulfillment_label": FULFILLMENT_LABELS.get(fulfillment, ""),
            "phone": mask_phone(order["phone"]),
            "created_at": order["created_at"],
            "paid_at": order["paid_at"] or None,
        },
    )


# ---------------------------------------------------------------- 买家账号
def _valid_phone(phone):
    return bool(re.fullmatch(r"1\d{10}", phone or ""))


def handle_register(data):
    if not isinstance(data, dict):
        return json_response(400, {"error": "invalid_json"})
    phone = str(data.get("phone", "")).strip()
    password = str(data.get("password", "") or "")
    if not _valid_phone(phone):
        return json_response(400, {"error": "invalid_phone"})
    if len(password) < 6:
        return json_response(
            400, {"error": "password_too_short", "message": "密码至少 6 位"}
        )
    if db_get_user_by_phone(phone):
        return json_response(
            409, {"error": "phone_registered", "message": "该手机号已注册，请直接登录"}
        )
    user_id = db_create_user(phone, hash_password(password))
    if user_id is None:
        return json_response(409, {"error": "phone_registered"})
    token = db_create_session(user_id, "user", SESSION_TTL_SECONDS)
    user = db_get_user_by_id(user_id)
    return with_cookie(
        json_response(201, {"user": public_user(user)}),
        USER_COOKIE,
        token,
        SESSION_TTL_SECONDS,
    )


def handle_login(data):
    if not isinstance(data, dict):
        return json_response(400, {"error": "invalid_json"})
    phone = str(data.get("phone", "")).strip()
    password = str(data.get("password", "") or "")
    user = db_get_user_by_phone(phone)
    if not user or not verify_password(password, user["password_hash"]):
        return json_response(
            401, {"error": "invalid_credentials", "message": "手机号或密码不正确"}
        )
    token = db_create_session(user["id"], "user", SESSION_TTL_SECONDS)
    return with_cookie(
        json_response(200, {"user": public_user(user)}),
        USER_COOKIE,
        token,
        SESSION_TTL_SECONDS,
    )


def handle_logout(headers):
    token = parse_cookies(headers).get(USER_COOKIE, "")
    db_delete_session(token)
    return with_cookie(json_response(200, {"ok": True}), USER_COOKIE, "", 0)


def handle_me(headers):
    user = current_user(headers)
    if not user:
        return json_response(401, {"error": "login_required"})
    return json_response(200, {"user": public_user(user)})


def handle_my_orders(headers):
    user = current_user(headers)
    if not user:
        return json_response(401, {"error": "login_required"})
    orders = db_list_orders_by_user(user["id"])
    return json_response(200, {"orders": [order_self_view(o) for o in orders]})


def handle_my_addresses(headers):
    user = current_user(headers)
    if not user:
        return json_response(401, {"error": "login_required"})
    addrs = db_list_addresses(user["id"])
    return json_response(200, {"addresses": [address_view(a) for a in addrs]})


def handle_add_address(data, headers):
    user = current_user(headers)
    if not user:
        return json_response(401, {"error": "login_required"})
    if not isinstance(data, dict):
        return json_response(400, {"error": "invalid_json"})
    addr = _extract_address(data)
    addr["phone"] = str(data.get("phone", "") or "").strip()
    missing = [k for k in ("recipient", "address_detail") if not addr[k]]
    if missing:
        return json_response(400, {"error": "address_required", "missing": missing})
    if addr["phone"] and not _valid_phone(addr["phone"]):
        return json_response(400, {"error": "invalid_phone"})
    addr_id = db_insert_address(
        user["id"], addr, make_default=bool(data.get("is_default"))
    )
    addrs = db_list_addresses(user["id"])
    saved = next((a for a in addrs if a["id"] == addr_id), None)
    return json_response(201, {"address": address_view(saved) if saved else None})


def handle_set_default_address(headers, address_id):
    user = current_user(headers)
    if not user:
        return json_response(401, {"error": "login_required"})
    if not db_set_default_address(user["id"], address_id):
        return json_response(404, {"error": "address_not_found"})
    return json_response(200, {"ok": True})


def handle_delete_address(headers, address_id):
    user = current_user(headers)
    if not user:
        return json_response(401, {"error": "login_required"})
    if not db_delete_address(user["id"], address_id):
        return json_response(404, {"error": "address_not_found"})
    return json_response(200, {"ok": True})


# ---------------------------------------------------------------- 游客订单追踪
def handle_track(order_id, query):
    order = db_get_order(order_id)
    if not order:
        return json_response(404, {"error": "order_not_found"})
    phone = str((query or {}).get("phone", "")).strip()
    if not phone:
        return json_response(
            400, {"error": "phone_required", "message": "需提供下单手机号（后四位即可）"}
        )
    if len(phone) == 4:
        matched = order["phone"].endswith(phone)
    elif _valid_phone(phone):
        matched = order["phone"] == phone
    else:
        matched = False
    if not matched:
        return json_response(403, {"error": "phone_mismatch"})
    fulfillment = order.get("fulfillment_status") or "AWAITING_PAYMENT"
    return json_response(
        200,
        {
            "order_id": order["order_id"],
            "tier_name": order["tier_name"],
            "amount": cents_to_yuan_str(order["amount_cents"]),
            "status": order["status"],
            "fulfillment_status": fulfillment,
            "fulfillment_label": FULFILLMENT_LABELS.get(fulfillment, ""),
            "courier": order.get("courier") or "",
            "tracking_no": order.get("tracking_no") or "",
            "province": order.get("province") or "",
            "city": order.get("city") or "",
            "created_at": order["created_at"],
            "paid_at": order["paid_at"] or None,
            "timeline": build_timeline(order),
        },
    )


# ---------------------------------------------------------------- 卖家后台
def handle_admin_login(data):
    if not ADMIN_TOKEN:
        return json_response(
            503, {"error": "admin_not_configured", "message": "后台未启用（未配置 ADMIN_TOKEN）"}
        )
    if not isinstance(data, dict):
        return json_response(400, {"error": "invalid_json"})
    token = str(data.get("token", "") or "")
    if not hmac.compare_digest(token, ADMIN_TOKEN):
        return json_response(401, {"error": "invalid_token", "message": "口令不正确"})
    sess = db_create_session(None, "admin", ADMIN_SESSION_TTL_SECONDS)
    return with_cookie(
        json_response(200, {"ok": True, "admin": True}),
        ADMIN_COOKIE,
        sess,
        ADMIN_SESSION_TTL_SECONDS,
    )


def handle_admin_logout(headers):
    token = parse_cookies(headers).get(ADMIN_COOKIE, "")
    db_delete_session(token)
    return with_cookie(json_response(200, {"ok": True}), ADMIN_COOKIE, "", 0)


def handle_admin_session(headers):
    if not ADMIN_TOKEN:
        return json_response(503, {"error": "admin_not_configured"})
    return json_response(200, {"admin": is_admin_request(headers)})


def _admin_guard(headers):
    """返回 None 表示放行，否则返回错误响应。"""
    if not ADMIN_TOKEN:
        return json_response(503, {"error": "admin_not_configured"})
    if not is_admin_request(headers):
        return json_response(401, {"error": "admin_required", "message": "请先登录后台"})
    return None


def handle_admin_orders(headers, query):
    guard = _admin_guard(headers)
    if guard:
        return guard
    status = str((query or {}).get("status", "")).strip()
    if status and status not in FULFILLMENT_STATUSES:
        return json_response(400, {"error": "invalid_status"})
    orders = db_list_all_orders(status or None)
    return json_response(200, {"orders": [order_admin_view(o) for o in orders]})


def handle_admin_fulfillment(order_id, data, headers):
    guard = _admin_guard(headers)
    if guard:
        return guard
    if not isinstance(data, dict):
        return json_response(400, {"error": "invalid_json"})
    order = db_get_order(order_id)
    if not order:
        return json_response(404, {"error": "order_not_found"})
    updates = {}
    if "fulfillment_status" in data:
        fs = str(data.get("fulfillment_status", "")).strip()
        if fs not in FULFILLMENT_STATUSES:
            return json_response(
                400,
                {
                    "error": "invalid_fulfillment_status",
                    "allowed": FULFILLMENT_STATUSES,
                },
            )
        updates["fulfillment_status"] = fs
    for key in ("courier", "tracking_no"):
        if key in data:
            updates[key] = str(data.get(key, "") or "").strip()
    if not updates:
        return json_response(400, {"error": "nothing_to_update"})
    db_update_fulfillment(order_id, updates)
    log.info("admin fulfillment update: %s -> %s", order_id, updates)
    return json_response(200, {"order": order_admin_view(db_get_order(order_id))})


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
    # 前端可显式指定通道（pay_type=native/h5）：手机端 H5 权限未开通时，
    # 前端用 pay_type=native 强制走扫码；不传或非法值保持按 UA 选择的默认行为。
    force_type = str(data.get("pay_type", "")).strip().lower()
    if force_type == "native":
        is_mobile = False
    elif force_type == "h5":
        is_mobile = True
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
def _dispatch_impl(
    method,
    path,
    json_body=None,
    form=None,
    user_agent="",
    headers=None,
    raw_body=b"",
    remote_addr="",
    query=None,
):
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    query = query or {}
    client_ip = headers.get("x-forwarded-for", "").split(",")[0].strip() or remote_addr
    if method == "GET" and path == "/api/health":
        return handle_health()
    if method == "POST" and path == "/api/order":
        return handle_create_order(json_body, headers)
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
    # ---- 商城：买家账号 ----
    if method == "POST" and path == "/api/register":
        return handle_register(json_body)
    if method == "POST" and path == "/api/login":
        return handle_login(json_body)
    if method == "POST" and path == "/api/logout":
        return handle_logout(headers)
    if method == "GET" and path == "/api/me":
        return handle_me(headers)
    if method == "GET" and path == "/api/my/orders":
        return handle_my_orders(headers)
    if method == "GET" and path == "/api/my/addresses":
        return handle_my_addresses(headers)
    if method == "POST" and path == "/api/my/addresses":
        return handle_add_address(json_body, headers)
    m = re.fullmatch(r"/api/my/addresses/(\d+)/default", path or "")
    if method == "POST" and m:
        return handle_set_default_address(headers, int(m.group(1)))
    m = re.fullmatch(r"/api/my/addresses/(\d+)", path or "")
    if method == "DELETE" and m:
        return handle_delete_address(headers, int(m.group(1)))
    # ---- 商城：游客追踪 ----
    m = re.fullmatch(r"/api/track/([A-Za-z0-9_-]+)", path or "")
    if method == "GET" and m:
        return handle_track(m.group(1), query)
    # ---- 商城：卖家后台 ----
    if method == "POST" and path == "/api/admin/login":
        return handle_admin_login(json_body)
    if method == "POST" and path == "/api/admin/logout":
        return handle_admin_logout(headers)
    if method == "GET" and path == "/api/admin/session":
        return handle_admin_session(headers)
    if method == "GET" and path == "/api/admin/orders":
        return handle_admin_orders(headers, query)
    m = re.fullmatch(r"/api/admin/order/([A-Za-z0-9_-]+)/fulfillment", path or "")
    if method == "POST" and m:
        return handle_admin_fulfillment(m.group(1), json_body, headers)
    # ---- 离线二维码 ----
    if method == "GET" and path == "/api/qr":
        return handle_qr(query)
    # ---- 站内客服 ----
    if method == "POST" and path == "/api/chat/send":
        return handle_chat_send(json_body)
    if method == "GET" and path == "/api/chat/poll":
        return handle_chat_poll(query)
    if method == "GET" and path == "/api/admin/chat/inbox":
        return handle_admin_chat_inbox(headers)
    m = re.fullmatch(r"/api/admin/chat/thread/([A-Za-z0-9_-]+)", path or "")
    if method == "GET" and m:
        return handle_admin_chat_thread(headers, m.group(1))
    if method == "POST" and path == "/api/admin/chat/reply":
        return handle_admin_chat_reply(json_body, headers)
    return json_response(404, {"error": "not_found", "path": path})


def dispatch(
    method,
    path,
    json_body=None,
    form=None,
    user_agent="",
    headers=None,
    raw_body=b"",
    remote_addr="",
    query=None,
):
    """返回 (status, content_type, body_bytes, extra_headers)。headers 统一转小写 key。"""
    result = _dispatch_impl(
        method,
        path,
        json_body=json_body,
        form=form,
        user_agent=user_agent,
        headers=headers,
        raw_body=raw_body,
        remote_addr=remote_addr,
        query=query,
    )
    if len(result) == 3:
        return result[0], result[1], result[2], {}
    return result


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
        status, ctype, body, extra = dispatch(
            request.method,
            request.path,
            json_body=json_body,
            form=form,
            user_agent=request.headers.get("User-Agent", ""),
            headers=dict(request.headers),
            raw_body=raw_body,
            remote_addr=request.remote_addr or "",
            query=request.args.to_dict(),
        )
        resp = app.response_class(body, status=status, content_type=ctype)
        for k, v in (extra or {}).items():
            resp.headers[k] = v
        return resp

    app.add_url_rule("/api/health", view_func=_handle, methods=["GET"])
    app.add_url_rule("/api/order", view_func=_handle, methods=["POST"])
    app.add_url_rule("/api/order/<order_id>", view_func=_handle, methods=["GET"])
    app.add_url_rule("/api/pay/alipay", view_func=_handle, methods=["POST"])
    app.add_url_rule("/api/pay/notify/alipay", view_func=_handle, methods=["POST"])
    app.add_url_rule("/api/pay/wechat", view_func=_handle, methods=["POST"])
    app.add_url_rule("/api/pay/notify/wechat", view_func=_handle, methods=["POST"])
    app.add_url_rule("/api/register", view_func=_handle, methods=["POST"])
    app.add_url_rule("/api/login", view_func=_handle, methods=["POST"])
    app.add_url_rule("/api/logout", view_func=_handle, methods=["POST"])
    app.add_url_rule("/api/me", view_func=_handle, methods=["GET"])
    app.add_url_rule("/api/my/orders", view_func=_handle, methods=["GET"])
    app.add_url_rule(
        "/api/my/addresses", view_func=_handle, methods=["GET", "POST"]
    )
    app.add_url_rule(
        "/api/my/addresses/<addr_id>/default", view_func=_handle, methods=["POST"]
    )
    app.add_url_rule(
        "/api/my/addresses/<addr_id>", view_func=_handle, methods=["DELETE"]
    )
    app.add_url_rule("/api/track/<order_id>", view_func=_handle, methods=["GET"])
    app.add_url_rule("/api/admin/login", view_func=_handle, methods=["POST"])
    app.add_url_rule("/api/admin/logout", view_func=_handle, methods=["POST"])
    app.add_url_rule("/api/admin/session", view_func=_handle, methods=["GET"])
    app.add_url_rule("/api/admin/orders", view_func=_handle, methods=["GET"])
    app.add_url_rule(
        "/api/admin/order/<order_id>/fulfillment",
        view_func=_handle,
        methods=["POST"],
    )
    app.add_url_rule("/api/qr", view_func=_handle, methods=["GET"])
    app.add_url_rule("/api/chat/send", view_func=_handle, methods=["POST"])
    app.add_url_rule("/api/chat/poll", view_func=_handle, methods=["GET"])
    app.add_url_rule("/api/admin/chat/inbox", view_func=_handle, methods=["GET"])
    app.add_url_rule(
        "/api/admin/chat/thread/<session_id>", view_func=_handle, methods=["GET"]
    )
    app.add_url_rule("/api/admin/chat/reply", view_func=_handle, methods=["POST"])

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

        def _respond(self, status, ctype, body, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
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
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            status, resp_ctype, body, extra = dispatch(
                self.command,
                parsed.path,
                json_body=json_body,
                form=form,
                user_agent=self.headers.get("User-Agent", ""),
                headers=dict(self.headers.items()),
                raw_body=raw,
                remote_addr=self.client_address[0],
                query=query,
            )
            self._respond(status, resp_ctype, body, extra)

        do_GET = _handle
        do_POST = _handle
        do_DELETE = _handle

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
