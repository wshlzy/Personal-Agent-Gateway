#!/usr/bin/env bash
# 网关功能全链路验收测试
#
# 前置条件：
#   1) 网关已启动（python -m src.api_gateway，默认 127.0.0.1:8010）
#   2) Redis 已启动（REDIS_URL 默认 redis://127.0.0.1:6379/0；Upstash 等远程 Redis 亦支持）
#   3) 环境变量 ADMIN_TOKEN 已导出，且与网关进程一致
#   4) 系统 PATH 里有 curl 和 python（Windows + Git Bash 下一般都满足）
#
# 用法：bash tests/acceptance.sh
# 可用 BASE_URL 环境变量覆盖网关地址，如：
#   BASE_URL=http://127.0.0.1:8011 bash tests/acceptance.sh
#
# 覆盖范围：
#   ① 安全中间件 6 条验收（design.md §7） + 边界补测
#   ② 公开豁免端点（/demo /demo/register /v1/models）
#   ③ 管理端点 /admin/keys 增/列/吊销 全链路
#   ④ 会话 /v1/conversations CRUD + tenant 隔离 + 404
#   ⑤ 知识库 / 画像 CRUD + 访客写入 403
#   ⑥ chat 端点鉴权链路 + 管理端点 user 越权 403
#   ⑦ demo/register 防滥用限流（放脚本末尾，避免污染后续 Redis 配额）
#
# 设计原则：
#   - 每条用例独立 POST /admin/keys 新建 key，RPM/预算/tenant 互不污染
#   - 绝大多数用例不触达上游模型（用不存在的模型触发端点 400 或用 mock 字段）
#   - Windows 兼容性：curl.exe + python（.venv 外的系统 Python）+ Git Bash
#   - 零 jq 依赖：简单字段用 sed，嵌套 JSON 用 python -c 解析

set -u

BASE="${BASE_URL:-http://127.0.0.1:8010}"
PASS=0; FAIL=0

# ---------- 工具函数 ----------

