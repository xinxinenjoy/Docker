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

COPY app/ ./app/

VOLUME ["/data"]
EXPOSE 8765

CMD ["uvicorn", "server:app", "--app-dir", "/app/app", "--host", "0.0.0.0", "--port", "8765"]
