# 888ai.me 支付后端（支付宝）

阿童木 P4-CAN 套件销售站的支付后端：建单、拉起支付宝支付、异步回调验签置单、查单。
独立进程跑在 ECS 上监听 `127.0.0.1:9000`，由 nginx 把 `/api/` 反代过来（见 `nginx-api.conf.example`），静态站其余部分不受影响。

## 文件

| 文件 | 说明 |
|---|---|
| `app.py` | 后端主程序（全部逻辑，单文件） |
| `requirements.txt` | 依赖说明 |
| `pay.service` | systemd unit 示例 |
| `nginx-api.conf.example` | nginx 反代片段示例 |

## 运行方式

- Python 3.10+。
- **Flask 模式**：环境装有 Flask 时自动使用 Flask。
- **标准库模式**：没有 Flask 时自动退回 `http.server`（ThreadingHTTPServer），接口行为一致，裸机零框架依赖。
- **支付宝签名硬性依赖 `cryptography`**（RSA2 签名/验签）。没装时建单/查单照常工作，支付接口返回 `503 crypto_dependency_missing` 并给出提示。

```bash
pip install cryptography          # 最小安装（标准库模式）
pip install -r requirements.txt   # 完整安装（含 Flask）
python3 app.py                    # 默认监听 127.0.0.1:9000
```

订单存 SQLite 单文件，默认 `server/orders.db`，可用环境变量 `ORDERS_DB_PATH` 改路径（生产建议放仓库外，如 `/var/lib/p4can/orders.db`）。

## 接口

档位与价格由服务端固定查表（单位元）：`legacy_basic` 老款基础 399、`legacy_eap` 老款 EAP 499、`legacy_fsd` 老款 FSD 599、`new_basic` 新款基础 499、`new_eap` 新款 EAP 599、`new_fsd` 新款 FSD 699。前端传任何金额都会被忽略。

### POST /api/order
```json
{"tier": "legacy_basic", "phone": "13800000000", "wechat": "可选", "car": "可选"}
```
→ `201 {"order_id": "AT2026...", "amount": "399.00", "status": "PENDING", ...}`
手机号须为 11 位大陆号码；档位非法返回 400。

### POST /api/pay/alipay
```json
{"order_id": "AT2026..."}
```
→ `200 {"pay_url": "https://openapi.alipay.com/gateway.do?...", "method": "alipay.trade.page.pay"}`
按请求 User-Agent 自动选接口：手机 UA → `alipay.trade.wap.pay`，其余 → `alipay.trade.page.pay`。前端拿到 `pay_url` 直接跳转即可。
商户参数缺失时返回 `503 {"error": "alipay_config_missing", "missing": [...]}`，服务不崩溃。

### POST /api/pay/notify/alipay
支付宝异步回调（表单 POST）。后端用支付宝公钥验 RSA2 签名，校验 `app_id`、`out_trade_no`、`total_amount` 与订单一致，且 `trade_status` 为 `TRADE_SUCCESS` / `TRADE_FINISHED` 时把订单置 `PAID` 并记录支付宝交易号，返回纯文本 `success`；验签或校验失败返回 `failure` 并写日志。重复通知（订单已 PAID）直接返回 `success`，幂等。

### GET /api/order/<order_id>
→ `{"order_id": ..., "amount": "399.00", "status": "PENDING|PAID", "phone": "138****0000", ...}`，手机号脱敏，不暴露全号。

### GET /api/health
→ `{"ok": true}`，存活探针。

## 上线环境变量清单

写进 `/etc/p4can/pay.env`（权限 600），由 systemd `EnvironmentFile` 注入：

