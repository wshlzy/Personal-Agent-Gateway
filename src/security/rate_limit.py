"""两维限流（Redis 原子计数）：RPM + 日预算（USD）。

键结构：
  ratelimit:{key_id}:{epoch_minute}   INCR 计数，TTL 70s
  budget:{key_id}:{YYYYMMDD}          INCRBYFLOAT 累计（北京时间），TTL 48h

行为约定：
- RPM 超限 → 429 + Retry-After（到下一分钟桶的秒数）
- 日花费 ≥ 日预算 → 429 + Retry-After（到北京时间次日 0 点的秒数），直接阻断该 key 新请求
- Redis 不可达时限流检查 fail-closed（503，避免限流失效裸奔）；
  记账（record_cost）失败只打日志，不阻塞响应
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from src.security.exceptions import ApiError
from src.security.keys import AuthContext

_CN_TZ = timezone(timedelta(hours=8))  # 北京时间（日预算按自然日结算）


def _minute_epoch(now: float) -> int:
    return int(now // 60)


def _budget_day() -> str:
    return datetime.now(_CN_TZ).strftime("%Y%m%d")


def _seconds_to_next_minute(now: float) -> int:
    return max(1, 60 - int(now % 60))


def _seconds_to_next_day() -> int:
    now = datetime.now(_CN_TZ)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


class RateLimiter:
    """封装 Redis 计数；redis_client 用 redis.asyncio 实例，测试可注入 fakeredis。"""

    def __init__(self, redis_client) -> None:
        self._redis = redis_client

    # ---------- 限流检查（请求前） ----------
    async def enforce(self, ctx: AuthContext) -> None:
        """RPM + 日预算检查；超限抛 ApiError(429)，Redis 故障抛 ApiError(503)。"""
        now = time.time()
        try:
            # 1) RPM：INCR 当前分钟桶，首次置 TTL
            rpm_key = f"ratelimit:{ctx.key_id}:{_minute_epoch(now)}"
            count = await self._redis.incr(rpm_key)
            if count == 1:
                await self._redis.expire(rpm_key, 70)
            if count > ctx.rpm_limit:
                raise ApiError(
                    429,
                    f"超出每分钟请求上限（{ctx.rpm_limit} 次）",
                    retry_after=_seconds_to_next_minute(now),
                )

            # 2) 日预算：达到 100% 直接阻断
            budget_key = f"budget:{ctx.key_id}:{_budget_day()}"
            spent_raw = await self._redis.get(budget_key)
            spent = float(spent_raw) if spent_raw else 0.0
            if spent >= ctx.daily_budget_usd:
                raise ApiError(
                    429,
                    f"已达当日预算上限（${ctx.daily_budget_usd:.4f}），明日重置",
                    retry_after=_seconds_to_next_day(),
                )
        except ApiError:
            raise
        except Exception as exc:  # Redis 连接错误等 → fail-closed
            raise ApiError(503, f"限流服务不可用：{exc}") from exc

    # ---------- 成本记账（成功响应后） ----------
    async def record_cost(self, ctx: AuthContext, cost_usd: float) -> None:
        """按 INCRBYFLOAT 累计当日花费；失败只打日志不阻塞。"""
        if cost_usd <= 0:
            return
        try:
            budget_key = f"budget:{ctx.key_id}:{_budget_day()}"
            await self._redis.incrbyfloat(budget_key, float(cost_usd))
            await self._redis.expire(budget_key, 48 * 3600)
        except Exception as exc:
            print(f"  [记账失败，已跳过] key={ctx.key_id} cost=${cost_usd:.6f}：{exc}")

    # ---------- 管理端点展示 ----------
    async def spent_today(self, key_id: str) -> float:
        """返回该 key 当日已花费 USD；Redis 故障按 0 计。"""
        try:
            raw = await self._redis.get(f"budget:{key_id}:{_budget_day()}")
            return round(float(raw), 6) if raw else 0.0
        except Exception:
            return 0.0