# 调 curl：req <METHOD> <PATH> [curl 额外参数...]
# 结果写入 R_STATUS / R_BODY / R_HEADERS / R_RETRY_AFTER
req() {
  local method="$1" path="$2"; shift 2
  local hfile bfile curl_out
  hfile=$(mktemp); bfile=$(mktemp)
  curl_out=$(curl -sS --max-time 30 -D "$hfile" -o "$bfile" -w '%{http_code}' \
             -X "$method" "$BASE$path" "$@" 2>/dev/null) || curl_out=""
  R_STATUS="$curl_out"
  R_BODY=$(cat "$bfile")
  R_HEADERS=$(cat "$hfile")
  R_RETRY_AFTER=$(printf '%s' "$R_HEADERS" \
    | sed -n 's/^[Rr]etry-[Aa]fter:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | tr -d '\r')
  rm -f "$hfile" "$bfile"
}

# python JSON 提取（JSON 走临时文件，避开 PowerShell/Git Bash 双引号地狱）
# jpy <python_expr> <json> [extra_argv...]
# python 里 _j = 已解析 JSON，_a = 额外 argv 列表
# 例： jpy '_j.get("detail")' "$R_BODY"
#     jpy 'len(_j.get("data",[]))' "$R_BODY"
#     jpy '_j.get(_a[0], "")' "$R_BODY" "status"
jpy() {
  local expr="$1"; shift
  local json="$1"; shift
  local jf
  jf=$(mktemp)
  printf '%s' "$json" > "$jf"
  local result
  result=$(python -c "
import sys, json
try:
    _j = json.load(open(sys.argv[1], encoding='utf-8'))
except Exception:
    _j = None
_a = list(sys.argv[2:])
print($expr)
" -- "$jf" "$@" 2>/dev/null)
  rm -f "$jf"
  printf '%s' "$result"
}

# 快捷：取单个 JSON 字段（值为字符串/数字）
jget() { jpy '_j.get(_a[0], "")' "$2" "$1"; }

# 创建独立密钥（走 /admin/keys）：create_key <请求JSON> → K_ID / K_KEY
create_key() {
  req POST /admin/keys \
    -H "X-Admin-Token: $ADMIN_TOKEN" \
    -H 'Content-Type: application/json' \
    -d "$1"
  K_ID=$(jpy '_j.get("id","")' "$R_BODY")
  K_KEY=$(jpy '_j.get("api_key","")' "$R_BODY")
}

# 鉴权头：auth_h <key> 写入 AUTH_H
auth_h() { AUTH_H="Authorization: Bearer $1"; }

# chat 快捷：chat <请求JSON> [额外 curl 头参数...]
chat() {
  local body="$1"; shift
  req POST /v1/chat/completions \
    -H 'Content-Type: application/json' "$@" -d "$body"
}

# ---------- 断言 ----------

expect_status() {
  if [ "$R_STATUS" = "$1" ]; then echo "    状态码 $R_STATUS ✓"; return 0
  else echo "    状态码期望 $1，实际 $R_STATUS；body: $R_BODY ✗"; return 1; fi
}

expect_detail() {
  if [ "$(jpy '1 if "detail" in d else 0' "$R_BODY" 2>/dev/null)" = "1" ]; then
    echo "    错误体含 detail 字段 ✓"; return 0
  else
    echo "    错误体缺 detail 字段：$R_BODY ✗"; return 1
  fi
}

expect_body_key() {
  # expect_body_key <json_key> [期望非空]
  local k="$1"
  if [ "$(jpy '1 if _j.get(_a[0]) is not None else 0' "$R_BODY" "$k" 2>/dev/null)" = "1" ]; then
    echo "    body 含字段 $k ✓"; return 0
  else
    echo "    body 缺字段 $k：$R_BODY ✗"; return 1
  fi
}

expect_retry_after() {
  # expect_retry_after <min> <max>
  case "$R_RETRY_AFTER" in
    ''|*[!0-9]*) echo "    缺少 Retry-After 数字头 ✗"; return 1 ;;
    *) if [ "$R_RETRY_AFTER" -ge "$1" ] 2>/dev/null && [ "$R_RETRY_AFTER" -le "$2" ] 2>/dev/null; then
         echo "    Retry-After=$R_RETRY_AFTER（区间 $1~$2）✓"; return 0
       else
         echo "    Retry-After=$R_RETRY_AFTER，期望区间 $1~$2 ✗"; return 1
       fi ;;
  esac
}

# 执行一条测试：run_test <编号/名称> <函数>
run_test() {
  echo "== $1 =="
  if "$2"; then PASS=$((PASS+1)); echo "  [PASS] $1"; else FAIL=$((FAIL+1)); echo "  [FAIL] $1"; fi
  echo
}

# ---------- 前置检查 ----------

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE/health" 2>/dev/null)
if [ "$code" != "200" ]; then
  echo "网关不可达（$BASE/health → ${code:-无响应}），请先启动网关：python -m src.api_gateway"
  exit 1
fi
if [ -z "${ADMIN_TOKEN:-}" ]; then
  echo "请先导出 ADMIN_TOKEN 环境变量（需与网关进程一致）"
  exit 1
fi
create_key '{"role":"user"}'
if [ "$R_STATUS" != "201" ]; then
  echo "管理端创建密钥失败（$R_STATUS：$R_BODY），请检查 ADMIN_TOKEN 是否与网关一致"
  exit 1
fi
# 清掉预检 key，避免浪费配额
req POST "/admin/keys/$K_ID/revoke" -H "X-Admin-Token: $ADMIN_TOKEN"
echo "网关 $BASE 可达，ADMIN_TOKEN 校验通过，开始验收。"
echo

# ---------- ① 安全中间件：design.md §7 六条验收 + 边界 ----------

# 验收 1：无 Authorization → 401
test_1_no_key_401() {
  chat '{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}]}'
  expect_status 401 || return 1
  expect_detail || return 1
}

