# Vernal 统一网关与多智能体编排层镜像（同时支持 linux/amd64 与 linux/arm64 麒麟国产平台）
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    nodejs \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY autodev ./autodev

RUN pip install --no-cache-dir -e . && mkdir -p /app/workspaces

EXPOSE 8787

CMD ["vernal", "serve", "--host", "0.0.0.0", "--port", "8787"]
