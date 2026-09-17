# ProxyScrape 自动注册 + Premium 代理下载

自动完成 ProxyScrape 账号注册，下载试用 Premium 代理列表。本机**不再主动推送**代理到远程订阅，而是由远程订阅（Resin）**反向拉取**本机的只读 feed 接口 `GET /api/feed/proxies`；同时支持按「可供给账号数」自动补齐注册。

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
| 6. 记录到期 | 用上一步已拿到的 overview 顺手缓存到期时间（`keys/account_details_cache.json`，零额外请求） |
| 7. 代理 Feed | 远程订阅反向拉取 `GET /api/feed/proxies`（只读本地 per-account 文件，无网络请求） |
| 8. 自动补齐 | 可供给账号数低于阈值时自动补注册（可选，`AUTO_REGISTER_*`） |
| 9. Web 管理 | 浏览器查看账号、试用到期、流量剩余，并一键发起注册 |

代理写入 **`keys/proxies.txt`（追加，不覆盖）**，适合批量多账号累积；**feed 只读 `keys/proxies_{accountId}.txt`**（该文件按账号隔离，`proxies.txt` 只追加、永不清理，混有已死账号，因此绝不作为 feed 数据源）。

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
| `GET /api/proxies/download-all` | 下载全部 proxies（一个文件，人工用） |
| `GET /api/feed/proxies` | **代理 Feed**（远程订阅拉取用）：`X-Feed-Token` 头或 `?token=` 鉴权 |
| `POST /api/accounts/delete` | `{"email":"..."}` 删除账号及本地相关数据 |
| `POST /api/accounts/delete-invalid` | `{"include_unknown":false}` 删除**已过期**账号（默认不动 `unknown`） |
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
**删除过期账号：** 右上角按钮，默认只删 `已过期`（`expired`）账号；可在第二个确认框里选择连 `到期时间未知`（`unknown`）的账号一起删。**不会自动删除任何账号**。  
**注册日志：** 页面底部实时滚动输出 `register_accounts` 过程日志（含自动补齐调度日志）。  
**配置页：** 管理台右上角「配置」→ `/config`，可改邮箱、Solver URL、`FEED_TOKEN`、`AUTO_REGISTER_*`、注册间隔、`WEB_PASSWORD` 等并写回 `.env`（登录后全部明文；`WEB_HOST`/`WEB_PORT` 需重启进程）。

查看当前 feed 内容（需先配置 `FEED_TOKEN`）：

```bash
curl 'http://127.0.0.1:5080/api/feed/proxies?token=<FEED_TOKEN>'
# 或
curl -H 'X-Feed-Token: <FEED_TOKEN>' http://127.0.0.1:5080/api/feed/proxies
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
# 留空则自动从公开 key 接口获取（进程内缓存 1 小时，不回写 .env），失败时回退 gpt-test
GPTMAIL_API_KEY=
# 可选：覆盖公开 key 接口地址
# GPTMAIL_PUBLIC_KEY_URL=https://mail.chatgpt.org.uk/api/public-key-status?reveal=1
```

> GPTMail 的 key 只缓存在**进程内**，不会写回 `.env`——配置页里 `GPTMAIL_API_KEY` 始终保持你填写的值（或空）。

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

### 4. 代理 Feed（远程拉取，可选）

本机**不做任何推送**：远程订阅（Resin 等）主动 `GET` 本机 feed 接口即可。

```env
# 留空 = 关闭 feed（接口返回 503，不会退化成匿名开放）
FEED_TOKEN=换成一个足够长的随机串
```

拉取方式（二选一）：

```bash
curl -H 'X-Feed-Token: <FEED_TOKEN>' http://<本机IP>:5080/api/feed/proxies
curl 'http://<本机IP>:5080/api/feed/proxies?token=<FEED_TOKEN>'
```

响应语义：

| 情况 | 状态码 | 说明 |
|------|--------|------|
| `FEED_TOKEN` 未配置 | `503` | feed 未开启 |
| token 不匹配 | `401` | 鉴权失败 |
| 无可用代理 | `200` | **空 body**（不是错误） |
| 正常 | `200` | `text/plain` 代理列表，一行一条 |

响应头：`Cache-Control: no-store`、`X-Proxy-Count`（行数）、`X-Proxy-Accounts`（可供给账号数）、`X-Proxy-Errors`（被跳过的账号及原因）。

数据来源与规则：

- **只读本地** `keys/proxies_{accountId}.txt`，**不发任何网络请求、不重登、无副作用**；
- 每个账号是否进 feed 只看**到期时间**：`expired` 排除，`valid` 和 `unknown`（从未刷新过详情）都算可用；
- 行清洗：跳过 `#` 与空行、必须是 `protocol://user:pass@host:port`、且 scheme 仅限 `http`/`https`（`socks5://` 会被丢弃），并按出现顺序去重；
- `proxies_{accountId}.txt` 是**追加**语义，所以同一行可能重复写入——feed 会去重。

> 注意：feed 行在**注册下载那一刻就冻结**，之后不会自动刷新。若某个账号的 per-account 文件写残了，feed 不会自愈——需要重新下载该账号代理或删掉它。

### 5. 自动补齐注册（可选）

在 `web_app.py` 进程内按间隔检查「可供给账号数」（= 账号到期有效/未知 **且** per-account 代理文件存在且非空），低于阈值就自动补注册。

