"""补全接口：/other/completion（LLM 直连，无知识库）。"""

import asyncio
from typing import AsyncIterable, Optional

from fastapi import Body
from sse_starlette.sse import EventSourceResponse

from kylin.adapters.inbound.http.server_state import get_prompt_template
from kylin.ports.model.gateway import ModelGatewayPort


def create_completion_handler(model_gateway: ModelGatewayPort):
    """创建注入模型网关的补全接口处理器。"""

    async def completion(
        query: str = Body(..., description="用户输入", examples=["恼羞成怒"]),
        stream: bool = Body(False, description="流式输出"),
        echo: bool = Body(False, description="除了输出之外，还回显输入"),
        model_name: str = Body(None, description="LLM 模型名称。"),
        temperature: float = Body(0.01, description="LLM 采样温度", ge=0.0, le=1.0),
        max_tokens: Optional[int] = Body(
            1024, description="限制LLM生成Token数量，默认None代表模型最大值"
        ),
        prompt_name: str = Body(
            "default", description="使用的prompt模板名称(在configs/prompt_config.py中配置)"
        ),
    ):
        async def completion_iterator(
            query: str,
            model_name: str = None,
            prompt_name: str = prompt_name,
            echo: bool = echo,
        ) -> AsyncIterable[str]:
            try:
                effective_max_tokens = max_tokens
                if isinstance(effective_max_tokens, int) and effective_max_tokens <= 0:
                    effective_max_tokens = None

                model = model_gateway.openai_model(
                    model_name=model_name,
                    temperature=temperature,
                    max_tokens=effective_max_tokens,
                    local_wrap=True,
                )

                prompt_template = get_prompt_template("llm_model", prompt_name)
                content = prompt_template.format(input=query)
                if echo:
                    content = f"{query}\n\n{content}"
                messages = [{"role": "user", "content": content}]

                if stream:
                    async for token in model.astream(messages):
                        yield token
                else:
                    resp = await model.ainvoke(messages)
                    yield resp.content

            except asyncio.exceptions.CancelledError:
                return

        return EventSourceResponse(
            completion_iterator(
                query=query, model_name=model_name, prompt_name=prompt_name
            ),
        )

    return completion
