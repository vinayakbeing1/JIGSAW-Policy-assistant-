# syntax=docker/dockerfile:1
# Jigsaw Policy Assistant - one image for the API, the ingestion worker and one-off commands.
#   runtime (default): application only
#   dev:               adds test/lint/tooling dependencies; used by the `tests` and `tools` compose services

FROM python:3.13-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    RERANKER_CACHE_DIR=/opt/models

# Tesseract reads scanned PDF pages. The install is retried as a whole because deb.debian.org is a CDN and a
# single unreachable edge fails individual downloads (behind a proxy as HTTP 403s, which apt does not retry).
RUN printf 'Acquire::Retries "3";\nAcquire::http::Timeout "30";\n' > /etc/apt/apt.conf.d/80-retries \
    && apt-get update \
    && for attempt in 1 2 3 4 5; do \
         apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng && break; \
         if [ "$attempt" = 5 ]; then exit 1; fi; \
         echo "apt-get install failed (attempt $attempt), retrying..." >&2; sleep $((attempt * 5)); \
       done \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb

RUN pip install --no-cache-dir uv==0.13.0
WORKDIR /app

# The cross-encoder reranker (app/rag/rerank.py) is downloaded into the image at build time, because the
# containers run on a read-only filesystem. Build with --build-arg RERANKER_MODEL=... when changing it.
ARG RERANKER_MODEL=Xenova/ms-marco-MiniLM-L-6-v2
ENV RERANKER_IMAGE_MODEL=${RERANKER_MODEL}


FROM base AS dev
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
RUN python -c "import os; from fastembed.rerank.cross_encoder import TextCrossEncoder; \
TextCrossEncoder(os.environ['RERANKER_IMAGE_MODEL'], cache_dir=os.environ['RERANKER_CACHE_DIR'])"
COPY . .


FROM base AS runtime
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data/uploads && chown app:app /data/uploads
RUN python -c "import os; from fastembed.rerank.cross_encoder import TextCrossEncoder; \
TextCrossEncoder(os.environ['RERANKER_IMAGE_MODEL'], cache_dir=os.environ['RERANKER_CACHE_DIR'])"
# Everything the API and worker need is in the image; never contact the model hub at runtime.
ENV HF_HUB_OFFLINE=1

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY eval ./eval
COPY sample_data/manifest.json ./sample_data/manifest.json
COPY sample_data/pdfs ./sample_data/pdfs

USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-server-header"]
