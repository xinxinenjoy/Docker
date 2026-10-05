FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt ./

# arm/v7（32 位 ARM）上 cffi 与 miniaudio 都没有预编译 wheel（miniaudio 只发过 cp37 的
# armv7l 轮子，对 Python 3.12 无用），pip 只能从源码编译，而 python:3.12-slim 里不含编译器
# ⇒ 仅在 arm/v7 上装一套工具链。libasound2-dev 供 miniaudio 在 Linux 上链接音频后端。
# amd64 / arm64 的依赖全都有 wheel，这一步直接跳过、什么都不装。
# ⚠️ 不能靠 --no-deps 跳过 miniaudio：pyatv/helpers.py 在 import 期就 `import miniaudio`，
#    少了它连 `import pyatv` 都会失败。
ARG TARGETARCH
ARG TARGETVARIANT
RUN if [ "$TARGETARCH" = "arm" ] && [ "$TARGETVARIANT" = "v7" ]; then \
      apt-get update && \
      apt-get install -y --no-install-recommends gcc g++ libffi-dev libasound2-dev && \
      rm -rf /var/lib/apt/lists/*; \
    fi

RUN pip install --no-cache-dir -r requirements.txt

COPY atv_tv_power.py ./

# keys/ 与 config.json 由 volume 挂载进来，不打进镜像（含私钥）
CMD ["python", "atv_tv_power.py"]