# 验收 2：无效 key → 401，已吊销 key → 401
test_2_invalid_revoked_key_401() {
  local rc=0
  chat '{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}]}' \
       -H 'Authorization: Bearer sk_live_invalid_dummy_0000'
  echo "  无效 key："
  expect_status 401 || rc=1; expect_detail || rc=1

  create_key '{"role":"user"}'
  req POST "/admin/keys/$K_ID/revoke" -H "X-Admin-Token: $ADMIN_TOKEN"
  chat '{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}]}' \
       -H "Authorization: Bearer $K_KEY"
  echo "  已吊销 key："
  expect_status 401 || rc=1; expect_detail || rc=1
  return $rc
}

# 验收 3：user 越权模型 → 403
test_3_forbidden_model_403() {
  create_key '{"role":"user","allowed_models":"deepseek-chat"}'
  chat '{"model":"kimi-k3","messages":[{"role":"user","content":"hi"}]}' \
       -H "Authorization: Bearer $K_KEY"
  expect_status 403 || return 1
  expect_detail || return 1
}

# 验收 4：超 RPM → 429 + Retry-After
# 技巧：用不存在的模型让请求在端点模型校验处以 400 快速结束，
#       但限流计数在中间件阶段已经发生（端点在中间件之后），
#       所以前 2 次各 +1 计数，第 3 次中间件直接 429，零成本确定性强
test_4_rpm_limit_429() {
  local rc=0 i
  create_key '{"role":"user","allowed_models":"*","rpm_limit":2,"daily_budget_cny":100}'
  for i in 1 2 3; do
    chat '{"model":"no-such-model-acceptance","messages":[{"role":"user","content":"hi"}]}' \
         -H "Authorization: Bearer $K_KEY"
    echo "  第 $i 次：$R_STATUS"
  done
  expect_status 429 || rc=1
  expect_detail || rc=1
  expect_retry_after 1 60 || rc=1
  return $rc
}

# 验收 5：日预算 100% → 429
# config 未配置模型单价（price_input_usd=0），无法靠真实调用累计预算。
# 改用 daily_budget_cny=0：spent(0) >= budget(0) 命中同一预算分支，首次请求即 429。
test_5_budget_exhausted_429() {
  local rc=0
  create_key '{"role":"user","allowed_models":"*","rpm_limit":600,"daily_budget_cny":0}'
  chat '{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}]}' \
       -H "Authorization: Bearer $K_KEY"
  expect_status 429 || rc=1
  expect_detail || rc=1
  expect_retry_after 1 86400 || rc=1
  return $rc
}

# 验收 6：/health 无 key → 200
test_6_health_exempt_200() {
  req GET /health
  expect_status 200 || return 1
  if [ "$(jpy '_j.get("status")' "$R_BODY" 2>/dev/null)" = "ok" ]; then
    echo "    body.status=ok ✓"
  else
    echo "    body 异常：$R_BODY ✗"; return 1
  fi
}

# 边界补测 A：Authorization 缺 Bearer 前缀 → 401
test_edge_a_no_bearer_401() {
  chat '{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}]}' \
       -H 'Authorization: sk_live_no_prefix_xxxxx'
  expect_status 401 || return 1
  expect_detail || return 1
}

# 边界补测 B：管理端点无 X-Admin-Token → 401
test_edge_b_admin_no_token_401() {
  req GET /admin/keys
  expect_status 401 || return 1
}

# 边界补测 C：管理端点 POST 不存在 id revoke → 404
test_edge_c_revoke_not_found_404() {
  req POST /admin/keys/no_such_id_zzzz/revoke -H "X-Admin-Token: $ADMIN_TOKEN"
  expect_status 404 || return 1
}

run_test "①-1 无 key → 401"                     test_1_no_key_401
run_test "①-2 无效 / 已吊销 key → 401"          test_2_invalid_revoked_key_401
run_test "①-3 user 越权模型 → 403"              test_3_forbidden_model_403
run_test "①-4 超 RPM → 429 + Retry-After"      test_4_rpm_limit_429
run_test "①-5 日预算 100% → 429"                test_5_budget_exhausted_429
run_test "①-6 /health 无 key → 200"             test_6_health_exempt_200
run_test "①-A 缺 Bearer 前缀 → 401"             test_edge_a_no_bearer_401
run_test "①-B 管理端无 X-Admin-Token → 401"     test_edge_b_admin_no_token_401
run_test "①-C 吊销不存在 id → 404"               test_edge_c_revoke_not_found_404

