"""配置加载：把 KYLIN_ROOT 下的 *_settings.yaml 映射为 Settings 对象。"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import typing as t

from kylin import __version__
from kylin.platform.config.pydantic_settings_file import *


# kylin 数据目录，必须通过环境变量设置。如未设置则自动使用当前目录。
KYLIN_ROOT = Path(os.environ.get("KYLIN_ROOT", ".")).resolve()

XF_MODELS_TYPES = {
    "text2image": {"model_family": ["stable_diffusion"]},
    "image2image": {"model_family": ["stable_diffusion"]},
    "speech2text": {"model_family": ["whisper"]},
    "text2speech": {"model_family": ["ChatTTS"]},
}


class BasicSettings(BaseFileSettings):
    """
    服务器基本配置信息
    除 log_verbose/HTTPX_DEFAULT_TIMEOUT 修改后即时生效
    其它配置项修改后都需要重启服务器才能生效，服务运行期间请勿修改
    """

    model_config = SettingsConfigDict(yaml_file=KYLIN_ROOT / "basic_settings.yaml")

    version: str = __version__
    """生成该配置模板的项目代码版本，如这里的值与程序实际版本不一致，建议重建配置文件模板"""

    log_verbose: bool = False
    """是否开启日志详细信息"""

    HTTPX_DEFAULT_TIMEOUT: float = 300
    """httpx 请求默认超时时间（秒）。如果加载模型或对话较慢，出现超时错误，可以适当加大该值。"""

    HTTPX_KB_INGEST_TIMEOUT: float = 3600
    """知识库上传/更新向量化专用超时（秒）。解析+OCR+embed 常超过默认 300s。"""

    # @computed_field
    @cached_property
    def PACKAGE_ROOT(self) -> Path:
        """代码根目录"""
        # settings.py 位于 kylin/platform/config，保持迁移前的 kylin 包根语义。
        return Path(__file__).resolve().parents[2]

    # @computed_field
    @cached_property
    def DATA_PATH(self) -> Path:
        """用户数据根目录"""
        p = KYLIN_ROOT / "data"
        return p

    # @computed_field
    @cached_property
    def IMG_DIR(self) -> Path:
        """项目相关图片目录"""
        p = self.PACKAGE_ROOT / "img"
        return p

    # @computed_field
    @cached_property
    def LOG_PATH(self) -> Path:
        """日志存储路径"""
        p = self.DATA_PATH / "logs"
        return p

    # @computed_field
    @cached_property
    def MEDIA_PATH(self) -> Path:
        """模型生成内容（图片、视频、音频等）保存位置"""
        p = self.DATA_PATH / "media"
        return p

    # @computed_field
    @cached_property
    def BASE_TEMP_DIR(self) -> Path:
        """临时文件目录，主要用于文件对话"""
        p = self.DATA_PATH / "temp"
        (p / "openai_files").mkdir(parents=True, exist_ok=True)
        return p

    KB_ROOT_PATH: str = str(KYLIN_ROOT / "data/knowledge_base")
    """知识库文档根目录（磁盘上的 PDF/MD，与 Milvus/ES 索引配合）"""

    SQLALCHEMY_DATABASE_URI: str = (
        "mysql+pymysql://root:CHANGE_ME@127.0.0.1:3306/kylin_rag?charset=utf8mb4"
    )
    """台账数据库（知识库登记 / 文件台账 / 工单）。在 basic_settings.yaml 配置。"""

    EXEMPT_DEPTS: list[str] = []
    """豁免部门 ID 列表：这些部门（或指定 userid）的用户不受检索部门过滤限制（可跨部门查看）。"""

    EXEMPT_USERIDS: list[str] = []
    """豁免用户 ID 列表（OA userid）：不受检索部门过滤限制。"""

    OPEN_CROSS_DOMAIN: bool = False
    """API 是否开启跨域"""

    api_token: str = ""
    """API 鉴权 token。留空则不启用鉴权；配置后，非本地请求需在请求头携带此 token（X-API-Key 或 Authorization: Bearer）。"""

    gateway_shared_secret: str = ""
    """与 Java Gateway 约定的共享密钥；通过部署配置设置。"""

    require_gateway_identity: bool = False
    """生产环境建议开启，要求业务 API 必须携带 Java Gateway 用户上下文。"""

    permission_check_url: str = (
        "http://127.0.0.1:48081/rpc-api/system/permission/has-any-permissions"
    )
    """Java 权限校验接口；可在运行时 basic_settings.yaml 中覆盖。"""

    DEFAULT_BIND_HOST: str = "0.0.0.0" if sys.platform != "win32" else "127.0.0.1"
    """
    各服务器默认绑定host。如改为"0.0.0.0"需要修改下方所有XX_SERVER的host
    Windows 下 WEBUI 自动弹出浏览器时，如果地址为 "0.0.0.0" 是无法访问的，需要手动修改地址栏
    """

    API_SERVER: dict = {"host": DEFAULT_BIND_HOST, "port": 7861, "public_host": "127.0.0.1", "public_port": 7861}
    """API 服务器地址。其中 public_host 用于生成云服务公网访问链接（如知识库文档链接）"""

    WEBUI_SERVER: dict = {"host": DEFAULT_BIND_HOST, "port": 8501}
    """WEBUI 服务器地址"""

    def make_dirs(self):
        '''创建所有数据目录'''
        for p in [
            self.DATA_PATH,
            self.MEDIA_PATH,
            self.LOG_PATH,
            self.BASE_TEMP_DIR,
        ]:
            p.mkdir(parents=True, exist_ok=True)
        for n in ["image", "audio", "video"]:
            (self.MEDIA_PATH / n).mkdir(parents=True, exist_ok=True)
        Path(self.KB_ROOT_PATH).mkdir(parents=True, exist_ok=True)


class KBSettings(BaseFileSettings):
    """知识库相关配置"""

    model_config = SettingsConfigDict(yaml_file=KYLIN_ROOT / "kb_settings.yaml")

    DEFAULT_KNOWLEDGE_BASE: str = "oa_manuals"
    """WebUI 默认知识库"""

    TICKET_KB_NAME: str = "knowledge_tickets"
    """工单人工答案独立知识库（双索引；问答时与正式库并行检索，不写进正式文档）"""

    DEFAULT_VS_TYPE: t.Literal["dual"] = "dual"
    """知识库类型：仅 dual（Milvus 向量 + ES 关键词）"""

    PARENT_CHUNK_SIZE: int = 1200
    """父子切分：Parent 最大字符数（优先按标题切节，超长再切）"""

    CHILD_CHUNK_SIZE: int = 250
    """父子切分：Child 目标字符数（用于检索粒度）"""

    CHILD_OVERLAP_SIZE: int = 40
    """父子切分：Child 相邻重叠字符数（当前切分实现未使用脏 overlap）"""

    VECTOR_SEARCH_TOP_K: int = 5
    """最终返回给 LLM 的父块数"""

    RERANK_CANDIDATE_TOP_K: int = 30
    """RRF 融合后送入 Rerank 的候选数（温和调参：30，兼顾延迟与覆盖）"""

    HYBRID_RECALL_TOP_K: int = 30
    """双路召回时 Milvus / ES 各自取的候选数（温和调参：30）"""

    QUERY_PLAN_MAX_TOKENS: int = 2048
    """查询计划 LLM 的 max_tokens。

    仅输出短 JSON 是不够的：QwQ 等推理模型会先生成长思考链，256 会让输出
    在思考中途被截断，JSON 根本没生成，规划必然回退原文（无改写、无子查询
    拆分）。实测 2048 可稳定拿到完整 JSON。与答案预算解耦。
    """

    RRF_K: int = 60
    """Reciprocal Rank Fusion 常数 k"""

    SCORE_THRESHOLD: float = 2.0
    """向量距离阈值：越小越严。Milvus COSINE 距离约在 [0,2]，取 2 等于不筛"""

    RERANK_SCORE_THRESHOLD: float = 0.50
    """Rerank 最高 relevance 低于该值则返回空（拒识）。
    实测校准：检索评估集 positive 最低 0.589、negative 最高 0.418，
    0.50 正好卡在正负分界空档中间，全拒 negative 且不影响 positive。
    弱证据区间 [RERANK_SCORE_THRESHOLD, RERANK_WEAK_THRESHOLD) 走保守生成。"""

    RERANK_WEAK_THRESHOLD: float = 0.65
    """弱证据上界：top1 relevance 低于该值视为证据不足，生成侧必须声明局限并倾向拒答。"""

    PDF_OCR_THRESHOLD: t.Tuple[float, float] = (0.6, 0.6)
    """
    PDF OCR 控制：只对宽高超过页面一定比例（图片宽/页面宽，图片高/页面高）的图片进行 OCR。
    这样可以避免 PDF 中一些小图片的干扰，提高非扫描版 PDF 处理速度
    """

    kbs_config: t.Dict[str, t.Dict] = {
            "dual": {},
            "milvus": {
                "host": "127.0.0.1",
                "port": "19530",
                "user": "",
                "password": "",
                "secure": False
            },
            "es": {
                "scheme": "http",
                "host": "127.0.0.1",
                "port": "9200",
                "user": "",
                "password": "",
            },
        }
    """dual 知识库依赖的 Milvus / ES 连接配置（dual 本身无额外字段）"""

    TEXT_SPLITTER_NAME: str = "ParentChild"
    """当前切分策略名称（仅 ParentChild）"""

    EMBEDDING_KEYWORD_FILE: str = "embedding_keywords.txt"
    """Embedding模型定制词语的词表文件"""

    EMBEDDING_DIM: int = 1024
    """双索引 Milvus collection 的向量维度（bge-m3 为 1024）"""


class PlatformConfig(MyBaseModel):
    """模型加载平台配置"""

    platform_name: str = "xinference"
    """平台名称"""

    platform_type: t.Literal["xinference", "ollama", "oneapi", "fastchat", "openai", "custom openai"] = "xinference"
    """平台类型"""

    api_base_url: str = "http://127.0.0.1:9997/v1"
    """openai api url"""

    api_key: str = "EMPTY"
    """api key if available"""

    api_proxy: str = ""
    """API 代理"""

    api_concurrencies: int = 5
    """该平台单模型最大并发数"""

    auto_detect_model: bool = False
    """是否自动获取平台可用模型列表。设为 True 时下方不同模型类型可自动检测"""

    disable_thinking: bool = False
    """是否向该平台发送 vLLM/Qwen 专用的关闭 thinking 参数（chat_template_kwargs.enable_thinking=false）。
    仅对 vLLM / Qwen 系推理模型有效；OpenAI 兼容的外部 API（如 DeepSeek）不认识该字段，必须保持 False。"""

    grounding_style: t.Literal["strict", "lenient"] = "strict"
    """接地生成的拒答判定风格：
    - strict：严格措辞匹配，表述与问题有细微差异（如"客户端"vs"移动端客户端"）即视为未直接回答并拒答（QwQ 等可自行推理对应关系，保持 strict）。
    - lenient：只要参考资料包含与问题直接相关的具体信息（网址/数字/名称/步骤等）就必须回答，仅完全无关/完全不能对应时才拒答
      （给 DeepSeek 等严格字面匹配、易过度拒答的模型用）。"""

    llm_models: t.Union[t.Literal["auto"], t.List[str]] = []
    """该平台支持的大语言模型列表，auto_detect_model 设为 True 时自动检测"""

    embed_models: t.Union[t.Literal["auto"], t.List[str]] = []
    """该平台支持的嵌入模型列表，auto_detect_model 设为 True 时自动检测"""

    text2image_models: t.Union[t.Literal["auto"], t.List[str]] = []
    """该平台支持的图像生成模型列表，auto_detect_model 设为 True 时自动检测"""

    image2text_models: t.Union[t.Literal["auto"], t.List[str]] = []
    """该平台支持的多模态模型列表，auto_detect_model 设为 True 时自动检测"""

    rerank_models: t.Union[t.Literal["auto"], t.List[str]] = []
    """该平台支持的重排模型列表，auto_detect_model 设为 True 时自动检测"""

    speech2text_models: t.Union[t.Literal["auto"], t.List[str]] = []
    """该平台支持的 STT 模型列表，auto_detect_model 设为 True 时自动检测"""

    text2speech_models: t.Union[t.Literal["auto"], t.List[str]] = []
    """该平台支持的 TTS 模型列表，auto_detect_model 设为 True 时自动检测"""


class ApiModelSettings(BaseFileSettings):
    """模型配置项"""

    model_config = SettingsConfigDict(yaml_file=KYLIN_ROOT / "model_settings.yaml")

    DEFAULT_LLM_MODEL: str = "glm4-chat"
    """默认选用的 LLM 名称"""

    FALLBACK_LLM_MODEL: str = ""
    """备用 LLM 名称：主模型 429/超时/连接失败/5xx 时自动降级到该模型（须在 MODEL_PLATFORMS 中配置）。
    留空表示不启用自动降级。"""

    DEFAULT_EMBEDDING_MODEL: str = "bge-m3"
    """默认选用的 Embedding 名称"""

    DEFAULT_RERANK_MODEL: str = "bge-reranker-large"
    """默认选用的 Rerank 名称（需在 MODEL_PLATFORMS 的 rerank_models 中配置）"""

    HISTORY_LEN: int = 3
    """默认历史对话轮数"""

    MAX_TOKENS: t.Optional[int] = None
    """大模型最长支持的长度，如果不填写，则使用模型默认的最大长度，如果填写，则为用户设定的最大长度"""

    TEMPERATURE: float = 0.7
    """LLM通用对话参数"""

    MODEL_PLATFORMS: t.List[PlatformConfig] = [
            PlatformConfig(**{
                "platform_name": "xinference",
                "platform_type": "xinference",
                "api_base_url": "http://127.0.0.1:9997/v1",
                "api_key": "EMPTY",
                "api_concurrencies": 5,
                "auto_detect_model": True,
                "llm_models": [],
                "embed_models": [],
                "text2image_models": [],
                "image2text_models": [],
                "rerank_models": [],
                "speech2text_models": [],
                "text2speech_models": [],
            }),
            PlatformConfig(**{
                "platform_name": "ollama",
                "platform_type": "ollama",
                "api_base_url": "http://127.0.0.1:11434/v1",
                "api_key": "EMPTY",
                "api_concurrencies": 5,
                "llm_models": [
                    "qwen:7b",
                    "qwen2:7b",
                ],
                "embed_models": [
                    "quentinz/bge-large-zh-v1.5",
                ],
            }),
            PlatformConfig(**{
                "platform_name": "oneapi",
                "platform_type": "oneapi",
                "api_base_url": "http://127.0.0.1:3000/v1",
                "api_key": "sk-",
                "api_concurrencies": 5,
                "llm_models": [
                    # 智谱 API
                    "chatglm_pro",
                    "chatglm_turbo",
                    "chatglm_std",
                    "chatglm_lite",
                    # 千问 API
                    "qwen-turbo",
                    "qwen-plus",
                    "qwen-max",
                    "qwen-max-longcontext",
                    # 千帆 API
                    "ERNIE-Bot",
                    "ERNIE-Bot-turbo",
                    "ERNIE-Bot-4",
                    # 星火 API
                    "SparkDesk",
                ],
                "embed_models": [
                    # 千问 API
                    "text-embedding-v1",
                    # 千帆 API
                    "Embedding-V1",
                ],
                "text2image_models": [],
                "image2text_models": [],
                "rerank_models": [],
                "speech2text_models": [],
                "text2speech_models": [],
            }),
            PlatformConfig(**{
                "platform_name": "openai",
                "platform_type": "openai",
                "api_base_url": "https://api.openai.com/v1",
                "api_key": "sk-proj-",
                "api_concurrencies": 5,
                "llm_models": [
                    "gpt-4o",
                    "gpt-3.5-turbo",
                ],
                "embed_models": [
                    "text-embedding-3-small",
                    "text-embedding-3-large",
                ],
            }),
        ]
    """模型平台配置"""


class MemorySettings(BaseFileSettings):
    """长期记忆（跨会话）配置"""

    model_config = SettingsConfigDict(yaml_file=KYLIN_ROOT / "memory_settings.yaml")

    enabled: bool = False
    """总开关：false = 长期记忆完全停用（不提取、不注入）"""

    extract_every_turns: int = 1
    """每 N 轮提取一次事实（当前每轮问答后提取；预留节流）"""

    importance_threshold: int = 3
    """importance 低于该值的记忆不注入生成（保留入库）"""

    recall_top_k: int = 5
    """Milvus 向量召回候选数"""

    inject_max_items: int = 3
    """注入生成 prompt 的记忆条数上限"""

    inject_max_chars: int = 200
    """单条记忆注入的字符上限"""

    embedding_model: str = ""
    """记忆向量化模型名；留空 = 用默认 embedding 模型（bge-m3）"""


class LangfuseSettings(BaseFileSettings):
    """Langfuse 全链路观测配置（Trace）"""

    model_config = SettingsConfigDict(yaml_file=KYLIN_ROOT / "langfuse_settings.yaml")

    enabled: bool = False
    """总开关：false = 不初始化客户端、问答链路走 NoOp"""

    host: str = "http://127.0.0.1:3000"
    """Langfuse Web 地址（自建或 Cloud）"""

    public_key: str = ""
    """项目 Public Key（pk-lf-...）"""

    secret_key: str = ""
    """项目 Secret Key（sk-lf-...）"""

    capture_content: bool = True
    """是否记录 query / messages / answer 正文；false 时仅记元数据与长度"""

    sample_rate: float = 1.0
    """采样率 0~1；进程内按请求随机丢弃"""


class PromptSettings(BaseFileSettings):
    """Prompt 模板（jinja2）；问答运行时由 applications/knowledge_qa/prompt.py 使用。"""

    model_config = SettingsConfigDict(yaml_file=KYLIN_ROOT / "prompt_settings.yaml",
                                      json_file=KYLIN_ROOT / "prompt_settings.json",
                                      extra="allow")

    llm_model: dict = {
        "default": "{{input}}",
        "with_history": (
            "The following is a friendly conversation between a human and an AI.\n"
            "The AI is talkative and provides lots of specific details from its context.\n"
            "If the AI does not know the answer to a question, it truthfully says it does not know.\n\n"
            "Current conversation:\n"
            "{{history}}\n"
            "Human: {{input}}\n"
            "AI:"
            ),
    }
    '''普通 LLM 用模板'''

    rag: dict = {
        "default": (
            "【指令】你是企业知识库助手。只能依据下方「参考资料」回答用户问题。\n"
            "规则：\n"
            "1. 每一句有事实依据的陈述，在句末标注来源编号，如 [1] 或 [1][2]；编号必须来自参考资料中已有的 [N]。\n"
            "2. 不得使用参考资料以外的公司制度、流程或数据；不得臆造。\n"
            "3. 若参考资料不足以回答，只回复：根据已知信息无法回答该问题。\n"
            "4. 使用中文，简洁专业。\n\n"
            "【参考资料】\n{{context}}\n\n"
            "【问题】\n{{question}}\n"
            ),
        "empty": (
            # 兼容旧配置名；主链路拒答已在 generate 代码门实现，不再走自由发挥
            "根据当前知识库未找到足以回答该问题的依据，无法给出确切答复。"
            ),
        "chitchat": (
            "【指令】用户在闲聊或寒暄。简短友好回复即可，不要编造公司制度或内部流程。\n\n"
            "【问题】\n{{question}}\n"
            ),
    }
    '''RAG 用模板；运行时由知识问答应用层使用，此处供 yaml 模板同步'''


class SettingsContainer:
    KYLIN_ROOT = KYLIN_ROOT

    basic_settings: BasicSettings = settings_property(BasicSettings())
    kb_settings: KBSettings = settings_property(KBSettings())
    model_settings: ApiModelSettings = settings_property(ApiModelSettings())
    prompt_settings: PromptSettings = settings_property(PromptSettings())
    memory_settings: MemorySettings = settings_property(MemorySettings())
    langfuse_settings: LangfuseSettings = settings_property(LangfuseSettings())

    def create_all_templates(self):
        self.basic_settings.create_template_file(write_file=True)
        self.kb_settings.create_template_file(write_file=True)
        self.model_settings.create_template_file(sub_comments={
            "MODEL_PLATFORMS": {"model_obj": PlatformConfig(),
                                "is_entire_comment": True}},
            write_file=True)
        self.prompt_settings.create_template_file(write_file=True, file_format="yaml")
        self.memory_settings.create_template_file(write_file=True)
        self.langfuse_settings.create_template_file(write_file=True)

    def set_auto_reload(self, flag: bool=True):
        self.basic_settings.auto_reload = flag
        self.kb_settings.auto_reload = flag
        self.model_settings.auto_reload = flag
        self.prompt_settings.auto_reload = flag
        self.memory_settings.auto_reload = flag
        self.langfuse_settings.auto_reload = flag


Settings = SettingsContainer()


if __name__ == "__main__":
    Settings.create_all_templates()
