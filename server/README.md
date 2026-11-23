# 888ai.me 支付后端（支付宝 + 微信支付）+ 商城 v1

阿童木 P4-CAN 套件销售站的支付后端：建单、拉起支付宝/微信支付、异步回调验签置单、查单。两条通道共用同一套订单表与订单号。
2026-11-22 起同进程承载商城 v1：买家账号（手机号 + 密码）、收货地址与地址簿、履约状态与快递追踪、卖家后台、离线二维码接口（`GET /api/qr`，纯标准库 QR 编码，前端渲染微信扫码图不依赖任何 CDN）。
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
- **支付签名硬性依赖 `cryptography`**（支付宝 RSA2 签名/验签、微信 APIv3 签名/验签与回调 AES-256-GCM 解密）。没装时建单/查单照常工作，支付接口返回 `503 crypto_dependency_missing` 并给出提示。

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
{"tier": "legacy_basic", "phone": "13800000000", "recipient": "张三",
 "province": "浙江省", "city": "台州市", "district": "仙居县",
 "address_detail": "某街道 1 号", "wechat": "可选", "car": "可选"}
```
→ `201 {"order_id": "AT2026...", "amount": "399.00", "status": "PENDING", "fulfillment_status": "AWAITING_PAYMENT", ...}`
手机号须为 11 位大陆号码；档位非法返回 400。**收货人 + 详细地址必填**，缺失返回 `400 {"error": "address_required", "missing": [...]}`（站内客服留言表单不走这个接口，不受影响）。带买家会话 Cookie 下单时自动关联账号，并把地址存进该用户的地址簿（相同地址不重复存，无默认地址时自动设为默认）。

### POST /api/pay/alipay
```json
{"order_id": "AT2026..."}
```
→ `200 {"pay_url": "https://openapi.alipay.com/gateway.do?...", "method": "alipay.trade.page.pay"}`
按请求 User-Agent 自动选接口：手机 UA → `alipay.trade.wap.pay`，其余 → `alipay.trade.page.pay`。前端拿到 `pay_url` 直接跳转即可。
商户参数缺失时返回 `503 {"error": "alipay_config_missing", "missing": [...]}`，服务不崩溃。

### POST /api/pay/notify/alipay
支付宝异步回调（表单 POST）。后端用支付宝公钥验 RSA2 签名，校验 `app_id`、`out_trade_no`、`total_amount` 与订单一致，且 `trade_status` 为 `TRADE_SUCCESS` / `TRADE_FINISHED` 时把订单置 `PAID` 并记录支付宝交易号，返回纯文本 `success`；验签或校验失败返回 `failure` 并写日志。重复通知（订单已 PAID）直接返回 `success`，幂等。

### POST /api/pay/wechat
```json
{"order_id": "AT2026..."}
```
微信支付 APIv3 下单，请求用商户证书私钥 RSA-SHA256 签名。按 User-Agent 自动选接口：
- 桌面 UA → Native 下单 → `200 {"pay_type": "native", "code_url": "weixin://..."}`，前端把 `code_url` 渲染成二维码。
- 手机 UA → H5 下单（`payer_client_ip` 取 `X-Forwarded-For` 首段）→ `200 {"pay_type": "h5", "h5_url": "https://wx.tenpay.com/..."}`，前端直接跳转。

金额用订单的 `amount_cents`（分），`out_trade_no` 用订单号。微信应答会先用微信支付公钥（公钥模式）验签，验不过返回 `502 wechat_response_verify_failed`，不把支付链接交给前端。
商户参数缺失时返回 `503 {"error": "wechat_config_missing", "missing": [...]}`，服务不崩溃。

### POST /api/pay/notify/wechat
微信支付异步回调（JSON POST）。后端先用微信支付公钥验签（请求头 `Wechatpay-Serial` 须等于公钥 ID，并校验时间戳 5 分钟内），再用 APIv3 密钥 AES-256-GCM 解密 `resource`，校验商户号、`out_trade_no`、金额与订单一致且 `trade_state=SUCCESS` 时把订单置 `PAID` 并记录微信交易号，返回 `200 {"code": "SUCCESS", "message": "成功"}`；验签失败返回 `401`、解密或业务校验失败返回 `400`，均为 `{"code": "FAIL", ...}`。重复通知（订单已 PAID）直接返回 SUCCESS，幂等。

### GET /api/order/<order_id>
→ `{"order_id": ..., "amount": "399.00", "status": "PENDING|PAID", "fulfillment_status": ..., "phone": "138****0000", ...}`，手机号脱敏，不返回收货人/地址等字段。

### 商城：买家账号（会话 Cookie `p4session`，HttpOnly，30 天）
- `POST /api/register` `{phone, password}` → 201 + 用户信息；手机号格式非法 400、密码 <6 位 400、已注册 409。密码只存 `pbkdf2_hmac(sha256, 20 万次, 随机盐)` 哈希。
- `POST /api/login` `{phone, password}` → 200 + Cookie；凭证错误统一 401（不区分号不存在/密码错）。
- `POST /api/logout` → 注销当前会话。
- `GET /api/me` → 当前用户；未登录 401。
- `GET /api/my/orders`（需登录）→ 本人订单列表：含履约状态、快递公司/单号、完整收货地址、状态时间线（timeline）。
- `GET /api/my/addresses`（需登录）→ 地址簿列表（默认地址在前）。
- `POST /api/my/addresses`（需登录）→ 新增地址（收货人+详细地址必填，可带 `is_default`）。
- `POST /api/my/addresses/<id>/default`（需登录）→ 设默认地址。
- `DELETE /api/my/addresses/<id>`（需登录）→ 删除地址。

### GET /api/track/<order_id>?phone=
游客订单追踪：`phone` 传下单手机号后四位（或全号）。订单不存在 404、缺 phone 400、不匹配 403。只返回状态节点时间线、快递公司+单号与省/市（城市级），**不返回详细地址与收货人**。

履约状态机：`AWAITING_PAYMENT 待付款 → PAID 付款成功（待发货） → PREPARING 备货中 → SHIPPED 已发货 → IN_TRANSIT 运输中 → DELIVERED 已签收 → COMPLETED 已完成`，旁路 `AFTER_SALE 售后中`。两个支付回调把订单置 PAID 时联动把 `fulfillment_status` 从 AWAITING_PAYMENT 置 PAID。

### 商城：卖家后台（需环境变量 ADMIN_TOKEN，未配置时全部后台接口 503）
- `POST /api/admin/login` `{token}` → 校验通过换后台会话 Cookie `p4admin`（12 小时）；其余后台接口也接受 `Authorization: Bearer <ADMIN_TOKEN>`。
- `GET /api/admin/session` → `{"admin": true|false}`。
- `POST /api/admin/logout` → 注销后台会话。
- `GET /api/admin/orders?status=<履约状态>` → 全部订单（倒序），含**完整电话、微信号、车型与收货地址**（仅后台可见）。
- `POST /api/admin/order/<order_id>/fulfillment` `{fulfillment_status?, courier?, tracking_no?}` → 改履约状态/快递并刷新 `status_updated_at`；状态非法 400。

### 商城：站内客服（替代原 FormSubmit→Gmail→仓库 JSON 中转，消息直达本站数据库）
- `POST /api/chat/send` `{session_id, text}` → 存客户消息（201）。`session_id` 前端生成存 localStorage，后端校验 `[A-Za-z0-9_-]{8,64}`；单条文本 ≤ 2000 字；同会话每分钟最多 20 条，超出 429。命中后端 `CHAT_QA` 关键词规则（与原前端问答库逐字一致）时立即以 `sender=agent, kind=auto` 存一条自动回复并在响应 `auto_reply` 里带回；未命中 `auto_reply` 为 null，只存消息等店主人工回复。
- `GET /api/chat/poll?session_id=&after_id=` → 该会话 id 大于 `after_id` 的全部消息（客户与客服双方），按 id 升序，附 `last_id`。前端每 3 秒增量拉取。
- 后台（复用 ADMIN_TOKEN 鉴权，未登录 401、未配置 503）：`GET /api/admin/chat/inbox` 会话列表（最后消息预览、最后时间、未读数 = id 大于 `agent_last_read_id` 的客户消息数）；`GET /api/admin/chat/thread/<session_id>` 完整线程，打开即把已读位推进到当前最大客户消息 id；`POST /api/admin/chat/reply` `{session_id, text}` 以 `agent/human` 存店主回复，客户下一次 poll 即收到。
- 数据表：`chat_sessions`（session_id 主键、first_seen、last_seen、customer_note、agent_last_read_id）、`chat_messages`（id 自增、session_id、sender ∈ customer/agent、kind ∈ human/auto、text、created_at），随 `db_init` 自动建表，无需手工迁移。

### GET /api/qr?text=
离线生成 QR 二维码 PNG（字节模式、ECC-M、版本 1–10，纯标准库实现，无任何外部依赖）。前端把微信 Native 的 `code_url` 传进来渲染扫码图。文本 UTF-8 超过 213 字节返回 400。

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
| `WECHAT_MCHID` | 微信必填 | 微信支付商户号 |
| `WECHAT_APPID` | 微信必填 | 与商户号绑定的 AppID（Native/H5 下单必填，缺失会被微信以 PARAM_ERROR 拒绝） |
| `WECHAT_CERT_SERIAL` | 微信必填 | 商户 API 证书序列号 |
| `WECHAT_PRIVATE_KEY_PATH` | 微信必填 | 商户 API 证书私钥 PEM 路径（`apiclient_key.pem`，只放服务器，禁止进 git） |
| `WECHAT_API_V3_KEY` | 微信必填 | APIv3 密钥（32 位，只写 pay.env，禁止进 git/日志） |
| `WECHAT_PUB_KEY_ID` | 微信必填 | 微信支付公钥 ID（`PUB_KEY_ID_...`，商户平台 API 安全页） |
| `WECHAT_PUB_KEY_PATH` | 微信必填 | 微信支付公钥 PEM 路径（商户平台下载的 `pub_key.pem`） |
| `WECHAT_NOTIFY_URL` | 微信必填 | 异步回调地址，如 `https://888ai.me/api/pay/notify/wechat` |
> 注：`WECHAT_APPID` 须为已与该商户号绑定的公众号 / 小程序 / 移动应用 AppID，在微信商户平台「AppID 账号管理」里完成绑定，未绑定的 AppID 下单会被拒绝。

