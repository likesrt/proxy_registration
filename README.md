# ProxyScrape 自动注册 + Premium 代理下载

自动完成 ProxyScrape 账号注册，下载试用 Premium 代理列表，并在批量全部成功后把 `proxies.txt` 上传到远程订阅（Resin）。

**主产物格式：**

```text
protocol://user:pass@host:port
```

示例：

```text
http://s04xvtwkxqsv:3f0gsnlwbbsonwm@209.50.163.168:3129
```

---

## 功能概览

| 步骤 | 说明 |
|------|------|
| 1. 注册 | `POST /v2/v4/account/auth/register`（需 Turnstile） |
| 2. 邮箱验证 | 拉取临时邮箱验证码 → `verify-email` |
| 3. 问卷 | 完成 `/v2/typeform` 引导（API 提交，无需手动点页面） |
| 4. 激活试用 | 确认 `/me` 的 `typeform=false` 后，`POST /v2/v4/account/premium/claim-trial` |
| 5. 下载代理 | 拉取 `/v2/services/premium/proxy-list/{accountId}` 对应列表 |
| 6. 远程上传 | 目标数量**全部成功**后，`PATCH` 上传 `keys/proxies.txt` |
| 7. Web 管理 | 浏览器查看账号、试用到期、流量剩余，并一键发起注册 |

代理写入 **`keys/proxies.txt`（追加，不覆盖）**，适合批量多账号累积。

---

## Web 管理台（推荐日常使用）

在浏览器中管理已注册账号、查看试用到期时间 / 流量剩余，并触发批量注册。

```bash
# 需已安装依赖；可选先启动 Turnstile Solver（注册时需要）
python web_app.py
```

浏览器打开：**http://127.0.0.1:5080/**（首次会跳转 **`/login`**）

**登录鉴权：** 管理台与配置页、受保护 API 均需登录。密码为 `.env` 中的 `WEB_PASSWORD`（默认 `admin`）。登录后配置页**全部字段明文显示**（含 Token / 密钥）。

| 页面/接口 | 说明 |
|-----------|------|
| `/login` | 登录页 |
| `/` | 管理首页：账号表、批量注册、注册日志、下载/删除 |
| `/config` | **配置页**：读写覆盖项目 `.env`，并热更新运行中参数（登录后明文） |
| `POST /api/auth/login` | `{"password":"..."}` 登录 |
| `POST /api/auth/logout` | 退出 |
| `GET /api/accounts` | 账号列表（默认不打远程；`?live=1` 刷新详情） |
| `POST /api/accounts/refresh` | `{"email":"..."}` 刷新单账号 overview；**JWT 过期时自动用密码+Turnstile 重登并写回 token** |
| `GET /api/accounts/download-proxies?email=` | 下载该账号 proxy 文本（token 过期时同样自动重登） |
| `GET /api/proxies/download-all` | 下载全部 proxies（一个文件） |
| `POST /api/proxies/upload-resin` | `{"live":true}` 上传全部代理到 Resin |
| `POST /api/accounts/delete` | `{"email":"..."}` 删除账号及本地相关数据 |
| `POST /api/accounts/delete-all` | 清空全部账号与本地代理文件 |
| `POST /api/register` | `{"count":N}` 启动与 CLI 相同的 `register_accounts` |
| `GET /api/register/status` | 注册任务状态 |
| `GET /api/register/logs?since=N` | 注册日志增量 |
| `GET /api/health` | 健康检查 |
| `GET /api/config` | 读取可编辑配置 + 当前值 |
| `POST /api/config` | `{"values":{...}}` 写入 `.env` 并热更新 |

