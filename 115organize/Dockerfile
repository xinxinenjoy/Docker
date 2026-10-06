FROM python:3.12-slim

# 国内网络下默认走镜像源加速；想用官方源：
#   docker compose build --build-arg PIP_INDEX=https://pypi.org/simple
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=$PIP_INDEX \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ⚠️ 本镜像只发布 linux/amd64（沿用本仓库 2026-10-05 的拍板：只做 x86-64）。
#    本项目的依赖比 115offline 轻得多 —— `p115client` 是纯 Python，
#    所以就算哪天要恢复 ARM，也不需要接编译器。真要恢复时只需在 workflow 的
#    platforms 里加回目标平台即可。

COPY app/ ./app/

# 树文件、cookie、快照、报告、日志全在 /data 下。
#   /data/tree/       ← 目录树导出件（放这里，`plan` 会挑最新的那份）
#   /data/cookie.txt  ← 115 cookie（或挂 115offline 的 /data 复用 accounts.json）
#   /data/reports/    ← plan.md / plan-json / 每轮回执
VOLUME ["/data"]

# ⚠️ 用 ENTRYPOINT 而不是 CMD —— 这样 `docker run <镜像> once` 能直接跑一轮增量，
#    不用先把整条 `python -m app ...` 抄一遍。默认动作是常驻（watch）。
ENTRYPOINT ["python", "-m", "app"]
CMD ["watch"]
