FROM python:3.12-slim

# 国内网络下默认走镜像源加速；想用官方源：
#   docker compose build --build-arg PIP_INDEX=https://pypi.org/simple
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=$PIP_INDEX \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    PORT=8765 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ⚠️ 本镜像只发布 linux/amd64（2026-10-05 红领巾拍板：只保留 x86-64，ARM 不做）。
#    amd64 上全部依赖都有现成轮子（含 brotli / zstandard / cffi）⇒ 不需要编译器。
#    若哪天要恢复 ARM：arm/v7 上 brotli / zstandard / cffi 都没有预编译轮子（piwheels 也没有
#    cp312 的），必须现场编译 —— 实测单是 pip install 就要 30 分钟。要恢复就把下面这段
#    加在 pip install 之前，并在 workflow 的 platforms 里加回目标平台：
#      ARG TARGETARCH
#      ARG TARGETVARIANT
#      RUN if [ "$TARGETARCH" = "arm" ] && [ "$TARGETVARIANT" = "v7" ]; then \
#            apt-get update && apt-get install -y --no-install-recommends gcc g++ libffi-dev && \
#            rm -rf /var/lib/apt/lists/*; \
#          fi

COPY app/ ./app/

VOLUME ["/data"]
EXPOSE 8765

CMD ["uvicorn", "server:app", "--app-dir", "/app/app", "--host", "0.0.0.0", "--port", "8765"]
