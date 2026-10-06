# 企业 AI 应用 V1 架构骨架

## 当前状态

本文件记录 V1 目标架构的骨架与渐进迁移状态。生产实现统一位于
`kylin/`，HTTP、CLI、Jobs、WebUI、数据与模型出口均已归档，
迁移过程不改变已有 API、配置或数据格式。

逻辑根目录 `kylin/` 对应仓库中的实际 Python 包：

```text
kylin/
```

仓库根不再创建第二个同名包，避免 Python 包发现和导入路径冲突。

`tests/test_architecture_skeleton.py` 同时充当“尚未迁移”清单：某个包开始承载
真实实现时，应将它从清单移除，并为新增契约或行为补充专用测试。

## 已迁移切片

`applications/knowledge_qa/` 已承载第一批无基础设施依赖的知识问答规则：

- `context.py`: 可引用上下文装配。
- `evidence.py`: 强弱证据判定。
- `grounding.py`: 无证据拒答与引用校验。
- `prompt.py`: 接地生成、弱证据和闲聊 Prompt。
- `query_plan/`: 查询计划模型、Prompt、历史整理、模型输出解析和降级规则。

首个模型能力闭环已经落地：

```text
QueryPlanner -> ChatModelPort <- KylinChatModelAdapter -> adapters/outbound/model/clients.py
```

`QueryPlanner` 只依赖模型端口；具体适配器负责调用现有模型工厂并将厂商响应
归一为文本。旧 QueryPlan 实现文件已经删除，仓库内调用方统一使用新路径。

知识库摄取纵向切片已开始接管知识库台账和部分文件生命周期：

- `applications/knowledge_ingestion/service.py`: 创建、删除、上传、更新和重建编排。
- `ports/data/ingestion.py` 及相关端口：知识库、文件、文档处理、索引和 Embedding 契约。
- `adapters/outbound/*`: 复用既有 pipeline 的解析、文件、台账、Embedding 和双索引实现。
- `adapters/inbound/http/knowledge_base.py`: 台账、文件详情、上传、更新、删除、下载和重建接口的 HTTP 转换。

SSE 重建接口已通过进度事件桥接切换到入库应用服务；旧知识库管理 API 已删除。

OA 同步纵向切片已完成迁移：

- `applications/knowledge_ingestion/oa_sync.py`: 保留 OA 过滤、增量、重试和 half-written 编排。
- `ports/application/oa_sync.py` 与 `ports/data/oa_sync.py`: 定义 OA 源和状态存储契约。
- `adapters/outbound/oa/`: 承载 OA 配置、认证、HTTP 文档源和状态文件适配。
- `adapters/inbound/cli/oa_sync.py`: 负责 OA CLI 参数和输出。
- 旧 `pipeline/sources/oa/` 已删除；OA 入库通过 `KnowledgeIngestionPort` 完成。

知识缺口工单纵向切片已完成迁移：

- `applications/knowledge_gap/service.py`: 建单、人工答案、反哺和验证状态机。
- `ports/application/knowledge_gap.py` 与 `ports/data/knowledge_gap.py`: 管理、仓储和验证契约。
- `adapters/inbound/http/knowledge_gap.py`: 保持 `/tickets` 旧接口不变。
- `adapters/outbound/mysql/knowledge_gap.py`: 复用既有工单表并隔离 ORM 类型。
- 反哺统一通过 `KnowledgeIngestionPort` 写入，旧根目录 `tickets/` 已删除。

知识问答主链路已迁入 `applications/knowledge_qa/service.py`，由
`bootstrap/knowledge_qa.py` 统一装配 QueryPlanner、KnowledgeRetriever、会话记忆、
知识检索和 Chat 模型端口。旧 HTTP/SSE 参数与事件转换已迁入
`adapters/inbound/http/`；会话记忆和检索底层仍由 outbound adapter 复用现有实现。

HTTP 兼容入口的模型目录与客户端通过 `ports/model/gateway.py` 注入：
`bootstrap/http.py` 将具体模型工厂装入 `app.state`，HTTP 适配器只消费端口，
不再直接导入模型 SDK 工厂。问答服务和 HTTP 协议适配器均按应用生命周期复用，
不会在每个请求中重新构造检索、记忆和模型对象。

身份与最小鉴权已落地：

- `applications/identity/` + `ports/data/oa_identity.py`：OA 工号校验与会话身份解析。
- `adapters/inbound/http/identity.py`、WebUI 身份模式：HTTP / 固定开发身份。
- `platform/security/middleware.py`：可选 API Token（`X-API-Key` / Bearer）；默认关闭，
  `KYLIN_DEV=1` 时放行。尚非完整 AuthZ。

长期记忆纵向切片已接入问答链路：

- `applications/long_term_memory/`：抽取与写入编排。
- `ports/data/long_term_memory.py` + MySQL / Milvus outbound 适配。
- 配置模板：`deploy/config.example/memory_settings.yaml`。

可观测 Trace 端口已落地（默认可关闭）：

