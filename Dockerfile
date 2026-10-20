# Spark stack: one container that prepares, serves, and reports on three models across two DGX Sparks,
# with the DeepSeek harness bundled and wired to whatever is loaded.
FROM node:22-bookworm-slim AS harness
RUN npm install -g @deepseek-ai/dsh@0.2.1-rc.2 && npm cache clean --force

FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client curl ca-certificates procps tini \
 && rm -rf /var/lib/apt/lists/*

# The harness: node + the dsh CLI, so the container can run `dsh web` against the stack.
COPY --from=harness /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
RUN ln -sf /usr/local/lib/node_modules/@deepseek-ai/dsh/lib/bin.js /usr/local/bin/dsh

RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.30" pyyaml

COPY stack /opt/spark-stack/stack
COPY docker/entrypoint.sh /usr/local/bin/stack-entrypoint
RUN chmod +x /usr/local/bin/stack-entrypoint
ENV PYTHONPATH=/opt/spark-stack \
    PYTHONUNBUFFERED=1 \
    STACK_STATE_DIR=/var/lib/spark-stack

EXPOSE 8099 3080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/local/bin/stack-entrypoint"]