**表格字段：** 邮箱、**剩余到期（实时倒计时）**、流量剩余、代理数、类型、状态。  
**操作：**「下载」拉本账号 proxy 列表；「刷新」更新到期/流量/地区数；「删除」清本地数据。  
**地区：** 刷新时额外请求 `proxy-list?type=displayproxies`，列表「地区」列为国家数量，悬停可看 `us:52, de:11…` 明细。  
**Token 续期：** ProxyScrape JWT 约 24h 过期。过期后状态栏显示 `token过期`；点「刷新」或下载时会自动 `POST /v2/v4/account/auth/login`（需本机 Turnstile Solver），新 token 写回 `keys/proxyscrape_accounts.txt`。重登失败时若有旧缓存会标 `流量缓存`（数值可能不是最新）。  
**详情缓存：** 刷新成功后的到期/流量会写入 `keys/account_details_cache.json`，刷新网页后仍会显示（无需重新点刷新）。流量展示按 SI（1000）与官网一致。  
**下载全部 proxies：** 右上角按钮，优先实时汇总各账号，失败则回退 `keys/proxies.txt`。  
**上传全部到 Resin：** 右上角按钮；确定=在线汇总后 PATCH 到订阅，取消=仅上传本地 `keys/proxies.txt`（需配置 `RESIN_*`）。  
**注册日志：** 页面底部实时滚动输出 `register_accounts` 过程日志。  
**配置页：** 管理台右上角「配置」→ `/config`，可改邮箱、Solver URL、Resin、注册间隔、`WEB_PASSWORD` 等并写回 `.env`（登录后全部明文；`WEB_HOST`/`WEB_PORT` 需重启进程）。

手动上传（CLI）：

```bash
python -c "from main import upload_proxies_to_resin; print(upload_proxies_to_resin())"
```

**Web 服务（含登录密码）：**

```env
WEB_HOST=127.0.0.1
WEB_PORT=5080
# 管理台登录密码（默认 admin，生产环境请修改）
WEB_PASSWORD=admin
```

> 刷新需要账号 token 仍有效（过期会自动重登）；失败时显示「详情不可用」而不崩溃。注册仍依赖本地 Solver 与邮箱配置。

---

## 环境要求

- Windows / Linux / macOS
- Python 3.10+（建议 3.11+）
- **Turnstile Solver**（`api_solver.py`，本机默认 `http://127.0.0.1:5072`，可远程，见分机部署）
- 可用的临时邮箱服务（Cloudflare Worker 或 GPTMail）

---

## 安装

```bash
cd ProxyScrape
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / macOS
# source .venv/bin/activate

pip install -r requirements.txt
python -m camoufox fetch
```

复制并编辑环境变量：

```bash
# 若已有 .env 可直接改；否则自行新建
notepad .env   # 或用任意编辑器
```

---

## 配置说明（`.env`）

### 1. 临时邮箱（必填其一）

**Cloudflare Worker 模式（默认）：**

```env
EMAIL_SERVICE_TYPE=cloudflare
WORKER_DOMAIN=你的worker域名
ADMIN_PASSWORD=管理密码
# 多个域名逗号分隔，注册时轮换
EMAIL_DOMAIN=example.com, example.org
```

**GPTMail 模式：**

```env
EMAIL_SERVICE_TYPE=gptmail
GPTMAIL_DOMAIN=可用域名列表
GPTMAIL_API_KEY=你的key
```

### 2. 注册参数

```env
# 固定密码，或 random 每次随机（需满足：≥8 位、大写、数字、特殊字符）
REGISTER_PASSWORD=

# 两次注册最小间隔（秒），0 表示不限制
REGISTER_INTERVAL=5

# 可选：HTTP 代理（给 curl_cffi 会话用）
PROXY=

# 可选：非交互指定注册数量（也可用命令行/stdin）
# REGISTER_COUNT=10

# 可选：Turnstile 连续失败轮数上限
# MAX_CAPTCHA_FAIL_ROUNDS=3

# 代理下载协议：试用账号一般为 http
PROXY_DOWNLOAD_PROTOCOL=http
```

### 3. 免费试用激活（自动）

邮箱验证与 onboarding 问卷都完成，并且 `/me` 确认 `typeform=false` 后，程序会使用该账号当前运行期的 `access_token` 调用：

```text
POST /v2/v4/account/premium/claim-trial
```

此步骤不需要在 `.env` 配置额外 Token、Cookie 或浏览器请求头；激活失败会将账号标记为 `NO_PREMIUM_TRIAL`，且不会继续下载代理。

### 4. 远程上传（Resin，可选）

批量**全部成功**后自动执行：

