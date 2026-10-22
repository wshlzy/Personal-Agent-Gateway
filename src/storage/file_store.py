"""FileDataStore：本地个人环境（APP_MODE=private），无外部依赖。

目录结构：
    DATA_DIR/<tenant_id>/conversations/<file_id>.json
    DATA_DIR/<tenant_id>/knowledge/<file_id>.json
    DATA_DIR/<tenant_id>/profile.json

- tenant_id 先用正则校验再拼路径（防目录穿越）
- 写文件一律 tmp + fsync + rename 原子替换，避免半截文件
- 文件名对非法 id（如中文主题）用 sha256 摘要，真实 id 存在 JSON 内
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.storage.base import (
    GUEST_TTL_HOURS,
    DataStore,
    InvalidTenantId,
    assert_writable,
    is_guest,
    validate_tenant_id,
)

_FILE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _file_id(raw: str) -> str:
    """把任意 id 映射成安全文件名；非法字符走摘要，保证不越出租户目录。"""
    if _FILE_ID_RE.fullmatch(raw or ""):
        return raw
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _atomic_write(path: Path, data: str) -> None:
    """原子写：同目录临时文件 → fsync → os.replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            # 目录项也落盘（Windows 不支持对目录 fsync）
            dfd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class FileDataStore(DataStore):
    """本地文件实现；root 下的每个一级目录就是一个租户。"""

    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------- 路径 ----------
    def _tenant_dir(self, tenant_id: str, *, create: bool = False) -> Path:
        validate_tenant_id(tenant_id)  # 必须在拼路径之前
        d = (self.root / tenant_id).resolve()
        # 双保险：解析后必须仍在 root 内（防 ../ 或符号链接逃逸）
        if d != self.root and self.root not in d.parents:
            raise InvalidTenantId(f"tenant_id 越界：{tenant_id!r}")
        if create:
            (d / "conversations").mkdir(parents=True, exist_ok=True)
            (d / "knowledge").mkdir(parents=True, exist_ok=True)
        return d

    def _conv_path(self, tenant_id: str, conversation_id: str) -> Path:
        return self._tenant_dir(tenant_id) / "conversations" / f"{_file_id(conversation_id)}.json"

    def _knowledge_path(self, tenant_id: str, item_id: str) -> Path:
        return self._tenant_dir(tenant_id) / "knowledge" / f"{_file_id(item_id)}.json"

    # ---------- 会话 ----------
    async def list_conversations(self, tenant_id: str) -> list[dict]:
        d = self._tenant_dir(tenant_id) / "conversations"
        if not d.exists():
            return []
        items: list[dict] = []
        for p in d.glob("*.json"):
            data = _read_json(p)
            if not data:
                continue
            items.append(
                {
                    "id": data.get("id", p.stem),
                    "title": data.get("title", ""),
                    "created_at": data.get("created_at", ""),
                    "message_count": len(data.get("messages") or []),
                }
            )
        items.sort(key=lambda x: x["created_at"])
        return items

    async def get_conversation(self, tenant_id: str, conversation_id: str) -> dict | None:
        path = self._conv_path(tenant_id, conversation_id)
        data = _read_json(path)
        if not data or data.get("id") != conversation_id:
            # 哈希文件名存在碰撞可能：以文件内真实 id 为准，不匹配即视为不存在
            return None
        return {
            "id": data.get("id", conversation_id),
            "tenant_id": tenant_id,
            "title": data.get("title", ""),
            "messages": data.get("messages") or [],
            "created_at": data.get("created_at", ""),
        }

    async def save_conversation(
        self,
        tenant_id: str,
        conversation_id: str,
        title: str,
        messages: list[dict],
        created_at: str | None = None,
    ) -> dict:
        path = self._conv_path(tenant_id, conversation_id)
        existing = _read_json(path)
        stamp = (
            (existing or {}).get("created_at")
            if existing and existing.get("id") == conversation_id
            else None
        ) or created_at or _now_iso()
        record = {
            "id": conversation_id,
            "tenant_id": tenant_id,
            "title": title,
            "messages": messages,
            "created_at": stamp,
        }
        _atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2))
        return record

    async def delete_conversation(self, tenant_id: str, conversation_id: str) -> bool:
        path = self._conv_path(tenant_id, conversation_id)
        data = _read_json(path)
        if not data or data.get("id") != conversation_id:
            return False
        path.unlink(missing_ok=True)
        return True

    # ---------- 知识 ----------
    async def list_knowledge(self, tenant_id: str) -> list[dict]:
        d = self._tenant_dir(tenant_id) / "knowledge"
        if not d.exists():
            return []
        items: list[dict] = []
        for p in d.glob("*.json"):
            data = _read_json(p)
            if not data:
                continue
            items.append(
                {
                    "id": data.get("id", p.stem),
                    "title": data.get("title", ""),
                    "created_at": data.get("created_at", ""),
                }
            )
        items.sort(key=lambda x: x["created_at"])
        return items

    async def get_knowledge(self, tenant_id: str, item_id: str) -> dict | None:
        data = _read_json(self._knowledge_path(tenant_id, item_id))
        if not data or data.get("id") != item_id:
            return None
        return {
            "id": data["id"],
            "tenant_id": tenant_id,
            "title": data.get("title", ""),
            "content": data.get("content", ""),
            "created_at": data.get("created_at", ""),
        }

    async def save_knowledge(
        self, tenant_id: str, item_id: str, title: str, content: str
    ) -> dict:
        assert_writable(tenant_id, "knowledge")
        path = self._knowledge_path(tenant_id, item_id)
        existing = _read_json(path)
        stamp = (
            (existing or {}).get("created_at")
            if existing and existing.get("id") == item_id
            else None
        ) or _now_iso()
        record = {
            "id": item_id,
            "tenant_id": tenant_id,
            "title": title,
            "content": content,
            "created_at": stamp,
        }
        _atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2))
        return record

    # ---------- 画像 ----------
    async def get_profile(self, tenant_id: str) -> dict | None:
        data = _read_json(self._tenant_dir(tenant_id) / "profile.json")
        if not data:
            return None
        return {
            "tenant_id": tenant_id,
            "profile": data.get("profile") or {},
            "updated_at": data.get("updated_at", ""),
        }

    async def save_profile(self, tenant_id: str, profile: dict) -> dict:
        assert_writable(tenant_id, "profile")
        record = {"tenant_id": tenant_id, "profile": profile, "updated_at": _now_iso()}
        _atomic_write(
            self._tenant_dir(tenant_id, create=True) / "profile.json",
            json.dumps(record, ensure_ascii=False, indent=2),
        )
        return record

    # ---------- 维护 ----------
    async def purge_expired_guests(self) -> int:
        """删除过期访客会话（按 created_at，超过 GUEST_TTL_HOURS）。"""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=GUEST_TTL_HOURS)
        removed = 0
        for tenant_dir in self.root.iterdir():
            if not tenant_dir.is_dir() or not is_guest(tenant_dir.name):
                continue
            conv_dir = tenant_dir / "conversations"
            if not conv_dir.exists():
                continue
            for p in conv_dir.glob("*.json"):
                data = _read_json(p)
                if not data:
                    continue
                created = _parse_dt(data.get("created_at"))
                if created and created < cutoff:
                    p.unlink(missing_ok=True)
                    removed += 1
            # 租户目录空了就整体清掉（访客本就不该残留）
            try:
                if not any(tenant_dir.iterdir()):
                    tenant_dir.rmdir()
            except OSError:
                pass
        return removed

    def clear_tenant(self, tenant_id: str) -> None:
        """删除整个租户目录（仅测试 / 运维使用）。"""
        d = self._tenant_dir(tenant_id)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
