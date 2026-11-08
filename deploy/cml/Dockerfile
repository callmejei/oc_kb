# Runtime image for the CML Application (gateway + UI) and the CML Jobs (eval, ontology, migrations).
# NOT built in the local prototype (no Docker by design). Before first use:
#   1. set BASE_IMAGE to the bank's approved CML ML Runtime (Python 3.11) and pin it by digest;
#   2. build behind the bank's package mirrors (PIP_INDEX_URL / npm registry), never the public internet;
#   3. scan the image and record its digest in VERSIONS.md.
ARG BASE_IMAGE=REPLACE_WITH_APPROVED_CML_ML_RUNTIME_PY311@sha256:REPLACE
FROM ${BASE_IMAGE}

# uv, pinned (same version as the prototype's VERSIONS.md)
ARG UV_VERSION=0.13.6
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /opt/kb
COPY pyproject.toml uv.lock ./
COPY infra/local/graphify/pyproject.toml infra/local/graphify/uv.lock infra/local/graphify/
COPY infra/local/ragas/pyproject.toml infra/local/ragas/uv.lock infra/local/ragas/
# locked environments only: --locked fails the build if uv.lock and pyproject.toml disagree
RUN uv sync --locked --no-dev --no-group synthetic \
 && uv sync --locked --project infra/local/graphify \
 && uv sync --locked --project infra/local/ragas

COPY src/ src/
COPY config/ config/
COPY shapes/ shapes/
COPY infra/local/graphify/guard/ infra/local/graphify/guard/
COPY infra/local/ragas/score.py infra/local/ragas/
COPY tests/eval/ tests/eval/
# the UI is built in CI (npm ci && npm run build) and copied in; the gateway serves ui/dist
COPY ui/dist/ ui/dist/

ENV KB_ENV=cml \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    RAGAS_DO_NOT_TRACK=true \
    PYTHONUNBUFFERED=1

# no secrets in the image: they come from the CML project's secret store at run time
USER cdsw
# CML Application entrypoint (port from CML's CDSW_APP_PORT); Jobs override the command
CMD ["sh", "-c", "uv run --no-sync uvicorn kb.gateway.app:create_app --factory --host 127.0.0.1 --port ${CDSW_APP_PORT:-8090}"]
