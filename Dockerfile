# Spark Stack controller: the web UI, the model runner (over ssh to the Sparks) and the /v1 front door.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client curl ca-certificates procps tini \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir fastapi==0.159.1 starlette==1.9.0 pydantic==2.14.1 "uvicorn[standard]==0.54.0" httpx==0.28.1

COPY stack /opt/spark-stack/stack
COPY docker/entrypoint.sh /usr/local/bin/stack-entrypoint
RUN chmod +x /usr/local/bin/stack-entrypoint
ENV PYTHONPATH=/opt/spark-stack \
    PYTHONUNBUFFERED=1 \
    STACK_STATE_DIR=/var/lib/spark-stack

EXPOSE 8099
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/local/bin/stack-entrypoint"]
