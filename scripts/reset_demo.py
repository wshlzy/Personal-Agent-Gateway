"""重置网关 demo 相关的 Redis 计数器与 DB 状态。

用法：
  python scripts/reset_demo.py                  # 只清 register 限流键（最常用）
  python scripts/reset_demo.py --all            # 清所有限流 + 预算 Redis 键
  python scripts/reset_demo.py --key-id abc123  # 只清某一把 key 的 RPM + 预算
  python scripts/reset_demo.py --revoke-db      # 吊销 DB 里所有未过期的 demo key
  python scripts/reset_demo.py --dry-run        # 打印将要清的 key，不真删

所有参数可组合：--all --revoke-db --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# 允许直接 `python scripts/reset_demo.py` 执行时 import src
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_env() -> None:
    """项目根目录 .env 中已有的变量自动注入（网关也用同一套）。"""
    dotenv = ROOT / ".env"
    if not dotenv.exists():
        return
    for line in dotenv.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


async def _redis_scan_delete(redis, pattern: str, dry_run: bool) -> int:
    """扫描并删除匹配 pattern 的 Redis key，返回删除数。"""
    count = 0
    async for key in redis.scan_iter(pattern):
        count += 1
        if dry_run:
            print(f"  [DRY] would DEL {key}")
        else:
            await redis.delete(key)
    return count


async def main() -> None:
    parser = argparse.ArgumentParser(description="重置 demo/register 相关状态")
    parser.add_argument("--all", action="store_true", help="清除 ratelimit:* 和 budget:*（所有 key 的 RPM + 预算）")
    parser.add_argument("--key-id", help="只清某一把 key 的 RPM + 预算（指定 key_id）")
    parser.add_argument("--revoke-db", action="store_true", help="吊销 DB 里所有未吊销的 demo key")
    parser.add_argument("--dry-run", action="store_true", help="只打印将清的 key，不真删")
    args = parser.parse_args()

    _load_env()

    import redis.asyncio as aioredis

    REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)

    total = 0

    # --- 1. register 限流键（默认清） ---
    print(f"\n[1] register 限流键（ip + total，匹配 register:*）")
    n = await _redis_scan_delete(redis, "register:*", args.dry_run)
    total += n
    print(f"  匹配 {n} 个 key")

    # --- 2. 可选：全限流 + 预算 ---
    if args.all:
        print(f"\n[2] 全限流键 ratelimit:*")
        n = await _redis_scan_delete(redis, "ratelimit:*", args.dry_run)
        total += n
        print(f"  匹配 {n} 个 key")

        print(f"\n[3] 全预算键 budget:*")
        n = await _redis_scan_delete(redis, "budget:*", args.dry_run)
        total += n
        print(f"  匹配 {n} 个 key")

    # --- 3. 可选：单 key ---
    if args.key_id:
        print(f"\n[*] 指定 key_id={args.key_id} 的 RPM + 预算")
        n1 = await _redis_scan_delete(redis, f"ratelimit:{args.key_id}:*", args.dry_run)
        n2 = await _redis_scan_delete(redis, f"budget:{args.key_id}:*", args.dry_run)
        total += n1 + n2
        print(f"  ratelimit:{n1}  budget:{n2}")

    # --- 4. 可选：吊销 DB demo key ---
    if args.revoke_db:
        from src.security.keys import open_key_store

        DATABASE_URL = os.environ.get("DATABASE_URL", "")
        KEYS_DB = os.environ.get("KEYS_DB", "data/keys.db")
        store = open_key_store(KEYS_DB, DATABASE_URL or None)

        print(f"\n[4] 吊销 DB 中所有未吊销的 demo key")
        keys = await store.list_keys()
        # demo key 特征：role=user 且有 expires_at（admin 手动签的永不过期 key 不碰）
        demo_keys = [k for k in keys if k.get("expires_at") and not k.get("revoked_at")]
        print(f"  共发现 {len(demo_keys)} 个未吊销 demo key")
        for k in demo_keys:
            if args.dry_run:
                print(f"  [DRY] would revoke id={k['id']} expires_at={k['expires_at']}")
            else:
                ok = await store.revoke(k["id"])
                print(f"  revoke id={k['id']}: {'OK' if ok else 'FAIL'}")

        if DATABASE_URL:
            await store.aclose()

    await redis.aclose()

    if args.dry_run:
        print(f"\n[DRY RUN] 共匹配 {total} 个 Redis key，未实际删除。去掉 --dry-run 执行。")
    else:
        print(f"\n完成：共清除 {total} 个 Redis key。")


if __name__ == "__main__":
    asyncio.run(main())
