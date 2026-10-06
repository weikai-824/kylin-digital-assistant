"""知识库台账 HTTP 适配器。

该模块只负责请求参数和响应模型转换，业务规则由 ``KnowledgeIngestionPort``
实现，避免 FastAPI 路由直接依赖底层存储和处理实现。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from typing import List

from fastapi import Body, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from kylin.ports.application.knowledge_ingestion import KnowledgeIngestionPort
from kylin.ports.data.ingestion import IngestDocument, IngestionProgress
from kylin.applications.knowledge_ingestion.reserved import ManagedKnowledgeBaseError
from kylin.platform.runtime.schemas import BaseResponse, ListResponse
from kylin.platform.config.settings import Settings


class KnowledgeBaseHttpAdapter:
    """把知识库台账请求转换为入库应用端口调用。"""

    def __init__(self, service: KnowledgeIngestionPort):
        self._service = service

    @staticmethod
    def _managed_error(error: ManagedKnowledgeBaseError) -> BaseResponse:
        return BaseResponse(code=403, msg=str(error))

    def list_kbs(self) -> ListResponse:
        """列出知识库，并保留旧 API 的字段命名；托管库标记 managed=true（可查看不可改）.

        工单库（TICKET_KB_NAME）是问答时的隐式并行检索分支，不面向用户选择，
        故从列表里剔除。
        """
        managed = set()
        names_fn = getattr(self._service, "managed_knowledge_base_names", None)
        if callable(names_fn):
            managed = set(names_fn())
        ticket_kb = Settings.kb_settings.TICKET_KB_NAME
        data = [
            {
                "kb_name": item.name,
                "kb_info": item.info,
                "vs_type": item.vector_store_type,
                "embed_model": item.embedding_model,
                "managed": item.name in managed,
            }
            for item in self._service.list_knowledge_bases()
            if item.name != ticket_kb
        ]
        return ListResponse(data=data)

    def list_files(self, knowledge_base_name: str) -> ListResponse:
        """列出文件目录与索引台账的兼容视图。"""
        try:
            details = self._service.list_file_details(knowledge_base_name)
        except LookupError as error:
            return ListResponse(code=404, msg=str(error), data=[])
        return ListResponse(
            data=[
                {
                    "No": item.sequence,
                    "kb_name": knowledge_base_name,
                    "file_name": item.filename,
                    "file_ext": item.file_ext,
                    "docs_count": item.docs_count,
                    "create_time": item.create_time,
                    "in_folder": item.in_folder,
                    "in_db": item.in_db,
                    "department_id": item.department_id,
                    "access_depts": list(item.access_depts),
                }
                for item in details
            ]
        )

    def create_kb(
        self,
        knowledge_base_name: str = Body(..., examples=["oa_manuals"]),
        vector_store_type: str = Body("dual", description="仅支持 dual"),
        kb_info: str = Body("", description="知识库内容简介"),
        embed_model: str | None = Body(None, description="Embedding 模型"),
    ) -> BaseResponse:
        """创建知识库台账及其双索引。"""
        if (vector_store_type or "dual").lower() != "dual":
            return BaseResponse(code=400, msg="仅支持 dual 知识库类型")
        try:
            self._service.create_knowledge_base(
                knowledge_base_name,
                info=kb_info,
                embedding_model=embed_model,
            )
        except ManagedKnowledgeBaseError as error:
            return self._managed_error(error)
        except ValueError as error:
            message = str(error)
            code = 404 if "同名" in message else 400
            return BaseResponse(code=code, msg=message)
        except Exception as error:
            return BaseResponse(code=500, msg=f"创建知识库出错： {error}")
        return BaseResponse(code=200, msg=f"已新增知识库 {knowledge_base_name.strip()}")

    def delete_kb(
        self,
        knowledge_base_name: str = Body(..., examples=["oa_manuals"]),
    ) -> BaseResponse:
        """删除知识库及其索引和文件。"""
        try:
            deleted = self._service.delete_knowledge_base(knowledge_base_name)
        except ManagedKnowledgeBaseError as error:
            return self._managed_error(error)
        except LookupError as error:
            return BaseResponse(code=404, msg=str(error))
        except Exception as error:
            return BaseResponse(code=500, msg=f"删除知识库时出现意外： {error}")
        if not deleted:
            return BaseResponse(code=500, msg=f"删除知识库失败 {knowledge_base_name}")
        return BaseResponse(code=200, msg=f"成功删除知识库 {knowledge_base_name}")

    def update_info(
        self,
        knowledge_base_name: str = Body(..., examples=["oa_manuals"]),
        kb_info: str = Body(..., description="知识库介绍"),
    ) -> BaseResponse:
        """更新知识库简介。"""
        try:
            self._service.update_info(knowledge_base_name, kb_info)
        except ManagedKnowledgeBaseError as error:
            return self._managed_error(error)
        except LookupError as error:
            return BaseResponse(code=404, msg=str(error))
        except Exception as error:
            return BaseResponse(code=500, msg=f"更新知识库介绍失败： {error}")
        return BaseResponse(
            code=200,
            msg="知识库介绍修改完成",
            data={"kb_info": kb_info},
        )

    def upload_docs(
        self,
        files: List[UploadFile] = File(..., description="上传文件，支持多文件"),
        knowledge_base_name: str = Form(..., description="知识库名称"),
        override: bool = Form(False, description="覆盖已有文件"),
        to_vector_store: bool = Form(True, description="上传文件后是否进行向量化"),
        chunk_size: int | None = Form(None, description="子块最大长度"),
        chunk_overlap: int | None = Form(None, description="子块重合长度"),
        docs: str = Form("", description="自定义 docs JSON"),
        metadata: str = Form("", description="文档级 metadata JSON（如访问范围 {\"access_depts\": [\"D1\"]}），应用于本批所有文件"),
    ) -> BaseResponse:
        """保存文件并按需解析或写入自定义 docs。"""
        try:
            documents = self._parse_documents(docs)
            extra_metadata = self._parse_extra_metadata(metadata)
            payload = [(str(file.filename or ""), file.file.read()) for file in files]
            # save-only：向量化统一走下方 update_files（幂等 upsert）。
            # 勿把 to_vector_store 改为 True——upload_files 的 True 分支是 OA 同步
            # 专用路径（save+ingest 单次），这里若 True 会与紧随的 update_files 对同批文件双写。
            upload_results = self._service.upload_files(
                knowledge_base_name,
                payload,
                override=override,
                to_vector_store=False,
                metadata_by_filename=(
                    {filename: dict(extra_metadata) for filename, _ in payload}
                    if extra_metadata
                    else None
                ),
            )
            filenames = [filename for filename, _ in payload]
            filenames.extend(
                filename for filename in documents if filename not in filenames
            )
            update_results = (
                self._service.update_files(
                    knowledge_base_name,
                    filenames,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                    documents_by_filename=documents or None,
                    metadata_by_filename=(
                        {filename: dict(extra_metadata) for filename in filenames}
                        if extra_metadata
                        else None
                    ),
                )
                if to_vector_store and filenames
                else ()
            )
        except ManagedKnowledgeBaseError as error:
            return self._managed_error(error)
        except LookupError as error:
            return BaseResponse(code=404, msg=str(error))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            return BaseResponse(code=400, msg=f"上传参数错误： {error}")
        except Exception as error:
            return BaseResponse(code=500, msg=f"文件上传失败： {error}")

        failed_files = {
            result.filename: result.message
            for result in upload_results
            if not result.success
        }
        for result in update_results:
            if result.success:
                failed_files.pop(result.filename, None)
            else:
                failed_files[result.filename] = result.message
        return BaseResponse(
            code=200,
            msg="文件上传与向量化完成",
            data={"failed_files": failed_files},
        )

    def update_docs(
        self,
        knowledge_base_name: str = Body(..., examples=["oa_manuals"]),
        file_names: List[str] = Body(..., examples=[["file_name.md", "text.txt"]]),
        chunk_size: int | None = Body(None, description="子块最大长度"),
        chunk_overlap: int | None = Body(None, description="子块重合长度"),
        docs: str = Body("", description="自定义 docs JSON"),
        access_depts: List[str] | None = Body(
            None,
            description="可见部门 ID 列表（空列表=全开放）；提供时更新文件访问范围并重建索引",
        ),
    ) -> BaseResponse:
        """重新解析文件或直接写入自定义 docs；可附带更新访问范围。"""
        try:
            documents = self._parse_documents(docs)
            filenames = list(file_names)
            filenames.extend(
                filename for filename in documents if filename not in filenames
            )
            metadata_by_filename = None
            if access_depts is not None:
                metadata_by_filename = {
                    filename: {"access_depts": list(access_depts)}
                    for filename in filenames
                }
            results = self._service.update_files(
                knowledge_base_name,
                filenames,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                documents_by_filename=documents or None,
                metadata_by_filename=metadata_by_filename,
            )
        except ManagedKnowledgeBaseError as error:
            return self._managed_error(error)
        except LookupError as error:
            return BaseResponse(code=404, msg=str(error))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            return BaseResponse(code=400, msg=f"更新参数错误： {error}")
        except Exception as error:
            return BaseResponse(code=500, msg=f"更新文档失败： {error}")
        return BaseResponse(
            code=200,
            msg="更新文档完成",
            data={
                "failed_files": {
                    result.filename: result.message
                    for result in results
                    if not result.success
                }
            },
        )

    def delete_docs(
        self,
        knowledge_base_name: str = Body(..., examples=["oa_manuals"]),
        file_names: List[str] = Body(..., examples=[["file_name.md", "test.txt"]]),
        delete_content: bool = Body(False),
    ) -> BaseResponse:
        """删除指定文件的索引，并按需删除源文件。"""
        try:
            results = self._service.delete_files(
                knowledge_base_name,
                file_names,
                delete_content=delete_content,
            )
        except ManagedKnowledgeBaseError as error:
            return self._managed_error(error)
        except LookupError as error:
            return BaseResponse(code=404, msg=str(error))
        failed_files = {
            result.filename: result.message
            for result in results
            if not result.success
        }
        return BaseResponse(
            code=200,
            msg="文件删除完成",
            data={"failed_files": failed_files},
        )

    def update_access_depts(
        self,
        knowledge_base_name: str = Body(..., examples=["oa_manuals"]),
        file_names: List[str] = Body(..., examples=[["file_name.md"]]),
        access_depts: List[str] = Body([], description="可见部门 ID 列表（空=全开放）"),
    ) -> BaseResponse:
        """只改写指定文件的 access_depts（纯刷权限，不重建内容）。

        系统托管库（OA/工单同步）也可用：仅改权限元数据，不触发重解析/重 embed。
        """
        try:
            results: dict[str, int] = {}
            for filename in file_names:
                results[filename] = self._service.refresh_doc_access_depts(
                    knowledge_base_name,
                    filename,
                    access_depts=list(access_depts or []),
                )
        except LookupError as error:
            return BaseResponse(code=404, msg=str(error))
        except (TypeError, ValueError) as error:
            return BaseResponse(code=400, msg=f"参数错误： {error}")
        except Exception as error:
            return BaseResponse(code=500, msg=f"更新访问范围失败： {error}")
        return BaseResponse(
            code=200,
            msg="访问范围更新完成",
            data={"updated": results},
        )

    def download_doc(
        self,
        knowledge_base_name: str = Query(..., examples=["oa_manuals"]),
        file_name: str = Query(..., examples=["test.txt"]),
        preview: bool = Query(False),
    ) -> FileResponse | BaseResponse:
        """下载知识库文件，preview=True 时以内联方式返回。"""
        try:
            file_path = Path(self._service.file_path(knowledge_base_name, file_name))
        except LookupError as error:
            return BaseResponse(code=404, msg=str(error))
        except (TypeError, ValueError) as error:
            return BaseResponse(code=400, msg=str(error))
        if not file_path.is_file():
            return BaseResponse(code=404, msg=f"{file_name} 文件不存在")
        return FileResponse(
            path=str(file_path),
            filename=file_path.name,
            media_type="multipart/form-data",
            content_disposition_type="inline" if preview else None,
        )

    def rebuild(
        self,
        request: Request,
        knowledge_base_name: str = Body(..., examples=["oa_manuals"]),
        allow_empty_kb: bool = Body(True),
        vs_type: str = Body("dual", description="仅支持 dual"),
        embed_model: str = Body("", description="为空知识库指定 Embedding 模型"),
        chunk_size: int = Body(
            Settings.kb_settings.CHILD_CHUNK_SIZE,
            description="子块最大长度",
        ),
        chunk_overlap: int = Body(
            Settings.kb_settings.CHILD_OVERLAP_SIZE,
            description="子块重合长度",
        ),
    ) -> EventSourceResponse:
        """重建知识库索引，并将应用进度转换为兼容旧 WebUI 的 SSE。"""
        queue: Queue[tuple[str, object]] = Queue()
        disconnected = Event()
        finished = object()

        class ProgressSink:
            def on_progress(self, event: IngestionProgress) -> None:
                # 客户端断开后仍继续重建，仅停止向前端推送进度。
                if not disconnected.is_set():
                    queue.put(("progress", event))

        def run_rebuild() -> None:
            try:
                self._service.rebuild(
                    knowledge_base_name,
                    allow_empty_kb=allow_empty_kb,
                    vector_store_type=vs_type,
                    embedding_model=embed_model or None,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                    progress=ProgressSink(),
                )
            except ManagedKnowledgeBaseError as error:
                if not disconnected.is_set():
                    queue.put(("error", error))
            except Exception as error:
                if not disconnected.is_set():
                    queue.put(("error", error))
            finally:
                queue.put(("finished", finished))

        heartbeat_seconds = 15.0

        async def output():
            worker = Thread(target=run_rebuild, daemon=True)
            worker.start()
            last_heartbeat = asyncio.get_running_loop().time()
            while True:
                if await request.is_disconnected():
                    disconnected.set()
                    break
                try:
                    kind, value = await asyncio.to_thread(queue.get, True, 0.5)
                except Empty:
                    now = asyncio.get_running_loop().time()
                    if now - last_heartbeat >= heartbeat_seconds:
                        last_heartbeat = now
                        yield ServerSentEvent(comment="ping")
                    if not worker.is_alive() and queue.empty():
                        break
                    continue
                if kind == "progress":
                    yield self._progress_event(value)
                elif kind == "error":
                    yield self._error_event(value)
                    break
                else:
                    break

        return EventSourceResponse(output())

    @staticmethod
    def _progress_event(event: IngestionProgress) -> str:
        result = event.result
        stage = event.stage or "done"
        if stage != "done":
            payload = {
                "code": 200,
                "msg": result.message
                or f"({event.finished} / {event.total}) {stage}: {event.filename}",
                "total": event.total,
                "finished": event.finished,
                "doc": event.filename,
                "stage": stage,
            }
        elif result.success:
            payload = {
                "code": 200,
                "msg": f"({event.finished} / {event.total}): {event.filename}",
                "total": event.total,
                "finished": event.finished,
                "doc": event.filename,
                "stage": "done",
            }
        else:
            payload = {
                "code": 500,
                "msg": result.message,
                "total": event.total,
                "finished": event.finished,
                "doc": event.filename,
                "stage": "done",
            }
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _error_event(error: Exception) -> str:
        if isinstance(error, LookupError):
            code = 404
        elif isinstance(error, ValueError):
            code = 400
        else:
            code = 500
        return json.dumps(
            {"code": code, "msg": str(error)},
            ensure_ascii=False,
        )

    @staticmethod
    def _parse_extra_metadata(raw: str) -> dict:
        """解析上传附带的文档级 metadata JSON；空串返回 {}。非法抛 ValueError。"""
        if not raw:
            return {}
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("metadata 必须是 JSON 对象")
        return payload

    @staticmethod
    def _parse_documents(raw_docs: str) -> dict[str, tuple[IngestDocument, ...]]:
        if not raw_docs:
            return {}
        payload = json.loads(raw_docs)
        if not isinstance(payload, dict):
            raise ValueError("docs 必须是按文件名分组的 JSON 对象")
        documents = {}
        for filename, items in payload.items():
            if not isinstance(items, list):
                raise ValueError(f"文件 {filename} 的 docs 必须是数组")
            parsed = []
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError(f"文件 {filename} 的 docs 项必须是对象")
                content = str(item.get("page_content") or "")
                metadata = item.get("metadata") or {}
                if not isinstance(metadata, dict):
                    raise ValueError(f"文件 {filename} 的 metadata 必须是对象")
                parsed.append(IngestDocument(content, dict(metadata)))
            documents[str(filename)] = tuple(parsed)
        return documents