```env
RESIN_SUBSCRIPTION_URL=https://resin.example.com/api/v1/subscriptions/<订阅UUID>
RESIN_API_TOKEN=你的Bearer令牌
RESIN_NAME=test
RESIN_UPDATE_INTERVAL=12h
RESIN_EPHEMERAL_NODE_EVICT_DELAY=72h0m0s
RESIN_ENABLED=true
RESIN_EPHEMERAL=false
RESIN_INCREMENTAL_ALIVE_NODES=false
```

等价于：

```bash
curl 'https://resin.example.com/api/v1/subscriptions/<UUID>' \
  -X PATCH \
  -H 'authorization: Bearer <TOKEN>' \
  -H 'content-type: application/json; charset=utf-8' \
  --data-raw '{"name":"test","update_interval":"12h",...,"content":"<proxies.txt 全文>"}'
```

未配置 `RESIN_SUBSCRIPTION_URL` / `RESIN_API_TOKEN` 时跳过上传，本地文件仍会保存。

---

## 使用方法

### 第一步：启动 Turnstile Solver

**另开一个终端**，保持运行：

```bash
.venv\Scripts\activate
python api_solver.py --browser_type camoufox --thread 1 --debug
```

默认监听 `http://0.0.0.0:5072`（可用 `--host` / `--port` 修改）。Solver 未启动时注册会因验证码失败而停止。

#### 分机部署（推荐：Solver 与注册分开）

完全可以：一台只跑 Solver，另一台只跑 `main.py` / `web_app.py`。

| 机器 | 做什么 |
|------|--------|
| **A（Solver）** | 装浏览器依赖，跑 `api_solver.py`，对外暴露 5072 |
| **B（注册）** | 配邮箱 / Resin，跑注册；`.env` 指向 A |

**A 上：**

```bash
# 监听所有网卡，便于 B 访问（默认 host 已是 0.0.0.0）
python api_solver.py --host 0.0.0.0 --port 5072 --browser_type camoufox --thread 2
```

防火墙放行 **TCP 5072**（仅内网更安全）。

**B 上 `.env`：**

```env
TURNSTILE_SOLVER_URL=http://A的内网IP或域名:5072
```

注册机通过 HTTP 调用：

- `GET {TURNSTILE_SOLVER_URL}/turnstile?url=...&sitekey=...` → `taskId`
- `GET {TURNSTILE_SOLVER_URL}/result?id=...` → token

不配置时默认 `http://127.0.0.1:5072`（同机）。

**注意：**

1. B 必须能访问 A 的 5072（同 VPC / 内网 / 隧道；勿无无公网暴露）。
2. Solver 机器更吃 CPU/内存（浏览器）；注册机主要是网络 + 邮箱。
3. 两边 Python 环境、依赖可独立安装，不必同步代码树全部文件；B 至少要有注册相关代码，A 至少要有 `api_solver.py` 与浏览器依赖。

### 第二步：批量注册并下载代理

```bash
.venv\Scripts\activate
python main.py
```

按提示输入注册数量，或：

```bash
# Windows PowerShell
$env:REGISTER_COUNT="5"
python main.py

# 或管道
echo 5 | python main.py
```

流程日志大致为：

```text
[*] 开始 ProxyScrape 注册: xxx@domain
[*] 求解 Turnstile ...
[+] 注册 API 成功
[*] 开始邮箱验证流程...
[+] 邮箱验证成功
[*] 开始 onboarding 问卷 ...
[+] 问卷完成
[*] 激活 Premium 免费试用...
[+] Premium 免费试用已激活
[*] 下载 proxy-list | format=protocol://user:pass@host:port
[✓] 代理下载成功: ... | 100 条
...
[*] 目标数量已全部完成，开始上传 keys/proxies.txt ...
[✓] 远程上传成功: N 行 | HTTP 200
```

### 第三步（可选）：已有账号单独下载代理

不重新注册，用已保存的 token 下载：

```bash
# 使用 keys/proxyscrape_accounts.txt 最后一行
python download_proxies.py

# 文件内所有账号
python download_proxies.py --all

# 指定 token / accountId
python download_proxies.py --token <JWT> --account-id <UUID>

# 指定协议
python download_proxies.py --protocol http
```

