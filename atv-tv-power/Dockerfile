FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt ./

# ⚠️ 本镜像只发布 linux/amd64（2026-10-05 红领巾拍板：只保留 x86-64，ARM 不做）。
#    amd64 上 miniaudio / cffi 都有现成轮子 ⇒ 不需要编译器，直接 pip install 即可。
#    若哪天要恢复 ARM：miniaudio 在 PyPI 上【没有】任何 Linux ARM 轮子
#    （aarch64 没有、armv7l 只有 cp37 的老轮子，对 3.12 无用），而 pyatv/helpers.py
#    在 import 期就 `import miniaudio`（没有 try/except）⇒ 不能用 --no-deps 跳过，
#    只能现场编译。要恢复就把下面这段加在 pip install 之前，并在 workflow 的
#    platforms 里加回目标平台：
#      ARG TARGETARCH
#      RUN if [ "$TARGETARCH" = "arm" ] || [ "$TARGETARCH" = "arm64" ]; then \
#            apt-get update && \
#            apt-get install -y --no-install-recommends gcc g++ libffi-dev libasound2-dev && \
#            rm -rf /var/lib/apt/lists/*; \
#          fi
#    （gcc/g++ 编译 miniaudio 与 cffi，libffi-dev 提供 cffi 构建所需头文件，
#      libasound2-dev 供 miniaudio 链接 Linux 音频后端。）

RUN pip install --no-cache-dir -r requirements.txt

COPY atv_tv_power.py ./

# keys/ 与 config.json 由 volume 挂载进来，不打进镜像（含私钥）
CMD ["python", "atv_tv_power.py"]
