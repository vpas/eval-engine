# eval-engine — ONE image for every role (control-plane API, Ray head/worker, reconciler).
# The role is chosen by the container *command*, not the image (see deploy/README.md):
#   - control plane : uvicorn eval_engine.api:app   (the default CMD below)
#   - Ray pods      : KubeRay injects `ray start ...` and runs our code as a submitted job
# Built from the prototype/ package; production code will graduate out of prototype/, but the
# deployable shape is identical, so this is the real artifact, not a throwaway.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EVAL_ENGINE_BACKEND=postgres

# Only curl/ca-certs needed — psycopg[binary], clickhouse-connect, ray, duckdb all ship wheels.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/prototype

# 1) DEPENDENCY layer — cached unless pyproject changes. Install deps via a stub package, then
#    drop the stub so only third-party deps remain. This is what makes code edits rebuild fast.
COPY prototype/pyproject.toml ./
RUN mkdir -p eval_engine && : > eval_engine/__init__.py \
    && pip install '.[postgres,ray,openrouter]' \
    && pip uninstall -y eval-engine-prototype

# 2) CODE layer — real source + editable install with --no-deps (seconds, deps already present).
COPY prototype/ ./
RUN pip install -e '.[postgres,ray,openrouter]' --no-deps

EXPOSE 8077

# Default role = control-plane API. Override `command` for Ray pods / one-off jobs.
CMD ["uvicorn", "eval_engine.api:app", "--host", "0.0.0.0", "--port", "8077"]
