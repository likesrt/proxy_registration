# syntax=docker/dockerfile:1.7

####################################################################
# Stage 1 · 只负责抓源码
#   这一层是唯一需要"每次重跑"的，成本很低（一次 git fetch）
####################################################################
FROM python:3.12-slim-bookworm AS src

ARG REPO_URL=https://github.com/likesrt/proxy_registration
ARG REPO_REF=main
ARG GITHUB_TOKEN=
# CACHEBUST 的唯一作用：值变了 -> 这层缓存失效 -> 重新 git fetch。
# 不传时默认 0，此时会命中缓存拿到旧代码，所以必须由 compose 传进来。
ARG CACHEBUST=0

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git \
 && rm -rf /var/lib/apt/lists/*

# 注意两点：
#   1. 不能加 set -x，否则 GITHUB_TOKEN 会被打进构建日志
#   2. GITHUB_TOKEN 用 $$ 转义，避免被 Docker 在构建期替换成明文写进镜像历史
RUN set -eu; \
    echo "cachebust=$CACHEBUST"; \
    if [ -n "$$GITHUB_TOKEN" ]; then \
        git config --global url."https://x-access-token:$${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"; \
    fi; \
    git init -q -b main /src; \
    cd /src; \
    git remote add origin "$REPO_URL"; \
    git fetch -q --depth 1 origin "$REPO_REF"; \
    git checkout -q FETCH_HEAD; \
    git rev-parse HEAD > /src/COMMIT; \
    rm -rf /src/.git

####################################################################
# Stage 2 · 最终镜像
####################################################################
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ★ 关键：只先把 requirements.txt 单独拷进来。
#   COPY --from 的缓存 key 是「所拷文件内容的哈希」，不是上一层 stage 的 id。
#   所以 src 因新 commit 重建时，只要依赖清单没变，下面这几层依然 CACHED。
COPY --from=src /src/requirements.txt /app/requirements.txt

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /app/requirements.txt

RUN python -m playwright install-deps firefox

# 不要给 camoufox 挂 cache mount：cache mount 不写进镜像层，
# 浏览器内核会留在挂载点里、镜像中反而没有，运行时会炸。
RUN python -m camoufox fetch

# ★ 源码放最后：这层每次新 commit 都会失效，但只拷贝文件，很便宜
COPY --from=src /src/ /app/

EXPOSE 5072 5080