```env
AUTO_REGISTER_ENABLED=false   # 关闭时不做事（每 60s 空转一次以便即时生效）
AUTO_REGISTER_INTERVAL=1800   # 检查间隔（秒），最小 30
AUTO_REGISTER_TARGET=50       # 目标可供给账号数
AUTO_REGISTER_MIN_VALID=10    # 低于该值才补齐
AUTO_REGISTER_MAX_PER_ROUND=20 # 单轮最多注册数量
```

行为要点：

- 与 `POST /api/register` 共用同一把锁，**绝不与手动注册并发**；
- 触发口径是「可供给账号数」，不是 `success_count`（后者只是注册 API 成功数）；
- **连续失败退避**：一轮结束后可供给账号数没有增加（典型原因：本地 Turnstile Solver 未启动，`register_accounts` 会立刻返回）时，下轮等待时间翻倍，最多 `6 × AUTO_REGISTER_INTERVAL`；一旦数量增加就复位；
- **仅支持单进程运行**：调度器是 `web_app.py` 内的 daemon 线程。不要用 gunicorn/uvicorn 多 worker 启动本进程，否则会起多个调度器共享同一份 `keys/` 而并发写坏文件。

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
| **B（注册）** | 配邮箱 / Feed，跑注册；`.env` 指向 A |

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
[*] 结束，成功注册 N/N
[*] 代理列表由 Resin 反向拉取本机 feed: GET /api/feed/proxies
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

### 手动查看 / 拉取 feed

```bash
# FEED_TOKEN 未配置时会返回 503
curl -H 'X-Feed-Token: <FEED_TOKEN>' http://127.0.0.1:5080/api/feed/proxies

# 只看行数与可供给账号数（不打印内容）
curl -sD - -o /dev/null -H 'X-Feed-Token: <FEED_TOKEN>' \
  http://127.0.0.1:5080/api/feed/proxies
```

在远程订阅端把订阅地址填成 `http://<本机IP>:5080/api/feed/proxies?token=<FEED_TOKEN>` 即可（若订阅端支持自定义请求头，更推荐用 `X-Feed-Token`，避免 token 出现在访问日志里）。用 `docker-compose.yml` 起的话，宿主机端口是 **15080**（映射到容器内 5080）。

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
├── main.py                 # 注册 + 验证 + 问卷 + 激活试用 + 下载 + 写详情缓存
├── web_app.py              # Web 管理台（账号 / 到期 / 流量 / 注册 / Feed / 自动补齐）
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
    ├── test_env_config.py
    ├── test_feed_and_auto_register.py
    └── test_gptmail_service.py
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

### Feed 返回 503 / 401

- `503` = `.env` 里 `FEED_TOKEN` 为空（feed 视为未开启，这是刻意的默认行为）
- `401` = token 不匹配；检查订阅端带上的是 `X-Feed-Token` 头还是 `?token=`
- `200` + 空 body = 配置正确但没有可供给的代理（不是错误）
- 想看被跳过的账号原因，读响应头 `X-Proxy-Errors`

### 自动补齐不生效 / 一直在退避

- 确认 `AUTO_REGISTER_ENABLED=true`（默认 `false`），并查看注册日志里的 `[auto]` 行
- 若日志出现「本轮未增加可供给账号 … 下轮等待 ×N」，通常是本地 Turnstile Solver 没启动：`register_accounts` 在验证码连续失败达到 `MAX_CAPTCHA_FAIL_ROUNDS` 时**立刻返回**，所以调度器会翻倍退避（上限 6×间隔）而不是原地空转
- 可供给账号数只统计**同时有到期时间和非空 per-account 代理文件**的账号；注册成功但代理下载失败的账号不算

### 忘记 / 丢失 FEED_TOKEN

在 `/config` 页重新设置 `FEED_TOKEN` 并保存，**立即生效**（路由每次请求都重新读 `os.getenv`），无需重启。

### 账号被删了但 feed 仍然供给它

- `unknown`（从未刷新过详情 / overview 失败）按规则算有效，因此既会进 feed，也**不会**被「删除过期账号」清掉——这是「未知算有效」+「只删 expired」两条规则的必然结果
- 需要清掉就用「删除过期账号」并选择 `include_unknown`，或单独删除该账号

### 密码不符合站点规则

站点要求：至少 8 位、含大写、数字、特殊字符。
`REGISTER_PASSWORD` 不满足时会自动改用随机合规密码。

---

## 注意

1. **仅用于你有权操作的账号与服务**；遵守 ProxyScrape 服务条款与当地法律。
2. 试用账号有代理数量、带宽与协议限制（例如试用约 100 条 HTTP、有流量上限）。
3. `.env`、`keys/` 含密钥与凭证，**不要提交到公开仓库**。
4. 批量注册注意间隔（`REGISTER_INTERVAL`），避免触发风控。
5. **`web_app.py` 只支持单进程运行**：自动补齐调度器是进程内线程，且会写 `keys/`。用多 worker（gunicorn/uvicorn `--workers N`）会起多个调度器并发注册，写坏账号与代理文件。
6. 旧的 `RESIN_*` 配置项已不再使用（推送链路已删除）；`.env` 里残留的 `RESIN_*` 是**无害**的，可自行清理。

---

## 快速开始清单

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 编辑 .env（邮箱 + 可选 FEED_TOKEN / 自动补齐）

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
