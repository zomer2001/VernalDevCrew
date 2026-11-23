# Vernal 统一网关与多智能体编排层镜像（同时支持 linux/amd64 与 linux/arm64 麒麟国产平台）
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    nodejs \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/* \
  && git config --system --add safe.directory '*'

WORKDIR /app

# 先仅安装 pyproject.toml 中的运行依赖（绕开 hatchling 隔离构建，且修改业务代码时完全命中 Docker 层缓存）
COPY pyproject.toml README.md ./
RUN python3 -c "import subprocess, sys, tomllib; deps = tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']; subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--no-cache-dir', *deps])" \
  && printf '#!/bin/sh\nexec python3 -m autodev.cli "$@"\n' > /usr/local/bin/vernal \
  && cp /usr/local/bin/vernal /usr/local/bin/autodev \
  && chmod +x /usr/local/bin/vernal /usr/local/bin/autodev

ENV PYTHONPATH=/app
COPY autodev ./autodev
RUN mkdir -p /app/workspaces

EXPOSE 8787

CMD ["vernal", "serve", "--host", "0.0.0.0", "--port", "8787"]