- `ports/observability/trace.py` + `adapters/outbound/observability/`（Langfuse / NoOp）。
- Chat 模型适配器可挂 span；配置模板：`deploy/config.example/langfuse_settings.yaml`。

Stage D1 已完成入口面迁移：FastAPI 路由、Streamlit WebUI、评估任务、知识库
维护命令和多进程启动均位于 `adapters/inbound` 或 `bootstrap`。根目录 `webui/`、
`pipeline/api/`、`pipeline/generate/`、`pipeline/eval/` 和旧 API server 已删除。

Stage D2 已完成数据面归档：知识库解析、切分、入库、双索引、检索实现位于
`adapters/outbound/knowledge_base/`；SQLAlchemy 引擎、模型、会话和仓储位于
`adapters/outbound/mysql/database/`。根目录 `pipeline/`、`dblib/` 和旧
`kylin/server/knowledge_base/` 已删除。

Stage D3 已完成平台基础设施归位：配置与配置文件模板位于
`platform/config/`，日志位于 `platform/observability/`，网络、并发、Document、
Message 和 HTTP 共享响应模型位于 `platform/runtime/`。生产代码已切换到新路径，
根级共享模块已删除；`kylin/cli.py` 作为公开 CLI 包入口保留。

旧目录的源码和缓存已从工作树删除。完整运行回归仍需在依赖和外部服务可用后执行。

## 三层逻辑架构

### CODE

代码层负责请求入口、身份与权限上下文、应用分发和具体业务编排。
业务代码通过公共端口使用模型、数据和可观测能力，不直接依赖具体厂商 SDK。

### MODEL

模型层提供 Chat、Embedding 和 Rerank 等已知模型能力。模型实现不理解
知识问答、知识入库或工单等具体业务。

### DATA

数据层提供检索、仓储和文件访问能力。数据实现不决定是否检索、是否拒答
或是否创建工单等业务规则。

MODEL 与 DATA 是相互独立的基础能力，两者都不反向依赖 CODE 中的具体应用。

## 包职责

```text
kylin/
├── platform/       平台运行能力
├── applications/   相互隔离的业务应用
├── ports/          上层依赖的能力契约
├── adapters/       进入系统或访问外部能力的适配器
└── bootstrap/      实例化、注册、注入与启动的组合根
```

- `platform/`: Application Runtime、安全中间件、日志/可观测骨架和平台配置；
  `platform/context/` 仍为 RequestContext 占位（尚未实现）。
- `applications/`: Knowledge QA、Knowledge Ingestion、Knowledge Gap、Identity、
  Long-Term Memory。
- `ports/`: Application、Model、Data、Observability 契约。
- `adapters/inbound/`: HTTP、CLI、后台任务和 WebUI 等入口适配。
- `adapters/outbound/`: 模型、MySQL、OA、文件存储（`file_storage`）、长期记忆、
  可观测（Langfuse/NoOp）与知识库双索引（`knowledge_base/`，内含 Milvus + ES，
  不再单独拆 `milvus/` / `elasticsearch/` / `files/` 包）。
- `bootstrap/`: 唯一允许同时装配抽象契约与具体实现的位置。

## 依赖方向

允许的目标方向：

```text
applications -> ports
platform     -> ports
adapters     -> ports
bootstrap    -> platform + applications + ports + adapters
```

必须遵守：

- `applications` 不依赖 `adapters`、`bootstrap` 或具体基础设施 SDK。
- `platform` 不写死任何具体 Application，也不依赖 `adapters`。
- `ports` 不依赖具体 Application、Adapter、启动代码或厂商 SDK。
- `adapters` 实现端口，但不把厂商类型泄漏进端口。
- 旧根目录实现已删除；禁止为了填满骨架而复制实现。

## 当前边界（原「本阶段非目标」已部分过时）

已落地、不再当作非目标：

- 最小 AuthN：可选 API Token 中间件 + OA Identity 校验（非完整 RBAC/AuthZ）。
- 可观测：`TracePort` + Langfuse/NoOp；日志仍在 `platform/observability/`。
- 新纵向能力：Identity、Long-Term Memory；配套可增配置模板与表结构，但须保持
  既有对外 API / 事件语义兼容。

仍不在本阶段目标内：

- 不实现 Registry、Dispatcher，也不实现统一 `RequestContext`（`platform/context` 仍为占位）。
- 不实现完整 AuthZ / 多租户权限模型。
- 不为尚未立项的 Application 预埋假实现。
- 不引入微服务拆分、消息队列、通用工作流引擎、开放 Agent/MCP 运行时。
- 不破坏已有对外 HTTP/SSE 契约与既有数据格式；新增配置/表须可默认关闭或向后兼容。

能力覆盖备注：

- Chat Model Port：QueryPlanner 与问答生成的同步 / 异步 / 流式文本。
- Embedding / Rerank：仍由知识库出站经 `adapters/outbound/model/clients.py` 访问。
- HTTP 兼容模型目录：`ports/model/gateway.py`。
- Data Port：知识检索、会话记忆、长期记忆、知识入库所需的仓储 / 文件 / 索引端口。

后续每次只迁移一个明确的纵向业务链路，并在迁移前单独确认契约。
