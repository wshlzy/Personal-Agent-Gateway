# Personal AI Gateway

多模型 AI 网关（OpenAI 兼容），并行调用 DeepSeek / Kimi / GLM，支持失败降级、协作流水线、画像注入与可选 RAG。CLI 自用 / FastAPI 公网网关共用同一套安全层——CLI 不触发任何限流或预算，网关则有完整防护。

核心链路：**鉴权(401) → 授权(403) → 限流(429) → 密钥代理**，任一层拒绝即短路。限流 fail-closed（Redis 不可达直接拒绝）。

---

## 快速开始

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
docker compose up -d redis          # 或用 Upstash 等托管 Redis
```

`.env` 填模型密钥（见 `.env.example`），`config/config.yaml` 管理模型与开关（热加载）。

```powershell
python main.py                    # CLI 交互对话（自用，无安全层）
python -m src.api_gateway         # 网关 http://127.0.0.1:8010
```

---

## 配置

| 变量 | 说明 |
|---|---|
| `APP_MODE` | `private`（默认，本地 FileDataStore）/ `demo`（Postgres） |
| `ADMIN_TOKEN` | 管理端点令牌，必须强随机 |
| `REDIS_URL` | 限流 + 预算计数器 |
| `DATABASE_URL` | 仅 demo 模式需，Postgres 连接串 |
| `KEY_MAX_RPM` | 创建密钥的硬性上限，默认 **3**（所有角色不豁免） |
| `KEY_MAX_DAILY_BUDGET_CNY` | 创建密钥的预算硬性上限，默认 **¥1.0** |
| `USD_TO_CNY` | 模型价格是 USD，网关按此汇率换算预算（默认 7.2） |

---

## API 概览

### 安全模型

- **业务端点**：`Authorization: Bearer sk_live_...`（虚拟密钥，库中只存 SHA-256，明文仅创建时返回一次）
- **管理端点**：`X-Admin-Token: <ADMIN_TOKEN>`（与业务密钥互不认）
- **角色**：`admin` 可调用所有模型、跨租户访问；`user` 仅能调 `allowed_models` 内模型、只能看自己租户
- **过期**：key 可设 `expires_at`（ISO8601），过期请求返回 401
- **限流**：每 key 独立 RPM + 日预算两维；RPM 用固定分钟桶

错误格式 `{"detail": "..."}`。状态码：400 / 401 / 403 / 404 / 429（带 `Retry-After`）/ 503。

### 对话：`POST /v1/chat/completions`

**第一步：申请 demo key**（无需任何 token）：

```bash
curl -X POST http://127.0.0.1:8010/demo/register
```
> 返回：`{"api_key":"sk_live_xxx...","rpm_limit":3,"daily_budget_cny":0.02,"allowed_models":["deepseek-v4.1-flash","glm-4-flash"],"expires_at":"..."}`
> 防滥用：每 IP 每天 2 把，全局每天 50 把，24 小时后自动过期。两个模型足够跑 `collab` 模式。

**第二步：发起对话**。先创建请求体文件 `chat.json`，再 `curl -d @chat.json`：

```json
{
  "model": "deepseek-v4.1-flash",
  "messages": [{"role": "user", "content": "你好，用一句话介绍自己"}],
  "stream": false,
  "topic": "主对话",
  "rag": false,
  "collab": false
}
```

```bash
# bash / zsh
curl -X POST http://127.0.0.1:8010/v1/chat/completions \
  -H "Authorization: Bearer sk_live_<上一步返回的api_key>" \
  -H "Content-Type: application/json" \
  -d @chat.json
```

```powershell
# PowerShell（Windows）
curl.exe -X POST http://127.0.0.1:8010/v1/chat/completions `
  -H "Authorization: Bearer sk_live_<上一步返回的api_key>" `
  -H "Content-Type: application/json" `
  -d "@chat.json"
