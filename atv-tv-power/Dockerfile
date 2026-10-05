FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt ./

# ⚠️ miniaudio 在 PyPI 上只发 x86_64 / macOS / Windows 的轮子 ——
#    **Linux 的 aarch64 与 armv7l 一个都没有**（armv7l 只有 cp37 的老轮子，对 3.12 无用）。
#    而 pyatv/helpers.py 在 import 期就 `import miniaudio`（不加 try/except），
#    所以【不能用 --no-deps 跳过它】，只能现场编译 ⇒ arm64 与 arm/v7 都要编译器。
#    python:3.12-slim 不含编译器，故只在【非 amd64】平台装工具链：
#      gcc/g++        —— 编译 miniaudio（cffi 的构建脚本会探测 c++，缺它就报 FileNotFoundError）
#      libffi-dev     —— 编译 cffi 所需，同时带来运行期要用的 libffi8
#      libasound2-dev —— miniaudio 在 Linux 上链接音频后端所需
#    amd64 有现成轮子，这一步直接跳过、什么都不装。
#    ⚠️ 代价：首次构建要编译 miniaudio（QEMU 下很慢）；workflow 已配 type=gha 层缓存，
#       只要 requirements.txt / 本文件不变，后续构建会直接命中缓存、不再重编。
ARG TARGETARCH
ARG TARGETVARIANT
RUN if [ "$TARGETARCH" = "arm" ] || [ "$TARGETARCH" = "arm64" ]; then \
      apt-get update && \
      apt-get install -y --no-install-recommends gcc g++ libffi-dev libasound2-dev && \
      rm -rf /var/lib/apt/lists/*; \
    fi

RUN pip install --no-cache-dir -r requirements.txt

COPY atv_tv_power.py ./

# keys/ 与 config.json 由 volume 挂载进来，不打进镜像（含私钥）
CMD ["python", "atv_tv_power.py"]