# ---------- ② 公开豁免端点 ----------

# /demo 公开豁免 → 200 HTML
test_7_demo_exempt_200() {
  req GET /demo
  expect_status 200 || return 1
  case "$R_BODY" in *"<html"*) echo "    HTML 内容 ✓"; return 0 ;; *) echo "    非 HTML：${R_BODY:0:80} ✗"; return 1 ;; esac
}

# /v1/models 公开豁免 → 200 含 data 数组
test_8_models_exempt_200() {
  req GET /v1/models
  expect_status 200 || return 1
  local n
  n=$(jpy 'len(_j.get("data",[]))' "$R_BODY" 2>/dev/null)
  if [ -n "$n" ] && [ "$n" -gt 0 ]; then
    echo "    含 $n 个模型 ✓"; return 0
  else
    echo "    模型列表为空 ✗"; return 1
  fi
}

# /demo/register 公开豁免 → 201 返回 api_key
test_9_demo_register_201() {
  req POST /demo/register -H 'Content-Type: application/json' -d '{"name":"acceptance"}'
  expect_status 201 || return 1
  local ak
  ak=$(jpy '_j.get("api_key","")' "$R_BODY" 2>/dev/null)
  case "$ak" in
    sk_live_*) echo "    返回 api_key=$ak ✓"; return 0 ;;
    *) echo "    api_key 格式异常：$R_BODY ✗"; return 1 ;;
  esac
}

run_test "②-1 /demo 公开豁免 → 200"              test_7_demo_exempt_200
run_test "②-2 /v1/models 公开豁免 → 200"         test_8_models_exempt_200
run_test "②-3 /demo/register 公开豁免 → 201"     test_9_demo_register_201

# ---------- ③ 管理端点 CRUD ----------

# /admin/keys 创建 user key → 201
test_10_admin_create_user_201() {
  req POST /admin/keys -H "X-Admin-Token: $ADMIN_TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{"role":"user","allowed_models":"deepseek-chat,kimi-k3","rpm_limit":10,"daily_budget_cny":0.5}'
  expect_status 201 || return 1
  local ak role al rpm
  ak=$(jpy '_j.get("api_key","")' "$R_BODY" 2>/dev/null)
  role=$(jpy '_j.get("role")' "$R_BODY" 2>/dev/null)
  al=$(jpy '_j.get("allowed_models")' "$R_BODY" 2>/dev/null)
  rpm=$(jpy '_j.get("rpm_limit")' "$R_BODY" 2>/dev/null)
  case "$ak" in
    sk_live_*) echo "    api_key=$ak ✓" ;; *) echo "    api_key 异常 ✗"; return 1 ;;
  esac
  [ "$role" = "user" ] || { echo "    role 异常：$role ✗"; return 1; }
  [ "$al" = "deepseek-chat,kimi-k3" ] || { echo "    allowed_models 异常：$al ✗"; return 1; }
  [ "$rpm" = "10" ] || { echo "    rpm_limit 异常：$rpm ✗"; return 1; }
}

# /admin/keys 创建 admin key → 201
test_11_admin_create_admin_201() {
  req POST /admin/keys -H "X-Admin-Token: $ADMIN_TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{"role":"admin"}'
  expect_status 201 || return 1
  [ "$(jpy '_j.get("role")' "$R_BODY" 2>/dev/null)" = "admin" ] || {
    echo "    role 异常 ✗"; return 1
  }
  echo "    admin key 创建 ✓"
}

# /admin/keys 列表 → 200
test_12_admin_list_keys_200() {
  req GET /admin/keys -H "X-Admin-Token: $ADMIN_TOKEN"
  expect_status 200 || return 1
  local n
  n=$(jpy 'len(d)' "$R_BODY" 2>/dev/null)
  if [ -n "$n" ]; then echo "    列表 $n 条 ✓"; return 0; fi
  echo "    列表解析失败 ✗"; return 1
}

