"""鉴权与限流精简包（个人 AI 网关，演示级多用户）。

链路：/health 豁免 → 鉴权(401) → 授权(403) → 限流(429) → 密钥代理调模型。
- 虚拟密钥 sk_live_*，库中只存 SHA-256，明文创建时返回一次
- 角色 admin / user，user 受 allowed_models 限制
- RPM 与日预算（USD）两维限流，Redis 原子计数
- /admin/* 由环境变量 ADMIN_TOKEN 保护，不走虚拟密钥
"""

from src.security.exceptions import ApiError
from src.security.keys import AuthContext, KeyStore, authenticate, authorize
from src.security.rate_limit import RateLimiter

__all__ = [
    "ApiError",
    "AuthContext",
    "KeyStore",
    "RateLimiter",
    "authenticate",
    "authorize",
]
