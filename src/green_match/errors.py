"""领域错误：携带 HTTP 状态码与结构化细节，便于 API 层直接翻译。"""

from __future__ import annotations

from typing import Any, Optional


class DomainError(Exception):
    """业务规则冲突或输入非法时抛出，API 层据此返回结构化错误响应。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