# /admin/keys/{id}/revoke → 200
test_13_admin_revoke_200() {
  create_key '{"role":"user"}'
  req POST "/admin/keys/$K_ID/revoke" -H "X-Admin-Token: $ADMIN_TOKEN"
  expect_status 200 || return 1
  # 复核：再用这个 key 请求 chat → 401（已吊销）
  chat '{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}]}' \
       -H "Authorization: Bearer $K_KEY"
  if [ "$R_STATUS" != "401" ]; then
    echo "    吊销后调用未被拒绝（仍返回 $R_STATUS）✗"; return 1
  fi
  echo "    吊销已立即生效 ✓"
}

run_test "③-1 /admin/keys 创建 user → 201"   test_10_admin_create_user_201
run_test "③-2 /admin/keys 创建 admin → 201"  test_11_admin_create_admin_201
run_test "③-3 /admin/keys 列表 → 200"         test_12_admin_list_keys_200
run_test "③-4 /admin/keys/{id}/revoke → 200" test_13_admin_revoke_200

# ---------- ④ 会话 CRUD + tenant 隔离 ----------
# 每个用例独立 key，topic 各自唯一，避免 tenant 隔离导致的跨用例互相干扰

# /v1/conversations POST 创建 → 200
test_14_conv_create_200() {
  local t="conv-create-$(date +%s)-$RANDOM"
  create_key '{"role":"user","allowed_models":"*"}'
  auth_h "$K_KEY"
  req POST /v1/conversations -H "$AUTH_H" -H 'Content-Type: application/json' \
    -d "{\"topic\":\"$t\",\"tags\":[\"acceptance\"]}"
  expect_status 200 || return 1
  local got
  got=$(jpy '_j.get("topic")' "$R_BODY" 2>/dev/null)
  [ "$got" = "$t" ] || { echo "    topic 异常：$got ≠ $t ✗"; return 1; }
  echo "    topic=$got ✓"
}

# /v1/conversations GET 列表 → 200
test_15_conv_list_200() {
  create_key '{"role":"user","allowed_models":"*"}'
  auth_h "$K_KEY"
  req GET /v1/conversations -H "$AUTH_H"
  expect_status 200 || return 1
  local n
  n=$(jpy 'len(d)' "$R_BODY" 2>/dev/null)
  [ -n "$n" ] && { echo "    列表 $n 条 ✓"; return 0; }
  echo "    列表解析失败 ✗"; return 1
}

# /v1/conversations/{topic} GET → 200（同一 key 下自建自读）
test_16_conv_get_200() {
  local t="conv-get-$(date +%s)-$RANDOM"
  create_key '{"role":"user","allowed_models":"*"}'
  auth_h "$K_KEY"
  req POST /v1/conversations -H "$AUTH_H" -H 'Content-Type: application/json' \
    -d "{\"topic\":\"$t\"}"
  [ "$R_STATUS" = "200" ] || { echo "    先创建会话失败 ✗"; return 1; }
  req GET "/v1/conversations/$t" -H "$AUTH_H"
  expect_status 200 || return 1
}

# /v1/conversations/{不存在} GET → 404
test_17_conv_get_missing_404() {
  create_key '{"role":"user","allowed_models":"*"}'
  auth_h "$K_KEY"
  req GET /v1/conversations/this-topic-does-not-exist-zzz -H "$AUTH_H"
  expect_status 404 || return 1
}

# /v1/conversations/{topic} DELETE → 200
test_18_conv_delete_200() {
  local t="conv-del-$(date +%s)-$RANDOM"
  create_key '{"role":"user","allowed_models":"*"}'
  auth_h "$K_KEY"
  req POST /v1/conversations -H "$AUTH_H" -H 'Content-Type: application/json' \
    -d "{\"topic\":\"$t\"}"
  req DELETE "/v1/conversations/$t" -H "$AUTH_H"
  expect_status 200 || return 1
}

