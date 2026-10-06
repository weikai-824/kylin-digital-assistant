"""OA 身份校验端口。

应用层（identity service）只依赖本协议，不感知 OAClient 等具体适配器实现，
由组合根（bootstrap）负责把适配器注入进来。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class OAIdentityPort(Protocol):
    """工号 → 员工身份 查询的最小契约。"""

    def query_employee_by_job_num(self, job_num: str) -> dict[str, str]:
        """按工号查询员工；返回含 userid / job_num / username / dept 的字典。"""
        ...
