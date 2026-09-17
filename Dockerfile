FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/obligations.db

WORKDIR /app

COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY backend/app /app/app
COPY backend/static /app/static

# 数据库文件目录（挂载卷持久化；首次启动自动建表并写入演示数据）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 单 worker：进程内写锁 + SQLite 单写者，保证 CAS 事务串行、无部分写入
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