# tenant 隔离验证：key A 创建的会话，key B（不同 tenant）GET → 404
test_19_tenant_isolation_404() {
  local t="conv-isol-$(date +%s)-$RANDOM"
  create_key '{"role":"user","allowed_models":"*"}'
  local keyA="$K_KEY"
  auth_h "$keyA"
  req POST /v1/conversations -H "$AUTH_H" -H 'Content-Type: application/json' \
    -d "{\"topic\":\"$t\"}"
  [ "$R_STATUS" = "200" ] || { echo "    keyA 创建会话失败 ✗"; return 1; }
  # 新建 keyB（tenant 是 keyB.id ≠ keyA.id）
  create_key '{"role":"user","allowed_models":"*"}'
  auth_h "$K_KEY"
  req GET "/v1/conversations/$t" -H "$AUTH_H"
  # 跨 tenant：应 404（网关统一越权 + 不存在都返回 404，不泄露存在性）
  expect_status 404 || return 1
}

run_test "④-1 会话 POST 创建 → 200"          test_14_conv_create_200
run_test "④-2 会话 GET 列表 → 200"           test_15_conv_list_200
run_test "④-3 会话 GET 单个 → 200"           test_16_conv_get_200
run_test "④-4 会话 GET 不存在 → 404"         test_17_conv_get_missing_404
run_test "④-5 会话 DELETE → 200"             test_18_conv_delete_200
run_test "④-6 跨 tenant GET → 404（隔离）"  test_19_tenant_isolation_404

# ---------- ⑤ 知识库 + 画像 CRUD + 访客写保护 ----------

# /v1/knowledge POST 创建 → 200
test_20_knowledge_create_200() {
  create_key '{"role":"user","allowed_models":"*"}'
  auth_h "$K_KEY"
  req POST /v1/knowledge -H "$AUTH_H" -H 'Content-Type: application/json' \
    -d '{"title":"acceptance test","content":"hello world"}'
  expect_status 200 || return 1
  local tid
  tid=$(jpy '_j.get("id")' "$R_BODY" 2>/dev/null)
  [ -n "$tid" ] && { echo "    id=$tid ✓"; return 0; }
  echo "    缺 id 字段 ✗"; return 1
}

# /v1/knowledge GET 列表 → 200
test_21_knowledge_list_200() {
  create_key '{"role":"user","allowed_models":"*"}'
  auth_h "$K_KEY"
  req GET /v1/knowledge -H "$AUTH_H"
  expect_status 200 || return 1
}

# /v1/knowledge GET 单条不存在 → 404
test_22_knowledge_get_missing_404() {
  create_key '{"role":"user","allowed_models":"*"}'
  auth_h "$K_KEY"
  req GET /v1/knowledge/no_such_item_zzz -H "$AUTH_H"
  expect_status 404 || return 1
}

# /v1/profile GET → 200（空画像返回默认结构）
test_23_profile_get_200() {
  create_key '{"role":"user","allowed_models":"*"}'
  auth_h "$K_KEY"
  req GET /v1/profile -H "$AUTH_H"
  expect_status 200 || return 1
}

# /v1/profile PUT 写入 → 200
test_24_profile_put_200() {
  create_key '{"role":"user","allowed_models":"*"}'
  auth_h "$K_KEY"
  req PUT /v1/profile -H "$AUTH_H" -H 'Content-Type: application/json' \
    -d '{"content":"这是验收测试写入的画像"}'
  expect_status 200 || return 1
}

# /v1/profile PUT 访客租户 → 403
test_25_guest_profile_forbid_403() {
  create_key '{"role":"user","allowed_models":"*","guest":true}'
  auth_h "$K_KEY"
  req PUT /v1/profile -H "$AUTH_H" -H 'Content-Type: application/json' \
    -d '{"content":"访客不应能写画像"}'
  expect_status 403 || return 1
  expect_detail || return 1
}

# /v1/knowledge POST 访客租户 → 403
test_26_guest_knowledge_forbid_403() {
  create_key '{"role":"user","allowed_models":"*","guest":true}'
  auth_h "$K_KEY"
  req POST /v1/knowledge -H "$AUTH_H" -H 'Content-Type: application/json' \
    -d '{"title":"xxx","content":"访客不应能写知识"}'
  expect_status 403 || return 1
  expect_detail || return 1
}

