FROM python:3.13-slim
# subhub 零第三方依赖, 装什么东西都不需要
WORKDIR /app
COPY checker.py db.py server.py ./
COPY static/ ./static/
# 数据库放挂载卷里, 容器重建不丢检测历史
ENV SUBHUB_DB=/data/subhub.db
VOLUME /data
EXPOSE 8765
CMD ["python3", "server.py"]