### 手动上传 proxies.txt 到 Resin

```bash
python -c "from main import upload_proxies_to_resin; print(upload_proxies_to_resin())"
```

---

## 输出文件

| 路径 | 说明 |
|------|------|
| `keys/proxies.txt` | **主产物**。所有账号代理**追加**写入，格式 `protocol://user:pass@host:port` |
| `keys/proxies_{accountId}.txt` | 按子账号追加的副本（含注释行） |
| `keys/proxyscrape_accounts.txt` | 旁路记录：`email----password----access_token`（不再单独写 tokens 文件） |

> 主目标是代理列表，不是账号 token。账号文件仅作复用 / 排障。

---

## 目录结构

```text
ProxyScrape/
├── main.py                 # 注册 + 验证 + 问卷 + 激活试用 + 下载 + 远程上传
├── web_app.py              # Web 管理台（账号 / 到期 / 流量 / 注册）
├── download_proxies.py     # 已有账号单独下载代理
├── api_solver.py           # 本地 Turnstile Solver
├── requirements.txt
├── .env                    # 本地配置（勿提交密钥）
├── keys/
│   ├── proxies.txt         # 主产物（追加）
│   ├── proxyscrape_accounts.txt
│   └── ...
├── src/
│   ├── email_service.py
│   ├── gptmail_service.py
│   ├── turnstile_service.py
│   └── proxyscrape_helpers.py
└── tests/
    ├── test_proxyscrape_helpers.py
    ├── test_premium_trial_claim.py
    ├── test_account_web_helpers.py
    └── test_env_config.py
```

---

## 单元测试

```bash
python -m unittest discover -s tests -v
```

---

## 常见问题

### Turnstile / CAPTCHA 失败

- 确认 Solver 已启动；本机默认 `http://127.0.0.1:5072`，分机请检查 `TURNSTILE_SOLVER_URL` 是否可达
- 连续失败会按 `MAX_CAPTCHA_FAIL_ROUNDS` 停止（默认 3）

### 收不到验证码

- 检查邮箱服务域名与 Worker / GPTMail 配置
- 程序会调用 `reset-verification-code` 主动发信
- 验证码为 **8–16 位十六进制**（不是 6 位数字）

### 注册成功但进不了后台

- 需完成 typeform 问卷；程序会自动 `POST /v2/v4/account/typeform`
- 失败账号可能标记为 `----NO_TYPEFORM`，不计入成功数

### 代理下载为空

- 确认问卷已完成（`/me` 中 `typeform=false`）且 Premium 免费试用已成功激活
- 试用一般为 **HTTP only**，`PROXY_DOWNLOAD_PROTOCOL=http`
- 检查 token 是否过期

### 远程上传未执行

- 仅当 `成功数 >= 目标数量` 才会上传
- 检查 `.env` 中 `RESIN_SUBSCRIPTION_URL`、`RESIN_API_TOKEN` 是否非空
- 未达目标时本地 `proxies.txt` 仍会保留（追加）

### 密码不符合站点规则

站点要求：至少 8 位、含大写、数字、特殊字符。
`REGISTER_PASSWORD` 不满足时会自动改用随机合规密码。

---

## 注意

1. **仅用于你有权操作的账号与服务**；遵守 ProxyScrape 服务条款与当地法律。
2. 试用账号有代理数量、带宽与协议限制（例如试用约 100 条 HTTP、有流量上限）。
3. `.env`、`keys/` 含密钥与凭证，**不要提交到公开仓库**。
4. 批量注册注意间隔（`REGISTER_INTERVAL`），避免触发风控。

---

## 快速开始清单

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 编辑 .env（邮箱 + 可选 Resin）

# 3. 终端 A：Solver（注册时需要）
python api_solver.py

# 4a. Web 管理（推荐）
python web_app.py
# 浏览器打开 http://127.0.0.1:5080/

# 4b. 或 CLI 批量注册
set REGISTER_COUNT=3
python main.py

# 5. 查看结果
type keys\proxies.txt
```
