from typing import Dict, List

from kylin.adapters.outbound.mysql.database.models.knowledge_base_model import KnowledgeBaseModel
from kylin.adapters.outbound.mysql.database.models.knowledge_file_model import (
    FileDocModel,
    KnowledgeFileModel,
)
from kylin.adapters.outbound.mysql.database.protocols import KnowledgeFileLike
from kylin.adapters.outbound.mysql.database.session import with_session


@with_session
def list_docs_from_db(
    session,
    kb_name: str,
    file_name: str = None,
    metadata: Dict = {},
) -> List[Dict]:
    """
    列出某知识库某文件对应的所有Document。
    返回形式：[{"id": str, "metadata": dict}, ...]
    """
    docs = session.query(FileDocModel).filter(FileDocModel.kb_name.ilike(kb_name))
    if file_name:
        docs = docs.filter(FileDocModel.file_name.ilike(file_name))
    for k, v in metadata.items():
        docs = docs.filter(FileDocModel.meta_data[k].as_string() == str(v))

    return [{"id": x.doc_id, "metadata": x.meta_data} for x in docs.all()]


@with_session
def delete_docs_from_db(
    session,
    kb_name: str,
    file_name: str = None,
) -> List[Dict]:
    """
    删除某知识库某文件对应的所有Document，并返回被删除的Document。
    返回形式：[{"id": str, "metadata": dict}, ...]
    """
    docs = list_docs_from_db(kb_name=kb_name, file_name=file_name)
    query = session.query(FileDocModel).filter(FileDocModel.kb_name.ilike(kb_name))
    if file_name:
        query = query.filter(FileDocModel.file_name.ilike(file_name))
    query.delete(synchronize_session=False)
    session.commit()
    return docs


@with_session
def add_docs_to_db(session, kb_name: str, file_name: str, doc_infos: List[Dict]):
    """
    将某知识库某文件对应的所有Document信息添加到数据库。
    doc_infos形式：[{"id": str, "metadata": dict}, ...]
    """
    # ! 这里会出现doc_infos为None的情况，需要进一步排查
    if doc_infos is None:
        print(
            "输入的server.db.repository.knowledge_file_repository.add_docs_to_db的doc_infos参数为None"
        )
        return False
    for d in doc_infos:
        obj = FileDocModel(
            kb_name=kb_name,
            file_name=file_name,
            doc_id=d["id"],
            meta_data=d["metadata"],
        )
        session.add(obj)
    return True


@with_session
def count_files_from_db(session, kb_name: str) -> int:
    return (
        session.query(KnowledgeFileModel)
        .filter(KnowledgeFileModel.kb_name.ilike(kb_name))
        .count()
    )


@with_session
def list_files_from_db(session, kb_name):
    files = (
        session.query(KnowledgeFileModel)
        .filter(KnowledgeFileModel.kb_name.ilike(kb_name))
        .all()
    )
    docs = [f.file_name for f in files]
    return docs


@with_session
def add_file_to_db(
    session,
    kb_file: KnowledgeFileLike,
    docs_count: int = 0,
    doc_infos: List[Dict] = [],  # 形式：[{"id": str, "metadata": dict}, ...]
):
    kb = session.query(KnowledgeBaseModel).filter_by(kb_name=kb_file.kb_name).first()
    if kb:
        existing_file: KnowledgeFileModel = (
            session.query(KnowledgeFileModel)
            .filter(
                KnowledgeFileModel.kb_name.ilike(kb_file.kb_name),
                KnowledgeFileModel.file_name.ilike(kb_file.filename),
            )
            .first()
        )
        mtime = kb_file.get_mtime()
        size = kb_file.get_size()

        if existing_file:
            existing_file.file_mtime = mtime
            existing_file.file_size = size
            existing_file.docs_count = docs_count
        else:
            new_file = KnowledgeFileModel(
                file_name=kb_file.filename,
                file_ext=kb_file.ext,
                kb_name=kb_file.kb_name,
                file_mtime=mtime,
                file_size=size,
                docs_count=docs_count,
            )
            kb.file_count += 1
            session.add(new_file)
        add_docs_to_db(
            kb_name=kb_file.kb_name, file_name=kb_file.filename, doc_infos=doc_infos
        )
    return True


@with_session
def delete_file_from_db(session, kb_file: KnowledgeFileLike):
    existing_file = (
        session.query(KnowledgeFileModel)
        .filter(
            KnowledgeFileModel.file_name.ilike(kb_file.filename),
            KnowledgeFileModel.kb_name.ilike(kb_file.kb_name),
        )
        .first()
    )
    if existing_file:
        session.delete(existing_file)
        delete_docs_from_db(kb_name=kb_file.kb_name, file_name=kb_file.filename)
        session.commit()

        kb = (
            session.query(KnowledgeBaseModel)
            .filter(KnowledgeBaseModel.kb_name.ilike(kb_file.kb_name))
            .first()
        )
        if kb:
            kb.file_count -= 1
            session.commit()
    return True


