#!/usr/bin/env bash
# 鉴权与限流 · 六条验收测试（design.md §7）
#
# 前置条件：
#   1) 网关已启动（python -m src.api_gateway，默认 127.0.0.1:8010）
#   2) Redis 已启动（REDIS_URL 默认 redis://127.0.0.1:6379/0）
#   3) 环境变量 ADMIN_TOKEN 已导出，且与网关进程一致
#
# 用法：bash tests/acceptance.sh
# 可用 BASE_URL 环境变量覆盖网关地址，如：
#   BASE_URL=http://127.0.0.1:8011 bash tests/acceptance.sh
#
# 说明：
# - 错误体断言的是 detail 字段：网关 4xx/5xx 统一返回 FastAPI 风格 {"detail": ...}
# - 每条用例独立 POST /admin/keys 新建 key，避免 ④⑤ 的限流/预算状态污染后续用例
# - 全部用例都不会真正调用上游模型（④ 利用"限流计数发生在端点模型校验之前"，
#   用不存在的模型让前两次请求以 400 快速结束，零成本且确定性强）

set -u

BASE="${BASE_URL:-http://127.0.0.1:8010}"
PASS=0; FAIL=0
EDGE_PASS=0; EDGE_FAIL=0

# ---------- 工具函数 ----------

# 免 jq 提取 JSON 字符串字段：jget <字段名> <json>
jget() { printf '%s' "$2" | sed -n 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'; }

# curl 封装：req <METHOD> <PATH> [curl 额外参数...]
# 结果写入 R_STATUS / R_BODY / R_RETRY_AFTER
req() {
  local method="$1" path="$2"; shift 2
  local hfile bfile
  hfile=$(mktemp); bfile=$(mktemp)
  R_STATUS=$(curl -sS --max-time 30 -D "$hfile" -o "$bfile" -w '%{http_code}' \
             -X "$method" "$BASE$path" "$@")
  R_BODY=$(cat "$bfile")
  R_RETRY_AFTER=$(sed -n 's/^[Rr]etry-[Aa]fter:[[:space:]]*\([0-9]\+\).*/\1/p' "$hfile" | tr -d '\r')
  rm -f "$hfile" "$bfile"
}

# 创建独立密钥：create_key <请求JSON> → K_ID / K_KEY
create_key() {
  req POST /admin/keys -H "X-Admin-Token: $ADMIN_TOKEN" -H 'Content-Type: application/json' -d "$1"
  K_ID=$(jget id "$R_BODY")
  K_KEY=$(jget api_key "$R_BODY")
}

chat() { # chat <请求JSON> [额外 curl 头参数...]
  local body="$1"; shift
  req POST /v1/chat/completions -H 'Content-Type: application/json' "$@" -d "$body"
}

CHAT_BODY='{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}]}'
# 不存在的模型：④ 用它让请求在端点校验阶段以 400 快速结束（限流计数已发生，不触达上游）
CHAT_BAD_MODEL='{"model":"no-such-model-acceptance","messages":[{"role":"user","content":"hi"}]}'

# 子断言（返回 0/1，由用例函数汇总）
expect_status() { # expect_status <期望码>
  if [ "$R_STATUS" = "$1" ]; then echo "    状态码 $R_STATUS ✓"; return 0
  else echo "    状态码期望 $1，实际 $R_STATUS；body: $R_BODY ✗"; return 1; fi
}
expect_detail() { # 错误体含 detail 字段
  if printf '%s' "$R_BODY" | grep -q '"detail"'; then echo "    错误体含 detail 字段 ✓"; return 0
  else echo "    错误体缺 detail 字段：$R_BODY ✗"; return 1; fi
}
expect_retry_after() { # expect_retry_after <min> <max>
  case "$R_RETRY_AFTER" in
    ''|*[!0-9]*) echo "    缺少 Retry-After 数字头 ✗"; return 1 ;;
    *) if [ "$R_RETRY_AFTER" -ge "$1" ] && [ "$R_RETRY_AFTER" -le "$2" ]; then
         echo "    Retry-After=$R_RETRY_AFTER（区间 $1~$2）✓"; return 0
       else
         echo "    Retry-After=$R_RETRY_AFTER，期望区间 $1~$2 ✗"; return 1
       fi ;;
  esac
}

