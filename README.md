# Kylin Digital Assistant

麒麟信安数字人系统 — 企业内网 **系统咨询数字助手**。

文档入库后按知识库检索，再交给大模型生成带依据的回答。

对外问答接口：

```http
POST /chat/kb_chat
```

请求体核心字段：`query`、`kb_name`（OA 生产库常用 `oa_kb`，WebUI 默认 `oa_manuals`）、`stream`、`score_threshold`、`top_k`。

## 架构一览

```
API :7861（FastAPI） + WebUI :8501（Streamlit）
        ↓
query_plan → 检索服务(service) → dual 检索（Milvus + ES）→ 拒答门 / grounded 生成 → LLM
```

- 问答主链路由 `kylin.applications.knowledge_qa` 编排（查询计划 → 检索 → 接地生成），API 层只做薄适配
- 知识库数据与处理实现统一位于 `kylin/adapters/outbound/`
- 平台基础设施统一位于 `kylin/platform/`：配置、日志、网络、并发和共享值对象；模型客户端位于 `kylin/adapters/outbound/model/`
- **无 langchain 依赖**（P7 移除）：Document dataclass 化、LLM 客户端 openai SDK 直连、流式用原生 async generator
- 工单出口经 `kylin.ports.application.knowledge_gap` 端口注入（组合根 `bootstrap.http.create_app`）

运行时依赖：

| 组件 | 说明 |
|---|---|
| MySQL | 台账（知识库 / 文件 / 工单），库名默认 `kylin_rag` |
| Milvus | 子块向量检索 |
| Elasticsearch | 子块 BM25 + 父块全文 |
| LLM / Embedding / Rerank | **外部服务**，在 `model_settings.yaml` 配置地址 |

配置与数据目录由环境变量 `KYLIN_ROOT` 指定（Docker 默认 `/data/kylin`）。代码在本仓库，运行时 yaml / 密钥不进 git。

## 日常开发（推荐：本机 app + Docker 中间件）

改代码**不需要**重建 app 容器。中间件用 Docker，应用在宿主机 venv 里跑，保存 `.py` 后 API / WebUI 自动重载。

### 1. 起中间件（不含 app）

```bash
cp .env.example .env   # 首次；填写 MYSQL_PASSWORD

docker compose up -d mysql elasticsearch milvus etcd minio
```

### 2. 本机 Python 环境

Python `>=3.10,<3.13`（3.12 已实测），建议独立 venv：

```bash
pip install -r requirements.txt
# torch CPU 示例：
#   pip install torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cpu
```

### 3. 初始化与配置

```bash
# Windows
set KYLIN_ROOT=%CD%\.data\kylin
set PYTHONPATH=%CD%
set KYLIN_DEV=1

# Linux / macOS
# export KYLIN_ROOT="$PWD/.data/kylin"
# export PYTHONPATH="$PWD"
# export KYLIN_DEV=1

python -m kylin.cli init
```

编辑 `KYLIN_ROOT` 下 yaml，**本机开发**时把 `basic_settings.yaml` 里的服务地址改为宿主机可达：

| 配置项 | Docker 内（app 容器） | 本机 app |
|---|---|---|
| `SQLALCHEMY_DATABASE_URI` | `@mysql:3306` | `@127.0.0.1:3306` |
| Milvus / ES | 容器网络服务名 | `127.0.0.1`（compose 已映射端口） |

模型地址在 `model_settings.yaml` 按内网填写。

### 4. 启动

```bash
python -m kylin.cli start -a
```

- WebUI：http://127.0.0.1:8501
- API 文档：http://127.0.0.1:7861/docs

`KYLIN_DEV=1` 时保存 `kylin/` 下 Python 文件即可生效，无需重启进程。

改 `*_settings.yaml`：保存后下次请求自动重读（Settings 按 mtime 刷新），一般无需重启。

---

## Docker 全栈开发（推荐本地默认）

复制 `.env.example` 为 `.env`，填写本机密码。日常请用统一入口（**不要**默认 `--no-cache`）：

```bash
make up          # 或 Windows: .\scripts\dev.ps1 up
make restart     # 秒级重启 app
```

| 变更 | 操作 |
|------|------|
| `kylin/**/*.py` | 保存即可热重载，无需 restart / rebuild |
| `*_settings.yaml` | 一般无需重启（mtime 自动重读） |
| 进程挂了 / 环境变量 | `make restart` |
| `requirements.txt` / `Dockerfile` | `make up`（允许 `--build`） |

