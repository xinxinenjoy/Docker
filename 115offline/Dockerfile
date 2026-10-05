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

# arm/v7（32 位 ARM）上网盘 SDK 拉进来的 brotli / zstandard / cffi 都没有预编译 wheel，
# pip 只能从源码编译，而 python:3.12-slim 里不含编译器 ⇒ 仅在 arm/v7 上装一套工具链。
# amd64 / arm64 的依赖全都有 wheel，这一步直接跳过、什么都不装。
# libffi-dev 是编译 cffi 所需，同时带来运行期要用的 libffi8。
ARG TARGETARCH
ARG TARGETVARIANT
RUN if [ "$TARGETARCH" = "arm" ] && [ "$TARGETVARIANT" = "v7" ]; then \
      apt-get update && \
      apt-get install -y --no-install-recommends gcc g++ libffi-dev && \
      rm -rf /var/lib/apt/lists/*; \
    fi

RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

VOLUME ["/data"]
EXPOSE 8765

CMD ["uvicorn", "server:app", "--app-dir", "/app/app", "--host", "0.0.0.0", "--port", "8765"]
