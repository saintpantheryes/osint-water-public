# 后台任务(worker,架构方案第 4 节)的运行环境:Python 3.11 与依赖。程序与配置不打进镜像:运行时挂载主机上的仓库检出
# /opt/osint-water(每天由 worker_update.sh 取 main 的新程序并重启);依赖或本文件变了才重建。构建上下文是仓库根目录(.dockerignore 只放行依赖清单)。
FROM python:3.11-slim
COPY requirements.txt /tmp/req/requirements.txt
RUN pip install --no-cache-dir -r /tmp/req/requirements.txt
ENV PYTHONUNBUFFERED=1 \
    OW_CACHE=/cache
WORKDIR /opt/osint-water
# 常驻循环:每 2 分钟补全上传的线索、阶段变更记跟进;每天 08:30 采集与判定、建议商机(core/worker.py)
CMD ["python", "-m", "core.worker", "--profile", "private/profile.yaml"]