- 挂载源码目录，容器内 `KYLIN_DEV=1`
- 身份默认 `fixed_dev`（无工号登录，固定 userid 供长期记忆）；上线改 `oa_settings.yaml` 的 `identity_mode: oa_login`

---

## 生产 / 协作部署（Docker）

前置：Docker Compose、能访问内网 LLM / Embedding / Rerank。

```bash
git clone https://github.com/weikai-824/kylin-digital-assistant.git
cd kylin-digital-assistant

cp .env.example .env
# 编辑 .env：填写 MYSQL_PASSWORD；模型地址在数据卷 model_settings.yaml 中改

docker compose up -d --build
```

首次启动会把 `deploy/config.example/*.yaml` 写入数据卷，并尝试建表。

- WebUI：http://127.0.0.1:8501
- API 文档：http://127.0.0.1:7861/docs

改配置：编辑数据卷内 `*_settings.yaml` 后 `docker compose restart app`。

改代码：需 `docker compose up -d --build`（或使用上文「Docker 全栈开发」overlay）。

OA 同步密钥写在 `KYLIN_ROOT/oa_settings.yaml`（可用 CLI 生成模板），**勿提交**。

## 仓库结构

```
kylin-system-digital-human/
├── kylin/                    应用、端口、适配器、平台配置和启动
│   ├── platform/             配置、日志、网络、并发和共享值对象
│   └── adapters/outbound/
│       ├── knowledge_base/   解析、清洗、切分、入库、双索引和检索
│       └── mysql/            SQLAlchemy 引擎、模型、会话和仓储
├── tests/                    pytest 特征化测试（重构安全网，含依赖方向守卫）
├── data/eval/                评估金标（检索 + 回答）
├── deploy/                   Docker 入口与配置示例
├── docker-compose.yml
├── docker-compose.dev.yml    开发 overlay（源码挂载 + 热重载）
├── Makefile / scripts/dev.ps1  开发启动入口（优先用这个）
├── Dockerfile
└── .env.example
```

| 路径 | 干什么 |
|---|---|
| `kylin/platform/config` | YAML 配置加载与 Settings |
| `kylin/platform/observability` | 日志构造与日志配置 |
| `kylin/platform/runtime` | HTTP 客户端、并发工具和共享值对象 |
| `kylin/adapters/outbound/oa` | OA 配置、认证、文档源和状态存储 |
| `kylin/adapters/outbound/knowledge_base/parse` / `clean` / `split` | 解析 → 清洗 → 切分 |
| `kylin/adapters/outbound/knowledge_base/ingest` / `index` | 入库、Milvus / ES |
| `kylin.applications.knowledge_qa` | 查询理解、检索编排、接地生成 |
| `kylin/adapters/outbound/mysql` | MySQL 台账模型、会话和仓储 |
| `kylin/adapters/inbound/http` | FastAPI、SSE 与 OpenAI 兼容入口 |
| `kylin/applications/knowledge_gap` | 工单创建、人工反哺和验证闭环 |
| `kylin/adapters/inbound/http/knowledge_gap.py` | 工单 HTTP 入口 |
| `kylin/adapters/inbound/webui` | Streamlit 前端入口 |
| `kylin/cli.py` / `kylin/bootstrap/startup.py` | `start -a` 拉起 API + WebUI |

## 文档导航

| 文档 | 面向谁 | 内容 |
|---|---|---|
| `README.md`（本文件） | 所有人 | 部署 / 启动 / 目录结构 |
| `ARCHITECTURE_V1.md` | 架构设计者 / 开发者 | V1 目标骨架、模块职责、依赖方向与阶段边界 |
| `data/eval/README.md` | 做评估的人 | eval 协议、指标、跑法 |

## 模型配置

在 `KYLIN_ROOT/model_settings.yaml` 中配置，地址按内网填写。

| 用途 | 配置项 | 说明 |
|---|---|---|
| LLM | `DEFAULT_LLM_MODEL` + `MODEL_PLATFORMS` | OpenAI 兼容接口 |
| Embedding | `DEFAULT_EMBEDDING_MODEL` | 如 Ollama / 内网向量服务 |
| Rerank | `DEFAULT_RERANK_MODEL` | OpenAI 兼容 rerank |

## Public repository notes

Environment-specific OA evaluation data, deployment files, and credentials are not included. Configure your own service endpoints and secrets before connecting to external systems.
