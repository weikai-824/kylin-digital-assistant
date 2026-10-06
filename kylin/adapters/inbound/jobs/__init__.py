"""离线任务入口适配器。

包含检索/答案评估（``jobs.eval``）与知识库维护命令（``jobs.knowledge_base``）。
由 CLI 显式调用，非常驻后台守护进程。
"""
"""后台任务与定时同步入口适配器。

OA 周期同步实现见 applications.knowledge_ingestion.oa_schedule，
CLI：kylin oa-sync --daemon。
"""
