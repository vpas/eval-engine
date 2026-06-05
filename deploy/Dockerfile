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

# Only curl/ca-certs needed — psycopg[binary], clickhouse-connect, duckdb all ship wheels.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1) DEPENDENCY layer — cached unless pyproject changes. Install deps via a stub package, then
#    drop the stub so only third-party deps remain. This is what makes code edits rebuild fast.
COPY pyproject.toml ./
RUN mkdir -p eval_engine && : > eval_engine/__init__.py \
    && pip install '.[postgres,openrouter,gcs]' \
    && pip uninstall -y eval-engine

# 2) CODE layer — real source + editable install with --no-deps (seconds, deps already present).
COPY eval_engine/ ./eval_engine/
COPY static/ ./static/
COPY examples/ ./examples/
RUN pip install -e '.[postgres,openrouter,gcs]' --no-deps

EXPOSE 8077

# Default role = control-plane API. Override `command` for the orchestrator / worker.
CMD ["uvicorn", "eval_engine.api:app", "--host", "0.0.0.0", "--port", "8077"]
