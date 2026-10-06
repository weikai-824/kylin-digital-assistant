from __future__ import annotations

from typing import Dict, List, Literal, Optional, Union

from fastapi import UploadFile
from openai.types.chat import (
    ChatCompletionMessageParam,
    ChatCompletionToolChoiceOptionParam,
    ChatCompletionToolParam,
    completion_create_params,
)

from pydantic import AnyUrl, BaseModel, Field


class OpenAIBaseInput(BaseModel):
    user: Optional[str] = None
    # Use the following arguments if you need to pass additional parameters to the API that aren't available via kwargs.
    # The extra values given here take precedence over values defined on the client or passed to this method.
    extra_headers: Optional[Dict] = None
    extra_query: Optional[Dict] = None
    extra_json: Optional[Dict] = Field(None, alias="extra_body")
    timeout: Optional[float] = None

    class Config:
        extra = "allow"


class OpenAIChatInput(OpenAIBaseInput):
    messages: List[ChatCompletionMessageParam]
    model: Optional[str] = None  # 未指定时由服务端解析默认(调用时,非 import 时)
    frequency_penalty: Optional[float] = None
    function_call: Optional[completion_create_params.FunctionCall] = None
    functions: List[completion_create_params.Function] = None
    logit_bias: Optional[Dict[str, int]] = None
    logprobs: Optional[bool] = None
    max_tokens: Optional[int] = None
    n: Optional[int] = None
    presence_penalty: Optional[float] = None
    response_format: completion_create_params.ResponseFormat = None
    seed: Optional[int] = None
    stop: Union[Optional[str], List[str]] = None
    stream: Optional[bool] = None
    temperature: Optional[float] = None  # 未指定时由服务端解析默认
    tool_choice: Optional[Union[ChatCompletionToolChoiceOptionParam, str]] = None
    tools: List[Union[ChatCompletionToolParam, str]] = None
    top_logprobs: Optional[int] = None
    top_p: Optional[float] = None


class OpenAIEmbeddingsInput(OpenAIBaseInput):
    input: Union[str, List[str]]
    model: str
    dimensions: Optional[int] = None
    encoding_format: Optional[Literal["float", "base64"]] = None


class OpenAIImageBaseInput(OpenAIBaseInput):
    model: str
    n: int = 1
    response_format: Optional[Literal["url", "b64_json"]] = None
    size: Optional[
        Literal["256x256", "512x512", "1024x1024", "1792x1024", "1024x1792"]
    ] = "256x256"


class OpenAIImageGenerationsInput(OpenAIImageBaseInput):
    prompt: str
    quality: Literal["standard", "hd"] = None
    style: Optional[Literal["vivid", "natural"]] = None


class OpenAIImageVariationsInput(OpenAIImageBaseInput):
    image: Union[UploadFile, AnyUrl]


class OpenAIImageEditsInput(OpenAIImageVariationsInput):
    prompt: str
    mask: Union[UploadFile, AnyUrl]


class OpenAIAudioTranslationsInput(OpenAIBaseInput):
    file: Union[UploadFile, AnyUrl]
    model: str
    prompt: Optional[str] = None
    response_format: Optional[str] = None
    temperature: Optional[float] = None  # 未指定时由服务端解析默认


class OpenAIAudioTranscriptionsInput(OpenAIAudioTranslationsInput):
    language: Optional[str] = None
    timestamp_granularities: Optional[List[Literal["word", "segment"]]] = None


class OpenAIAudioSpeechInput(OpenAIBaseInput):
    input: str
    model: str
    voice: str
    response_format: Optional[
        Literal["mp3", "opus", "aac", "flac", "pcm", "wav"]
    ] = None
    speed: Optional[float] = None


# class OpenAIFileInput(OpenAIBaseInput):
#     file: UploadFile # FileTypes
#     purpose: Literal["fine-tune", "assistants"] = "assistants"


# OpenAI 兼容输出模型已下沉为 kylin.platform.runtime.schemas（共享值对象），此处再导出保持旧路径兼容
from kylin.platform.runtime.schemas import (  # noqa: E402,F401
    MsgType,
    OpenAIBaseOutput,
    OpenAIChatOutput,
)