run_test() { # run_test <编号/名称> <函数>
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
# 管理端预检：确认 X-Admin-Token 可用
create_key '{"role":"user"}'
if [ "$R_STATUS" != "201" ]; then
  echo "管理端创建密钥失败（$R_STATUS：$R_BODY），请检查 ADMIN_TOKEN 是否与网关一致"
  exit 1
fi
echo "网关 $BASE 可达，ADMIN_TOKEN 校验通过，开始验收。"
echo

# ---------- 验收 1：无 key → 401 ----------
test_1() {
  local rc=0
  chat "$CHAT_BODY"                      # 不带 Authorization
  expect_status 401 || rc=1
  expect_detail || rc=1
  return $rc
}

# ---------- 验收 2：无效 / 已吊销 key → 401 ----------
test_2() {
  local rc=0
  chat "$CHAT_BODY" -H "Authorization: Bearer sk_live_invalid_000"
  echo "  无效 key："
  expect_status 401 || rc=1
  expect_detail || rc=1
  # 新建 key → 吊销 → 再调用
  create_key '{"role":"user"}'
  req POST "/admin/keys/$K_ID/revoke" -H "X-Admin-Token: $ADMIN_TOKEN"
  chat "$CHAT_BODY" -H "Authorization: Bearer $K_KEY"
  echo "  已吊销 key："
  expect_status 401 || rc=1
  expect_detail || rc=1
  return $rc
}

# ---------- 验收 3：user 越权模型 → 403 ----------
test_3() {
  local rc=0
  create_key '{"role":"user","allowed_models":"deepseek-chat"}'
  chat '{"model":"kimi-k3","messages":[{"role":"user","content":"hi"}]}' \
       -H "Authorization: Bearer $K_KEY"
  expect_status 403 || rc=1
  expect_detail || rc=1
  return $rc
}

# ---------- 验收 4：超 RPM → 429 + Retry-After ----------
test_4() {
  local rc=0 i
  create_key '{"role":"user","allowed_models":"*","rpm_limit":2,"daily_budget_usd":100}'
  # rpm_limit=2：前两次用不存在的模型快速 400（限流计数已 +1，不调上游），第 3 次必 429
  for i in 1 2 3; do
    chat "$CHAT_BAD_MODEL" -H "Authorization: Bearer $K_KEY"
    echo "  第 $i 次：$R_STATUS"
  done
  expect_status 429 || rc=1
  expect_detail || rc=1
  expect_retry_after 1 60 || rc=1        # 到下一分钟桶的秒数
  return $rc
}

# ---------- 验收 5：日预算 100% → 429 ----------
test_5() {
  local rc=0
  # design 原版为"一次成功调用累计成本后第二次 429"；当前 config 未配置模型单价
  # （price_*_usd=0 不计费），改用 daily_budget_usd=0：spent(0) >= budget(0) 命中同一
  # 预算检查分支，首次请求即 429，确定性强且不依赖上游。
  create_key '{"role":"user","allowed_models":"*","rpm_limit":600,"daily_budget_usd":0}'
  chat "$CHAT_BODY" -H "Authorization: Bearer $K_KEY"
  expect_status 429 || rc=1
  expect_detail || rc=1
  expect_retry_after 1 86400 || rc=1     # 到北京时间次日 0 点的秒数
  return $rc
}

# ---------- 验收 6：/health 无 key → 200 ----------
test_6() {
  local rc=0
  req GET /health
  expect_status 200 || rc=1
  if printf '%s' "$R_BODY" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
    echo "    body 含 status=ok ✓"
  else
    echo "    body 异常：$R_BODY ✗"; rc=1
  fi
  return $rc
}

run_test "验收 1/6：无 key → 401"                 test_1
run_test "验收 2/6：无效 / 已吊销 key → 401"      test_2
run_test "验收 3/6：user 越权模型 → 403"          test_3
run_test "验收 4/6：超 RPM → 429 + Retry-After"  test_4
run_test "验收 5/6：日预算 100% → 429"            test_5
run_test "验收 6/6：/health 无 key → 200"         test_6

# ---------- 手工补测：两个边界 ----------
echo "== 边界补测 A：model 不可用 → 400 =="
# 注：完全不传 model 时网关会回落 default_models，不会 400；400 对应"指定的模型不可用"
create_key '{"role":"user"}'
chat "$CHAT_BAD_MODEL" -H "Authorization: Bearer $K_KEY"
if expect_status 400 && expect_detail; then EDGE_PASS=$((EDGE_PASS+1)); else EDGE_FAIL=$((EDGE_FAIL+1)); fi
echo

echo "== 边界补测 B：Authorization 缺 Bearer 前缀 → 401 =="
chat "$CHAT_BODY" -H "Authorization: sk_live_no_bearer_prefix"
if expect_status 401 && expect_detail; then EDGE_PASS=$((EDGE_PASS+1)); else EDGE_FAIL=$((EDGE_FAIL+1)); fi
echo

# ---------- 汇总 ----------
echo "========================================"
echo "结果：$PASS passed / $FAIL failed"
echo "边界补测：$EDGE_PASS passed / $EDGE_FAIL failed"
[ "$FAIL" -eq 0 ] && [ "$EDGE_FAIL" -eq 0 ]
