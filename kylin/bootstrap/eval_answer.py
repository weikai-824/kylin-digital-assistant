"""回答评估组合入口：注入禁用工单副作用的问答服务。"""

from kylin.adapters.inbound.http.knowledge_qa_protocol import (
    KnowledgeQaHttpProtocolAdapter,
)
from kylin.adapters.inbound.jobs.eval.answer_runner import main
from kylin.bootstrap.knowledge_qa import build_knowledge_qa_service
from kylin.adapters.outbound.model.gateway import KylinModelGatewayAdapter


def build_eval_chat_adapter() -> KnowledgeQaHttpProtocolAdapter:
    """构造不会写入知识缺口工单的评估问答适配器。"""
    return KnowledgeQaHttpProtocolAdapter(
        build_knowledge_qa_service(knowledge_gap=None),
        KylinModelGatewayAdapter(),
    )


if __name__ == "__main__":
    raise SystemExit(main(chat=build_eval_chat_adapter().chat))