| `ORDERS_DB_PATH` | 否 | 默认 `server/orders.db`，生产建议 `/var/lib/p4can/orders.db` |
| `PAY_HOST` / `PAY_PORT` | 否 | 默认 `127.0.0.1` / `9000`，不要改成对外监听 |
| `ADMIN_TOKEN` | 商城后台必填 | 卖家后台口令（长随机串，只写 pay.env，禁止进 git/日志）；未配置时 `/api/admin/*` 一律 503 |
| `COOKIE_SECURE` | 否 | 置 `1` 时会话 Cookie 加 `Secure` 属性（生产 HTTPS 建议开启） |

> 商城数据表（users / sessions / addresses）与 orders 新列（user_id、recipient、province、city、district、address_detail、fulfillment_status、courier、tracking_no、status_updated_at）由服务启动时自动建表/ALTER 迁移，老库可直接升级；存量已付款订单的 fulfillment_status 会回填为 PAID。

## 部署步骤（ECS）

```bash
# 1) 代码随站点仓库 git pull 到 /var/www/888ai 后：
pip install cryptography        # 或 pip install -r server/requirements.txt
# 2) 密钥与环境
mkdir -p /etc/p4can /var/lib/p4can && chown www-data /var/lib/p4can
#    把支付宝应用私钥、支付宝公钥、微信商户私钥（apiclient_key.pem）、微信支付公钥（pub_key.pem）
#    放到 /etc/p4can/，写好 /etc/p4can/pay.env，chmod 600
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
  -d '{"tier":"legacy_basic","phone":"13800000000","recipient":"张三","address_detail":"某街道 1 号","car":"2023 Model 3"}'
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

**第三轮：微信通道（2026-11-22，本地离线自测，未调用微信生产接口）**

- 签名自洽：用真实商户证书私钥（`apiclient_key.pem`）对示例 APIv3 签名串签名，再用商户证书（`apiclient_cert.pem`）公钥验签**通过**；篡改签名串后验签失败 ✅
- 回调全链路（Flask 模式，端口 19092）：微信支付公钥临时替换为自签测试公钥，手工构造 AES-256-GCM 加密 + RSA 签名的 SUCCESS 通知 → 返回 `200 {"code":"SUCCESS"}`，订单置 **PAID** ✅
- 重复发送同一通知 → 仍返回 SUCCESS（幂等），订单不变 ✅
- 金额篡改通知（签名合法、金额不符）→ `400 FAIL`，订单保持 PENDING ✅
- 验签失败通知（用商户私钥冒充微信签名）→ `401 FAIL`，订单保持 PENDING ✅
- 公钥 ID（Serial）不符的通知 → `401 FAIL` ✅
- 无参数模式（标准库模式，端口 19093）：`POST /api/pay/wechat` → 503 `wechat_config_missing`（列全 7 项缺失）；支付宝接口不受影响（建单 201、查单脱敏、支付 503 `alipay_config_missing`、伪造回调返回 `failure`）✅
- 支付宝回归（假密钥全链路复测）：建单 201、page/wap 按 UA 切换、pay_url 签名独立验签通过、模拟回调置 PAID、重复回调幂等、金额篡改被拒，8/8 通过 ✅

测试用订单库、假密钥与测试公钥均在 /tmp；APIv3 密钥仅经环境变量注入测试进程，未写入任何文件。

**第四轮：微信下单补 appid 修复（2026-11-22，本地离线自测，未调用微信生产接口）**

背景：生产真链路验证时微信以 `PARAM_ERROR` 拒绝下单——APIv3 的 Native/H5 下单请求体必填与商户号绑定的 `appid`，原代码未带该字段。本轮修复：`wechat_config()` 新增第 8 项配置 `WECHAT_APPID`（纳入缺失检查），Native 与 H5 下单请求体均加上 `"appid"`（与 `mchid` 并列）。

- 无参数模式（Phase A，7/7）：建单 201、查单脱敏 ✅；`POST /api/pay/wechat` → 503 `wechat_config_missing`，缺失清单共 8 项且含 `WECHAT_APPID` ✅；支付宝 503（4 项缺失）与伪造回调 `failure` 不受影响 ✅
- 全配置拦截验证（Phase B，23/23）：拦截 `wechat_api_post` 捕获请求体（不真调微信）——Native 路径 `/v3/pay/transactions/native`、H5 路径 `/v3/pay/transactions/h5`，两条请求体均断言 `appid` 存在且等于注入的测试 AppID、`mchid` 并列正确、金额与订单号正确、H5 的 `scene_info.payer_client_ip` 正确 ✅
- 微信回调回归：加密+签名通知 → SUCCESS 置 PAID、重复通知幂等、金额篡改 400、冒充签名 401、公钥 ID 不符 401 ✅
- 支付宝回归：page/wap 按 UA 切换、pay_url 签名独立验签通过、模拟回调置 PAID、重复回调幂等、金额篡改被拒 ✅

测试用订单库、假密钥与测试 AppID 均在 /tmp / 进程内存，未进仓库；未调用微信生产接口。

**第五轮：商城 v1（2026-11-22，本地离线自测，未调用任何生产接口）**

新增可复跑测试脚本（`server/tests/`）：

- `tests/test_qr.py`（13/13）：纯标准库 QR 编码器与参考库 `qrcode`（仅测试预言机）逐格比对——格式/版本信息位常数、7 个载荷（11–200 字节，覆盖 V1/V2/V3/V4/V7/V10）在同版本+同掩码下矩阵**完全一致**；PNG 魔数与尺寸、超长文本拒绝。测试中修复校正图形跳过逻辑（旧逻辑误跳定时线上的校正图形，V7+ 才暴露）。
- `tests/test_store.py`（50/50）：双服务并行——Server A（Flask 模式 + 假支付宝/微信参数 + ADMIN_TOKEN）与 Server B（标准库模式 + 零参数）。覆盖：注册校验/重复 409/登录 401/会话 Cookie/退出注销；无地址建单 400 `address_required`；游客单与登录单隔离（my/orders 只见本人单）；下单地址自动入地址簿并设默认、增删改默认；公开查单脱敏（无 recipient/address_detail 字段、电话打码）；游客追踪（缺参 400、错尾号 403、后四位/全号均可、只回城市级 + 时间线）；**双通道回调联动**：支付宝模拟回调与微信模拟回调（AES-GCM 加密 + RSA 签名）均置 status=PAID 且 fulfillment_status 联动 PAID；后台未登录 401、错口令 401、登录后全量订单含完整电话+地址、改状态非法值 400、PREPARING→SHIPPED+快递单号后游客追踪同步可见、按状态筛选、Bearer 直通；`/api/qr` 返回 PNG；Server B 回归：后台未配置 503、支付宝无参数 503、非法档位 400、标准库模式下注册+Cookie 会话正常。
- 老库迁移专项：用旧 schema（无商城列）建库并塞入 1 单 PAID + 1 单 PENDING，新代码启动后列自动补齐、已付款单 fulfillment_status 回填 PAID、待付款单为 AWAITING_PAYMENT，二次启动幂等。

前端（index.html / account.html / admin.html）全部内联脚本经 `node --check` 语法校验；三页引用的 /api 路径与后端路由逐条交叉核对一致；新页面零外部资源引用（仅页脚备案链接）。

**第六轮：站内实时客服（2026-11-23，本地离线自测，未调用任何生产接口）**

- `tests/test_chat.py`（32/32）：双服务并行（Server A Flask + ADMIN_TOKEN，Server B 标准库 + 无 ADMIN_TOKEN）。覆盖：send 参数校验（非法/过短 session 400、空文本 400、超 2000 字 400）；关键词命中（「套件多少钱？」→ auto 回复含 ¥399/¥699 全档价格）与未命中（auto_reply 为 null、只存消息）；poll 全量与 after_id 增量、未知会话返回空、非法 session 400；后台三接口未鉴权 401；inbox 未读数 = 2 且预览为最后一条客户消息；打开 thread 后未读清零（已读推进）；后台 reply 后客户 poll 增量收到 kind=human 的店主消息；thread/reply 未知会话 404；Bearer 直通；限频（同会话 20 条放行、第 21 条 429）；Server B：后台聊天接口 503、标准库模式下 send+秒回+poll 正常。
- 回归：`tests/test_store.py` 50/50、`tests/test_qr.py` 13/13 均无回退；index.html / admin.html 内联脚本 `node --check` 通过；聊天窗已无 FormSubmit 与仓库 `chat/*.json` 依赖（留言表单 kfForm 仍走 FormSubmit，不在本次范围）。
