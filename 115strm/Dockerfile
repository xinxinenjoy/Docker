FROM python:3.12-slim

# 国内网络下默认走镜像源加速；想用官方源：
#   docker compose build --build-arg PIP_INDEX=https://pypi.org/simple
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=$PIP_INDEX \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    OUTPUT_DIR=/out \
    PORT=8767 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ⚠️ 本镜像只发布 linux/amd64（沿用本仓库 2026-10-05 的拍板：只做 x86-64）。
#    本项目依赖全是**纯 Python**（p115client / fastapi / uvicorn 无 [standard]）⇒
#    就算哪天要恢复 ARM，也不需要接编译器，只在 workflow 的 platforms 里加回来即可。

COPY app/ ./app/

# 两个卷（都不含凭据，凭据在 /data 的账号文件里）：
#   /data  —— config.json（网页上改的参数）、accounts.json（115 账号）、
#             tree/（目录树导出件丢这里）、reports/（回执）、logs/、断点
#   /out   —— 生成的 strm 树。宿主机挂一个目录给它（如 `-v /你的目录/115strm:/out`），
#             再由媒体服务器 / Alist 挂出去给播放设备扫。
VOLUME ["/data", "/out"]
EXPOSE 8767

# ⚠️ 用 ENTRYPOINT 而不是 CMD —— 这样 `docker run <镜像> tree` / `doctor` 能直接跑。
ENTRYPOINT ["python", "-m", "app"]
CMD ["serve"]
