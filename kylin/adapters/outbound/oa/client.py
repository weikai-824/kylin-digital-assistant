"""OA HTTP and document source adapter."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

import httpx

from kylin.adapters.outbound.oa.auth import OAAuth
from kylin.adapters.outbound.oa.config import OASettings
from kylin.platform.runtime.net import get_httpx_client
from kylin.platform.runtime.redact import redact
from kylin.ports.data.oa_sync import OASyncSettings
from kylin.platform.observability.logging import build_logger

logger = build_logger()


class OAClient:
    """Implement the OA source port over the vendor HTTP API."""

    def __init__(self, settings: OASettings):
        self.settings = settings
        self._http = get_httpx_client(
            timeout=settings.request_timeout,
            follow_redirects=True,
        )
        self.auth = OAAuth(settings, client=self._http)
        self._userid = settings.userid.strip()

    def close(self) -> None:
        self.auth.close()
        self._http.close()

    @property
    def userid(self) -> str:
        if not self._userid:
            raise RuntimeError("OA userid 未配置，请在 oa_settings.yaml 填写 userid 或 job_num/account")
        return self._userid

    def set_userid(self, userid: str) -> None:
        self._userid = (userid or "").strip()

    def resolve_userid(self, settings: OASyncSettings) -> str:
        """Resolve the account id without exposing the HTTP client to the application."""
        if settings.userid.strip():
            return settings.userid.strip()
        body: dict[str, Any] = {
            "current": 1,
            "pageSize": 10,
            "userStatusList": ["normal"],
            "containUserInfo": False,
        }
        if settings.job_num.strip():
            body["jobNumList"] = [settings.job_num.strip()]
        elif settings.account.strip():
            body["account"] = settings.account.strip()
            body["needAccountInfo"] = True
        else:
            raise RuntimeError("请在 oa_settings.yaml 配置 userid，或配置 job_num / account 以便自动解析")

        url = f"{settings.base_url.rstrip('/')}/papi/openapi/api/hrm/restful/queryEmployee"
        response = self._http.post(url, json=body, **self._auth_kwargs())
        response.raise_for_status()
        data = response.json()
        if data.get("fail") is True or str(data.get("code", "0")) not in ("0", "200", ""):
            raise RuntimeError(f"queryEmployee 失败: {redact(data)}")
        ensure_api_ok(data)
        employees = _extract_employees(data)
        if not employees:
            raise RuntimeError(f"queryEmployee 未找到人员: body={body}, resp={redact(data)}")
        userid = str(employees[0].get("id") or "").strip()
        if not userid:
            raise RuntimeError(f"人员记录缺少 id: {employees[0]}")
        logger.info(
            f"已解析 OA userid={userid}, job_num={employees[0].get('job_num')}, "
            f"username={employees[0].get('username')}"
        )
        return userid

    def query_employee_by_job_num(self, job_num: str) -> dict[str, str]:
        """按工号查询单个员工身份（供长期记忆身份校验使用）。

        返回 {userid, job_num, username, dept}；查无此人抛 RuntimeError。
        与 ``resolve_userid`` 的区别：后者解析配置里固定的同步身份（OASyncSettings），
        本方法校验任意登录工号，供 WebUI 工号登录链路使用。
        """
        job_num = (job_num or "").strip()
        if not job_num:
            raise RuntimeError("工号不能为空")
        body: dict[str, Any] = {
            "current": 1,
            "pageSize": 10,
            "userStatusList": ["normal"],
            "containUserInfo": False,
            "jobNumList": [job_num],
        }
        url = (
            f"{self.settings.base_url.rstrip('/')}"
            f"/papi/openapi/api/hrm/restful/queryEmployee"
        )
        response = self._http.post(url, json=body, **self._auth_kwargs())
        response.raise_for_status()
        data = response.json()
        if data.get("fail") is True or str(data.get("code", "0")) not in ("0", "200", ""):
            raise RuntimeError(f"queryEmployee 失败: {redact(data)}")
        ensure_api_ok(data)
        employees = _extract_employees(data)
        if not employees:
            raise RuntimeError(f"queryEmployee 未找到工号 {job_num}: resp={redact(data)}")
        employee = employees[0]
        userid = str(employee.get("id") or "").strip()
        if not userid:
            raise RuntimeError(f"人员记录缺少 id: {employee}")
        username = str(employee.get("username") or employee.get("name") or "").strip() or job_num
        dept = _pick_dept(employee)
        logger.info(
            "已校验工号 job_num=%s userid=%s username=%s dept=%s",
            job_num, userid, username, dept or "未提供",
        )
        return {
            "userid": userid,
            "job_num": job_num,
            "username": username,
            "dept": dept,
        }

    def iter_documents(self, page_size: int) -> Iterator[Mapping[str, Any]]:
        page_no = 1
        while True:
            data = self.post_json(
                "/papi/openapi/api/doc/documents/list/all",
                {
                    "pageNo": page_no,
                    "pageSize": page_size,
                    "order": {"property": "last_update_time", "direction": "DESC"},
                },
            )
            payload = data.get("data") or {}
            documents = payload.get("documents") or []
            if not documents:
                return
            yield from documents
            total = int(payload.get("total") or 0)
            if page_no * page_size >= total:
                return
            page_no += 1

    def list_attachments(self, doc_id: str) -> Sequence[Mapping[str, Any]]:
        data = self.get_json(f"/papi/openapi/api/doc/v2/attList/{doc_id}")
        return tuple(data.get("data") or [])

    def download_file(self, file_id: str, module: str = "") -> bytes:
        params = {"module": module} if module else None
        return self.get_bytes(
            f"/papi/openapi/api/file/v2/common/download/{file_id}",
            params=params,
        )

    def get_document_content(self, doc_id: str) -> Mapping[str, Any]:
        data = self.get_json(
            "/papi/openapi/api/doc/document/v2/info",
            params={"id": doc_id},
        )
        return data.get("data") or {}

    def get_document_department_id(self, doc_id: str) -> str:
        """Return the owning department id of a document ('' when unavailable).

        OA 文档详情接口 ``document/v2/info`` 返回 ``departmentId``（部门 ID，
        与人员接口的 ``department`` 同体系）；取不到返回空串（视为未打标）。
        """
        try:
            info = self.get_document_content(doc_id)
        except Exception as error:
            logger.warning(f"获取文档部门失败 doc_id={doc_id}: {error}")
            return ""
        return str(info.get("departmentId") or "").strip()

    def extract_content(self, info: Mapping[str, Any]) -> str | None:
        if not info:
            return None
        for key in (
            "content",
            "htmlContent",
            "html_content",
            "docContent",
            "doc_content",
            "text",
            "body",
            "richContent",
            "rich_content",
        ):
            value = info.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for key in ("document", "doc", "detail", "info"):
            nested = info.get(key)
            if isinstance(nested, Mapping):
                content = self.extract_content(nested)
                if content:
                    return content
        return None

    def _auth_kwargs(self) -> dict[str, dict[str, str]]:
        """按 ``auth_in_header`` 生成 httpx 鉴权参数。

        默认（false）返回 query 参数 ``?access_token=...``，与既有行为一致；
        置 true 时改走 ``Authorization: Bearer`` 头，token 不再进访问日志。
        """
        token = self.auth.get_access_token()
        if self.settings.auth_in_header:
            return {"headers": {"Authorization": f"Bearer {token}"}}
        return {"params": {"access_token": token}}

    def _url(self, path: str, extra_params: Mapping[str, Any] | None = None) -> str:
        params: dict[str, Any] = {"userid": self.userid}
        if extra_params:
            params.update({key: value for key, value in extra_params.items() if value is not None and value != ""})
        normalized_path = path if path.startswith("/") else f"/{path}"
        return f"{self.settings.base_url.rstrip('/')}{normalized_path}?{urlencode(params)}"

    def _send(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None,
        json_body: Any,
        data: Any,
    ) -> httpx.Response:
        """发一次请求：鉴权方式由 ``_auth_kwargs`` 决定，调用方无需感知。"""
        auth = self._auth_kwargs()
        merged: dict[str, Any] = dict(params or {})
        merged.update(auth.get("params", {}))
        return self._http.request(
            method,
            self._url(path, merged),
            json=json_body,
            data=data,
            headers=auth.get("headers"),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        retry_auth: bool = True,
    ) -> httpx.Response:
        response = self._send(method, path, params, json_body, data)
        if retry_auth and response.status_code in (401, 403):
            logger.warning("OA 请求鉴权失败，强制刷新 token 后重试一次")
            self.auth.get_access_token(force_refresh=True)
            response = self._send(method, path, params, json_body, data)
        return response

    def get_json(self, path: str, *, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        response = self.request("GET", path, params=params)
        response.raise_for_status()
        data = response.json()
        ensure_api_ok(data)
        return data

    def post_json(
        self,
        path: str,
        body: dict[str, Any],
        *,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self.request("POST", path, params=params, json_body=body)
        response.raise_for_status()
        data = response.json()
        ensure_api_ok(data)
        return data

    def get_bytes(self, path: str, *, params: Mapping[str, Any] | None = None) -> bytes:
        response = self.request("GET", path, params=params)
        response.raise_for_status()
        if "application/json" in (response.headers.get("content-type") or "").lower():
            data = response.json()
            ensure_api_ok(data)
            raise RuntimeError(f"期望文件流，收到 JSON: {redact(data)}")
        return response.content


def _extract_employees(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    payload = data.get("data")
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in ("records", "list", "employees", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


# 部门字段在 OA 接口中的常见命名（扁平字段或嵌套对象两种形态）
_DEPT_KEYS = (
    "dept",
    "dept_name",
    "deptName",
    "department",
    "department_name",
    "departmentName",
    "org_name",
    "orgName",
    "organize_name",
    "organizeName",
    "belong_org",
)


def _pick_dept(employee: Mapping[str, Any]) -> str:
    """从人员记录中宽松提取部门名；取不到返回空串（不阻塞身份校验）。"""
    for key in _DEPT_KEYS:
        value = employee.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, Mapping):
            for sub in ("name", "deptName", "departmentName"):
                nested = value.get(sub)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return ""


def ensure_api_ok(data: Mapping[str, Any]) -> None:
    message = data.get("message")
    if isinstance(message, Mapping):
        error_code = message.get("errcode")
        if error_code is not None and str(error_code) not in ("0", ""):
            raise RuntimeError(
                f"OA API 失败: errcode={error_code}, errmsg={message.get('errmsg')}, raw={redact(data)}"
            )
    error_code = data.get("errcode")
    if error_code is not None and str(error_code) not in ("0", ""):
        raise RuntimeError(f"OA API 失败: errcode={error_code}, errmsg={data.get('errmsg')}, raw={redact(data)}")
