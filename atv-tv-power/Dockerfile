FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt ./

# ⚠️ arm/v7 走特殊分支，原因：
#    pyatv 依赖 miniaudio（C 扩展），而 miniaudio 【没有 32 位 ARM 的预编译 wheel】
#    ⇒ 在 arm/v7 上 pip 会尝试现场编译（python:3.12-slim 里没有 gcc，QEMU 下必败）。
#    本项目只用 pyatv 的开关机能力（pyatv / const / interface / protocols.companion），
#    完全不碰音频流 ⇒ arm/v7 上跳过 miniaudio：--no-deps 装 pyatv，再显式装其余依赖。
#    ⚠️ pyatv 升级时需同步下面这份依赖清单（对照 pypi.org/pypi/pyatv/json 的 requires_dist）。
#    平台支持见仓库根 README 的「支持的平台」一节。
ARG TARGETARCH
ARG TARGETVARIANT
RUN if [ "$TARGETARCH" = "arm" ] && [ "$TARGETVARIANT" = "v7" ]; then \
      pip install --no-cache-dir 'pyatv==0.18.0' --no-deps && \
      pip install --no-cache-dir \
        'aiohttp<5,>=3.8.3' 'cryptography>=44.0.1' 'chacha20poly1305-reuseable>=0.13.2' \
        'ifaddr>=0.1.7' 'protobuf>=6.31.1' 'pydantic>=2.0.0' 'requests>=2.30.0' \
        'srptools>=0.2.0' 'tabulate>=0.9.0' 'tinytag>=1.10.0' 'zeroconf>=0.129.0'; \
    else \
      pip install --no-cache-dir -r requirements.txt; \
    fi

COPY atv_tv_power.py ./

# keys/ 与 config.json 由 volume 挂载进来，不打进镜像（含私钥）
CMD ["python", "atv_tv_power.py"]
