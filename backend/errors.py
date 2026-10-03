"""业务错误类型。"""
from __future__ import annotations


class BusinessError(Exception):
    """400：规则不允许（状态非法、已过闸、未确认恢复等）。"""

    def __init__(self, message: str, code: str = "business_rule"):
        super().__init__(message)
        self.message = message
        self.code = code


class ConflictError(Exception):
    """409：并发冲突（同一张票重复处理、名额竞争失败）。"""

    def __init__(self, message: str = "操作冲突，请刷新后重试", code: str = "conflict"):
        super().__init__(message)
        self.message = message
        self.code = code