```

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `model` | string | — | 单模型名。demo key 可用：`deepseek-v4.1-flash`、`glm-4-flash` |
| `models` | string[] | — | 多模型并行（与 `model` 二选一，不支持流式） |
| `messages` | array | 必填 | OpenAI 格式，只需本轮问题；历史、画像、RAG 由网关自动拼接 |
| `stream` | bool | `false` | SSE 流式，仅单模型 |
| `topic` | string | `主对话` | 会话主题，请求自动持久化 |
| `rag` | bool | 跟 config | 临时覆盖 RAG 开关 |
| `collab` | bool | `false` | 四角色协作流水线（顾问→裁判→主笔→审计），适合制定具体计划的提示词 |

### 密钥管理（`/admin/*`，需 `X-Admin-Token`）

**`POST /admin/keys`** → 201，返回明文 API key（仅此一次）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `role` | `user` | `admin` / `user` |
| `allowed_models` | `*` | 逗号分隔模型名；`*` 全部放行 |
| `rpm_limit` | 60 | ≤ `KEY_MAX_RPM`（默认 3） |
| `daily_budget_cny` | ¥1.0 | ≤ `KEY_MAX_DAILY_BUDGET_CNY`（默认 ¥1.0） |
| `tenant_id` | = key id | 可选，同一用户多 key 可共用 |
| `expires_at` | 永不过期 | ISO8601 |
| `guest` | false | 访客密钥：强制 user + `tenant_id=guest-<8位随机>` |

**`GET /admin/keys`** → 列表（含 `spent_today_cny`，不含明文与哈希）。
**`POST /admin/keys/{id}/revoke`** → 吊销立即生效。

### 公开自助注册 `POST /demo/register`

**无鉴权，任何人可调。** 防滥用：每 IP 每天 2 把 + 全局每天 50 把（Redis INCR+EXPIRE 原子操作，fail-closed，超限 429 + `Retry-After`）。

返回的 key **所有参数由服务端固定，外部不可选**：
- `role` = `user`
- `rpm_limit` = 3，`daily_budget_cny` = ¥0.02
- `allowed_models` = `["deepseek-v4.1-flash"]`
- `expires_at` = 24 小时后

示例：
```bash
curl -X POST https://your-gateway/demo/register
# {"api_key":"sk_live_...","rpm_limit":3,"daily_budget_cny":0.02,
#  "allowed_models":["deepseek-v4.1-flash"],"expires_at":"...","notice":"..."}
```

过期 key 在每次 register 时惰性清理。

---

## 支持的模型

| 名称 | 供应商 | 档位 |
|---|---|---|
| `deepseek-chat` | DeepSeek | 轻量 |
| `deepseek-v4.1-flash` | DeepSeek | 旗舰 |
| `kimi-k2.6` | Moonshot | 轻量 |
| `kimi-k3` | Moonshot | 旗舰 |
| `glm-4-flash` | 智谱 | 轻量 |
| `glm-5.3` | 智谱 | 旗舰 |

用 `GET /v1/models` 可查运行时状态（含 `enabled` 开关）。`config/config.yaml` 控制哪些启用、哪些停用（热加载，改完即生效）。

---

## 数据存储与租户隔离

| | private（默认） | demo |
|---|---|---|
| 存储 | `FileDataStore`，本地文件 | `PostgresDataStore` |
| 对话 / 画像 / 知识库 | 真实个人数据 | 虚构预置 + 访客 TTL 1h |

- 每个密钥绑定 `tenant_id`（默认 = key id）；user 只能看自己租户，越权返回 404（不泄露存在性）
- admin 跨租户需显式 `?tenant=<id>`
- 访客（`guest-*`）只能写会话，画像 / 知识库返回 403

---

## 设计范围

**做**：虚拟密钥（SHA-256 存储）、admin/user 两角色、RPM + 日预算两维限流、硬性上限（防 ADMIN_TOKEN 泄露后海量开 key）、公开自助注册（IP + 全局双级限流、24h 过期）、Redis fail-closed、UTF-8 BOM 兼容。

**不做**：邮箱/人机验证（演示级用 IP 限流代替）、多角色 RBAC 之外的权限模型、独立审计表、TPM、管理/数据平面分离。

---

## 测试

```bash
python -m pytest tests -q
```

31 项（安全 15 + 存储契约 16），25 passed + 6 PG skipped（无 `TEST_DATABASE_URL`）。6 条验收标准 + 5 条 demo register 专项。
