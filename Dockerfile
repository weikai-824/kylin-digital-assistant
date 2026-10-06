# syntax=docker/dockerfile:1
# 应用镜像：API :7861 + WebUI :8501
FROM python:3.11-slim-bookworm

# pip 镜像源：默认清华源（国内直连 PyPI 慢且易超时）；如需切换用 --build-arg PIP_INDEX_URL=<url> 覆盖
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KYLIN_ROOT=/data/kylin \
    PYTHONPATH=/app

# apt 换清华源（deb.debian.org 国内直连不稳，易超时）
RUN sed -i 's@deb.debian.org@mirrors.tuna.tsinghua.edu.cn@g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        curl \
        build-essential \
        gcc \
        g++ \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
# 依赖分两步装，规避 pip 26 的 ResolutionTooDeep：
# langfuse 的 opentelemetry 传递依赖与 docling 生态的严格钉死版本一起全量解析时，
# 依赖图复杂度会超过 pip 的解析深度上限；先装其余依赖、再单独装 langfuse 即可。
# langfuse 版本仍以 requirements.txt 为准（此处从文件提取，不重复写死）。
# pip 下载缓存挂载到 buildkit（不进镜像层）：requirements 变更导致本层缓存失效时，
# 已下载的 wheel 直接命中，只重新下载变更的包，避免 30 分钟级全量重装。
# 注意：PIP_NO_CACHE_DIR 必须移除，否则 pip 不写缓存、挂载形同虚设。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip \
    && pip install torch==2.5.1 torchvision==0.20.1 \
        --index-url https://download.pytorch.org/whl/cpu \
    && grep -v -E '^langfuse==' /app/requirements.txt > /tmp/requirements-base.txt \
    && pip install -r /tmp/requirements-base.txt \
    && grep -E '^langfuse==' /app/requirements.txt | xargs -r pip install \
    && pip check

COPY . /app
RUN chmod +x /app/deploy/entrypoint.sh

VOLUME ["/data/kylin"]
EXPOSE 7861 8501

ENTRYPOINT ["/app/deploy/entrypoint.sh"]
