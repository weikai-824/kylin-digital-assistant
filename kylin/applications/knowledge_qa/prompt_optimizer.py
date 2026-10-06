"""发送前的输入补全旁路：仅在原句「不清晰但语义可自足」时做克制补全.

与主链路（``QueryPlanner`` / ``KnowledgeQaService``）完全解耦：
- 触发时机在用户点击「发送」之前，由独立路由 ``POST /chat/optimize_prompt`` 调用；
- 不检索、不写会话、不进长期记忆；
- 职责边界（v2 教训）：本模块**不做指代消歧**。涉及代词/上文指代的问句
  一律判定 ``no_rewrite`` 原样返回，把消歧交给带历史的主链路规划器——
  无上下文的旁路改写代词（“我刚才问了啥”→“你刚才问的问题是什么”）
  是上次的严重事故，必须杜绝；
- 原句已经完整清晰也判定 ``no_rewrite``，不为“优化”而强行改写（不引入噪声）；
- 任何失败（模型不可用 / 空输出 / 异常）回退原文并注明 notes。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kylin.ports.model.chat import ChatMessage, ChatModelPort

logger = logging.getLogger(__name__)

# 改写输出上限：目标篇幅通常为原句 1.0~1.5 倍，256 token 远够，只做护栏。
DEFAULT_MAX_TOKENS = 256

# 模型判定“无需改写 / 需上下文消歧”时输出的唯一标记。
NO_REWRITE_MARKER = "__NO_REWRITE__"


@dataclass(frozen=True, slots=True)
class OptimizeResult:
    """一次改写的结果。

    kind 语义：
    - ``rewrite``   正常改写，text 为改写后文本；
    - ``no_rewrite`` 原句已清晰或需上下文指代，text 恒等于原句；
    - ``fallback``  模型不可用/空输出/异常，text 恒等于原句（notes 说明原因）。
    """

    text: str
    kind: str = "rewrite"
    notes: str = ""


# Prompt 换行是模型输入的一部分，有意保留长行。
# ruff: noqa: E501
PROMPT_SYSTEM = """你是企业知识库问答的「输入补全助手」。用户会给出一个问句，你按规则判断它是否需要补全，需要时做最小幅度的修补。

分两步处理：

【第 1 步：判断是否需要改写】
- A. 原句需要对话上下文才能理解（含代词或指代词：它/这个/那个/刚才/他/她/你们/这个系统/上一步 等，或明显指向上一轮对话）→ 只输出 __NO_REWRITE__，不做任何改写。上下文消歧不属于你的职责。
- B. 原句已经完整清晰（动作、对象、意图齐备，无语病、无口语简写）→ 只输出 __NO_REWRITE__，不做任何改写。
- C. 其余情况（有错别字/口语简写/省略成分，但无需上下文就能看懂）→ 进入第 2 步做克制补全。

【第 2 步：克制补全，任何一条违反都是错误】
1. 禁止改变主语、代词、视角。原句里的指代在 A 已拦截，因此进入此步的句子不允许再出现代词替换；也不要引入新的主语。
2. 禁止删减原句中的关键动作、对象、限定词（“使用”“报销单”“提交”“审批”等必须保留）。
3. 禁止引入原文中不存在的别名、概念、对象、系统名。不得自行联想“微信=微信企业号”“OA=麒麟信安”“费控=易快报”之类的映射。
4. 只允许三类修补：① 补全省略的主语/对象（只能补到原句能推得出的程度，推不出就保持原样）；② 纠错别字与明显语病；③ 把口语简写展开为规范说法（“咋弄”→“怎么操作”），但展开只限常识缩写（如“企微”→“企业微信”），不得把展开当成联想其他系统的机会。
5. 篇幅不得比原句明显缩短，也不得大幅扩写；以原句长度的 1.0~1.5 倍为佳，上限 80 字。保持原句口语/正式的原始风格。
6. 只输出改写后的问句本身。禁止解释、思考过程、Markdown、引号、括号备注、前后缀。

示例：
输入：如何使用微信快捷审批报销单
输出：__NO_REWRITE__

输入：审批流程怎么发起
输出：__NO_REWRITE__

输入：我刚才问了啥
输出：__NO_REWRITE__

输入：怎么登录这个
输出：__NO_REWRITE__

输入：出差咋整
输出：出差申请怎么提交

输入：企微考勤咋打卡
输出：企业微信考勤怎么打卡

输入：审批流咋走
输出：审批流程怎么走
"""

PROMPT_USER_TEMPLATE = "用户问句：\n{query}"


class PromptOptimizer:
    """通过 Chat 模型端口执行一次输入补全；需上下文指代或已清晰时不改写。"""

    def __init__(self, chat_model: ChatModelPort):
        self._chat_model = chat_model

    def optimize(
        self,
        query: str,
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> OptimizeResult:
        """判断并（仅在可自足时）补全单个问句；任何失败回退原文."""
        raw = (query or "").strip()
        if not raw:
            return OptimizeResult(text=raw, kind="fallback", notes="empty_query")

        try:
            messages: list[ChatMessage] = [
                {"role": "system", "content": PROMPT_SYSTEM},
                {
                    "role": "user",
                    "content": PROMPT_USER_TEMPLATE.format(query=raw),
                },
            ]
            content = self._chat_model.complete(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            optimized = (content or "").strip()
            if not optimized:
                logger.warning("prompt_optimizer 返回空输出，回退原文 query=%r", raw[:200])
                return OptimizeResult(text=raw, kind="fallback", notes="empty_output")
            # 模型可能附带反引号/引号包住标记，宽松匹配
            if optimized.replace("`", "").replace("'", "").replace('"', "").strip() == NO_REWRITE_MARKER or optimized.startswith(NO_REWRITE_MARKER):
                return OptimizeResult(text=raw, kind="no_rewrite", notes="no_rewrite")
            if optimized == raw:
                return OptimizeResult(text=raw, kind="no_rewrite", notes="same_as_input")
            return OptimizeResult(text=optimized, kind="rewrite", notes="")
        except Exception as error:  # noqa: BLE001
            logger.warning("prompt_optimizer 调用失败，回退原文: %s", error)
            return OptimizeResult(text=raw, kind="fallback", notes=f"exception:{type(error).__name__}")
