"""统一异常：中间件捕获后转为标准 JSON 错误响应。"""

from __future__ import annotations


class ApiError(Exception):
    """鉴权/授权/限流失败；status_code 为 HTTP 状态码，retry_after 仅 429 使用。"""

    def __init__(self, status_code: int, detail: str, retry_after: int | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after