run_test "⑤-1 知识库 POST 创建 → 200"     test_20_knowledge_create_200
run_test "⑤-2 知识库 GET 列表 → 200"      test_21_knowledge_list_200
run_test "⑤-3 知识库 GET 不存在 → 404"    test_22_knowledge_get_missing_404
run_test "⑤-4 画像 GET → 200"              test_23_profile_get_200
run_test "⑤-5 画像 PUT → 200"              test_24_profile_put_200
run_test "⑤-6 访客画像 PUT → 403"          test_25_guest_profile_forbid_403
run_test "⑤-7 访客知识 POST → 403"         test_26_guest_knowledge_forbid_403

# ---------- ⑥ chat 端点链路 + admin 端点越权 ----------

# chat 鉴权通过但模型不存在 → 400（验证鉴权+授权+限流全链路已走到端点）
test_27_chat_invalid_model_400() {
  create_key '{"role":"user","allowed_models":"*"}'
  auth_h "$K_KEY"
  chat '{"model":"no-such-model-acceptance","messages":[{"role":"user","content":"hi"}]}' \
       -H "$AUTH_H"
  expect_status 400 || return 1
  expect_detail || return 1
}

# /v1/admin/reload user 角色 → 403（user 无权访问管理端点）
test_28_user_admin_reload_403() {
  create_key '{"role":"user","allowed_models":"*"}'
  auth_h "$K_KEY"
  req POST /v1/admin/reload -H "$AUTH_H"
  expect_status 403 || return 1
  expect_detail || return 1
}

# /v1/admin/reload admin 角色 → 200
test_29_admin_reload_200() {
  create_key '{"role":"admin"}'
  auth_h "$K_KEY"
  req POST /v1/admin/reload -H "$AUTH_H"
  expect_status 200 || return 1
  local rl
  rl=$(jpy '_j.get("reloaded")' "$R_BODY" 2>/dev/null)
  echo "    reloaded=$rl ✓"
}

run_test "⑥-1 chat 鉴权通过但模型不存在 → 400" test_27_chat_invalid_model_400
run_test "⑥-2 user 调 /v1/admin/reload → 403"   test_28_user_admin_reload_403
run_test "⑥-3 admin 调 /v1/admin/reload → 200"  test_29_admin_reload_200

# ---------- ⑦ demo/register 防滥用限流（放最后，避免污染 Redis 配额） ----------
# 每 IP 每天 2 把；前面的 test_9 已消耗了第一次，这里是第 2 / 3 次

# 同 IP 第 2 次注册 → 201（每 IP 限额 2 把，刚好到）
test_30_demo_register_2nd_201() {
  req POST /demo/register -H 'Content-Type: application/json' -d '{}'
  expect_status 201 || return 1
}

# 同 IP 第 3 次注册 → 429 + Retry-After（超限）
test_31_demo_register_3rd_429() {
  req POST /demo/register -H 'Content-Type: application/json' -d '{}'
  expect_status 429 || return 1
  expect_retry_after 1 86400 || return 1
}

run_test "⑦-1 demo/register 同 IP 第 2 次 → 201" test_30_demo_register_2nd_201
run_test "⑦-2 demo/register 同 IP 第 3 次 → 429" test_31_demo_register_3rd_429

# ---------- ⑧ demo register 固定参数验证 + 过期 key ----------
# 这些用 /admin/keys 临时建 key（消耗的是 admin 配额，不碰 demo 限流）