| 变量 | 必填 | 说明 |
|---|---|---|
| `ALIPAY_APP_ID` | 是 | 支付宝开放平台应用 APPID |
| `ALIPAY_APP_PRIVATE_KEY_PATH` | 是 | 应用私钥 PEM 文件路径（只放服务器，禁止进 git） |
| `ALIPAY_PUBLIC_KEY_PATH` | 是 | 支付宝公钥 PEM 文件路径（开放平台下载的支付宝公钥，非应用公钥） |
| `ALIPAY_NOTIFY_URL` | 是 | 异步回调地址，如 `https://888ai.me/api/pay/notify/alipay` |
| `ALIPAY_RETURN_URL` | 否 | 支付后同步跳转，如 `https://888ai.me/` |
| `ALIPAY_GATEWAY` | 否 | 默认 `https://openapi.alipay.com/gateway.do`（沙箱联调时换沙箱网关） |
| `ORDERS_DB_PATH` | 否 | 默认 `server/orders.db`，生产建议 `/var/lib/p4can/orders.db` |
| `PAY_HOST` / `PAY_PORT` | 否 | 默认 `127.0.0.1` / `9000`，不要改成对外监听 |

## 部署步骤（ECS）

```bash
# 1) 代码随站点仓库 git pull 到 /var/www/888ai 后：
pip install cryptography        # 或 pip install -r server/requirements.txt
# 2) 密钥与环境
mkdir -p /etc/p4can /var/lib/p4can && chown www-data /var/lib/p4can
#    把应用私钥、支付宝公钥两个 PEM 放到 /etc/p4can/，写好 /etc/p4can/pay.env，chmod 600
# 3) systemd
cp server/pay.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now pay
# 4) nginx：把 nginx-api.conf.example 的 location 块加进 888ai.me 的 443 server 块
nginx -t && systemctl reload nginx
# 5) 验证
curl https://888ai.me/api/health
```

## 本地自测步骤（无支付宝参数）

```bash
cd server
ORDERS_DB_PATH=/tmp/test-orders.db PAY_PORT=19090 python3 app.py &
curl -s http://127.0.0.1:19090/api/health
curl -s -X POST http://127.0.0.1:19090/api/order -H 'Content-Type: application/json' \
  -d '{"tier":"legacy_basic","phone":"13800000000","car":"2023 Model 3"}'
curl -s http://127.0.0.1:19090/api/order/<上一步的 order_id>
curl -s -X POST http://127.0.0.1:19090/api/pay/alipay -H 'Content-Type: application/json' \
  -d '{"order_id":"<order_id>"}'   # 无商户参数时应返回 503 alipay_config_missing
```

## 自测记录

2026-11-22 本地冒烟测试（测试机 Python 3.12.3），两轮均通过：

**第一轮：标准库模式、无支付宝参数**（系统 Python，无 Flask / 无 cryptography，端口 19090，临时 SQLite）

- `GET /api/health` → `{"ok": true}` ✅
- `POST /api/order`（legacy_eap）→ 201，订单号 `AT20261122085425464815`，金额服务端定价 `499.00`，状态 PENDING ✅
- `GET /api/order/<id>` → 状态 PENDING，手机号脱敏为 `138****5678` ✅
- `POST /api/pay/alipay`（无商户参数）→ 503 `alipay_config_missing`，明确列出缺失的 4 个环境变量，服务不崩溃 ✅
- 非法档位 → 400 `invalid_tier`；非法手机号 → 400 `invalid_phone`；未知订单 → 404 ✅
- 伪造回调（无配置）→ 返回 `failure` ✅

**第二轮：Flask 模式 + 假支付宝密钥全链路**（venv 内 Flask 3.1.3 + cryptography 50.0.2，本地生成应用/支付宝两对测试 RSA 密钥，端口 19091）

- 建单（new_basic）→ 201，金额 `499.00` ✅
- 电脑 UA → `alipay.trade.page.pay`，返回的 pay_url 签名用应用公钥独立验签**通过** ✅
- iPhone UA → 自动切换 `alipay.trade.wap.pay` ✅
- 按官方口径构造并签名的模拟异步回调 → 返回 `success`，订单置 **PAID** ✅
- 重复发送同一回调 → 仍返回 `success`（幂等），订单不变 ✅
- 金额被篡改的回调（签名合法、金额不符）→ 返回 `failure`，订单保持 PENDING ✅

测试中发现并已修复两个问题：① Flask 适配层未接收路径参数导致查单 500；② 验签串口径——官方文档要求剔除 `sign` 与 `sign_type`，服务端现两种串都兼容尝试（仍需合法签名）。测试用订单库与密钥均在 /tmp，未进仓库。
