"""数据存储包：按租户隔离的会话 / 知识 / 画像。"""

from src.storage.base import (
    GUEST_PREFIX,
    GUEST_TTL_HOURS,
    DataStore,
    ForbiddenWrite,
    InvalidTenantId,
    assert_writable,
    is_guest,
    new_guest_tenant,
    validate_tenant_id,
)
from src.storage.factory import app_mode, build_data_store, data_dir
from src.storage.file_store import FileDataStore
from src.storage.postgres_store import PostgresDataStore

__all__ = [
    "GUEST_PREFIX",
    "GUEST_TTL_HOURS",
    "DataStore",
    "FileDataStore",
    "ForbiddenWrite",
    "InvalidTenantId",
    "PostgresDataStore",
    "app_mode",
    "assert_writable",
    "build_data_store",
    "data_dir",
    "is_guest",
    "new_guest_tenant",
    "validate_tenant_id",
]
