# 多阶段构建
# syntax=docker/dockerfile:1.7

############################
# 阶段1: 依赖构建
############################
FROM python:3.12-slim AS builder

ARG DEBIAN_MIRROR=mirrors.ustc.edu.cn
ARG PYPI_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PYPI_TRUSTED=pypi.tuna.tsinghua.edu.cn

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# 换源 + 安装构建工具
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    else \
      sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list; \
    fi; \
    apt-get update || apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# 复制依赖文件并安装
COPY pyproject.toml poetry.lock* ./

RUN set -eux; \
    pip config set global.index-url "${PYPI_URL}"; \
    pip config set install.trusted-host "${PYPI_TRUSTED}"; \
    pip install --upgrade pip; \
    pip install poetry==2.0.0; \
    poetry config virtualenvs.create false; \
    poetry install --no-interaction --no-ansi --no-root;

############################
# 阶段2: 运行时镜像
############################
FROM python:3.12-slim AS runtime

ARG DEBIAN_MIRROR=mirrors.ustc.edu.cn
ARG TZ=Asia/Shanghai

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PATH="/app/.venv/bin:$PATH"

# 换源 + 时区
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    else \
      sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list; \
    fi; \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# 从 builder 阶段复制已安装的依赖
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# 复制业务代码
COPY . .

# 非 root 用户
RUN useradd -m -u 5678 appuser && \
    chown -R appuser:appuser /app
USER appuser

CMD ["gunicorn", "-c", "config/gunicorn.py", "main:app"]