# eval-engine — ONE image for every role; the role is chosen by the container *command*:
#   - control-plane API : uvicorn eval_engine.api:app          (the default CMD below)
#   - orchestrator      : python -m eval_engine.orchestrator   (admit → expand → reconcile → finalize)
#   - worker            : python -m eval_engine.worker         (claim → execute → commit → load)
# Build context = repo root (so the Dockerfile path is deploy/Dockerfile, context is `.`).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EVAL_ENGINE_BACKEND=postgres

# curl/ca-certs for wheels; helm for the agentic k8s sandbox (inspect-k8s-sandbox shells out to it).
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://get.helm.sh/helm-v3.16.3-linux-amd64.tar.gz | tar xz -C /tmp \
    && mv /tmp/linux-amd64/helm /usr/local/bin/helm && rm -rf /tmp/linux-amd64

WORKDIR /app

# 1) DEPENDENCY layer — cached unless pyproject changes. Install deps via a stub package, then
#    drop the stub so only third-party deps remain. This is what makes code edits rebuild fast.
COPY pyproject.toml ./
RUN mkdir -p eval_engine && : > eval_engine/__init__.py \
    && pip install '.[postgres,openrouter,gcs,k8s]' \
    && pip uninstall -y eval-engine

# 2) CODE layer — real source + editable install with --no-deps (seconds, deps already present).
COPY eval_engine/ ./eval_engine/
COPY static/ ./static/
COPY examples/ ./examples/
COPY deploy/sandbox/ ./deploy/sandbox/
RUN pip install -e '.[postgres,openrouter,gcs,k8s]' --no-deps

# Reproducibility pin (DESIGN §14): stamp the build's git SHA so every run records the exact worker
# code that produced it. Placed late so changing it only rebuilds this tiny layer (deps/code cached).
# Pass at build time: `docker build --build-arg GIT_SHA=$(git rev-parse --short HEAD) …`.
ARG GIT_SHA=dev
ENV EVAL_ENGINE_IMAGE_DIGEST=$GIT_SHA

EXPOSE 8077

# Default role = control-plane API. Override `command` for the orchestrator / worker.
CMD ["uvicorn", "eval_engine.api:app", "--host", "0.0.0.0", "--port", "8077"]