@with_session
def delete_files_from_db(session, knowledge_base_name: str):
    session.query(KnowledgeFileModel).filter(
        KnowledgeFileModel.kb_name.ilike(knowledge_base_name)
    ).delete(synchronize_session=False)
    session.query(FileDocModel).filter(
        FileDocModel.kb_name.ilike(knowledge_base_name)
    ).delete(synchronize_session=False)
    kb = (
        session.query(KnowledgeBaseModel)
        .filter(KnowledgeBaseModel.kb_name.ilike(knowledge_base_name))
        .first()
    )
    if kb:
        kb.file_count = 0

    session.commit()
    return True


@with_session
def file_exists_in_db(session, kb_file: KnowledgeFileLike):
    existing_file = (
        session.query(KnowledgeFileModel)
        .filter(
            KnowledgeFileModel.file_name.ilike(kb_file.filename),
            KnowledgeFileModel.kb_name.ilike(kb_file.kb_name),
        )
        .first()
    )
    return True if existing_file else False


@with_session
def get_file_detail(session, kb_name: str, filename: str) -> dict:
    file: KnowledgeFileModel = (
        session.query(KnowledgeFileModel)
        .filter(
            KnowledgeFileModel.file_name.ilike(filename),
            KnowledgeFileModel.kb_name.ilike(kb_name),
        )
        .first()
    )
    if file:
        return {
            "kb_name": file.kb_name,
            "file_name": file.file_name,
            "file_ext": file.file_ext,
            "create_time": file.create_time,
            "file_mtime": file.file_mtime,
            "file_size": file.file_size,
            "docs_count": file.docs_count,
        }
    else:
        return {}


@with_session
def get_file_department_ids(session, kb_name: str) -> Dict[str, str]:
    """返回 {file_name: department_id}：每文件取第一个非空 department_id（子块 metadata 聚合）。

    未打标文件（所有子块 department_id 为空）不在结果里。
    """
    rows = (
        session.query(FileDocModel.file_name, FileDocModel.meta_data)
        .filter(FileDocModel.kb_name.ilike(kb_name))
        .all()
    )
    result: Dict[str, str] = {}
    for file_name, meta_data in rows:
        if file_name in result:
            continue
        if isinstance(meta_data, dict):
            dept = str(meta_data.get("department_id") or "").strip()
            if dept:
                result[str(file_name)] = dept
    return result


@with_session
def get_file_access_depts(session, kb_name: str) -> Dict[str, list]:
    """返回 {file_name: [dept_id, ...]}：每文件取第一个非空 access_depts（子块 metadata 聚合）。

    access_depts 可能存为 list 或 ";D1;D2;" 字符串；空/缺失 = 全开放（不在结果里或空列表）。
    """
    rows = (
        session.query(FileDocModel.file_name, FileDocModel.meta_data)
        .filter(FileDocModel.kb_name.ilike(kb_name))
        .all()
    )
    result: Dict[str, list] = {}
    for file_name, meta_data in rows:
        if file_name in result:
            continue
        if isinstance(meta_data, dict) and meta_data.get("access_depts"):
            raw = meta_data["access_depts"]
            if isinstance(raw, (list, tuple)):
                items = [str(v).strip() for v in raw if str(v).strip()]
            else:
                items = [
                    v.strip()
                    for v in str(raw).replace(";", ",").split(",")
                    if v.strip()
                ]
            if items:
                result[str(file_name)] = items
    return result


@with_session
def update_doc_meta_access_depts(
    session, kb_name: str, filename: str, access_depts: list
) -> int:
    """按文件批量改写台账中全部子块的 access_depts（'只刷权限'）。

    仅更新 MySQL file_doc 台账的 meta_data.access_depts；Milvus/ES 索引
    由索引层独立更新。access_depts 空列表 = 全开放。返回更新的子块记录数。
    """
    access_depts = [
        str(v).strip() for v in (access_depts or []) if str(v).strip()
    ]
    rows = (
        session.query(FileDocModel)
        .filter(
            FileDocModel.kb_name.ilike(kb_name),
            FileDocModel.file_name.ilike(filename),
        )
        .all()
    )
    for row in rows:
        meta = dict(row.meta_data or {})
        meta["access_depts"] = access_depts
        row.meta_data = meta
    session.commit()
    return len(rows)


@with_session
def get_file_permission_metadata(session, kb_name: str, filename: str) -> dict:
    """返回某文件的权限 metadata（从 file_doc 首个含权限的子块读取）。

    返回可能含 department_id / access_depts（list 形式）；无记录返回 {}。
    用于重建索引时保留已设置的访问范围。
    """
    row = (
        session.query(FileDocModel.meta_data)
        .filter(
            FileDocModel.kb_name.ilike(kb_name),
            FileDocModel.file_name.ilike(filename),
        )
        .first()
    )
    if row is None or not isinstance(row[0], dict):
        return {}
    meta = row[0]
    out: dict = {}
    if meta.get("department_id"):
        out["department_id"] = str(meta["department_id"])
    if meta.get("access_depts"):
        raw = meta["access_depts"]
        if isinstance(raw, (list, tuple)):
            out["access_depts"] = [str(v).strip() for v in raw if str(v).strip()]
        else:
            out["access_depts"] = [
                v.strip()
                for v in str(raw).replace(";", ",").split(",")
                if v.strip()
            ]
    return out
