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

# ⚠️ 本镜像只发布 linux/amd64 与 linux/arm64 —— 【不出 arm/v7】。原因（构建日志实测）：
#    arm/v7 上 brotli / zstandard / cffi 都没有预编译轮子（piwheels 也没有 cp312 的），
#    必须现场编译 ⇒ 光 pip install 一项就是 1819s，整条 arm/v7 腿 1880s（≈31 分钟），
#    占整个构建时长的 88%，而它只多覆盖树莓派 3 这类老设备。不划算，故不提供。
#    真要恢复：把下面这段加在 RUN pip install 之前，并恢复 workflow 里的 linux/arm/v7。
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