# demo register 返回值：双模型 + 固定 rpm/budget + expires_at + notice
test_32_demo_register_fixed_params() {
  req POST /demo/register -H 'Content-Type: application/json' -d '{"name":"acceptance-fix"}'
  expect_status 201 || return 1
  local rc=0
  # 双模型
  local al
  al=$(jpy 'set(_j.get("allowed_models",[]))' "$R_BODY" 2>/dev/null)
  case "$al" in
    *deepseek-v4.1-flash*) echo "    allowed_models 含 deepseek-v4.1-flash ✓" ;;
    *) echo "    allowed_models 缺 deepseek-v4.1-flash：$al ✗"; rc=1 ;;
  esac
  case "$al" in
    *glm-4-flash*) echo "    allowed_models 含 glm-4-flash ✓" ;;
    *) echo "    allowed_models 缺 glm-4-flash：$al ✗"; rc=1 ;;
  esac
  # rpm/budget/expires_at 固定值
  local rpm bud exp notice
  rpm=$(jpy '_j.get("rpm_limit")' "$R_BODY" 2>/dev/null)
  bud=$(jpy '_j.get("daily_budget_cny")' "$R_BODY" 2>/dev/null)
  exp=$(jpy '_j.get("expires_at")' "$R_BODY" 2>/dev/null)
  notice=$(jpy '_j.get("notice","")' "$R_BODY" 2>/dev/null)
  [ "$rpm" = "3" ] || { echo "    rpm_limit 非 3：$rpm ✗"; rc=1; }
  [ "$bud" = "0.02" ] || { echo "    daily_budget_cny 非 0.02：$bud ✗"; rc=1; }
  [ -n "$exp" ] || { echo "    expires_at 为空 ✗"; rc=1; }
  [ -n "$notice" ] || { echo "    notice 为空 ✗"; rc=1; }
  echo "    rpm=$rpm budget=$bud expires=$exp notice=OK"
  return $rc
}

# admin 手工建 expires_at 过去的 key → chat 返回 401 "已过期"
test_33_expired_key_401() {
  local rc=0
  req POST /admin/keys -H "X-Admin-Token: $ADMIN_TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{"role":"user","allowed_models":"*","expires_at":"2000-01-01T00:00:00"}'
  [ "$R_STATUS" = "201" ] || { echo "    创建过期 key 失败 ✗"; return 1; }
  local ek
  ek=$(jpy '_j.get("api_key","")' "$R_BODY" 2>/dev/null)
  chat '{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}]}' \
       -H "Authorization: Bearer $ek"
  if [ "$R_STATUS" != "401" ]; then
    echo "    过期 key 未返回 401（实际 $R_STATUS）✗"; rc=1
  else
    echo "    过期 key → 401 ✓"
  fi
  return $rc
}

# 硬性上限默认：KEY_MAX_RPM=3，超 3 → 400
test_34_hard_cap_rpm_400() {
  req POST /admin/keys -H "X-Admin-Token: $ADMIN_TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{"role":"user","rpm_limit":4}'
  expect_status 400 || return 1
  local d
  d=$(jpy 'str(_j.get("detail",""))' "$R_BODY" 2>/dev/null)
  case "$d" in
    *KEY_MAX_RPM*) echo "    detail 含 KEY_MAX_RPM ✓" ;;
    *) echo "    detail 异常：$d ✗"; return 1 ;;
  esac
}

# collab 至少 2 个模型（demo key 有 2 个，admin key 传 collab=true 但模型不够 → 400）
test_35_collab_min_models_400() {
  local rc=0
  create_key '{"role":"user","allowed_models":"deepseek-v4.1-flash"}'
  chat '{"model":"deepseek-v4.1-flash","collab":true,"messages":[{"role":"user","content":"hi"}]}' \
       -H "Authorization: Bearer $K_KEY"
  expect_status 400 || rc=1
  local d
  d=$(jpy 'str(_j.get("detail",""))' "$R_BODY" 2>/dev/null)
  case "$d" in
    *至少需要*模型*|"collab"*) echo "    collab 模型不足 → 400 ✓" ;;
    *) echo "    collab 错误信息异常：$d ✗"; rc=1 ;;
  esac
  return $rc
}

run_test "⑧-1 demo register 固定参数（双模型+rpm+budget+expires_at）" test_32_demo_register_fixed_params
run_test "⑧-2 过期 key → 401"                                    test_33_expired_key_401
run_test "⑧-3 硬性上限默认 KEY_MAX_RPM=3 → 超 3 建 key 400"    test_34_hard_cap_rpm_400
run_test "⑧-4 collab 模型不足 → 400"                             test_35_collab_min_models_400

# ---------- 汇总 ----------
echo "========================================"
echo "结果：$PASS passed / $FAIL failed"
echo "========================================"
[ "$FAIL" -eq 0 ]
