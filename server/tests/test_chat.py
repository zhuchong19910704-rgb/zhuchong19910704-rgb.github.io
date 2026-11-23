#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""站内客服聊天自测（本地，临时 SQLite，不连任何外部服务）。

运行：
    /path/to/venv/bin/python tests/test_chat.py

Server A：venv Python（Flask 模式）+ ADMIN_TOKEN
Server B：系统 python3（标准库模式）+ 无 ADMIN_TOKEN（后台接口应 503）
"""
import http.cookiejar
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

SERVER_DIR = os.path.join(os.path.dirname(__file__), "..")
PORT_A, PORT_B = 19097, 19098
ADMIN_TOKEN = "test-admin-token-xyz"
SID1 = "test-session-0001"
SID2 = "test-session-0002"

results = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if extra else ""))
    results.append((name, bool(cond)))


def opener(with_cookies=False):
    if with_cookies:
        cj = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    return urllib.request.build_opener()


def call(op, method, url, json_body=None, headers=None):
    data = None
    hdrs = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
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


def main():
    for db in ("/tmp/chat-test-a.db", "/tmp/chat-test-b.db"):
        if os.path.exists(db):
            os.remove(db)

    env_a = dict(os.environ)
    env_a.update(
        {
            "ORDERS_DB_PATH": "/tmp/chat-test-a.db",
            "PAY_HOST": "127.0.0.1",
            "PAY_PORT": str(PORT_A),
            "ADMIN_TOKEN": ADMIN_TOKEN,
        }
    )
    env_b = dict(os.environ)
    env_b.update(
        {
            "ORDERS_DB_PATH": "/tmp/chat-test-b.db",
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
        admin = opener(with_cookies=True)

        # ---------- 参数校验 ----------
        s, j = call_json(guest, "POST", A + "/api/chat/send",
                         {"session_id": "bad id!", "text": "你好"})
        check("send invalid session 400", s == 400 and j.get("error") == "invalid_session_id")
        s, j = call_json(guest, "POST", A + "/api/chat/send",
                         {"session_id": "short", "text": "你好"})
        check("send short session 400", s == 400 and j.get("error") == "invalid_session_id")
        s, j = call_json(guest, "POST", A + "/api/chat/send",
                         {"session_id": SID1, "text": "   "})
        check("send empty text 400", s == 400 and j.get("error") == "text_required")
        s, j = call_json(guest, "POST", A + "/api/chat/send",
                         {"session_id": SID1, "text": "x" * 2001})
        check("send too long 400", s == 400 and j.get("error") == "text_too_long")

        # ---------- 关键词命中自动回复 ----------
        s, j = call_json(guest, "POST", A + "/api/chat/send",
                         {"session_id": SID1, "text": "套件多少钱？"})
        check("send keyword 201", s == 201 and j["message"]["sender"] == "customer", j)
        auto = j.get("auto_reply") or {}
        check("auto reply hit", auto.get("sender") == "agent" and auto.get("kind") == "auto"
              and "¥399" in auto.get("text", "") and "¥699" in auto.get("text", ""), auto.get("text", "")[:40])
        first_id = j["message"]["id"]
        auto_id = auto["id"]

        s, j = call_json(guest, "GET", A + "/api/chat/poll?session_id=%s&after_id=0" % SID1)
        msgs = j.get("messages", [])
        check("poll full thread", s == 200 and len(msgs) == 2
              and msgs[0]["id"] == first_id and msgs[1]["id"] == auto_id
              and j.get("last_id") == auto_id, msgs)
        s, j = call_json(guest, "GET",
                         A + "/api/chat/poll?session_id=%s&after_id=%d" % (SID1, first_id))
        check("poll incremental", s == 200 and [m["id"] for m in j["messages"]] == [auto_id])

        # ---------- 未命中：只存消息，无自动回复 ----------
        s, j = call_json(guest, "POST", A + "/api/chat/send",
                         {"session_id": SID1, "text": "今天天气怎么样"})
        check("send miss no auto", s == 201 and j.get("auto_reply") is None, j)
        miss_id = j["message"]["id"]
        s, j = call_json(guest, "GET",
                         A + "/api/chat/poll?session_id=%s&after_id=%d" % (SID1, auto_id))
        check("poll after miss", s == 200 and [m["id"] for m in j["messages"]] == [miss_id]
              and j["messages"][0]["sender"] == "customer")

        # ---------- poll 边界 ----------
        s, j = call_json(guest, "GET", A + "/api/chat/poll?session_id=unknown-session-99")
        check("poll unknown session empty", s == 200 and j.get("messages") == [])
        s, j = call_json(guest, "GET", A + "/api/chat/poll?session_id=bad%20id")
        check("poll invalid session 400", s == 400)

        # ---------- 后台未鉴权 ----------
        s, j = call_json(guest, "GET", A + "/api/admin/chat/inbox")
        check("inbox requires auth 401", s == 401)
        s, j = call_json(guest, "GET", A + "/api/admin/chat/thread/" + SID1)
        check("thread requires auth 401", s == 401)
        s, j = call_json(guest, "POST", A + "/api/admin/chat/reply",
                         {"session_id": SID1, "text": "在的"})
        check("reply requires auth 401", s == 401)

        # ---------- 后台：inbox 未读数 / 已读推进 ----------
        s, j = call_json(admin, "POST", A + "/api/admin/login", {"token": ADMIN_TOKEN})
        check("admin login 200", s == 200)
        s, j = call_json(admin, "GET", A + "/api/admin/chat/inbox")
        sess = next((x for x in j.get("sessions", []) if x["session_id"] == SID1), None)
        check("inbox lists session", sess is not None, j.get("sessions"))
        check("inbox unread = 2", sess and sess["unread"] == 2, sess)
        check("inbox preview is last msg",
              sess and sess["last_message"]["text"] == "今天天气怎么样"
              and sess["last_message"]["sender"] == "customer")
        s, j = call_json(admin, "GET", A + "/api/admin/chat/thread/" + SID1)
        check("thread full", s == 200 and len(j.get("messages", [])) == 3
              and [m["sender"] for m in j["messages"]] == ["customer", "agent", "customer"])
        s, j = call_json(admin, "GET", A + "/api/admin/chat/inbox")
        sess = next((x for x in j.get("sessions", []) if x["session_id"] == SID1), None)
        check("unread cleared after thread", sess and sess["unread"] == 0, sess)

        # ---------- 后台回复 → 客户 poll 收到 ----------
        s, j = call_json(admin, "POST", A + "/api/admin/chat/reply",
                         {"session_id": SID1, "text": "在的，我是店主本人。天气这个我可管不了哈。"})
        check("admin reply 201", s == 201 and j["message"]["sender"] == "agent"
              and j["message"]["kind"] == "human", j)
        reply_id = j["message"]["id"]
        s, j = call_json(guest, "GET",
                         A + "/api/chat/poll?session_id=%s&after_id=%d" % (SID1, miss_id))
        got = j.get("messages", [])
        check("customer poll gets reply", len(got) == 1 and got[0]["id"] == reply_id
              and got[0]["kind"] == "human" and "店主本人" in got[0]["text"], got)

        # ---------- 后台 404 ----------
        s, j = call_json(admin, "GET", A + "/api/admin/chat/thread/no-such-session-1")
        check("thread unknown 404", s == 404)
        s, j = call_json(admin, "POST", A + "/api/admin/chat/reply",
                         {"session_id": "no-such-session-1", "text": "hi"})
        check("reply unknown 404", s == 404)
        s, j = call_json(guest, "GET", A + "/api/admin/chat/inbox",
                         headers={"Authorization": "Bearer " + ADMIN_TOKEN})
        check("inbox Bearer auth", s == 200)

        # ---------- 限频：同会话每分钟 20 条 ----------
        ok_count = 0
        for i in range(20):
            s, j = call_json(guest, "POST", A + "/api/chat/send",
                             {"session_id": SID2, "text": "第 %d 条测试消息" % (i + 1)})
            if s == 201:
                ok_count += 1
        check("rate limit allows 20", ok_count == 20, ok_count)
        s, j = call_json(guest, "POST", A + "/api/chat/send",
                         {"session_id": SID2, "text": "第 21 条"})
        check("rate limit 21st 429", s == 429 and j.get("error") == "rate_limited", s)

        # ---------- Server B：无 ADMIN_TOKEN ----------
        s, j = call_json(guest, "GET", B + "/api/admin/chat/inbox")
        check("B inbox 503", s == 503 and j.get("error") == "admin_not_configured")
        s, j = call_json(guest, "POST", B + "/api/admin/chat/reply",
                         {"session_id": SID1, "text": "hi"})
        check("B reply 503", s == 503)
        s, j = call_json(guest, "POST", B + "/api/chat/send",
                         {"session_id": SID1, "text": "影响保修吗？"})
        check("B stdlib send + auto", s == 201 and j.get("auto_reply")
              and "基本保修期" in j["auto_reply"]["text"], j.get("auto_reply"))
        s, j = call_json(guest, "GET", B + "/api/chat/poll?session_id=" + SID1)
        check("B stdlib poll", s == 200 and len(j.get("messages", [])) == 2)
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
