# Personal AI Gateway

多模型个人 AI 网关：OpenAI 兼容接口，并行调用 DeepSeek / Kimi / 智谱 GLM，支持多结果对比、失败降级、个人画像注入、滑动窗口记忆与可选 RAG。
提供 **CLI**（`main.py`）与 **FastAPI 网关**（`src/api_gateway.py`）两种用法。

核心链路：**鉴权(401) → 授权(403) → 限流(429) → 密钥代理**，任一环拒绝即短路。

---

## 快速开始（本地 private 模式）

需要 Python 3.10+。

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
docker compose up -d redis          # 本地 Redis
```

`.env` 填入模型密钥（见 `.env.example`），`config/config.yaml` 管理模型与开关（热加载）。自检：`python -m src.config_loader`

```powershell
python main.py                  # CLI 交互对话
python -m src.api_gateway       # 网关，默认 http://127.0.0.1:8010
```

---

## 设计范围

**做**：虚拟密钥（只存 SHA-256，创建时明文仅返回一次，吊销即时生效）、admin/user 两角色、RPM + 日预算两维限流、密钥代理。
**不做**：TPM、单次 token 上限、三级预算刹车、四角色 RBAC、多租户账单、独立审计表、管理/数据平面分离。

限流失败策略为 **fail-closed**（Redis 不可达时拒绝请求）。

---

## 数据存储：愿景 vs 演示

**愿景**：网关应是无状态代理，对话 / 画像 / 知识库存在用户本地（客户端 / 桌面 App / IndexedDB）。

**演示**：用 Neon 托管一组**虚构**预置数据（`tenant_id='demo'`），并提供访客写入（`tenant_id='guest-*'`，TTL 1 小时）。

**迁移路径**：`DataStore` 接口已抽象（`src/storage/`），未来加客户端 adapter 即可把存储移出网关。

### 两种运行模式

| | `APP_MODE=private`（默认，本地） | `APP_MODE=demo`（Render 演示） |
|---|---|---|
| 存储 | `FileDataStore`，`DATA_DIR/<tenant>/`，原子写 | `PostgresDataStore`，Neon，按租户强制隔离 |
| 数据 | 个人真实数据，永久保留 | 虚构预置数据 + 访客数据（TTL 1h） |
| 启动守卫 | — | `DATA_DIR` 含 `private` 直接退出 |

### 多租户隔离

- 每个密钥绑定 `tenant_id`（默认 = key id），为"一用户多 key"预留。
- user 只能访问自己租户；越权返回 **404**（不泄露存在性）。
- admin 需显式 `?tenant=` 才能跨租户。
- 网关层 403（未授权模型）与数据层 404（越权读）是两条独立链路。

### 访客策略（演示环境必读）

> ⚠️ 访客数据 **TTL = 1 小时**，到期自动删除（惰性清理）。**请勿输入真实隐私信息。**
> 提示在 `GET /demo` 顶部与底部固定展示。

- 租户 `guest-<8位随机>`，互相隔离，看不到 demo 数据。
- 访客**只能写会话**；写画像 / 知识库返回 403。
- 访客数据不参与画像提炼、不进向量库、不被后台任务读取。

---

## API 概览

鉴权头：`Authorization: Bearer sk_live_...`（虚拟密钥，库中只存 SHA-256）。
管理端点：`X-Admin-Token: <ADMIN_TOKEN>`，路径前缀 `/admin/`，不认业务虚拟密钥。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | 对话；`models` 多模型并行、`stream` 流式、`topic` 指定会话、`collab` 四角色协作 |
| GET | `/v1/conversations` · `/v1/knowledge` · `/v1/profile` | 本租户数据（admin 可 `?tenant=`） |
| GET | `/demo` · `/health` | 演示页 · 健康检查（豁免鉴权与限流） |
| POST | `/admin/keys` | 创建密钥（`{"guest": true}` 发访客密钥） |
| GET | `/admin/keys` | 列表（不含明文与哈希） |
| DELETE | `/admin/keys/{id}` | 吊销（软删除，幂等 204） |

统一错误格式：`{"error":"<code>","message":"<human readable>"}`

---

## 验收标准（六条，脚本化）

| # | 场景 | 期望 |
|---|---|---|
| ① | 无 key | 401 `missing_key` |
| ② | 无效 / 已吊销 key | 401 `invalid_key` / `revoked_key` |
| ③ | user 调用 `allowed_models` 外的模型 | 403 `model_not_allowed` |
| ④ | 超 RPM | 429 + `Retry-After` 响应头 |
| ⑤ | 日预算 100% | 429 `daily_budget_exceeded` |
| ⑥ | `/health` 无 key | 200 `{"status":"ok"}` |

```bash
BASE=http://127.0.0.1:8010 ADMIN_TOKEN=devtoken bash tests/acceptance.sh
```

每个测试使用独立 key，脚本可重复执行（无需重置 Redis）。验收 ⑤ 通过创建极小预算 key 发真实请求触发，不压 Redis。

---

## 部署到 Render

**环境变量**

| 变量 | 值 | 备注 |
|---|---|---|
| `APP_MODE` | `demo` | 启用 Postgres 存储与隐私守卫 |
| `DATABASE_URL` | Neon 连接串 | 含 `?sslmode=require` |
| `REDIS_URL` | Upstash 连接串 | `rediss://`（TLS） |
| `ADMIN_TOKEN` | `openssl rand -hex 32` | 只存 Render |
| `BIND_HOST` | `0.0.0.0` | 本地为 `127.0.0.1` |
| 模型密钥 | 上游供应商 | `OPENAI_API_KEY` 等 |

**不要设置 `DATA_DIR`**。启动命令 `python -m src.api_gateway`，`PORT` 由 Render 注入。首次启动自动建表并从 `data/demo/seed/` 导入预置数据（`tenant_id='demo'`）。

**免费层注意**：15 分钟无请求休眠，冷启动约 1 分钟；文件系统临时，故演示数据必须走 Neon。

**上线后验证顺序**：`/health` → `/admin/keys`（验 DB）→ 业务端点（验 Redis + 鉴权链）→ 线上跑六条验收。

---

## 测试

```bash
python -m pytest tests -q
```

21 项（安全 12 + 存储契约 9，文件后端）。设 `TEST_DATABASE_URL` 后，同一组契约断言对 Neon Postgres 再跑 15 项，覆盖租户隔离、访客写限制、TTL 清理。

---

## 项目结构（关键路径）

```
app / src/
  api_gateway.py        网关入口，装配中间件链
  auth.py               虚拟密钥鉴权（SHA-256 查表）
  authorize.py          角色与 allowed_models 校验
  ratelimit.py          RPM + 日预算（Redis 原子操作）
  proxy.py              密钥代理与 cost 记账
  admin.py              管理端点
  storage/              DataStore 接口 + File / Postgres 实现
data/
  demo/seed/            虚构预置数据，随仓库
  private/              真实数据，.gitignore 排除
tests/
  acceptance.sh         六条验收
```

---

## 边界与取舍

- **cost 记账非事务**：请求前检查、请求后累加，存在"进行中请求完成后才超额"的竞态。演示级有意接受（不做单次 token 上限、不做分级刹车）。
- **RPM 用固定分钟窗口**：`Retry-After = 60 - now%60`
- **请求日志只记元数据**（key_id、model、status、cost、latency），不记消息正文。
- **生产环境关闭 debug**，500 不回显内部细节。
- **冷启动**：Render 免费层休眠后首次访问需等待，属平台特性而非故障。