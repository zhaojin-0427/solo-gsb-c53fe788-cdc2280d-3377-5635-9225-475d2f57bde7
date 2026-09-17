FROM python:3.12-slim

WORKDIR /app

# 依赖安装随镜像构建完成
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite 数据目录（可通过卷持久化）；数据库初始化随应用启动自动完成
ENV APP_DB=/data/app.db
RUN mkdir -p /data

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
