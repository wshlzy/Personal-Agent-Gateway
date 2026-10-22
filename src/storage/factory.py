"""存储工厂：按 APP_MODE 选择实现。

- APP_MODE=private（默认）：FileDataStore(DATA_DIR)，默认 ./data/private
- APP_MODE=demo：PostgresDataStore(DATABASE_URL)，要求 Neon Postgres
- 启动守卫：demo 模式下 DATA_DIR 仍指向 private 目录（说明配置混用）→ 直接退出
"""

from __future__ import annotations

import os
from pathlib import Path

from src.config_loader import BASE_DIR
from src.storage.base import DataStore
from src.storage.file_store import FileDataStore
from src.storage.postgres_store import PostgresDataStore

DEFAULT_DATA_DIR = "./data/private"
SEED_DIR = BASE_DIR / "data" / "demo" / "seed"

_APP_MODE = os.environ.get("APP_MODE", "private").strip().lower()
_DATA_DIR = os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)
_DATABASE_URL = os.environ.get("DATABASE_URL", "")


def app_mode() -> str:
    """当前模式（每次读环境变量，便于测试与热切换）。"""
    return os.environ.get("APP_MODE", _APP_MODE).strip().lower()


def data_dir() -> Path:
    raw = os.environ.get("DATA_DIR", _DATA_DIR)
    p = Path(raw)
    return p if p.is_absolute() else (BASE_DIR / p).resolve()


def build_data_store(
    mode: str | None = None,
    directory: Path | str | None = None,
    database_url: str | None = None,
) -> DataStore:
    mode = (mode or app_mode()).strip().lower()
    raw_dir = str(directory if directory is not None else data_dir())
    url = database_url if database_url is not None else os.environ.get("DATABASE_URL", "")

    if mode == "demo":
        # 守卫：演示环境不允许把数据落回本地 private 目录（通常意味着忘了配 DATABASE_URL）
        if "private" in raw_dir.replace("\\", "/").lower():
            raise SystemExit(
                "[启动失败] APP_MODE=demo 但 DATA_DIR 指向 private 目录"
                f"（{raw_dir}）。演示模式必须使用 Postgres：请设置 DATABASE_URL，"
                "并确保 DATA_DIR 不使用 private（或留空）。"
            )
        if not url:
            raise SystemExit(
                "[启动失败] APP_MODE=demo 需要配置 DATABASE_URL（Neon Postgres 连接串，"
                "需带 ?sslmode=require）。"
            )
        return PostgresDataStore(url, seed_dir=SEED_DIR)

    if mode != "private":
        raise SystemExit(f"[启动失败] APP_MODE 只能是 private 或 demo，当前：{mode!r}")

    p = Path(raw_dir)
    return FileDataStore(p if p.is_absolute() else (BASE_DIR / p))
