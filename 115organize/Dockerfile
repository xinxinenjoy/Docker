FROM python:3.12-slim

# 国内网络下默认走镜像源加速；想用官方源：
#   docker compose build --build-arg PIP_INDEX=https://pypi.org/simple
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=$PIP_INDEX \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    PORT=8766 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ⚠️ 本镜像只发布 linux/amd64（沿用本仓库 2026-10-05 的拍板：只做 x86-64）。
#    本项目依赖全是**纯 Python**（p115client / fastapi / uvicorn 无 [standard] / pydantic 有轮子）
#    ⇒ 就算哪天要恢复 ARM，也不需要接编译器，只在 workflow 的 platforms 里加回来即可。

COPY app/ ./app/

# /data 是**唯一**要持久化的地方：
#   tree/            目录树导出件（丢进去，页面会自己挑最新那份）
#   config.json      ★ 参数（**网页上改的就是它**，`.env` 里不用写业务参数）
#   accounts.json    ★ 115 账号（与 115offline 共用这一份 ⇒ cookie 只维护一处）
#   reports/         plan.md 与每轮回执
#   logs/  web-state.json
VOLUME ["/data"]

# ⚠️ 用 ENTRYPOINT 而不是 CMD —— 这样 `docker run <镜像> plan` / `once` 能直接跑，
#    不用先把整条 `python -m app ...` 抄一遍。
ENTRYPOINT ["python", "-m", "app"]
# 默认起**网页界面**（原来是 watch）。想每天定时跑就再开一个容器：
#     docker run -d --name 115organize-cron --env-file .env \
#        -v /volume1/docker/115organize/data:/data <镜像> watch
CMD ["serve"]
