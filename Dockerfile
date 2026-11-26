# Vernal 统一网关与多智能体编排层镜像（同时支持 linux/amd64 与 linux/arm64 麒麟国产平台）
ARG VERNAL_PYTHON_IMAGE=python:3.12-slim
FROM ${VERNAL_PYTHON_IMAGE}

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    nodejs \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/* \
  && git config --system --add safe.directory '*'

WORKDIR /app

# 先安装运行依赖与 PDF/DOCX 文档解析依赖，并建立非特权运行用户 vernal
COPY pyproject.toml README.md ./
RUN python3 -c "import subprocess, sys, tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; deps = [*p['dependencies'], *p.get('optional-dependencies', {}).get('docs', []), 'socksio>=1.0', 'pytest>=8,<10']; subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--no-cache-dir', *deps])" \
  && printf '#!/bin/sh\nexec python3 -m autodev.cli "$@"\n' > /usr/local/bin/vernal \
  && cp /usr/local/bin/vernal /usr/local/bin/autodev \
  && chmod +x /usr/local/bin/vernal /usr/local/bin/autodev \
  && (id -u vernal >/dev/null 2>&1 || useradd -m -u 1000 -s /bin/sh vernal)

ENV PYTHONPATH=/app
COPY autodev ./autodev
COPY deploy/loopforge/start-runner.sh deploy/loopforge/wait-and-bind-runner.sh ./deploy/loopforge/
RUN mkdir -p /app/workspaces && chown -R vernal:vernal /app

USER vernal
EXPOSE 8787

CMD ["vernal", "serve", "--host", "0.0.0.0", "--port", "8787"]
